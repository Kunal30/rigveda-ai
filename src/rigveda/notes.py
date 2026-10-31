"""What Rigved has learned about you from talking with you: your preferences, your plans, the people and things in
your life, and what you meant when it got you wrong ("the Rigved app" is the orb). Claude notes them as it talks
(REMEMBER: …, FORGET: … at the end of a reply, never said aloud); they're kept here, on this computer, and go back to
both minds, so you needn't explain things twice.

Only what was said in conversation Claude already hears becomes a note: never what was answered from your files.
"""

from __future__ import annotations

import json
import time
from difflib import SequenceMatcher

from .config import _xdg

PATH = _xdg("XDG_DATA_HOME", ".local/share") / "rigveda" / "notes.json"
MOST = 80  # notes kept; the oldest go first
SHOWN = 30  # notes given to a model with each turn, the newest
SAME = 0.8  # how alike two notes must be to be the same one, the newer replacing the older


def _read() -> list[dict]:
    try:
        notes = json.loads(PATH.read_text())
    except (OSError, ValueError):
        return []
    return [note for note in notes if isinstance(note, dict) and note.get("note")]


def _write(notes: list[dict]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(notes[-MOST:], indent=1))


def _alike(one: str, other: str) -> float:
    return SequenceMatcher(None, one.lower(), other.lower()).ratio()


def add(note: str) -> None:
    """Keep a note, replacing one that says nearly the same."""
    note = " ".join(note.split()).strip(" .") + "."
    if len(note) < 6:
        return
    notes = [kept for kept in _read() if _alike(kept["note"], note) < SAME]
    _write(notes + [{"note": note[:300], "at": time.time()}])


def forget(note: str) -> bool:
    """Forget the note nearest what's described; whether there was one."""
    notes = _read()
    if not notes:
        return False
    nearest = max(notes, key=lambda kept: _alike(kept["note"], note))
    if _alike(nearest["note"], note) < 0.5 and not any(word in nearest["note"].lower() for word in note.lower().split() if len(word) > 4):
        return False
    _write([kept for kept in notes if kept is not nearest])
    return True


def described() -> str:
    """The notes, newest last, a line each; "" if there are none."""
    return "\n".join(f"- {note['note']}" for note in _read()[-SHOWN:])
