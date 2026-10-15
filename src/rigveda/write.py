"""Writing help: draft an answer for a text box, or continue what you are writing, from your own files."""

from __future__ import annotations

import re
from pathlib import Path

from . import facts, llm
from .config import Config
from .search import cite, search
from .store import connect

SOURCES = 6  # passages from your files given to the model
# Film, TV and other recordings are other people's words: drawing on them made "my most recent role" into a
# therapist's job. Writing help reads documents, images and notes only.
SKIP_KINDS = facts.SKIP_KINDS
# Likewise books, papers and manuals: a 14B model told "use these only as background" still wrote a sensors
# handbook up as the writer's job. With your name set (`rigveda facts owner`), only passages that name you are used.
BEFORE = 3000  # characters of your text before the cursor that the model sees
AFTER = 600
# Facts that help a letter or an answer sound like you. ID numbers and dates of birth are never offered.
PROFILE_KEYS = ["full_name", "email", "phone", "address.city", "address.state", "address.country",
                "employer", "job_title", "school", "degree"]


def field_question(field: dict) -> str:
    """What the text box asks, in the page's own words."""
    parts = [str(field.get(name, "")).strip() for name in ("label", "ariaLabel", "placeholder", "nearby")]
    return " / ".join(dict.fromkeys(part for part in parts if part))[:400]


def search_query(request: dict) -> str:
    """Search for what the text is about: your instruction, the box's question and the last thing you wrote."""
    before = str(request.get("before", ""))
    recent = re.split(r"(?<=[.!?\n])\s+", before.strip()[-600:])
    return " ".join(part for part in [str(request.get("instruction", "")), field_question(request.get("field") or {}),
                                      " ".join(recent[-2:])] if part.strip())[:1500]


def profile(con) -> list[str]:
    approved = facts.listing(con, "approved")
    lines = []
    for key in PROFILE_KEYS:
        values = [fact["value"] for fact in approved if fact["key"] == key][:2]
        if values:
            lines.append(f"{facts.KEYS[key]}: {' / '.join(values)}")
    return lines


def secrets(con) -> list[str]:
    """Approved ID numbers, so they can be kept out of anything written for a web page."""
    return [fact["value"] for fact in facts.listing(con, "approved") if fact["key"] in facts.SENSITIVE]


def build_prompt(request: dict, sources: list, about_you: list[str], config: Config) -> str:
    owner = config.owner_name
    field = request.get("field") or {}
    before, after = str(request.get("before", ""))[-BEFORE:], str(request.get("after", ""))[:AFTER]
    instruction = str(request.get("instruction", "")).strip()
    limit = int(field.get("maxLength") or 0)
    page = " · ".join(part for part in (str(request.get("title", "")), str(request.get("site", ""))) if part)
    context = "\n\n".join(f"[{i + 1}] {cite(row)}\n{row['text']}" for i, row in enumerate(sources)) or "(nothing found)"
    lines = [
        f"You help {owner or 'the user'} write text in their own voice, in the first person, using only what their files say.",
        "Rules:",
        "- Anything about the writer's own life, work, history or plans must come from the facts or passages below. "
        "Many passages will be unrelated to what is being written; ignore those.",
        "- Never invent employers, job titles, dates, numbers, names, events or achievements. Where the text needs a "
        "detail you were not given, write a short placeholder in square brackets, such as [your role], for the writer to fill in.",
        "- If the facts and passages say nothing about the subject, do not make up a situation (such as looking for work) "
        "or vague claims about it: write a brief outline the writer can complete, with [placeholders] for the specifics.",
        "- Never include ID numbers such as passport, Social Security or license numbers.",
        "- In \"text\", give only the words to insert: no preamble, quotation marks or notes. In \"used\", list the "
        "numbers of the passages you actually drew on (none: an empty list).",
        "",
        "Facts about the writer:", *(about_you or ["(none approved yet)"]), "",
        "Passages from the writer's files:", context, "",
    ]
    if page:
        lines.append(f"Web page: {page}")
    if field_question(field):
        lines.append(f"The text box asks: {field_question(field)}")
    if limit:
        lines.append(f"The text box allows at most {limit} characters.")
    if instruction:
        lines.append(f"What the writer wants: {instruction}")
    if before.strip():
        lines += ["", "The writer's text so far:", "<<<", before + "▮" + after, ">>>",
                  "Continue at ▮ with one to three sentences that follow on naturally, in the same language and tone. "
                  "Start exactly where ▮ is, mid-sentence if the text stops mid-sentence, and do not repeat any words "
                  "that come before it."]
    else:
        lines += ["", "Write a complete answer for this text box: concise, specific and grounded, usually one short "
                  "paragraph unless the box or the writer asks for more."]
    return "\n".join(lines)


def overlap(before: str, text: str) -> int:
    """How many characters at the start of `text` repeat the last words of `before` ("…job I" + "I work")."""
    tail = re.findall(r"\S+", before)[-12:]
    for n in range(len(tail), 0, -1):
        words = [re.escape(word) for word in tail[-n:]]
        found = re.match(r"\s*" + r"\s+".join(words) + r"(?=\s|$)", text, flags=re.I)
        if found:
            return found.end()
    return 0


def tidy(text: str, request: dict, hidden: list[str]) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^(here(’s|'s| is)|sure\b|certainly\b)[^\n]*:\s*\n+", "", text, flags=re.I).strip()  # "Here is a draft:"
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'“”":
        text = text[1:-1].strip()
    text = text.replace("▮", "")
    # Keep ID numbers out, whatever the model did: drop any sentence that contains one.
    squash = lambda value: re.sub(r"[^0-9a-z]", "", value.lower())
    numbers = [squash(value) for value in hidden if len(squash(value)) >= 5]
    if numbers:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        text = " ".join(s for s in sentences if not any(number in squash(s) for number in numbers)).strip()
    before = str(request.get("before", ""))
    if before.strip():
        text = text[overlap(before, text):].lstrip()
        if text and not before[-1:].isspace() and text[0] not in ".,;:!?)'’-":
            text = " " + text
    limit = int((request.get("field") or {}).get("maxLength") or 0)
    room = limit - len(before) - len(str(request.get("after", ""))) if limit > 0 else 0
    if limit > 0 and len(text) > room:
        cut = text[:max(room, 0)]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind(".\n"))
        text = cut[:end + 1] if end > 0 else cut
    return text


def write(db: Path, config: Config, request: dict) -> dict:
    """A draft or continuation for one text box, with the files it drew on."""
    found = search(db, search_query(request) or "about me", 50, config)
    terms = facts.owner_terms(config)
    con = connect(db)
    try:
        about_you, hidden = profile(con), secrets(con)
        paths = list({row["path"] for row in found})
        kinds = dict(con.execute(f"SELECT path, kind FROM documents WHERE path IN ({','.join('?' * len(paths))})", paths))
        sources = [row for row in found if kinds.get(row["path"]) not in SKIP_KINDS
                   and (not terms or facts.candidate(row["text"], terms))][:SOURCES]
    finally:
        con.close()
    prompt = build_prompt(request, sources, about_you, config)
    reply = llm.write_text(config.ollama, config.model, prompt)
    text = tidy(reply["text"], request, hidden)
    used = {n for n in reply["used"] if isinstance(n, int) and 1 <= n <= len(sources)}
    seen, cited = set(), []
    for row in (row for i, row in enumerate(sources, 1) if i in used):  # only the files the text drew on
        if row["path"] not in seen:
            seen.add(row["path"])
            cited.append({"path": row["path"], "name": row["path"].rsplit("/", 1)[-1], "cite": cite(row)})
    return {"text": text, "mode": "continue" if str(request.get("before", "")).strip() else "draft", "sources": cited}
