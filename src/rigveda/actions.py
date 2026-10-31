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

from . import conversation, names
from .config import Config

WAKE_WORDS = ("rigved", "rigveda")
SHORT_NAMES = {"ved", "veda"}  # "Hey Ved"; said alone it's heard as "wait" or "read", so it needs the "hey"
FILLERS = {"hey", "hi", "hello", "ok", "okay", "oh", "o", "ah", "uh", "um", "hmm", "namaste", "so", "yo"}
VERBS = (r"(?:please\s+)?(?:open|launch|start|run|show(?:\s+me)?|go\s+to|bring\s+up|pause|play|resume|find|search|"
         r"google(?!\s+(?:chrome|drive|maps|docs|calendar|photos|music)))")
KINDS = ("open", "find", "search", "ask", "talk", "time", "date", "volume", "media", "forget", "stop",
         "app", "press", "setting", "power", "window", "screenshot", "type", "check", "do")
DESKTOP = ("app", "press", "setting", "power", "window", "screenshot", "type")  # done through desk.py
YES = re.compile(r"(?:(?:yes|yeah|yep|yup|sure|ok|okay|alright|confirm|absolutely|of course|please do|do it|go ahead|"
                 r"go for it)(?: |$))+(?:please|rigved|now)?")
NO = re.compile(r"(?:no|nope|cancel|don't|do not|never ?mind|leave it|not now|forget it|stop)(?: (?:thanks|thank you|"
                r"please|rigved|don't|do it))*")
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
    if re.fullmatch(r"(?:what(?:'s| is) my (?:name|first name|full name)(?: likely)?|who am i|do you know (?:my name|who i am))", text):
        return Step("talk", text)  # it's in the config, not something to look for in your files ("George", 2026-10-30)
    if re.fullmatch(r"(?:where(?:'s| is| did) (?:the |my )?(?:floating )?orb(?: go)?|(?:show|open|bring back|start|restart) "
                    r"(?:me )?(?:the |my )?(?:floating |rigved )?orb(?: again| back)?(?: up)?)", text):
        return Step("do", "start the orb")  # it was closed: its service starts it again
    if re.fullmatch(r"(?:undo (?:that|it|this|the last (?:change|thing))|undo|put (?:it|that) back|change (?:it|that) back|"
                    r"revert (?:that|it)|go back to how it was)", text):
        return Step("setting", "undo")  # whatever Rigved last changed: a setting, a service, the sound output
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
app: do something an app offers, in that app (target "app: what", e.g. "terminal: new window", "chrome: new incognito window", "firefox: new private window", "chrome: new tab", "pycharm: new project", "pycharm: run"). Use this, not open, for a new or another window or tab of an app, or anything done inside an app. When the app isn't named, use the one in front
press: press a button, menu, tab or menu item in the app in front, by its name (target: the name, e.g. "File", "Preferences", "Export")
setting: change a desktop setting (target: the setting and on or off, e.g. "dark mode on", "night light off", "do not disturb on", "large text on", "wifi off", "bluetooth on", "animations off", "hide the dock on"); for a setting listed below as "<schema> <key>: what it is (what it can be; now what it is)", the target is "<schema> <key> <value>", e.g. "org.gnome.settings-daemon.plugins.color night-light-temperature 2700"; target "undo" puts back the last setting changed. The power mode (performance, balanced, power saver, battery saver) and muting the microphone are do, not setting
power: target "lock", "suspend", "restart", "shut down" or "log out"
window: something done to the window in front (target "maximize", "minimize", "restore", "left half", "right half", "full screen", "close", or "switch to <app>")
screenshot: take a screenshot (target "screen" or "window")
type: type text into the app in front (target: the text)
do: change something about this computer that can be undone, other than a setting: start, stop or restart a service (e.g. ollama, docker, the listening service, the orb), connect to or disconnect from a Wi-Fi network or connection by name, connect or disconnect a Bluetooth device (mouse, headphones), switch the sound output or input (e.g. to the headphones or the TV), mute or unmute the microphone or sound, set the power mode (performance, balanced, power saver, battery saver), or make an app the default for a kind of file, e.g. VLC for MKV (target: what to do, complete on its own)
do also (Rigved asks before doing these): quit or force quit a program ("quit firefox", "kill what's using the most memory"), delete, trash or remove a file or folder by name (it goes to the Trash), move or rename a file ("move the trailer to documents" is one do step, not find then do), empty the Trash, install or uninstall software
Deleting for good, wiping, formatting, and anything else needing sudo: unknown (target: ""), one step
"undo that", "put it back", "change it back": setting "undo" (not forget)
check: a question about this computer itself, answered by looking at it: disk space, memory, CPU, GPU, temperatures, battery, network, IP address, Wi-Fi, uptime, what's running or using resources, installed software and versions, updates, services, devices (target: the question, complete on its own)
unknown: none of these fit (target: "")
Keep the user's order, and add no step that wasn't asked for. Reply with JSON only.
"""
CATALOG_PROMPT = """Settings and apps on this computer that may be what the command means. To open an app, the target must be an app's name as given here, even when it was called something else: its maker, a nickname or what it does (e.g. "mozilla" is Firefox):
{catalog}

"""
CONTEXT_PROMPT = """What's on screen, to work out what the command is about ("close it", "a new one", "the file menu"):
{context}

"""
CONVERSATION_PROMPT = """The conversation so far, to work out what the command refers to. Make every target complete on its own: a follow-up such as "and the deposit?" becomes the full question, and "it", "that" or "the second one" become what they refer to.
{conversation}

"""


def understand(command: str, config: Config) -> list[Step]:
    """Steps for a command: rules first, the local model for anything they don't cover. "Open" by rule only names
    something there is ("open terminal"); "open a new terminal window" goes to the model, with what's on screen."""
    if (pending := conversation.confirming()) is not None:
        said = " ".join(words(command))
        if YES.fullmatch(said):
            return [Step("yes", pending["thing"])]
        if NO.fullmatch(said):
            return [Step("cancel", pending.get("name", ""))]
    if (choices := conversation.choices()) and (picked := names.choose(command, choices)):
        app = (conversation.asking() or {}).get("app")
        return [Step("open", f"{picked} in {app}" if app else picked)]  # "the remastered one", after Rigved asked which
    if (match := re.fullmatch(OPEN_IN, " ".join(words(command)))) and opens_in(match.group(1)):
        return [Step("open", match.group(1))]  # "open Mirzapur in VLC": one thing, not Mirzapur and then VLC
    steps = parse(command)
    talked = conversation.transcript()
    if steps is not None and any(step.kind == "open" and not plain(step.target, config) for step in steps):
        steps = None  # nothing is called "a new terminal window": the model works out what was meant
    if steps is not None and not (talked and any(leans(step.target) for step in steps)):
        return steps
    from . import llm

    from . import catalog

    seen, near = on_screen(), catalog.offered(command, config)
    prompt = (MODEL_PROMPT + (CATALOG_PROMPT.format(catalog=near) if near else "") + (CONTEXT_PROMPT.format(context=seen) if seen else "")
              + (CONVERSATION_PROMPT.format(conversation=talked) if talked else "") + "Command: " + command)
    try:
        reply = llm.quick(config.ollama, config.model, prompt, timeout=60, format=MODEL_FORMAT)
        steps = [Step(str(step.get("action", "")), str(step.get("target", "")).strip()) for step in json.loads(reply)["steps"]]
    except (llm.OllamaError, ValueError, KeyError, TypeError, AttributeError):
        return [Step("talk", command)]
    if any(step.kind in ("time", "date") for step in steps) and re.search(ELSEWHERE, command.lower()):
        return [Step("talk", command)]  # "what time is it in London" isn't the time here: Claude knows
    if any(step.kind in ("volume", "media") for step in steps) and not re.search(SOUND_WORDS, command.lower()):
        # "can you hear me now?" after some volume commands came back as volume up, 100 and play (2026-10-25)
        steps = [step for step in steps if step.kind not in ("volume", "media")] or [Step("talk", command)]
    if config.claude_model and not re.search(ASKED_TO_SEARCH, command.lower()):  # "the worst news in London today"
        steps = [Step("talk", command) if step.kind == "search" else step for step in steps]  # Claude looks it up
    steps = [Step("open", known_app(step.target, config) or step.target)
             if step.kind == "open" and not plain(step.target, config) else step for step in steps]
    return [step for step in steps if step.kind in KINDS] or [Step("unknown", command)]


APP_FORMAT = {"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]}


def known_app(called: str, config: Config) -> str:
    """The installed app that something nothing is called is another name for ("mozilla" → Firefox), by the local
    model's knowledge of who makes what; "" if none is."""
    if len(called.split()) > 4:
        return ""
    from . import llm

    installed = sorted({name for name, _ in apps().values()})
    try:
        reply = llm.quick(config.ollama, config.model, "Installed apps: " + ", ".join(installed) + f"\nIs \"{called}\" "
                          "another name for one of them (its maker, a nickname, an old name)? Reply with JSON: the app's "
                          "name exactly as listed, or \"\" if none is.", timeout=30, format=APP_FORMAT)
        named = str(json.loads(reply)["app"]).strip()
    except (llm.OllamaError, ValueError, KeyError, TypeError):
        return ""
    return named if named in installed else ""


OPEN_IN = r"(?:(?:okay|ok|so|now|uh|um|rigved|hey rigved) )*(?:can you |please |could you )*(?:open|play|watch|start|put on) " \
          r"((?:the )?.+ (?:in|with|using|on) .+?)(?: please)?"
SOUND_WORDS = (r"\b(?:volume|loud|louder|quiet|quieter|soft|softer|mute|unmute|silence|sound|up|down|max|"
               r"percent|\d+|play|playing|pause|resume|stop|next|previous|skip|back|track|song|music|movie|film|"
               r"video|show|episode|it)\b|%")
ASKED_TO_SEARCH = r"\b(?:search|google|look (?:it |that )?up|browser)\b"
ELSEWHERE = r"\b(?:in|at|utc|gmt|zone|timezone|until|since|ago|from now)\b"


def quick(text: str) -> bool:
    """A short command that's safe to take without the name, when it's your voice: pause, play, louder, next and such."""
    steps = parse(text)
    return bool(steps) and len(words(text)) <= 5 and all(step.kind in ("media", "volume") for step in steps)


BARE_MEDIA = r"(?:pause|play|resume)(?: it| the (?:movie|film|video|show|music|song))?(?: please)?"


def bare_media(text: str) -> bool:
    """Just "pause" or "play" (or "pause the movie"): taken whoever's voice it sounds like, since a film never says
    only that, and can't when it's paused. Over a loud film, or shouted, your voice scored as low as the film's."""
    return bool(re.fullmatch(BARE_MEDIA, " ".join(words(text))))


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


GREETING = re.compile(r"(?:(?:i said|and) )?(?:(?:hey|hi|hello|okay|ok) )?(?:good (?:morning|afternoon|evening|night)|"
                      r"morning|night|good ?bye|bye|i'm back|i am back|namaste)(?: (?:rigved|buddy|friend|mate))?")


def addressed(said: str, config: Config) -> bool:
    """Whether something you said was said to Rigved, rather than to someone else, another app or yourself."""
    if parse(said) is not None:  # a plain command: "pause", "open downloads"
        return True
    if (choices := conversation.choices()) and names.choose(said, choices):  # "the remastered one", after "which one?"
        return True
    if conversation.confirming() is not None and (YES.fullmatch(" ".join(words(said))) or NO.fullmatch(" ".join(words(said)))):
        return True  # "yes", after "restart?"
    if GREETING.fullmatch(" ".join(words(said))):  # "good morning" in your voice: the model took it for someone else
        return True
    from . import llm

    prompt = ADDRESSED_PROMPT.format(conversation=conversation.transcript() or "(nothing yet)", said=said)
    try:
        reply = llm.quick(config.ollama, config.model, prompt, timeout=20, format=ADDRESSED_FORMAT, options={"temperature": 0})
        return bool(json.loads(reply)["for_rigved"])
    except (llm.OllamaError, ValueError, KeyError, TypeError):
        return True  # can't tell: better to answer than to ignore you


def plain(target: str, config: Config) -> bool:
    """Whether "open <target>" names something there is: an app, folder, site or file by name, or what was opened."""
    if target in REFERENCES or Path(target).is_absolute() or opens_in(target):
        return True
    if " ".join(words(target)) in GENERIC:  # "open preferences": of the app in front, most likely
        return False
    parts = target.split(" and ")
    return all(resolve(part, config, files=False)[0] or names.matches(part) for part in parts)


GENERIC = {"preferences", "settings", "options", "help", "about", "history", "bookmarks", "extensions", "menu",
           "the menu", "the settings", "the preferences", "new tab", "a new tab", "new window", "a new window"}


def opens_in(target: str) -> bool:
    """Whether it's a file to open in a named app ("mirzapur in vlc"), not something to do in it ("a new tab in
    chrome")."""
    rest, app = with_app(target)
    return app is not None and (bool(names.matches(rest)) or Path(rest).is_absolute())


def app_name(seen: str) -> str:
    """An app as the accessibility tree names it, as you would: "org.gnome.Terminal" → "Terminal"."""
    return re.sub(r"^(?:[a-z0-9]+\.)+", "", seen) if re.fullmatch(r"(?:[a-z0-9]+\.)+\w+", seen) else seen


HIDDEN_APPS = {"gnome-shell", "gjs", "orb.py", "ibus-extension-gtk3", "update-notifier", "evolution-alarm-notify",
               "xdg-desktop-portal-gtk", "mutter-x11-frames", "fdm"}


def on_screen() -> str:
    """What's on screen, for the model: the app in front, its window, what can be pressed in it, the apps open and
    what Rigved opened last. Window titles and menus go to the local model only."""
    from . import desk

    seen = desk.look()
    lines = []
    if seen["app"]:
        lines.append(f"In front: {app_name(seen['app'])}, window \"{seen['window'][:100]}\"")
    if seen["controls"]:
        lines.append("What can be pressed there: " + ", ".join(seen["controls"][:40]))
    if apps_open := [app_name(app) for app in seen["apps"] if app not in HIDDEN_APPS]:
        lines.append("Apps open: " + ", ".join(apps_open))
    if last := conversation.last_opened():
        lines.append(f"Last opened by Rigved: {last['name']} ({last['kind']})")
    return "\n".join(lines)


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
    the index (which takes seconds). kind is "" when nothing matches, "choices" when several files are equally meant
    (what to open is then each of them, a line each)."""
    if Path(target).is_absolute() and Path(target).is_file():  # chosen from those
        return "file", Path(target).name, target
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
    meant = names.meant(names.matches(target))  # by name first: a film's name is all there is to go on
    if len(meant) > 1:
        return "choices", names.spoken(meant[0].path), "\n".join(str(match.path) for match in meant)
    if meant:
        return "file", meant[0].path.name, str(meant[0].path)
    found = find_file(target, config)
    if found and named(target, found):  # "open" mustn't play a film because its subtitles mentioned music
        return "file", Path(found).name, found
    return "", target, ""


MEDIA = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac"}


def named(asked: str, path: str) -> bool:
    """Whether a file's name has at least half the words that matter in what was asked for."""
    wanted = {word for word in words(asked) if len(word) > 2 and word not in STOPWORDS}
    return bool(wanted) and len(wanted & set(words(re.sub(r"[_\-.]+", " ", Path(path).name)))) * 2 >= len(wanted)


COUNTS = {2: "two", 3: "three"}


def which(name: str, paths: list[str], config: Config) -> str:
    """Rigved asking which of several files you meant: "You've got two of Iron Man 3 (2013): the HDR WeSLeY one, or
    the remastered one. Which one?" """
    told = options(paths)
    options_said = ", ".join(told[:-1]) + ", or " + told[-1]
    return line(config, "which", count=COUNTS.get(len(paths), str(len(paths))), name=name, options=options_said)


def options(paths: list[str]) -> list[str]:
    """Each file as Rigved names it when asking which: "the HDR WeSLeY one", "the one in multimedia"."""
    return [f"the {difference} one" if not difference.startswith("in ") else f"the one {difference}"
            for difference in names.differences([Path(path) for path in paths])]


def on_desktop(step: Step, config: Config, log=print, dry_run: bool = False, confirmed: bool = False,
               opened: list | None = None) -> tuple[bool, str]:
    """Do something on the desktop (desk.py): what an app offers, a press, a setting, power, a window, a screenshot or
    typing. What can't be undone (restart, shut down, log out, closing a window) waits for your yes: (done, said)."""
    from . import desk

    opened = [] if opened is None else opened
    target = step.target.strip()
    disruptive = (step.kind == "power" and any(name in target.lower() for name in desk.DISRUPTIVE)
                  or step.kind == "window" and re.search(r"\bclose\b", target.lower()))
    meant = ""
    if step.kind == "window" and not re.match(r"(?:switch|go|change) to ", target.lower()):
        meant = re.sub(r"^.*\bin ", "", target) if " in " in target else ""  # "close [window] in Chrome", on a yes
        front = desk.look()["app"]
        if not meant:
            if front in HIDDEN_APPS or not front:  # the orb, or nothing it can see: not what "this window" means
                return False, line(config, "cant")
            meant = app_name(front)
            target = f"{target} in {meant}"
    if disruptive and not confirmed:
        what = (target[0].upper() + target[1:] if step.kind == "power"
                else f"Close {meant}" if step.kind == "window" else "Close this window")
        opened.append({"kind": "confirm", "name": what, "thing": json.dumps([[step.kind, target]])})
        return True, line(config, "sure", what=what)
    if dry_run:
        return True, f"({step.kind} {target})"
    if step.kind == "window" and meant and not desk._same_app(desk.look()["app"], meant):
        # the window asked about went behind: answering in the orb, or anything else, brought another to the front
        if not desk.bring_forward(meant) or not desk._same_app(desk.look()["app"], meant):
            return False, f"I couldn't get {meant} back in front, so I left it alone."
        target = target.rsplit(" in ", 1)[0]
    if step.kind == "app":
        app, _, what = target.partition(":") if ":" in target else ("", "", target)
        app = app_name(app.strip() or desk.look()["app"] or (conversation.last_opened() or {}).get("name", ""))
        value, _, found = best(" ".join(words(app)), apps()) if app else (0.0, "", None)
        name, file = found if value >= 0.8 and found else (app, None)
        what = " ".join(word for word in words(what) if word not in set(words(name)) | {"app"}) or what
        ok, how = desk.do_in_app(name, what, file, log)
        return ok, line(config, "did", what=f"{how} in {name}".strip()) if ok else line(config, "not found", target=what, app=name or "the app in front")
    if step.kind == "press":
        pressed = desk.press(target)
        if pressed.get("ok"):
            return True, line(config, "did", what=pressed.get("pressed", target))
        app = desk.look()["app"]
        if desk.JETBRAINS.search(app):
            ok, how = desk.do_in_app(app, target, None, log)
            return ok, line(config, "did", what=how) if ok else line(config, "not found", target=target, app=app)
        if re.search(r"\bmenu$", target.lower()) and (letter := re.sub(r"[^a-z]", "", target.lower())[:1]):
            return desk.keys(f"alt+{letter}"), line(config, "did", what=target)  # "the file menu": Alt+F
        log(f"press {target!r}: {pressed.get('why')}; nearest {pressed.get('near')}")
        return False, line(config, "not found", target=target, app=app or "the app in front")
    if step.kind == "setting":
        ok, reply = desk.setting(target)
        if not ok and not reply and target.lower() != "undo":  # no setting by that name: "power mode battery saver"
            from . import machine

            return machine.do(target, config, log, opened)
        return ok, reply or line(config, "cant")
    if step.kind == "power":
        ok, which = desk.power(target)
        return ok, line(config, "did", what=which.capitalize()) if ok else line(config, "cant")
    if step.kind == "window":
        ok, which = desk.window(target)
        return ok, line(config, "did", what=which.capitalize()) if ok else line(config, "cant")
    if step.kind == "screenshot":
        return (True, line(config, "screenshot")) if desk.screenshot(target) else (False, line(config, "broke"))
    if step.kind == "type":
        return (True, line(config, "typed")) if desk.type_text(target) else (False, line(config, "broke"))
    return False, line(config, "cant")


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


def open_thing(kind: str, thing: str, app: str = "") -> None:
    """Open it: an app, or anything else with what it opens in, or in `app` (its .desktop file) when one was named."""
    if kind == "app":
        launch("gio", "launch", thing)
    elif app:
        launch("gio", "launch", app, thing)
    else:
        launch("gio", "open", thing)


PLAYS_IN = {".mkv": "vlc media player"}  # opened by voice in this app, whatever the desktop's default (you chose VLC)


def player(path: str) -> tuple[str, Path] | None:
    """The app a file opens in when you didn't name one: VLC for MKV films; None for the desktop's own choice."""
    wanted = PLAYS_IN.get(Path(path).suffix.lower())
    if not wanted:
        return None
    value, _, app = best(wanted, apps())
    return app if value >= 0.8 else None


def with_app(target: str) -> tuple[str, tuple[str, Path] | None]:
    """What to open and the app named to open it in: "mirzapur in vlc media player" → ("mirzapur", ("VLC media
    player", its .desktop file)); the target as it was and None when no app is named."""
    match = re.fullmatch(r"(.+)\s+(?:in|with|using|on)\s+(.+)", target, re.IGNORECASE)
    if not match:
        return target, None
    value, _, app = best(" ".join(words(match.group(2))), apps())
    return (match.group(1), app) if value >= 0.8 else (target, None)


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
        "about": "a warm, easygoing friend who happens to be brilliant: quick, playful and lightly funny, genuinely "
                 "interested in the person and glad to help, like a good-natured sidekick. Humour is gentle and never "
                 "at their expense; no sarcasm about them, their files or their questions",
        "speed": 1.05,
        "looking": ["Let me have a look through your files.", "One sec, checking your files.", "Give me a moment, I'll find it."],
        "greet": ["Hey {you}, what's up?", "I'm here. What can I do for you?", "Hey! What are we up to?",
                  "Go ahead, I'm listening.", "Yes, {you}?"],
        "done": ["Done.", "All set.", "There you go."],
        "unsure": ["Sorry, I didn't quite catch that. Say it once more?", "I missed that one. Mind saying it again?"],
        "cant": ["I can't do that one yet, sorry. I'm learning, though.", "That's not something I can do yet. Soon, I hope."],
        "bye": ["Okay, I'll be right here if you need me.", "Going quiet. Just call when you want me."],
        "broke": "Hmm, something went wrong on my end. Want to try that again?",
        "folder": "Here's your {name} folder.", "app": "{name} is opening.",
        "website": "Opening {name} for you.", "file": "Found it: {name}.",
        "missing": "I couldn't find anything called {target}. Is it called something else, maybe?",
        "no file": "I looked through all your files and couldn't find {target}, sorry.",
        "which": "I found {count} of {name}: {options}. Which one would you like?",
        "search": "Searching the web for {target}.",
        "web": ["Let me look that up.", "One sec, checking.", "Good question, let me find out."],
        "no music": "Nothing's playing right now, so there's nothing to pause.",
        "no sound": "Nothing's making any sound at the moment.",
        "no answer": "I couldn't find anything about that in your files, sorry.",
        "time": "It's {time}.", "date": "It's {date}.",
        "fresh": ["Okay, fresh start.", "Sure, let's start over. What's next?"],
        "did": ["{what}. Done.", "{what}, all set."], "sure": "{what}? Just say yes and I'll do it.",
        "cancelled": ["Okay, I'll leave it.", "No problem, leaving it as it is."], "not found": "I can't find {target} in {app}.",
        "screenshot": "Got it. The screenshot's in Pictures and on your clipboard.", "typed": "Typed it in.",
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
        "which": "I find {count} of {name}, my child: {options}. Which shall it be?",
        "search": "I have asked the wide world about {target}.",
        "web": ["Let me consult the wide world, my child.", "A moment. I shall seek this out."],
        "no music": "No music plays that I could guide.",
        "no sound": "No sound flows that I could soften or raise, my child.",
        "no answer": "My child, your writings are silent on this.",
        "time": "The hour is {time}.", "date": "Today is {date}.",
        "fresh": ["It is forgotten. We begin anew.", "Like footprints in the tide, it is gone."],
        "did": "{what}. It is done.", "sure": "{what}, my child? Say yes, and it shall be done.",
        "cancelled": "Very well. We leave it be.", "not found": "I do not see {target} in {app}, my child.",
        "screenshot": "I have captured the screen. It rests in your Pictures.", "typed": "It is written.",
    },
}


def persona(config: Config) -> dict:
    return PERSONAS.get(config.persona, PERSONAS["witty"])


def line(config: Config, key: str, **values: str) -> str:
    """What the persona says for `key`: one of its variations, with the names filled in ({you}: your first name)."""
    said = persona(config)[key]
    return (random.choice(said) if isinstance(said, list) else said).format(you=first_name(config) or "there", **values)


def first_name(config: Config) -> str:
    return config.owner_name.split()[0] if config.owner_name.strip() else ""


def about(config: Config) -> str:
    """Who Rigved is and who it's talking to, for the model that speaks for it."""
    name = first_name(config)
    return (persona(config)["about"] + (f". You're talking with {name} ({config.owner_name}), whose computer this is; "
            f"call them {name} now and then, as a friend would, not in every reply" if name else ""))


def spoken_reply(config: Config, prompt: str) -> str:
    from . import llm

    intro = (f"You are Rigved, the personal AI living in this computer, {about(config)}. You are being heard, "
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


def talk(said: str, config: Config, log=print, searching=lambda: None, private: list | None = None,
         sentence=None) -> str:
    """A reply in character: from Claude when it's set up (see claude.py), else, or if Claude can't be had, locally.
    Claude hands back what only your files can answer, and they're searched here (and `private` is told). Claude
    hears the conversation without the turns answered from your files. With `sentence`, Claude's reply is handed to it
    sentence by sentence as it's written; what's returned is then what was said."""
    if config.claude_model:
        from . import claude

        told: list[str] = []

        def tell(part: str) -> None:
            told.append(part)
            sentence(part)

        try:
            reply = claude.reply(said, conversation.transcript(public=True), about(config), config.claude_model,
                                 searching, sentence=tell if sentence else None)
            if reply != claude.FILES:
                return " ".join(told) or reply
            if private is not None:
                private.append(True)
            return answer(said, config)
        except claude.ClaudeError as error:
            if told:  # it stopped partway: what was said stands
                log(f"Claude stopped partway ({error}).")
                return " ".join(told)
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
              searching=lambda: None, private: list | None = None, sentence=None, confirmed: bool = False) -> str:
    """Do each step and return what Rigved says about it all. What it opens is added to `opened`, for "open it" later;
    `searching` is told when an answer has to wait on the web, `private` when one came from your files. With
    `sentence`, a reply that is all Claude's is handed to it sentence by sentence as it's written."""
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
            rest, app = with_app(step.target) if not last else (step.target, None)
            kind, name, thing = (last["kind"], last["name"], last["thing"]) if last else resolve(rest, config)
            if app and kind not in ("file", "choices"):  # "youtube in chrome" is still a website
                app = None
                kind, name, thing = resolve(step.target, config)
            if not kind and " and " in step.target:  # "open fidelity.com and google chrome": two things
                steps.extend(Step("open", part) for part in step.target.split(" and "))
                continue
            log(f"open {step.target!r} → {kind or 'nothing'} {thing}")
            if kind == "choices":
                said.append(which(name, thing.split("\n"), config))
                opened.append({"kind": kind, "name": name, "thing": thing, **({"app": app[0]} if app else {})})
                continue
            if not kind:
                said.append(line(config, "missing", target=step.target))
                failed = True
                continue
            if not dry_run:
                opener = app or (player(thing) if kind == "file" else None)
                open_thing(kind, thing, str(opener[1]) if opener else "")
            opened.append({"kind": kind, "name": name, "thing": thing})
            done = True
            said.append(line(config, kind, name=(names.spoken(thing) + (f" in {app[0]}" if app else "")) if kind == "file" else name))
        elif step.kind == "find":
            meant = names.meant(names.matches(step.target))
            if len(meant) > 1:
                paths = [str(match.path) for match in meant]
                said.append(which(names.spoken(paths[0]), paths, config))
                opened.append({"kind": "choices", "name": names.spoken(paths[0]), "thing": "\n".join(paths)})
                continue
            found = str(meant[0].path) if meant else find_file(step.target, config)
            if found and Path(found).suffix.lower() in MEDIA and not named(step.target, found):
                found = ""  # a film found by its subtitles plays in VLC, which can take the speakers for itself
            log(f"find {step.target!r} → {found or 'nothing'}")
            if not found:
                said.append(line(config, "no file", target=step.target))
                failed = True
                continue
            if not dry_run:
                opener = player(found)
                open_thing("file", found, str(opener[1]) if opener else "")
            opened.append({"kind": "file", "name": Path(found).name, "thing": found})
            done = True
            said.append(line(config, "file", name=names.spoken(found)))
        elif step.kind == "search":
            if not dry_run:
                launch("gio", "open", "https://www.google.com/search?q=" + re.sub(r"\s+", "+", step.target))
            done = True
            said.append(line(config, "search", target=step.target))
        elif step.kind == "ask" and (not config.claude_model or personal(step.target)):
            private.append(True)
            said.append(answer(step.target, config) if not dry_run else "(an answer from your files)")
        elif step.kind in ("ask", "talk"):  # a question about the world rather than you goes to Claude, when it's set up
            said.append(talk(step.target, config, log, searching, private, sentence if len(steps) == 1 else None)
                        if not dry_run else "(a reply in character)")
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
        elif step.kind == "yes":  # what Rigved asked about ("restart?"), now that you said yes
            inner = [Step(kind, target) for kind, target in json.loads(step.target)]
            said.append(carry_out(inner, config, log, dry_run, opened, searching, private, confirmed=True))
            done = True
        elif step.kind == "run":  # what Rigved asked about exactly, after your yes; only ever from a "confirm"
            from . import machine

            ok, reply = machine.carry(json.loads(step.target), log) if not dry_run else (True, "(done)")
            said.append(reply)
            done, failed = done or ok, failed or not ok
        elif step.kind == "cancel":
            said.append(line(config, "cancelled"))
        elif step.kind == "check":  # read-only commands, read by the local model: nothing about this computer leaves it
            from . import machine

            private.append(True)
            said.append(machine.check(step.target, config, log) if not dry_run else "(what the computer says)")
        elif step.kind == "do":
            from . import machine

            ok, reply = machine.do(step.target, config, log, opened) if not dry_run else (True, "(done)")
            said.append(reply)
            done, failed = done or ok, failed or not ok
        elif step.kind in DESKTOP:
            ok, reply = on_desktop(step, config, log, dry_run, confirmed, opened)
            said.append(reply)
            done, failed = done or ok, failed or not ok
    if done and not failed and not said and all(step.kind in ("media", "volume") for step in steps):
        return ""  # pausing a film or turning it up needs no words over it, just the tone
    if done and not failed and config.persona == "sage":  # "Tathaastu" before the news; the witty one just gets on with it
        said.insert(0, line(config, "done"))
    return " ".join(said) or line(config, "done")
