"""Persistent successful exports and a cross-process lock for local WSL runs."""

from contextlib import contextmanager
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
    def __init__(self):
        self.path = CACHE_DIR / "exported.json"
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
