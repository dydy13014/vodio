"""VODIO — addon Stremio « Nouveautés VOD FR » + watchlist personnelle.

Deux catalogues :
- `vodio-new` : nouveautés VOD scrapées d'AlloCiné (refresh 24h).
- `vodio-watchlist` : films ajoutés à la main via la page web `/` (protégée par
  mot de passe), avec badges de disponibilité ✅/⏳.

Servi derrière Traefik en PathPrefix /vodio (stripprefix) : l'app expose tout à
la racine. Manifest et catalogues restent publics (Stremio en a besoin) ; seules
les routes de gestion `/api/*` exigent le mot de passe VODIO_PASSWORD.
"""
import asyncio
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from . import availability, notify, scraper, tmdb
from .watchlist import Watchlist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("vodio")

TMDB_API_KEY = os.environ["TMDB_API_KEY"]
ALLOCINE_PAGES = int(os.environ.get("ALLOCINE_PAGES", "3"))
REFRESH_HOURS = int(os.environ.get("REFRESH_HOURS", "24"))
DATA_FILE = Path(os.environ.get("DATA_FILE", "/app/data/catalog.json"))
WATCHLIST_FILE = os.environ.get("WATCHLIST_FILE", "/app/data/watchlist.json")
VODIO_PASSWORD = os.environ.get("VODIO_PASSWORD", "")
STATIC_DIR = Path(__file__).parent / "static"
# Badges ✅/⏳ via AIOStreams (config compte dydy) — désactivés si CONFIG absent
STREAM_CHECK_URL = os.environ.get("STREAM_CHECK_URL", "http://aiostreams:3000")
STREAM_CHECK_CONFIG = os.environ.get("STREAM_CHECK_CONFIG", "")
# Résolution minimale pour un badge ✅ (sinon ⏳). 720 = exclut CAM/TS/sans réso.
QUALITY_MIN = int(os.environ.get("VODIO_QUALITY_MIN", "720"))

MANIFEST = {
    # Personnalisable par instance : changer l'id évite les collisions si un
    # utilisateur installe plusieurs instances VODIO. Défaut = instance d'origine.
    "id": os.environ.get("VODIO_ADDON_ID", "org.eddy.vodio"),
    "version": "1.3.0",
    "name": os.environ.get("VODIO_ADDON_NAME", "VODIO"),
    "description": "Nouveautés VOD françaises (AlloCiné) + ma liste perso (films & séries)",
    "resources": ["catalog"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [
        {"type": "movie", "id": "vodio-new", "name": "Nouveautés VOD"},
        {"type": "movie", "id": "vodio-soon", "name": "Bientôt en VOD"},
        {"type": "movie", "id": "vodio-watchlist", "name": "VODIO - Ma liste"},
        {"type": "series", "id": "vodio-watchlist", "name": "VODIO - Ma liste"},
    ],
    "behaviorHints": {"configurable": False},
}

state = {"metas": [], "last_refresh": 0.0, "last_error": ""}
watchlist = Watchlist(WATCHLIST_FILE)


def load_cache() -> None:
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text())
            state["metas"] = data["metas"]
            state["last_refresh"] = data["last_refresh"]
            log.info("cache chargé : %d films", len(state["metas"]))
        except (json.JSONDecodeError, KeyError) as exc:
            log.warning("cache illisible, ignoré : %s", exc)


def save_cache() -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps({"metas": state["metas"], "last_refresh": state["last_refresh"]})
    )


async def _badge(metas: list[dict]) -> None:
    if STREAM_CHECK_CONFIG and metas:
        await availability.add_availability_badges(
            STREAM_CHECK_URL, STREAM_CHECK_CONFIG, metas, QUALITY_MIN
        )


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
    # Rafraîchit les badges de la watchlist + SMS sur les passages ⏳ → ✅.
    current = watchlist.metas()  # non vus, avec les anciens badges
    was_available = {m["id"]: m["name"].startswith("✅") for m in current}
    wl = [_strip_badge(m) for m in current]
    if wl:
        await _badge(wl)
        watchlist.replace_all(wl)
        log.info("watchlist : badges rafraîchis (%d titres)", len(wl))
        for m in wl:
            if m["name"].startswith("✅") and not was_available.get(m["id"]):
                title = _strip_badge(m)["name"]
                await notify.send_sms(f"VODIO : « {title} » est dispo ! ✅")
                log.info("SMS envoyé : %s dispo", title)


def _strip_badge(meta: dict) -> dict:
    """Copie du meta sans le préfixe ✅/⏳ (pour recalculer proprement)."""
    m = dict(meta)
    name = m.get("name", "")
    if name[:2] in ("✅ ", "⏳ "):
        m["name"] = name[2:]
    return m


async def refresh_loop() -> None:
    while True:
        try:
            await refresh()
        except Exception:
            log.exception("refresh en échec")
            state["last_error"] = "refresh : exception (voir logs)"
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


def require_password(x_vodio_password: str = Header(default="")) -> None:
    if not VODIO_PASSWORD or not hmac.compare_digest(x_vodio_password, VODIO_PASSWORD):
        raise HTTPException(status_code=401, detail="Mot de passe invalide")


# ── Endpoints publics (Stremio) ────────────────────────────────────────────
@app.get("/manifest.json")
async def manifest():
    return JSONResponse(MANIFEST, headers=CORS)


def _clean_name(meta: dict) -> dict:
    m = dict(meta)
    if m.get("name", "")[:2] in ("✅ ", "⏳ "):
        m["name"] = m["name"][2:]
    return m


@app.get("/catalog/movie/vodio-new.json")
async def catalog_new():
    # Dispo maintenant = badge ✅ (ou pas de badge si check désactivé).
    metas = [_clean_name(m) for m in state["metas"] if not m["name"].startswith("⏳")]
    return JSONResponse({"metas": metas}, headers=CORS)


@app.get("/catalog/movie/vodio-soon.json")
async def catalog_soon():
    # Bientôt = listé sur AlloCiné mais pas encore dispo en ≥720p (badge ⏳).
    metas = [_clean_name(m) for m in state["metas"] if m["name"].startswith("⏳")]
    return JSONResponse({"metas": metas}, headers=CORS)


@app.get("/catalog/movie/vodio-watchlist.json")
async def catalog_watchlist_movie():
    return JSONResponse({"metas": watchlist.metas("movie")}, headers=CORS_LIVE)


@app.get("/catalog/series/vodio-watchlist.json")
async def catalog_watchlist_series():
    return JSONResponse({"metas": watchlist.metas("series")}, headers=CORS_LIVE)


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


# ── Page web de gestion + assets PWA ────────────────────────────────────────
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


# ── Endpoints de gestion (protégés) ─────────────────────────────────────────
@app.get("/api/search", dependencies=[Depends(require_password)])
async def api_search(q: str):
    if not q.strip():
        return {"results": []}
    return {"results": await tmdb.search_titles(TMDB_API_KEY, q)}


@app.get("/api/watchlist", dependencies=[Depends(require_password)])
async def api_watchlist():
    # all_items inclut les vus (flag watched) pour la section « Déjà vus » de l'UI.
    return {"items": watchlist.all_items()}


async def _badge_and_persist(entry: dict) -> None:
    """Calcule le badge d'un titre en arrière-plan puis sauvegarde (UI snappy)."""
    try:
        await _badge([entry])  # mute entry["name"] (objet stocké dans la liste)
        watchlist.persist()
    except Exception:
        log.exception("badge en arrière-plan en échec pour %s", entry.get("id"))


@app.post("/api/watchlist", dependencies=[Depends(require_password)])
async def api_add(payload: dict):
    tmdb_id = payload.get("tmdb_id")
    media_type = payload.get("media_type", "movie")
    if not tmdb_id:
        raise HTTPException(status_code=400, detail="tmdb_id requis")
    meta = await tmdb.build_meta_from_tmdb(TMDB_API_KEY, int(tmdb_id), media_type)
    if not meta:
        raise HTTPException(status_code=404, detail="Titre introuvable ou sans ID IMDb")
    entry = watchlist.add(meta)  # stocké immédiatement, badge calculé après
    if entry is not None:
        asyncio.create_task(_badge_and_persist(entry))
    return {"added": entry is not None, "item": entry}


@app.post("/api/watchlist/{imdb_id}/watched", dependencies=[Depends(require_password)])
async def api_watched(imdb_id: str, payload: dict):
    return {"ok": watchlist.set_watched(imdb_id, bool(payload.get("watched", True)))}


@app.delete("/api/watchlist/{imdb_id}", dependencies=[Depends(require_password)])
async def api_remove(imdb_id: str):
    return {"removed": watchlist.remove(imdb_id)}
