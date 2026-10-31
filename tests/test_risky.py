import json
import os
from pathlib import Path

import pytest

from rigveda import actions, catalog, conversation, llm, machine, risky
from rigveda.actions import Step
from rigveda.config import Config

QUIET = lambda message: None  # noqa: E731


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A home folder of the test's own, with a film, its trailer and an empty Documents."""
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    (home / "Documents").mkdir()
    (home / ".config").mkdir()
    (home / ".config" / "secret.txt").write_text("x")
    (home / "Downloads" / "Mirzapur S01E01.mkv").write_text("film")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def seen() -> dict:
    return {"system": [], "user": [], "sound": {"sinks": [], "sources": []}, "connections": [], "bluetooth": {}, "power": "",
            "apps": {}, "processes": [{"pid": 4242, "name": "firefox", "memory": 9.0, "cpu": 3.0},
                                      {"pid": 4243, "name": "firefox", "memory": 2.0, "cpu": 0.0},
                                      {"pid": 77, "name": "gnome-shell", "memory": 3.0, "cpu": 5.0}]}


def test_quitting_asks_first_and_then_ends_exactly_those(monkeypatch, seen: dict) -> None:
    ended = []
    monkeypatch.setattr(machine, "situation", lambda: seen)
    monkeypatch.setattr(llm, "quick", lambda host, model, prompt, **options: json.dumps({"commands": ["quit firefox"]}))
    monkeypatch.setattr(risky, "_signal", lambda pid, number: ended.append((pid, number)))
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: "firefox" if str(self).startswith("/proc/424") else real(self, *a, **k))
    monkeypatch.setattr(Path, "stat", lambda self, *a, **k: os.stat_result((0, 0, 0, 0, os.getuid(), 0, 0, 0, 0, 0)))
    opened: list[dict] = []
    reply = actions.carry_out([Step("do", "quit firefox")], Config(), log=QUIET, opened=opened)
    assert reply == "Quit firefox, 2 processes? Just say yes and I'll do it." and ended == []
    conversation.add("quit firefox", reply, "voice", opened[-1])
    [step] = actions.understand("yes", Config())
    assert actions.carry_out([step], Config(), log=QUIET).endswith("Quit firefox.")
    assert sorted(ended) == [(4242, 15), (4243, 15)]  # kindly: SIGTERM


def test_what_the_desktop_needs_is_never_quit(seen: dict) -> None:
    assert isinstance(risky.planned("quit 77", seen), str)
    assert isinstance(risky.planned("force quit gnome-shell", seen), str)
    assert isinstance(risky.planned("quit 99999", seen), str)  # not running


def test_trash_asks_first_and_undo_brings_it_back(home: Path, monkeypatch, no_real_changes: list) -> None:
    film = home / "Downloads" / "Mirzapur S01E01.mkv"
    plan = risky.planned("trash mirzapur", {})
    assert plan["ask"] == "Put Mirzapur S01E01.mkv in the Trash" and plan["path"] == str(film)
    monkeypatch.setattr(machine, "_run", lambda arguments, most=3000: f"trash:///Mirzapur%20S01E01.mkv\t{film}")
    ok, said = machine.carry([plan], log=QUIET)
    assert ok and no_real_changes == [["gio", "trash", str(film)]]
    actions.carry_out([Step("setting", "undo")], Config(), log=QUIET)
    assert no_real_changes[-1] == ["gio", "trash", "--restore", "trash:///Mirzapur%20S01E01.mkv"]


@pytest.mark.parametrize("command", ["trash downloads", "trash ~/.config/secret.txt", "trash /etc/passwd", "trash ~",
                                     "move mirzapur to /tmp", "move mirzapur to ~/.config", "trash nothing-like-this"])
def test_home_folders_hidden_files_and_the_rest_of_the_disk_are_left_alone(home: Path, command: str) -> None:
    assert isinstance(risky.planned(command, {}), str)


def test_moving_and_renaming(home: Path) -> None:
    moved = risky.planned("move mirzapur to documents", {})
    assert moved["to"] == str(home / "Documents" / "Mirzapur S01E01.mkv") and moved["ask"] == "Move Mirzapur S01E01.mkv to Documents"
    renamed = risky.planned("rename mirzapur to Mirzapur pilot", {})
    assert renamed["to"] == str(home / "Downloads" / "Mirzapur pilot.mkv")  # still an MKV
    (home / "Documents" / "Mirzapur S01E01.mkv").write_text("another")
    assert "write over" in risky.planned("move mirzapur to documents", {})


def test_software_is_checked_by_a_dry_run_first(monkeypatch) -> None:
    outputs = {"snap list": "Name Version\nfirefox 1\n", "dpkg -l cowsay": "", "apt-get -s install --no-remove cowsay": "Inst cowsay (3.0)",
               "apt-get -s remove gimp": "Remv gimp\nRemv gimp-data\n", "apt-get -s remove libgtk": "\n".join(f"Remv p{n}" for n in range(9))}
    monkeypatch.setattr(machine, "_run", lambda arguments, most=3000: outputs.get(" ".join(arguments), ""))
    assert risky.planned("install cowsay", {})["run"] == ["apt-get", "install", "-y", "--no-remove", "cowsay"]
    assert risky.planned("uninstall gimp", {})["ask"] == "Uninstall gimp and 1 more"
    assert risky.planned("uninstall firefox", {})["run"] == ["snap", "remove", "firefox"]
    assert isinstance(risky.planned("uninstall libgtk", {}), str)  # would take nine packages with it
    assert isinstance(risky.planned("uninstall ubuntu-desktop", {}), str)
    assert isinstance(risky.planned("install firefox", {}), str)  # already there
    assert not isinstance(risky.planned("install cowsay; rm -rf ~", {}), dict)  # not a form: refused by machine.planned
    assert isinstance(machine.planned("install cowsay; rm -rf ~", {}), str)


def test_the_model_cannot_skip_the_question() -> None:
    assert "run" not in actions.KINDS  # a "run" step only ever comes from a yes to Rigved's own question
