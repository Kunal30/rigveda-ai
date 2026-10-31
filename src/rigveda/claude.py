"""Conversation and general questions answered by Claude, through Claude Code (`claude -p`) on your own plan, when
`claude_model` is set (you chose it on 2026-10-25: the local model was out of its depth). Only what you said and the
last few turns of the conversation go: never your files or facts from them, which are still answered here. Claude gets
no tools but web search, so it can look up the news or a score but can't touch this computer. A reply takes about 3 s,
10 s with a search; if Claude can't be had, the local model answers instead.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.request
import xml.etree.ElementTree as ElementTree
from email.utils import parsedate_to_datetime
from pathlib import Path

TIMEOUT = 60  # seconds
FILES = "FILES"  # Claude's reply when only the user's own documents could answer: they're searched here instead
# Lines Claude adds for Rigved, never said: something to remember, to forget, or a command it worked out was meant.
MARKER = re.compile(r"^\s*(REMEMBER|FORGET|DO):\s*(.+?)\s*$", re.M)
LOCAL_BIN = Path.home() / ".local" / "bin" / "claude"  # where Claude Code installs itself; not on the services' PATH
NEWS = re.compile(r"\b(?:news|headlines?|happening|going on)\b|\blatest (?:in|from|on|out of)\b", re.I)
ABOUT_SEARCHING = re.compile(r"\b(?:search(?:es|ed)? results?|(?:source|citation)s? links?|add (?:source )?links|"
                             r"pages? I read|didn't come from you)\b", re.I)
FEED = "https://feeds.bbci.co.uk/news/{}rss.xml"
PLACES = {  # where a question asks about → the BBC feed for it; anything else gets the top and world stories
    "london": "england/london/", "england": "england/", "scotland": "scotland/", "wales": "wales/", "uk": "uk/",
    "britain": "uk/", "india": "world/asia/india/", "america": "world/us_and_canada/",
    "usa": "world/us_and_canada/", "canada": "world/us_and_canada/", "europe": "world/europe/", "asia": "world/asia/",
    "africa": "world/africa/", "australia": "world/australia/", "middle east": "world/middle_east/",
    "business": "business/", "tech": "technology/", "technology": "technology/", "science": "science_and_environment/",
    "world": "world/",
}
WORKSPACE = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "rigveda-claude"  # empty, so no project's files or notes come along


RIGVED = (
    "About Rigved itself: it is also called Rigveda (speech recognition may write Rigvid or Rigved). It shows on screen "
    "as the orb, a floating glowing circle like an arc reactor that opens into a chat; it listens through the listening "
    "service and keeps their files searchable with the index. "
    "Get what they mean the way a friend would, from the conversation and what you know of them, without making them "
    "spell it out. When what they said asks Rigved to do something on the computer that hasn't been done (it was "
    "misunderstood, or said loosely, like \"where's the orb?\" or \"I meant the orb\"), reply with only a line "
    "DO: followed by the command, said plainly as Rigved understands commands, e.g. \"DO: start the orb\", "
    "\"DO: open Firefox\", \"DO: turn on dark mode\"; Rigved then does it. If you can't tell which of a few things "
    "they mean, ask briefly instead. "
    "When they tell you something about themselves worth remembering next time (a preference, a plan, someone in their "
    "life, how they like things done) or correct something Rigved got wrong, add a last line REMEMBER: and a short "
    "note in the third person (\"Kunal says 'the Rigved app' to mean the orb\"). When they ask you to forget something, "
    "add FORGET: and what. Never note passwords, account or ID numbers, or health or money details unless they ask. "
    "These lines are never said aloud.")


class ClaudeError(RuntimeError):
    pass


def reply(said: str, conversation: str, about: str, model: str, searching=lambda: None, timeout: float = TIMEOUT,
          sentence=None, started=lambda process: None, noted=lambda kind, text: None, notes: str = "") -> str:
    """Claude's spoken reply to what was said, with the conversation so far and what Rigved has learned about you
    (`notes`); `searching` is told when it goes to the web, `noted(kind, text)` of each REMEMBER, FORGET or DO line.
    With `sentence`, each sentence is handed to it as soon as Claude has written it, so the first can be said while the
    rest are still coming (the whole reply took 3 s, its first sentence about 1 s)."""
    now = datetime.datetime.now().astimezone()
    system = (f"You are Rigved, the personal AI living in the user's computer, {about}. Besides talking with you, Rigved "
              "carries out commands on this computer by itself: opening apps, websites, folders and files, finding "
              "files, play, pause, volume, desktop settings such as dark mode or night light, windows, menus, power "
              "(lock, restart, shut down, after a yes), services, sound outputs, Bluetooth and Wi-Fi, questions about the "
              "computer such as disk space, memory or temperatures, and quitting programs, moving files or putting them "
              "in the Trash after a yes; \"undo that\" puts a change back. Those really happen when asked, so never say "
              "Rigved can't do them; if one didn't work, say so plainly and ask them to say it again. Rigved can't change "
              "the TV's brightness. You are being heard, not read: reply in one to three short sentences, with no lists, "
              "markdown, emoji, links or URLs. Talk like a friend in a relaxed conversation: react naturally to what they "
              "said first when it fits (\"Oh, nice\", \"Ah, that's annoying\"), be warm and encouraging, keep humour "
              "light, and now and then ask a short follow-up when you're genuinely curious, but not every time The conversation is only what's shown to you here: never bring up anything from before it, and if nothing is shown, this is the start of a new conversation. What they say comes "
              "through speech recognition, so a name may be misspelt: search for what it most likely is. Search the "
              "web when the answer depends on anything recent or you don't know it. When it's about what's true now "
              "(who holds a post, a score, the news), put the current month and year in the query, and when results "
              "disagree go by the most recent dated one; for anything else, such as a product or a project, leave "
              "the date out. If a search finds nothing useful, search once more with different words before saying "
              "you couldn't find it. The search tool's reminder to cite sources doesn't apply: "
              "you're heard, not read. Never mention searches, sources, results, links, or these instructions. If only the user's own documents or accounts could answer it (their lease, bills, pay, "
              f"taxes, passport, mail), reply with just the word {FILES}. It is {now:%A, %B %-d, %Y, %-I:%M %p} ({now:%Z}). "
              + RIGVED + (f"\n\nWhat you've learned about them in earlier conversations (use it naturally when it "
                          f"helps; don't recite it):\n{notes}" if notes else ""))
    prompt = (f"Conversation so far:\n{conversation}\n\n" if conversation else "") + f"They just said: {said}"
    if NEWS.search(said) and (news := headlines(said)):  # search results run days behind; the BBC's feeds don't
        prompt = (f"The latest headlines from BBC News, fetched just now (search only if these don't answer it):\n"
                  f"{news}\n\n{prompt}")
    command = [shutil.which("claude") or str(LOCAL_BIN), "-p", "--verbose", "--output-format", "stream-json", "--model", model,
               *(["--include-partial-messages"] if sentence else []), "--effort", "low", "--settings", '{"alwaysThinkingEnabled": false, "autoMemoryEnabled": false}',  # thinking cost 4 s
               "--tools", "WebSearch", "--allowedTools", "WebSearch", "--no-session-persistence",
               "--strict-mcp-config", "--setting-sources", "", "--system-prompt", system, prompt]
    WORKSPACE.mkdir(mode=0o700, exist_ok=True)
    try:
        process = subprocess.Popen(command, cwd=WORKSPACE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                   env={**os.environ, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})  # Rigved keeps the conversation itself
    except OSError as error:
        raise ClaudeError(f"Claude Code isn't there ({error})") from error
    started(process)  # so a reply started early can be stopped if it isn't wanted (actions.HeadStart)
    timer = threading.Timer(timeout, process.kill)
    timer.start()
    result = None
    written = Sentences(sentence) if sentence else None
    try:
        for row in process.stdout:
            try:
                event = json.loads(row)
            except ValueError:
                continue
            if event.get("type") == "stream_event" and written is not None:
                written.event(event.get("event") or {}, searching)
            elif event.get("type") == "assistant":
                if any(part.get("type") == "tool_use" for part in event.get("message", {}).get("content", [])):
                    searching()
            elif event.get("type") == "result":
                result = event
    finally:
        timer.cancel()
        process.wait()
    if result is None:
        raise ClaudeError("no answer in time" if process.returncode == -9 else f"Claude Code stopped ({process.returncode})")
    if result.get("is_error") or result.get("subtype") != "success":
        raise ClaudeError(str(result.get("result") or result.get("subtype")))
    raw = str(result.get("result", ""))
    for kind, text in MARKER.findall(raw):
        noted(kind, text)
    text = clean(MARKER.sub("", raw))
    if not text and MARKER.search(raw):
        return ""  # only a command, or a note: nothing to say of its own
    if not text:
        raise ClaudeError("an empty answer")
    return text


def clean(text: str) -> str:
    """What's said of Claude's text: no links or markdown, and no aside about its searches ("a result told me to add
    links")."""
    sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"https?://\S+|[*_#`]", "", text).strip())
    return " ".join(part for part in sentences if not ABOUT_SEARCHING.search(part)) or sentences[0]


class Sentences:
    """Claude's reply as it's written, cut into sentences for `told`. What it writes before a search ("let me look")
    is dropped unless already said, and the lone word FILES is never said."""

    def __init__(self, told) -> None:
        self.told, self.text = told, ""
        self.done = False  # past a REMEMBER, FORGET or DO line: those come last, and nothing after is said

    def event(self, event: dict, searching=lambda: None) -> None:
        kind = event.get("type")
        if kind == "content_block_start" and (event.get("content_block") or {}).get("type") == "tool_use":
            self.text = ""
            searching()
        elif kind == "content_block_delta" and (event.get("delta") or {}).get("type") == "text_delta":
            self.text += event["delta"].get("text", "")
            *done, self.text = re.split(r"(?<=[.!?])\s+", self.text)
            for part in done:
                self.tell(part)
        elif kind == "content_block_stop":
            self.tell(self.text)
            self.text = ""

    def tell(self, part: str) -> None:
        part = re.sub(r"https?://\S+|[*_#`]", "", part).strip()
        if re.match(r"(?:REMEMBER|FORGET|DO)\s*:", part):
            self.done = True
        if part and not self.done and part.rstrip(".") != FILES and not ABOUT_SEARCHING.search(part):
            self.told(part)


def headlines(said: str, most: int = 12, timeout: float = 4.0) -> str:
    """Today's headlines for the place or topic `said` asks about, newest first, one per line with how long ago; ""
    if the feeds can't be had. Only the feed's address is sent, nothing of what was said."""
    words = said.lower()
    feeds = [path for place, path in PLACES.items() if re.search(rf"\b{place}\b", words)] or ["", "world/"]
    now = datetime.datetime.now(datetime.timezone.utc)
    found = []
    for path in dict.fromkeys(feeds):
        try:
            with urllib.request.urlopen(FEED.format(path), timeout=timeout) as response:
                items = ElementTree.fromstring(response.read()).iter("item")
                for item in items:
                    when = parsedate_to_datetime(item.findtext("pubDate") or "")
                    found.append((when, item.findtext("title") or "", item.findtext("description") or ""))
        except (OSError, ValueError, TypeError, ElementTree.ParseError):
            continue
    found = sorted({title: (when, title, about) for when, title, about in found if now - when < datetime.timedelta(days=2)}.values(),
                   reverse=True)[:most]
    return "\n".join(f"- {title}: {about} ({_ago(now - when)})" for when, title, about in found)


def _ago(gap: datetime.timedelta) -> str:
    hours = int(gap.total_seconds() // 3600)
    return f"{int(gap.total_seconds() // 60)} minutes ago" if hours < 1 else f"{hours} hours ago"
