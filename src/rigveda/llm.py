"""Minimal client for a local Ollama server."""

from __future__ import annotations

import base64
import json
from typing import Iterable
from urllib.error import URLError
from urllib.request import Request, urlopen


class OllamaError(RuntimeError):
    pass


def chat(host: str, model: str, messages: list[dict], timeout: float = 120) -> str:
    data = json.dumps({"model": model, "stream": False, "messages": messages}).encode()
    request = Request(host.rstrip("/") + "/api/chat", data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)["message"]["content"]
    except (URLError, TimeoutError, KeyError, json.JSONDecodeError) as error:
        raise OllamaError(f"Could not reach Ollama at {host} with model {model}: {error}") from error


IMAGE_PROMPT = (
    "Transcribe all readable text in this image exactly as written, preserving line breaks. "
    "Then add one line starting with 'Description:' that describes the image in one to three sentences. "
    "If there is no text, give only the Description line. Do not invent text."
)


def image_text(host: str, model: str, image: bytes, prompt: str = IMAGE_PROMPT) -> str:
    message = {"role": "user", "content": prompt, "images": [base64.b64encode(image).decode()]}
    return chat(host, model, [message], timeout=300)


def ask(question: str, sources: Iterable, model: str, host: str) -> str:
    from .search import cite

    context = "\n\n".join(f"SOURCE: {cite(row)}\n{row['text']}" for row in sources)
    prompt = "Answer only from this local-file context; say when it is insufficient and cite paths.\n\n" + context + "\n\nQUESTION: " + question
    return chat(host, model, [{"role": "user", "content": prompt}])
