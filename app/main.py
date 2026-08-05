"""VODIO — addon Stremio « Nouveautés VOD FR » + watchlist personnelle.

Deux catalogues :
- `vodio-new` : nouveautés VOD scrapées d'AlloCiné (refresh 24h).
- `vodio-watchlist` : films ajoutés à la main via la page web `/` (protégée par
  mot de passe), avec badges de disponibilité ✅/⏳.

Servi derrière Traefik en PathPrefix /vodio (stripprefix) : l'app expose tout à
la racine. Manifest et catalogues restent publics (Stremio en a besoin) ; seules
les routes de gestion `/api/*` exigent le mot de passe VODIO_PASSWORD.

Multi-utilisateur (2026-07-22) : l'utilisateur par défaut garde exactement ses
chemins historiques (`/`, `/manifest.json`, `watchlist.json`...). Chaque
utilisateur supplémentaire (VODIO_EXTRA_USERS) a sa propre watchlist et son
propre mot de passe, servis sous `/u/<nom>/...` (même structure de routes,
construite par `build_user_router`) — installation Stremio séparée par
personne. Le catalogue VOD/Cinéma (AlloCiné) reste partagé par tous.
"""
import asyncio
import datetime
import hmac
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from . import alldebrid, availability, cinema_scraper, notify, scraper, tmdb
from .watchlist import Watchlist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("vodio")

TMDB_API_KEY = os.environ["TMDB_API_KEY"]
ALLOCINE_PAGES = int(os.environ.get("ALLOCINE_PAGES", "3"))
REFRESH_HOURS = int(os.environ.get("REFRESH_HOURS", "24"))
DATA_FILE = Path(os.environ.get("DATA_FILE", "/app/data/catalog.json"))
CINEMA_DATA_FILE = Path(os.environ.get("CINEMA_DATA_FILE", "/app/data/cinema.json"))
WATCHLIST_FILE = os.environ.get("WATCHLIST_FILE", "/app/data/watchlist.json")
VODIO_PASSWORD = os.environ.get("VODIO_PASSWORD", "")
STATIC_DIR = Path(__file__).parent / "static"
# Badges ✅/⏳ via AIOStreams (config compte dydy) — désactivés si CONFIG absent
STREAM_CHECK_URL = os.environ.get("STREAM_CHECK_URL", "http://aiostreams:3000")
STREAM_CHECK_CONFIG = os.environ.get("STREAM_CHECK_CONFIG", "")
# Résolution minimale pour un badge ✅ (sinon ⏳). 720 = exclut CAM/TS/sans réso.
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
    "version": "1.4.0",
    "name": os.environ.get("VODIO_ADDON_NAME", "VODIO"),
    "description": "Nouveautés VOD françaises (AlloCiné) + ma liste perso (films & séries)",
    "resources": ["catalog"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [
        # Un seul catalogue "nouveautés" (fusion de l'ancien vodio-soon) : tous
        # les films scrapés viennent de la page AlloCiné "Derniers films en
        # VOD" — par définition déjà sortis sur les plateformes VOD. Le split
        # ✅/⏳ ne reflétait que notre propre check de dispo (Wacustom), pas le
        # vrai statut VOD, d'où la confusion (ex. LES K D'OR 2026-08-02).
        {"type": "movie", "id": "vodio-new", "name": "Nouveautés VOD"},
        {"type": "movie", "id": "vodio-watchlist", "name": "VODIO - Ma liste"},
        {"type": "series", "id": "vodio-watchlist", "name": "VODIO - Ma liste"},
    ],
    "behaviorHints": {"configurable": False},
}

state = {"metas": [], "last_refresh": 0.0, "last_error": ""}
state_cinema = {"metas": [], "last_refresh": 0.0, "last_error": ""}
watchlist = Watchlist(WATCHLIST_FILE)


def _parse_extra_users(raw: str, data_dir: Path) -> dict[str, dict]:
    """`VODIO_EXTRA_USERS=nom1:motdepasse1,nom2:motdepasse2` — chaque nom
    obtient sa propre watchlist (`watchlist_<nom>.json`) et son propre accès,
    servis sous `/u/<nom>/`. L'utilisateur par défaut (VODIO_PASSWORD,
    watchlist.json) n'est pas affecté par ce mécanisme."""
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
            WACUSTOM_URL, WACUSTOM_CONFIG, ALLDEBRID_API_KEY, TMDB_API_KEY, metas, QUALITY_MIN
        )
    else:
        await _badge(metas)


async def _refresh_watchlist_badges(label: str, wl: Watchlist, sms: bool = True) -> None:
    """Rafraîchit les badges d'une watchlist + SMS sur les passages ⏳ → ✅.
    `label` (nom d'utilisateur, vide pour le défaut) préfixe le SMS pour
    distinguer qui a un titre dispo — toutes les watchlists partagent le
    même numéro Free Mobile. `sms=False` (VODIO_NOSMS_USERS) désactive
    uniquement la notification, pas le calcul des badges."""
    current = wl.metas()  # non vus, avec les anciens badges
    was_available = {m["id"]: m["name"].startswith("✅") for m in current}
    items = [_strip_badge(m) for m in current]
    if not items:
        return
    await _badge_watchlist(items)
    wl.replace_all(items)
    log.info("watchlist%s : badges rafraîchis (%d titres)", f" ({label})" if label else "", len(items))
    if not sms:
        return
    for m in items:
        if m["name"].startswith("✅") and not was_available.get(m["id"]):
            title = _strip_badge(m)["name"]
            prefix = f"VODIO ({label})" if label else "VODIO"
            await notify.send_sms(f"{prefix} : « {title} » est dispo ! ✅")
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


_BADGE_RE = re.compile(r"^[✅⏳⚡]+\s*")


def _strip_badge(meta: dict) -> dict:
    """Copie du meta sans le préfixe ✅/⚡/⏳ (pour recalculer proprement)."""
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
                    WACUSTOM_URL, WACUSTOM_CONFIG, TMDB_API_KEY, entry, QUALITY_MIN, season, ep
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


def build_user_router(wl: Watchlist, password: str) -> APIRouter:
    """Construit l'ensemble des routes (Stremio + page web + API de gestion)
    pour un utilisateur donné : sa propre watchlist, son propre mot de passe.
    Le catalogue VOD/Cinéma (AlloCiné) reste partagé — mêmes fonctions
    globales `state`/`state_cinema`, dupliquées uniquement pour que le
    manifest de chaque utilisateur puisse les résoudre (Stremio préfixe
    toutes les requêtes de ressources par l'URL du manifest installé)."""
    router = APIRouter()

    def require_pw(x_vodio_password: str = Header(default="")) -> None:
        if not password or not hmac.compare_digest(x_vodio_password, password):
            raise HTTPException(status_code=401, detail="Mot de passe invalide")

    # ── Endpoints publics (Stremio) ─────────────────────────────────────────
    @router.get("/manifest.json")
    async def manifest_route():
        return JSONResponse(MANIFEST, headers=CORS)

    @router.get("/catalog/movie/vodio-new.json")
    async def catalog_new():
        metas = [_clean_name(m) for m in state["metas"]]
        return JSONResponse({"metas": metas}, headers=CORS)

    @router.get("/catalog/movie/vodio-watchlist.json")
    async def catalog_watchlist_movie():
        return JSONResponse({"metas": wl.metas("movie")}, headers=CORS_LIVE)

    @router.get("/catalog/series/vodio-watchlist.json")
    async def catalog_watchlist_series():
        return JSONResponse({"metas": wl.metas("series")}, headers=CORS_LIVE)

    # ── Page web de gestion + assets PWA ────────────────────────────────────
    @router.get("/")
    async def home():
        return FileResponse(STATIC_DIR / "index.html")

    @router.get("/manifest.webmanifest")
    async def pwa_manifest():
        return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @router.get("/sw.js")
    async def pwa_sw():
        return FileResponse(
            STATIC_DIR / "sw.js", media_type="application/javascript",
            headers={"Service-Worker-Allowed": "./"},
        )

    @router.get("/{icon}.png")
    async def pwa_icon(icon: str):
        path = STATIC_DIR / f"{icon}.png"
        if not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="image/png")

    # ── Endpoints de gestion (protégés) ─────────────────────────────────────
    @router.get("/api/vod", dependencies=[Depends(require_pw)])
    async def api_vod():
        return {"items": state["metas"], "last_refresh": state["last_refresh"]}

    @router.get("/api/cinema", dependencies=[Depends(require_pw)])
    async def api_cinema():
        return {"items": state_cinema["metas"], "last_refresh": state_cinema["last_refresh"]}

    @router.get("/api/search", dependencies=[Depends(require_pw)])
    async def api_search(q: str):
        if not q.strip():
            return {"results": []}
        return {"results": await tmdb.search_titles(TMDB_API_KEY, q)}

    @router.get("/api/trailer/{tmdb_id}", dependencies=[Depends(require_pw)])
    async def api_trailer(tmdb_id: int, media_type: str = "movie"):
        return {"key": await tmdb.get_trailer(TMDB_API_KEY, tmdb_id, media_type)}

    @router.get("/api/trending", dependencies=[Depends(require_pw)])
    async def api_trending():
        return {"items": await tmdb.get_trending(TMDB_API_KEY)}

    @router.get("/api/digital-releases", dependencies=[Depends(require_pw)])
    async def api_digital_releases():
        return {"items": await tmdb.get_digital_releases(TMDB_API_KEY)}

    @router.get("/api/watchlist", dependencies=[Depends(require_pw)])
    async def api_watchlist():
        # all_items inclut les vus (flag watched) pour la section « Déjà vus » de l'UI.
        return {"items": wl.all_items()}

    @router.post("/api/watchlist", dependencies=[Depends(require_pw)])
    async def api_add(payload: dict):
        tmdb_id = payload.get("tmdb_id")
        media_type = payload.get("media_type", "movie")
        if not tmdb_id:
            raise HTTPException(status_code=400, detail="tmdb_id requis")
        meta = await tmdb.build_meta_from_tmdb(TMDB_API_KEY, int(tmdb_id), media_type)
        if not meta:
            raise HTTPException(status_code=404, detail="Titre introuvable ou sans ID IMDb")
        entry = wl.add(meta)  # stocké immédiatement, badge calculé après
        if entry is not None:
            asyncio.create_task(_badge_and_persist(wl, entry))
        return {"added": entry is not None, "item": entry}

    @router.post("/api/watchlist/{imdb_id}/watched", dependencies=[Depends(require_pw)])
    async def api_watched(imdb_id: str, payload: dict):
        return {"ok": wl.set_watched(imdb_id, bool(payload.get("watched", True)))}

    @router.post("/api/watchlist/{imdb_id}/precache", dependencies=[Depends(require_pw)])
    async def api_precache(imdb_id: str):
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
            WACUSTOM_URL, WACUSTOM_CONFIG, TMDB_API_KEY, entry, QUALITY_MIN
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

    @router.get("/api/watchlist/{imdb_id}/precache", dependencies=[Depends(require_pw)])
    async def api_precache_status(imdb_id: str):
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

    @router.get("/api/watchlist/{imdb_id}/download", dependencies=[Depends(require_pw)])
    async def api_download(imdb_id: str):
        """Lien de téléchargement direct d'un film disponible (watchlist
        uniquement). Le lien AllDebrid est débloqué côté serveur puis relayé
        via MediaFlow — un lien AllDebrid brut est lié à l'IP qui l'a
        débloqué, MediaFlow permet de télécharger depuis n'importe quel réseau
        (même principe qu'un partage manuel de lien MediaFlow).

        Un badge ✅ ne veut PAS dire que VODIO a lui-même déclenché un
        pré-cache (`precache_magnet_id` peut être absent — cas courant : le
        titre était déjà caché ailleurs, trouvé directement par Wacustom, ou
        via un signal externe/un check AllDebrid ponctuel, cf.
        `_check_one_watchlist`). Si aucun magnet n'est encore suivi, on
        retrouve le meilleur candidat via find_precache_candidate (même
        logique que le bouton Précharger) et on le pousse sur AllDebrid —
        quasi instantané si vraiment déjà caché (cohérent avec le badge ✅),
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
                WACUSTOM_URL, WACUSTOM_CONFIG, TMDB_API_KEY, entry, QUALITY_MIN
            )
            if cand["status"] not in ("cached", "candidate"):
                raise HTTPException(status_code=409, detail="Aucune source exploitable trouvée pour ce film")
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
                # Badge ✅ basé sur une source DDL (non téléchargeable par ce
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

        proxy_url = (
            f"{MEDIAFLOW_URL}/proxy/stream/{quote(file['filename'])}"
            f"?d={quote(file['link'], safe='')}&api_password={quote(MEDIAFLOW_API_PASSWORD)}"
        )
        return {"download_url": proxy_url, "filename": file["filename"]}

    @router.post("/api/watchlist/{imdb_id}/precache-season", dependencies=[Depends(require_pw)])
    async def api_precache_season(imdb_id: str, payload: dict):
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

    @router.get("/api/watchlist/{imdb_id}/precache-season/{season}", dependencies=[Depends(require_pw)])
    async def api_precache_season_status(imdb_id: str, season: int):
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
                    season, int(ep), exclude_magnets=tried,
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

    @router.post(
        "/api/watchlist/{imdb_id}/precache-season/{season}/episode/{episode}/magnet",
        dependencies=[Depends(require_pw)],
    )
    async def api_precache_episode_magnet(imdb_id: str, season: int, episode: int, payload: dict):
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

    @router.delete("/api/watchlist/{imdb_id}", dependencies=[Depends(require_pw)])
    async def api_remove(imdb_id: str):
        return {"removed": wl.remove(imdb_id)}

    return router


app.include_router(build_user_router(watchlist, VODIO_PASSWORD))
for _name, _udata in EXTRA_USERS.items():
    app.include_router(build_user_router(_udata["watchlist"], _udata["password"]), prefix=f"/u/{_name}")
    log.info("utilisateur additionnel enregistré : %s (/u/%s/)", _name, _name)


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
    }
    return JSONResponse(body, status_code=200 if ok else 503)
