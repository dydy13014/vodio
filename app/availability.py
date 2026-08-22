"""Vérification de disponibilité des sources via AIOStreams (interne).

Pour chaque titre, interroge AIOStreams avec la config du compte dydy (prod) —
il agrège TOUTES les sources (WAStream, StreamFusion, Frenchio, Wacustom…).
Un titre est 🧲 (dispo, téléchargeable) dès qu'au moins une source **en
cache instantané** atteint la résolution minimale `min_res` (défaut 720p) ;
⚡ s'ajoute en plus quand cette confirmation vient spécifiquement du cache
mutualisé **Lumio** (signal le plus fiable — cf. `_is_lumio` plus bas) ;
sinon ⏳ (aucune source, ou uniquement du CAM/TS/basse qualité/non-caché sans
résolution fiable). Les torrents non mis en cache (⏳) sont ignorés pour le
calcul de la résolution : leur nom n'a jamais été vérifié par un
téléchargement réel et peut mentir sur la qualité (ex. torrent annoncé
"1080p BluRay" pour un film sorti au cinéma la semaine précédente — un vrai
BluRay n'existe pas encore à ce stade). Les DDL sont quasi toujours en cache
instantané (lien direct, pas de notion de cache) et ne sont donc pas
pénalisés par cette règle.
En cas d'erreur (timeout, config invalide) le nom reste sans badge — inconnu
n'est pas indisponible. Fan-out AIOStreams ~10-30 s : concurrence limitée à 3,
uniquement au refresh quotidien.
"""
import asyncio
import datetime
import logging
import re
from typing import Awaitable, Callable

import httpx

from . import alldebrid, tmdb, wacustom
try:
    from . import _private_cache_check as _extra_check
except ImportError:
    _extra_check = None

log = logging.getLogger("vodio.availability")


def stream_resolution(text: str) -> int:
    """Extrait la résolution (en lignes) du nom/description d'un stream, 0 sinon.

    La résolution vit soit dans le nom du stream (« … 1080p »), soit dans le nom
    de fichier (« …2160p.WEBRip », « HDLIGHT.1080p », « 4Klight »). Pas de tag
    détecté = 0 → traité comme basse qualité (typique des CAM/TS).
    """
    t = text.lower()
    if re.search(r"2160[pi]|\buhd\b|\b4k\b|4klight", t):
        return 2160
    if re.search(r"1440[pi]", t):
        return 1440
    if re.search(r"1080[pi]", t):
        return 1080
    if re.search(r"720[pi]", t):
        return 720
    if re.search(r"(480|576)[pi]", t):
        return 480
    return 0


_BADGE_RE = re.compile(r"^[✅⏳⚡🧲]+\s*")


def _strip(name: str) -> str:
    return _BADGE_RE.sub("", name)


def _badge_prefix(available: bool, is_lumio: bool = False) -> str:
    """🧲 = dispo (téléchargeable) ; ⚡ s'ajoute en plus quand la confirmation
    vient spécifiquement du cache mutualisé Lumio (jamais ⚡ seul — toujours
    accompagné de 🧲, l'un n'exclut pas l'autre)."""
    if not available:
        return "⏳ "
    return "🧲⚡ " if is_lumio else "🧲 "


def _is_available_name(name: str) -> bool:
    return name.startswith("🧲")


_SIZE_RE = re.compile(r"([\d.,]+)\s*(GB|MB|TB)", re.IGNORECASE)

# Taille max plausible (Go) par résolution — généreux pour couvrir les vrais
# REMUX/BluRay, mais rejette les torrents factices/empoisonnés (ex. un "1080p"
# annoncé à 231 Go pour Toy Story 5, trouvé le 2026-07-19 sur Tr4ker : un vrai
# film de ~100 min en 1080p ne dépasse jamais un ordre de grandeur pareil).
_MAX_PLAUSIBLE_GB = {480: 6, 720: 15, 1080: 40, 1440: 60, 2160: 100}


def _parse_size_gb(text: str) -> float | None:
    m = _SIZE_RE.search(text)
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    unit = m.group(2).upper()
    if unit == "TB":
        return num * 1024
    if unit == "MB":
        return num / 1024
    return num


# Tags scène explicites pour une captation salle (caméra ou télésynchro),
# jamais une vraie sortie WEB-DL/BluRay quelle que soit la résolution
# annoncée dans le nom. Détecté directement plutôt que de compter sur le
# délai `MIN_DAYS_FOR_ACTIVE_CHECK` : ce délai suppose une fenêtre VOD de
# 45-90j, trop courte pour un blockbuster (cas réel 2026-08-22 — "Vaiana" et
# "Spider-Man: Brand New Day", déjà >45j après leur sortie salle mais dont
# les seules sources "cache=1080p" trouvées étaient explicitement taguées
# CAM/HDTS dans leur nom de fichier — 2ᵉ occurrence du même piège que celui
# qui avait justifié le délai le 2026-07-19).
_CAM_RE = re.compile(
    r"\b(CAM|HDCAM|CAMRip|TS|HDTS|TC|HDTC|TELESYNC|TELECINE|SCREENER|DVDSCR|SCR)\b",
    re.IGNORECASE,
)


def _is_cam(text: str) -> bool:
    return bool(_CAM_RE.search(text))


def _is_cached(text: str) -> bool:
    """Un flux ⚡ (cache instantané) a déjà été réellement téléchargé par
    quelqu'un — sa qualité annoncée est donc fiable. Un flux ⏳ (torrent non
    caché) n'a jamais été vérifié : son nom peut prétendre n'importe quoi.
    Les DDL (liens directs) n'ont pas de notion de cache et sont marqués ⚡
    par convention par AIOStreams — ils ne sont donc pas pénalisés ici.
    Un flux dont la source est "Lumio" a déjà été vérifié en cache par leur
    base mutualisée (cf. module privé Wacustom, 2026-07-20) — traité comme
    cached même si Wacustom lui-même ne peut plus jamais afficher ⚡ pour un
    torrent depuis qu'AllDebrid a retiré son endpoint de vérification
    instantanée (`/magnet/instant`, 404 depuis courant 2026). Un CAM/TS
    explicitement tagué (cf. `_is_cam`) n'est JAMAIS considéré caché, même
    marqué ⚡ — le cache confirme que le fichier existe, pas qu'il vaut la
    peine d'être regardé."""
    if _is_cam(text):
        return False
    return "⚡" in text or "🌐 Lumio" in text


def _is_lumio(text: str) -> bool:
    """Contrairement à `_is_cached` (n'importe quelle confirmation de cache),
    identifie spécifiquement une source Lumio — le signal le plus fiable
    (base mutualisée vérifiée), affiché en plus du badge 🧲 générique."""
    return "🌐 Lumio" in text


async def _check_one(
    client: httpx.AsyncClient, base: str, config: str, meta: dict,
    sem: asyncio.Semaphore, min_res: int,
) -> None:
    async with sem:
        # Série : on sonde S01E01 comme indicateur de disponibilité globale.
        if meta.get("type") == "series":
            path = f"stream/series/{meta['id']}:1:1.json"
        else:
            path = f"stream/movie/{meta['id']}.json"
        try:
            resp = await client.get(f"{base}/{config}/{path}")
            resp.raise_for_status()
            streams = resp.json().get("streams", [])
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("check sources %s (%s) : %s", meta["id"], meta["name"], exc)
            return
        best = 0
        lumio_ok = False
        for s in streams:
            name = s.get("name", "")
            text = f"{name} {s.get('description') or s.get('title') or ''}"
            if not _is_cached(text):
                continue
            res = stream_resolution(text)
            best = max(best, res)
            if res >= min_res and _is_lumio(text):
                lumio_ok = True
        # Pas d'appel à la source externe ici, volontairement : ce chemin sert
        # au catalogue AlloCiné (dizaines de titres à chaque refresh), pour un
        # simple badge sur des films qu'on ne regarde pas forcément. Son quota
        # est trop bas pour ça — il est réservé à la watchlist, qui est courte
        # et correspond à ce que l'utilisateur attend vraiment (cf.
        # _check_one_watchlist).
        available = best >= min_res
        meta["name"] = _badge_prefix(available, lumio_ok) + _strip(meta["name"])
        log.info(
            "%s %s : %d sources, meilleure réso %dp", meta["id"], meta["name"],
            len(streams), best,
        )


# Délai avant le 2ᵉ passage : le temps qu'AIOStreams ait fini de scraper les
# addons sources après le 1ᵉʳ passage (cache froid → résultats partiels sinon).
RETRY_DELAY_S = 30


async def add_availability_badges(
    base: str, config: str, metas: list[dict], min_res: int = 720
) -> None:
    sem = asyncio.Semaphore(3)
    async with httpx.AsyncClient(timeout=120) as client:
        # Passage 1 : chauffe le cache AIOStreams + badge provisoire.
        await asyncio.gather(
            *(_check_one(client, base, config, m, sem, min_res) for m in metas)
        )
        # Passage 2 : re-vérifie uniquement ceux qui ressortent indisponibles
        # (un vrai indispo le restera ; un faux ⏳ dû au cache froid passe 🧲).
        pending = [m for m in metas if not _is_available_name(m["name"])]
        if pending:
            await asyncio.sleep(RETRY_DELAY_S)
            await asyncio.gather(
                *(_check_one(client, base, config, m, sem, min_res) for m in pending)
            )
    available = sum(1 for m in metas if _is_available_name(m["name"]))
    log.info("badges : %d/%d titres dispo en ≥%dp", available, len(metas), min_res)


# En dessous de ce délai depuis la sortie, aucune source WEB-DL/BluRay/WEBRip/
# HDRip légitime ne peut exister — un torrent qui le prétend est presque
# certainement un CAM déguisé (cas réel : Minions & Monsters, Toy Story 5,
# Vaiana — sortis 11 à 32 jours plus tôt, marqués ✅ à tort le 2026-07-19 malgré
# le filtre de taille, tailles de CAM compressés plausibles pour du "1080p").
# Un vrai WEB-DL/digital arrive généralement 45-90j après la sortie salle.
MIN_DAYS_FOR_ACTIVE_CHECK = 45


def _days_since_release(release_date: str | None) -> int | None:
    if not release_date:
        return None
    try:
        d = datetime.date.fromisoformat(release_date[:10])
    except ValueError:
        return None
    return (datetime.date.today() - d).days


_SOURCE_RE = re.compile(r"🌐\s*([A-Za-z0-9_]+)")

# Zilean est un index de cache DMM (torrents vus un jour par un autre compte
# debrid, pas forcément encore vivants) plutôt qu'un tracker en direct — cas
# réel (2026-07-20) : son candidat "le plus petit" pour une série s'est révélé
# mort (0 seeder) alors qu'un pack C411/Tr4ker plus gros mais bien vivant
# existait. On tente donc les sources non-Zilean en premier, quelle que soit
# leur taille, et Zilean seulement en dernier recours.
def _is_zilean(text: str) -> bool:
    m = _SOURCE_RE.search(text)
    return bool(m) and m.group(1).lower() == "zilean"


def _scan_streams(
    streams: list[dict], min_res: int, content_type: str = "movie"
) -> tuple[int, str | None, list[tuple], bool]:
    """Analyse les flux Wacustom d'un titre. Renvoie (meilleure résolution
    déjà en cache, magnet du meilleur flux TORRENT déjà en cache (ou None),
    liste triée des candidats torrent non-cachés plausibles, si la
    confirmation de cache atteignant `min_res` vient spécifiquement de
    Lumio — sert au badge ⚡ additionnel, cf. `_badge_prefix`). Un candidat =
    (source Zilean ?, taille Go, résolution, lien magnet), trié non-Zilean
    d'abord puis du plus petit au plus gros dans chaque groupe. Le magnet du
    flux caché sert au téléchargement direct (VODIO n'a pas forcément
    lui-même déclenché ce cache — ex. déjà caché par un autre compte
    AllDebrid) — un titre peut être ✅ via une source DDL déjà cachée alors
    qu'aucun magnet n'est disponible : `best_cached` (résolution, sert au
    badge) suit alors la meilleure source toutes confondues, tandis que
    `best_cached_magnet` (téléchargement) ne retient que la meilleure parmi
    celles qui sont un vrai magnet. Écarte les torrents dont la taille est
    incohérente avec la résolution annoncée (factices/empoisonnés) —
    **films uniquement** : les seuils supposent un long-métrage (~90-150
    min). Un épisode de série peut légitimement peser 20x moins et un pack
    de saison 10-20x plus (cas réel 2026-07-20, pack C411 19-22 Go rejeté à
    tort par le seuil 720p=15 Go pensé pour un film) — pas de seuils fiables
    pour ces deux cas, donc pas de filtre du tout pour une série (même choix
    que le filtre équivalent côté Wacustom). Partagé entre le calcul des
    badges, le pré-cache et le téléchargement direct."""
    best_cached = 0
    best_cached_magnet = None
    best_cached_magnet_res = 0
    best_cached_is_lumio = False
    candidates = []
    for s in streams:
        name = s.get("name", "")
        text = f"{name} {s.get('description') or s.get('title') or ''}"
        # Zilean (recherche par hash DMM croisant des trackers publics tiers,
        # sans vérif de contenu) exclu totalement, pas juste déprioritisé :
        # faux positifs réels constatés le 2026-08-22 (deux torrents
        # "Vaiana"/"Spider-Man" téléchargés via ce chemin, contenu réel
        # complètement différent — mauvais hash associé au mauvais titre).
        # Un score de confiance ne suffit pas à protéger le téléchargement
        # automatique (`best_cached_magnet` ci-dessous) : mieux vaut l'exclure
        # ici, à la source, plutôt que de compter sur un tri en aval.
        if _is_zilean(text) or _is_cam(text):
            continue
        res = stream_resolution(text)
        if _is_cached(text):
            best_cached = max(best_cached, res)
            if res >= min_res and _is_lumio(text):
                best_cached_is_lumio = True
            link = wacustom.extract_link(s.get("url", ""))
            if link and link.startswith("magnet:") and res > best_cached_magnet_res:
                best_cached_magnet_res = res
                best_cached_magnet = link
            continue
        if res < min_res:
            continue
        size_gb = _parse_size_gb(text)
        if content_type == "movie" and (size_gb is None or size_gb > _MAX_PLAUSIBLE_GB.get(res, 40)):
            continue
        link = wacustom.extract_link(s.get("url", ""))
        if link and link.startswith("magnet:"):
            candidates.append((size_gb if size_gb is not None else float("inf"), res, link))
    candidates.sort(key=lambda c: c[0])
    return best_cached, best_cached_magnet, candidates, best_cached_is_lumio


async def find_precache_candidate(
    wacustom_base: str, wacustom_config: str, tmdb_api_key: str,
    meta: dict, min_res: int, season: int = 1, episode: int = 1,
    exclude_magnets: set[str] | None = None,
    verify_fn: Callable[[str], Awaitable[bool]] | None = None,
) -> dict:
    """Pour le pré-cache à la demande : renvoie le meilleur candidat torrent
    à envoyer à AllDebrid pour un titre non-caché. {"status", "magnet"?, ...}.
    status : "cached" (déjà dispo — "magnet" fourni si connu ; sinon la
    source cachée est un DDL et "fallback_magnet" propose le meilleur
    candidat torrent, non confirmé caché mais plausible pour un titre déjà
    disponible ailleurs — utile pour le téléchargement direct, inutile pour
    le seul badge), "candidate" (magnet à précharger), "too_recent" (sorti
    trop tôt, sources = CAM probables), "none" (aucune source plausible).
    `season`/`episode` ignorés pour un film. `exclude_magnets` : liens déjà
    essayés et confirmés morts (repli automatique sur le candidat suivant,
    cf. main._refresh) — jamais re-proposés même s'ils redeviennent "le plus
    petit" candidat plausible."""
    streams = await wacustom.get_streams(
        wacustom_base, wacustom_config, meta["id"], meta.get("type", "movie"), season, episode
    )
    best_cached, best_cached_magnet, candidates, _ = _scan_streams(streams, min_res, meta.get("type", "movie"))
    if best_cached >= min_res:
        result = {"status": "cached", "magnet": best_cached_magnet}
        if not best_cached_magnet and candidates:
            # Aucun des candidats n'est confirmé caché (Wacustom ne l'a pas
            # signalé) — plusieurs essayés au téléchargement, un autre que le
            # plus petit peut être celui réellement en cache partagé AllDebrid
            # (cas réel : le plus petit candidat 720p pas caché, un 1080p plus
            # gros l'était).
            result["fallback_magnets"] = [c[2] for c in candidates[:5]]
        return result
    if exclude_magnets:
        candidates = [c for c in candidates if c[2] not in exclude_magnets]
    if not candidates:
        return {"status": "none"}

    # Même garde-fou anti-CAM que le calcul des badges : ne pas précharger un
    # torrent d'un film trop récent (aucune vraie source WEB-DL/BluRay possible
    # → ce serait un CAM déguisé, taille plausible mais contenu pourri). Même
    # exemption "trackers vérifiés" que `_check_one_watchlist` (cf. son
    # commentaire) : un titre ajouté manuellement mais réellement présent sur
    # C411/Tr4ker/V3X n'a pas à attendre.
    days = _days_since_release(meta.get("release_date"))
    if days is None:
        days = _days_since_release(
            await tmdb.get_release_date(tmdb_api_key, meta["id"], meta.get("type", "movie"))
        )
    is_series = meta.get("type") == "series"
    if days is not None and days < MIN_DAYS_FOR_ACTIVE_CHECK:
        verified = meta.get("source") == "c411"
        if not verified and not is_series and verify_fn is not None:
            try:
                verified = await verify_fn(meta["id"])
            except Exception:
                log.exception("%s : échec vérification tracker live", meta["id"])
        if not verified:
            return {"status": "too_recent", "days": days}

    size_gb, res, magnet = candidates[0]
    return {
        "status": "candidate", "magnet": magnet,
        "size_gb": round(size_gb, 2) if size_gb != float("inf") else None,
        "resolution": res,
    }


async def find_lumio_direct_link(meta: dict) -> dict | None:
    """Repli pour le téléchargement direct (film uniquement) quand Wacustom
    ne renvoie aucun candidat exploitable : Lumio a parfois déjà un lien
    pré-résolu par leur propre infrastructure debrid (signal utilisé pour le
    badge ✅⚡, cf. `_check_one_watchlist`) même quand Wacustom lui-même n'a
    plus aucune source — voir `_private_cache_check.get_direct_link`.
    {"url", "filename"} ou None."""
    if _extra_check is None:
        return None
    return await _extra_check.get_direct_link(meta["id"])


async def find_lumio_candidates(meta: dict) -> list[dict]:
    """Variante de `find_lumio_direct_link` pour la liste « Sources » : liste
    plusieurs candidats Lumio sans les résoudre (0 appel supplémentaire),
    laissant l'utilisateur choisir lequel résoudre — voir
    `_private_cache_check.list_candidates`."""
    if _extra_check is None:
        return []
    return await _extra_check.list_candidates(meta["id"])


async def resolve_lumio_link(playback_url: str) -> dict | None:
    """Résout à la demande un candidat renvoyé par `find_lumio_candidates`."""
    if _extra_check is None:
        return None
    return await _extra_check.resolve_direct_link(playback_url)


# Depuis le fix de `wacustom.extract_link` (2026-08-22), Wacustom renvoie à nouveau
# ses dizaines de candidats bruts par tracker (Zilean à lui seul en fournit
# souvent 15+ quasi identiques pour la même release) — une liste "Sources"
# à choix manuel n'a aucun intérêt à en montrer autant, ça noie le choix
# plutôt que de l'éclairer. On garde peu de candidats par (source,
# résolution), et on remonte les caches confirmés en premier (dispo tout de
# suite) avant la taille.
_MAX_PER_BUCKET = 2
_MAX_SOURCES_LISTED = 8

# Nos propres trackers (clé API à nous, curation Unit3d) — à privilégier sur
# les agrégateurs publics. Zilean (recherche par hash DMM croisant des
# trackers publics tiers, sans vérif de contenu) est totalement EXCLU (pas
# juste déprioritisé) — faux positifs confirmés en réel le 2026-08-22 : deux
# torrents ("Vaiana", "Spider-Man: Brand New Day") dont le contenu réellement
# téléchargé était un titre complètement différent (mauvais hash associé au
# mauvais nom). Trop dangereux pour rester même en dernier choix. Filtré à la
# source dans `_scan_streams` (badges + précache + DL auto) et ici.
_TRUSTED_SOURCES = {"C411", "Tr4ker", "V3X"}

# Plafond de résolution pour la liste "Sources" : au-delà, le fichier passe
# par le relais MediaFlow (débit plafonné) —
# un 2160p n'est ni téléchargeable ni lisible dans un temps raisonnable par
# ce chemin. 1080p reste large pour ce plafond.
_MAX_RES_LISTED = 1080


def _source_rank(source: str) -> int:
    if source in _TRUSTED_SOURCES:
        return 0
    return 1


async def list_sources(wacustom_base: str, wacustom_config: str, meta: dict, min_res: int) -> list[dict]:
    """Liste des meilleures sources trouvées par Wacustom pour un titre (pas
    forcément toutes — cf. `_MAX_PER_BUCKET`/`_MAX_SOURCES_LISTED` — ni
    seulement la meilleure retenue par `find_precache_candidate`) — pour un
    affichage détaillé façon Ludio, où l'utilisateur choisit lui-même la
    source à débrider plutôt que de laisser VODIO décider automatiquement.
    Chaque entrée : {"source", "title", "size_gb", "resolution", "cached",
    "link"} — `link` est un magnet ou une URL DDL selon la source."""
    streams = await wacustom.get_streams(
        wacustom_base, wacustom_config, meta["id"], meta.get("type", "movie")
    )
    candidates = []
    for s in streams:
        name = s.get("name", "")
        title = s.get("description") or s.get("title") or ""
        text = f"{name} {title}"
        if _is_zilean(text) or _is_cam(text):
            continue
        link = wacustom.extract_link(s.get("url", ""))
        # Uniquement les magnets : seuls ceux-là ont un bouton DL côté page
        # web (poussable directement vers AllDebrid) — un lien hébergeur
        # (1Fichier, Alldebrid share...) n'offre que "Copier", jugé moins
        # utile dans cette liste de choix rapide.
        if not link or not link.startswith("magnet:"):
            continue
        res = stream_resolution(text)
        if res < min_res or res > _MAX_RES_LISTED:
            continue
        m = _SOURCE_RE.search(text)
        candidates.append({
            "source": m.group(1) if m else "Wacustom",
            "title": title.strip()[:140] or name.strip()[:140],
            "size_gb": _parse_size_gb(text),
            "resolution": res,
            "cached": _is_cached(text),
            "link": link,
        })

    def sort_key(c: dict) -> tuple:
        return (
            not c["cached"],
            _source_rank(c["source"]),
            c["size_gb"] if c["size_gb"] is not None else float("inf"),
        )

    candidates.sort(key=sort_key)

    buckets: dict[tuple, list[dict]] = {}
    out = []
    for c in candidates:
        key = (c["source"], c["resolution"])
        bucket = buckets.setdefault(key, [])
        if len(bucket) >= _MAX_PER_BUCKET:
            continue
        bucket.append(c)
        out.append(c)

    out.sort(key=lambda c: (-c["resolution"], sort_key(c)))
    return out[:_MAX_SOURCES_LISTED]


async def _check_one_watchlist(
    wacustom_base: str, wacustom_config: str, alldebrid_api_key: str,
    tmdb_api_key: str, meta: dict, min_res: int,
    verify_fn: Callable[[str], Awaitable[bool]] | None = None,
) -> None:
    streams = await wacustom.get_streams(
        wacustom_base, wacustom_config, meta["id"], meta.get("type", "movie")
    )

    best_cached, _, candidates, best_cached_is_lumio = _scan_streams(streams, min_res, meta.get("type", "movie"))

    is_series = meta.get("type") == "series"

    # Garde-fou anti-CAM : sous ce délai depuis la sortie, aucune vraie
    # source WEB-DL/BluRay légitime ne peut exister pour un FILM (fenêtre
    # salle → VOD, 45-90j) — un flux qui prétend l'être est presque
    # certainement un CAM déguisé (cas réel : Minions & Monsters, Toy Story
    # 5, Vaiana). Ce raisonnement ne vaut PAS pour une série : ses épisodes
    # sortent en WEB-DL le jour même de la diffusion, donc `release_date`
    # (= date du premier épisode) ne dit rien sur la légitimité d'un cache
    # trouvé pour un épisode récent. Régression du 2026-08-15 (revue GLM,
    # étendait le garde-fou à best_cached/extra_hit) corrigée le même jour
    # (revue Qwen) : le garde-fou étendu ne s'applique donc qu'aux films —
    # le check AllDebrid actif, lui, reste protégé pour les deux types
    # (comportement d'origine, jamais remis en cause). Si la date de sortie
    # reste indéterminable (days=None), on n'ajoute ni ne retire de
    # protection.
    days = _days_since_release(meta.get("release_date"))
    if days is None:
        days = _days_since_release(
            await tmdb.get_release_date(tmdb_api_key, meta["id"], meta.get("type", "movie"))
        )
    # Un titre ajouté depuis le catalogue Nouveautés Torrent (C411) a déjà été
    # filtré à l'ingestion sur un vrai tag de résolution dans le nom de la
    # release (cf. c411_feed.filter_relevant) — contrairement à un titre
    # AlloCiné/recherche manuelle, dont on n'a encore aucune preuve de source
    # réelle. Choix explicite de l'utilisateur (2026-08-21) : exempter ces
    # titres du délai anti-CAM plutôt que de leur imposer la même prudence
    # qu'un titre sans aucun signal.
    from_verified_source = meta.get("source") == "c411"
    # Un titre ajouté MANUELLEMENT (recherche watchlist) n'a jamais cette
    # preuve statique — mais peut très bien exister sur C411/Tr4ker/V3X quand
    # même (cas réel 2026-08-22 : "72 heures", ajouté via la recherche,
    # bloqué "En attente ~12j" alors qu'une vraie release WEB-DL 1080p
    # existait déjà sur Tr4ker). Un appel par film est acceptable ici — la
    # watchlist est courte et cette vérification ne se déclenche que pour un
    # film récent pas déjà exempté, jamais pour le catalogue bulk (cf.
    # `c411_feed.search_by_imdb`, qui documente pourquoi un appel/film y
    # serait dangereux).
    would_be_too_recent = (
        days is not None and days < MIN_DAYS_FOR_ACTIVE_CHECK and not from_verified_source
    )
    if would_be_too_recent and not is_series and verify_fn is not None:
        try:
            if await verify_fn(meta["id"]):
                from_verified_source = True
        except Exception:
            log.exception("%s : échec vérification tracker live", meta["id"])
    too_recent = days is not None and days < MIN_DAYS_FOR_ACTIVE_CHECK and not from_verified_source
    too_recent_for_cached_signal = too_recent and not is_series

    available = best_cached >= min_res and not too_recent_for_cached_signal
    checked_alldebrid = False
    extra_hit = False

    # Signal externe (rapide, gratuit) avant de consommer un check AllDebrid
    # actif : si déjà confirmé caché ailleurs, inutile de vérifier nous-mêmes.
    if not available and not too_recent_for_cached_signal and _extra_check is not None:
        try:
            extra_hit = await _extra_check.is_cached(meta["id"], meta.get("type", "movie"), 1 if is_series else None, 1 if is_series else None)
        except Exception:
            pass
        available = available or extra_hit

    # Rien de déjà caché en ≥min_res : on vérifie/déclenche activement le
    # candidat torrent le plus plausible (le plus petit parmi ceux qui ont
    # passé le filtre de taille) — inutile si trop récent, cf. garde-fou
    # ci-dessus (ici, films ET séries : contrairement au cache déjà
    # confirmé, un candidat non vérifié peut toujours être un CAM déguisé
    # même pour une série).
    if not available and candidates and not too_recent:
        checked_alldebrid = True
        available = await alldebrid.is_cached(alldebrid_api_key, candidates[0][2])
    elif not available and too_recent:
        log.info(
            "%s : check ignoré (sorti il y a %s j < %dj, CAM probable)",
            meta["id"], days, MIN_DAYS_FOR_ACTIVE_CHECK,
        )

    # ⚡ additionnel si la confirmation vient de Lumio — via le scan Wacustom
    # (best_cached_is_lumio) ou le signal externe direct (extra_hit, qui
    # interroge littéralement Lumio) ; jamais via le check AllDebrid actif,
    # qui n'a rien de spécifique à Lumio.
    is_lumio = best_cached_is_lumio or extra_hit
    meta["name"] = _badge_prefix(available, is_lumio) + _strip(meta["name"])
    # ETA affichée côté page web (« dispo estimée dans ~N j ») — seulement
    # pour un film indisponible à cause du garde-fou anti-CAM lui-même : pour
    # une série, un ⏳ ne veut pas dire "trop tôt" de la même façon (cf.
    # garde-fou scindé plus haut), une ETA y serait trompeuse.
    if not available and not is_series and days is not None and days < MIN_DAYS_FOR_ACTIVE_CHECK:
        meta["eta_days"] = MIN_DAYS_FOR_ACTIVE_CHECK - days
    else:
        meta.pop("eta_days", None)
    log.info(
        "%s %s : %d sources (direct Wacustom), cache=%dp, %d candidat(s) plausible(s)%s%s",
        meta["id"], meta["name"], len(streams), best_cached, len(candidates),
        " + check AllDebrid" if checked_alldebrid else "",
        " + signal externe" if extra_hit else "",
    )


async def add_availability_badges_watchlist(
    wacustom_base: str, wacustom_config: str, alldebrid_api_key: str,
    tmdb_api_key: str, metas: list[dict], min_res: int = 720,
    verify_fn: Callable[[str], Awaitable[bool]] | None = None,
) -> None:
    """Variante watchlist : interroge Wacustom directement (pas AIOStreams,
    qui tronque parfois les résultats — cf. JOURNAL) et vérifie activement
    via AllDebrid le meilleur candidat torrent non-caché trouvé (si le film
    est sorti depuis assez longtemps, cf. MIN_DAYS_FOR_ACTIVE_CHECK). Réservé
    à la watchlist (liste courte, choisie par l'utilisateur) pour ne pas
    multiplier les appels à Wacustom/AllDebrid sur le catalogue quotidien.
    `verify_fn(imdb_id) -> bool` (optionnel) : vérification live sur les
    trackers Torznab pour lever le garde-fou anti-CAM sur un titre ajouté
    manuellement mais réellement sorti (cf. `_check_one_watchlist`)."""
    if not alldebrid_api_key:
        log.warning("ALLDEBRID_API_KEY absente, check watchlist en mode passif uniquement")
    await asyncio.gather(*(
        _check_one_watchlist(wacustom_base, wacustom_config, alldebrid_api_key, tmdb_api_key, m, min_res, verify_fn)
        for m in metas
    ))
    available = sum(1 for m in metas if _is_available_name(m["name"]))
    log.info("watchlist badges : %d/%d titres dispo en ≥%dp", available, len(metas), min_res)
