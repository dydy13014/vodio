"""Vérification de disponibilité des sources via AIOStreams (interne).

Pour chaque titre, interroge AIOStreams avec la config du compte dydy (prod) —
il agrège TOUTES les sources (WAStream, StreamFusion, Frenchio, Wacustom…).
Un titre est ✅ seulement si au moins une source **en cache instantané** (⚡)
atteint la résolution minimale `min_res` (défaut 720p) ; sinon ⏳ (aucune
source, ou uniquement du CAM/TS/basse qualité/non-caché sans résolution
fiable). Les torrents non mis en cache (⏳) sont ignorés pour le calcul de la
résolution : leur nom n'a jamais été vérifié par un téléchargement réel et
peut mentir sur la qualité (ex. torrent annoncé "1080p BluRay" pour un film
sorti au cinéma la semaine précédente — un vrai BluRay n'existe pas encore à
ce stade). Les DDL sont quasi toujours en cache instantané (lien direct, pas
de notion de cache) et ne sont donc pas pénalisés par cette règle.
En cas d'erreur (timeout, config invalide) le nom reste sans badge — inconnu
n'est pas indisponible. Fan-out AIOStreams ~10-30 s : concurrence limitée à 3,
uniquement au refresh quotidien.
"""
import asyncio
import datetime
import logging
import re

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


_BADGE_RE = re.compile(r"^[✅⏳⚡]+\s*")


def _strip(name: str) -> str:
    return _BADGE_RE.sub("", name)


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
    instantanée (`/magnet/instant`, 404 depuis courant 2026)."""
    return "⚡" in text or "🌐 Lumio" in text


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
        for s in streams:
            name = s.get("name", "")
            text = f"{name} {s.get('description') or s.get('title') or ''}"
            if not _is_cached(text):
                continue
            best = max(best, stream_resolution(text))
        # Pas d'appel à la source externe ici, volontairement : ce chemin sert
        # au catalogue AlloCiné (dizaines de titres à chaque refresh), pour un
        # simple badge sur des films qu'on ne regarde pas forcément. Son quota
        # est trop bas pour ça — il est réservé à la watchlist, qui est courte
        # et correspond à ce que l'utilisateur attend vraiment (cf.
        # _check_one_watchlist).
        available = best >= min_res
        meta["name"] = ("✅⚡ " if available else "⏳ ") + _strip(meta["name"])
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
        # (un vrai indispo le restera ; un faux ⏳ dû au cache froid passe ✅).
        pending = [m for m in metas if not m["name"].startswith("✅")]
        if pending:
            await asyncio.sleep(RETRY_DELAY_S)
            await asyncio.gather(
                *(_check_one(client, base, config, m, sem, min_res) for m in pending)
            )
    available = sum(1 for m in metas if m["name"].startswith("✅"))
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


def _scan_streams(streams: list[dict], min_res: int, content_type: str = "movie") -> tuple[int, str | None, list[tuple]]:
    """Analyse les flux Wacustom d'un titre. Renvoie (meilleure résolution
    déjà en cache, magnet du meilleur flux TORRENT déjà en cache (ou None),
    liste triée des candidats torrent non-cachés plausibles). Un candidat =
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
    candidates = []
    for s in streams:
        name = s.get("name", "")
        text = f"{name} {s.get('description') or s.get('title') or ''}"
        res = stream_resolution(text)
        if _is_cached(text):
            best_cached = max(best_cached, res)
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
            candidates.append((_is_zilean(text), size_gb if size_gb is not None else float("inf"), res, link))
    candidates.sort(key=lambda c: (c[0], c[1]))
    return best_cached, best_cached_magnet, candidates


async def find_precache_candidate(
    wacustom_base: str, wacustom_config: str, tmdb_api_key: str,
    meta: dict, min_res: int, season: int = 1, episode: int = 1,
    exclude_magnets: set[str] | None = None,
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
    best_cached, best_cached_magnet, candidates = _scan_streams(streams, min_res, meta.get("type", "movie"))
    if best_cached >= min_res:
        result = {"status": "cached", "magnet": best_cached_magnet}
        if not best_cached_magnet and candidates:
            # Aucun des candidats n'est confirmé caché (Wacustom ne l'a pas
            # signalé) — plusieurs essayés au téléchargement, un autre que le
            # plus petit peut être celui réellement en cache partagé AllDebrid
            # (cas réel : le plus petit candidat 720p pas caché, un 1080p plus
            # gros l'était).
            result["fallback_magnets"] = [c[3] for c in candidates[:5]]
        return result
    if exclude_magnets:
        candidates = [c for c in candidates if c[3] not in exclude_magnets]
    if not candidates:
        return {"status": "none"}

    # Même garde-fou anti-CAM que le calcul des badges : ne pas précharger un
    # torrent d'un film trop récent (aucune vraie source WEB-DL/BluRay possible
    # → ce serait un CAM déguisé, taille plausible mais contenu pourri).
    days = _days_since_release(meta.get("release_date"))
    if days is None:
        days = _days_since_release(
            await tmdb.get_release_date(tmdb_api_key, meta["id"], meta.get("type", "movie"))
        )
    if days is not None and days < MIN_DAYS_FOR_ACTIVE_CHECK:
        return {"status": "too_recent", "days": days}

    _, size_gb, res, magnet = candidates[0]
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


async def list_sources(wacustom_base: str, wacustom_config: str, meta: dict, min_res: int) -> list[dict]:
    """Liste brute de toutes les sources trouvées par Wacustom pour un titre
    (pas seulement la meilleure retenue par `find_precache_candidate`) — pour
    un affichage détaillé façon Ludio, où l'utilisateur choisit lui-même la
    source à débrider plutôt que de laisser VODIO décider automatiquement.
    Chaque entrée : {"source", "title", "size_gb", "resolution", "cached",
    "link"} — `link` est un magnet ou une URL DDL selon la source."""
    streams = await wacustom.get_streams(
        wacustom_base, wacustom_config, meta["id"], meta.get("type", "movie")
    )
    out = []
    for s in streams:
        name = s.get("name", "")
        title = s.get("description") or s.get("title") or ""
        text = f"{name} {title}"
        link = wacustom.extract_link(s.get("url", ""))
        if not link:
            continue
        res = stream_resolution(text)
        if res < min_res:
            continue
        m = _SOURCE_RE.search(text)
        out.append({
            "source": m.group(1) if m else "Wacustom",
            "title": title.strip()[:140] or name.strip()[:140],
            "size_gb": _parse_size_gb(text),
            "resolution": res,
            "cached": _is_cached(text),
            "link": link,
        })
    out.sort(key=lambda c: c["size_gb"] if c["size_gb"] is not None else float("inf"))
    return out


async def _check_one_watchlist(
    wacustom_base: str, wacustom_config: str, alldebrid_api_key: str,
    tmdb_api_key: str, meta: dict, min_res: int,
) -> None:
    streams = await wacustom.get_streams(
        wacustom_base, wacustom_config, meta["id"], meta.get("type", "movie")
    )

    best_cached, _, candidates = _scan_streams(streams, min_res, meta.get("type", "movie"))

    available = best_cached >= min_res
    checked_alldebrid = False
    extra_hit = False

    # Signal externe (rapide, gratuit) avant de consommer un check AllDebrid
    # actif : si déjà confirmé caché ailleurs, inutile de vérifier nous-mêmes.
    if not available and _extra_check is not None:
        is_series = meta.get("type") == "series"
        try:
            extra_hit = await _extra_check.is_cached(meta["id"], meta.get("type", "movie"), 1 if is_series else None, 1 if is_series else None)
        except Exception:
            pass
        available = available or extra_hit

    # Rien de déjà caché en ≥min_res : on vérifie/déclenche activement le
    # candidat torrent le plus plausible (le plus petit parmi ceux qui ont
    # passé le filtre de taille), mais seulement si le film est sorti assez
    # tôt pour qu'une vraie source WEB-DL/BluRay/etc puisse exister — sinon
    # un CAM déguisé passerait quand même le filtre de taille (watchlist
    # uniquement, liste courte, une fois par jour).
    if not available and candidates:
        days = _days_since_release(meta.get("release_date"))
        if days is None:
            days = _days_since_release(
                await tmdb.get_release_date(tmdb_api_key, meta["id"], meta.get("type", "movie"))
            )
        if days is not None and days >= MIN_DAYS_FOR_ACTIVE_CHECK:
            checked_alldebrid = True
            available = await alldebrid.is_cached(alldebrid_api_key, candidates[0][3])
        else:
            log.info(
                "%s : check AllDebrid ignoré (sorti il y a %s j < %dj, CAM probable)",
                meta["id"], days, MIN_DAYS_FOR_ACTIVE_CHECK,
            )

    meta["name"] = ("✅⚡ " if available else "⏳ ") + _strip(meta["name"])
    log.info(
        "%s %s : %d sources (direct Wacustom), cache=%dp, %d candidat(s) plausible(s)%s%s",
        meta["id"], meta["name"], len(streams), best_cached, len(candidates),
        " + check AllDebrid" if checked_alldebrid else "",
        " + signal externe" if extra_hit else "",
    )


async def add_availability_badges_watchlist(
    wacustom_base: str, wacustom_config: str, alldebrid_api_key: str,
    tmdb_api_key: str, metas: list[dict], min_res: int = 720,
) -> None:
    """Variante watchlist : interroge Wacustom directement (pas AIOStreams,
    qui tronque parfois les résultats — cf. JOURNAL) et vérifie activement
    via AllDebrid le meilleur candidat torrent non-caché trouvé (si le film
    est sorti depuis assez longtemps, cf. MIN_DAYS_FOR_ACTIVE_CHECK). Réservé
    à la watchlist (liste courte, choisie par l'utilisateur) pour ne pas
    multiplier les appels à Wacustom/AllDebrid sur le catalogue quotidien."""
    if not alldebrid_api_key:
        log.warning("ALLDEBRID_API_KEY absente, check watchlist en mode passif uniquement")
    await asyncio.gather(*(
        _check_one_watchlist(wacustom_base, wacustom_config, alldebrid_api_key, tmdb_api_key, m, min_res)
        for m in metas
    ))
    available = sum(1 for m in metas if m["name"].startswith("✅"))
    log.info("watchlist badges : %d/%d titres dispo en ≥%dp", available, len(metas), min_res)
