import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from rigveda import actions, browser, conversation, desk, llm
from rigveda.actions import Step
from rigveda.config import Config

QUIET = lambda message: None  # noqa: E731
PAGE = {"ok": True, "tabs": [{"id": 1, "title": "Inbox (3) - Gmail", "site": "mail.google.com", "active": False},
                             {"id": 2, "title": "headphones - Google Search", "site": "www.google.com", "active": True},
                             {"id": 3, "title": "Lofi beats - YouTube", "site": "www.youtube.com", "active": False},
                             {"id": 4, "title": "Cat video - YouTube", "site": "www.youtube.com", "active": False}],
        "page": {"title": "headphones - Google Search", "url": "https://www.google.com/search?q=headphones",
                 "controls": [{"n": 1, "label": "Sony WH-1000XM6 review", "role": "link", "seen": True},
                              {"n": 2, "label": "Best headphones 2026", "role": "link", "seen": True},
                              {"n": 3, "label": "Buy now", "role": "button", "seen": True}], "headings": ["Results"]}}


@pytest.fixture
def chrome(monkeypatch) -> list:
    """The extension, as the listener sees it: what it was asked, answered from PAGE."""
    asked: list = []

    def ask(command, config=None):
        asked.append(command)
        if command["do"] == "look":
            return PAGE
        if command["do"] in ("label", "click"):
            label = next(control["label"] for control in PAGE["page"]["controls"] if control["n"] == command["n"])
            return {"ok": True, "label": label}
        if command["do"] == "close":
            return {"ok": True, "closed": len(command["ids"])}
        if command["do"] == "tab":
            return {"ok": True, "title": next(tab["title"] for tab in PAGE["tabs"] if tab["id"] == command["id"])}
        return {"ok": True}

    monkeypatch.setattr(browser, "ask", ask)
    return asked


def model(actions_chosen: list):
    return lambda host, model, prompt, **options: json.dumps({"actions": actions_chosen})


def test_the_relay_hands_commands_to_the_extension_and_its_answers_back() -> None:
    relay = browser.Relay()
    assert relay.do({"do": "look"}) == {"ok": False, "why": "not connected"}

    def extension() -> None:
        job = relay.next(wait=2)
        relay.done(job["id"], {"ok": True, "did": job["command"]["do"]})

    relay.seen = __import__("time").time()
    threading.Thread(target=extension, daemon=True).start()
    assert relay.do({"do": "scroll", "how": "down"}, wait=2) == {"ok": True, "did": "scroll"}


@pytest.mark.parametrize("said, meant", [("scroll down", "=scroll down"), ("scroll down a bit", "=scroll bit down"),
                                         ("go back", "=back"), ("show numbers", "=hints on"), ("click twenty three", "=click 23"),
                                         ("go to the top", "=scroll top"), ("open terminal", None), ("next", None)])
def test_common_phrasings_need_no_model(said: str, meant: str | None) -> None:
    assert browser.quick(said) == meant


def test_clicking_what_was_meant(chrome: list, monkeypatch) -> None:
    monkeypatch.setattr(llm, "quick", model([{"do": "click", "n": 2}]))
    assert actions.carry_out([Step("browse", "open the second result")], Config(), log=QUIET) == "Clicked Best headphones 2026."
    assert chrome[-1] == {"do": "click", "n": 2}


def test_tabs_by_what_they_are(chrome: list, monkeypatch) -> None:
    monkeypatch.setattr(llm, "quick", model([{"do": "close", "ids": [3, 4]}]))
    assert actions.carry_out([Step("browse", "close the YouTube tabs")], Config(), log=QUIET) == "Closed 2 tabs."
    monkeypatch.setattr(llm, "quick", model([{"do": "tab", "id": 1}]))
    assert actions.carry_out([Step("browse", "go to my Gmail tab")], Config(), log=QUIET) == "Here's Inbox (3) - Gmail."


def test_a_click_that_buys_waits_for_your_yes(chrome: list, monkeypatch) -> None:
    monkeypatch.setattr(llm, "quick", model([{"do": "click", "n": 3}]))
    opened: list[dict] = []
    reply = actions.carry_out([Step("browse", "buy it")], Config(), log=QUIET, opened=opened)
    assert reply == "Click “Buy now”? Just say yes and I'll do it." and not any(asked["do"] == "click" for asked in chrome)
    conversation.add("buy it", reply, "voice", opened[-1])
    [step] = actions.understand("yes", Config())
    actions.carry_out([step], Config(), log=QUIET)
    assert chrome[-1] == {"do": "click", "n": 3, "label": "Buy now"}  # only if it's still that button


def test_numbers_then_just_the_number(chrome: list) -> None:
    opened: list[dict] = []
    reply = actions.carry_out(actions.understand("show numbers", Config()), Config(), log=QUIET, opened=opened)
    assert reply.startswith("Numbers are on")
    conversation.add("show numbers", reply, "voice", opened[-1])
    assert actions.understand("Fourteen.", Config()) == [Step("browse", "=click 14")]
    assert conversation.last_opened() is None  # numbers aren't something "open it" means


def test_without_the_extension_scrolling_still_works(monkeypatch) -> None:
    pressed = []
    monkeypatch.setattr(browser, "ask", lambda command, config=None: {"ok": False, "why": "not connected"})
    monkeypatch.setattr(desk, "keys", lambda *combos, gap=0.15: pressed.extend(combos) or True)
    assert actions.carry_out([Step("browse", "=scroll down")], Config(), log=QUIET) == ""  # just the tone
    assert pressed == ["pagedown"]
    assert "isn't connected" in actions.carry_out([Step("browse", "click sign in")], Config(), log=QUIET)


@pytest.mark.skipif(shutil.which("node") is None, reason="no node")
@pytest.mark.parametrize("script", ["background.js", "browse.js", "content.js"])
def test_the_extension_scripts_are_valid(script: str) -> None:
    path = Path(__file__).resolve().parents[1] / "extension" / script
    assert subprocess.run(["node", "--check", str(path)], capture_output=True).returncode == 0
    assert json.loads((path.parent / "manifest.json").read_text())["content_scripts"][0]["js"] == ["content.js", "browse.js"]


def test_looking_at_chrome_makes_loose_words_about_the_page(monkeypatch) -> None:
    # 2026-10-30: "scroll up" was heard as "Screw it up." and taken as not said to Rigved; "click home" went to the Terminal
    asked = []

    def ask(command, config=None):
        asked.append(command)
        if command["do"] == "attention":
            return {"ok": True, "on": True, "title": "India's Batting vs NZ | willow.tv", "ago": 1.0}
        return {"ok": True, "label": "Home"}

    monkeypatch.setattr(browser, "ask", ask)
    assert actions.addressed("Screw it up.", Config())
    assert actions.understand("Screw it up.", Config()) == [Step("browse", "=scroll up")]
    assert actions.understand("a bit higher", Config()) == [Step("browse", "=scroll bit up")]
    prompts = []

    def model(host, model, prompt, **options):
        prompts.append(prompt)
        return json.dumps({"steps": [{"action": "press", "target": "Home"}]})

    monkeypatch.setattr(llm, "quick", model)
    monkeypatch.setattr(desk, "look", lambda: {"app": "org.gnome.Terminal", "window": "", "controls": [], "apps": []})
    assert actions.understand("Click home.", Config()) == [Step("browse", "Click home.")]
    assert "looking at Chrome" in prompts[0] and "Where they're looking: Google Chrome" in prompts[0]


def test_focus_on_chrome_by_voice(chrome: list) -> None:
    assert actions.understand("focus on chrome", Config()) == [Step("browse", "=focus on")]
    assert actions.carry_out([Step("browse", "=focus on")], Config(), log=QUIET).startswith("Okay, I'm focused on Chrome")
    assert chrome[-1] == {"do": "focus", "on": True}
    assert actions.understand("stop focusing", Config()) == [Step("browse", "=focus off")]


def test_video_on_the_page(chrome: list, monkeypatch) -> None:
    monkeypatch.setattr(llm, "quick", model([{"do": "video", "what": "forward", "seconds": 600}]))
    assert actions.carry_out([Step("browse", "fast forward by 10 minutes")], Config(), log=QUIET) == "Skipped ahead 10 minutes."


@pytest.mark.parametrize("said, meant", [("Screw it up.", "=scroll up"), ("scroll it down a little", "=scroll bit down"),
                                         ("page down", "=scroll page down"), ("scroll up a little bit please", "=scroll bit up"),
                                         ("keep going", "=scroll down"), ("take me to the top", "=scroll top")])
def test_scrolling_said_loosely(said: str, meant: str) -> None:
    assert browser.quick(said) == meant


def test_click_home_in_chrome_is_one_click(monkeypatch) -> None:
    monkeypatch.setattr(browser, "ask", lambda command, config=None: {"ok": True, "on": True, "title": "willow.tv", "ago": 1.0})
    monkeypatch.setattr(llm, "quick", lambda host, model, prompt, **options: json.dumps({"steps": [
        {"action": "app", "target": "chrome: new window"}, {"action": "window", "target": "switch to chrome"},
        {"action": "press", "target": "Home"}]}))
    assert actions.understand("Click home in Chrome.", Config()) == [Step("browse", "Click home in Chrome.")]
