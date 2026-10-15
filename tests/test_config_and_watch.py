from pathlib import Path

from rigveda import config as configuration
from rigveda.search import search
from rigveda.watch import process


def test_config_round_trip() -> None:
    config = configuration.Config(folders=["/home/me/Documents", 'C:\\odd "name"'])
    configuration.save(config)
    assert configuration.load() == config


def test_watch_batch_indexes_and_removes(tmp_path: Path, config) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    note = root / "note.txt"
    note.write_text("visa appointment on Monday")
    process(config.database_path, {note}, [root], config, log=lambda message: None)
    assert search(config.database_path, "visa", 3)
    note.unlink()
    process(config.database_path, {note}, [root], config, log=lambda message: None)
    assert search(config.database_path, "visa", 3) == []


def test_watch_ignores_hidden_and_outside_paths(tmp_path: Path, config) -> None:
    root = tmp_path / "docs"
    (root / ".cache").mkdir(parents=True)
    hidden = root / ".cache" / "secret.txt"
    hidden.write_text("hidden secret")
    outside = tmp_path / "other.txt"
    outside.write_text("outside secret")
    process(config.database_path, {hidden, outside}, [root], config, log=lambda message: None)
    assert search(config.database_path, "secret", 3) == []
