"""Track newly created match tabs; never infer ownership from a tab name."""

from datetime import date
import json
import logging
from threading import Lock

from match_discovery import local_start
import scheduled_runs


_state_lock = Lock()


def read_state():
    path = scheduled_runs.CACHE_DIR / "sheets-cleanup.json"
    if not path.exists():
        return {"version": 1, "books": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(state, dict) or state.get("version") != 1
            or not isinstance(state.get("books"), dict)):
        raise ValueError(f"Etat de nettoyage Sheets invalide : {path}")
    if state.get("last_cleanup") is not None:
        date.fromisoformat(state["last_cleanup"])
    for book_id, book in state["books"].items():
        if (not isinstance(book_id, str) or not isinstance(book, dict)
                or not isinstance(book.get("sheets"), dict)):
            raise ValueError(f"Classeur invalide dans {path}")
        for sheet_id, entry in book["sheets"].items():
            if (not sheet_id.isdecimal() or not isinstance(entry, dict)
                    or not isinstance(entry.get("title"), str)
                    or entry["title"] == "Exécutions"):
                raise ValueError(f"Onglet invalide dans {path}")
            local_start(entry["start"])
    return state


def write_state(state):
    path = scheduled_runs.CACHE_DIR / "sheets-cleanup.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def track_match_sheet(book_id, sheet_id, title, start, created):
    kickoff = local_start(start)
    with _state_lock:
        state = read_state()
        book = state["books"].setdefault(book_id, {"sheets": {}})
        key = str(sheet_id)
        if not created and key not in book["sheets"]:
            return
        previous = book["sheets"].get(key)
        if title == "Exécutions":
            raise ValueError("L'onglet Executions ne peut pas etre un onglet de match")
        if previous:
            kickoff = max(kickoff, local_start(previous["start"]))
        book["sheets"][key] = {"title": title, "start": kickoff.isoformat()}
        write_state(state)


def cleanup_match_sheets(open_book, today):
    with _state_lock:
        state = read_state()
        # Avoid even opening Google on subsequent passages the same day.
        if state.get("last_cleanup") == today.isoformat():
            return
        book = open_book()
        tracked = state["books"].setdefault(book.id, {"sheets": {}})
        existing = {str(sheet.id): sheet for sheet in book.worksheets()}
        for sheet_id, entry in list(tracked["sheets"].items()):
            if local_start(entry["start"]).date() >= today:
                continue
            sheet = existing.get(sheet_id)
            if sheet is not None:
                if sheet.title != entry["title"] or sheet.title == "Exécutions":
                    logging.warning("[NETTOYAGE] Onglet renomme conserve : %s", sheet.title)
                    continue
                logging.info("[NETTOYAGE] Suppression du match du %s : %s",
                             local_start(entry["start"]).date(), sheet.title)
                book.del_worksheet(sheet)
            del tracked["sheets"][sheet_id]
            write_state(state)
        state["last_cleanup"] = today.isoformat()
        write_state(state)
