"""Talk to Rigveda: hold Right Ctrl while you speak (or tap Super+R, or in wake-word mode say "Hey Rigved"), and it
answers aloud. Or type to it in the orb's Voice tab, and it answers there in text.

Everything happens on this computer. With only the keys, the microphone is closed until you use one, and closes again
when you let go (or, after Super+R, once you stop talking). Wake-word mode keeps the keys and also keeps the microphone
open, and only speech that starts with "Rigved" goes further than Whisper.
Silero (bundled with faster-whisper) notices when someone speaks, Whisper writes it down, and Kokoro speaks the reply.
The orb follows along through a small state file: it pops when the microphone opens and pulses with both voices.
"""

from __future__ import annotations

import faulthandler
import json
import os
import queue
import re
import signal
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import numpy as np

from . import actions, conversation
from .voiceprint import MATCH
from .config import Config, _xdg

RATE = 16_000
FRAME = 512  # 32 ms, what Silero expects at 16 kHz
START, END = 0.5, 0.3  # speech probability that starts and ends a stretch of speech
QUIET = 0.8  # seconds of quiet that end it
PAUSE = 0.25  # seconds of quiet after which the turn model is asked whether you've finished
MID_THOUGHT = 1.5  # seconds of quiet that end it anyway when you sounded mid-sentence
LONGEST = 15.0  # seconds; longer speech is cut off (a film playing, say)
WAKE_WINDOW = 4.0  # seconds; the wake word must come first, so only this much is read before deciding
PROMPT = "Rigved, open Downloads. Hey Rigved. Hey Ved."  # nudges Whisper to spell the names the same way every time
# Kokoro's men's voices: American (am_), British (bm_) and Hindi (hm_, English with an Indian accent).
VOICES = ("am_michael", "am_fenrir", "am_puck", "am_eric", "am_onyx", "bm_george", "bm_fable", "hm_omega", "hm_psi")
MODEL_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "rigveda" / "voice"
MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
MODEL_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
STATE = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "rigveda-voice.json"  # what the orb shows, live
ANSWER_WITHIN = 6.0  # seconds after the key to start talking
STALLED = 3.0  # seconds with no sound at all from an open microphone before it's opened again
NAME_ALONE = 8.0  # seconds to start the request after saying just "Rigved"
CONVERSATION = 20.0  # seconds of quiet after an answer that end the conversation (then it's the name again)
INTERRUPT = 0.6  # seconds of you talking over Rigved before it stops to listen
QUIET_MATCH = 0.2  # MATCH when nothing is playing, so the TV can't be talking: "Play the movie", said softly over a paused film, scored 0.32 (2026-10-25)
INTERRUPT_MATCH = 0.55  # stricter than MATCH: through the TV, Rigved's own voice scored up to 0.44 as you (2026-10-25)
HISTORY = _xdg("XDG_DATA_HOME", ".local/share") / "rigveda" / "voice-history.jsonl"  # what you said and what it said
KEEP = 1000  # conversations kept in the history
MUTED = STATE.with_name("rigveda-muted")  # there while Rigved is muted from the orb: hands-free, the microphone is closed
SOCKET = STATE.with_name("rigveda-voice.sock")  # typed commands from the orb's Voice tab, by way of the server
TYPED_LONGEST = 2_000  # characters


def remember(heard: str, did: list[str], said: str, typed: bool = False) -> None:
    """Add an exchange to the history the orb shows. The file is readable only by you."""
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": int(time.time()), "heard": heard, "did": did, "said": said}
    if typed:
        record["typed"] = True
    entry = json.dumps(record) + "\n"
    descriptor = os.open(HISTORY, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as file:
        file.write(entry)
    lines = HISTORY.read_text(encoding="utf-8").splitlines(keepends=True)
    if len(lines) > 2 * KEEP:  # drop the oldest now and then, not on every exchange
        HISTORY.write_text("".join(lines[-KEEP:]), encoding="utf-8")


def history(limit: int = 200) -> list[dict]:
    """The latest exchanges, oldest first."""
    try:
        lines = HISTORY.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


class Mouth:
    """Speaks with Kokoro, downloading its model (about 340 MB) the first time."""

    def __init__(self, voice: str, speed: float = 1.0, log=print) -> None:
        from kokoro_onnx import Kokoro

        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        for name in MODEL_FILES:
            path = MODEL_DIR / name
            if not path.exists():
                log(f"Downloading the voice model ({name}) to {MODEL_DIR}…")
                partial = path.with_suffix(".part")
                with urlopen(MODEL_URL + name, timeout=60) as response, partial.open("wb") as file:
                    while chunk := response.read(1 << 20):
                        file.write(chunk)
                partial.rename(path)
        self.kokoro = Kokoro(str(MODEL_DIR / MODEL_FILES[0]), str(MODEL_DIR / MODEL_FILES[1]))
        self.voice, self.speed = voice, speed

    def say(self, text: str, level=lambda value: None, stop: threading.Event | None = None) -> None:
        """Speak, telling `level` how loud it is as it goes; stop early if `stop` is set. Sentence by sentence: the
        first is heard while the rest are still being made."""
        import sounddevice

        # American voices read American English; the others say words like "Tathaastu" better as British.
        lang = "en-us" if self.voice.startswith("a") else "en-gb"
        sentences = [part for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part] or [text]
        made: queue.Queue = queue.Queue()

        def make() -> None:
            for sentence in sentences:
                if stop is not None and stop.is_set():
                    break
                made.put(self.kokoro.create(sentence, voice=self.voice, speed=self.speed, lang=lang))
            made.put(None)

        threading.Thread(target=make, daemon=True).start()
        while (piece := made.get()) is not None:
            audio, rate = piece
            window = rate // 25
            loudness = [float(np.sqrt(np.mean(audio[i:i + window] ** 2))) for i in range(0, len(audio), window)]
            peak = max(loudness, default=0) or 1
            sounddevice.play(audio, rate)
            start = time.monotonic()
            while (elapsed := time.monotonic() - start) < len(audio) / rate:
                if stop is not None and stop.is_set():
                    sounddevice.stop()
                    return
                level(loudness[min(int(elapsed * 25), len(loudness) - 1)] / peak)
                time.sleep(0.03)
            sounddevice.wait()

    @staticmethod
    def blip(rising: bool = True) -> None:
        """A short soft tone: rising when the microphone opens, falling when it closes."""
        import sounddevice

        rate, length = 24_000, 0.09
        t = np.arange(int(rate * length)) / rate
        pitch = np.linspace(660, 990, len(t)) if rising else np.linspace(880, 590, len(t))
        tone = 0.18 * np.sin(2 * np.pi * np.cumsum(pitch) / rate) * np.sin(np.pi * t / length)
        try:
            sounddevice.play(tone.astype(np.float32), rate)
        except Exception:  # no speakers right now; the orb still shows it
            pass


class Ears:
    """The microphone, cut into stretches of speech."""

    def __init__(self, level=lambda value: None, watch: bool = False) -> None:
        import onnxruntime
        import sounddevice
        from faster_whisper.vad import get_assets_path

        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = options.intra_op_num_threads = 1
        options.log_severity_level = 4
        self.vad = onnxruntime.InferenceSession(str(Path(get_assets_path()) / "silero_vad_v6.onnx"),
                                                providers=["CPUExecutionProvider"], sess_options=options)
        self.level = level  # told how loud each moment of speech is, 0 to 1
        self.watch = watch  # raise OSError if the microphone stops sending anything (it can stall while staying open)
        self.last = time.monotonic()  # when the last sound arrived
        self.frames: queue.Queue[np.ndarray] = queue.Queue()
        self.stream = sounddevice.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=FRAME,
                                              callback=lambda data, *_: self.frames.put(data[:, 0].copy()))
        self.reset()

    def reset(self) -> None:
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros(64, dtype=np.float32)

    def speech(self, frame: np.ndarray) -> float:
        """How likely this frame is speech, carrying Silero's memory from frame to frame."""
        audio = np.concatenate([self.context, frame])[None, :]
        self.context = frame[-64:]
        probability, h, c = self.vad.run(None, {"input": audio, "h": self.state[:1], "c": self.state[1:]})
        self.state = np.concatenate([h, c])
        return float(probability.ravel()[0])

    def __enter__(self) -> Ears:
        self.stream.start()
        self.last = time.monotonic()
        return self

    def __exit__(self, *_) -> None:
        self.stream.stop()
        self.stream.close()

    def clear(self) -> None:
        """Forget what the microphone picked up meanwhile, e.g. while Rigveda was speaking."""
        while not self.frames.empty():
            self.frames.get_nowait()
        self.reset()

    def unread(self, frames: list[np.ndarray]) -> None:
        """Put speech back to be heard again: what you said over Rigved, which stopped it."""
        later = []
        while not self.frames.empty():
            later.append(self.frames.get_nowait())
        for frame in [*frames, *later]:
            self.frames.put(frame)
        self.reset()

    def heard(self, frame: np.ndarray) -> float:
        """How likely a frame is speech; tells `level` how loud it is (speech sits around -40 to -15 dBFS) as 0 to 1."""
        probability = self.speech(frame)
        loudness = 20 * np.log10(float(np.sqrt(np.mean(frame ** 2))) + 1e-9)
        self.level(min(max((loudness + 45) / 30, 0.0), 1.0) if probability >= END else 0.0)
        return probability

    def record(self, until: threading.Event, longest: float = 60.0) -> np.ndarray | None:
        """Everything until `until` is set (the talk key let go), or None if it was too short to be speech."""
        frames: list[np.ndarray] = []
        while not until.is_set() and len(frames) * FRAME / RATE < longest:
            try:
                frame = self.frames.get(timeout=0.1)
            except queue.Empty:
                continue
            if len(frame) == FRAME:
                self.heard(frame)
                frames.append(frame)
        time.sleep(0.15)  # the end of the last word, still on its way from the microphone
        while not self.frames.empty():
            frames.append(self.frames.get_nowait())
        return np.concatenate(frames) if len(frames) * FRAME / RATE >= 0.3 else None

    def next(self, timeout: float | None = None, cancel: threading.Event | None = None,
             finished=None) -> np.ndarray | None:
        """The next stretch of speech, or None if nobody starts speaking within `timeout` seconds or `cancel` is set.
        With `finished` (speech → how likely you've finished your turn), a short pause ends it if you sound done and a
        longer one if you sound mid-sentence; without, any pause of QUIET seconds does."""
        before: list[np.ndarray] = []  # a little audio from just before the speech, so its first sound isn't lost
        spoken: list[np.ndarray] = []
        quiet = 0
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            if cancel is not None and cancel.is_set():
                return None
            try:
                frame = self.frames.get(timeout=0.5)
                self.last = time.monotonic()
            except queue.Empty:
                frame = None
                if self.watch and time.monotonic() - self.last > STALLED:
                    raise OSError(f"no sound from the microphone for {STALLED:.0f} seconds")
            if frame is None or len(frame) != FRAME:
                if not spoken and deadline and time.monotonic() > deadline:
                    return None
                continue
            probability = self.heard(frame)
            if not spoken:
                before = (before + [frame])[-10:]
                if probability >= START:
                    spoken = before
                    quiet = 0
                elif deadline and time.monotonic() > deadline:
                    return None
                continue
            spoken.append(frame)
            quiet = quiet + 1 if probability < END else 0
            paused = quiet * FRAME / RATE
            if len(spoken) * FRAME / RATE >= LONGEST:
                return np.concatenate(spoken)
            if finished is None:
                if paused >= QUIET:
                    return np.concatenate(spoken)
            elif paused >= MID_THOUGHT:
                return np.concatenate(spoken)
            elif quiet == int(PAUSE * RATE / FRAME) and finished(np.concatenate(spoken[:len(spoken) - quiet])) >= 0.5:
                return np.concatenate(spoken)


class Listener:
    def __init__(self, config: Config, log=print) -> None:
        from faster_whisper import WhisperModel

        self.config, self.log = config, log
        from . import embeddings

        embeddings.SERVER = "http://127.0.0.1:8765"  # the server holds the vectors; a copy here took up to 61 GB
        self.whisper = WhisperModel(config.whisper_model, device="auto", compute_type="default")
        self.mouth = Mouth(config.voice if config.voice in VOICES else VOICES[0], actions.persona(config)["speed"], log)
        self.pressed = threading.Event()  # time to listen: Super+R (SIGUSR1 from the shortcut) or the talk key held
        self.hush = threading.Event()  # stop speaking: a key pressed while Rigved talks
        self.busy = threading.Lock()  # one conversation at a time, whether the key or the name began it
        self.done = 0.0  # when the last conversation ended, so the name heard during it (or in Rigved's reply) is ignored
        self.holding = False  # whether it was the talk key, which is held while you speak
        self.let_go = threading.Event()  # the talk key came up
        self.shown = ("", 0.0, 0.0)  # state, level, when: what the orb was last told
        self.voiceprint = self._voiceprint()
        self.finished = self._turns()  # speech → how likely you've finished your turn; None: a fixed pause ends it
        self.interrupted = False  # you talked over Rigved, and what you said is waiting to be heard

    def _voiceprint(self):
        """Voice match, so hands-free it answers only you; None (everyone counts) if it can't be had."""
        try:
            from .voiceprint import Voiceprint

            voiceprint = Voiceprint(log=self.log)
        except Exception as error:
            self.log(f"Voice match is off ({type(error).__name__}: {error}); anyone's voice counts.")
            return None
        if not voiceprint.ready:
            self.log("Voice match learns your voice from what you say holding the talk key, or `rigveda listen --enroll`.")
        return voiceprint

    def _turns(self):
        try:
            from .turns import Turns

            return Turns(log=self.log).finished
        except Exception as error:
            self.log(f"Turn-taking model is off ({type(error).__name__}: {error}); a pause of {QUIET} s ends what you say.")
            return None

    def score(self, audio: np.ndarray) -> float | None:
        """How like you the speech sounds, None if that can't be told (voice match off, your voice not known yet)."""
        voiceprint = getattr(self, "voiceprint", None)
        return None if voiceprint is None else voiceprint.score(audio)

    @staticmethod
    def match() -> float:
        """How like you speech must sound to count as you: less when nothing is playing, so short or soft commands
        aren't lost when there's nothing to mistake for you."""
        return MATCH if audible() else QUIET_MATCH

    def dictating(self, began: float, ended: float, margin: float = 1.0) -> bool:
        """Whether Space was held for Claude Code's dictation while this was said (give or take `margin` seconds): then
        it was you, and not for Rigved."""
        holds = getattr(self, "holds", None)
        return holds is not None and holds.during(began, ended, margin)

    def elsewhere(self, began: float, ended: float) -> str:
        """Why speech from `began` to `ended` (time.monotonic()) was for something else, or "": Space held for Claude
        Code's dictation, or another app (a call, a meeting) using the microphone."""
        if self.dictating(began, ended):
            return "Space held: that was for Claude"
        if apps := recording():
            return f"{', '.join(apps)} using the microphone"
        return ""

    def keep(self, audio: np.ndarray, score: float | None) -> None:
        """Keep what was heard hands-free to train voice match on (`rigveda listen --train`): said holding Space for
        Claude's dictation, it's you for certain, with no labelling; anything else with its score, to be labelled."""
        voiceprint = getattr(self, "voiceprint", None)
        if voiceprint is not None:
            now = time.monotonic()
            voiceprint.heard(audio, "you" if self.dictating(now - len(audio) / RATE, now, margin=0) else "heard", score)

    def show(self, state: str, level: float = 0.0) -> None:
        """Tell the orb: idle, listening, thinking or speaking, and how loud. At most 30 times a second."""
        now = time.monotonic()
        if state == self.shown[0] and (now - self.shown[2] < 0.033 or abs(level - self.shown[1]) < 0.02):
            return
        self.shown = (state, level, now)
        try:
            partial = STATE.with_suffix(".part")
            partial.write_text(json.dumps({"state": state, "level": round(level, 3), "at": time.time()}))
            partial.replace(STATE)
        except OSError:
            pass

    def hear(self, audio: np.ndarray) -> str:
        pieces, _ = self.whisper.transcribe(audio, language="en", beam_size=5, initial_prompt=PROMPT,
                                            condition_on_previous_text=False, vad_filter=True)
        return " ".join(piece.text.strip() for piece in pieces).strip()

    def command(self, audio: np.ndarray, yours: bool = False) -> tuple[str, str | None]:
        """What was heard at the start of a stretch of speech, and the command in it: "" for the wake word alone, None
        if it didn't start with the name. Speech known to be yours is written down whole at once, which is quicker;
        anyone else's only at the start, to see if it begins with the name."""
        if yours:
            heard = self.hear(audio)
            return heard, actions.wake(heard)
        start = audio[: int(WAKE_WINDOW * RATE)]
        heard = self.hear(start)
        command = actions.wake(heard)
        if command is None or len(audio) == len(start):
            return heard, command
        whole = actions.wake(self.hear(audio))
        return heard, command if whole is None else whole

    def handle(self, command: str, typed: bool = False, before=lambda steps: None,
               searching=lambda: None) -> tuple[list[str], str]:
        """Carry out a command and remember it; what was done, and what Rigved says about it. `before` is told the
        steps before they're carried out, `searching` when an answer has to wait on the web."""
        try:
            steps = actions.understand(command, self.config)
            before(steps)
            self.log(f"{'Typed' if typed else 'Heard'}: {command!r} → "
                     f"{', '.join(f'{step.kind} {step.target!r}' for step in steps) or 'nothing'}")
            opened: list[dict] = []
            private: list = []  # answered from your files: kept from Claude
            reply = actions.carry_out(steps, self.config, self.log, opened=opened, searching=searching, private=private)
            did = [f"{step.kind} {step.target}".strip() for step in steps]
            self.log(f"Said: {reply}")
            if not any(step.kind == "forget" for step in steps):
                conversation.add(command, reply, "typed" if typed else "voice", opened[-1] if opened else None,
                                 private=bool(private))
        except Exception as error:  # keep going whatever went wrong
            self.log(f"Couldn't do {command!r}: {type(error).__name__}: {error}")
            did, reply = [f"failed: {type(error).__name__}"], actions.line(self.config, "broke")
        remember(command, did, reply, typed)
        return did, reply

    def respond(self, command: str, ears: Ears | None = None) -> list[str]:
        """Carry out a spoken command and answer aloud; what was done. Searching your files takes a few seconds, so
        Rigved says so first rather than going quiet. With `ears`, you can talk over the answer to stop it."""
        self.show("thinking")
        filler: list[threading.Thread] = []

        def before(steps: list) -> None:
            if any(step.kind == "ask" for step in steps):
                line = actions.line(self.config, "looking")
                filler.append(threading.Thread(target=lambda: (self.say(line), self.show("thinking")), daemon=True))
                filler[-1].start()

        def searching() -> None:
            if not filler:  # once, and not on top of "checking your files"
                line = actions.line(self.config, "web")
                filler.append(threading.Thread(target=lambda: (self.say(line), self.show("thinking")), daemon=True))
                filler[-1].start()

        did, reply = self.handle(command, before=before, searching=searching)
        for thread in filler:
            thread.join()
        if reply:
            self.say(reply, ears)
        else:  # done, and nothing to say about it (a film paused): just the tone
            Mouth.blip(rising=False)
            self.show("idle")
        return did

    @staticmethod
    def hold() -> list[str]:
        """Pause whatever is playing while you talk to Rigved, so a film's voices aren't taken for yours."""
        paused = actions.playing()
        for player in paused:
            actions.press(player, "Pause")
        return paused

    @staticmethod
    def release(paused: list[str], did: list[str]) -> None:
        """Play again what `hold` paused, unless you told it what to do with the playback yourself."""
        if not any(step.startswith("media") for step in did):
            for player in paused:
                actions.press(player, "Play")

    def typed(self) -> None:
        """Take typed commands from the orb's Voice tab (through the server, over a socket only you can open) and
        answer them in text rather than aloud. Runs here, not in the server, because opening apps needs the desktop
        session this service lives in."""
        listener = self

        class Typed(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                try:
                    command = json.loads(self.rfile.readline(4 * TYPED_LONGEST))["text"].strip()[:TYPED_LONGEST]
                except (ValueError, KeyError, TypeError, AttributeError):
                    return
                if not actions.words(command):
                    return
                listener.show("thinking")
                did, reply = listener.handle(actions.wake(command) or command, typed=True)
                listener.show("idle")
                self.wfile.write(json.dumps({"did": did, "said": reply or "Done."}).encode() + b"\n")

        SOCKET.unlink(missing_ok=True)  # left behind by a listener that didn't stop cleanly
        server = socketserver.ThreadingUnixStreamServer(str(SOCKET), Typed)
        os.chmod(SOCKET, 0o600)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def say(self, text: str, ears: Ears | None = None) -> None:
        """Speak, or if the speakers can't be had (another app can hold HDMI audio to itself, e.g. VLC passing a film's
        Dolby sound straight to the TV), show it as a notification. Pressing the key again cuts it short; with `ears`,
        so does you talking over it, once your voice is known (Rigved's own voice must not count)."""
        self.hush.clear()
        self.interrupted = False
        speaking = threading.Event()
        speaking.set()
        voiceprint = getattr(self, "voiceprint", None)
        watcher = None
        if ears is not None and voiceprint is not None and voiceprint.ready:
            watcher = threading.Thread(target=self.overheard, args=(ears, speaking), daemon=True)
            watcher.start()
        try:
            self.mouth.say(text, level=lambda value: self.show("speaking", value), stop=self.hush)
        except Exception as error:
            self.log(f"Couldn't speak ({type(error).__name__}: {error}); showing it instead.")
            subprocess.run(["notify-send", "--app-name=Rigveda", "Rigved", text], capture_output=True, timeout=5)
        finally:
            speaking.clear()
            if watcher is not None:
                watcher.join(timeout=1)
        self.show("idle")

    def overheard(self, ears: Ears, speaking: threading.Event) -> None:
        """While Rigved speaks, listen for you talking over it; then stop it, and keep what you said for the next turn."""
        voiceprint = getattr(self, "voiceprint", None)
        heard: list[np.ndarray] = []
        run = 0  # frames of speech in a row
        ears.clear()
        while speaking.is_set():
            try:
                frame = ears.frames.get(timeout=0.1)
            except queue.Empty:
                continue
            if len(frame) != FRAME:
                continue
            heard = (heard + [frame])[-60:]
            run = run + 1 if ears.speech(frame) >= START else 0
            if run * FRAME / RATE < INTERRUPT:
                continue
            spoken = heard[-(run + 10):]
            score = self.score(np.concatenate(spoken))
            run = 0
            if score is not None and score < INTERRUPT_MATCH and voiceprint is not None:
                voiceprint.heard(np.concatenate(spoken), "rigved")  # its own voice coming back, to train on
            if score is not None and score >= INTERRUPT_MATCH:
                self.log(f"You talked over me ({score:.2f}); listening.")
                self.interrupted = True
                ears.unread(spoken)
                self.hush.set()
                return

    def run(self) -> None:
        faulthandler.register(signal.SIGUSR2, all_threads=True)  # `systemctl --user kill -s USR2 rigveda-listen`: where each thread is
        self.show("idle")
        self.typed()
        if self.config.listen == "wake":
            from .keyboard import Holds

            self.holds = Holds()  # Space held: you're dictating to Claude Code, not talking to Rigved
            self.holds.start()
            threading.Thread(target=self.run_wake, daemon=True).start()
        self.run_key()  # the keys work in both modes

    def run_key(self) -> None:
        """Wait for the key; then open the microphone, hear one request, answer it, and close the microphone."""
        from .keyboard import Keyboard, name

        def tapped(*_) -> None:
            self.holding = False
            self.hush.set()
            self.pressed.set()

        def held() -> None:
            self.holding = True
            self.let_go.clear()
            self.hush.set()
            self.pressed.set()

        signal.signal(signal.SIGUSR1, tapped)
        Keyboard(self.config.talk_key, held, self.let_go.set, self.log).start()
        self.log(f"Ready. Hold {name(self.config.talk_key)} while you speak, or press Super+R"
                 + (', or say "Hey Rigved".' if self.config.listen == "wake" else "."))
        while True:
            while not self.pressed.wait(timeout=1):
                pass
            while self.pressed.is_set():  # used again while it was speaking: listen again straight away
                self.pressed.clear()
                with self.busy:
                    self.converse()
                self.done = time.monotonic()

    def converse(self) -> None:
        paused, did = self.hold(), []
        try:
            did = self.listen_and_respond()
        finally:
            self.release(paused, did)

    def listen_and_respond(self) -> list[str]:
        holding = self.holding
        self.show("listening")
        Mouth.blip()
        with Ears(level=lambda value: self.show("listening", value)) as ears:
            time.sleep(0.12)  # let the blip finish before listening
            ears.clear()
            if holding:  # until the key comes up
                audio = ears.record(until=self.let_go)
            else:
                audio = ears.next(timeout=ANSWER_WITHIN, cancel=self.pressed, finished=self.finished)
        if audio is None:  # nothing said, or the key pressed again to cancel
            self.pressed.clear()
            Mouth.blip(rising=False)
            self.show("idle")
            return []
        self.show("thinking")
        heard = self.hear(audio)
        command = actions.wake(heard)
        command = command if command else heard  # saying the name first is fine
        if not actions.words(command):
            self.show("idle")
            return []
        if getattr(self, "voiceprint", None) is not None:
            self.voiceprint.heard(audio, "you")  # said holding the key, so it's you
            self.voiceprint.learn(audio)
        return self.respond(command)

    def run_wake(self) -> None:
        """Listen hands-free alongside the keys; start again if the microphone goes away (a headset unplugged). While
        muted from the orb, the microphone is closed."""
        self.quiet = threading.Event()  # set while muted
        threading.Thread(target=self.watch_mute, daemon=True).start()
        while True:
            if self.quiet.is_set():
                time.sleep(0.5)
                continue
            try:
                self.listen_for_name()
            except Exception as error:
                self.log(f"Stopped listening for the name ({type(error).__name__}: {error}); opening the microphone again.")
                time.sleep(1)

    def watch_mute(self) -> None:
        while True:
            if muted() != self.quiet.is_set():
                (self.quiet.set if muted() else self.quiet.clear)()
                self.log("Muted: not listening hands-free (the talk key still works)." if muted() else "Listening again.")
            time.sleep(0.5)

    def listen_for_name(self) -> None:
        with Ears(watch=True) as ears:
            self.log('Listening. Say "Rigved" or "Hey Rigved", then what you want.' if self.config.need_name else
                     'Listening to your voice: just say what you want (not while Space is held, which is for Claude).')
            while True:
                audio = ears.next(finished=getattr(self, "finished", None), cancel=getattr(self, "quiet", None))
                if audio is None:  # muted: close the microphone
                    return
                began = time.monotonic() - len(audio) / RATE
                if self.busy.locked() or began < self.done:  # said during a conversation, or Rigved talking
                    self.log("Passed over: said while Rigved was busy.")
                    continue
                score = self.score(audio)
                self.keep(audio, score)
                if score is not None and score < self.match():
                    self.log(f"Not your voice ({score:.2f}).")
                    continue
                if why := self.elsewhere(began, time.monotonic()):
                    self.log(f"Not for me ({why}).")
                    continue
                heard, command = self.command(audio, yours=score is not None)
                quick = command is None and score is not None and actions.quick(heard)  # "pause", in your voice
                if command is None and not quick:
                    # No name needed: anything in your voice that was said to Rigved. Until your voice is known, it
                    # takes the name, or the TV could talk to it.
                    if (self.config.need_name or score is None or not actions.meaningful(heard)
                            or not actions.addressed(heard, self.config)):
                        self.log(f"Not for me ({'voice unknown' if score is None else f'your voice, {score:.2f}'}): {heard!r}")
                        continue
                    command = heard
                if not self.busy.acquire(blocking=False):
                    continue
                try:
                    if quick:
                        self.respond(heard)
                    else:
                        self.answer(ears, command)
                finally:
                    self.done = time.monotonic()
                    self.busy.release()
                    ears.clear()

    def answer(self, ears: Ears, command: str) -> None:
        paused, did = self.hold(), []
        try:
            did = self.follow(ears, command)
        finally:
            self.release(paused, did)

    def follow(self, ears: Ears, command: str) -> list[str]:
        """Answer what followed the name, then carry on the conversation: what you say next needs no name, for as long
        as you're talking to Rigved. It ends when you go quiet for a while, say you're done, have something played (its
        sound would be taken for you) or turn to someone else. No tones: the orb shows when it's listening."""
        done: list[str] = []
        if not actions.meaningful(command):  # "Rigved" alone, or "Rigved, um": a tone says it's listening
            command = ""
            Mouth.blip()
        while True:
            if not command:
                command = self.request(ears, timeout=CONVERSATION if done else NAME_ALONE, check=bool(done))
                if not command:
                    self.show("idle")
                    return done
            did = self.respond(command, ears)
            done += did
            if any(step == "stop" or step.startswith("media") for step in did):
                return done
            command = ""

    def request(self, ears: Ears, timeout: float, check: bool = False) -> str:
        """What you say next, the name not needed; "" if you say nothing within `timeout` seconds or, with `check`,
        turn out to be talking to someone else. Other voices (the TV, someone in the room) are passed over."""
        if not self.interrupted:  # else what you said over Rigved is waiting in `ears`
            time.sleep(0.15)  # the last of Rigved's voice, still on its way back through the microphone
            ears.clear()
        self.interrupted = False
        self.show("listening")
        ears.level = lambda value: self.show("listening", value)
        deadline = time.monotonic() + timeout
        try:
            while (left := deadline - time.monotonic()) > 0:
                audio = ears.next(timeout=left, finished=getattr(self, "finished", None))
                if audio is None:
                    return ""
                score = self.score(audio)
                self.keep(audio, score)
                if score is not None and score < self.match():
                    self.log(f"Not your voice ({score:.2f}); still listening.")
                    continue
                if why := self.elsewhere(time.monotonic() - len(audio) / RATE, time.monotonic()):
                    self.log(f"Not for me ({why}); still listening.")
                    continue
                self.show("thinking")
                heard = self.hear(audio)
                command = actions.wake(heard)  # "Rigved" said again is fine
                command = heard if command is None else command
                if not actions.meaningful(command):  # nothing Whisper could make out, or just "hey"
                    self.show("listening")
                    continue
                if check and not actions.addressed(command, self.config):
                    self.log(f"Not said to me: {command!r}")
                    return ""
                return command
            return ""
        finally:
            ears.level = lambda value: None


def muted() -> bool:
    return MUTED.exists()


def mute(on: bool) -> bool:
    """Mute or unmute hands-free listening (the talk key still works); whether it's muted now. Until the next login."""
    if on:
        MUTED.touch()
    else:
        MUTED.unlink(missing_ok=True)
    return muted()


def streams(kind: str, own: bool = False) -> list[str]:
    """The apps with a running audio stream of `kind` ("Input": recording, "Output": playing), by name; Rigved's own,
    through PipeWire's ALSA plugin, only with `own`. A paused film's stream is idle, not running."""
    try:
        found = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=3).stdout
        nodes = json.loads(found or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    apps = []
    for node in nodes:
        info = node.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != f"Stream/{kind}/Audio" or info.get("state") != "running":
            continue
        app = props.get("application.name") or props.get("node.name") or "an app"
        if (own or "python" not in app.lower()) and app not in apps:
            apps.append(app)
    return apps


def recording() -> list[str]:
    """Other apps recording from the microphone right now (a call, a meeting, dictation)."""
    return streams("Input")


def audible() -> bool:
    """Whether anything is playing sound (a film, a video, music, Rigved itself) that could be taken for you."""
    return bool(streams("Output", own=True))


PASSAGE = """Hey Rigved, it's me. I'm reading this so you know my voice from anyone else's.
When a film is playing, or someone else is talking in the room, only my voice should count.
Rigved, pause the movie. Turn it up a little. Open my Downloads folder.
What's on my calendar this week, and did I pay the electricity bill?
The quick brown fox jumps over the lazy dog, and that's more than enough of that."""


def enroll(log=print) -> None:
    """Learn your voice from you reading a short passage, for hands-free listening to tell it apart."""
    from .voiceprint import ENOUGH, Voiceprint

    voiceprint = Voiceprint(log=log)
    print("Read this aloud at your normal pace, as you'd talk to Rigved:\n\n" + PASSAGE + "\n", flush=True)
    pieces: list[np.ndarray] = []
    heard, silences = 0.0, 0
    with Ears() as ears:
        ears.clear()
        while heard < 30 and silences < 2:
            audio = ears.next(timeout=8 if heard else 90)  # time to find the passage before starting
            if audio is None:
                silences += 1
                continue
            heard += len(audio) / RATE
            pieces += [audio[i:i + 3 * RATE] for i in range(0, len(audio), 3 * RATE) if len(audio) - i >= 1.5 * RATE]
            print(f"  {min(heard, 30):.0f} of 30 seconds", flush=True)
    if len(pieces) < ENOUGH:
        raise SystemExit("That wasn't enough speech to learn from. Run it again and read the whole passage.")
    voiceprint.enroll(pieces)
    for piece in pieces:
        voiceprint.heard(piece, "you")
    alike = voiceprint.enrolled @ voiceprint.enrolled.T
    print(f"Learned your voice from {len(pieces)} pieces (they agree {alike[np.triu_indices(len(alike), 1)].mean():.2f}). "
          "Hands-free Rigved now answers only you.")


def send(text: str, timeout: float = 180.0) -> dict:
    """Hand a typed command to the running listener; what it did and what it said. OSError if it isn't running."""
    import socket

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(SOCKET))
        connection.sendall(json.dumps({"text": text}).encode() + b"\n")
        with connection.makefile("rb") as replies:
            line = replies.readline()
    if not line:
        raise OSError("The listener didn't answer.")
    return json.loads(line)


def press() -> None:
    """What the keyboard shortcut does: tell the running listener the key was pressed."""
    result = subprocess.run(["systemctl", "--user", "kill", "--signal=SIGUSR1", "--kill-whom=main", "rigveda-listen.service"],
                            capture_output=True, text=True)
    if result.returncode:
        raise SystemExit("Rigveda isn't listening for the key. Start it with `rigveda listen --autostart`.")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
