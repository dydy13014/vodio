"""Nouveautés torrent C411 (Torznab) — croisé avec TMDB + dispo.

Complète le catalogue AlloCiné (`scraper.py`, limité au cinéma FR grand
public) avec les vraies sorties trackées sur C411 : documentaires, séries
étrangères, tout ce qui n'apparaît jamais sur la page VOD d'AlloCiné. Même
tracker que Wacustom (`wastream/scrapers/torznab/base.py`), même format XML,
mais lu ici indépendamment (VODIO et Wacustom sont deux apps séparées).

C411 fournit directement `imdbid`/`tmdbid` en attribut Torznab sur la plupart
des résultats films/séries — pas besoin de matching flou par titre comme pour
AlloCiné (cf. `tmdb.build_meta_from_external`), bien plus fiable.
"""
import logging
import re
import xml.etree.ElementTree as ET

import httpx

from . import availability

log = logging.getLogger("vodio.c411")

_TORZNAB_NS = "{http://torznab.com/schemas/2015/feed}"

# Marqueurs audio français courants dans les noms de release scène.
_FRENCH_RE = re.compile(r"\b(VFF|VFQ|VFI|VF2|MULTI|MULTi|TRUEFRENCH|FRENCH|VOSTFR)\b")
_EPISODE_RE = re.compile(r"\bS\d{1,2}E\d{1,3}\b", re.I)

# Catégories Torznab standard : 2xxx = Movies, 5xxx = TV. Le reste (jeux,
# ebooks, musique...) est hors-sujet pour un catalogue Stremio.
_MOVIE_CAT_PREFIX = "2"
_TV_CAT_PREFIX = "5"


async def fetch_latest(url: str, api_key: str, limit: int = 100, name: str = "C411") -> list[dict]:
    """Derniers torrents ajoutés sur un tracker Torznab (recherche avec `q`
    vide — le tracker renvoie ses derniers ajouts triés par date, pas une
    recherche par titre). Générique : marche à l'identique sur C411, Tr4ker,
    V3X (même format Torznab constaté sur les trois — imdbid/tmdbid en
    attribut, catégories Newznab standard). `limit` est transmis à l'API, pas
    juste appliqué côté client : sans lui ces trackers ne renvoient qu'une
    poignée de résultats par défaut (25 sur C411). Toujours UNE SEULE requête
    quel que soit `limit` — c'est ce qui compte pour rester sous le radar du
    rate-limit (le piège de la tentative précédente
    était une requête PAR FILM, pas la taille d'une requête unique)."""
    params = {"t": "search", "q": "", "apikey": api_key, "limit": str(limit)}
    try:
        # follow_redirects=True : Tr4ker redirige systématiquement /api vers
        # /api/ (301) — httpx ne suit pas les redirections par défaut, la
        # requête échouerait toujours même quand le tracker est en ligne
        # (même piège déjà rencontré et documenté côté Ludio/torznab.py).
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("[%s] injoignable : %s", name, exc)
        return []
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        log.warning("[%s] XML illisible (%s)", name, exc)
        return []

    items = []
    for item in root.findall(".//item")[:limit]:
        title_node = item.find("title")
        if title_node is None or not title_node.text:
            continue
        release_name = title_node.text

        category = None
        imdb_id = None
        tmdb_id = None
        for attr in item.findall(f".//{_TORZNAB_NS}attr"):
            name = attr.attrib.get("name")
            value = attr.attrib.get("value")
            if name == "category":
                category = value
            elif name == "imdbid":
                imdb_id = value
            elif name == "tmdbid" and value and value.isdigit():
                tmdb_id = int(value)

        items.append({
            "release_name": release_name,
            "category": category or "",
            "imdb_id": imdb_id,
            "tmdb_id": tmdb_id,
        })
    return items


def _guess_type(item: dict) -> str | None:
    """movie / series / None (hors-sujet). La catégorie déclarée par C411
    n'est pas fiable seule (constaté : des séries taguées 2000=Movies) — le
    pattern SxxExx dans le nom de release prime sur la catégorie."""
    if _EPISODE_RE.search(item["release_name"]):
        return "series"
    cat = item["category"]
    if cat.startswith(_MOVIE_CAT_PREFIX):
        return "movie"
    if cat.startswith(_TV_CAT_PREFIX):
        return "series"
    return None


def filter_relevant(items: list[dict], quality_min: int) -> list[dict]:
    """Garde uniquement : un identifiant TMDB/IMDb exploitable (sinon
    impossible de matcher un meta Stremio proprement), un marqueur audio
    français dans le nom, une catégorie film/série, et une résolution ≥ seuil
    (même filtre anti-CAM que le reste de VODIO)."""
    out = []
    for item in items:
        if not item.get("imdb_id") and not item.get("tmdb_id"):
            continue
        if not _FRENCH_RE.search(item["release_name"]):
            continue
        media_type = _guess_type(item)
        if media_type is None:
            continue
        if availability.stream_resolution(item["release_name"]) < quality_min:
            continue
        item["media_type"] = media_type
        out.append(item)
    return out
