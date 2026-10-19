"""Saved emails (.eml): the headers, the message, and any attachment Rigveda can read, such as a PDF statement."""

from __future__ import annotations

import email
import email.policy
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path

from . import Segment

MAX_ATTACHMENT = 50 * 2**20


class _Text(HTMLParser):
    """Visible text of an HTML email: scripts and styles dropped, blocks on their own lines."""

    BLOCKS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "section"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs) -> None:
        if tag in ("script", "style", "head"):
            self.hidden += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag) -> None:
        if tag in ("script", "style", "head"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data) -> None:
        if not self.hidden:
            self.parts.append(data)


def html_text(markup: str) -> str:
    parser = _Text()
    parser.feed(markup)
    text = "".join(parser.parts)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t\xa0]+", " ", text)).strip()


def read_email(path: Path, config) -> list[Segment]:
    from . import extract_any

    with path.open("rb") as file:
        message = email.message_from_binary_file(file, policy=email.policy.default)
    headers = "\n".join(f"{name}: {message[name]}" for name in ("From", "To", "Cc", "Date", "Subject") if message[name])
    body = message.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        try:
            content = body.get_content()
        except (LookupError, ValueError):  # unknown or broken character set
            content = body.get_payload(decode=True).decode("utf-8", "replace")
        text = html_text(content) if body.get_content_type() == "text/html" else content
    segments = [Segment(f"{headers}\n\n{text}".strip(), "email")]
    with tempfile.TemporaryDirectory(prefix="rigveda-mail-") as folder:
        for number, part in enumerate(message.iter_attachments()):
            name = Path(part.get_filename() or "").name
            data = part.get_payload(decode=True) or b""
            if not name or not data or len(data) > MAX_ATTACHMENT:
                continue
            saved = Path(folder) / f"{number}-{name}"
            saved.write_bytes(data)
            try:
                _, found = extract_any(saved, config, fallback=False)
            except Exception:  # a broken attachment doesn't lose the email
                continue
            segments += [Segment(segment.text, f"attachment {name}" + (f", {segment.location}" if segment.location else ""))
                         for segment in found]
    return segments


EXTRACTORS = [(".eml", "email", read_email)]
