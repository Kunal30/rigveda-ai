"""What this computer can do, as the computer itself describes it: every desktop setting (with what it's for, in
the words of the program that owns it) and every app with what its launcher offers. Rigved searches it by meaning
for what you said, and gives the local model only the dozen nearest, so it chooses among a few that fit rather than
thousands ("make the screen warmer at night" → Night light).

Settings come from GNOME's schema files, apps from their launchers. Built once and again every week, or with
`rigveda catalog`; it's small (a couple of thousand entries) and nothing in it is personal.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ElementTree
from functools import lru_cache
from pathlib import Path

import numpy as np

from .config import Config, _xdg

SCHEMAS = Path("/usr/share/glib-2.0/schemas")
PATH = _xdg("XDG_DATA_HOME", ".local/share") / "rigveda" / "catalog.json"
VECTORS = PATH.with_suffix(".npy")
STALE = 7 * 86400  # seconds before it's built again
NEAR = 0.45  # how near in meaning an entry must be to what was said to be offered to the model
# Keys that are a program's own bookkeeping, not settings anyone would ask for.
BOOKKEEPING = re.compile(r"(?:^|-)(?:window|state|geometry|position|size|width|height|history|recent|last|cache|"
                         r"version|migrated|sort|column|columns|maximized|pane|sidebar-width|uuid|id|ids|count|"
                         r"timestamp|first-run|seen|welcome|tour|dialog|selected|expanded)(?:-|$)")
# Settings never changed by voice: they would leave Rigved deaf, or shut it out.
NEVER = {("org.gnome.desktop.privacy", "disable-microphone"), ("org.gnome.desktop.lockdown", "disable-command-line"),
         ("org.gnome.desktop.lockdown", "disable-lock-screen"), ("org.gnome.desktop.a11y.applications", "screen-keyboard-enabled")}
SIMPLE = {"b", "s", "i", "u", "d", "q", "n", "x", "t"}  # true/false, text and numbers: what can be said


def installed() -> set[str]:
    try:
        found = subprocess.run(["gsettings", "list-schemas"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(found.split())


def enums(folder: Path | None = None) -> dict[str, list[str]]:
    """The named lists of values settings choose from ("org.gnome.desktop.GDesktopColorScheme" → default,
    prefer-dark, prefer-light)."""
    found: dict[str, list[str]] = {}
    for path in (folder or SCHEMAS).glob("*.xml"):
        try:
            root = ElementTree.parse(path).getroot()
        except (ElementTree.ParseError, OSError):
            continue
        for enum in root.iter("enum"):
            found[enum.get("id", "")] = [value.get("nick", "") for value in enum.iter("value")]
    return found


def settings(folder: Path | None = None, only: set[str] | None = None) -> list[dict]:
    """Every setting one could ask to change: {"kind": "setting", "schema", "key", "type", "choices", "range",
    "default", "summary", "description"}."""
    folder = folder or SCHEMAS
    named = enums(folder)
    battery = any(Path("/sys/class/power_supply").glob("BAT*"))  # a desktop's battery settings do nothing
    found = []
    for path in sorted(folder.glob("*.gschema.xml")):
        try:
            root = ElementTree.parse(path).getroot()
        except (ElementTree.ParseError, OSError):
            continue
        for schema in root.iter("schema"):
            name = schema.get("id", "")
            if not schema.get("path") or (only is not None and name not in only):  # relocatable: no one place to set
                continue
            for key in schema.iter("key"):
                kind = key.get("type") or ("s" if key.get("enum") else "")
                summary = " ".join((key.findtext("summary") or "").split())
                if (name, key.get("name")) in NEVER or not battery and "battery" in key.get("name", "") or not summary or kind not in SIMPLE and not key.get("enum") or BOOKKEEPING.search(key.get("name", "")):
                    continue
                choices = named.get(key.get("enum", ""), []) or [choice.get("value", "") for choice in key.iter("choice")]
                bounds = key.find("range")
                found.append({"kind": "setting", "schema": name, "key": key.get("name", ""), "type": kind,
                              "choices": choices, "range": [bounds.get("min"), bounds.get("max")] if bounds is not None else [],
                              "default": (key.findtext("default") or "").strip(), "summary": summary,
                              "description": " ".join((key.findtext("description") or "").split())[:400]})
    return found


def apps() -> list[dict]:
    """Every app in the app grid, with what it's for and what its launcher offers."""
    from .actions import APP_DIRS

    found, seen = [], set()
    for directory in APP_DIRS:
        for path in sorted(Path(directory).expanduser().glob("*.desktop")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fields = dict(line.split("=", 1) for line in text.split("\n[", 1)[0].splitlines() if "=" in line and not line.startswith("#"))
            if fields.get("Type") != "Application" or fields.get("NoDisplay") == "true" or fields.get("Hidden") == "true":
                continue
            if not fields.get("Name") or fields["Name"] in seen:
                continue
            seen.add(fields["Name"])
            actions = re.findall(r"\n\[Desktop Action [^\]]+\]\n(?:[^\[]*?\n)?Name=([^\n]+)", text)
            found.append({"kind": "app", "name": fields["Name"], "generic": fields.get("GenericName", ""),
                          "comment": fields.get("Comment", ""), "keywords": fields.get("Keywords", "").replace(";", ", ").strip(", "),
                          "actions": actions, "desktop": str(path)})
    return found


@lru_cache(maxsize=1)
def called() -> dict[tuple[str, str], list[str]]:
    """What people call some settings, which their own descriptions don't say ("dark mode" is the color scheme)."""
    from .desk import SETTINGS

    names: dict[tuple[str, str], list[str]] = {}
    for name, (schema, key, _, _) in SETTINGS.items():
        names.setdefault((schema, key), []).append(name)
    return names


def text(entry: dict) -> str:
    """What an entry is, in words, to be searched by meaning."""
    if entry["kind"] == "app":
        return (f"App {entry['name']}: {entry['generic']}. {entry['comment']} {entry['keywords']}. "
                f"{', '.join(entry['actions'])}").strip()
    where = entry["schema"].removeprefix("org.gnome.").replace(".", " ")
    also = called().get((entry["schema"], entry["key"]))
    return (f"Setting {entry['key'].replace('-', ' ')} ({where}): {entry['summary']}. "
            + (f"Also called {', '.join(also)}. " if also else "") + entry["description"])


def build(config: Config, log=print) -> int:
    """Read the settings and apps on this computer, work out what each means, and keep them; how many."""
    from . import llm

    entries = settings(only=installed() or None) + apps()
    vectors = []
    for start in range(0, len(entries), 64):
        vectors += llm.embed(config.ollama, config.embedding_model, [text(entry)[:600] for entry in entries[start:start + 64]])
    matrix = np.asarray(vectors, dtype=np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    PATH.parent.mkdir(parents=True, exist_ok=True)
    np.save(VECTORS, matrix)
    PATH.write_text(json.dumps({"built": time.time(), "model": config.embedding_model, "entries": entries}))
    log(f"Catalog: {sum(entry['kind'] == 'setting' for entry in entries)} settings and "
        f"{sum(entry['kind'] == 'app' for entry in entries)} apps.")
    _loaded.clear()
    return len(entries)


_loaded: dict = {}


def load() -> tuple[list[dict], np.ndarray] | None:
    if "entries" not in _loaded:
        try:
            saved = json.loads(PATH.read_text())
            _loaded.update(entries=saved["entries"], vectors=np.load(VECTORS), built=saved["built"])
        except (OSError, ValueError, KeyError):
            return None
    return _loaded["entries"], _loaded["vectors"]


def stale() -> bool:
    try:
        return time.time() - json.loads(PATH.read_text())["built"] > STALE
    except (OSError, ValueError, KeyError):
        return True


def find(said: str, config: Config, most: int = 10, kind: str | None = None) -> list[tuple[float, dict]]:
    """The entries nearest in meaning to what was said, nearest first, with how near (0 to 1)."""
    from . import llm

    loaded = load()
    if loaded is None:
        return []
    entries, vectors = loaded
    try:
        query = np.asarray(llm.embed(config.ollama, config.embedding_model, [said])[0], dtype=np.float32)
    except Exception:  # the embedding model is down: no catalog this time
        return []
    query /= np.linalg.norm(query) + 1e-9
    scores = vectors @ query + BOOST * np.asarray([_shared(said, entry) for entry in entries])
    order = np.argsort(-scores)
    found = [(float(scores[index]), entries[index]) for index in order[: most * 4]
             if kind is None or entries[index]["kind"] == kind]
    return [(score, entry) for score, entry in found if score >= NEAR][:most]


BOOST = 0.15  # added for the words said that an entry's name has: meaning alone mixes up "dark mode" and night light
COMMON = {"the", "a", "an", "to", "on", "off", "of", "in", "my", "me", "make", "turn", "switch", "set", "show", "open",
          "please", "it", "is", "and", "for", "can", "you", "change", "enable", "disable", "start", "stop"}


def _shared(said: str, entry: dict) -> float:
    """The share of the words said (other than common ones) in the entry's name, choices or what it's also called."""
    wanted = set(re.findall(r"[a-z]+", said.lower())) - COMMON
    if not wanted:
        return 0.0
    if entry["kind"] == "app":
        named = f"{entry['name']} {entry['generic']} {entry['keywords']}"
    else:
        named = f"{entry['key']} {' '.join(entry['choices'])} {' '.join(called().get((entry['schema'], entry['key']), []))}"
    return len(wanted & set(re.findall(r"[a-z]+", named.lower()))) / len(wanted)


def values(entry: dict) -> str:
    """What a setting can be set to, in a few words: "true or false", "one of default, prefer-dark, prefer-light"."""
    if entry["type"] == "b":
        return "true or false"
    if entry["choices"]:
        return "one of " + ", ".join(entry["choices"][:12])
    if entry["range"]:
        return f"a number from {entry['range'][0]} to {entry['range'][1]}"
    if entry["type"] in ("i", "u", "d", "q", "n", "x", "t"):
        return "a number"
    return "text"


# ── changing a setting the catalog knows, and putting it back ──

LAST = Path(__import__("os").environ.get("XDG_RUNTIME_DIR") or "/tmp") / "rigveda-settings-changed.json"
KEPT = 10  # changes remembered for "put it back"
TRUE = re.compile(r"true|on|yes|enable[ds]?|show|1")
FALSE = re.compile(r"false|off|no|disable[ds]?|hide|0")


def entry(schema: str, key: str) -> dict | None:
    loaded = load()
    return next((found for found in (loaded[0] if loaded else []) if found["kind"] == "setting"
                 and found["schema"] == schema and found["key"] == key), None)


def _gsettings(*arguments: str) -> str | None:
    """What gsettings printed, or None if it failed."""
    try:
        done = subprocess.run(["gsettings", *arguments], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def now(schema: str, key: str) -> str | None:
    return _gsettings("get", schema, key)


def value(setting: dict, said: str) -> str | None:
    """What was said as a value the setting takes, written as gsettings wants it ("'prefer-dark'", "true", "2700"),
    or None if it isn't one it can be."""
    said = said.strip().strip("'\"").strip()
    if setting["type"] == "b":
        return "true" if TRUE.fullmatch(said.lower()) else "false" if FALSE.fullmatch(said.lower()) else None
    if setting["choices"]:
        from difflib import get_close_matches

        near = get_close_matches(said.lower().replace(" ", "-"), setting["choices"], n=1, cutoff=0.6)
        return f"'{near[0]}'" if near else None
    if setting["type"] in ("i", "u", "d", "q", "n", "x", "t"):
        try:
            number = float(said.rstrip("%"))
        except ValueError:
            return None
        if setting["range"]:
            number = min(max(number, float(setting["range"][0])), float(setting["range"][1]))
        return repr(number) if setting["type"] == "d" else str(int(round(number)))
    return "'" + said.replace("\\", "\\\\").replace("'", "\\'") + "'"


def put(schema: str, key: str, written: str) -> bool:
    """Set it, remembering what it was so it can be put back."""
    before = now(schema, key)
    if _gsettings("set", schema, key, written) is None:
        return False
    if before is not None and before != written:
        remember([schema, key, before])
    return True


def remember(change: list | dict) -> None:
    """A change to put back on "undo that": [schema, key, value before] for a setting, or {"run": the command that
    undoes it, "said": what was done} (machine.do)."""
    try:
        changed = json.loads(LAST.read_text())
    except (OSError, ValueError):
        changed = []
    LAST.write_text(json.dumps((changed + [change])[-KEPT:]))


def change(target: str) -> tuple[bool, str] | None:
    """A setting the model picked from the catalog, "<schema> <key> <value>": (done, what to say), or None if the
    target isn't one."""
    parts = target.strip().split(None, 2)
    if len(parts) < 3 or not re.fullmatch(r"[\w-]+(?:\.[\w-]+)+", parts[0]):
        return None
    setting = entry(parts[0], parts[1])
    if setting is None or (parts[0], parts[1]) in NEVER:
        return None
    written = value(setting, parts[2])
    if written is None:
        return False, f"{setting['summary'].rstrip('.')} can be {values(setting)}, not {parts[2]}."
    return put(parts[0], parts[1], written), f"{setting['summary'].rstrip('.')}: {written.strip(chr(39))}."


def undo() -> tuple[bool, str]:
    """Put back the last setting changed."""
    try:
        changed = json.loads(LAST.read_text())
    except (OSError, ValueError):
        changed = []
    if not changed:
        return False, "I haven't changed anything lately."
    last = changed.pop()
    LAST.write_text(json.dumps(changed))
    if isinstance(last, dict):
        from .machine import _act

        done, _ = _act(last["run"])
        return done, last.get("undone", "Okay, that's back the way it was.") if done else "Sorry, I couldn't undo that."
    schema, key, before = last
    done = _gsettings("set", schema, key, before) is not None
    found = entry(schema, key)
    return done, f"{(found['summary'] if found else key).rstrip('.')} is back to {before.strip(chr(39))}."


def offered(said: str, config: Config) -> str:
    """The settings and apps nearest what was said, for the model's prompt; empty if there's no catalog."""
    found = find(said, config, most=12)
    settings_near = [entry for _, entry in found if entry["kind"] == "setting"][:8]
    apps_near = [entry for _, entry in found if entry["kind"] == "app"][:4]
    lines = []
    for setting in settings_near:
        current = now(setting["schema"], setting["key"])
        lines.append(f"{setting['schema']} {setting['key']}: {setting['summary'].rstrip('.')} "
                     f"({values(setting)}{f'; now {current}' if current is not None else ''})")
    for app in apps_near:
        lines.append(f"app {app['name']}: {app['generic'] or app['comment']}"
                     + (f" (offers {', '.join(app['actions'][:6])})" if app["actions"] else ""))
    return "\n".join(lines)
