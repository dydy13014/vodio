"""Scraper AlloCiné — page « Derniers films en VOD » (/vod/films/new/).

Parsing par regex sur la structure des cartes (12 films/page, pagination ?page=N).
Pas de date VOD exacte sur la page liste : l'ordre de la page EST le tri nouveauté.
"""
import html as htmllib
import logging
import re

import httpx

log = logging.getLogger("vodio.scraper")

BASE_URL = "https://www.allocine.fr"
LIST_PATH = "/vod/films/new/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

CARD_RE = re.compile(
    r'<div class="card entity-card entity-card-list cf">.*?'
    r'(?=<div class="card entity-card entity-card-list cf">|<nav class|$)',
    re.S,
)
TITLE_RE = re.compile(
    r'<a class="meta-title-link" href="(/film/fichefilm-(\d+)/[^"]*)">([^<]+)</a>'
)
ORIGINAL_TITLE_RE = re.compile(
    r"Titre original\s*</span>\s*<span[^>]*>([^<]+)</span>", re.S
)
SYNOPSIS_RE = re.compile(r'<div class="synopsis">\s*<div[^>]*>(.*?)</div>', re.S)


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return htmllib.unescape(text).strip()


def parse_page(page_html: str) -> list[dict]:
    films = []
    for card in CARD_RE.findall(page_html):
        m = TITLE_RE.search(card)
        if not m:
            continue
        title = _clean(m.group(3))
        # AlloCiné suffixe les titres de la page VOD par " VOD"
        title = re.sub(r"\s+VOD$", "", title)
        film = {
            "allocine_id": m.group(2),
            "title": title,
        }
        if om := ORIGINAL_TITLE_RE.search(card):
            film["original_title"] = _clean(om.group(1))
        if sm := SYNOPSIS_RE.search(card):
            film["synopsis"] = _clean(sm.group(1))
        films.append(film)
    return films


async def scrape(pages: int = 3) -> list[dict]:
    """Récupère les N premières pages, dans l'ordre nouveauté d'AlloCiné."""
    films: list[dict] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(
        headers={"User-Agent": UA}, timeout=20, follow_redirects=True
    ) as client:
        for page in range(1, pages + 1):
            url = BASE_URL + LIST_PATH
            if page > 1:
                url += f"?page={page}"
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("page %d inaccessible : %s", page, exc)
                continue
            page_films = parse_page(resp.text)
            log.info("page %d : %d films", page, len(page_films))
            for f in page_films:
                if f["allocine_id"] not in seen:
                    seen.add(f["allocine_id"])
                    films.append(f)
    return films
