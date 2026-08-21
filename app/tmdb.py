"""Matching AlloCiné → TMDB → ID IMDb.

Stratégie MVP : recherche par titre original (fallback titre FR, langue fr-FR),
premier résultat (tri popularité TMDB — fiable pour des sorties VOD récentes).
L'ID IMDb (tt…) est la clé d'intégration Stremio : Cinemeta/AIOMetadata
fournissent les métadonnées et AIOStreams trouve les sources.
"""
import datetime
import logging

import httpx

log = logging.getLogger("vodio.tmdb")

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"
BACKDROP = "https://image.tmdb.org/t/p/w1280"


async def get_trending(api_key: str, window: str = "day") -> list[dict]:
    """Tendances TMDB (films) pour le bandeau héros défilant — indépendant du
    catalogue AlloCiné, pas forcément disponible via une source Wacustom
    (juste une vitrine de découverte, l'ajout à la liste marche pareil)."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"{API}/trending/movie/{window}", params={"api_key": api_key, "language": "fr-FR"}
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("get_trending en échec : %s", exc)
            return []
    results = []
    for r in resp.json().get("results", [])[:10]:
        if not r.get("backdrop_path"):
            continue
        results.append({
            "tmdb_id": r["id"],
            "media_type": "movie",
            "name": r.get("title", ""),
            "overview": r.get("overview", ""),
            "backdrop": BACKDROP + r["backdrop_path"],
            "poster": IMG + r["poster_path"] if r.get("poster_path") else None,
            "year": (r.get("release_date") or "")[:4],
        })
    return results


async def get_digital_releases(api_key: str) -> list[dict]:
    """Sorties digitales récentes TMDB (onglet VOD) — pure vitrine, sans
    vérif de dispo (contrairement à AlloCiné/AIOStreams pour le reste de
    l'onglet). Type 4 = release TMDB "Digital" (distinct de 3 = salles) —
    fenêtre 90 jours + tri date desc pour rester sur des sorties VOD
    récentes, pas tout l'historique TMDB."""
    today = datetime.date.today().isoformat()
    ninety_days_ago = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"{API}/discover/movie",
                params={
                    "api_key": api_key, "language": "fr-FR", "region": "FR", "page": 1,
                    "with_release_type": "4",
                    "sort_by": "release_date.desc",
                    "release_date.gte": ninety_days_ago,
                    "release_date.lte": today,
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("get_digital_releases en échec : %s", exc)
            return []
    items = []
    for r in resp.json().get("results", []):
        if not r.get("poster_path"):
            continue
        items.append({
            "tmdb_id": r["id"],
            "media_type": "movie",
            "name": r.get("title", ""),
            "overview": r.get("overview", ""),
            "poster": IMG + r["poster_path"],
            "backdrop": BACKDROP + r["backdrop_path"] if r.get("backdrop_path") else None,
            "year": (r.get("release_date") or "")[:4],
        })
    return items[:20]


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
        "tmdb_id": tmdb_id,
        "name": film["title"],
        "description": film.get("synopsis", ""),
    }
    if result.get("poster_path"):
        meta["poster"] = IMG + result["poster_path"]
    if result.get("release_date"):
        meta["releaseInfo"] = result["release_date"][:4]
    # Date de sortie exacte scrapée (agenda cinéma) — plus précise que l'année
    # TMDB seule, utile pour afficher "sort le 22 juillet 2026" côté cinéma.
    if film.get("release_date"):
        meta["cinema_release_date"] = film["release_date"]
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
        "tmdb_id": tmdb_id,  # nécessaire pour retrouver les saisons (pré-cache)
        "name": d.get("title") or d.get("name") or d.get("original_title") or d.get("original_name", ""),
        "description": d.get("overview", ""),
    }
    if d.get("poster_path"):
        meta["poster"] = IMG + d["poster_path"]
    if date:
        meta["releaseInfo"] = date[:4]
        meta["release_date"] = date
    if is_series and d.get("number_of_seasons"):
        meta["seasons"] = d["number_of_seasons"]
    return meta


async def build_meta_from_external(
    api_key: str, imdb_id: str | None, tmdb_id: int | None, media_type_hint: str,
) -> dict | None:
    """Construit un meta Stremio à partir d'un item source qui fournit déjà
    un imdb_id et/ou tmdb_id (ex. C411/Torznab) — pas de recherche floue par
    titre comme `match_film`, donc bien plus fiable quand l'identifiant est
    disponible."""
    if tmdb_id:
        return await build_meta_from_tmdb(api_key, tmdb_id, media_type_hint)
    if not imdb_id:
        return None
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"{API}/find/{imdb_id}",
                params={"api_key": api_key, "external_source": "imdb_id", "language": "fr-FR"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("build_meta_from_external %s en échec : %s", imdb_id, exc)
            return None
    data = resp.json()
    media_type = media_type_hint
    results = data.get("tv_results" if media_type == "series" else "movie_results", [])
    if not results:
        # La catégorie source pouvait être fausse (constaté sur C411) : on
        # retente l'autre type avant d'abandonner.
        media_type = "movie" if media_type == "series" else "series"
        results = data.get("tv_results" if media_type == "series" else "movie_results", [])
    if not results:
        log.info("pas de correspondance TMDB pour imdb:%s", imdb_id)
        return None
    r = results[0]
    meta = {
        "id": imdb_id,
        "type": media_type,
        "tmdb_id": r["id"],
        "name": r.get("title") or r.get("name", ""),
        "description": r.get("overview", ""),
    }
    if r.get("poster_path"):
        meta["poster"] = IMG + r["poster_path"]
    date = r.get("release_date") or r.get("first_air_date")
    if date:
        meta["releaseInfo"] = date[:4]
    return meta


async def get_trailer(api_key: str, tmdb_id: int, media_type: str = "movie") -> str | None:
    """Clé YouTube de la bande-annonce officielle (endpoint TMDB dédié
    /videos, pas dans les détails de base) — récupérée à la demande à
    l'ouverture de la fiche plutôt que stockée, pour ne pas alourdir le
    scraping VOD/Cinéma d'un appel TMDB de plus par film.

    ⚠️ `language` ET `include_video_language` sont tous les deux nécessaires,
    ils ne font pas la même chose :
    - sans `language`, TMDB répond en `en-US` → BA systématiquement en anglais
      alors qu'une VF existe souvent (constaté le 2026-08-05 : Le Comte de
      Monte-Cristo renvoyait 1 vidéo `en`, et `fr` avec le paramètre) ;
    - avec `language=fr-FR` SEUL, TMDB ne renvoie QUE les vidéos françaises →
      aucune BA du tout sur les titres qui n'en ont pas (couverture FR très
      inégale). `include_video_language=fr,en,null` élargit la réponse aux
      vidéos anglaises et sans langue déclarée, qu'on garde en repli.
    Le tri ci-dessous préfère donc le français, puis retombe sur le reste :
    une BA anglaise vaut mieux que pas de BA.
    """
    tmdb_path = "tv" if media_type == "series" else "movie"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"{API}/{tmdb_path}/{tmdb_id}/videos",
                params={
                    "api_key": api_key,
                    "language": "fr-FR",
                    "include_video_language": "fr,en,null",
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("get_trailer %s:%s en échec : %s", tmdb_path, tmdb_id, exc)
            return None
    videos = resp.json().get("results", [])
    youtube = [v for v in videos if v.get("site") == "YouTube"]
    trailers = [v for v in youtube if v.get("type") == "Trailer"]
    pool = trailers or youtube
    if not pool:
        return None
    # Clé de tri (False < True) : français d'abord, puis officiel d'abord.
    pool.sort(key=lambda v: (v.get("iso_639_1") != "fr", not v.get("official")))
    return pool[0].get("key")


async def get_tmdb_id(api_key: str, imdb_id: str, media_type: str = "series") -> int | None:
    """Repli pour les entrées watchlist créées avant le stockage de tmdb_id."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"{API}/find/{imdb_id}", params={"api_key": api_key, "external_source": "imdb_id"}
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("get_tmdb_id %s en échec : %s", imdb_id, exc)
            return None
    results = resp.json().get("tv_results" if media_type == "series" else "movie_results", [])
    return results[0]["id"] if results else None


async def get_season_episode_count(api_key: str, tmdb_id: int, season: int) -> int | None:
    """Nombre d'épisodes d'une saison donnée (pré-cache saison entière)."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"{API}/tv/{tmdb_id}/season/{season}",
                params={"api_key": api_key, "language": "fr-FR"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("get_season_episode_count tv:%s saison %s en échec : %s", tmdb_id, season, exc)
            return None
    episodes = resp.json().get("episodes", [])
    return len(episodes) or None


async def get_release_date(api_key: str, imdb_id: str, media_type: str = "movie") -> str | None:
    """Repli pour les entrées watchlist créées avant l'ajout de release_date
    (cf. garde-fou anti-CAM des vérifications actives AllDebrid)."""
    tmdb_path = "tv" if media_type == "series" else "movie"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"{API}/find/{imdb_id}", params={"api_key": api_key, "external_source": "imdb_id"}
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("get_release_date %s en échec : %s", imdb_id, exc)
            return None
    results = resp.json().get("tv_results" if media_type == "series" else "movie_results", [])
    if not results:
        return None
    date = results[0].get("first_air_date") if media_type == "series" else results[0].get("release_date")
    return date or None
