"""Scraper AlloCiné — page « Sorties de la semaine » (/film/agenda/).

Même structure de cartes que scraper.py (VOD), mais URL de fiche film et
regex de titre différentes (fichefilm_gen_cfilm=N.html, pas fichefilm-N/...).
Contient en plus une date de sortie en clair (ex. "22 juillet 2026") — pas de
pagination : une page = les sorties de la semaine en cours (mise à jour par
AlloCiné chaque mercredi).
"""
import base64
import html as htmllib
import logging
import re
from typing import Optional

import httpx

log = logging.getLogger("vodio.cinema_scraper")

BASE_URL = "https://www.allocine.fr"
LIST_PATH = "/film/agenda/"
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
    r'<a class="meta-title-link" href="/film/fichefilm_gen_cfilm=(\d+)\.html">([^<]+)</a>'
)
DATE_RE = re.compile(r'class="date">([^<]+)<')
ORIGINAL_TITLE_RE = re.compile(
    r"Titre original\s*</span>\s*<span[^>]*>([^<]+)</span>", re.S
)
SYNOPSIS_RE = re.compile(r'<div class="synopsis">\s*<div[^>]*>(.*?)</div>', re.S)

_MONTHS = {
    "janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
}


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return htmllib.unescape(text).strip()


def _parse_date(text: str) -> Optional[str]:
    """« 22 juillet 2026 » -> « 2026-07-22 »."""
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", text.strip().lower())
    if not m:
        return None
    day, month_name, year = m.groups()
    month = _MONTHS.get(month_name)
    if not month:
        return None
    return f"{year}-{month:02d}-{int(day):02d}"


def parse_page(page_html: str) -> list[dict]:
    films = []
    for card in CARD_RE.findall(page_html):
        m = TITLE_RE.search(card)
        if not m:
            continue
        title = _clean(m.group(2))
        film = {
            "allocine_id": m.group(1),
            "title": title,
        }
        if dm := DATE_RE.search(card):
            date = _parse_date(dm.group(1))
            if date:
                film["release_date"] = date
        if om := ORIGINAL_TITLE_RE.search(card):
            film["original_title"] = _clean(om.group(1))
        if sm := SYNOPSIS_RE.search(card):
            film["synopsis"] = _clean(sm.group(1))
        films.append(film)
    return films


# Bouton "Précédente" du sélecteur de semaine : la classe CSS encode le lien
# de la semaine d'avant en base64, précédé d'un préfixe fixe non signifiant
# (constaté 2026-07-21 : "ACrL2ZACrpbG0v" + base64("agenda/sem-YYYY-MM-DD/")).
# Le décoder évite de recalculer nous-mêmes les mercredis de sortie ciné FR.
_PREV_LINK_RE = re.compile(
    r'class="([A-Za-z0-9+/=]{20,}) button button-md button-primary-full button-left">'
    r'<i class="icon icon-left icon-arrow-left">'
)
_PREV_LINK_SALT_LEN = 14


def _decode_prev_link(page_html: str) -> Optional[str]:
    m = _PREV_LINK_RE.search(page_html)
    if not m:
        return None
    payload = m.group(1)[_PREV_LINK_SALT_LEN:]
    try:
        decoded = base64.b64decode(payload + "=" * (-len(payload) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if not decoded.startswith("agenda/"):
        return None
    return "/film/" + decoded


async def scrape(weeks: int = 2) -> list[dict]:
    """Récupère la semaine en cours + jusqu'à `weeks - 1` semaines précédentes
    (lien "Précédente" décodé dynamiquement à chaque page, cf. ci-dessus)."""
    films: list[dict] = []
    seen: set[str] = set()
    path = LIST_PATH
    async with httpx.AsyncClient(
        headers={"User-Agent": UA}, timeout=20, follow_redirects=True
    ) as client:
        for _ in range(max(1, weeks)):
            try:
                resp = await client.get(BASE_URL + path)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("page agenda (%s) inaccessible : %s", path, exc)
                break
            page_films = parse_page(resp.text)
            log.info("%s : %d films", path, len(page_films))
            for f in page_films:
                if f["allocine_id"] not in seen:
                    seen.add(f["allocine_id"])
                    films.append(f)
            prev = _decode_prev_link(resp.text)
            if not prev:
                break
            path = prev
    log.info("agenda total : %d films", len(films))
    return films
