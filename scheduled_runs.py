"""Persistent scheduled-run state (exports, lineup sightings) and a cross-process lock."""

from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import json
import logging
from pathlib import Path


CACHE_DIR = Path(__file__).resolve().parent / ".match-cache"


@contextmanager
def run_lock():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with (CACHE_DIR / "run.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.info("[PLANIFICATION] Une execution est deja en cours ; lancement ignore.")
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class ExportedMatches:
    """Identifiants PMU exportes ; un fichier par type d'export (officiel, probable)."""

    def __init__(self, filename="exported.json"):
        self.path = CACHE_DIR / filename
        self.exported = set()
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(payload, dict) or payload.get("version") != 1
                    or not isinstance(payload.get("exported"), list)
                    or any(type(value) is not int or value <= 0 for value in payload["exported"])):
                raise ValueError(f"Etat des exports invalide : {self.path}")
            self.exported = set(payload["exported"])

    def contains(self, event_pmu):
        return event_pmu in self.exported

    def mark_exported(self, event_pmu):
        updated = self.exported | {event_pmu}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "exported": sorted(updated)}),
                             encoding="utf-8")
        temporary.replace(self.path)
        self.exported = updated


class SeenLineups:
    """Premier passage ou les compositions officielles d'un match ont ete vues."""

    RETENTION = timedelta(days=2)

    def __init__(self):
        self.path = CACHE_DIR / "lineups-seen.json"
        self.seen = {}
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(payload, dict) or payload.get("version") != 1
                    or not isinstance(payload.get("seen"), dict)):
                raise ValueError(f"Etat des compositions invalide : {self.path}")
            try:
                self.seen = {int(event): datetime.fromisoformat(moment)
                             for event, moment in payload["seen"].items()}
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Etat des compositions invalide : {self.path}") from exc

    def contains(self, event_pmu):
        return event_pmu in self.seen

    def mark_seen(self, event_pmu, moment):
        updated = {event: seen for event, seen in self.seen.items()
                   if seen >= moment - self.RETENTION}
        updated.setdefault(event_pmu, moment)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "seen": {
            str(event): seen.isoformat() for event, seen in sorted(updated.items())
        }}), encoding="utf-8")
        temporary.replace(self.path)
        self.seen = updated
