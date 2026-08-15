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
                migrated = False
                for i in self.items:
                    old = i.pop("precache_season", None)
                    if old and old.get("season") is not None:
                        # Migration (2026-07-20) : une seule saison suivie à la
                        # fois → une saison par clé, plusieurs en parallèle.
                        seasons = i.setdefault("precache_seasons", {})
                        seasons.setdefault(str(old["season"]), {
                            "total": old.get("total", len(old.get("episodes", {}))),
                            "episodes": old.get("episodes", {}),
                        })
                        migrated = True
                log.info("watchlist chargée : %d films", len(self.items))
                if migrated:
                    self._save()
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

    def get(self, imdb_id: str) -> dict | None:
        for i in self.items:
            if i["id"] == imdb_id:
                return i
        return None

    def set_precache(self, imdb_id: str, magnet_id: int | None) -> bool:
        """Mémorise l'ID du magnet lancé en pré-cache pour ce titre (permet de
        re-vérifier son avancement plus tard). None efface le suivi.
        `precache_started_at` sert à détecter un magnet mort (0 seeder) sans
        attendre le timeout de 20 min d'AllDebrid — cf. availability. Remis à
        zéro dès que le magnet change réellement (`setdefault` seul laissait
        l'ancien chrono si /precache était rappelé sur un nouveau candidat
        sans être passé par le nettoyage magnet_id=None entre les deux —
        relevé par une revue GLM, 2026-08-15)."""
        for i in self.items:
            if i["id"] == imdb_id:
                if magnet_id is None:
                    i.pop("precache_magnet_id", None)
                    i.pop("precache_started_at", None)
                else:
                    if i.get("precache_magnet_id") != magnet_id:
                        i["precache_started_at"] = time.time()
                    else:
                        i.setdefault("precache_started_at", time.time())
                    i["precache_magnet_id"] = magnet_id
                self._save()
                return True
        return False

    def start_precache_season(self, imdb_id: str, season: int, total: int) -> bool:
        """Initialise le suivi de pré-cache d'une saison entière : un épisode
        par entrée, statut "pending" (rempli au fil du traitement en tâche de
        fond). Chaque saison est suivie indépendamment (clé = numéro de
        saison, dans `precache_seasons`) — en relancer une réinitialise
        seulement celle-ci, les autres saisons suivies restent intactes."""
        for i in self.items:
            if i["id"] == imdb_id:
                seasons = i.setdefault("precache_seasons", {})
                seasons[str(season)] = {
                    "total": total,
                    "episodes": {str(e): {"status": "pending"} for e in range(1, total + 1)},
                }
                self._save()
                return True
        return False

    def set_precache_episode(
        self, imdb_id: str, season: int, episode: int, status: str,
        magnet_id: int | None = None, magnet: str | None = None,
    ) -> bool:
        """Met à jour le statut d'un épisode d'une saison en cours de
        pré-cache. Ignoré si cette saison n'est pas (ou plus) suivie.
        `started_at` sert à détecter un magnet mort sans attendre le timeout
        de 20 min d'AllDebrid (cf. availability/main) — remis à zéro quand le
        magnet change (nouvelle tentative), préservé sinon. `tried_magnets`
        mémorise les liens déjà essayés pour cet épisode, pour que le repli
        automatique sur le candidat suivant (cf. main._refresh) ne reboucle
        jamais sur un magnet déjà confirmé mort."""
        for i in self.items:
            if i["id"] == imdb_id:
                ps = i.get("precache_seasons", {}).get(str(season))
                if not ps:
                    return False
                previous = ps["episodes"].get(str(episode), {})
                entry = {"status": status}
                if magnet_id is not None:
                    entry["magnet_id"] = magnet_id
                if status == "downloading":
                    if magnet_id is not None and magnet_id != previous.get("magnet_id"):
                        entry["started_at"] = time.time()
                    else:
                        entry["started_at"] = previous.get("started_at") or time.time()
                tried = list(previous.get("tried_magnets", []))
                if magnet and magnet not in tried:
                    tried.append(magnet)
                if tried:
                    entry["tried_magnets"] = tried
                ps["episodes"][str(episode)] = entry
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
        """Remplace les entrées (mêmes ids) après recalcul des badges. `metas`
        sont des copies (cf. `_strip_badge`) — sans ce report explicite,
        `eta_days` calculé dessus serait perdu au lieu d'être persisté."""
        by_id = {m["id"]: m for m in metas}
        for i in self.items:
            m = by_id.get(i["id"])
            if m:
                i["name"] = m["name"]  # badge rafraîchi
                if "eta_days" in m:
                    i["eta_days"] = m["eta_days"]
                else:
                    i.pop("eta_days", None)
        self._save()
