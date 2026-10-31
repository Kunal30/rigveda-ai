import re
import zlib
from pathlib import Path

import pytest

from rigveda import browser, catalog, conversation, desk, llm, machine, names, notes
from rigveda.config import Config


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read or write the real ~/.config/rigveda during tests."""
    monkeypatch.setenv("RIGVEDA_CONFIG", str(tmp_path / "config" / "config.toml"))
    monkeypatch.setattr(conversation, "PATH", tmp_path / "conversation.json")
    monkeypatch.setattr(notes, "PATH", tmp_path / "notes.json")  # never what Rigved has learned about you
    monkeypatch.setattr(names, "ROOTS", (tmp_path / "home",))  # never your real files: a home of the test's own
    monkeypatch.setattr(names, "_read", (0.0, []))


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


@pytest.fixture(autouse=True)
def no_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never look at or press anything on the real desktop."""
    monkeypatch.setattr(desk, "ask", lambda request, timeout=15.0: {"ok": False, "why": "no desktop in tests"})
    monkeypatch.setattr(browser, "ask", lambda command, config=None: {"ok": False, "why": "not connected"})  # nor your Chrome


@pytest.fixture(autouse=True)
def no_real_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Tests never read or change the real desktop settings, nor use the real catalog: settings live in a dict."""
    monkeypatch.setattr(catalog, "PATH", tmp_path / "catalog.json")
    monkeypatch.setattr(catalog, "VECTORS", tmp_path / "catalog.npy")
    monkeypatch.setattr(catalog, "LAST", tmp_path / "settings-changed.json")
    monkeypatch.setattr(catalog, "installed", lambda: set())
    monkeypatch.setattr(catalog, "apps", lambda: [])
    catalog._loaded.clear()
    values: dict = {}

    def gsettings(*arguments: str) -> str | None:
        if arguments[0] == "get":
            return values.get(arguments[1:3], "false")
        values[arguments[1:3]] = arguments[3]
        return ""

    monkeypatch.setattr(catalog, "_gsettings", gsettings)
    return values


@pytest.fixture(autouse=True)
def no_real_changes(monkeypatch: pytest.MonkeyPatch) -> list:
    """Tests never start, stop or switch anything for real: what would have run is kept in a list."""
    ran: list = []
    monkeypatch.setattr(machine, "_act", lambda arguments, seconds=120: ran.append(arguments) or (True, ""))
    return ran
