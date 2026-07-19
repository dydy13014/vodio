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
import logging
import re

import httpx

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


def _strip(name: str) -> str:
    return name[2:] if name[:2] in ("✅ ", "⏳ ") else name


def _is_cached(stream_name: str) -> bool:
    """Un flux ⚡ (cache instantané) a déjà été réellement téléchargé par
    quelqu'un — sa qualité annoncée est donc fiable. Un flux ⏳ (torrent non
    caché) n'a jamais été vérifié : son nom peut prétendre n'importe quoi.
    Les DDL (liens directs) n'ont pas de notion de cache et sont marqués ⚡
    par convention par AIOStreams — ils ne sont donc pas pénalisés ici."""
    return "⚡" in stream_name


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
            if not _is_cached(name):
                continue
            text = f"{name} {s.get('description') or s.get('title') or ''}"
            best = max(best, stream_resolution(text))
        available = best >= min_res
        meta["name"] = ("✅ " if available else "⏳ ") + _strip(meta["name"])
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
