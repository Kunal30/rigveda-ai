"""Minimal client for a local Ollama server."""

from __future__ import annotations

import base64
import json
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class OllamaError(RuntimeError):
    pass


def chat(host: str, model: str, messages: list[dict], timeout: float = 120, **options) -> str:
    """One reply from the model. Extra options (format, think, options) are passed to Ollama as is."""
    data = json.dumps({"model": model, "stream": False, "messages": messages, **options}).encode()
    request = Request(host.rstrip("/") + "/api/chat", data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)["message"]["content"]
    except HTTPError as error:  # Ollama explains what was wrong in the body, e.g. "model does not support thinking"
        detail = error.read().decode("utf-8", "replace").strip()
        raise OllamaError(f"Ollama at {host} refused the request for model {model}: {error.code} {detail}") from error
    except (URLError, TimeoutError, KeyError, json.JSONDecodeError) as error:
        raise OllamaError(f"Could not reach Ollama at {host} with model {model}: {error}") from error


def embed(host: str, model: str, texts: list[str]) -> list[list[float]]:
    data = json.dumps({"model": model, "input": texts}).encode()
    request = Request(host.rstrip("/") + "/api/embed", data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=300) as response:
            return json.load(response)["embeddings"]
    except (URLError, TimeoutError, KeyError, json.JSONDecodeError) as error:
        raise OllamaError(f"Could not get embeddings from Ollama at {host} with model {model}: {error}") from error


IMAGE_PROMPT = (
    "Transcribe all readable text in this image exactly as written, preserving line breaks. "
    "Then add one line starting with 'Description:' that describes the image in one to three sentences. "
    "If there is no text, give only the Description line. Do not invent text."
)


def image_text(host: str, model: str, image: bytes, prompt: str = IMAGE_PROMPT) -> str:
    message = {"role": "user", "content": prompt, "images": [base64.b64encode(image).decode()]}
    return chat(host, model, [message], timeout=300)


FACTS_FORMAT = {
    "type": "object",
    "properties": {"facts": {"type": "array", "items": {
        "type": "object",
        "properties": {"key": {"type": "string"}, "value": {"type": "string"}, "evidence": {"type": "string"}},
        "required": ["key", "value", "evidence"],
    }}},
    "required": ["facts"],
}


def extract_facts(host: str, model: str, prompt: str) -> list[dict]:
    """Facts as {key, value, evidence} dicts. The reply is constrained to JSON; no thinking, for speed."""
    messages = [{"role": "user", "content": prompt}]
    try:
        reply = chat(host, model, messages, timeout=300, format=FACTS_FORMAT, think=False, options={"temperature": 0})
    except OllamaError as error:
        if "think" not in str(error).lower():
            raise
        reply = chat(host, model, messages, timeout=300, format=FACTS_FORMAT, options={"temperature": 0})  # model can't think
    try:
        facts = json.loads(reply).get("facts", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    return [fact for fact in facts if isinstance(fact, dict)]


def ask(question: str, sources: Iterable, model: str, host: str) -> str:
    from .search import cite

    context = "\n\n".join(f"SOURCE: {cite(row)}\n{row['text']}" for row in sources)
    prompt = ("Answer only from this local-file context; say when it is insufficient and cite paths. "
              "If several different values could answer the question, list each one with what it applies to "
              "instead of picking one.\n\n" + context + "\n\nQUESTION: " + question)
    return chat(host, model, [{"role": "user", "content": prompt}])
