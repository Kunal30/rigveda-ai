import re
import zlib
from pathlib import Path

import pytest

from rigveda import conversation, llm
from rigveda.config import Config


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read or write the real ~/.config/rigveda during tests."""
    monkeypatch.setenv("RIGVEDA_CONFIG", str(tmp_path / "config" / "config.toml"))
    monkeypatch.setattr(conversation, "PATH", tmp_path / "conversation.json")


def fake_embed(host: str, model: str, texts: list[str]) -> list[list[float]]:
    """Deterministic stand-in for an embedding model: bag of crudely stemmed words.

    "monthly" and "month" share a stem, so it can show paraphrase matching
    that exact-word search misses.
    """
    vectors = []
    for text in texts:
        vector = [0.0] * 4096
        for word in re.findall(r"[a-z]+", text.lower()):
            stem = re.sub(r"(ly|s)$", "", word)
            vector[zlib.crc32(stem.encode()) % 4096] += 1.0
        vectors.append(vector)
    return vectors


@pytest.fixture(autouse=True)
def offline_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never call a real Ollama."""
    monkeypatch.setattr(llm, "embed", fake_embed)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(database=str(tmp_path / "index.db"))


@pytest.fixture(autouse=True)
def no_fact_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fact extraction finds nothing unless a test supplies its own fake model."""
    monkeypatch.setattr(llm, "extract_facts", lambda host, model, prompt: [])
