"""Requête directe à Wacustom, en contournant AIOStreams.

Constaté le 2026-07-19 : AIOStreams ne relaie parfois qu'une fraction des
flux réels de Wacustom (7-8 sur 20, Wawacity notamment disparaissant tout le
temps) — mécanisme interne non identifié, indépendant de Wacustom (vérifié en
l'interrogeant directement : réponse complète et fiable à chaque fois).
Utilisé uniquement pour la watchlist personnelle (petite liste, vérifiée
activement) — le catalogue AlloCiné quotidien (~40 films) continue de passer
par AIOStreams pour ne pas multiplier les requêtes à Wacustom.

Le champ "url" d'un flux Wacustom est un token de lecture interne, encodé
(pas chiffré — simple base64 JSON, cf. `encode_playback_token` dans le code
de Wacustom) contenant le lien réel ("l") et l'hébergeur ("h"). Utile pour
extraire le magnet d'un torrent non-caché sans dépendre du format d'affichage.
"""
import base64
import json
import logging

import httpx

log = logging.getLogger("vodio.wacustom")


async def get_streams(
    base: str, config: str, imdb_id: str, content_type: str = "movie",
    season: int = 1, episode: int = 1,
) -> list[dict]:
    if content_type == "series":
        path = f"stream/series/{imdb_id}:{season}:{episode}.json"
    else:
        path = f"stream/movie/{imdb_id}.json"
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.get(f"{base}/{config}/{path}")
            resp.raise_for_status()
            return resp.json().get("streams", [])
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("requête directe Wacustom échouée (%s) : %s", imdb_id, exc)
        return []


def extract_link(stream_url: str) -> str | None:
    """Décode le token de lecture Wacustom pour extraire le lien réel
    (magnet pour un torrent, URL d'hébergeur pour un DDL). None si le format
    ne correspond pas à ce qu'on attend (changement côté Wacustom).

    Depuis le rebase Wacustom 3.8.2 (2026-08-16), le token a un suffixe
    ".<signature>" après le JSON base64 (`<b64json>.<hash>`, en plus d'un
    éventuel "/<nom de fichier>" après le token) — jamais retiré ici avant,
    ce qui faisait échouer le décodage base64 ("Incorrect padding",
    silencieusement avalé par le except) sur 100% des flux Wacustom. Voir
    aussi le bug jumeau sur le
    format de config."""
    try:
        token = stream_url.rsplit("/playback/", 1)[1].split("/", 1)[0].split(".", 1)[0]
        padding = "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(token + padding))
        return data.get("l")
    except (IndexError, ValueError, KeyError, TypeError):
        return None
