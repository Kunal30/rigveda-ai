"""Talk to Rigveda: hold Right Ctrl while you speak (or tap Super+R, or in wake-word mode say "Rigved"), and it answers
aloud. Or type to it in the orb's Voice tab, and it answers there in text.

Everything happens on this computer. With the keys, the microphone is closed until you use one, and closes again when
you let go (or, after Super+R, once you stop talking). In wake-word mode it stays open, and only speech that starts with "Rigved" goes further than Whisper.
Silero (bundled with faster-whisper) notices when someone speaks, Whisper writes it down, and Kokoro speaks the reply.
The orb follows along through a small state file: it pops when the microphone opens and pulses with both voices.
"""

from __future__ import annotations

import json
import os
import queue
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
from .config import Config, _xdg

RATE = 16_000
FRAME = 512  # 32 ms, what Silero expects at 16 kHz
START, END = 0.5, 0.3  # speech probability that starts and ends a stretch of speech
QUIET = 0.8  # seconds of quiet that end it
LONGEST = 15.0  # seconds; longer speech is cut off (a film playing, say)
WAKE_WINDOW = 4.0  # seconds; the wake word must come first, so only this much is read before deciding
PROMPT = "Rigved, open Downloads. Hey Rigved."  # nudges Whisper to spell the name the same way every time
# Kokoro's men's voices: American (am_), British (bm_) and Hindi (hm_, English with an Indian accent).
VOICES = ("am_michael", "am_fenrir", "am_puck", "am_eric", "am_onyx", "bm_george", "bm_fable", "hm_omega", "hm_psi")
MODEL_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "rigveda" / "voice"
MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
MODEL_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
STATE = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "rigveda-voice.json"  # what the orb shows, live
ANSWER_WITHIN = 6.0  # seconds after the key to start talking
HISTORY = _xdg("XDG_DATA_HOME", ".local/share") / "rigveda" / "voice-history.jsonl"  # what you said and what it said
KEEP = 1000  # conversations kept in the history
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
        """Speak, telling `level` how loud it is as it goes; stop early if `stop` is set."""
        import sounddevice

        # American voices read American English; the others say words like "Tathaastu" better as British.
        lang = "en-us" if self.voice.startswith("a") else "en-gb"
        audio, rate = self.kokoro.create(text, voice=self.voice, speed=self.speed, lang=lang)
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

    def __init__(self, level=lambda value: None) -> None:
        import onnxruntime
        import sounddevice
        from faster_whisper.vad import get_assets_path

        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = options.intra_op_num_threads = 1
        options.log_severity_level = 4
        self.vad = onnxruntime.InferenceSession(str(Path(get_assets_path()) / "silero_vad_v6.onnx"),
                                                providers=["CPUExecutionProvider"], sess_options=options)
        self.level = level  # told how loud each moment of speech is, 0 to 1
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
        return self

    def __exit__(self, *_) -> None:
        self.stream.stop()
        self.stream.close()

    def clear(self) -> None:
        """Forget what the microphone picked up meanwhile, e.g. while Rigveda was speaking."""
        while not self.frames.empty():
            self.frames.get_nowait()
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

    def next(self, timeout: float | None = None, cancel: threading.Event | None = None) -> np.ndarray | None:
        """The next stretch of speech, or None if nobody starts speaking within `timeout` seconds or `cancel` is set."""
        before: list[np.ndarray] = []  # a little audio from just before the speech, so its first sound isn't lost
        spoken: list[np.ndarray] = []
        quiet = 0
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            if cancel is not None and cancel.is_set():
                return None
            try:
                frame = self.frames.get(timeout=0.5)
            except queue.Empty:
                frame = None
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
            if quiet * FRAME / RATE >= QUIET or len(spoken) * FRAME / RATE >= LONGEST:
                return np.concatenate(spoken)


class Listener:
    def __init__(self, config: Config, log=print) -> None:
        from faster_whisper import WhisperModel

        self.config, self.log = config, log
        self.whisper = WhisperModel(config.whisper_model, device="auto", compute_type="default")
        self.mouth = Mouth(config.voice if config.voice in VOICES else VOICES[0], actions.persona(config)["speed"], log)
        self.pressed = threading.Event()  # time to listen: Super+R (SIGUSR1 from the shortcut) or the talk key held
        self.holding = False  # whether it was the talk key, which is held while you speak
        self.let_go = threading.Event()  # the talk key came up
        self.shown = ("", 0.0, 0.0)  # state, level, when: what the orb was last told

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

    def command(self, audio: np.ndarray) -> str | None:
        """The command in a stretch of speech, "" for the wake word alone, None if it wasn't for Rigveda."""
        start = audio[: int(WAKE_WINDOW * RATE)]
        command = actions.wake(self.hear(start))
        if command is None or len(audio) == len(start):
            return command
        whole = actions.wake(self.hear(audio))
        return command if whole is None else whole

    def handle(self, command: str, typed: bool = False) -> tuple[list[str], str]:
        """Carry out a command and remember it; what was done, and what Rigved says about it."""
        try:
            steps = actions.understand(command, self.config)
            self.log(f"{'Typed' if typed else 'Heard'}: {command!r} → "
                     f"{', '.join(f'{step.kind} {step.target!r}' for step in steps) or 'nothing'}")
            opened: list[dict] = []
            reply = actions.carry_out(steps, self.config, self.log, opened=opened)
            did = [f"{step.kind} {step.target}".strip() for step in steps]
            self.log(f"Said: {reply}")
            if not any(step.kind == "forget" for step in steps):
                conversation.add(command, reply, "typed" if typed else "voice", opened[-1] if opened else None)
        except Exception as error:  # keep going whatever went wrong
            self.log(f"Couldn't do {command!r}: {type(error).__name__}: {error}")
            did, reply = [f"failed: {type(error).__name__}"], actions.line(self.config, "broke")
        remember(command, did, reply, typed)
        return did, reply

    def respond(self, command: str) -> None:
        self.show("thinking")
        self.say(self.handle(command)[1])

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
                self.wfile.write(json.dumps({"did": did, "said": reply}).encode() + b"\n")

        SOCKET.unlink(missing_ok=True)  # left behind by a listener that didn't stop cleanly
        server = socketserver.ThreadingUnixStreamServer(str(SOCKET), Typed)
        os.chmod(SOCKET, 0o600)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def say(self, text: str) -> None:
        """Speak, or if the speakers can't be had (another app can hold HDMI audio to itself, e.g. VLC passing a film's
        Dolby sound straight to the TV), show it as a notification. Pressing the key again cuts it short."""
        self.pressed.clear()
        try:
            self.mouth.say(text, level=lambda value: self.show("speaking", value), stop=self.pressed)
        except Exception as error:
            self.log(f"Couldn't speak ({type(error).__name__}: {error}); showing it instead.")
            subprocess.run(["notify-send", "--app-name=Rigveda", "Rigved", text], capture_output=True, timeout=5)
        self.show("idle")

    def run(self) -> None:
        self.show("idle")
        self.typed()
        if self.config.listen == "wake":
            self.run_wake()
        else:
            self.run_key()

    def run_key(self) -> None:
        """Wait for the key; then open the microphone, hear one request, answer it, and close the microphone."""
        from .keyboard import Keyboard, name

        def tapped(*_) -> None:
            self.holding = False
            self.pressed.set()

        def held() -> None:
            self.holding = True
            self.let_go.clear()
            self.pressed.set()

        signal.signal(signal.SIGUSR1, tapped)
        Keyboard(self.config.talk_key, held, self.let_go.set, self.log).start()
        self.log(f"Ready. Hold {name(self.config.talk_key)} while you speak, or press Super+R.")
        while True:
            while not self.pressed.wait(timeout=1):
                pass
            while self.pressed.is_set():  # used again while it was speaking: listen again straight away
                self.pressed.clear()
                self.converse()

    def converse(self) -> None:
        holding = self.holding
        self.show("listening")
        Mouth.blip()
        with Ears(level=lambda value: self.show("listening", value)) as ears:
            time.sleep(0.12)  # let the blip finish before listening
            ears.clear()
            if holding:  # until the key comes up
                audio = ears.record(until=self.let_go)
            else:
                audio = ears.next(timeout=ANSWER_WITHIN, cancel=self.pressed)
        if audio is None:  # nothing said, or the key pressed again to cancel
            self.pressed.clear()
            Mouth.blip(rising=False)
            self.show("idle")
            return
        self.show("thinking")
        heard = self.hear(audio)
        command = actions.wake(heard)
        command = command if command else heard  # saying the name first is fine
        if not actions.words(command):
            self.show("idle")
            return
        self.respond(command)

    def run_wake(self) -> None:
        with Ears() as ears:
            self.log('Listening. Say "Rigved" or "Hey Rigved", then what you want.')
            while True:
                audio = ears.next()
                command = self.command(audio)
                if command is None:
                    continue
                if not command:  # just the name: answer, then listen for the request
                    greeting = actions.line(self.config, "greet")
                    remember("Rigved", [], greeting)
                    self.say(greeting)
                    ears.clear()
                    self.show("listening")
                    audio = ears.next(timeout=8)
                    if audio is None:
                        self.show("idle")
                        continue
                    command = self.hear(audio)
                    command = actions.wake(command) or command  # "Rigved" said again is fine
                    if not actions.words(command):
                        self.show("idle")
                        continue
                self.respond(command)
                ears.clear()


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
