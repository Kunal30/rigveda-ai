import json
import math
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import numpy as np
import pytest

from rigveda import actions, voice
from rigveda.server import ServerConfig, make_handler


@pytest.fixture
def listener(tmp_path, monkeypatch, config):
    """A listener with no microphone, speakers or models: just the typed-command socket."""
    monkeypatch.setattr(voice, "SOCKET", tmp_path / "voice.sock")
    monkeypatch.setattr(voice, "HISTORY", tmp_path / "history.jsonl")
    monkeypatch.setattr(voice, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(actions, "understand", lambda command, config: [actions.Step("time", "")])
    monkeypatch.setattr(actions, "carry_out", lambda steps, config, log=print, **options: "It's noon.")
    monkeypatch.setattr(actions, "playing", lambda: [])  # never the real media players
    monkeypatch.setattr(actions, "press", lambda player, method: None)
    monkeypatch.setattr(voice.Mouth, "blip", staticmethod(lambda rising=True: None))  # no tones from the speakers
    monkeypatch.setattr(actions, "addressed", lambda said, config: "honey" not in said)  # no model asked
    monkeypatch.setattr(voice, "recording", lambda: [])  # never the real PipeWire
    heard = voice.Listener.__new__(voice.Listener)
    heard.config, heard.log, heard.shown = config, lambda message: None, ("", 0.0, 0.0)
    heard.interrupted = False
    heard.typed()
    return heard


def test_typed_command_is_carried_out_and_remembered(listener) -> None:
    assert voice.send("Rigved, what time is it?") == {"did": ["time"], "said": "It's noon."}
    [entry] = voice.history()
    assert entry["heard"] == "what time is it" and entry["typed"] is True
    assert oct(voice.SOCKET.stat().st_mode & 0o777) == "0o600"


def test_server_passes_typed_commands_on(listener, tmp_path) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ServerConfig(tmp_path / "db.sqlite", "model", "http://x", 5)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        http = HTTPConnection("127.0.0.1", httpd.server_address[1])
        http.request("POST", "/api/voice", json.dumps({"text": "time"}), {"Content-Type": "application/json"})
        response = http.getresponse()
        assert response.status == 200 and json.loads(response.read())["said"] == "It's noon."
        voice.SOCKET.unlink()
        http.request("POST", "/api/voice", json.dumps({"text": "time"}), {"Content-Type": "application/json"})
        assert http.getresponse().status == 503
    finally:
        httpd.shutdown()


class Done(Exception):
    pass


class FakeEars:
    """Hands out stretches of "speech" (each just its words; None for a silence) and then stops the listener."""

    def __init__(self, said):
        self.said = list(said)
        self.level = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def next(self, timeout=None, finished=None):
        while self.said:
            speech = self.said.pop(0)
            if speech is not None:
                return speech
            if timeout:
                return None
        if timeout:
            return None
        raise Done

    def clear(self):
        pass


class Speech(str):
    def __len__(self):
        return 0  # no time at all, so none of it overlaps a conversation that just ended


def test_after_the_name_you_can_go_on_without_it_until_you_stop(listener, monkeypatch) -> None:
    said = ["Rigved, what time is it", "turn it up", None, "a film playing", "Rigved, pause the movie", "and play it"]
    monkeypatch.setattr(voice, "Ears", lambda **options: FakeEars(Speech(text) if text else None for text in said))
    listener.busy, listener.done = threading.Lock(), 0.0
    listener.command = lambda audio, yours=False: (str(audio), actions.wake(audio))
    listener.hear = lambda audio: str(audio)
    answered = []
    listener.respond = lambda command, ears=None: answered.append(command) or [actions.parse(command)[0].kind]
    with pytest.raises(Done):
        listener.listen_for_name()
    # "turn it up" needed no name; after the silence "a film playing" did; after "pause the movie" it stopped listening
    assert answered == ["what time is it", "turn it up", "pause the movie"]


def test_the_conversation_ends_when_you_turn_to_someone_else(listener, monkeypatch) -> None:
    said = ["Rigved, what time is it", "tell me a joke", "honey can you grab the door", "and another one"]
    monkeypatch.setattr(voice, "Ears", lambda **options: FakeEars(Speech(text) for text in said))
    listener.busy, listener.done = threading.Lock(), 0.0
    listener.command = lambda audio, yours=False: (str(audio), actions.wake(audio))
    listener.hear = lambda audio: str(audio)
    answered = []
    listener.respond = lambda command, ears=None: answered.append(command) or ["talk"]
    with pytest.raises(Done):
        listener.listen_for_name()
    assert answered == ["what time is it", "tell me a joke"]  # "and another one" needed the name again


def test_nothing_is_heard_while_a_conversation_by_key_goes_on(listener, monkeypatch) -> None:
    monkeypatch.setattr(voice, "Ears", lambda **options: FakeEars([Speech("Rigved, open downloads")]))
    listener.busy, listener.done = threading.Lock(), 0.0
    listener.command = lambda audio, yours=False: (str(audio), actions.wake(audio))
    answered = []
    listener.respond = lambda command, ears=None: answered.append(command) or ["talk"]
    listener.busy.acquire()
    with pytest.raises(Done):
        listener.listen_for_name()
    assert answered == []


def test_a_stalled_microphone_is_noticed(monkeypatch) -> None:
    ears = voice.Ears.__new__(voice.Ears)
    ears.frames, ears.watch = voice.queue.Queue(), True
    ears.last = voice.time.monotonic() - voice.STALLED - 1
    with pytest.raises(OSError):
        ears.next()


def test_what_plays_is_paused_while_you_talk_and_played_again_after(listener, monkeypatch) -> None:
    pressed = []
    monkeypatch.setattr(actions, "playing", lambda: ["vlc"])
    monkeypatch.setattr(actions, "press", lambda player, method: pressed.append(method))
    listener.respond = lambda command, ears=None: [f"talk {command}"]
    listener.answer(FakeEars([]), "what time is it")
    assert pressed == ["Pause", "Play"]
    pressed.clear()
    listener.respond = lambda command, ears=None: ["media pause"]  # "pause the movie": it stays paused
    listener.answer(FakeEars([]), "pause the movie")
    assert pressed == ["Pause"]


def test_other_voices_are_passed_over_and_yours_needs_no_name_to_pause(listener, monkeypatch) -> None:
    said = ["Rigved, open downloads", "pause", "Rigved, open downloads", "pause", "pause the movie and tell me a story"]
    voices = iter([0.1, 0.1, 0.8, 0.8, 0.8])  # a film's actors, then you
    monkeypatch.setattr(voice, "Ears", lambda **options: FakeEars(Speech(text) for text in said))
    listener.busy, listener.done = threading.Lock(), 0.0
    listener.score = lambda audio: next(voices)
    listener.command = lambda audio, yours=False: (str(audio), actions.wake(audio))
    listener.hear = lambda audio: str(audio)
    answered = []
    listener.respond = lambda command, ears=None: answered.append(command) or ["media pause"]
    listener.config.need_name = True
    with pytest.raises(Done):
        listener.listen_for_name()
    assert answered == ["open downloads", "pause"]  # with the name needed, the longer request without it isn't taken


def test_without_the_name_anything_in_your_voice_said_to_rigved_is_taken(listener, monkeypatch) -> None:
    from rigveda import keyboard

    said = ["what's the weather in seattle", "honey, where are my keys", "open chrome", "turn it down", "find my lease"]
    voices = iter([0.9, 0.9, 0.1, 0.9, 0.9])  # the third is the TV
    monkeypatch.setattr(voice, "Ears", lambda **options: FakeEars(Speech(text) for text in said))
    listener.busy, listener.done = threading.Lock(), 0.0
    listener.score = lambda audio: next(voices)
    listener.command = lambda audio, yours=False: (str(audio), actions.wake(audio))
    listener.hear = lambda audio: str(audio)
    listener.holds = keyboard.Holds()
    calls = iter([None, None, "space", None])  # asked only of what was in your voice
    real = listener.holds.during
    listener.holds.during = lambda start, end: next(calls) == "space"  # Space held during the fourth
    recording = iter([[], [], ["Zoom"]])  # a meeting during the fifth (Space was held during the fourth)
    monkeypatch.setattr(voice, "recording", lambda: next(recording))
    answered = []
    listener.respond = lambda command, ears=None: answered.append(command) or ["time"]
    listener.answer = lambda ears, command: answered.append(command)
    with pytest.raises(Done):
        listener.listen_for_name()
    assert answered == ["what's the weather in seattle"]  # not to someone else, not the TV, not Claude, not Zoom
    assert real(0, 1) is False


def test_space_counts_only_when_held_not_tapped() -> None:
    from rigveda import keyboard

    holds = keyboard.Holds()
    holds.pressed(1, 10.0), holds.pressed(0, 10.1)  # typing
    assert not holds.during(9.0, 11.0)
    holds.pressed(1, 20.0), holds.pressed(0, 23.0)  # dictating to Claude
    assert holds.during(23.5, 25.0)  # finished talking just after letting go
    assert holds.during(18.0, 19.5)  # started just before pressing
    assert not holds.during(25.0, 27.0)


def test_your_voice_is_learned_from_the_talk_key_and_told_apart(tmp_path, monkeypatch) -> None:
    from rigveda import voiceprint

    class Extractor:  # a "fingerprint" that is just which voice it is
        dim = 2

        def create_stream(self):
            return Stream()

        def compute(self, stream):
            return [1.0, 0.0] if stream.audio.mean() > 0 else [0.0, 1.0]

    class Stream:
        def accept_waveform(self, rate, audio):
            self.audio = audio

        def input_finished(self):
            pass

    me, film = np.ones(2 * voiceprint.RATE, dtype=np.float32), -np.ones(2 * voiceprint.RATE, dtype=np.float32)
    known = stub_voiceprint(Extractor(), tmp_path)
    assert known.score(me) is None and known.mine(film)  # not known yet: everyone counts
    for _ in range(voiceprint.ENOUGH):
        known.learn(me)
    assert known.ready and known.mine(me) and not known.mine(film)
    known.learn(film)  # a film heard while holding the key isn't learned as you
    assert len(known.learned) == voiceprint.ENOUGH
    assert oct(known.path.stat().st_mode & 0o777) == "0o600"


def stub_voiceprint(extractor, folder):
    from rigveda import voiceprint

    known = voiceprint.Voiceprint.__new__(voiceprint.Voiceprint)
    known.extractor, known.path, known.loaded = extractor, folder / "my-voice.npz", None
    known.trained, known.model, known.model_loaded = folder / voiceprint.TRAINED.name, None, None
    known.enrolled = known.learned = np.zeros((0, extractor.dim), dtype=np.float32)
    known._load()
    return known


def test_training_tells_you_from_a_voice_that_sounds_close(tmp_path) -> None:
    """An AI voice through the speakers lies close to yours, so closeness alone takes it for you; the trained model doesn't."""
    from rigveda import voiceprint

    class Extractor:  # the first samples of the clip are its "fingerprint"
        dim = 3

        def create_stream(self):
            return Stream()

        def compute(self, stream):
            return list(stream.audio[:3])

    class Stream:
        def accept_waveform(self, rate, audio):
            self.audio = audio

        def input_finished(self):
            pass

    rng = np.random.default_rng(1)

    def speech(lean: float) -> np.ndarray:
        audio = np.zeros(voiceprint.RATE, dtype=np.float32)
        audio[:3] = [0.9, lean + rng.normal(0, 0.03), rng.normal(0, 0.05)]
        return audio

    clips = tmp_path / "clips"
    known = stub_voiceprint(Extractor(), tmp_path)
    for _ in range(40):
        voiceprint.keep(speech(0.1), "you", clips=clips)
        voiceprint.keep(speech(-0.1), "rigved" if _ % 2 else "heard", 0.9, clips=clips)
    played = []
    answers = iter(["r"] + ["n"] * 19 + ["s", "q"])  # the heard ones are labelled not you, one skipped
    voiceprint.label(log=print, clips=clips, ask=lambda prompt: next(answers), play=played.append, service=None)
    assert len(played) == 21 and played[0] == played[1]  # "r" played it again
    assert len(list((clips / "others").glob("*.wav"))) == 19 and len(list((clips / "heard").glob("*.wav"))) == 1
    voiceprint.keep(speech(-0.1), "others", clips=clips)
    for _ in range(voiceprint.ENOUGH):
        known.learn(np.concatenate([speech(0.1)] * 2))
    assert known.score(speech(-0.1)) > voiceprint.MATCH  # before: taken for you

    said = []
    assert voiceprint.train(log=said.append, clips=clips, voiceprint=known)
    assert "40 of you, 40 of others (20 of Rigved's own voice), 1 not labelled" in said[0]
    assert known.score(speech(0.1)) > voiceprint.MATCH > known.score(speech(-0.1))
    assert oct(known.trained.stat().st_mode & 0o777) == "0o600"
    assert oct(clips.stat().st_mode & 0o777) == "0o700"


def test_training_waits_for_enough_clips(tmp_path) -> None:
    from rigveda import voiceprint

    said = []
    assert not voiceprint.train(log=said.append, clips=tmp_path, voiceprint=object())
    assert "Not enough" in said[-1]


def fake_ears(frames) -> voice.Ears:
    """Ears whose "frames" say how loud speech is (1 speech, 0 quiet), with no microphone or Silero."""
    ears = voice.Ears.__new__(voice.Ears)
    ears.frames, ears.watch, ears.level = voice.queue.Queue(), False, lambda value: None
    ears.speech = lambda frame: float(frame[0])
    ears.reset = lambda: None
    for frame in frames:
        ears.frames.put(np.full(voice.FRAME, frame, dtype=np.float32))
    return ears


def test_a_finished_thought_ends_quickly_and_a_pause_mid_sentence_doesnt() -> None:
    pause = int(voice.PAUSE * voice.RATE / voice.FRAME)
    longest = math.ceil(voice.MID_THOUGHT * voice.RATE / voice.FRAME)
    said = [1] * 20 + [0] * pause + [1] * 20 + [0] * (longest + 5)
    finished = fake_ears(said).next(timeout=1, finished=lambda audio: 0.9)
    assert len(finished) == (20 + pause) * voice.FRAME  # "what time is it?" and a short pause: done
    mid = fake_ears(said).next(timeout=1, finished=lambda audio: 0.1)
    assert len(mid) == (20 + pause + 20 + longest) * voice.FRAME  # "remind me to call mom and…": waited for the rest


def test_talking_over_rigved_stops_it_but_its_own_voice_doesnt(listener) -> None:
    for score, stops in ((0.9, True), (0.2, False)):
        ears = fake_ears([1] * 40)
        ears.clear = lambda: None
        listener.hush, listener.interrupted = threading.Event(), False
        listener.score = lambda audio: score
        speaking = threading.Event()
        speaking.set()
        watcher = threading.Thread(target=listener.overheard, args=(ears, speaking))
        watcher.start()
        watcher.join(timeout=0.5)
        speaking.clear()
        watcher.join()
        assert listener.hush.is_set() is stops and listener.interrupted is stops
        assert (ears.frames.qsize() > 0) is stops  # what you said is kept for the next turn
