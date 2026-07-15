"""Matching AlloCiné → TMDB → ID IMDb.

Stratégie MVP : recherche par titre original (fallback titre FR, langue fr-FR),
premier résultat (tri popularité TMDB — fiable pour des sorties VOD récentes).
L'ID IMDb (tt…) est la clé d'intégration Stremio : Cinemeta/AIOMetadata
fournissent les métadonnées et AIOStreams trouve les sources.
"""
import logging

import httpx

log = logging.getLogger("vodio.tmdb")

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"


async def match_film(client: httpx.AsyncClient, api_key: str, film: dict) -> dict | None:
    """Retourne un meta Stremio ou None si non matché."""
    queries = []
    if film.get("original_title"):
        queries.append((film["original_title"], "en-US"))
    queries.append((film["title"], "fr-FR"))

    result = None
    for query, lang in queries:
        try:
            resp = await client.get(
                f"{API}/search/movie",
                params={"api_key": api_key, "query": query, "language": lang},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("recherche TMDB en échec pour %r : %s", query, exc)
            continue
        results = resp.json().get("results", [])
        if results:
            result = results[0]
            break

    if not result:
        log.info("non matché : %s (%s)", film["title"], film.get("original_title", "-"))
        return None

    tmdb_id = result["id"]
    try:
        resp = await client.get(
            f"{API}/movie/{tmdb_id}/external_ids", params={"api_key": api_key}
        )
        resp.raise_for_status()
        imdb_id = resp.json().get("imdb_id")
    except httpx.HTTPError as exc:
        log.warning("external_ids en échec pour tmdb:%s : %s", tmdb_id, exc)
        return None

    if not imdb_id or not imdb_id.startswith("tt"):
        log.info("pas d'ID IMDb : %s (tmdb:%s)", film["title"], tmdb_id)
        return None

    meta = {
        "id": imdb_id,
        "type": "movie",
        "name": film["title"],
        "description": film.get("synopsis", ""),
    }
    if result.get("poster_path"):
        meta["poster"] = IMG + result["poster_path"]
    if result.get("release_date"):
        meta["releaseInfo"] = result["release_date"][:4]
    return meta


async def match_all(api_key: str, films: list[dict]) -> list[dict]:
    metas = []
    async with httpx.AsyncClient(timeout=15) as client:
        for film in films:
            meta = await match_film(client, api_key, film)
            if meta:
                metas.append(meta)
    log.info("matching : %d/%d films matchés", len(metas), len(films))
    return metas


async def search_titles(api_key: str, query: str) -> list[dict]:
    """Recherche libre films + séries (page web watchlist) → résultats pour l'UI."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"{API}/search/multi",
                params={"api_key": api_key, "query": query, "language": "fr-FR"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("recherche watchlist en échec pour %r : %s", query, exc)
            return []
    out = []
    for r in resp.json().get("results", []):
        mt = r.get("media_type")
        if mt not in ("movie", "tv"):
            continue
        date = r.get("release_date") if mt == "movie" else r.get("first_air_date")
        out.append({
            "tmdb_id": r["id"],
            "media_type": "movie" if mt == "movie" else "series",
            "name": r.get("title") or r.get("name") or r.get("original_title") or r.get("original_name", ""),
            "year": (date or "")[:4],
            "poster": IMG + r["poster_path"] if r.get("poster_path") else None,
            "overview": r.get("overview", ""),
        })
    # Correspondances exactes du titre d'abord (évite d'enterrer un classique
    # sous des titres récents sans rapport), puis les plus récents en tête.
    q = query.strip().casefold()
    out.sort(key=lambda m: (m["name"].casefold() != q, -(int(m["year"]) if m["year"] else 0)))
    return out[:20]


async def build_meta_from_tmdb(
    api_key: str, tmdb_id: int, media_type: str = "movie"
) -> dict | None:
    """Construit un meta Stremio (avec ID IMDb) depuis un tmdb_id + type choisis."""
    is_series = media_type == "series"
    tmdb_path = "tv" if is_series else "movie"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            details = await client.get(
                f"{API}/{tmdb_path}/{tmdb_id}",
                params={"api_key": api_key, "language": "fr-FR"},
            )
            details.raise_for_status()
            ext = await client.get(
                f"{API}/{tmdb_path}/{tmdb_id}/external_ids", params={"api_key": api_key}
            )
            ext.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("build_meta %s:%s en échec : %s", tmdb_path, tmdb_id, exc)
            return None
    d = details.json()
    imdb_id = ext.json().get("imdb_id")
    if not imdb_id or not imdb_id.startswith("tt"):
        log.info("pas d'ID IMDb pour %s:%s", tmdb_path, tmdb_id)
        return None
    date = d.get("first_air_date") if is_series else d.get("release_date")
    meta = {
        "id": imdb_id,
        "type": "series" if is_series else "movie",
        "name": d.get("title") or d.get("name") or d.get("original_title") or d.get("original_name", ""),
        "description": d.get("overview", ""),
    }
    if d.get("poster_path"):
        meta["poster"] = IMG + d["poster_path"]
    if date:
        meta["releaseInfo"] = date[:4]
    return meta
