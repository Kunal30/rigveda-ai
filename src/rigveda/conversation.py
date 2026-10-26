"""What you and Rigved said in the last few minutes, so a follow-up such as "and the deposit?" or "open it" makes sense.

Voice, the orb's Voice box and the orb's file chat all share it, so you can say something and follow up by typing.
It keeps the last few exchanges and forgets them after a quiet spell, so an old topic doesn't leak into a new one.
It lives in the runtime folder, which only you can read and which is emptied when you log out; the lasting record of
what was said by voice is voice-history.jsonl, which is only ever shown, never fed back to the model.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

PATH = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "rigveda-conversation.json"
TURNS = 6  # exchanges remembered
FRESH = 600  # seconds of quiet after which the conversation starts over
LONGEST = 1_200  # characters kept of each thing said; a long answer's gist is enough to follow up on

STANDALONE_PROMPT = """Rewrite the user's latest message so it makes sense on its own, without the conversation: put in \
what words such as "it", "that", "there" or "the second one" refer to, and turn a follow-up such as "and the deposit?" \
into a full question. If it already makes sense on its own, or starts a new topic, return it unchanged. Reply with the \
rewritten message only.

{conversation}

Latest message: {message}"""


def _read() -> list[dict]:
    try:
        turns = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [turn for turn in turns if isinstance(turn, dict)] if isinstance(turns, list) else []


def _update(change) -> None:
    """Change the turns under a lock: the voice listener and the server both write here."""
    PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(PATH.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        turns = change(recent())
        partial = PATH.with_suffix(".part")
        partial.write_text(json.dumps(turns), encoding="utf-8")
        os.chmod(partial, 0o600)
        partial.replace(PATH)


def recent() -> list[dict]:
    """The exchanges still part of the conversation, oldest first: none if it went quiet for FRESH seconds."""
    turns = _read()[-TURNS:]
    if not turns or time.time() - turns[-1].get("at", 0) > FRESH:
        return []
    return turns


def add(you: str, rigved: str, source: str, opened: dict | None = None, private: bool = False) -> None:
    """Remember an exchange. `opened` is what it opened ({"kind", "name", "thing"}), for "open it" later; `private`,
    that it was answered from your files, so it's never sent off this computer."""
    turn = {"at": time.time(), "you": you[:LONGEST], "rigved": rigved[:LONGEST], "from": source}
    if private:
        turn["private"] = True
    if opened:
        turn["opened"] = opened
    _update(lambda turns: (turns + [turn])[-TURNS:])


def forget() -> None:
    _update(lambda turns: [])


def last_opened() -> dict | None:
    """The last thing Rigved opened in this conversation."""
    for turn in reversed(recent()):
        if turn.get("opened"):
            return turn["opened"]
    return None


def messages() -> list[dict]:
    """The conversation as chat messages, to go before a new question."""
    said: list[dict] = []
    for turn in recent():
        said += [{"role": "user", "content": turn["you"]}, {"role": "assistant", "content": turn["rigved"]}]
    return said


def transcript(turns: list[dict] | None = None, public: bool = False) -> str:
    """The conversation as lines a model can read, or "" when there is none; with `public`, for Claude, without the
    turns answered from your files."""
    turns = recent() if turns is None else turns
    if public:
        turns = [turn for turn in turns if not turn.get("private") and turn.get("from") != "chat"]
    return "\n".join(f"User: {turn['you']}\nRigved: {turn['rigved']}" for turn in turns)


def standalone(message: str, host: str, model: str) -> str:
    """The message with what it leans on from the conversation filled in, for searching your files. Unchanged when
    there is no conversation going, or the model can't be reached."""
    from . import llm

    conversation = transcript()
    if not conversation:
        return message
    try:
        rewritten = llm.quick(host, model, STANDALONE_PROMPT.format(conversation=conversation, message=message),
                              timeout=30, options={"temperature": 0, "num_predict": 120})
    except llm.OllamaError:
        return message
    rewritten = rewritten.strip().strip('"').strip()
    return rewritten if 0 < len(rewritten) <= 4 * len(message) + 300 else message
