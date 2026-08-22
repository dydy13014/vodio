"""VODIO — addon Stremio « Nouveautés VOD FR » + watchlist personnelle.

Deux catalogues :
- `vodio-new` : nouveautés VOD scrapées d'AlloCiné (refresh 24h).
- `vodio-watchlist` : films ajoutés à la main via la page web `/` (protégée par
  mot de passe), avec badges de disponibilité 🧲/⏳.

Servi derrière Traefik en PathPrefix /vodio (stripprefix) : l'app expose tout à
la racine. Manifest et catalogues Stremio restent publics ; seules les routes
de gestion `/api/*` exigent une authentification.

Multi-utilisateur (réécrit le 2026-08-14) : une seule
page web / une seule URL pour tout le monde, avec un vrai formulaire nom +
mot de passe. `POST /api/login` échange les identifiants contre un jeton de
session (`resolve_session`, 2026-08-15) — plus de chemin distinct par
personne (`/u/<nom>/` retiré) ni de mot de passe renvoyé en clair à chaque
appel. Décision explicite : seul le compte principal
(VODIO_PASSWORD/VODIO_DEFAULT_NAME) est réellement installé dans Stremio,
donc les routes manifest/catalog Stremio restent sur ce seul compte — les
comptes additionnels (VODIO_EXTRA_USERS) n'ont que la page web/API.
"""
import asyncio
import datetime
import hmac
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import alldebrid, availability, c411_feed, changelog, cinema_scraper, notify, scraper, tmdb
from .watchlist import Watchlist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("vodio")

TMDB_API_KEY = os.environ["TMDB_API_KEY"]
ALLOCINE_PAGES = int(os.environ.get("ALLOCINE_PAGES", "3"))
REFRESH_HOURS = int(os.environ.get("REFRESH_HOURS", "24"))
DATA_FILE = Path(os.environ.get("DATA_FILE", "/app/data/catalog.json"))
CINEMA_DATA_FILE = Path(os.environ.get("CINEMA_DATA_FILE", "/app/data/cinema.json"))
# Nouveautés torrent (docs/séries étrangères absents d'AlloCiné VOD) — un ou
# plusieurs trackers Torznab, chacun facultatif indépendamment des autres.
C411_URL = os.environ.get("C411_URL", "")
C411_API_KEY = os.environ.get("C411_API_KEY", "")
TR4KER_URL = os.environ.get("TR4KER_URL", "")
TR4KER_API_KEY = os.environ.get("TR4KER_API_KEY", "")
V3X_URL = os.environ.get("V3X_URL", "")
V3X_API_KEY = os.environ.get("V3X_API_KEY", "")
C411_DATA_FILE = Path(os.environ.get("C411_DATA_FILE", "/app/data/c411.json"))
C411_LIMIT = int(os.environ.get("C411_LIMIT", "100"))
WATCHLIST_FILE = os.environ.get("WATCHLIST_FILE", "/app/data/watchlist.json")
VODIO_PASSWORD = os.environ.get("VODIO_PASSWORD", "")
# Nom du compte principal dans le nouveau formulaire de connexion unifié —
# facultatif, "" (défaut) = champ Nom laissé vide pour se connecter avec ce
# compte (comportement historique conservé si la variable n'est pas définie).
VODIO_DEFAULT_NAME = os.environ.get("VODIO_DEFAULT_NAME", "")
STATIC_DIR = Path(__file__).parent / "static"
# Badges 🧲/⏳ via AIOStreams (config compte dydy) — désactivés si CONFIG absent
STREAM_CHECK_URL = os.environ.get("STREAM_CHECK_URL", "http://aiostreams:3000")
STREAM_CHECK_CONFIG = os.environ.get("STREAM_CHECK_CONFIG", "")
# Résolution minimale pour un badge 🧲 (sinon ⏳). 720 = exclut CAM/TS/sans réso.
QUALITY_MIN = int(os.environ.get("VODIO_QUALITY_MIN", "720"))
# Badges watchlist : requête directe à Wacustom (bypass AIOStreams, qui tronque
# parfois les résultats) + vérification active AllDebrid — réservé à la
# watchlist (liste courte), pas au catalogue AlloCiné quotidien.
WACUSTOM_URL = os.environ.get("WACUSTOM_URL", "http://wacustom:7000")
WACUSTOM_CONFIG = os.environ.get("WACUSTOM_CONFIG", "")
ALLDEBRID_API_KEY = os.environ.get("ALLDEBRID_API_KEY", "")
# Téléchargement direct (films pré-cachés) : lien AllDebrid relayé via MediaFlow
# pour fonctionner depuis n'importe quel réseau (un lien AllDebrid brut est lié
# à l'IP qui l'a débloqué — cf. workflow "partager un lien" déjà en place).
MEDIAFLOW_URL = os.environ.get("MEDIAFLOW_URL", "https://mediaflow.example.org/mf")
MEDIAFLOW_API_PASSWORD = os.environ.get("MEDIAFLOW_API_PASSWORD", "")

MANIFEST = {
    # Personnalisable par instance : changer l'id évite les collisions si un
    # utilisateur installe plusieurs instances VODIO. Défaut = instance d'origine.
    "id": os.environ.get("VODIO_ADDON_ID", "org.eddy.vodio"),
    "version": "1.5.0",
    "name": os.environ.get("VODIO_ADDON_NAME", "VODIO"),
    "description": "Nouveautés VOD françaises (AlloCiné) + ma liste perso (films & séries)",
    "resources": ["catalog"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [
        # Un seul catalogue "nouveautés" (fusion de l'ancien vodio-soon) : tous
        # les films scrapés viennent de la page AlloCiné "Derniers films en
        # VOD" — par définition déjà sortis sur les plateformes VOD. Le split
        # 🧲/⏳ ne reflétait que notre propre check de dispo (Wacustom), pas le
        # vrai statut VOD, d'où la confusion (ex. LES K D'OR 2026-08-02).
        {"type": "movie", "id": "vodio-new", "name": "Nouveautés VOD"},
        # Sourcé directement du tracker C411 (docs/séries étrangères absents
        # d'AlloCiné VOD), filtré (audio FR, ≥720p) et croisé avec la même
        # vérif de dispo que le reste de VODIO.
        {"type": "movie", "id": "vodio-c411-new", "name": "Nouveautés Torrent"},
        {"type": "series", "id": "vodio-c411-new", "name": "Nouveautés Torrent"},
        {"type": "movie", "id": "vodio-watchlist", "name": "VODIO - Ma liste"},
        {"type": "series", "id": "vodio-watchlist", "name": "VODIO - Ma liste"},
    ],
    "behaviorHints": {"configurable": False},
}

state = {"metas": [], "last_refresh": 0.0, "last_error": ""}
state_cinema = {"metas": [], "last_refresh": 0.0, "last_error": ""}
state_c411 = {"metas": [], "last_refresh": 0.0, "last_error": ""}
watchlist = Watchlist(WATCHLIST_FILE)


def _parse_extra_users(raw: str, data_dir: Path) -> dict[str, dict]:
    """`VODIO_EXTRA_USERS=nom1:motdepasse1,nom2:motdepasse2` — chaque nom
    obtient sa propre watchlist (`watchlist_<nom>.json`) et son propre mot de
    passe, choisis dans le formulaire de connexion unifié (champ Nom).
    L'utilisateur par défaut (VODIO_PASSWORD, watchlist.json) n'est pas
    affecté par ce mécanisme."""
    users: dict[str, dict] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        name, pwd = pair.split(":", 1)
        name = name.strip()
        pwd = pwd.strip()
        if not name or not pwd:
            continue
        users[name] = {"password": pwd, "watchlist": Watchlist(str(data_dir / f"watchlist_{name}.json"))}
    return users


EXTRA_USERS = _parse_extra_users(os.environ.get("VODIO_EXTRA_USERS", ""), Path(WATCHLIST_FILE).parent)
# Utilisateurs exclus des SMS de disponibilité (tous partagent le même numéro
# Free Mobile — ex. VODIO_NOSMS_USERS=alice). N'affecte pas les badges.
NOSMS_USERS = {n.strip() for n in os.environ.get("VODIO_NOSMS_USERS", "").split(",") if n.strip()}
ALL_WATCHLISTS: list[tuple[str, Watchlist, bool]] = [("", watchlist, True)] + [
    (name, u["watchlist"], name not in NOSMS_USERS) for name, u in EXTRA_USERS.items()
]

# Table d'authentification unifiée (2026-08-14) : un seul formulaire nom + mot
# de passe pour tous les comptes, vérifié par `_check_credentials`/`POST
# /api/login` ci-dessous. Clé = ce que l'utilisateur tape dans le champ Nom
# ("" = compte principal, sauf si VODIO_DEFAULT_NAME lui donne un vrai nom).
USERS: dict[str, dict] = {VODIO_DEFAULT_NAME: {"password": VODIO_PASSWORD, "watchlist": watchlist}}
if VODIO_DEFAULT_NAME in EXTRA_USERS:
    # Un compte additionnel portant le même nom que VODIO_DEFAULT_NAME
    # écraserait silencieusement le compte principal sans ce garde-fou
    # (relevé par une revue GLM, 2026-08-15).
    log.warning(
        "VODIO_EXTRA_USERS contient un nom identique à VODIO_DEFAULT_NAME (%r) — "
        "ce compte additionnel écrase le compte principal, renomme-le",
        VODIO_DEFAULT_NAME,
    )
USERS.update(EXTRA_USERS)


def load_cache() -> None:
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text())
            state["metas"] = data["metas"]
            state["last_refresh"] = data["last_refresh"]
            log.info("cache chargé : %d films", len(state["metas"]))
        except (json.JSONDecodeError, KeyError) as exc:
            log.warning("cache illisible, ignoré : %s", exc)
    if CINEMA_DATA_FILE.exists():
        try:
            data = json.loads(CINEMA_DATA_FILE.read_text())
            state_cinema["metas"] = data["metas"]
            state_cinema["last_refresh"] = data["last_refresh"]
            log.info("cache cinéma chargé : %d films", len(state_cinema["metas"]))
        except (json.JSONDecodeError, KeyError) as exc:
            log.warning("cache cinéma illisible, ignoré : %s", exc)
    if C411_DATA_FILE.exists():
        try:
            data = json.loads(C411_DATA_FILE.read_text())
            state_c411["metas"] = data["metas"]
            state_c411["last_refresh"] = data["last_refresh"]
            log.info("cache C411 chargé : %d titres", len(state_c411["metas"]))
        except (json.JSONDecodeError, KeyError) as exc:
            log.warning("cache C411 illisible, ignoré : %s", exc)


def save_cache() -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps({"metas": state["metas"], "last_refresh": state["last_refresh"]})
    )


def save_cache_cinema() -> None:
    CINEMA_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    CINEMA_DATA_FILE.write_text(
        json.dumps({"metas": state_cinema["metas"], "last_refresh": state_cinema["last_refresh"]})
    )


def save_cache_c411() -> None:
    C411_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    C411_DATA_FILE.write_text(
        json.dumps({"metas": state_c411["metas"], "last_refresh": state_c411["last_refresh"]})
    )


async def _badge(metas: list[dict]) -> None:
    if STREAM_CHECK_CONFIG and metas:
        await availability.add_availability_badges(
            STREAM_CHECK_URL, STREAM_CHECK_CONFIG, metas, QUALITY_MIN
        )


async def _badge_watchlist(metas: list[dict]) -> None:
    """Watchlist : requête directe Wacustom + vérif active AllDebrid si
    configuré, sinon repli sur le comportement standard (AIOStreams)."""
    if not metas:
        return
    if WACUSTOM_CONFIG:
        await availability.add_availability_badges_watchlist(
            WACUSTOM_URL, WACUSTOM_CONFIG, ALLDEBRID_API_KEY, TMDB_API_KEY, metas, QUALITY_MIN,
            verify_fn=_verify_on_trackers,
        )
    else:
        await _badge(metas)


async def _refresh_watchlist_badges(label: str, wl: Watchlist, sms: bool = True) -> None:
    """Rafraîchit les badges d'une watchlist + SMS sur les passages ⏳ → 🧲.
    `label` (nom d'utilisateur, vide pour le défaut) préfixe le SMS pour
    distinguer qui a un titre dispo — toutes les watchlists partagent le
    même numéro Free Mobile. `sms=False` (VODIO_NOSMS_USERS) désactive
    uniquement la notification, pas le calcul des badges."""
    current = wl.metas()  # non vus, avec les anciens badges
    was_available = {m["id"]: availability._is_available_name(m["name"]) for m in current}
    items = [_strip_badge(m) for m in current]
    if not items:
        return
    await _badge_watchlist(items)
    wl.replace_all(items)
    log.info("watchlist%s : badges rafraîchis (%d titres)", f" ({label})" if label else "", len(items))
    if not sms:
        return
    for m in items:
        if availability._is_available_name(m["name"]) and not was_available.get(m["id"]):
            title = _strip_badge(m)["name"]
            prefix = f"VODIO ({label})" if label else "VODIO"
            await notify.send_sms(f"{prefix} : « {title} » est dispo ! 🧲")
            log.info("SMS envoyé : %s dispo", title)


async def refresh() -> None:
    films = await scraper.scrape(ALLOCINE_PAGES)
    if not films:
        state["last_error"] = "scrape AlloCiné : 0 film (structure HTML changée ?)"
        log.error(state["last_error"])
        return
    metas = await tmdb.match_all(TMDB_API_KEY, films)
    if not metas:
        state["last_error"] = "matching TMDB : 0 film matché"
        log.error(state["last_error"])
        return
    await _badge(metas)
    state["metas"] = metas
    state["last_refresh"] = time.time()
    state["last_error"] = ""
    save_cache()
    log.info("refresh OK : %d films au catalogue", len(metas))
    for label, wl, sms in ALL_WATCHLISTS:
        await _refresh_watchlist_badges(label, wl, sms)


async def refresh_cinema() -> None:
    """Sorties cinéma de la semaine (page unique, pas de pagination) — pas de
    badge de dispo calculé : un film qui sort en salle n'a par définition
    aucune source VOD/torrent avant des mois (cf. MIN_DAYS_FOR_ACTIVE_CHECK),
    inutile de vérifier. Onglet purement informatif, pour ajouter à la
    watchlist en avance et être notifié plus tard quand ça sort en VOD."""
    films = await cinema_scraper.scrape()
    if not films:
        state_cinema["last_error"] = "scrape agenda cinéma : 0 film (structure HTML changée ?)"
        log.error(state_cinema["last_error"])
        return
    metas = await tmdb.match_all(TMDB_API_KEY, films)
    if not metas:
        state_cinema["last_error"] = "matching TMDB (cinéma) : 0 film matché"
        log.error(state_cinema["last_error"])
        return
    # L'agenda AlloCiné mélange les vraies nouveautés et les reprises/
    # rétrospectives (ex. Cowboy Bebop 2001, Cast a Dark Shadow 1955 qui
    # ressortent en salle cette semaine) — écarte tout ce dont l'année TMDB
    # est trop ancienne pour être une vraie première sortie (une sortie FR
    # retardée peut dépasser 1 an après l'original, d'où la marge à 3 ans).
    current_year = datetime.date.today().year
    metas = [
        m for m in metas
        if not m.get("releaseInfo") or int(m["releaseInfo"]) >= current_year - 3
    ]
    if not metas:
        state_cinema["last_error"] = "0 film après filtre reprises"
        log.error(state_cinema["last_error"])
        return
    state_cinema["metas"] = metas
    state_cinema["last_refresh"] = time.time()
    state_cinema["last_error"] = ""
    save_cache_cinema()
    log.info("refresh cinéma OK : %d films", len(metas))


def _configured_trackers() -> list[tuple[str, str, str]]:
    tracker_configs = [
        ("C411", C411_URL, C411_API_KEY),
        ("Tr4ker", TR4KER_URL, TR4KER_API_KEY),
        ("V3X", V3X_URL, V3X_API_KEY),
    ]
    return [(name, url, key) for name, url, key in tracker_configs if url and key]


async def _verify_on_trackers(imdb_id: str) -> bool:
    """Vérification live (watchlist uniquement, cf. `availability.py`) :
    existe-t-il une release française ≥QUALITY_MIN pour cet imdb_id sur l'un
    des trackers configurés ? Un appel par tracker (jamais plus), et
    seulement pour un titre déjà bloqué par le garde-fou anti-CAM — pas de
    risque de rejouer l'incident 429 du refresh bulk (une seule requête/tout
    le catalogue, cf. `c411_feed.fetch_latest`)."""
    for name, url, key in _configured_trackers():
        items = await c411_feed.search_by_imdb(url, key, imdb_id, name=name)
        # require_dub=True : une release VOSTFR prouve qu'un vrai fichier
        # existe mais pas qu'il est en VF — lever le garde-fou anti-CAM sur
        # cette seule preuve exposerait ensuite un cache Lumio VOSTFR comme
        # "dispo" (cas réel 2026-08-22, "Mutiny").
        if c411_feed.filter_relevant(items, QUALITY_MIN, require_dub=True):
            return True
    return False


async def refresh_c411() -> None:
    """Nouveautés torrent — complète AlloCiné (docs/séries étrangères absents
    de sa page VOD), filtré (audio FR, ≥QUALITY_MIN) et badgé via le même
    mécanisme AIOStreams que le catalogue AlloCiné. Interroge un ou plusieurs
    trackers Torznab (C411, Tr4ker, V3X — même format chez les trois : une
    seule requête par tracker et par refresh, jamais une par titre). Source
    entièrement désactivée si aucun tracker n'est configuré."""
    configured = _configured_trackers()
    if not configured:
        return
    items: list[dict] = []
    for name, url, key in configured:
        tracker_items = await c411_feed.fetch_latest(url, key, C411_LIMIT, name=name)
        log.info("Nouveautés Torrent [%s] : %d résultats bruts", name, len(tracker_items))
        items.extend(tracker_items)
    if not items:
        state_c411["last_error"] = "Nouveautés Torrent : 0 résultat (trackers down ou clés invalides ?)"
        log.error(state_c411["last_error"])
        return
    relevant = c411_feed.filter_relevant(items, QUALITY_MIN)
    metas: list[dict] = []
    seen_ids: set[str] = set()
    for item in relevant:
        meta = await tmdb.build_meta_from_external(
            TMDB_API_KEY, item.get("imdb_id"), item.get("tmdb_id"), item["media_type"]
        )
        if not meta or meta["id"] in seen_ids:
            continue
        seen_ids.add(meta["id"])
        metas.append(meta)
    # Films uniquement filtrés sur l'année en cours (un vieux film reposté sur
    # le tracker n'est pas une "nouveauté") — pas les séries : un nouvel
    # épisode d'une série ancienne (ex. Silo, 2023) reste une vraie nouveauté,
    # exclure sur l'année de première diffusion serait contre-productif.
    current_year = str(datetime.date.today().year)
    metas = [
        m for m in metas
        if m["type"] == "series" or m.get("releaseInfo") == current_year
    ]
    if not metas:
        state_c411["last_error"] = "C411 : 0 titre matché après filtre"
        log.error(state_c411["last_error"])
        return
    await _badge(metas)
    state_c411["metas"] = metas
    state_c411["last_refresh"] = time.time()
    state_c411["last_error"] = ""
    save_cache_c411()
    log.info("refresh C411 OK : %d titres (%d bruts, %d après filtre)", len(metas), len(items), len(relevant))


_BADGE_RE = re.compile(r"^[✅⏳⚡🧲]+\s*")


def _strip_badge(meta: dict) -> dict:
    """Copie du meta sans le préfixe 🧲/⚡/⏳ (pour recalculer proprement)."""
    m = dict(meta)
    m["name"] = _BADGE_RE.sub("", m.get("name", ""))
    return m


async def refresh_loop() -> None:
    while True:
        try:
            await refresh()
        except Exception:
            log.exception("refresh en échec")
            state["last_error"] = "refresh : exception (voir logs)"
        try:
            await refresh_cinema()
        except Exception:
            log.exception("refresh cinéma en échec")
            state_cinema["last_error"] = "refresh : exception (voir logs)"
        try:
            await refresh_c411()
        except Exception:
            log.exception("refresh C411 en échec")
            state_c411["last_error"] = "refresh : exception (voir logs)"
        await asyncio.sleep(REFRESH_HOURS * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_cache()
    task = asyncio.create_task(refresh_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)

CORS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=900"}
# Watchlist : cache court pour que Stremio voie vite les ajouts manuels.
CORS_LIVE = {"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=30"}


def _clean_name(meta: dict) -> dict:
    m = dict(meta)
    m["name"] = _BADGE_RE.sub("", m.get("name", ""))
    return m


# Un magnet mort (0 seeder) reste "Downloading" côté AllDebrid pendant ~20 min
# avant d'être officiellement déclaré en échec. On ne veut pas faire attendre
# l'utilisateur aussi longtemps : au-delà de ce délai sans le moindre octet ni
# le moindre pair, on le traite nous-mêmes comme mort.
STALL_GRACE_S = 60


def _is_stalled(st: dict, started_at: float | None) -> bool:
    return bool(
        started_at and st["seeders"] == 0 and st["downloaded_pct"] == 0
        and time.time() - started_at > STALL_GRACE_S
    )


async def _badge_and_persist(wl: Watchlist, entry: dict) -> None:
    """Calcule le badge d'un titre en arrière-plan puis sauvegarde (UI snappy)."""
    try:
        await _badge_watchlist([entry])  # mute entry["name"] (objet stocké dans la liste)
        wl.persist()
    except Exception:
        log.exception("badge en arrière-plan en échec pour %s", entry.get("id"))


async def _run_precache_season(wl: Watchlist, imdb_id: str, season: int, episode_count: int) -> None:
    """Traite chaque épisode d'une saison en tâche de fond (concurrence
    limitée, comme les badges) : cherche un candidat via Wacustom puis lance
    le téléchargement AllDebrid si besoin. Statut persisté épisode par épisode
    pour que le polling front voie la progression en direct."""
    entry = wl.get(imdb_id)
    if entry is None:
        return
    sem = asyncio.Semaphore(2)

    async def _one(ep: int) -> None:
        async with sem:
            try:
                cand = await availability.find_precache_candidate(
                    WACUSTOM_URL, WACUSTOM_CONFIG, TMDB_API_KEY, entry, QUALITY_MIN, season, ep,
                    verify_fn=_verify_on_trackers,
                )
                if cand["status"] in ("cached", "none", "too_recent"):
                    wl.set_precache_episode(imdb_id, season, ep, cand["status"])
                    return
                result = await alldebrid.start_download(ALLDEBRID_API_KEY, cand["magnet"])
                wl.set_precache_episode(
                    imdb_id, season, ep,
                    "ready" if result["ready"] else "downloading", result["id"], magnet=cand["magnet"],
                )
            except alldebrid.AllDebridError as exc:
                log.warning("précache saison %s ép %s (%s) : %s", season, ep, imdb_id, exc)
                wl.set_precache_episode(imdb_id, season, ep, "failed")
            except Exception:
                log.exception("précache saison %s ép %s (%s) en échec", season, ep, imdb_id)
                wl.set_precache_episode(imdb_id, season, ep, "failed")

    await asyncio.gather(*(_one(ep) for ep in range(1, episode_count + 1)))
    log.info("précache saison %s (%s) terminé : %d épisode(s) traité(s)", season, imdb_id, episode_count)


# Authentification par jeton de session (2026-08-15) : remplace l'ancien
# `resolve_user` qui exigeait de renvoyer le mot de passe en clair à CHAQUE
# appel API. Désormais `POST /api/login` échange nom+mot de passe (une seule
# fois) contre un jeton opaque, renvoyé ensuite en `Authorization: Bearer
# <jeton>` sur chaque requête. Jetons en mémoire (perdus au redémarrage du
# conteneur) — acceptable pour un usage familial : le client garde nom+mot de
# passe en cache local pour se reconnecter silencieusement si besoin (cf.
# `autoUnlock` côté page web), sans jamais les renvoyer à chaque appel.
SESSION_TTL_S = 30 * 24 * 3600  # 30 jours
SESSIONS: dict[str, dict] = {}  # token -> {"watchlist": Watchlist, "expires": float}


def _check_credentials(name: str, password: str) -> Watchlist | None:
    account = USERS.get(name)
    if account is None or not account["password"] or not hmac.compare_digest(password, account["password"]):
        return None
    return account["watchlist"]


@app.post("/api/login")
async def api_login(payload: dict):
    name = (payload.get("name") or "").strip()
    password = payload.get("password") or ""
    wl = _check_credentials(name, password)
    if wl is None:
        raise HTTPException(status_code=401, detail="Nom ou mot de passe invalide")
    now = time.time()
    # Purge opportuniste des jetons expirés à chaque connexion — sans ça
    # SESSIONS ne redescend jamais tant que le conteneur tourne (relevé par
    # une revue GLM, 2026-08-15). Peu d'utilisateurs ici, mais gratuit.
    for old_token, session in list(SESSIONS.items()):
        if session["expires"] < now:
            del SESSIONS[old_token]
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"watchlist": wl, "expires": now + SESSION_TTL_S}
    return {"token": token}


def resolve_session(authorization: str = Header(default="")) -> Watchlist:
    token = authorization.removeprefix("Bearer ").strip()
    session = SESSIONS.get(token) if token else None
    if session is None or session["expires"] < time.time():
        SESSIONS.pop(token, None)
        raise HTTPException(status_code=401, detail="Session invalide ou expirée")
    return session["watchlist"]


@app.post("/api/logout")
async def api_logout(authorization: str = Header(default="")):
    """Révoque le jeton courant (bouton Déconnexion) — sans ça, un jeton
    reste valable jusqu'à ses 30 jours même après un clic sur déconnexion."""
    token = authorization.removeprefix("Bearer ").strip()
    SESSIONS.pop(token, None)
    return {"ok": True}


# ── Stremio (compte principal uniquement — seul compte réellement installé
# dans Stremio, décision explicite du 2026-08-14 : les comptes additionnels
# n'ont que la page web/API) ─────────────────────────────────────────────────
@app.get("/manifest.json")
async def manifest_route():
    return JSONResponse(MANIFEST, headers=CORS)


@app.get("/catalog/movie/vodio-new.json")
async def catalog_new():
    metas = [_clean_name(m) for m in state["metas"]]
    return JSONResponse({"metas": metas}, headers=CORS)


@app.get("/catalog/movie/vodio-c411-new.json")
async def catalog_c411_movie():
    metas = [_clean_name(m) for m in state_c411["metas"] if m.get("type") == "movie"]
    return JSONResponse({"metas": metas}, headers=CORS)


@app.get("/catalog/series/vodio-c411-new.json")
async def catalog_c411_series():
    metas = [_clean_name(m) for m in state_c411["metas"] if m.get("type") == "series"]
    return JSONResponse({"metas": metas}, headers=CORS)


@app.get("/catalog/movie/vodio-watchlist.json")
async def catalog_watchlist_movie():
    return JSONResponse({"metas": watchlist.metas("movie")}, headers=CORS_LIVE)


@app.get("/catalog/series/vodio-watchlist.json")
async def catalog_watchlist_series():
    return JSONResponse({"metas": watchlist.metas("series")}, headers=CORS_LIVE)


# ── Page web de gestion + assets PWA (partagés, une seule URL pour tous) ────
@app.get("/")
async def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/manifest.webmanifest")
async def pwa_manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def pwa_sw():
    return FileResponse(
        STATIC_DIR / "sw.js", media_type="application/javascript",
        headers={"Service-Worker-Allowed": "./"},
    )


@app.get("/{icon}.png")
async def pwa_icon(icon: str):
    path = STATIC_DIR / f"{icon}.png"
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/png")


# ── Anciennes URLs par personne (/u/<nom>/…, retirées le 2026-08-14) : simple
# redirection vers l'accueil pour les favoris/PWA déjà installés côté client,
# le nouveau formulaire de connexion prend le relais. ───────────────────────
#
# ⚠️ La redirection doit être ABSOLUE et inclure le préfixe public. En WAN,
# Traefik sert l'app sous /vodio et retire ce préfixe (stripprefix) : un
# `url="/"` renvoyait donc le visiteur à la racine du DOMAINE, c'est-à-dire
# sur StreamFusion, qui redirige lui-même vers /configure — page réservée à
# l'IP d'administration, donc 403. Cinq de ces 403 suffisaient à déclencher
# un bannissement fail2ban : un utilisateur légitime ouvrant un ancien favori
# se faisait bannir sur TOUS les ports (cas réel, 2026-08-16).
# Le préfixe est lu dans l'en-tête X-Forwarded-Prefix posé par Traefik, avec
# repli sur "/" en accès direct (LAN, sans reverse proxy).
@app.get("/u/{name}/{rest:path}")
async def legacy_user_url(request: Request, name: str, rest: str = ""):
    prefix = (request.headers.get("x-forwarded-prefix") or "").rstrip("/")
    return RedirectResponse(url=f"{prefix}/" if prefix else "/")


# ── Endpoints de gestion (protégés, identité résolue depuis les identifiants
# envoyés — X-Vodio-User / X-Vodio-Password) ────────────────────────────────
@app.get("/api/vod")
async def api_vod(_: Watchlist = Depends(resolve_session)):
    return {"items": state["metas"], "last_refresh": state["last_refresh"]}


@app.get("/api/cinema")
async def api_cinema(_: Watchlist = Depends(resolve_session)):
    return {"items": state_cinema["metas"], "last_refresh": state_cinema["last_refresh"]}


@app.get("/api/c411")
async def api_c411(_: Watchlist = Depends(resolve_session)):
    return {"items": state_c411["metas"], "last_refresh": state_c411["last_refresh"]}


@app.get("/api/search")
async def api_search(q: str, _: Watchlist = Depends(resolve_session)):
    if not q.strip():
        return {"results": []}
    return {"results": await tmdb.search_titles(TMDB_API_KEY, q)}


@app.get("/api/trailer/{tmdb_id}")
async def api_trailer(tmdb_id: int, media_type: str = "movie", _: Watchlist = Depends(resolve_session)):
    return {"key": await tmdb.get_trailer(TMDB_API_KEY, tmdb_id, media_type)}


@app.get("/api/trending")
async def api_trending(_: Watchlist = Depends(resolve_session)):
    return {"items": await tmdb.get_trending(TMDB_API_KEY)}


@app.get("/api/digital-releases")
async def api_digital_releases(_: Watchlist = Depends(resolve_session)):
    return {"items": await tmdb.get_digital_releases(TMDB_API_KEY)}


@app.get("/api/changelog")
async def api_changelog():
    """Public (pas de Depends(resolve_session)) : le contenu n'a rien de
    sensible, et ça permet d'afficher le numéro de version sur l'écran de
    connexion avant même d'être identifié (relevé par une revue GLM,
    2026-08-15)."""
    return {"version": changelog.VERSION, "entries": changelog.CHANGELOG}


@app.get("/api/watchlist")
async def api_watchlist(wl: Watchlist = Depends(resolve_session)):
    # all_items inclut les vus (flag watched) pour la section « Déjà vus » de l'UI.
    return {"items": wl.all_items()}


@app.post("/api/watchlist")
async def api_add(payload: dict, wl: Watchlist = Depends(resolve_session)):
    tmdb_id = payload.get("tmdb_id")
    media_type = payload.get("media_type", "movie")
    if not tmdb_id:
        raise HTTPException(status_code=400, detail="tmdb_id requis")
    meta = await tmdb.build_meta_from_tmdb(TMDB_API_KEY, int(tmdb_id), media_type)
    if not meta:
        raise HTTPException(status_code=404, detail="Titre introuvable ou sans ID IMDb")
    # Provenance (ex. "c411") — exempte du garde-fou anti-CAM les titres déjà
    # filtrés sur un vrai tag qualité à l'ingestion, cf. availability.py.
    source = payload.get("source")
    if source:
        meta["source"] = source
    entry = wl.add(meta)  # stocké immédiatement, badge calculé après
    if entry is not None:
        asyncio.create_task(_badge_and_persist(wl, entry))
    return {"added": entry is not None, "item": entry}


@app.post("/api/watchlist/{imdb_id}/watched")
async def api_watched(imdb_id: str, payload: dict, wl: Watchlist = Depends(resolve_session)):
    return {"ok": wl.set_watched(imdb_id, bool(payload.get("watched", True)))}


@app.post("/api/watchlist/{imdb_id}/precache")
async def api_precache(imdb_id: str, wl: Watchlist = Depends(resolve_session)):
    """Pré-cache à la demande : trouve le meilleur candidat torrent plausible
    pour ce titre non-caché et déclenche son téléchargement sur AllDebrid.
    Une fois prêt (statut re-vérifiable), le contenu devient lisible dans
    Stremio via le flux normal (Wacustom)."""
    if not (WACUSTOM_CONFIG and ALLDEBRID_API_KEY):
        raise HTTPException(status_code=503, detail="Pré-cache non configuré (Wacustom/AllDebrid)")
    entry = wl.get(imdb_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Titre absent de la watchlist")

    cand = await availability.find_precache_candidate(
        WACUSTOM_URL, WACUSTOM_CONFIG, TMDB_API_KEY, entry, QUALITY_MIN,
        verify_fn=_verify_on_trackers,
    )
    if cand["status"] == "cached":
        return {"status": "cached", "detail": "Déjà disponible en cache"}
    if cand["status"] == "none":
        return {"status": "none", "detail": "Aucune source de qualité suffisante trouvée"}
    if cand["status"] == "too_recent":
        return {"status": "too_recent", "detail": f"Sorti il y a {cand['days']} j — pas encore de vraie source (CAM uniquement)"}

    try:
        result = await alldebrid.start_download(ALLDEBRID_API_KEY, cand["magnet"])
    except alldebrid.AllDebridError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    wl.set_precache(imdb_id, result["id"])
    return {
        "status": "ready" if result["ready"] else "downloading",
        "magnet_id": result["id"],
        "size_gb": cand.get("size_gb"),
        "resolution": cand.get("resolution"),
    }


@app.get("/api/watchlist/{imdb_id}/precache")
async def api_precache_status(imdb_id: str, wl: Watchlist = Depends(resolve_session)):
    """Avancement d'un pré-cache lancé précédemment (polling depuis la page)."""
    if not ALLDEBRID_API_KEY:
        raise HTTPException(status_code=503, detail="AllDebrid non configuré")
    entry = wl.get(imdb_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Titre absent de la watchlist")
    magnet_id = entry.get("precache_magnet_id")
    if not magnet_id:
        return {"status": "idle"}
    st = await alldebrid.get_status(ALLDEBRID_API_KEY, magnet_id)
    if st["ready"]:
        return {"status": "ready", "downloaded_pct": 100}
    if st["failed"] or _is_stalled(st, entry.get("precache_started_at")):
        wl.set_precache(imdb_id, None)
        return {"status": "failed"}
    return {"status": "downloading", "downloaded_pct": st["downloaded_pct"]}


def _mediaflow_proxy_url(filename: str, link: str) -> str:
    return (
        f"{MEDIAFLOW_URL}/proxy/stream/{quote(filename)}"
        f"?d={quote(link, safe='')}&api_password={quote(MEDIAFLOW_API_PASSWORD)}"
    )


async def _resolve_ready_download(magnet_id: int) -> dict | None:
    """Fichier prêt côté AllDebrid → lien MediaFlow (portable, pas lié à
    l'IP qui a débloqué). Partagé entre `/download`, `/sources` et leur
    statut respectif pour ne pas dupliquer la construction de l'URL."""
    file = await alldebrid.get_direct_link(ALLDEBRID_API_KEY, magnet_id)
    if not file:
        return None
    return {"download_url": _mediaflow_proxy_url(file["filename"], file["link"]), "filename": file["filename"]}


@app.get("/api/watchlist/{imdb_id}/sources")
async def api_sources(imdb_id: str, wl: Watchlist = Depends(resolve_session)):
    """Liste détaillée des sources trouvées (façon Ludio) — repli manuel
    quand l'utilisateur préfère choisir lui-même plutôt que le bouton
    Télécharger automatique (`/download`, qui prend la meilleure sans
    demander)."""
    if not WACUSTOM_CONFIG:
        return {"sources": []}
    entry = wl.get(imdb_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Titre absent de la watchlist")
    sources = await availability.list_sources(WACUSTOM_URL, WACUSTOM_CONFIG, entry, QUALITY_MIN)
    if not sources and MEDIAFLOW_API_PASSWORD and entry.get("type", "movie") == "movie":
        # Wacustom n'a rien : même repli que /download (signal Lumio), pour
        # que la liste explique pourquoi le bouton Télécharger fonctionne
        # quand même malgré "aucune source" ici (cas réel signalé 2026-08-14).
        # Listés sans résolution (0 appel Lumio) — l'utilisateur choisit
        # ensuite lequel résoudre via /resolve-lumio (un seul appel, pour
        # l'entrée choisie — demandé le 2026-08-15, la 1ʳᵉ version ne
        # résolvait automatiquement qu'un seul candidat sans laisser de choix).
        for c in await availability.find_lumio_candidates(entry):
            sources.append({
                "source": "Lumio",
                "title": c["filename"],
                "size_gb": c["size_gb"],
                "resolution": None,
                "cached": True,
                "link": None,
                "lumio_playback_url": c["playback_url"],
            })
    return {"sources": sources}


@app.post("/api/watchlist/{imdb_id}/resolve-lumio")
async def api_resolve_lumio(imdb_id: str, payload: dict, wl: Watchlist = Depends(resolve_session)):
    """Résolution à la demande d'un candidat Lumio listé par `/sources` (1
    appel Lumio, seulement pour l'entrée choisie par l'utilisateur)."""
    if not MEDIAFLOW_API_PASSWORD:
        raise HTTPException(status_code=503, detail="MediaFlow non configuré")
    playback_url = (payload.get("playback_url") or "").strip()
    filename = payload.get("filename") or "video.mkv"
    resolved = await availability.resolve_lumio_link(playback_url)
    if resolved is None:
        raise HTTPException(status_code=502, detail="Résolution impossible (source expirée ou quota Lumio atteint)")
    return {"download_url": _mediaflow_proxy_url(filename, resolved["url"]), "filename": filename}


@app.post("/api/watchlist/{imdb_id}/download-source")
async def api_download_source(imdb_id: str, payload: dict, wl: Watchlist = Depends(resolve_session)):
    """Démarre AllDebrid pour un lien magnet choisi explicitement dans la
    liste `/sources` (bouton ⬇️ AllDebrid par source, façon Ludio)."""
    if not ALLDEBRID_API_KEY:
        raise HTTPException(status_code=503, detail="AllDebrid non configuré")
    if not MEDIAFLOW_API_PASSWORD:
        raise HTTPException(status_code=503, detail="MediaFlow non configuré")
    link = (payload.get("link") or "").strip()
    if not link.startswith("magnet:"):
        raise HTTPException(status_code=400, detail="Ce lien n'est pas un magnet — rien à démarrer automatiquement, copie-le et ouvre-le toi-même")
    try:
        result = await alldebrid.start_download(ALLDEBRID_API_KEY, link)
    except alldebrid.AllDebridError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if result["ready"]:
        resolved = await _resolve_ready_download(result["id"])
        if resolved:
            return {"ready": True, "magnet_id": result["id"], **resolved}
    return {"ready": False, "magnet_id": result["id"]}


@app.get("/api/watchlist/{imdb_id}/download-source/{magnet_id}")
async def api_download_source_status(imdb_id: str, magnet_id: int, wl: Watchlist = Depends(resolve_session)):
    """Revérifie un téléchargement démarré via `/download-source` (polling
    manuel côté page, bouton 🔄 Revérifier)."""
    if not ALLDEBRID_API_KEY:
        raise HTTPException(status_code=503, detail="AllDebrid non configuré")
    st = await alldebrid.get_status(ALLDEBRID_API_KEY, magnet_id)
    if st["failed"]:
        return {"ready": False, "failed": True}
    if not st["ready"]:
        return {"ready": False, "failed": False, "downloaded_pct": st["downloaded_pct"]}
    resolved = await _resolve_ready_download(magnet_id)
    if resolved is None:
        return {"ready": False, "failed": True}
    return {"ready": True, **resolved}


@app.get("/api/watchlist/{imdb_id}/download")
async def api_download(imdb_id: str, wl: Watchlist = Depends(resolve_session)):
    """Lien de téléchargement direct d'un film disponible (watchlist
    uniquement). Le lien AllDebrid est débloqué côté serveur puis relayé
    via MediaFlow — un lien AllDebrid brut est lié à l'IP qui l'a
    débloqué, MediaFlow permet de télécharger depuis n'importe quel réseau
    (même principe qu'un partage manuel de lien MediaFlow).

    Un badge 🧲 ne veut PAS dire que VODIO a lui-même déclenché un
    pré-cache (`precache_magnet_id` peut être absent — cas courant : le
    titre était déjà caché ailleurs, trouvé directement par Wacustom, ou
    via un signal externe/un check AllDebrid ponctuel, cf.
    `_check_one_watchlist`). Si aucun magnet n'est encore suivi, on
    retrouve le meilleur candidat via find_precache_candidate (même
    logique que le bouton Précharger) et on le pousse sur AllDebrid —
    quasi instantané si vraiment déjà caché (cohérent avec le badge 🧲),
    sinon on prévient l'utilisateur plutôt que de bloquer la requête."""
    if not ALLDEBRID_API_KEY:
        raise HTTPException(status_code=503, detail="AllDebrid non configuré")
    if not MEDIAFLOW_API_PASSWORD:
        raise HTTPException(status_code=503, detail="MediaFlow non configuré")
    entry = wl.get(imdb_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Titre absent de la watchlist")
    if entry.get("type", "movie") != "movie":
        raise HTTPException(status_code=400, detail="Téléchargement disponible pour les films uniquement")

    magnet_id = entry.get("precache_magnet_id")
    if not magnet_id:
        if not WACUSTOM_CONFIG:
            raise HTTPException(status_code=409, detail="Film pas encore pré-caché")
        cand = await availability.find_precache_candidate(
            WACUSTOM_URL, WACUSTOM_CONFIG, TMDB_API_KEY, entry, QUALITY_MIN,
            verify_fn=_verify_on_trackers,
        )
        if cand["status"] not in ("cached", "candidate"):
            # Wacustom n'a plus aucune source pour ce film (cas du badge
            # 🧲⚡ obtenu uniquement via le "signal externe" Lumio, cf.
            # `_check_one_watchlist` — le badge reflète alors la
            # disponibilité en streaming Stremio, pas forcément un
            # candidat téléchargeable). Repli : Lumio a parfois déjà un
            # lien pré-résolu par leur propre infra debrid.
            direct = await availability.find_lumio_direct_link(entry)
            if direct is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Disponible en streaming via Stremio, mais aucune "
                        "source téléchargeable trouvée pour ce film pour "
                        "le moment"
                    ),
                )
            return {
                "download_url": _mediaflow_proxy_url(direct["filename"], direct["url"]),
                "filename": direct["filename"],
            }
        ddl_only = cand["status"] == "cached" and not cand.get("magnet")
        if cand.get("magnet"):
            magnets = [cand["magnet"]]
        else:
            magnets = cand.get("fallback_magnets") or []
        if not magnets:
            raise HTTPException(status_code=409, detail="Aucune source exploitable trouvée pour ce film")

        # "cached" sans magnet direct : plusieurs candidats torrent tentés
        # jusqu'à en trouver un déjà en cache partagé AllDebrid (le plus
        # petit n'est pas forcément celui-là, cf. commentaire côté
        # find_precache_candidate).
        result = None
        for magnet in magnets:
            try:
                r = await alldebrid.start_download(ALLDEBRID_API_KEY, magnet)
            except alldebrid.AllDebridError:
                continue
            if r["ready"]:
                result = r
                break
            result = result or r

        if result is None:
            raise HTTPException(status_code=502, detail="Échec AllDebrid sur toutes les sources candidates")
        if not result["ready"] and ddl_only:
            # Badge 🧲 basé sur une source DDL (non téléchargeable par ce
            # flux) ; aucun des candidats torrent tentés en repli n'est
            # finalement déjà en cache non plus — pas de vrai
            # téléchargement à lancer en douce pour un titre censé être
            # "déjà disponible".
            raise HTTPException(
                status_code=409,
                detail="Disponible uniquement via un lien direct (téléchargement pas encore pris en charge pour ce type de source)",
            )
        wl.set_precache(imdb_id, result["id"])
        if not result["ready"]:
            raise HTTPException(
                status_code=409,
                detail="Mise en cache démarrée sur AllDebrid, réessaie dans quelques instants",
            )
        magnet_id = result["id"]

    file = await alldebrid.get_direct_link(ALLDEBRID_API_KEY, magnet_id)
    if not file:
        raise HTTPException(status_code=502, detail="Fichier introuvable ou magnet pas prêt")

    return {"download_url": _mediaflow_proxy_url(file["filename"], file["link"]), "filename": file["filename"]}


@app.post("/api/watchlist/{imdb_id}/precache-season")
async def api_precache_season(imdb_id: str, payload: dict, wl: Watchlist = Depends(resolve_session)):
    """Lance le pré-cache de tous les épisodes d'une saison (à la demande),
    en tâche de fond. Le statut se consulte via le GET du même chemin."""
    if not (WACUSTOM_CONFIG and ALLDEBRID_API_KEY):
        raise HTTPException(status_code=503, detail="Pré-cache non configuré (Wacustom/AllDebrid)")
    entry = wl.get(imdb_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Titre absent de la watchlist")
    if entry.get("type") != "series":
        raise HTTPException(status_code=400, detail="Le pré-cache de saison ne s'applique qu'aux séries")
    try:
        season = int(payload.get("season", 1))
    except (TypeError, ValueError):
        season = 0
    if season < 1:
        raise HTTPException(status_code=400, detail="Numéro de saison invalide")

    tmdb_id = entry.get("tmdb_id") or await tmdb.get_tmdb_id(TMDB_API_KEY, imdb_id, "series")
    if not tmdb_id:
        raise HTTPException(status_code=404, detail="tmdb_id introuvable pour ce titre")
    count = await tmdb.get_season_episode_count(TMDB_API_KEY, tmdb_id, season)
    if not count:
        raise HTTPException(status_code=404, detail=f"Saison {season} introuvable ou vide")

    wl.start_precache_season(imdb_id, season, count)
    asyncio.create_task(_run_precache_season(wl, imdb_id, season, count))
    return {"status": "started", "season": season, "total": count}


@app.get("/api/watchlist/{imdb_id}/precache-season/{season}")
async def api_precache_season_status(imdb_id: str, season: int, wl: Watchlist = Depends(resolve_session)):
    """Avancement d'un pré-cache de saison lancé précédemment. Rafraîchit au
    passage le statut AllDebrid des épisodes en téléchargement."""
    if not ALLDEBRID_API_KEY:
        raise HTTPException(status_code=503, detail="AllDebrid non configuré")
    entry = wl.get(imdb_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Titre absent de la watchlist")
    ps = entry.get("precache_seasons", {}).get(str(season))
    if not ps:
        return {"status": "idle", "season": season}

    sem = asyncio.Semaphore(3)

    async def _retry_candidate(ep: str, info: dict) -> None:
        """Cherche un nouveau candidat pour un épisode bloqué (magnet mort
        confirmé, ou resté "échec"/"aucune source" faute d'une 2ᵉ chance —
        cas réel 2026-07-20 : un timeout réseau passager avait empêché le
        repli automatique de retrouver le pack C411, pourtant valide et
        utilisé avec succès pour d'autres épisodes de la même saison). Exclut
        les magnets déjà essayés pour ne jamais reboucler sur un torrent mort."""
        tried = set(info.get("tried_magnets", []))
        async with sem:
            cand = await availability.find_precache_candidate(
                WACUSTOM_URL, WACUSTOM_CONFIG, TMDB_API_KEY, entry, QUALITY_MIN,
                season, int(ep), exclude_magnets=tried, verify_fn=_verify_on_trackers,
            )
        if cand["status"] == "cached":
            wl.set_precache_episode(imdb_id, season, int(ep), "cached")
            return
        if cand["status"] != "candidate":
            # Toujours rien de neuf (aucune source, ou trop récent) : on
            # laisse le statut existant tel quel plutôt que d'écraser un
            # "échec" par un "aucune source" qui repartirait de zéro le suivi.
            return
        try:
            result = await alldebrid.start_download(ALLDEBRID_API_KEY, cand["magnet"])
        except alldebrid.AllDebridError:
            return
        wl.set_precache_episode(
            imdb_id, season, int(ep),
            "ready" if result["ready"] else "downloading", result["id"], magnet=cand["magnet"],
        )

    async def _refresh_ep(ep: str, info: dict) -> None:
        status = info.get("status")
        if status in ("failed", "none"):
            await _retry_candidate(ep, info)
            return
        if status != "downloading" or "magnet_id" not in info:
            return
        async with sem:
            st = await alldebrid.get_status(ALLDEBRID_API_KEY, info["magnet_id"])
        if st["ready"]:
            wl.set_precache_episode(imdb_id, season, int(ep), "ready", info["magnet_id"])
            return
        if not (st["failed"] or _is_stalled(st, info.get("started_at"))):
            return
        # Ce candidat est mort (ex. 0 seeder jamais reparti) : on retente
        # automatiquement avec le prochain candidat plausible avant
        # d'abandonner l'épisode — cas réel (2026-07-20) où le plus petit
        # candidat automatique s'est révélé être un torrent mort.
        await _retry_candidate(ep, info)

    await asyncio.gather(*(_refresh_ep(ep, info) for ep, info in ps["episodes"].items()))

    entry = wl.get(imdb_id)
    ps = entry.get("precache_seasons", {}).get(str(season), {})
    episodes = ps.get("episodes", {})
    counts: dict[str, int] = {}
    for info in episodes.values():
        counts[info["status"]] = counts.get(info["status"], 0) + 1
    return {"status": "ok", "season": season, "total": ps.get("total", len(episodes)), "counts": counts, "episodes": episodes}


@app.post("/api/watchlist/{imdb_id}/precache-season/{season}/episode/{episode}/magnet")
async def api_precache_episode_magnet(
    imdb_id: str, season: int, episode: int, payload: dict, wl: Watchlist = Depends(resolve_session),
):
    """Ajout manuel d'un magnet pour un épisode précis — repli quand Wacustom
    ne propose qu'un torrent mort (0 seeder) ou rien du tout : l'utilisateur
    colle un lien trouvé lui-même (ex. directement sur un tracker) et VODIO le
    suit exactement comme un candidat automatique."""
    if not ALLDEBRID_API_KEY:
        raise HTTPException(status_code=503, detail="AllDebrid non configuré")
    magnet = (payload.get("magnet") or "").strip()
    if not magnet.startswith("magnet:"):
        raise HTTPException(status_code=400, detail="Lien magnet invalide")
    entry = wl.get(imdb_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Titre absent de la watchlist")
    ps = entry.get("precache_seasons", {}).get(str(season))
    if not ps or str(episode) not in ps.get("episodes", {}):
        raise HTTPException(status_code=404, detail="Aucun suivi de pré-cache pour cet épisode")

    try:
        result = await alldebrid.start_download(ALLDEBRID_API_KEY, magnet)
    except alldebrid.AllDebridError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    wl.set_precache_episode(
        imdb_id, season, episode, "ready" if result["ready"] else "downloading", result["id"], magnet=magnet,
    )
    return {"status": "ready" if result["ready"] else "downloading", "magnet_id": result["id"]}


@app.delete("/api/watchlist/{imdb_id}")
async def api_remove(imdb_id: str, wl: Watchlist = Depends(resolve_session)):
    return {"removed": wl.remove(imdb_id)}


@app.get("/health")
async def health():
    age_h = (time.time() - state["last_refresh"]) / 3600 if state["last_refresh"] else -1
    ok = bool(state["metas"]) and 0 <= age_h < 48
    body = {
        "ok": ok,
        "films": len(state["metas"]),
        "watchlist": len(watchlist.items),
        "last_refresh_age_hours": round(age_h, 1),
        "last_error": state["last_error"],
        "c411_titles": len(state_c411["metas"]),
    }
    return JSONResponse(body, status_code=200 if ok else 503)
