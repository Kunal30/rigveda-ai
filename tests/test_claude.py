import io
import json

import pytest

from rigveda import actions, claude
from rigveda.config import Config


class FakeClaude:
    """What `claude -p --output-format stream-json` prints: events, one per line, the answer last."""
    def __init__(self, events, returncode=0):
        self.stdout = io.StringIO("".join(json.dumps(event) + "\n" for event in events) + "not json\n")
        self.returncode = returncode

    def wait(self):
        return self.returncode

    def kill(self):
        pass


def answer(text, **extra):
    return {"type": "result", "subtype": "success", "is_error": False, "result": text, **extra}


def test_claude_replies_without_markdown_or_links_and_says_when_it_searches(monkeypatch, tmp_path) -> None:
    started = []
    events = [{"type": "system", "subtype": "init"},
              {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "WebSearch"}]}},
              answer("**Antonelli** won in Austin. See https://example.com")]
    monkeypatch.setattr(claude, "WORKSPACE", tmp_path / "claude")
    monkeypatch.setattr(claude.subprocess, "Popen", lambda command, **kwargs: started.append((command, kwargs)) or FakeClaude(events))
    searched = []
    said = claude.reply("who won the race", "User: hi\nRigved: Hey.", "witty", "claude-sonnet-5-5", lambda: searched.append(1))
    assert said == "Antonelli won in Austin. See"
    assert searched == [1]
    command, kwargs = started[0]
    assert command[command.index("--tools") + 1] == "WebSearch"  # nothing that could touch the computer
    assert command[command.index("--model") + 1] == "claude-sonnet-5-5"
    assert "User: hi" in command[-1] and command[-1].endswith("They just said: who won the race")
    assert kwargs["cwd"] == tmp_path / "claude"


@pytest.mark.parametrize("events, returncode", [
    ([], 1),
    ([{"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "limit reached"}], 1),
    ([answer("  ")], 0),
])
def test_claude_failures_are_errors(monkeypatch, tmp_path, events, returncode) -> None:
    monkeypatch.setattr(claude, "WORKSPACE", tmp_path / "claude")
    monkeypatch.setattr(claude.subprocess, "Popen", lambda command, **kwargs: FakeClaude(events, returncode))
    with pytest.raises(claude.ClaudeError):
        claude.reply("hello", "", "witty", "claude-sonnet-5-5")


def test_talk_goes_to_claude_when_set_and_falls_back_locally(monkeypatch) -> None:
    monkeypatch.setattr(actions, "spoken_reply", lambda config, prompt: "local")
    monkeypatch.setattr(claude, "reply", lambda said, history, about, model, searching: "from claude")
    assert actions.talk("hi", Config()) == "local"  # not set up: stays here
    assert actions.talk("hi", Config(claude_model="claude-sonnet-5-5")) == "from claude"

    def broken(*args):
        raise claude.ClaudeError("limit reached")

    monkeypatch.setattr(claude, "reply", broken)
    logged = []
    assert actions.talk("hi", Config(claude_model="claude-sonnet-5-5"), logged.append) == "local"
    assert "limit reached" in logged[0]


@pytest.mark.parametrize("question, mine", [
    ("what's my lease deposit", True),
    ("when did I pay the electricity bill", True),
    ("how much did we spend on the trip", True),
    ("why do cats purr", False),
    ("tell me who won the latest F1 race", False),
])
def test_questions_about_you_stay_on_this_computer(question, mine) -> None:
    assert actions.personal(question) == mine


def test_questions_go_to_claude_unless_theyre_about_you(monkeypatch) -> None:
    asked, searched = [], []
    monkeypatch.setattr(claude, "reply", lambda said, *args: asked.append(said) or ("FILES" if "deposit" in said else "Because."))
    monkeypatch.setattr(actions, "answer", lambda question, config: searched.append(question) or "From your files.")
    config = Config(claude_model="claude-sonnet-5-5")
    assert actions.carry_out([actions.Step("ask", "why do cats purr")], config) == "Because."
    assert actions.carry_out([actions.Step("ask", "what's my lease deposit")], config) == "From your files."
    assert asked == ["why do cats purr"] and searched == ["what's my lease deposit"]  # yours never went to Claude
    assert actions.carry_out([actions.Step("talk", "what's the deposit on the lease")], config) == "From your files."
    assert searched[-1] == "what's the deposit on the lease"  # Claude handed it back


def test_the_time_somewhere_else_goes_to_claude(monkeypatch) -> None:
    from rigveda import llm

    monkeypatch.setattr(llm, "quick", lambda *args, **kwargs: '{"steps": [{"action": "time", "target": ""}]}')
    assert actions.understand("what time is it in London right now", Config()) == [actions.Step("talk", "what time is it in London right now")]
    assert actions.understand("what's the UTC time", Config()) == [actions.Step("talk", "what's the UTC time")]
    assert actions.understand("what time is it now", Config()) == [actions.Step("time", "")]


def test_what_rigved_cant_do_yet_is_said_plainly() -> None:
    said = actions.carry_out([actions.Step("unknown", "reboot the system")], Config())
    assert said in actions.PERSONAS["witty"]["cant"]
    assert actions.carry_out([actions.Step("unknown", "mm")], Config()) in actions.PERSONAS["witty"]["unsure"]
