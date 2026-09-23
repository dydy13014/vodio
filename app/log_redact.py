"""Masque les secrets dans tous les journaux (VODIO, httpx, uvicorn).

httpx journalisait chaque requete avec son URL complete (cle TMDB, apikey des
trackers, identifiants SMS Free Mobile), et les messages d'erreur httpx
reprennent eux aussi l'URL complete. Un filtre pose sur les handlers couvre
tous les loggers qui y remontent, y compris les tracebacks.
"""
import logging
import os
import re

_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:api_?key|apikey|api_token|token|passkey|pass(?:word)?|user)=)[^&\s'\"<>]+"
)

_SECRET_ENV = (
    "TMDB_API_KEY", "RPDB_API_KEY", "STREAM_CHECK_CONFIG", "WACUSTOM_CONFIG",
    "ALLDEBRID_API_KEY", "VODIO_PASSWORD", "VODIO_SMS_USER", "VODIO_SMS_PASS",
    "LUMIO_MANIFEST_ID", "MEDIAFLOW_API_PASSWORD", "C411_API_KEY",
    "TR4KER_API_KEY", "YGGREBORN_API_KEY", "V3X_API_KEY", "TORR9_API_KEY",
)
_MIN_SECRET_LEN = 6


def _secret_values() -> list:
    values = [os.environ.get(name, "") for name in _SECRET_ENV]
    # VODIO_EXTRA_USERS = "nom:motdepasse,nom2:motdepasse2"
    for entry in os.environ.get("VODIO_EXTRA_USERS", "").split(","):
        if ":" in entry:
            values.append(entry.split(":", 1)[1].strip())
    # Les plus longs d'abord : un secret peut en contenir un autre.
    return sorted({v for v in values if len(v) >= _MIN_SECRET_LEN}, key=len, reverse=True)


def redact(text: str) -> str:
    if not text:
        return text
    for value in _secret_values():
        text = text.replace(value, "***")
    return _QUERY_SECRET.sub(r"\1***", text)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = None
            if record.exc_info and not record.exc_text:
                record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        except Exception:
            pass
        return True


def install() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    flt = RedactFilter()
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            if not any(isinstance(f, RedactFilter) for f in handler.filters):
                handler.addFilter(flt)
