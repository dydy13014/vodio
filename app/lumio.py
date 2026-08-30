"""Intégration Lumio (mylumio.tv) — cache mutualisé, réservée à la watchlist
(liste courte) : le quota Lumio est bas et se bloque globalement en cas de
dépassement (24h de pause), inadapté au catalogue principal qui vérifie des
dizaines de titres à chaque refresh. Optionnelle (LUMIO_MANIFEST_ID), chaque
utilisateur fournit son propre manifest.

Interroge directement l'endpoint Stremio de Lumio (mylumio.tv) pour savoir si
un titre est déjà vérifié en cache par leur base mutualisée, indépendamment
de ce que Wacustom en garde après sa propre déduplication — qui peut faire
disparaître le signal (un flux étiqueté "Lumio" perd la déduplication face à
un doublon trouvé par une autre source, même si les deux pointent vers le
même torrent réellement caché). Interroger Lumio en direct évite ce problème.
"""
import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("vodio.private_cache_check")

_BASE_URL = "https://mylumio.tv"
_MANIFEST_ID = os.environ.get("LUMIO_MANIFEST_ID", "")

# Marqueurs de vraie VF (doublage) — VOSTFR exclu à dessein : Lumio est une
# base mutualisée, "⚡" confirme juste qu'UN fichier est en cache, pas sa
# langue. Vérifié en réel le 2026-08-22 (tt32338669, "Mutiny") : les 2 flux
# en cache étaient tous les deux VOSTFR (behaviorHints.filename
# "Mutiny.2026.VOSTFR.1080p.WEB-DL.x264-FS.mkv") — badgé "dispo" + SMS envoyé
# à tort avant ce correctif, alors qu'aucune VF n'existait.
_DUB_RE = re.compile(r"\b(VFF|VFQ|VFI|VF2|MULTI|MULTi|TRUEFRENCH|FRENCH)\b")

# Quota Lumio très bas et blocage global (toute requête compte). Surtout :
# chaque appel émis PENDANT un blocage le prolonge — inutile de retenter vite.
# La pause est écrite sur disque (volume de données) pour survivre à un
# redémarrage, sinon on repart taper aussitôt. Le fichier est partagé avec
# l'autre service qui interroge Lumio si les volumes sont montés en commun.
_RATE_LIMIT_PAUSE_S = int(os.environ.get("LUMIO_RATE_LIMIT_PAUSE", "86400"))
_PAUSE_FILE = Path(os.environ.get("LUMIO_PAUSE_FILE", "/app/data/lumio_pause"))


def _paused_for() -> int:
    """Secondes restantes de pause quota, 0 si on peut interroger."""
    try:
        return max(0, int(float(_PAUSE_FILE.read_text().strip()) - time.time()))
    except (OSError, ValueError):
        return 0


def _start_pause() -> None:
    try:
        _PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PAUSE_FILE.write_text(str(time.time() + _RATE_LIMIT_PAUSE_S))
    except OSError as exc:
        log.warning("pause non persistée : %s", exc)


async def _fetch_streams(path: str) -> Optional[list[dict]]:
    """None si le quota est en pause ou si la requête échoue — distinct
    d'une liste vide (titre vraiment absent du cache Lumio)."""
    if not _MANIFEST_ID or _paused_for():
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{_BASE_URL}/{_MANIFEST_ID}/{path}")
            if resp.status_code == 429:
                _start_pause()
                log.warning("quota atteint (429) — pause %dh", _RATE_LIMIT_PAUSE_S // 3600)
                return None
            resp.raise_for_status()
            return resp.json().get("streams", [])
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        log.warning("requête Lumio échouée : %s", exc)
        return None


async def is_cached(
    imdb_id: str, content_type: str = "movie",
    season: Optional[int] = None, episode: Optional[int] = None,
) -> bool:
    if content_type == "series" and season and episode:
        path = f"stream/series/{imdb_id}:{season}:{episode}.json"
    else:
        path = f"stream/movie/{imdb_id}.json"
    streams = await _fetch_streams(path)
    if not streams:
        return False
    for s in streams:
        if "⚡" not in s.get("name", ""):
            continue
        filename = s.get("behaviorHints", {}).get("filename", "")
        if _DUB_RE.search(f"{filename} {s.get('description', '')}"):
            return True
    return False


# Nombre de flux "lien direct" tentés avant d'abandonner — un candidat peut
# individuellement échouer ("Résolution impossible" si le lien sous-jacent
# est mort côté hébergeur, constaté le 2026-08-14 sur le plus gros des
# candidats alors qu'un plus petit du même film résolvait sans problème) ;
# borné pour ne pas gaspiller le quota Lumio partagé sur un titre têtu.
_MAX_DIRECT_ATTEMPTS = 3


def _direct_stream_candidates(streams: list[dict]) -> list[dict]:
    """Flux déjà pré-résolus côté Lumio (candidat "d" = lien hébergeur
    direct, quel que soit l'hébergeur — alldebrid, rapidgator, turbobit…
    Lumio résout tout ça côté serveur avec ses propres comptes premium et
    renvoie systématiquement un lien final *.debrid.it, vérifié
    empiriquement le 2026-08-14 y compris pour un lien rapidgator caché
    derrière un dl-protect.link). Triés du plus gros au plus petit — mais un
    candidat peut individuellement être mort, d'où plusieurs tentatives côté
    appelant. Les flux "hash torrent" (candidat "t") restent écartés : ils
    demandent à Lumio une re-vérification cache en direct au moment de la
    résolution, encore moins fiable."""
    found = []
    for s in streams:
        parts = s.get("url", "").rsplit("/playback/", 1)
        if len(parts) != 2:
            continue
        try:
            token = parts[1].split(".", 1)[0]
            padding = "=" * (-len(token) % 4)
            data = json.loads(base64.urlsafe_b64decode(token + padding))
        except (ValueError, TypeError, IndexError, KeyError):
            continue
        candidates = data.get("c") or []
        if not candidates or candidates[0].get("t") != "d":
            continue
        found.append(s)
    found.sort(key=lambda s: s.get("behaviorHints", {}).get("videoSize", 0), reverse=True)
    return found


async def _resolve_playback_url(client: httpx.AsyncClient, url: str) -> tuple[Optional[str], bool]:
    """(lien, quota_atteint) — quota_atteint=True doit interrompre toute
    tentative suivante, y compris sur d'autres candidats du même film."""
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        log.warning("résolution lien direct Lumio échouée : %s", exc)
        return None, False
    if resp.status_code == 429:
        _start_pause()
        return None, True
    if resp.status_code not in (301, 302, 303, 307, 308):
        return None, False
    return resp.headers.get("location"), False


async def get_direct_link(imdb_id: str) -> Optional[dict]:
    """Repli pour le téléchargement direct (bouton VODIO) quand Wacustom n'a
    plus aucun candidat exploitable pour un film : suit l'URL de lecture
    Lumio du meilleur flux déjà pré-résolu et récupère le lien final via la
    redirection qu'elle renvoie — {"url", "filename"} ou None. Plusieurs
    candidats tentés en cascade (cf. `_MAX_DIRECT_ATTEMPTS`), même quota
    partagé que `is_cached`."""
    streams = await _fetch_streams(f"stream/movie/{imdb_id}.json")
    if not streams:
        return None
    candidates = _direct_stream_candidates(streams)[:_MAX_DIRECT_ATTEMPTS]
    if not candidates:
        return None
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        for stream in candidates:
            link, quota_hit = await _resolve_playback_url(client, stream["url"])
            if quota_hit:
                return None
            if not link:
                continue
            hints = stream.get("behaviorHints", {})
            filename = hints.get("filename") or f"{imdb_id}.mkv"
            size = hints.get("videoSize")
            return {
                "url": link, "filename": filename,
                "size_gb": round(size / 1_000_000_000, 2) if size else None,
            }
    return None


# Nombre de candidats "lien direct" listés pour l'affichage — pas de
# résolution en direct ici (aucun appel Lumio), juste ce que le listing
# initial contient déjà. Borné pour ne pas noyer l'UI ni suggérer d'en
# résoudre trop d'un coup (résoudre reste 1 appel Lumio par clic).
_MAX_LISTED_CANDIDATES = 5


async def list_candidates(imdb_id: str) -> list[dict]:
    """Repli pour la liste « Sources » (2026-08-15) : candidats Lumio "lien
    direct" affichés sans être résolus (0 appel Lumio supplémentaire — les
    infos viennent du listing déjà récupéré). L'utilisateur choisit ensuite
    lequel résoudre via `resolve_direct_link`, au lieu que VODIO en résolve
    un automatiquement pour lui (`get_direct_link`). Triés du plus léger au
    plus lourd sur l'ensemble des candidats (pas seulement les plus gros que
    `get_direct_link` tente en premier pour la qualité) — préférence
    explicite de l'utilisateur pour un choix manuel."""
    streams = await _fetch_streams(f"stream/movie/{imdb_id}.json")
    if not streams:
        return []
    ascending = sorted(
        _direct_stream_candidates(streams),
        key=lambda s: s.get("behaviorHints", {}).get("videoSize", 0),
    )
    out = []
    for s in ascending[:_MAX_LISTED_CANDIDATES]:
        hints = s.get("behaviorHints", {})
        size = hints.get("videoSize")
        out.append({
            "filename": hints.get("filename") or "",
            "size_gb": round(size / 1_000_000_000, 2) if size else None,
            "playback_url": s["url"],
        })
    return out


async def resolve_direct_link(playback_url: str) -> Optional[dict]:
    """Résolution à la demande (1 appel Lumio) d'une entrée listée par
    `list_candidates` — appelée seulement quand l'utilisateur clique dessus,
    pas systématiquement sur toute la liste. `playback_url` doit venir de
    Lumio (vérifié) : c'est une valeur renvoyée au client puis reçue en
    retour, à ne jamais faire suivre sans validation (SSRF)."""
    if not playback_url.startswith(_BASE_URL + "/"):
        return None
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        link, quota_hit = await _resolve_playback_url(client, playback_url)
    if quota_hit or not link:
        return None
    return {"url": link}
