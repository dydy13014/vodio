"""Notification SMS via l'API Free Mobile (même passerelle que send-sms.sh).

Utilisé pour prévenir quand un titre de la watchlist passe ⏳ → ✅ (dispo).
Désactivé silencieusement si les identifiants ne sont pas configurés.
"""
import logging
import os

import httpx

log = logging.getLogger("vodio.notify")

SMS_USER = os.environ.get("VODIO_SMS_USER", "")
SMS_PASS = os.environ.get("VODIO_SMS_PASS", "")
API = "https://smsapi.free-mobile.fr/sendmsg"


async def send_sms(message: str) -> None:
    if not (SMS_USER and SMS_PASS):
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(API, params={"user": SMS_USER, "pass": SMS_PASS, "msg": message})
            if r.status_code != 200:
                log.warning("SMS refusé (HTTP %s)", r.status_code)
    except httpx.HTTPError as exc:
        log.warning("SMS en échec : %s", exc)
