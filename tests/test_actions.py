from pathlib import Path

import pytest

from rigveda import actions, llm
from rigveda.actions import Step
from rigveda.config import Config


@pytest.mark.parametrize("heard, command", [
    ("Rigved.", ""),
    ("Hey Rigved, open Downloads", "open downloads"),
    ("Rig Ved open documents", "open documents"),
    ("OK Rigvid, what time is it?", "what time is it"),
    ("Rigveda, open fidelity.com", "open fidelity.com"),
    ("Open Downloads", None),
    ("The Rigveda is an ancient text", None),
    ("I was reading the Rigveda", None),
    ("Hey, Rick Fed, open Chrome", None),
    ("", None),
    ("What time is it, Rigved?", "what time is it"),
    ("Hey Ved, pause the movie.", "pause the movie"),
    ("Turn it up, Ved.", "turn it up"),
    ("The Vedas are old.", None),
    ("Hey Ved.", ""),
    ("Ved.", None),
    ("Ved, open downloads", "open downloads"),
    ("Thank you for listening to me, Rigved. Hey Rigved.", "thank you for listening to me rigved"),
    ("Can you pause the movie? Rigved.", "can you pause the movie"),
    ("That's the Rigveda, an ancient text.", None),
    ("I told my wife, Rigved is great", None),
    ("Play. Hey Rigved. Hey Rigved. Play the movie.", "play the movie"),
    ("That is good. Rigved, what time is it?", "what time is it"),
    ("I told my wife. Wait. Ved is fine.", None),
    ("I was reading. The Rigveda is old.", None),
])
def test_wake_word_must_come_first_or_last(heard: str, command: str | None) -> None:
    assert actions.wake(heard) == command


@pytest.mark.parametrize("command, steps", [
    ("open chrome and open fidelity", [Step("open", "chrome"), Step("open", "fidelity")]),
    ("please open my documents folder", [Step("open", "documents folder")]),
    ("show me the downloads", [Step("open", "downloads")]),
    ("turn it up", [Step("volume", "up")]),
    ("set volume to forty", [Step("volume", "40")]),
    ("volume 150", [Step("volume", "100")]),
    ("increased the volume by 50", [Step("volume", "+50")]),
    ("turn it down by twenty percent", [Step("volume", "-20")]),
    ("raise the volume", [Step("volume", "up")]),
    ("pause the music then open terminal", [Step("media", "pause"), Step("open", "terminal")]),
    ("resume the play in the vlc media player", [Step("media", "play")]),
    ("play the vlc media player movie", [Step("media", "play")]),
    ("pause vlc", [Step("media", "pause")]),
    ("stop the movie in vlc", [Step("media", "pause")]),
    ("open vlc", [Step("open", "vlc")]),
    ("what's the time", [Step("time")]),
    ("search for weather in san jose", [Step("search", "weather in san jose")]),
    ("find my passport", [Step("find", "passport")]),
    ("never mind", [Step("stop")]),
    ("open the door and the window", [Step("open", "door and the window")]),  # "and" only splits before a verb
])
def test_rules(command: str, steps: list[Step]) -> None:
    assert actions.parse(command) == steps


def test_anything_else_goes_to_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    assert actions.parse("could you make it a bit louder") is None
    asked = []
    monkeypatch.setattr(llm, "quick", lambda host, model, prompt, **options: asked.append(prompt) or
                        '{"steps": [{"action": "volume", "target": "up"}, {"action": "rm -rf", "target": "/"}]}')
    assert actions.understand("could you make it a bit louder", Config()) == [Step("volume", "up")]  # only known actions
    assert asked[0].endswith("could you make it a bit louder")


def test_the_model_being_down_still_gets_a_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(*args, **kwargs):
        raise llm.OllamaError("down")
    monkeypatch.setattr(llm, "quick", down)
    assert actions.understand("tell me a story", Config()) == [Step("talk", "tell me a story")]


@pytest.fixture
def desktop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home with standard folders and a few apps."""
    home = tmp_path / "home"
    for name in ("Downloads", "Documents", "github/rigveda-ai", "snap/vlc"):
        (home / name).mkdir(parents=True)
    apps = tmp_path / "applications"
    apps.mkdir()
    for file, fields in {"google-chrome": "Name=Google Chrome", "vlc": "Name=VLC media player",
                         "hidden": "Name=Secret Tool\nNoDisplay=true", "ptyxis": "Name=Ptyxis\nGenericName=Terminal"}.items():
        (apps / f"{file}.desktop").write_text(f"[Desktop Entry]\nType=Application\n{fields}\n[Desktop Action new]\nName=New Window\n")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(actions, "APP_DIRS", [str(apps)])
    monkeypatch.setattr(actions, "find_file", lambda description, config: "")
    return home


@pytest.mark.parametrize("spoken, kind, name", [
    ("downloads", "folder", "Downloads"),
    ("documents folder", "folder", "Documents"),
    ("chrome", "app", "Google Chrome"),
    ("vlc", "app", "VLC media player"),  # the app, not ~/snap/vlc
    ("terminal", "app", "Ptyxis"),
    ("fidelity", "website", "Fidelity"),
    ("fidelity dot com", "website", "fidelity.com"),
    ("rigveda ai", "folder", "rigveda-ai"),
    ("secret tool", "", "secret tool"),  # hidden apps aren't offered
    ("new window", "", "new window"),  # nor an app's extra actions
])
def test_resolving_names(desktop: Path, spoken: str, kind: str, name: str) -> None:
    assert actions.resolve(spoken, Config())[:2] == (kind, name)


def test_replies_without_doing_anything(desktop: Path) -> None:
    reply = actions.carry_out([Step("open", "chrome"), Step("open", "fidelity")], Config(), log=lambda message: None,
                              dry_run=True)
    assert reply == "Google Chrome is opening. Opening Fidelity for you."
    assert actions.carry_out([Step("open", "nowhere land")], Config(), log=lambda message: None, dry_run=True) == \
        "I couldn't find anything called nowhere land. Is it called something else, maybe?"
    assert actions.carry_out([Step("stop"), Step("open", "chrome")], Config(), dry_run=True) in actions.PERSONAS["witty"]["bye"]
    sage = Config(persona="sage")
    assert actions.carry_out([Step("open", "chrome")], sage, log=lambda message: None, dry_run=True).endswith(
        " Google Chrome awakens.")  # after "Tathaastu." or another blessing
    assert actions.carry_out([Step("open", "nowhere land")], sage, log=lambda message: None, dry_run=True) == \
        "I searched far and wide, but found nothing called nowhere land."


def test_every_persona_has_every_line() -> None:
    keys = set(actions.PERSONAS["witty"])
    for name, persona in actions.PERSONAS.items():
        assert set(persona) == keys, name
        for key in ("folder", "app", "website", "file"):
            assert "{name}" in persona[key]


def test_open_only_plays_a_file_whose_name_fits(desktop: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    film = "/films/La.La.Land.2016.1080p.mp4"
    monkeypatch.setattr(actions, "find_file", lambda description, config: film)
    assert actions.resolve("la la land", Config()) == ("file", "La.La.Land.2016.1080p.mp4", film)
    assert actions.resolve("something to listen to", Config())[0] == ""  # a search hit isn't enough


@pytest.mark.parametrize("command, steps", [
    ("open hey rigved open downloads", [Step("open", "downloads")]),  # the name said again
    ("openfidelity.com", [Step("open", "fidelity.com")]),
    ("open fidelity and google chrome", [Step("open", "fidelity and google chrome")]),  # "google" isn't a search here
])
def test_mishearings(command: str, steps: list[Step]) -> None:
    assert actions.parse(command) == steps


def test_open_x_and_y_opens_both(desktop: Path) -> None:
    reply = actions.carry_out([Step("open", "fidelity and google chrome")], Config(), log=lambda message: None, dry_run=True)
    assert reply == "Opening Fidelity for you. Google Chrome is opening."
    assert actions.resolve("apple music in google chrome", Config())[:2] == ("website", "Apple Music")


def test_voice_history_is_private_and_trimmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rigveda import voice

    monkeypatch.setattr(voice, "HISTORY", tmp_path / "voice-history.jsonl")
    monkeypatch.setattr(voice, "KEEP", 5)
    for n in range(40):
        voice.remember(f"open {n}", [f"open {n}"], "Done.")
    entries = voice.history()
    assert len(entries) <= 2 * 5 and entries[-1]["heard"] == "open 39" and entries[-1]["said"] == "Done."
    assert (tmp_path / "voice-history.jsonl").stat().st_mode & 0o777 == 0o600
    assert voice.history(limit=2)[0]["heard"] == "open 38"


def test_find_never_plays_a_film_it_only_found_by_its_words(desktop: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quiet = lambda message: None
    monkeypatch.setattr(actions, "find_file", lambda description, config: "/films/Killers.Of.The.Flower.Moon.2023.mkv")
    assert actions.carry_out([Step("find", "details")], Config(), log=quiet, dry_run=True).startswith("I looked through all your files")
    assert actions.carry_out([Step("find", "flower moon")], Config(), log=quiet, dry_run=True) == \
        "Found it: Killers Of The Flower Moon (2023)."
    monkeypatch.setattr(actions, "find_file", lambda description, config: "/docs/lease.pdf")
    assert actions.carry_out([Step("find", "apartment details")], Config(), log=quiet, dry_run=True) == "Found it: lease."


def test_media_commands_are_done_quietly_and_any_wording_works(monkeypatch: pytest.MonkeyPatch) -> None:
    pressed = []
    monkeypatch.setattr(actions, "players", lambda: ["org.mpris.MediaPlayer2.vlc"])
    monkeypatch.setattr(actions.subprocess, "run", lambda command, **options: pressed.append(command[-1]))
    assert actions.carry_out([Step("media", "resume")], Config(), log=lambda message: None) == ""
    assert actions.carry_out([Step("media", "whatever")], Config(), log=lambda message: None) == ""
    assert pressed == ["org.mpris.MediaPlayer2.Player.Play", "org.mpris.MediaPlayer2.Player.PlayPause"]


def test_volume_turns_the_apps_not_rigved_and_keeps_the_speakers_full(monkeypatch) -> None:
    import json
    import subprocess

    levels = {"@DEFAULT_AUDIO_SINK@": 0.6, "84": 1.5}  # speakers turned down to 60%, VLC at 150%
    streams = [{"id": 84, "info": {"props": {"media.class": "Stream/Output/Audio", "application.name": "VLC"}}},
               {"id": 91, "info": {"props": {"media.class": "Stream/Output/Audio",
                                             "application.name": "PipeWire ALSA [python3.14]"}}}]

    def run(command, **options):
        if command[0] == "pw-dump":
            return subprocess.CompletedProcess(command, 0, json.dumps(streams))
        if command[1] == "get-volume":
            return subprocess.CompletedProcess(command, 0, f"Volume: {levels[command[2]]:.2f}\n")
        if command[1] == "set-volume":
            value = command[-1]
            levels[command[-2]] = int(value[:-1]) / 100 if value.endswith("%") else float(value)
        return subprocess.CompletedProcess(command, 0, "")

    monkeypatch.setattr(subprocess, "run", run)
    assert actions.volume("60%") is True
    assert levels == {"@DEFAULT_AUDIO_SINK@": 1.0, "84": 0.6}  # Rigved's own stream (91) never touched
    streams.pop(0)
    assert actions.volume("up") is False  # nothing but Rigved: nothing to turn


def test_volume_or_music_needs_words_for_it(monkeypatch) -> None:
    from rigveda import llm

    reply = '{"steps": [{"action": "volume", "target": "up"}, {"action": "volume", "target": "100"}, {"action": "media", "target": "play"}]}'
    monkeypatch.setattr(llm, "quick", lambda *args, **kwargs: reply)
    assert actions.understand("okay can you hear me now", Config()) == [Step("talk", "okay can you hear me now")]
    assert actions.understand("crank that all the way and play", Config())[-1] == Step("media", "play")


def test_a_greeting_in_your_voice_is_for_rigved(monkeypatch):
    from rigveda import llm

    monkeypatch.setattr(llm, "quick", lambda *a, **k: '{"for_rigved": false}')  # the model's verdict that morning
    for said in ("Good morning.", "I said good morning.", "Good night, Rigved.", "I'm back"):
        assert actions.addressed(said, None)


def test_rigved_knows_your_name_without_looking_in_your_files() -> None:
    config = Config(owner_name="Kunal Vinay Kumar Suthar")
    assert actions.understand("What's my name likely?", config) == [actions.Step("talk", "what's my name likely")]
    assert "talking with Kunal" in actions.about(config) and "{you}" not in actions.line(config, "greet")
