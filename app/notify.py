"""Notification SMS via l'API Free Mobile (même passerelle que send-sms.sh).

Utilisé pour prévenir quand un titre de la watchlist passe ⏳ → ✅ (dispo).
Désactivé silencieusement si les identifiants ne sont pas configurés.
"""
import json
import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger("vodio.notify")

SMS_USER = os.environ.get("VODIO_SMS_USER", "")
SMS_PASS = os.environ.get("VODIO_SMS_PASS", "")
API = "https://smsapi.free-mobile.fr/sendmsg"

# Historique persistant (survit aux redémarrages/rebuilds, contrairement aux
# logs docker soumis à la rotation globale) — un SMS par ligne JSON, plafonné
# pour ne pas grossir indéfiniment (2026-09-17, demande utilisateur après un
# doublon d'alerte watchlist).
SMS_LOG_FILE = Path(os.environ.get("VODIO_SMS_LOG_FILE", "/app/data/sms_log.jsonl"))
SMS_LOG_MAX_LINES = 2000


def _log_sms(message: str, status: str, http_status: int | None = None) -> None:
    try:
        SMS_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps(
            {"at": time.time(), "message": message, "status": status, "http_status": http_status},
            ensure_ascii=False,
        )
        with SMS_LOG_FILE.open("a") as f:
            f.write(entry + "\n")
        lines = SMS_LOG_FILE.read_text().splitlines()
        if len(lines) > SMS_LOG_MAX_LINES:
            SMS_LOG_FILE.write_text("\n".join(lines[-SMS_LOG_MAX_LINES:]) + "\n")
    except OSError as exc:
        log.warning("journal SMS illisible/inscriptible : %s", exc)


async def send_sms(message: str) -> None:
    if not (SMS_USER and SMS_PASS):
        _log_sms(message, status="disabled")
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(API, params={"user": SMS_USER, "pass": SMS_PASS, "msg": message})
            if r.status_code != 200:
                log.warning("SMS refusé (HTTP %s)", r.status_code)
                _log_sms(message, status="rejected", http_status=r.status_code)
            else:
                _log_sms(message, status="sent", http_status=r.status_code)
    except httpx.HTTPError as exc:
        log.warning("SMS en échec : %s", exc)
        _log_sms(message, status="error")
