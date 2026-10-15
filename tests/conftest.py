from pathlib import Path

import pytest

from rigveda.config import Config


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read or write the real ~/.config/rigveda during tests."""
    monkeypatch.setenv("RIGVEDA_CONFIG", str(tmp_path / "config" / "config.toml"))


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(database=str(tmp_path / "index.db"))
