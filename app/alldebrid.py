"""Client AllDebrid minimal — vérifie/déclenche la mise en cache d'un magnet
trouvé pour un film de la watchlist. Réservé à la watchlist (petite liste,
vérifiée une fois par jour au refresh) : le catalogue AlloCiné quotidien
(~40 films) n'appelle jamais AllDebrid directement, pour ne pas ajouter au
compte des dizaines de magnets pour des films que personne ne
regardera peut-être jamais. Même endpoint que Wacustom/Ludio (magnet/upload),
agent distinct pour les stats AllDebrid."""
import logging

import httpx

log = logging.getLogger("vodio.alldebrid")

API_URL = "https://api.alldebrid.com/v4"
VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".webm")
AGENT = "vodio"


async def is_cached(api_key: str, magnet: str) -> bool:
    """Ajoute le magnet au compte (idempotent) et renvoie son statut "ready"
    immédiat. Ne fait AUCUNE attente/polling — si pas prêt, le téléchargement
    démarre en tâche de fond côté AllDebrid, sera peut-être prêt au refresh
    suivant (24h)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{API_URL}/magnet/upload",
                params={"agent": AGENT, "apikey": api_key},
                data={"magnets[]": magnet},
            )
        data = resp.json()
        if data.get("status") != "success":
            return False
        magnets = data.get("data", {}).get("magnets", [])
        if not magnets or magnets[0].get("error"):
            return False
        return bool(magnets[0].get("ready"))
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("check AllDebrid échoué : %s", exc)
        return False


class AllDebridError(Exception):
    pass


async def start_download(api_key: str, magnet: str) -> dict:
    """Pré-cache à la demande : ajoute le magnet au compte (idempotent) et
    DÉCLENCHE son téléchargement s'il n'est pas déjà en cache. Renvoie
    {"id", "ready"}. Une fois téléchargé (get_status → ready), le même torrent
    devient lisible dans Stremio via Wacustom (qui retrouve le même infohash
    déjà en cache)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{API_URL}/magnet/upload",
                params={"agent": AGENT, "apikey": api_key},
                data={"magnets[]": magnet},
            )
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AllDebridError(f"échec de contact AllDebrid : {exc}")
    if data.get("status") != "success":
        raise AllDebridError(data.get("error", {}).get("message", "upload refusé"))
    magnets = data.get("data", {}).get("magnets", [])
    if not magnets:
        raise AllDebridError("réponse AllDebrid vide")
    m = magnets[0]
    if m.get("error"):
        raise AllDebridError(m["error"].get("message", "magnet invalide"))
    return {"id": m["id"], "ready": bool(m.get("ready"))}


# statusCode 4 = prêt ; >= 5 = échec définitif (timeout, fichier trop gros,
# torrent mort…) — cf. Ludio, même sémantique AllDebrid.
_FAILED_THRESHOLD = 5


_IDLE = {"ready": False, "failed": False, "downloaded_pct": 0, "seeders": 0}


async def get_status(api_key: str, magnet_id: int) -> dict:
    """État d'un téléchargement lancé : {"ready", "failed", "downloaded_pct",
    "seeders"}. `seeders` sert à repérer un magnet mort (0 pair, jamais
    déclaré "failed" par AllDebrid avant son timeout interne de 20 min) sans
    attendre ce délai — cf. appelant (détection de blocage)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.alldebrid.com/v4.1/magnet/status",
                params={"agent": AGENT, "apikey": api_key, "id": magnet_id},
            )
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("status AllDebrid échoué : %s", exc)
        return dict(_IDLE)
    if data.get("status") != "success":
        return dict(_IDLE)
    magnets = data.get("data", {}).get("magnets")
    m = magnets[0] if isinstance(magnets, list) else magnets
    if not m:
        return dict(_IDLE)
    code = m.get("statusCode", 0)
    size = m.get("size") or 0
    downloaded = m.get("downloaded") or 0
    pct = round(downloaded / size * 100) if size else 0
    return {
        "ready": code == 4,
        "failed": code >= _FAILED_THRESHOLD,
        "downloaded_pct": pct,
        "seeders": m.get("seeders") or 0,
    }


def _flatten_magnet_files(entries: list, prefix: str = "") -> list[dict]:
    """Aplatit l'arborescence renvoyée par /magnet/files (dossiers imbriqués
    via "e") — même format que Wacustom."""
    flat = []
    for entry in entries:
        name = entry.get("n", "")
        if "e" in entry:
            flat.extend(_flatten_magnet_files(entry["e"], prefix=f"{prefix}{name}/"))
        elif entry.get("l"):
            flat.append({"filename": f"{prefix}{name}", "size": entry.get("s", 0), "link": entry["l"]})
    return flat


def _select_video_file(files: list[dict]) -> dict | None:
    """Le plus gros fichier vidéo (exclut samples et extras)."""
    candidates = [
        f for f in files
        if f["filename"].lower().endswith(VIDEO_EXTENSIONS) and "sample" not in f["filename"].lower()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f["size"])


async def get_direct_link(api_key: str, magnet_id: int) -> dict | None:
    """Fichier vidéo principal d'un magnet prêt : {"filename", "link"} (lien
    AllDebrid débloqué, prêt à être relayé via MediaFlow). None si le magnet
    n'est pas prêt ou ne contient aucun fichier vidéo exploitable."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            files_resp = await client.get(
                f"{API_URL}/magnet/files",
                params=[("agent", AGENT), ("apikey", api_key), ("id[]", str(magnet_id))],
            )
        files_data = files_resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("magnet/files AllDebrid échoué : %s", exc)
        return None
    if files_data.get("status") != "success":
        return None
    magnets = files_data.get("data", {}).get("magnets", [])
    if not magnets:
        return None
    all_files = _flatten_magnet_files(magnets[0].get("files", []))
    selected = _select_video_file(all_files)
    if not selected:
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            unlock_resp = await client.get(
                f"{API_URL}/link/unlock",
                params={"agent": AGENT, "apikey": api_key, "link": selected["link"]},
            )
        unlock_data = unlock_resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("link/unlock AllDebrid échoué : %s", exc)
        return None
    if unlock_data.get("status") != "success":
        return None
    direct_link = unlock_data.get("data", {}).get("link")
    if not direct_link:
        return None
    return {"filename": selected["filename"], "link": direct_link}
