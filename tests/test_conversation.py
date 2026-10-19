import json

import pytest

from rigveda import actions, conversation, llm
from rigveda.actions import Step
from rigveda.config import Config


def test_keeps_the_last_few_exchanges_and_forgets_after_a_quiet_spell(monkeypatch: pytest.MonkeyPatch) -> None:
    for n in range(conversation.TURNS + 2):
        conversation.add(f"question {n}", f"answer {n}", "voice")
    turns = conversation.recent()
    assert len(turns) == conversation.TURNS and turns[-1]["you"] == f"question {conversation.TURNS + 1}"
    assert oct(conversation.PATH.stat().st_mode & 0o777) == "0o600"
    assert conversation.transcript().endswith(f"User: question {conversation.TURNS + 1}\nRigved: answer {conversation.TURNS + 1}")
    later = conversation.time.time() + conversation.FRESH + 1
    monkeypatch.setattr(conversation.time, "time", lambda: later)
    assert conversation.recent() == [] and conversation.transcript() == ""


def test_forget() -> None:
    conversation.add("my lease", "It ends in May.", "chat")
    assert actions.parse("forget that") == [Step("forget")] and actions.parse("start over") == [Step("forget")]
    actions.carry_out([Step("forget")], Config(), log=lambda message: None)
    assert conversation.recent() == []


def test_open_it_opens_what_was_just_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    lease = {"kind": "file", "name": "lease.pdf", "thing": "/home/me/lease.pdf"}
    conversation.add("find my lease", "Found it: lease.", "voice", lease)
    conversation.add("what time is it", "It's noon.", "voice")
    assert conversation.last_opened() == lease
    monkeypatch.setattr(actions, "resolve", lambda target, config: pytest.fail("should not look up 'it'"))
    opened: list[dict] = []
    reply = actions.carry_out(actions.parse("open it again"), Config(), log=lambda message: None, dry_run=True, opened=opened)
    assert reply == "Found it: lease." and opened == [lease]


def test_follow_ups_go_to_the_model_with_the_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = []

    def model(host, model, prompt, **options):
        prompts.append(prompt)
        return json.dumps({"steps": [{"action": "ask", "target": "how much is the deposit on my lease"}]})

    monkeypatch.setattr(llm, "quick", model)
    assert actions.understand("open the one from 2023", Config()) == [Step("open", "one from 2023")]  # nothing to lean on
    assert prompts == []
    conversation.add("when does my lease end", "In May.", "voice")
    assert actions.understand("and the deposit", Config()) == [Step("ask", "how much is the deposit on my lease")]
    assert actions.understand("open the one from 2023", Config())[0].kind == "ask"  # now it leans on the conversation
    assert "User: when does my lease end\nRigved: In May." in prompts[0]
    assert actions.understand("open downloads", Config()) == [Step("open", "downloads")]  # needs nothing earlier
    assert len(prompts) == 2


def test_standalone_rewrites_only_when_there_is_a_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm, "quick", lambda host, model, prompt, **options: '"How much is the deposit on my lease?"')
    assert conversation.standalone("and the deposit?", "host", "model") == "and the deposit?"
    conversation.add("when does my lease end", "In May.", "chat")
    assert conversation.standalone("and the deposit?", "host", "model") == "How much is the deposit on my lease?"
