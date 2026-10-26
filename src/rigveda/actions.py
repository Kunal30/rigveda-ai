"""What Rigveda can do when you speak to it, and the sage's replies.

A command such as "open Chrome and open Fidelity" becomes steps (open "chrome", open "fidelity"). Simple rules handle
the common phrasings at once; anything else is handed to the local model, which may only choose from these actions.
It never runs a command of its own, so a misheard sentence can at worst open the wrong thing. The last few exchanges
(conversation.py) go along, so "open it" or "and the deposit?" mean what they meant a moment ago.
"""

from __future__ import annotations

import datetime
import difflib
import json
import random
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import conversation
from .config import Config

WAKE_WORDS = ("rigved", "rigveda")
SHORT_NAMES = {"ved", "veda"}  # "Hey Ved"; said alone it's heard as "wait" or "read", so it needs the "hey"
FILLERS = {"hey", "hi", "hello", "ok", "okay", "oh", "o", "ah", "uh", "um", "hmm", "namaste", "so", "yo"}
VERBS = (r"(?:please\s+)?(?:open|launch|start|run|show(?:\s+me)?|go\s+to|bring\s+up|pause|play|resume|find|search|"
         r"google(?!\s+(?:chrome|drive|maps|docs|calendar|photos|music)))")
KINDS = ("open", "find", "search", "ask", "talk", "time", "date", "volume", "media", "forget", "stop")
# What "open it" can say: the last thing opened. Other targets with these words in them are left to the model.
REFERENCES = {"it", "that", "this", "that file", "the file", "that one", "this one", "it again", "that again", "the same"}
POINTING = {"it", "that", "this", "one", "ones", "them", "those", "these", "same", "there", "previous", "last", "other"}

# Websites that can be named instead of spelled out.
SITES = {
    "fidelity": "https://www.fidelity.com", "google": "https://www.google.com", "gmail": "https://mail.google.com",
    "google drive": "https://drive.google.com", "drive": "https://drive.google.com", "youtube": "https://www.youtube.com",
    "google calendar": "https://calendar.google.com", "calendar": "https://calendar.google.com",
    "google maps": "https://maps.google.com", "maps": "https://maps.google.com", "google photos": "https://photos.google.com",
    "github": "https://github.com", "linkedin": "https://www.linkedin.com", "amazon": "https://www.amazon.com",
    "netflix": "https://www.netflix.com", "onedrive": "https://onedrive.live.com", "outlook": "https://outlook.live.com",
    "whatsapp": "https://web.whatsapp.com", "claude": "https://claude.ai", "chatgpt": "https://chatgpt.com",
    "apple music": "https://music.apple.com", "spotify": "https://open.spotify.com",
    "youtube music": "https://music.youtube.com", "reddit": "https://www.reddit.com", "wikipedia": "https://en.wikipedia.org", "news": "https://news.google.com",
    "rigveda": "http://127.0.0.1:8765", "status": "http://127.0.0.1:8765/status",
}
APP_DIRS = ["/usr/share/applications", "/usr/local/share/applications", "~/.local/share/applications",
            "/var/lib/snapd/desktop/applications", "/var/lib/flatpak/exports/share/applications",
            "~/.local/share/flatpak/exports/share/applications"]
NUMBERS = {"zero": 0, "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
           "eighty": 80, "ninety": 90, "hundred": 100, "a hundred": 100, "one hundred": 100}


STOPWORDS = {"the", "and", "for", "with", "that", "this", "from", "into", "some", "want", "need", "file", "document",
             "please", "open", "show", "listen", "watch", "play", "music", "can", "you", "about"}


@dataclass(frozen=True)
class Step:
    kind: str
    target: str = ""


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+(?:\.[a-z0-9]+)*", text.lower().replace("’", "'"))  # keeps "fidelity.com" whole


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def wake(text: str) -> str | None:
    """What was said to Rigved by name, or None when it wasn't: the name first ("Rigved, open Downloads" → "open
    downloads"; "" for the name alone), or last after a pause, as you'd address a person ("What time is it, Rigved?" →
    "what time is it"). Whisper hears the name in different ways ("Rig Ved", "Rigvid")."""
    tokens = words(text)
    start = 0
    while start < min(len(tokens), 2) and tokens[start] in FILLERS:
        start += 1
    if (size := _name(tokens[start:start + 3])) is not None:
        if start == 0 and tokens[0] in SHORT_NAMES and len(tokens) == 1:
            return None  # "Ved." alone is too often "wait", "weed" or "wed" misheard: it takes "Hey Ved"
        return " ".join(tokens[start + size:])
    ending = re.fullmatch(r"(.*)[,.!?;]\s*(.+?)", text.strip().rstrip(" .!?"), re.S)  # what follows the last pause
    if not ending:
        return None
    before, after = ending.groups()
    last = [token for token in words(after) if token not in FILLERS]
    size = _name(last)
    if size is not None and size == len(last) and words(before):
        return " ".join(words(before))
    return _named_later(text)


def _named_later(text: str) -> str | None:
    """The request after the name when it starts a later sentence, as you'd start over: "Play. Hey Rigved. Play the
    movie." → "play the movie". Only a full stop, question or exclamation mark counts, so "my wife, Rigved is" doesn't."""
    sentences = [part for part in re.split(r"[.!?]+\s+", text.strip()) if words(part)]
    for index in range(len(sentences) - 1, 0, -1):
        tokens = words(sentences[index])
        start = 0
        while start < min(len(tokens), 2) and tokens[start] in FILLERS:
            start += 1
        if (size := _name(tokens[start:start + 3])) is None or (tokens[start] in SHORT_NAMES and start == 0):
            continue  # a bare "Ved" mid-speech is too often "wait" or "wed"
        rest = " ".join([" ".join(tokens[start + size:]), *(" ".join(words(part)) for part in sentences[index + 1:])])
        return rest.strip()
    return None


def _name(tokens: list[str]) -> int | None:
    """How many of the first tokens make up the name ("rigved", "rig ved", "rig ve da"), or None if they don't."""
    if tokens and tokens[0] in SHORT_NAMES:
        return 1
    for size in (1, 2, 3):
        name = "".join(tokens[:size])
        if len(tokens) >= size and name.startswith("r") and max(similar(name, word) for word in WAKE_WORDS) >= 0.8:
            return size
    return None


def meaningful(text: str) -> bool:
    """Whether there's more to it than "hey", "um" or "okay"."""
    return any(word not in FILLERS for word in words(text))


def parse(command: str) -> list[Step] | None:
    """Steps for the common phrasings, or None when the model should work out what was meant."""
    text = " ".join(words(command))
    text = " ".join(word for word in text.split() if wake(word) is None and word != "hey")  # the name said again
    text = re.sub(r"^(open|launch)(?=[a-z]{3})", r"\1 ", text)  # Whisper sometimes runs them together: "openfidelity.com"
    text = re.sub(r"\b(\w+)(?: \1\b)+", r"\1", text)  # "open open", left after the name said twice
    if not text:
        return []
    parts = re.split(rf"\s+(?:and\s+then|then|and|also)\s+(?={VERBS}\b)", text)
    steps = [_rule(part) for part in parts]
    return None if None in steps else steps


PLAYER = r"(?:vlc(?: media player)?|the media player|media player|the player|youtube|spotify)"
WATCHED = rf"(?:(?:the|this|my) )?(?:{PLAYER} )?(?:music|song|video|movie|film|show|episode|playback|play|it)|{PLAYER}"


def _rule(text: str) -> Step | None:
    text = re.sub(r"^(?:please|can you|could you|would you|will you)\s+", "", text).removesuffix(" please")
    text = re.sub(rf"^((?:pause|stop|hold|play|resume|continue|unpause)\b.*?) (?:in|on) (?:the )?{PLAYER}$", r"\1", text)
    if re.fullmatch(r"(?:stop|cancel|never ?mind|nothing|quiet|be quiet|shut up|go to sleep|that's all|thank you|thanks)", text):
        return Step("stop")
    if re.fullmatch(r"(?:forget (?:that|it|this|everything|all that|the conversation|what i said)|start over|"
                    r"start (?:a )?new (?:conversation|topic)|new (?:conversation|topic)|change (?:of )?(?:the )?topic|clean slate)", text):
        return Step("forget")
    if re.fullmatch(r"(?:what(?:'s| is) the time|what time is it|tell me the time|time)", text):
        return Step("time")
    if re.fullmatch(r"(?:what(?:'s| is) (?:the date|today's date)|what day is (?:it|today)|today's date|date)", text):
        return Step("date")
    by = r"(?: by (\d{1,3}|[a-z ]+?)(?: percent| %)?)?"  # "turn it up by 20": Whisper often writes "increased"
    if match := re.fullmatch(rf"(?:turn (?:it|the volume) up|volume up|louder|(?:increased?|raised?) (?:the )?volume){by}", text):
        return Step("volume", _by("+", match.group(1)))
    if match := re.fullmatch(rf"(?:turn (?:it|the volume) down|volume down|quieter|softer|"
                             rf"(?:decreased?|lowered?|reduced?) (?:the )?volume){by}", text):
        return Step("volume", _by("-", match.group(1)))
    if re.fullmatch(r"(?:mute|mute (?:it|the sound|the volume)|silence)", text):
        return Step("volume", "mute")
    if re.fullmatch(r"(?:unmute|unmute (?:it|the sound|the volume))", text):
        return Step("volume", "unmute")
    if match := re.fullmatch(r"(?:set )?(?:the )?volume (?:to )?(\d{1,3}|[a-z ]+?)(?: percent)?", text):
        level = match.group(1)
        if level.isdigit() or level in NUMBERS:
            return Step("volume", str(min(int(level) if level.isdigit() else NUMBERS[level], 100)))
    if re.fullmatch(rf"(?:pause|stop|hold) (?:{WATCHED})|pause", text):  # "pause vlc", "stop the movie in vlc"
        return Step("media", "pause")
    if re.fullmatch(rf"(?:play|resume|continue|unpause)(?: (?:{WATCHED}))?", text):  # "resume the play in vlc"
        return Step("media", "play")
    if re.fullmatch(r"(?:next|skip)(?: (?:song|track|video|one))?|play the next (?:song|track|video|one)", text):
        return Step("media", "next")
    if re.fullmatch(r"(?:previous|go back)(?: (?:song|track|video|one))?|play the previous (?:song|track|video|one)", text):
        return Step("media", "previous")
    if match := re.fullmatch(r"(?:search|google|look up)(?: the web| online| google)?(?: for)? (.+)", text):
        return Step("search", match.group(1))
    if match := re.fullmatch(r"(?:find|where is|where's|locate)(?: me)?(?: my| the| a)? (.+)", text):
        return Step("find", match.group(1))
    if match := re.fullmatch(r"(?:open|launch|start|run|show(?: me)?|go to|bring up)(?: up)?(?: the| my| a)? (.+)", text):
        return Step("open", match.group(1))
    return None


MODEL_FORMAT = {
    "type": "object",
    "properties": {"steps": {"type": "array", "items": {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": [*KINDS, "unknown"]}, "target": {"type": "string"}},
        "required": ["action", "target"]}}},
    "required": ["steps"],
}
MODEL_PROMPT = """You turn a spoken command for a desktop assistant into steps. Actions:
open: open an app, folder, website or file by name (target: the name, e.g. "chrome", "downloads", "fidelity")
find: find a file by what it is about and open it (target: a description, e.g. "2025 tax return")
search: open a web search in the browser, only when the user says to search, google or look something up (target: the query)
ask: a question that can only be answered from the user's own documents or accounts, e.g. their lease, taxes, passport, bills or pay (target: the question)
talk: conversation, opinions, chit-chat, plans, advice or general knowledge such as news, current events, scores, weather, travel, facts, films or how-to, even about the user's own plans (target: what was said). When unsure between ask and talk, choose talk
time, date: say the current time or date (target: "")
volume: target "up", "down", "mute", "unmute" or a number 0-100
media: target "play", "pause", "next" or "previous"
forget: the user wants to start over, forget the conversation or change the topic (target: "")
stop: the user wants nothing (target: "")
unknown: none of these fit (target: "")
Keep the user's order. Reply with JSON only.
"""
CONVERSATION_PROMPT = """The conversation so far, to work out what the command refers to. Make every target complete on its own: a follow-up such as "and the deposit?" becomes the full question, and "it", "that" or "the second one" become what they refer to.
{conversation}

"""


def understand(command: str, config: Config) -> list[Step]:
    """Steps for a command: rules first, the local model for anything they don't cover."""
    steps = parse(command)
    talked = conversation.transcript()
    if steps is not None and not (talked and any(leans(step.target) for step in steps)):
        return steps
    from . import llm

    prompt = MODEL_PROMPT + (CONVERSATION_PROMPT.format(conversation=talked) if talked else "") + "Command: " + command
    try:
        reply = llm.quick(config.ollama, config.model, prompt, timeout=60, format=MODEL_FORMAT)
        steps = [Step(str(step.get("action", "")), str(step.get("target", "")).strip()) for step in json.loads(reply)["steps"]]
    except (llm.OllamaError, ValueError, KeyError, TypeError, AttributeError):
        return [Step("talk", command)]
    if any(step.kind in ("time", "date") for step in steps) and re.search(ELSEWHERE, command.lower()):
        return [Step("talk", command)]  # "what time is it in London" isn't the time here: Claude knows
    if config.claude_model and not re.search(ASKED_TO_SEARCH, command.lower()):  # "the worst news in London today"
        steps = [Step("talk", command) if step.kind == "search" else step for step in steps]  # Claude looks it up
    return [step for step in steps if step.kind in KINDS] or [Step("unknown", command)]


ASKED_TO_SEARCH = r"\b(?:search|google|look (?:it |that )?up|browser)\b"
ELSEWHERE = r"\b(?:in|at|utc|gmt|zone|timezone|until|since|ago|from now)\b"


def quick(text: str) -> bool:
    """A short command that's safe to take without the name, when it's your voice: pause, play, louder, next and such."""
    steps = parse(text)
    return bool(steps) and len(words(text)) <= 5 and all(step.kind in ("media", "volume") for step in steps)


ADDRESSED_PROMPT = """Rigved is a voice assistant on the user's computer that is always listening. It hears \
everything the user says out loud: requests to Rigved, but also the user dictating to other apps and AIs (often about \
Rigved itself, or about their work), talking on calls, talking to people in the room, and thinking aloud. Decide if \
this was said TO Rigved, expecting Rigved to do or answer it.

Said to Rigved: a command for the computer (open, play, pause, volume, find a file), a question Rigved could answer \
for them (weather, facts, explanations, the time, their own files), a request (tell me a joke), a reply to what \
Rigved just said, or anything said directly to "you" (are you listening to me, I'm going for a walk, see you later). \
Dictation to other AIs is already filtered out, so when unsure between a request and thinking aloud, choose true.
Not said to Rigved: talk ABOUT Rigved or "it" (is it listening, it's not responsive, how many clips are done), \
feedback or instructions about building or fixing software, work discussion (incidents, jobs, datasets, models, \
code), greetings or chat with other people, fragments, and thinking aloud.

Examples:
"what's the weather in Seattle" -> true
"open my downloads folder" -> true
"why do cats purr" -> true
"is it listening right now" -> false
"are you even listening to me" -> true
"I'm going for a walk, back soon" -> true
"it still waits fifteen seconds, it's not snappy" -> false
"we need to rerun the dataflow job on all datasets" -> false
"hey, how are you doing" -> false
"okay so" -> false

Conversation with Rigved so far:
{conversation}

They just said: {said}

Reply with JSON only."""
# Tried on things you said on 2026-10-25 (requests, and dictation to Claude): see tests in the session notes.
ADDRESSED_FORMAT = {"type": "object", "properties": {"for_rigved": {"type": "boolean"}}, "required": ["for_rigved"]}


def addressed(said: str, config: Config) -> bool:
    """Whether something you said was said to Rigved, rather than to someone else, another app or yourself."""
    if parse(said) is not None:  # a plain command: "pause", "open downloads"
        return True
    from . import llm

    prompt = ADDRESSED_PROMPT.format(conversation=conversation.transcript() or "(nothing yet)", said=said)
    try:
        reply = llm.quick(config.ollama, config.model, prompt, timeout=20, format=ADDRESSED_FORMAT, options={"temperature": 0})
        return bool(json.loads(reply)["for_rigved"])
    except (llm.OllamaError, ValueError, KeyError, TypeError):
        return True  # can't tell: better to answer than to ignore you


def leans(target: str) -> bool:
    """Whether a target leans on what was said before ("the one from 2023") in a way only the model can follow.
    "it" and "that" alone are the last thing opened, and need no model."""
    return target not in REFERENCES and bool(set(words(target)) & POINTING)


# ── finding what to open ──

def user_folders() -> dict[str, Path]:
    """Your standard folders by name, from ~/.config/user-dirs.dirs, plus home."""
    folders = {"home": Path.home()}
    try:
        lines = (Path.home() / ".config" / "user-dirs.dirs").read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        if match := re.fullmatch(r'XDG_(\w+)_DIR="(.+)"', line.strip()):
            path = Path(match.group(2).replace("$HOME", str(Path.home())))
            folders[path.name.lower()] = path
    for name in ("Desktop", "Documents", "Downloads", "Music", "Pictures", "Videos"):
        folders.setdefault(name.lower(), Path.home() / name)
    folders.update(photos=folders["pictures"], movies=folders["videos"], download=folders["downloads"])
    return {name: path for name, path in folders.items() if path.is_dir()}


def other_folders(config: Config) -> dict[str, Path]:
    """Folders one and two levels into home, and the folders Rigveda watches, by name."""
    found: dict[str, Path] = {}
    tops = [path for path in Path.home().iterdir() if path.is_dir() and not path.name.startswith(".") and path.name != "snap"]
    for top in [Path.home(), *tops]:
        try:
            for path in top.iterdir():
                if path.is_dir() and not path.name.startswith((".", "__")) and path.name != "snap":  # snap: apps' data
                    found.setdefault(path.name.lower().replace("-", " ").replace("_", " "), path)
        except OSError:
            continue
    for folder in config.folders:
        path = Path(folder).expanduser()
        found[path.name.lower()] = path
    return found


def apps() -> dict[str, tuple[str, Path]]:
    """Installed apps you can see in the app grid: their names, written as the app writes them, and .desktop files."""
    found: dict[str, tuple[str, Path]] = {}
    for directory in APP_DIRS:
        for path in sorted(Path(directory).expanduser().glob("*.desktop")):
            try:
                entry = path.read_text(encoding="utf-8", errors="replace").split("\n[", 1)[0]
            except OSError:
                continue
            fields = dict(line.split("=", 1) for line in entry.splitlines() if "=" in line and not line.startswith("#"))
            if fields.get("Type") != "Application" or fields.get("NoDisplay") == "true" or fields.get("Hidden") == "true":
                continue
            for name in (fields.get("Name"), fields.get("GenericName")):
                if name:
                    found.setdefault(name.lower(), (fields["Name"], path))
    return found


def score(target: str, name: str) -> float:
    """How well a spoken name matches: 1 for the same, .9 when it's a whole word of it ("chrome" in "google chrome")."""
    if target == name:
        return 1.0
    if re.search(rf"\b{re.escape(target)}\b", name):
        return 0.9
    return similar(target, name)


def best(target: str, candidates: dict[str, object]) -> tuple[float, str, object]:
    return max(((score(target, name), name, value) for name, value in candidates.items()), default=(0.0, "", None),
               key=lambda found: found[0])


def resolve(target: str, config: Config, files: bool = True) -> tuple[str, str, str]:
    """(kind, display name, what to open) for a spoken name: a folder, an app, a website or, with `files`, a file in
    the index (which takes seconds). kind is "" when nothing matches."""
    target = " ".join(words(target))
    wants_folder = bool(re.search(r"\b(?:folder|directory)$", target))
    target = re.sub(r"\s+(?:in|on|with|using) (?:google chrome|chrome|the browser|my browser|firefox)\b.*$", "", target)
    target = re.sub(r"\s+(?:folder|directory|app|application|website|site|page|account)$", "", target)
    if re.fullmatch(r"[a-z0-9-]+(?:(?:\.|\s+dot\s+)[a-z0-9-]+)+", target):  # "fidelity.com", "fidelity dot com"
        host = re.sub(r"\s*(?:\.|dot)\s*", ".", target)
        return "website", host, "https://" + host
    deeper = best(target, other_folders(config))  # a folder somewhere in home ranks below an app of the same name
    options = [("folder", *best(target, user_folders())), ("app", *best(target, apps())),
               ("website", *best(target, SITES)), ("folder", deeper[0] * 0.88, *deeper[1:])]
    if wants_folder:
        options = [option for option in options if option[0] == "folder"]
    kind, value, name, thing = max(options, key=lambda option: option[1])  # ties go to the earlier option
    if value >= 0.8:
        if kind == "app":
            name, thing = thing
        label = {"folder": Path(thing).name, "website": name.title() if name.islower() else name}.get(kind, name)
        return kind, label, str(thing)
    if not files:
        return "", target, ""
    found = find_file(target, config)
    if found and named(target, found):  # "open" mustn't play a film because its subtitles mentioned music
        return "file", Path(found).name, found
    return "", target, ""


MEDIA = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac"}


def named(asked: str, path: str) -> bool:
    """Whether a file's name has at least half the words that matter in what was asked for."""
    wanted = {word for word in words(asked) if len(word) > 2 and word not in STOPWORDS}
    return bool(wanted) and len(wanted & set(words(re.sub(r"[_\-.]+", " ", Path(path).name)))) * 2 >= len(wanted)


def find_file(description: str, config: Config) -> str:
    """The file in Rigveda's index that best fits a description, if it's on this computer."""
    from .search import search

    try:
        rows = search(config.database_path, description, 8, config)
    except Exception:  # no index yet, or the embedding model is down
        return ""
    for row in rows:
        if not row["path"].startswith("gdrive://") and Path(row["path"]).exists():
            return row["path"]
    return ""


# ── doing it ──

def launch(*command: str) -> None:
    """Start something in its own systemd scope, as GNOME does, so it lives on if Rigveda's listener restarts."""
    if shutil.which("systemd-run"):
        command = ("systemd-run", "--user", "--scope", "--quiet", "--collect", *command)
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def open_thing(kind: str, thing: str) -> None:
    if kind == "app":
        launch("gio", "launch", thing)
    else:
        launch("gio", "open", thing)


def players() -> list[str]:
    """Media players that can be controlled (MPRIS): the one playing first, then a paused one."""
    try:
        names = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path",
                                "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.ListNames"],
                               capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = re.findall(r"'(org\.mpris\.MediaPlayer2\.[^']+)'", names)

    def rank(name: str) -> int:
        status = _status(name)
        return 0 if "Playing" in status else 1 if "Paused" in status else 2

    return sorted(found, key=rank)


def playing() -> list[str]:
    """The media players playing right now."""
    return [name for name in players() if _status(name) == "Playing"]


def _status(name: str) -> str:
    try:
        return subprocess.run(["gdbus", "call", "--session", "--dest", name, "--object-path", "/org/mpris/MediaPlayer2",
                               "--method", "org.freedesktop.DBus.Properties.Get", "org.mpris.MediaPlayer2.Player",
                               "PlaybackStatus"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def press(player: str, method: str) -> None:
    """Press a media player's button: Play, Pause, PlayPause, Next or Previous."""
    try:
        subprocess.run(["gdbus", "call", "--session", "--dest", player, "--object-path", "/org/mpris/MediaPlayer2",
                        "--method", f"org.mpris.MediaPlayer2.Player.{method}"], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


# What the model may call each media action; anything else toggles between playing and paused.
MEDIA_METHODS = {"play": "Play", "resume": "Play", "continue": "Play", "unpause": "Play", "start": "Play",
                 "pause": "Pause", "stop": "Pause", "hold": "Pause", "next": "Next", "skip": "Next",
                 "previous": "Previous", "back": "Previous"}


def media(what: str) -> bool:
    method = MEDIA_METHODS.get(what.split()[0] if what else "", "PlayPause")
    found = players()
    if not found:
        return False
    press(found[0], method)
    return True


def _by(sign: str, amount: str | None) -> str:
    """ "up" or "down" by the usual step, or "+20" / "-20" when you said how much."""
    if amount and (amount.isdigit() or amount in NUMBERS):
        return sign + str(min(int(amount) if amount.isdigit() else NUMBERS[amount], 100))
    return "up" if sign == "+" else "down"


def volume(what: str) -> bool:
    """Turn the apps playing sound (VLC, the browser) up, down, mute or to a level; False if there are none. The
    speakers stay at 100% so Rigved is always heard in full: a level they were turned down to moves into the apps."""
    what = what.strip().rstrip("%").strip()  # "15%", as the model sometimes gives it
    apps = sounding()
    _speakers_full(apps)
    for app in apps:
        command = {"up": ["set-volume", "-l", "1.0", app, "10%+"], "down": ["set-volume", app, "10%-"],
                   "mute": ["set-mute", app, "1"], "unmute": ["set-mute", app, "0"]}.get(what)
        if what[:1] in "+-" and what[1:].isdigit():
            command = ["set-volume", "-l", "1.0", app, f"{what[1:]}%{what[0]}"]
        subprocess.run(["wpctl", *(command or ["set-volume", app, f"{int(what)}%"])], capture_output=True, timeout=5)
    return bool(apps)


def sounding() -> list[str]:
    """The PipeWire ids of the apps' sound streams, playing or paused; never Rigved's own voice (python, through
    PipeWire's ALSA plugin)."""
    try:
        nodes = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=3).stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    found = []
    for node in nodes:
        props = (node.get("info") or {}).get("props") or {}
        app = props.get("application.name") or props.get("node.name") or ""
        if props.get("media.class") == "Stream/Output/Audio" and "python" not in app.lower():
            found.append(str(node["id"]))
    return found


def _level(target: str) -> float | None:
    found = subprocess.run(["wpctl", "get-volume", target], capture_output=True, text=True, timeout=5).stdout
    match = re.search(r"Volume: ([\d.]+)", found)
    return float(match.group(1)) if match else None


def _speakers_full(apps: list[str]) -> None:
    """Put the speakers back to 100%, turning each app down to match so it sounds as loud as it did."""
    sink = "@DEFAULT_AUDIO_SINK@"
    speakers = _level(sink)
    if speakers is None or speakers >= 1.0:
        return
    for app in apps:
        if (level := _level(app)) is not None:
            subprocess.run(["wpctl", "set-volume", app, f"{level * speakers:.3f}"], capture_output=True, timeout=5)
    subprocess.run(["wpctl", "set-volume", sink, "1.0"], capture_output=True, timeout=5)


# ── who's talking ──
# Each persona has its own way of saying the same things; `persona` in the config picks one.

PERSONAS = {
    "witty": {
        "about": "a quick-witted, supremely confident genius with dry humour and playful sarcasm, like a brilliant "
                 "inventor's AI sidekick: warm underneath, never mean",
        "speed": 1.05,
        "looking": ["Let me dig through your files.", "Checking your files, one sec.", "Give me a second to look."],
        "greet": ["Talk to me.", "I'm all ears. Well, all microphone.", "Yeah? What are we building today?",
                  "Go ahead, make it interesting."],
        "done": ["Done.", "Consider it handled.", "Easy."],
        "unsure": ["Didn't catch that. Run it by me again?", "Missed that one. Again, maybe slower?"],
        "cant": ["That one's beyond me for now. Give me a few upgrades.", "Can't do that yet. Soon, though."],
        "bye": ["Fine. I'll be here, being brilliant.", "Going quiet. Call me when you need a genius."],
        "broke": "Something broke. Not my fault, probably.",
        "folder": "{name}, open. Try not to judge the clutter.", "app": "{name}, up and running.",
        "website": "{name}, coming right up.", "file": "Found it: {name}.",
        "missing": "I looked everywhere. No {target}. Are we sure that exists?",
        "no file": "Went through every file you own. No sign of {target}.",
        "search": "Asking the internet about {target}. Brace yourself.",
        "web": ["Looking that up.", "One sec, checking the internet.", "Let me look into that."],
        "no music": "Nothing's playing. I can't pause silence.",
        "no sound": "Nothing's making any sound to turn. I'm always at full volume.",
        "no answer": "Your files have nothing on that. Shocking, I know.",
        "time": "It's {time}.", "date": "It's {date}.",
        "fresh": ["Wiped. Clean slate.", "Forgotten. What's next?"],
    },
    "sage": {
        "about": "a wise and gentle Indian sage who speaks calmly, warmly and simply, sometimes with an image from "
                 "nature or an old teaching, and now and then calls the listener \"my child\" or \"beta\"",
        "speed": 0.88,
        "looking": ["Let me look through your scrolls, my child.", "A moment. I shall search your records."],
        "greet": ["Yes, my child. I am listening.", "Speak, and I shall listen.", "Haan, beta. What do you seek?",
                  "I am here. Tell me what you need."],
        "done": ["Tathaastu.", "So be it.", "It is done."],
        "cant": ["That is not yet within my power, my child."],
        "unsure": ["Forgive me, my child. Those words escape me. Say them once more.",
                   "The meaning eludes me, beta. Speak it again, a little differently."],
        "bye": ["Very well. I shall rest, but never far.", "As you wish. Call, and I will be here."],
        "broke": "Forgive me, my child. Something stood in my way.",
        "folder": "Your {name} folder lies open before you.", "app": "{name} awakens.",
        "website": "The path to {name} is open.", "file": "I have found it: {name}.",
        "missing": "I searched far and wide, but found nothing called {target}.",
        "no file": "I looked through all your writings, but {target} is not among them.",
        "search": "I have asked the wide world about {target}.",
        "web": ["Let me consult the wide world, my child.", "A moment. I shall seek this out."],
        "no music": "No music plays that I could guide.",
        "no sound": "No sound flows that I could soften or raise, my child.",
        "no answer": "My child, your writings are silent on this.",
        "time": "The hour is {time}.", "date": "Today is {date}.",
        "fresh": ["It is forgotten. We begin anew.", "Like footprints in the tide, it is gone."],
    },
}


def persona(config: Config) -> dict:
    return PERSONAS.get(config.persona, PERSONAS["witty"])


def line(config: Config, key: str, **values: str) -> str:
    """What the persona says for `key`: one of its variations, with the names filled in."""
    said = persona(config)[key]
    return (random.choice(said) if isinstance(said, list) else said).format(**values)


def spoken_reply(config: Config, prompt: str) -> str:
    from . import llm

    intro = (f"You are Rigved, the personal AI living in this computer, {persona(config)['about']}. You are being heard, "
             "not read: reply in one to three short sentences, with no lists, markdown, emoji or file paths. "
             "Answer the latest message; never repeat an earlier reply.")
    history = [{"role": "system", "content": intro}, *conversation.messages()]
    reply = llm.quick(config.ollama, config.model, prompt, timeout=120, history=history)
    return re.sub(r"<think>.*?</think>|[*_#`]", "", reply, flags=re.S).strip()


def answer(question: str, config: Config) -> str:
    """An answer from your files, short enough to be spoken."""
    from .search import cite, search

    question = conversation.standalone(question, config.ollama, config.model)  # "and the deposit?" → the full question
    rows = search(config.database_path, question, 8, config)
    if not rows:
        return line(config, "no answer")
    context = "\n\n".join(f"SOURCE: {cite(row)}\n{row['text']}" for row in rows)
    return spoken_reply(config, "Answer the question from this context from the user's own files. If it is about "
                                "them and the files don't answer it, say so; if it is general knowledge the files "
                                "don't cover, answer it anyway, briefly.\n\n" + context + "\n\nQUESTION: " + question)


def talk(said: str, config: Config, log=print, searching=lambda: None, private: list | None = None) -> str:
    """A reply in character: from Claude when it's set up (see claude.py), else, or if Claude can't be had, locally.
    Claude hands back what only your files can answer, and they're searched here (and `private` is told). Claude
    hears the conversation without the turns answered from your files."""
    if config.claude_model:
        from . import claude

        try:
            reply = claude.reply(said, conversation.transcript(public=True), persona(config)["about"], config.claude_model,
                                 searching)
            if reply != claude.FILES:
                return reply
            if private is not None:
                private.append(True)
            return answer(said, config)
        except claude.ClaudeError as error:
            log(f"Claude couldn't answer ({error}); answering locally.")
    return spoken_reply(config, "They said: " + said)


FIRST_PERSON = {"i", "i'm", "i've", "i'd", "i'll", "my", "mine", "myself", "our", "ours", "we", "we're", "us"}


def personal(question: str) -> bool:
    """Whether a question is about you ("when did I pay the bill", "what's my lease deposit"), so it's answered from
    your files and never leaves this computer. "Tell me why cats purr" isn't: "me" alone doesn't count."""
    return bool(set(words(question)) & FIRST_PERSON)


ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth", "eleventh",
            "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth",
            "twentieth", "twenty-first", "twenty-second", "twenty-third", "twenty-fourth", "twenty-fifth", "twenty-sixth",
            "twenty-seventh", "twenty-eighth", "twenty-ninth", "thirtieth", "thirty-first"]


def spoken_name(name: str) -> str:
    """A file name as it should be said: no extension, no underscores."""
    return re.sub(r"[_\-.]+", " ", Path(name).stem if "." in name else name).strip()


def carry_out(steps: list[Step], config: Config, log=print, dry_run: bool = False, opened: list[dict] | None = None,
              searching=lambda: None, private: list | None = None) -> str:
    """Do each step and return what Rigved says about it all. What it opens is added to `opened`, for "open it" later;
    `searching` is told when an answer has to wait on the web, `private` when one came from your files."""
    private = [] if private is None else private
    opened = [] if opened is None else opened
    said: list[str] = []
    done = failed = False
    steps = list(steps)  # an "open X and Y" that turns out to be two things adds a step
    for step in steps:
        if step.kind == "stop":
            return line(config, "bye")
        if step.kind == "unknown" and meaningful(step.target) and len(words(step.target)) > 1:
            said.append(line(config, "cant"))  # heard it fine; it's something Rigved can't do yet ("reboot the system")
        elif step.kind == "unknown" or (not step.target and step.kind in ("open", "find", "search", "ask", "talk", "volume", "media")):
            said.append(line(config, "unsure"))
        elif step.kind == "time":
            now = datetime.datetime.now()
            time = now.strftime("%-I") + " o clock" if now.minute == 0 else now.strftime("%-I:%M")
            part = "morning" if now.hour < 12 else "afternoon" if now.hour < 17 else "evening"
            said.append(line(config, "time", time=f"{time} in the {part}"))
        elif step.kind == "date":
            now = datetime.date.today()
            said.append(line(config, "date", date=f"{now.strftime('%A')}, the {ORDINALS[now.day - 1]} of {now.strftime('%B')}"))
        elif step.kind == "forget":
            if not dry_run:
                conversation.forget()
            said.append(line(config, "fresh"))
        elif step.kind == "open":
            last = conversation.last_opened() if step.target in REFERENCES else None
            parts = step.target.split(" and ")
            if not last and len(parts) > 1 and all(resolve(part, config, files=False)[0] for part in parts):
                steps.extend(Step("open", part) for part in parts)  # "google drive and google chrome": no file search
                continue
            kind, name, thing = (last["kind"], last["name"], last["thing"]) if last else resolve(step.target, config)
            if not kind and " and " in step.target:  # "open fidelity.com and google chrome": two things
                steps.extend(Step("open", part) for part in step.target.split(" and "))
                continue
            log(f"open {step.target!r} → {kind or 'nothing'} {thing}")
            if not kind:
                said.append(line(config, "missing", target=step.target))
                failed = True
                continue
            if not dry_run:
                open_thing(kind, thing)
            opened.append({"kind": kind, "name": name, "thing": thing})
            done = True
            said.append(line(config, kind, name=spoken_name(name) if kind == "file" else name))
        elif step.kind == "find":
            found = find_file(step.target, config)
            if found and Path(found).suffix.lower() in MEDIA and not named(step.target, found):
                found = ""  # a film found by its subtitles plays in VLC, which can take the speakers for itself
            log(f"find {step.target!r} → {found or 'nothing'}")
            if not found:
                said.append(line(config, "no file", target=step.target))
                failed = True
                continue
            if not dry_run:
                open_thing("file", found)
            opened.append({"kind": "file", "name": Path(found).name, "thing": found})
            done = True
            said.append(line(config, "file", name=spoken_name(Path(found).name)))
        elif step.kind == "search":
            if not dry_run:
                launch("gio", "open", "https://www.google.com/search?q=" + re.sub(r"\s+", "+", step.target))
            done = True
            said.append(line(config, "search", target=step.target))
        elif step.kind == "ask" and (not config.claude_model or personal(step.target)):
            private.append(True)
            said.append(answer(step.target, config) if not dry_run else "(an answer from your files)")
        elif step.kind in ("ask", "talk"):  # a question about the world rather than you goes to Claude, when it's set up
            said.append(talk(step.target, config, log, searching, private) if not dry_run else "(a reply in character)")
        elif step.kind == "volume":
            if not dry_run and not volume(step.target):
                said.append(line(config, "no sound"))
                failed = True
                continue
            done = True
        elif step.kind == "media":
            if not dry_run and not media(step.target):
                said.append(line(config, "no music"))
                failed = True
                continue
            done = True
    if done and not failed and not said and all(step.kind in ("media", "volume") for step in steps):
        return ""  # pausing a film or turning it up needs no words over it, just the tone
    if done and not failed and config.persona == "sage":  # "Tathaastu" before the news; the witty one just gets on with it
        said.insert(0, line(config, "done"))
    return " ".join(said) or line(config, "done")
