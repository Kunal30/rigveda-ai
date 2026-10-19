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
    """What was said after the wake word ("Rigved, open Downloads" → "open downloads"), "" for the wake word alone,
    or None when the speech didn't start with it. Whisper hears the name in different ways ("Rig Ved", "Rigvid")."""
    tokens = words(text)
    start = 0
    while start < min(len(tokens), 2) and tokens[start] in FILLERS:
        start += 1
    for size in (1, 2, 3):  # "rigved", "rig ved", "rig ve da"
        name = "".join(tokens[start:start + size])
        if name.startswith("r") and max(similar(name, word) for word in WAKE_WORDS) >= 0.8:
            return " ".join(tokens[start + size:])
    return None


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


def _rule(text: str) -> Step | None:
    text = re.sub(r"^(?:please|can you|could you|would you|will you)\s+", "", text).removesuffix(" please")
    if re.fullmatch(r"(?:stop|cancel|never ?mind|nothing|quiet|be quiet|shut up|go to sleep|that's all|thank you|thanks)", text):
        return Step("stop")
    if re.fullmatch(r"(?:forget (?:that|it|this|everything|all that|the conversation|what i said)|start over|"
                    r"start (?:a )?new (?:conversation|topic)|new (?:conversation|topic)|change (?:of )?(?:the )?topic|clean slate)", text):
        return Step("forget")
    if re.fullmatch(r"(?:what(?:'s| is) the time|what time is it|tell me the time|time)", text):
        return Step("time")
    if re.fullmatch(r"(?:what(?:'s| is) (?:the date|today's date)|what day is (?:it|today)|today's date|date)", text):
        return Step("date")
    if re.fullmatch(r"(?:turn (?:it|the volume) up|volume up|louder|increase (?:the )?volume)", text):
        return Step("volume", "up")
    if re.fullmatch(r"(?:turn (?:it|the volume) down|volume down|quieter|softer|decrease (?:the )?volume|lower (?:the )?volume)", text):
        return Step("volume", "down")
    if re.fullmatch(r"(?:mute|mute (?:it|the sound|the volume)|silence)", text):
        return Step("volume", "mute")
    if re.fullmatch(r"(?:unmute|unmute (?:it|the sound|the volume))", text):
        return Step("volume", "unmute")
    if match := re.fullmatch(r"(?:set )?(?:the )?volume (?:to )?(\d{1,3}|[a-z ]+?)(?: percent)?", text):
        level = match.group(1)
        if level.isdigit() or level in NUMBERS:
            return Step("volume", str(min(int(level) if level.isdigit() else NUMBERS[level], 100)))
    if re.fullmatch(r"(?:pause|stop) (?:the |this )?(?:music|song|video|movie|playback|it)|pause", text):
        return Step("media", "pause")
    if re.fullmatch(r"(?:play|resume|continue|unpause)(?: (?:the |this )?(?:music|song|video|movie|playback|it))?", text):
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
search: search the web (target: the query)
ask: a question whose answer is in the user's own documents or accounts, e.g. their lease, taxes, passport or pay (target: the question)
talk: conversation, plans, advice or general knowledge such as weather, travel, facts or how-to, even about the user's own plans (target: what was said)
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
    return [step for step in steps if step.kind in KINDS] or [Step("unknown", command)]


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


def resolve(target: str, config: Config) -> tuple[str, str, str]:
    """(kind, display name, what to open) for a spoken name: a folder, an app, a website or a file in the index.
    kind is "" when nothing matches."""
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
    """Media players that can be controlled (MPRIS), the one playing first."""
    try:
        names = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path",
                                "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.ListNames"],
                               capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = re.findall(r"'(org\.mpris\.MediaPlayer2\.[^']+)'", names)

    def playing(name: str) -> bool:
        status = subprocess.run(["gdbus", "call", "--session", "--dest", name, "--object-path", "/org/mpris/MediaPlayer2",
                                 "--method", "org.freedesktop.DBus.Properties.Get", "org.mpris.MediaPlayer2.Player",
                                 "PlaybackStatus"], capture_output=True, text=True, timeout=5).stdout
        return "Playing" in status

    return sorted(found, key=lambda name: not playing(name))


def media(what: str) -> bool:
    method = {"play": "Play", "pause": "Pause", "next": "Next", "previous": "Previous"}[what]
    found = players()
    if not found:
        return False
    subprocess.run(["gdbus", "call", "--session", "--dest", found[0], "--object-path", "/org/mpris/MediaPlayer2",
                    "--method", f"org.mpris.MediaPlayer2.Player.{method}"], capture_output=True, timeout=5)
    return True


def volume(what: str) -> None:
    sink = "@DEFAULT_AUDIO_SINK@"
    command = {"up": ["set-volume", "-l", "1.0", sink, "10%+"], "down": ["set-volume", sink, "10%-"],
               "mute": ["set-mute", sink, "1"], "unmute": ["set-mute", sink, "0"]}.get(what)
    subprocess.run(["wpctl", *(command or ["set-volume", sink, f"{int(what)}%"])], capture_output=True, timeout=5)


# ── who's talking ──
# Each persona has its own way of saying the same things; `persona` in the config picks one.

PERSONAS = {
    "witty": {
        "about": "a quick-witted, supremely confident genius with dry humour and playful sarcasm, like a brilliant "
                 "inventor's AI sidekick: warm underneath, never mean",
        "speed": 1.05,
        "greet": ["Talk to me.", "I'm all ears. Well, all microphone.", "Yeah? What are we building today?",
                  "Go ahead, make it interesting."],
        "done": ["Done.", "Consider it handled.", "Easy."],
        "unsure": ["Didn't catch that. Run it by me again?", "Missed that one. Again, maybe slower?"],
        "bye": ["Fine. I'll be here, being brilliant.", "Going quiet. Call me when you need a genius."],
        "broke": "Something broke. Not my fault, probably.",
        "folder": "{name}, open. Try not to judge the clutter.", "app": "{name}, up and running.",
        "website": "{name}, coming right up.", "file": "Found it: {name}.",
        "missing": "I looked everywhere. No {target}. Are we sure that exists?",
        "no file": "Went through every file you own. No sign of {target}.",
        "search": "Asking the internet about {target}. Brace yourself.",
        "no music": "Nothing's playing. I can't pause silence.",
        "no answer": "Your files have nothing on that. Shocking, I know.",
        "time": "It's {time}.", "date": "It's {date}.",
        "fresh": ["Wiped. Clean slate.", "Forgotten. What's next?"],
    },
    "sage": {
        "about": "a wise and gentle Indian sage who speaks calmly, warmly and simply, sometimes with an image from "
                 "nature or an old teaching, and now and then calls the listener \"my child\" or \"beta\"",
        "speed": 0.88,
        "greet": ["Yes, my child. I am listening.", "Speak, and I shall listen.", "Haan, beta. What do you seek?",
                  "I am here. Tell me what you need."],
        "done": ["Tathaastu.", "So be it.", "It is done."],
        "unsure": ["Forgive me, my child. Those words escape me. Say them once more.",
                   "The meaning eludes me, beta. Speak it again, a little differently."],
        "bye": ["Very well. I shall rest, but never far.", "As you wish. Call, and I will be here."],
        "broke": "Forgive me, my child. Something stood in my way.",
        "folder": "Your {name} folder lies open before you.", "app": "{name} awakens.",
        "website": "The path to {name} is open.", "file": "I have found it: {name}.",
        "missing": "I searched far and wide, but found nothing called {target}.",
        "no file": "I looked through all your writings, but {target} is not among them.",
        "search": "I have asked the wide world about {target}.",
        "no music": "No music plays that I could guide.",
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


def talk(said: str, config: Config) -> str:
    return spoken_reply(config, "They said: " + said)


ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth", "eleventh",
            "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth",
            "twentieth", "twenty-first", "twenty-second", "twenty-third", "twenty-fourth", "twenty-fifth", "twenty-sixth",
            "twenty-seventh", "twenty-eighth", "twenty-ninth", "thirtieth", "thirty-first"]


def spoken_name(name: str) -> str:
    """A file name as it should be said: no extension, no underscores."""
    return re.sub(r"[_\-.]+", " ", Path(name).stem if "." in name else name).strip()


def carry_out(steps: list[Step], config: Config, log=print, dry_run: bool = False, opened: list[dict] | None = None) -> str:
    """Do each step and return what Rigved says about it all. What it opens is added to `opened`, for "open it" later."""
    opened = [] if opened is None else opened
    said: list[str] = []
    done = failed = False
    steps = list(steps)  # an "open X and Y" that turns out to be two things adds a step
    for step in steps:
        if step.kind == "stop":
            return line(config, "bye")
        if step.kind == "unknown" or (not step.target and step.kind in ("open", "find", "search", "ask", "talk", "volume", "media")):
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
        elif step.kind == "ask":
            said.append(answer(step.target, config) if not dry_run else "(an answer from your files)")
        elif step.kind == "talk":
            said.append(talk(step.target, config) if not dry_run else "(a reply in character)")
        elif step.kind == "volume":
            if not dry_run:
                volume(step.target)
            done = True
        elif step.kind == "media":
            if not dry_run and not media(step.target):
                said.append(line(config, "no music"))
                failed = True
                continue
            done = True
    if done and not failed and config.persona == "sage":  # "Tathaastu" before the news; the witty one just gets on with it
        said.insert(0, line(config, "done"))
    return " ".join(said) or line(config, "done")
