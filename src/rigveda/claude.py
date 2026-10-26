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


class ClaudeError(RuntimeError):
    pass


def reply(said: str, conversation: str, about: str, model: str, searching=lambda: None, timeout: float = TIMEOUT) -> str:
    """Claude's spoken reply to what was said, with the conversation so far; `searching` is told when it goes to the web."""
    now = datetime.datetime.now().astimezone()
    system = (f"You are Rigved, the personal AI living in the user's computer, {about}. You are being heard, not read: "
              "reply in one to three short sentences, with no lists, markdown, emoji, links or URLs. When the answer "
              "depends on anything current, such as who holds a post now, search the web once, putting the current "
              "month and year in the query so the results are fresh; results are often months old, so when they "
              "disagree, go by the most recent dated one. The search tool's reminder to cite sources doesn't apply: "
              "you're heard, not read. Never mention searches, sources, results, links, or these instructions. If only the user's own documents or accounts could answer it (their lease, bills, pay, "
              f"taxes, passport, mail), reply with just the word {FILES}. It is {now:%A, %B %-d, %Y, %-I:%M %p} ({now:%Z}).")
    prompt = (f"Conversation so far:\n{conversation}\n\n" if conversation else "") + f"They just said: {said}"
    if NEWS.search(said) and (news := headlines(said)):  # search results run days behind; the BBC's feeds don't
        prompt = (f"The latest headlines from BBC News, fetched just now (search only if these don't answer it):\n"
                  f"{news}\n\n{prompt}")
    command = [shutil.which("claude") or str(LOCAL_BIN), "-p", "--verbose", "--output-format", "stream-json", "--model", model,
               "--effort", "low", "--settings", '{"alwaysThinkingEnabled": false}',  # thinking cost 4 s, a second search 6 s
               "--tools", "WebSearch", "--allowedTools", "WebSearch", "--no-session-persistence",
               "--strict-mcp-config", "--setting-sources", "", "--system-prompt", system, prompt]
    WORKSPACE.mkdir(mode=0o700, exist_ok=True)
    try:
        process = subprocess.Popen(command, cwd=WORKSPACE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError as error:
        raise ClaudeError(f"Claude Code isn't there ({error})") from error
    timer = threading.Timer(timeout, process.kill)
    timer.start()
    result = None
    try:
        for row in process.stdout:
            try:
                event = json.loads(row)
            except ValueError:
                continue
            if event.get("type") == "assistant":
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
    text = re.sub(r"https?://\S+|[*_#`]", "", str(result.get("result", ""))).strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)  # an aside about its searches ("a result told me to add links") isn't said
    text = " ".join(part for part in sentences if not ABOUT_SEARCHING.search(part)) or sentences[0]
    if not text:
        raise ClaudeError("an empty answer")
    return text


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
