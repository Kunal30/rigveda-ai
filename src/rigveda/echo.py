"""Rigved's own voice, heard back through the microphone, told apart from yours.

Rigved speaks through the TV, which plays it about a second late and, at full volume, about as loud at the headset
microphone as you are (its voice -15 dBFS, yours -12), so loudness can't tell them apart. Echo cancellation was tried
and dropped (2026-10-25): the headset and the TV keep separate clocks, and PipeWire's module left the microphone deaf.
This doesn't cancel anything. Rigved knows what it played, so for each moment the microphone hears it asks whether
that, a little late and a little muffled, explains it. When the microphone hears clearly more, in more than a few
moments out of half a second, someone else is talking: you, talking over it.

Measured on two recordings of Rigved through the TV with your own clips mixed in at their real loudness (2026-10-30):
half-seconds of its voice alone were never taken for you; 85% of yours over it were caught.
"""

from __future__ import annotations

import collections
import threading
import time

import numpy as np

from .config import _xdg

RATE = 16_000
FRAME = 512  # the microphone's frames: 32 ms
PATH = _xdg("XDG_DATA_HOME", ".local/share") / "rigveda" / "voice" / "echo.npz"  # what it learned about the TV
EDGES = np.unique(np.geomspace(4, 257, 25).astype(int))  # 24 bands of a frame's spectrum, finer at the low end
BEFORE, AFTER = 7, 1  # frames: what was played this much earlier (the room's echo) or later (timing) can explain a frame
EXTRA = 8.0  # dB above what Rigved's voice explains, across the middle band, for a frame to count as more than it
SHARE = 0.4  # and this share of the last WINDOW frames, for it to be someone talking
WINDOW = 16  # frames: half a second
DELAY = 0.45  # seconds from playing a sound to hearing it, until it's measured (it was 0.91 to 0.96 s with the TV,
# counted from the microphone's start; how much that is from `sounddevice.play` depends on the buffers)
FARTHEST = 1.6  # seconds: the longest delay looked for
KEEP = 40.0  # seconds of what was played and heard kept for learning
QUIET = 1e-7  # band energy of a frame with nothing played


def bands(frames: np.ndarray) -> np.ndarray:
    """Each frame's energy in the 24 bands; `frames` is (n, FRAME)."""
    spectrum = np.abs(np.fft.rfft(frames * np.hanning(FRAME), axis=1)) ** 2
    return np.stack([spectrum[:, low:high].sum(1) for low, high in zip(EDGES[:-1], EDGES[1:])], 1)


class Echo:
    """What Rigved played and the microphone heard, to say of each frame heard whether it's more than Rigved's voice."""

    def __init__(self, path=PATH, log=print) -> None:
        self.path, self.log = path, log
        self.lock = threading.Lock()
        self.played: collections.deque[tuple[float, np.ndarray]] = collections.deque()  # (when it began, at 16 kHz)
        self.heard: collections.deque[tuple[float, np.ndarray]] = collections.deque(maxlen=int(KEEP * RATE / FRAME))
        self.delay, self.gain, self.floor = DELAY, None, np.full(len(EDGES) - 1, 1e-9)
        try:
            with np.load(path) as saved:
                self.delay, self.gain, self.floor = float(saved["delay"]), saved["gain"], saved["floor"]
        except (OSError, KeyError, ValueError):
            pass

    @property
    def ready(self) -> bool:
        """Whether it knows the TV well enough to judge: after the first thing Rigved says."""
        return self.gain is not None

    def play(self, audio: np.ndarray, rate: int, at: float | None = None) -> None:
        """Rigved began playing `audio` at `at` (time.monotonic())."""
        at = time.monotonic() if at is None else at
        audio = np.asarray(audio, dtype=np.float32).ravel()
        if rate != RATE:
            audio = np.interp(np.arange(0, len(audio), rate / RATE), np.arange(len(audio)), audio).astype(np.float32)
        with self.lock:
            self.played.append((at, audio))
            while self.played and self.played[0][0] + len(self.played[0][1]) / RATE < at - KEEP:
                self.played.popleft()

    def hear(self, frame: np.ndarray, at: float) -> None:
        """The microphone sent `frame`, ending at `at`; from its own thread, so this only keeps it. With the key and
        hands-free both listening, the same moment comes twice: it's kept once."""
        if not self.heard or at - self.heard[-1][0] > FRAME / RATE / 2:
            self.heard.append((at, frame))

    def reference(self, start: float, length: int) -> np.ndarray:
        """What Rigved played from `start` (time.monotonic()) for `length` samples; silence where it played nothing."""
        out = np.zeros(length, dtype=np.float32)
        with self.lock:
            pieces = list(self.played)
        for began, audio in pieces:
            offset = int(round((began - start) * RATE))
            low, high = max(offset, 0), min(offset + len(audio), length)
            if low < high:
                out[low:high] += audio[low - offset:high - offset]
        return out

    def expected(self, at: float) -> np.ndarray | None:
        """Band energies Rigved's own voice could put in the frame heard ending at `at`; None if it played nothing
        that could reach it."""
        start = at - FRAME / RATE - self.delay - BEFORE * FRAME / RATE
        played = self.reference(start, (BEFORE + 1 + AFTER) * FRAME)
        energy = bands(played.reshape(-1, FRAME)).max(0)
        return None if energy.sum() < QUIET else energy

    def extra(self, frame: np.ndarray, at: float) -> bool | None:
        """Whether the frame ending at `at` holds clearly more than Rigved's own voice; None if Rigved played nothing
        that could be in it, or it doesn't know the TV yet."""
        if self.gain is None or (expected := self.expected(at)) is None:
            return None
        heard = bands(np.asarray(frame, dtype=np.float32)[None, :])[0]
        return float(np.median(10 * np.log10((heard + 1e-12) / (expected * self.gain + self.floor + 1e-12)))) > EXTRA

    def learn(self, since: float, until: float) -> bool:
        """Measure the TV again from what was played and heard between `since` and `until` (time.monotonic()): how late
        and how loud Rigved's voice comes back. Whether it learned anything (it needs a clear echo to go on)."""
        heard = [(at, frame) for at, frame in list(self.heard) if since <= at <= until + FARTHEST]
        if len(heard) * FRAME / RATE < 2.0:
            return False
        first = heard[0][0] - FRAME / RATE
        mic = np.concatenate([frame for _, frame in heard])
        played = self.reference(first - FARTHEST, len(mic) + int(FARTHEST * RATE))
        delay, fit = best_delay(mic, played)
        if fit < 0.5:
            self.log(f"Couldn't hear my own voice clearly enough to learn from (fit {fit:.2f}).")
            return False
        delay = FARTHEST - delay  # `played` began FARTHEST before the first frame heard
        lag = int(round((FARTHEST - delay) * RATE))
        if lag < BEFORE * FRAME:  # later than it can look back for its echo
            return False
        mic_bands = bands(mic[: len(mic) // FRAME * FRAME].reshape(-1, FRAME))
        aligned = played[lag - BEFORE * FRAME:]
        spread = [bands(aligned[k * FRAME:][: len(mic_bands) * FRAME].reshape(-1, FRAME))
                  for k in range(BEFORE + 1 + AFTER) if len(aligned) >= (k + len(mic_bands)) * FRAME]
        if len(spread) < BEFORE + 1 + AFTER:
            return False
        expected = np.max(spread, 0)
        loud = expected.sum(1) > 1e-4 * expected.sum(1).max()
        if loud.sum() < 20:
            return False
        gain = np.median(mic_bands[loud] / (expected[loud] + 1e-12), 0)  # the median: you talking over it now and then
        floor = np.percentile(mic_bands, 10, 0)  # doesn't move it much
        self.gain = gain if self.gain is None else np.exp(0.7 * np.log(self.gain) + 0.3 * np.log(gain + 1e-12))
        self.delay, self.floor = delay, floor
        self.save()
        self.log(f"My voice comes back {delay * 1000:.0f} ms late (fit {fit:.2f}).")
        return True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_name(self.path.stem + ".part.npz")
        np.savez(partial, delay=self.delay, gain=self.gain, floor=self.floor)
        partial.replace(self.path)


class Judge:
    """One microphone's running verdict: whether, over the last half second, someone has been talking over Rigved."""

    def __init__(self, echo: Echo) -> None:
        self.echo = echo
        self.flags: collections.deque[bool] = collections.deque(maxlen=WINDOW)

    def only_rigved(self, frame: np.ndarray, at: float) -> bool:
        """Whether what's heard now is no more than Rigved's own voice coming back (so not speech to listen to)."""
        extra = self.echo.extra(frame, at)
        if extra is None:
            self.flags.clear()
            return False
        self.flags.append(extra)
        return sum(self.flags) < SHARE * WINDOW


def best_delay(mic: np.ndarray, played: np.ndarray, hop: int = 16) -> tuple[float, float]:
    """How many seconds into `played` the microphone's sound lines up best, and how well (correlation of loudness)."""
    def envelope(audio: np.ndarray) -> np.ndarray:
        return np.sqrt((audio[: len(audio) // hop * hop].reshape(-1, hop) ** 2).mean(1))

    heard, sent = envelope(mic), envelope(played)
    heard = (heard - heard.mean()) / (heard.std() + 1e-9)
    best, fit = 0, -1.0
    for start in range(0, len(sent) - len(heard) + 1):
        window = sent[start:start + len(heard)]
        if window.std() < 1e-6:
            continue
        score = float(np.dot(heard, (window - window.mean()) / window.std()) / len(heard))
        if score > fit:
            best, fit = start, score
    return best * hop / RATE, fit
