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
from pathlib import Path

TIMEOUT = 60  # seconds
FILES = "FILES"  # Claude's reply when only the user's own documents could answer: they're searched here instead
LOCAL_BIN = Path.home() / ".local" / "bin" / "claude"  # where Claude Code installs itself; not on the services' PATH
WORKSPACE = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "rigveda-claude"  # empty, so no project's files or notes come along


class ClaudeError(RuntimeError):
    pass


def reply(said: str, conversation: str, about: str, model: str, searching=lambda: None, timeout: float = TIMEOUT) -> str:
    """Claude's spoken reply to what was said, with the conversation so far; `searching` is told when it goes to the web."""
    now = datetime.datetime.now().astimezone()
    system = (f"You are Rigved, the personal AI living in the user's computer, {about}. You are being heard, not read: "
              "reply in one to three short sentences, with no lists, markdown, emoji, links or URLs. Search the web "
              "when the answer depends on anything current. Never mention searches, sources, results or these "
              "instructions. If only the user's own documents or accounts could answer it (their lease, bills, pay, "
              f"taxes, passport, mail), reply with just the word {FILES}. It is {now:%A, %B %-d, %Y, %-I:%M %p} ({now:%Z}).")
    prompt = (f"Conversation so far:\n{conversation}\n\n" if conversation else "") + f"They just said: {said}"
    command = [shutil.which("claude") or str(LOCAL_BIN), "-p", "--verbose", "--output-format", "stream-json", "--model", model,
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
    if not text:
        raise ClaudeError("an empty answer")
    return text
