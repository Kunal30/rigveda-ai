"""Your web browser by voice, through Rigveda's own extension in Chrome: "click sign in", "the second result",
"scroll down a bit", "go back", "find refund policy", "show numbers" then "fourteen", "go to my Gmail tab", "close
the YouTube tabs", "reopen that tab".

The extension can't be called, so it calls: it keeps a request open to Rigveda's server (`Relay`, in the server's
process), which hands it the listener's commands and hands back what it found or did. What's on a page (its links,
buttons and headings, your tabs' titles) goes to the local model only, never to Claude: pages show your bank, your
mail, your doctor. A click on something that pays, sends or deletes waits for your yes.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

from .config import Config

WAIT = 25.0  # seconds the extension's request is held open for a command
FRESH = 40.0  # seconds since it last asked for one, for it to count as connected
ANSWER = 8.0  # seconds to wait for it to do something
MOST_ACTIONS = 5
RISKY = re.compile(r"\b(?:buy|purchase|pay|payment|place (?:your )?order|order now|checkout|check out|delete|remove|"
                   r"send|submit|transfer|confirm|unsubscribe|sign out|log ?out|post|publish|book now|donate|"
                   r"subscribe|cancel (?:my |your )?(?:order|subscription|account)|deactivate|close account)\b", re.I)
NUMBER_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
                "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
                "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "for": 4, "to": 2,
                "too": 2, "won": 1, "ate": 8}


# ── in the server: the relay between the listener and the extension ──

class Relay:
    """Commands waiting for the extension, and its answers."""

    def __init__(self) -> None:
        self.waiting: list[tuple[str, dict]] = []
        self.answers: dict[str, dict] = {}
        self.changed = threading.Condition()
        self.seen = 0.0
        self.attention: dict = {}  # where your mouse and eyes were in Chrome last: {"at", "title", "url", "focused"}

    def connected(self) -> bool:
        return time.time() - self.seen < FRESH

    def next(self, wait: float = WAIT) -> dict:
        """For the extension: the next command, or {} if none came in `wait` seconds."""
        self.seen = time.time()
        with self.changed:
            self.changed.wait_for(lambda: self.waiting, timeout=wait)
            self.seen = time.time()
            if not self.waiting:
                return {}
            job, command = self.waiting.pop(0)
            return {"id": job, "command": command}

    def done(self, job: str, result: dict) -> None:
        with self.changed:
            self.answers[job] = result
            self.changed.notify_all()

    def looked(self, page: dict) -> None:
        """For the extension: whether Rigved is focused on Chrome now (you said so, the mouse is over a page, or
        Chrome is in front), and on what page; it says so every few seconds while it is, and once when it stops."""
        self.attention = {"at": time.time(), "on": bool(page.get("on")), "locked": bool(page.get("locked")),
                          "title": str(page.get("title", ""))[:200], "url": str(page.get("url", ""))[:300]}

    def do(self, command: dict, wait: float = ANSWER) -> dict:
        """For the listener: have the extension do something, and what it said."""
        if command.get("do") == "attention":  # known here: no need to ask the browser
            ago = time.time() - self.attention.get("at", 0)
            return {"ok": True, **self.attention, "ago": ago} if self.attention and self.connected() else {"ok": False, "why": "not connected"}
        if not self.connected():
            return {"ok": False, "why": "not connected"}
        job = uuid.uuid4().hex
        with self.changed:
            self.waiting.append((job, command))
            self.changed.notify_all()
            self.changed.wait_for(lambda: job in self.answers, timeout=wait)
            if job in self.answers:
                return self.answers.pop(job)
            self.waiting = [(other, waiting) for other, waiting in self.waiting if other != job]
        return {"ok": False, "why": "no answer from the browser"}


RELAY = Relay()


# ── in the listener ──

def ask(command: dict, config: Config | None = None) -> dict:
    """Have the extension do something, through the server: what it answered."""
    from . import config as configuration

    port = 8765
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/browser/do", data=json.dumps(command).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {configuration.load_token()}"})
    try:
        with urllib.request.urlopen(request, timeout=ANSWER + 2) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as error:
        return {"ok": False, "why": f"Rigveda's server isn't answering ({error})"}


FOCUS_FRESH = 12.0  # seconds the extension's last word on focus holds (it says so every 3 s while focused)


def looking(config: Config | None = None) -> str:
    """The page you're looking at in Chrome, if you are: the mouse moved over it lately, or Chrome has the keyboard.
    "" if not, or if the extension isn't connected."""
    seen = ask({"do": "attention"}, config)
    if seen.get("ok") is False or not seen.get("on"):
        return ""
    return (seen.get("title") or "a page in Chrome") if seen.get("ago", 1e9) < FOCUS_FRESH else ""


def number(text: str) -> int | None:
    """A number said in words or digits ("fourteen", "twenty three", "14", "number 7")."""
    said = re.sub(r"\b(?:number|no\.?|click|press|open|the|on|hint|#)\b", " ", text.lower()).replace("-", " ")
    if match := re.fullmatch(r"\s*(\d{1,3})\s*\.?\s*", said):
        return int(match.group(1))
    parts = said.split()
    if not parts or any(part not in NUMBER_WORDS for part in parts) or len(parts) > 2:
        return None
    values = [NUMBER_WORDS[part] for part in parts]
    if len(values) == 2 and not (values[0] >= 20 and values[0] % 10 == 0 and values[1] < 10):
        return None
    return sum(values)


SCROLLS = {"down": "down", "up": "up", "a bit down": "bit down", "down a bit": "bit down", "bit down": "bit down",
           "bit up": "bit up", "a bit up": "bit up", "up a bit": "bit up", "more": "down", "down more": "down",
           "up more": "up", "more up": "up", "a lot": "page down", "down a lot": "page down", "up a lot": "up",
           "to the top": "top", "top": "top", "to the bottom": "bottom", "bottom": "bottom", "all the way down": "bottom",
           "all the way up": "top", "back to the top": "top", "back up": "up", "further": "down", "further down": "down",
           "higher": "bit up", "lower": "bit down", "a bit higher": "bit up", "a bit lower": "bit down"}
# What speech recognition makes of "scroll" ("screw it up", 2026-10-30), and the words that change nothing.
MISHEARD = {"screw": "scroll", "school": "scroll", "stroll": "scroll", "scrawl": "scroll", "crawl": "scroll",
            "scrolled": "scroll", "scrolling": "scroll", "scroll's": "scroll", "squirrel": "scroll", "scrool": "scroll",
            "scold": "scroll", "skull": "scroll", "little": "bit", "tad": "bit", "touch": "bit", "smidge": "bit"}
NOTHING = {"it", "this", "that", "the", "page", "please", "just", "can", "you", "could", "would", "will", "a", "me",
           "for", "now", "again", "then", "so", "okay", "ok", "um", "uh", "rigved", "hey"}


def _plainly(said: str) -> str:
    text = " ".join(MISHEARD.get(word, word) for word in re.sub(r"[^a-z0-9' ]", " ", said.lower()).split())
    words = text.split()
    kept = [word for index, word in enumerate(words) if word not in NOTHING or word == "a" and index + 1 < len(words) and words[index + 1] in ("bit", "lot")]
    return " ".join(kept)


def quick(said: str, looking: bool = False) -> str | None:
    """What a common phrasing means, without the model ("scroll down a bit" → "=scroll bit down"). While you're
    looking at Chrome, a bare direction is enough too ("a bit higher", "down")."""
    original = " ".join(re.sub(r"[^a-z0-9' ]", " ", said.lower()).split()).removesuffix(" please")
    if re.fullmatch(r"(?:show|give me|put up|turn on)(?: me)? (?:the )?(?:numbers|hints|labels)(?: on (?:the|this) page)?", original):
        return "=hints on"
    if re.fullmatch(r"(?:focus|stay|stick|lock)(?: on| in| with)?(?: the)? (?:chrome|browser|web browser)|(?:browser|chrome|focus) mode(?: on)?|"
                    r"turn on (?:browser|chrome|focus) mode", original):
        return "=focus on"
    if re.fullmatch(r"(?:stop|end|quit|exit|leave) (?:focusing|focus mode|browser mode|chrome mode|focusing on (?:the )?(?:chrome|browser))|"
                    r"(?:unfocus|leave (?:the )?(?:chrome|browser))|(?:browser|chrome|focus) mode off|turn off (?:browser|chrome|focus) mode", original):
        return "=focus off"
    if match := re.fullmatch(r"(?:scroll )?(?:one )?page (down|up)", original):
        return f"=scroll {'page down' if match.group(1) == 'down' else 'up'}"
    text = re.sub(r"\b(bit)(?: bit)+\b", r"\1", _plainly(said))  # "a little bit" is "a bit"
    if looking and (how := SCROLLS.get(text) or SCROLLS.get(text.replace("bit", "a bit"))):
        return f"=scroll {how}"  # "a bit higher", "down", with Chrome in front of you
    if re.fullmatch(r"(?:go|move|keep going|keep scrolling)(?: (up|down))?(?: (bit|more|a bit|a lot))?", text) and text != "go":
        direction, amount = re.fullmatch(r"(?:go|move|keep going|keep scrolling)(?: (up|down))?(?: (bit|more|a bit|a lot))?", text).groups()
        if direction or text.startswith("keep"):
            return f"=scroll {'bit ' if amount in ('bit', 'a bit') else ''}{direction or 'down'}"
    if re.fullmatch(r"(?:go )?back(?: a page)?|previous page|back page", text):
        return "=back"
    if re.fullmatch(r"(?:go )?forward(?: a page)?", text):
        return "=forward"
    if match := re.fullmatch(r"scroll(?: (.+))?|go (to (?:the )?(?:top|bottom)|all the way (?:up|down))|take (?:me )?(to (?:the )?(?:top|bottom))", text):
        how = (match.group(1) or match.group(2) or match.group(3) or "down").replace("to top", "to the top").replace("to bottom", "to the bottom")
        how = SCROLLS.get(how) or SCROLLS.get(how.replace("bit", "a bit"))
        return f"=scroll {how}" if how else None
    if text in ("scroll more", "scroll some more", "more scroll"):
        return "=scroll down"
    if re.fullmatch(r"(?:reload|refresh)(?: (?:the|this) page)?", text):
        return "=reload"
    if re.fullmatch(r"(?:show|give me|put up|turn on)(?: me)? (?:the )?(?:numbers|hints|labels)(?: on (?:the|this) page)?", text):
        return "=hints on"
    if re.fullmatch(r"(?:hide|remove|clear|turn off|no more)(?: the)? (?:numbers|hints|labels)", text):
        return "=hints off"
    if re.fullmatch(r"(?:click|press|pick|choose|open) (?:number )?.+|number .+", text) and (n := number(text)) is not None:
        return f"=click {n}"
    return None


BROWSE_PROMPT = """You control the user's web browser (Chrome) for something they said. Choose actions from what's here.

The page in front: {title} ({url}){where}
What can be clicked, by number (* = on screen now):
{controls}
Headings: {headings}{video}
Tabs open (id: title, site; > = the one in front):
{tabs}

They said: {said}

Reply with JSON {{"actions": [...]}}, at most {most}, each one of:
{{"do": "click", "n": number}}   (a link, button or field above, by what it says or what was meant: "the second result" is the second result link, not the second thing on the page)
{{"do": "scroll", "how": "down" | "up" | "bit down" | "bit up" | "top" | "bottom"}}
{{"do": "back"}} {{"do": "forward"}} {{"do": "reload"}}
{{"do": "find", "text": words to look for on the page}}
{{"do": "go", "to": a site's address, or words to search for}}   (in this tab: "go to Kaggle competitions" is kaggle.com/competitions)
{{"do": "video", "what": "play" | "pause" | "forward" | "back" | "mute" | "unmute" | "faster" | "slower" | "normal speed" | "full screen" | "exit full screen" | "captions", "seconds": how far, for forward or back (10 minutes is 600)}}   (the video on the page)
{{"do": "hints", "on": true or false}}   (numbers on everything clickable)
{{"do": "tab", "id": tab id}}   (go to that tab)
{{"do": "close", "ids": [tab ids]}}   (close those tabs; "this tab" is the one in front)
{{"do": "reopen"}}   (the tab closed last)
{{"do": "new", "url": an address or ""}}
{{"do": "type", "text": text}}   (into the box in front)
{{"do": "say", "text": a short spoken answer}}   (to say what tabs are open, or that nothing here fits)
Prefer what's on screen now. Only what was asked."""
BROWSE_FORMAT = {"type": "object", "properties": {"actions": {"type": "array", "items": {"type": "object"}}},
                 "required": ["actions"]}


def described(seen: dict) -> dict:
    page = seen.get("page") or {}
    controls = page.get("controls") or []
    tabs = seen.get("tabs") or []
    return {
        "title": page.get("title") or "(no page)", "url": (page.get("url") or "")[:120],
        "where": f", scrolled {page['scrolled']}% down" if page.get("scrolled") is not None else "",
        "controls": "\n".join(f"{control['n']}{'*' if control.get('seen') else ''}: {control['label'][:80]}"
                              + (f" [{control['role']}]" if control.get("role") not in (None, "", "link") else "")
                              for control in controls[:150]) or "(nothing)",
        "headings": "; ".join(page.get("headings") or [])[:600] or "(none)",
        "video": (f"\nA video is on the page: {'paused' if page['video'].get('paused') else 'playing'}, at "
                  f"{_clock(page['video'].get('at'))} of {_clock(page['video'].get('length'))}"
                  f"{', muted' if page['video'].get('muted') else ''}" if page.get("video") else ""),
        "tabs": "\n".join(f"{'>' if tab.get('active') else ' '}{tab['id']}: {tab.get('title', '')[:70]}, {tab.get('site', '')}"
                          for tab in tabs[:40]) or "(none)",
    }


def _clock(seconds) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "?"
    hours, rest = divmod(total, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def _length(seconds: float) -> str:
    seconds = abs(int(seconds))
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60} minute{'s' if seconds != 60 else ''}"
    return f"{seconds} seconds"


def browse(wanted: str, config: Config, log=print, opened: list | None = None, confirmed: bool = False) -> tuple[bool, str]:
    """Do what was said in the browser: (done, what to say; "" when the page itself shows it, as a scroll does)."""
    opened = [] if opened is None else opened
    wanted = wanted if wanted.startswith("=") else quick(wanted) or wanted
    if wanted.startswith("="):  # said plainly: no model needed
        actions = [_direct(wanted[1:])]
    else:
        seen = ask({"do": "look"}, config)
        if seen.get("ok") is False:
            return _without_extension(wanted, seen, log)
        from . import llm

        try:
            reply = llm.quick(config.ollama, config.model,
                              BROWSE_PROMPT.format(**described(seen), said=wanted, most=MOST_ACTIONS),
                              timeout=60, format=BROWSE_FORMAT, options={"temperature": 0})
            actions = [action for action in json.loads(reply)["actions"] if isinstance(action, dict)][:MOST_ACTIONS]
        except (llm.OllamaError, ValueError, KeyError, TypeError):
            return False, "I couldn't work out what to do in the browser."
        log(f"browse {wanted!r}: {json.dumps(actions)}")
    said, done = [], False
    for action in actions:
        ok, line = _carry(action, config, log, opened, confirmed)
        done = done or ok
        if line:
            said.append(line)
        if not ok:
            break
    return done, " ".join(said)


def _direct(command: str) -> dict:
    """"click 14", "scroll bit down", "back", "hints on", or the yes to "click 37 Place order"."""
    word, _, rest = command.partition(" ")
    if word == "click":
        n, _, label = rest.partition(" ")
        return {"do": "click", "n": int(n), **({"label": label} if label else {})}
    if word == "scroll":
        return {"do": "scroll", "how": rest or "down"}
    if word in ("hints", "focus"):
        return {"do": word, "on": rest != "off"}
    return {"do": word}


def _carry(action: dict, config: Config, log, opened: list, confirmed: bool) -> tuple[bool, str]:
    kind = action.get("do")
    if kind == "say":
        return True, str(action.get("text", ""))
    if kind == "click" and not confirmed:
        seen = ask({"do": "label", "n": action.get("n")}, config)
        label = seen.get("label") or ""
        if seen.get("ok") is False:
            return False, _why(seen)
        if RISKY.search(label):  # it pays, sends or deletes: only on your yes, and only if it's still that button
            from .actions import line

            opened.append({"kind": "confirm", "name": f"Click {label}",
                           "thing": json.dumps([["browse", f"=click {action.get('n')} {label}"]])})
            return True, line(config, "sure", what=f"Click “{label}”")
    result = ask(action, config)
    if result.get("ok") is False:
        log(f"browse {kind}: {result.get('why')}")
        if kind == "video" and result.get("why") == "needs a key" and action.get("what") == "full screen":
            from . import desk

            return desk.keys("f"), ""  # the page wouldn't go full screen unasked: most players take F
        if (keys := KEYS.get((kind, action.get("how")))) and result.get("why") in ("not connected", "no page"):
            from . import desk

            return desk.keys(keys), ""  # no extension, or a page it can't reach: keys still scroll and go back
        return False, _why(result)
    if kind == "hints":
        if action.get("on", True):
            opened.append({"kind": "hints", "name": "numbers", "thing": ""})
            return True, "Numbers are on. Say the one you want."
        return True, ""
    if kind == "click":
        return True, f"Clicked {result.get('label') or 'it'}."
    if kind == "find":
        return True, "" if result.get("found") else f"I can't see “{action.get('text')}” on this page."
    if kind == "tab":
        return True, f"Here's {result.get('title') or 'that tab'}."
    if kind == "close":
        count = int(result.get("closed", 0))
        return True, f"Closed {count} tab{'s' if count != 1 else ''}."
    if kind == "reopen":
        return True, f"Reopened {result.get('title') or 'it'}."
    if kind == "type":
        return True, "Typed it."
    if kind == "focus":
        return True, ("Okay, I'm focused on Chrome. Say “stop focusing” when you're done." if action.get("on", True)
                      else "Okay, I'm not focused on Chrome any more.")
    if kind == "video":
        what = action.get("what", "")
        if what in ("forward", "back") and action.get("seconds"):
            return True, f"{'Skipped ahead' if what == 'forward' else 'Back'} {_length(float(action['seconds']))}."
        if what in ("faster", "slower", "normal speed"):
            return True, f"Playing at {result.get('speed', 1):g} times."
        return True, ""  # play, pause, mute: you hear it
    return True, ""  # a scroll, back, forward, reload or new tab: you see it happen


def _why(result: dict) -> str:
    why = str(result.get("why", ""))
    if why == "not connected" or "isn't answering" in why:
        return ("The Rigveda extension in Chrome isn't connected. Reload it at chrome://extensions, and check it has "
                "the token.")
    if why == "changed":
        return "The page changed, so I didn't click it."
    if why == "no page":
        return "There's no web page in front that I can work with."
    if why == "no video":
        return "I can't see a video on this page."
    if why == "no such number":
        return "There's no number like that on the page. Say “show numbers” to see them."
    return "That didn't work in the browser."


def _without_extension(wanted: str, seen: dict, log) -> tuple[bool, str]:
    log(f"browse {wanted!r}: {seen.get('why')}")
    return False, _why(seen)


KEYS = {("scroll", "down"): "pagedown", ("scroll", "up"): "pageup", ("scroll", "top"): "home",
        ("scroll", "bottom"): "end", ("scroll", "page down"): "pagedown", ("scroll", "bit down"): "down",
        ("scroll", "bit up"): "up", ("back", None): "alt+left", ("forward", None): "alt+right", ("reload", None): "f5"}
