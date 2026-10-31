import json
import py_compile
from pathlib import Path

import pytest

from rigveda import actions, conversation, desk, llm
from rigveda.actions import Step
from rigveda.config import Config

QUIET = lambda message: None  # noqa: E731


@pytest.fixture
def terminal(tmp_path: Path, monkeypatch) -> Path:
    """A Terminal whose launcher offers a new window and a new tab, as Ptyxis's does."""
    launcher = tmp_path / "org.gnome.Ptyxis.desktop"
    launcher.write_text("[Desktop Entry]\nName=Terminal\nExec=ptyxis --new-window\nType=Application\nActions=new-window;new-tab;\n\n"
                        "[Desktop Action new-window]\nName=New Window\nExec=ptyxis --new-window\n\n"
                        "[Desktop Action new-tab]\nName=New Tab\nExec=ptyxis --tab %U\n")
    monkeypatch.setattr(actions, "apps", lambda: {"terminal": ("Terminal", launcher)})
    return launcher


def test_open_a_new_terminal_window_is_understood_not_looked_for(terminal: Path, monkeypatch) -> None:
    # 2026-10-30: "Open the new terminal window please." → "I looked everywhere. No new terminal window."
    prompts = []

    def model(host, model, prompt, **options):
        prompts.append(prompt)
        return json.dumps({"steps": [{"action": "app", "target": "terminal: new window"}]})

    monkeypatch.setattr(llm, "quick", model)
    monkeypatch.setattr(desk, "look", lambda: {"app": "Google Chrome", "window": "GitHub", "controls": ["File"], "apps": ["Google Chrome"]})
    assert actions.understand("Open the new terminal window please.", Config()) == [Step("app", "terminal: new window")]
    assert "In front: Google Chrome" in prompts[0] and "app: do something an app offers" in prompts[0]
    assert actions.understand("open terminal", Config()) == [Step("open", "terminal")]  # it's called that: no model
    assert len(prompts) == 1


def test_what_an_app_offers_is_done_its_own_way(terminal: Path, monkeypatch) -> None:
    launched = []
    monkeypatch.setattr(actions, "launch", lambda *command: launched.append(command))
    reply = actions.carry_out([Step("app", "terminal: a new terminal window")], Config(), log=QUIET)
    assert launched == [("ptyxis", "--new-window")] and reply.startswith("New Window in Terminal")
    actions.carry_out([Step("app", "terminal: new tab")], Config(), log=QUIET)
    assert launched[-1] == ("ptyxis", "--tab")  # without the %U for files


def test_launcher_actions_match_what_was_meant() -> None:
    assert desk._alike("private window", "New Incognito Window") >= 0.75
    assert desk._alike("new private window", "New Incognito Window") > desk._alike("new private window", "New Window")
    assert desk._alike("new tab", "New Window") < 0.75 and desk._alike("preferences", "New Window") < 0.75


def test_restarting_waits_for_your_yes(monkeypatch) -> None:
    done = []
    monkeypatch.setattr(desk, "power", lambda target: done.append(target) or (True, target))
    opened: list[dict] = []
    reply = actions.carry_out([Step("power", "restart")], Config(), log=QUIET, opened=opened)
    assert reply == "Restart? Just say yes and I'll do it." and done == []
    conversation.add("restart the computer", reply, "voice", opened[-1])
    assert actions.addressed("yes", Config())
    [step] = actions.understand("Yes, do it.", Config())
    assert step.kind == "yes"
    actions.carry_out([step], Config(), log=QUIET)
    assert done == ["restart"]
    opened.clear()
    actions.carry_out([Step("power", "shut down")], Config(), log=QUIET, opened=opened)
    conversation.add("shut down", "Shut down?", "voice", opened[-1])
    assert actions.understand("no", Config()) == [Step("cancel", "Shut down")] and done == ["restart"]
    assert actions.carry_out([Step("power", "lock")], Config(), log=QUIET).startswith("Lock")  # nothing lost: at once


def test_settings_by_what_you_call_them(monkeypatch, no_real_settings: dict) -> None:
    ran = []
    monkeypatch.setattr(desk, "_run", lambda command: ran.append(command) or True)
    assert desk.setting("dark mode on") == (True, "Dark mode on.")
    assert no_real_settings[("org.gnome.desktop.interface", "color-scheme")] == "'prefer-dark'"
    assert desk.setting("turn off night light")[1] == "Night light off."
    assert desk.setting("wifi off") == (True, "Wi-Fi off.") and ran[-1] == ["nmcli", "radio", "wifi", "off"]
    assert desk.setting("bluetooth on")[0] and ran[-1] == ["bluetoothctl", "power", "on"]
    assert desk.setting("brightness 50")[0] is False


def test_a_menu_nobody_can_see_into_opens_with_its_key(monkeypatch) -> None:
    pressed = []
    monkeypatch.setattr(desk, "press", lambda name: {"ok": False, "why": "not there"})
    monkeypatch.setattr(desk, "look", lambda: {"app": "Some App", "window": "", "controls": [], "apps": []})
    monkeypatch.setattr(desk, "keys", lambda *combos, gap=0.15: pressed.extend(combos) or True)
    assert actions.carry_out([Step("press", "file menu")], Config(), log=QUIET).startswith("file menu")
    assert pressed == ["alt+f"]


def test_the_orb_asks_yes_or_no(monkeypatch, tmp_path: Path) -> None:
    from rigveda import server, voice

    conversation.add("restart", "Restart?", "voice", {"kind": "confirm", "name": "Restart", "thing": '[["power", "restart"]]'})
    asked = server.asking()
    assert asked["kind"] == "confirm" and [option["label"] for option in asked["options"]] == ["Yes, do it", "No, leave it"]
    assert conversation.choices() == [] and conversation.last_opened() is None


def test_the_desktop_helper_is_valid_python() -> None:
    py_compile.compile(str(Path(desk.__file__).with_name("hands.py")), doraise=True)


def test_closing_this_window_closes_the_one_you_were_looking_at(monkeypatch) -> None:
    # 2026-10-30: "close this?" then Yes in the orb, which brought the orb to the front: Alt+F4 closed the orb.
    front = {"app": "Google Chrome"}
    pressed, forward = [], []
    monkeypatch.setattr(desk, "look", lambda: {"app": front["app"], "window": "", "controls": [], "apps": []})
    monkeypatch.setattr(desk, "keys", lambda *combos, gap=0.15: pressed.extend(combos) or True)
    monkeypatch.setattr(desk, "bring_forward", lambda app: forward.append(app) or front.update(app=app) or True)
    opened: list[dict] = []
    reply = actions.carry_out([Step("window", "close")], Config(), log=QUIET, opened=opened)
    assert reply == "Close Google Chrome? Just say yes and I'll do it." and pressed == []
    conversation.add("close this", reply, "voice", opened[-1])
    front["app"] = "orb.py"  # you pressed Yes in the orb
    [step] = actions.understand("yes", Config())
    actions.carry_out([step], Config(), log=QUIET)
    assert forward == ["Google Chrome"] and pressed == ["alt+f4"]


def test_the_orb_itself_is_never_the_window_in_front(monkeypatch) -> None:
    pressed = []
    monkeypatch.setattr(desk, "look", lambda: {"app": "orb.py", "window": "", "controls": [], "apps": []})
    monkeypatch.setattr(desk, "keys", lambda *combos, gap=0.15: pressed.extend(combos) or True)
    actions.carry_out([Step("window", "minimize")], Config(), log=QUIET)
    assert pressed == []
    assert actions.understand("where's the orb", Config()) == [Step("do", "start the orb")]
