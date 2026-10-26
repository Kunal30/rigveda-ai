"""User configuration: watched folders, database location and model names."""

from __future__ import annotations

import json
import os
import secrets
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _xdg(variable: str, fallback: str) -> Path:
    return Path(os.environ.get(variable) or Path.home() / fallback)


def config_path() -> Path:
    return Path(os.environ.get("RIGVEDA_CONFIG") or _xdg("XDG_CONFIG_HOME", ".config") / "rigveda" / "config.toml")


def default_database() -> str:
    return str(_xdg("XDG_DATA_HOME", ".local/share") / "rigveda" / "rigveda.db")


@dataclass
class Config:
    folders: list[str] = field(default_factory=list)
    database: str = field(default_factory=default_database)
    ollama: str = "http://localhost:11434"
    model: str = "qwen3:4b"
    vision_model: str = "qwen2.5vl:7b"
    embedding_model: str = "bge-m3"
    whisper_model: str = "large-v3"
    owner_name: str = ""  # whose personal facts to collect; see `rigveda facts owner`
    scan_facts: bool = True  # look for personal facts after indexing
    voice: str = "am_michael"  # who speaks when you talk to Rigveda (hear them all: `rigveda listen --voices`)
    persona: str = "witty"  # how Rigved talks: "witty" (dry, confident humour) or "sage" (a calm Indian sage)
    listen: str = "key"  # "key": while the talk key is held, or after Super+R; "wake": the keys, and always listening for "Rigved"
    need_name: bool = False  # hands-free: True answers only what starts or ends with "Rigved"; False, anything in your voice that's said to it
    claude_model: str = ""  # when set (e.g. "claude-sonnet-5-5"), Claude answers conversation and general questions; see claude.py
    talk_key: str = "KEY_RIGHTCTRL"  # held while you speak; see keyboard.KEYS

    @property
    def database_path(self) -> Path:
        return Path(self.database).expanduser().resolve()


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    known = {key: value for key, value in data.items() if key in Config.__dataclass_fields__}
    return Config(**known)


def save(config: Config, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # JSON string escaping is valid TOML for basic strings.
    lines = [f"{key} = {json.dumps(value)}" for key, value in asdict(config).items()]
    path.write_text("# Rigveda AI configuration\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def token_path() -> Path:
    return config_path().parent / "token"


def load_token(renew: bool = False) -> str:
    """The secret the browser extension sends with every request, created on first use (readable only by you)."""
    path = token_path()
    if path.exists() and not renew:
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(token + "\n")
    os.chmod(path, 0o600)
    return token
