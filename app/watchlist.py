"""Watchlist personnelle VODIO — persistée en JSON dans le volume de données.

Chaque entrée est un meta Stremio (id IMDb, name, poster, releaseInfo,
description) enrichi de `added_at` (tri : plus récents en tête). Le badge de
disponibilité ✅/⏳ est appliqué dans le champ `name` au même titre que le
catalogue des nouveautés (recalculé à l'ajout + au refresh quotidien).
"""
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("vodio.watchlist")


class Watchlist:
    def __init__(self, path: str):
        self.path = Path(path)
        self.items: list[dict] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.items = json.loads(self.path.read_text())
                log.info("watchlist chargée : %d films", len(self.items))
            except (json.JSONDecodeError, ValueError) as exc:
                log.warning("watchlist illisible, ignorée : %s", exc)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items, ensure_ascii=False))

    def add(self, meta: dict) -> dict | None:
        """Ajoute un titre. Retourne l'entrée stockée, ou None si déjà présent."""
        base_id = meta["id"]
        if any(i["id"] == base_id for i in self.items):
            return None
        entry = dict(meta)
        entry["added_at"] = time.time()
        self.items.insert(0, entry)
        self._save()
        return entry

    def persist(self) -> None:
        """Sauvegarde publique (après mise à jour d'un badge en arrière-plan)."""
        self._save()

    def remove(self, imdb_id: str) -> bool:
        before = len(self.items)
        self.items = [i for i in self.items if i["id"] != imdb_id]
        if len(self.items) != before:
            self._save()
            return True
        return False

    def set_watched(self, imdb_id: str, watched: bool) -> bool:
        """Marque un titre comme vu / non vu (retiré des catalogues Stremio)."""
        for i in self.items:
            if i["id"] == imdb_id:
                i["watched"] = watched
                self._save()
                return True
        return False

    def metas(self, media_type: str | None = None, include_watched: bool = False) -> list[dict]:
        """Copie triée (plus récents d'abord), filtrable par type ; exclut les vus."""
        items = self.items
        if not include_watched:
            items = [i for i in items if not i.get("watched")]
        if media_type:
            items = [i for i in items if i.get("type", "movie") == media_type]
        return sorted(items, key=lambda i: i.get("added_at", 0), reverse=True)

    def all_items(self) -> list[dict]:
        """Toutes les entrées (vus inclus), pour l'UI de gestion."""
        return sorted(self.items, key=lambda i: i.get("added_at", 0), reverse=True)

    def replace_all(self, metas: list[dict]) -> None:
        """Remplace les entrées (mêmes ids) après recalcul des badges."""
        by_id = {m["id"]: m for m in metas}
        for i in self.items:
            m = by_id.get(i["id"])
            if m:
                i["name"] = m["name"]  # badge rafraîchi
        self._save()
