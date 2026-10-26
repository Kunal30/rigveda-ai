"""Voice match: tell your voice from anyone else's (a film's actors, the TV, people in the room), so hands-free Rigved
answers only you.

A small speaker-recognition model (CAM++ from 3D-Speaker, run by sherpa-onnx; about 29 MB, downloaded the first time)
turns a stretch of speech into a fingerprint, and speech counts as yours when its fingerprint is close enough to the
ones it has of you. Those come from you reading a short passage (`rigveda listen --enroll`) and from what you say while
holding the talk key, which can only be you; the latter alone is enough after a few commands. Everything stays here.

Once there's enough of it, `rigveda listen --train` learns a model of you against everything else this microphone hears
(the film, the TV, Rigved's own voice, other people) from clips kept for that and labelled by you (`rigveda listen
--label`): a fingerprint's closeness to yours alone can't tell you from a voice it was never shown, which is how Tony
Stark on the TV scored 0.92 as you (2026-10-25). The model also weighs how loud the speech is: through the headset you
came in at -14 to -7 dBFS, the TV mostly at -34 to -16.
"""

from __future__ import annotations

import time
import wave
from pathlib import Path
from urllib.request import urlopen

import numpy as np

from .config import _xdg

RATE = 16_000
MODEL = "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"  # sic: the release's spelling
DIR = _xdg("XDG_DATA_HOME", ".local/share") / "rigveda" / "voice"
SAMPLES = DIR / "my-voice.npz"  # fingerprints of your speech: from the passage you read, and learned since
ENOUGH = 5  # fingerprints of you before speech is told apart; until then everyone counts as you
LEARNED = 40  # fingerprints learned from the talk key that are kept, the latest
SHORTEST = 0.3  # seconds; anything shorter says next to nothing about whose voice it is
LEARN_FROM = 1.5  # seconds; only speech at least this long is learned from
CLIPS = DIR / "clips"  # what was heard, to train and measure on: you/ (the talk key, the passage, and what you labelled
# yours), others/ (labelled not yours), rigved/ (its own voice coming back while it talks) and heard/ (everything else
# hands-free, not yet labelled, its score in the name); you agreed to it on 2026-10-25
KEEP_CLIPS = 2000  # of each kind, the latest
TRAINED = DIR / "my-voice-model.npz"  # you against everything else, from `rigveda listen --train`
MATCH = 0.45  # lowered from 0.5 on 2026-10-25 so softer commands count, still above Rigved's own voice through the TV (0.44); with a trained model, its probability that it's you; before, how close (cosine, -1 to 1) speech must be to your fingerprint to count as you: on 2026-10-25 a film scored up to 0.42, you 0.51 to 0.88


class Voiceprint:
    def __init__(self, path: Path = SAMPLES, log=print) -> None:
        import sherpa_onnx

        model = DIR / MODEL
        if not model.exists():
            DIR.mkdir(parents=True, exist_ok=True)
            log(f"Downloading the voice-match model ({MODEL}) to {DIR}…")
            partial = model.with_suffix(".part")
            with urlopen(MODEL_URL + MODEL, timeout=60) as response, partial.open("wb") as file:
                while chunk := response.read(1 << 20):
                    file.write(chunk)
            partial.rename(model)
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=1, provider="cpu"))
        self.path, self.loaded = path, None
        empty = np.zeros((0, self.extractor.dim), dtype=np.float32)
        self.enrolled, self.learned = empty, empty
        self.trained, self.model = path.with_name(TRAINED.name), None
        self.model_loaded = None
        self._load()

    def _load(self) -> None:
        """Read what's known of your voice, again if `rigveda listen --enroll` changed it since."""
        try:
            changed = self.path.stat().st_mtime_ns
        except OSError:
            changed = None
        if changed is not None and changed != self.loaded:
            with np.load(self.path) as saved:
                self.enrolled, self.learned = saved["enrolled"], saved["learned"]
        self.loaded = changed
        self.me = self._mean()
        try:
            changed = self.trained.stat().st_mtime_ns
        except OSError:
            changed = None
        if changed != self.model_loaded:
            self.model = None
            if changed is not None:
                with np.load(self.trained) as saved:
                    if len(saved["weights"]) == self.extractor.dim + 1:  # else from before loudness counted
                        self.model = (saved["weights"], float(saved["bias"]))
            self.model_loaded = changed

    @property
    def ready(self) -> bool:
        """Whether it knows your voice well enough to tell it apart."""
        return len(self.enrolled) + len(self.learned) >= ENOUGH

    def fingerprint(self, audio: np.ndarray) -> np.ndarray:
        stream = self.extractor.create_stream()
        stream.accept_waveform(RATE, audio.astype(np.float32))
        stream.input_finished()
        found = np.array(self.extractor.compute(stream), dtype=np.float32)
        return found / (np.linalg.norm(found) or 1.0)

    def score(self, audio: np.ndarray) -> float | None:
        """How like you the speech sounds: the trained model's probability that it's you, else closeness to your
        fingerprint (cosine, 1 is identical); None if it can't be told yet or it's too short."""
        self._load()
        if not self.ready or len(audio) < SHORTEST * RATE:
            return None
        found = self.fingerprint(audio)
        if self.model is not None:
            return probability(features(found, audio), *self.model)
        return float(found @ self.me)

    def mine(self, audio: np.ndarray) -> bool:
        """Whether speech is yours; until your voice is known, or when it's too short to tell, it's given the benefit."""
        score = self.score(audio)
        return score is None or score >= MATCH

    def learn(self, audio: np.ndarray) -> None:
        """Learn from speech that can only be yours (said holding the talk key), if it's long enough and, once your
        voice is known, sounds like you: a film playing in the background shouldn't be learned as you."""
        if len(audio) < LEARN_FROM * RATE or (self.ready and not self.mine(audio)):
            return
        self._load()
        self.learned = np.vstack([self.learned, self.fingerprint(audio)])[-LEARNED:]
        self._save()

    def heard(self, audio: np.ndarray, kind: str, score: float | None = None) -> None:
        """Keep a clip to train on: "you", "rigved" or "heard" (anything else, with its score)."""
        keep(audio, kind, score, self.path.parent / CLIPS.name)

    def enroll(self, pieces: list[np.ndarray]) -> None:
        """Start again from speech you read out for it."""
        self.enrolled = np.vstack([self.fingerprint(piece) for piece in pieces])
        self.learned = self.learned[:0]
        self._save()

    def _mean(self) -> np.ndarray:
        both = np.vstack([self.enrolled, self.learned])
        if not len(both):
            return np.zeros(self.extractor.dim, dtype=np.float32)
        mean = both.mean(axis=0)
        return mean / (np.linalg.norm(mean) or 1.0)

    def _save(self) -> None:
        self.me = self._mean()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_name(self.path.stem + ".part.npz")
        np.savez(partial, enrolled=self.enrolled, learned=self.learned)
        partial.replace(self.path)
        self.path.chmod(0o600)
        self.loaded = self.path.stat().st_mtime_ns


def keep(audio: np.ndarray, kind: str, score: float | None = None, clips: Path = CLIPS) -> None:
    """Save a clip as 16-bit WAV under clips/<kind>/, only for you to read; the oldest go past KEEP_CLIPS."""
    if len(audio) < SHORTEST * RATE:
        return
    try:
        folder = clips / kind
        folder.mkdir(parents=True, exist_ok=True)
        clips.chmod(0o700)
        path = folder / (f"{time.time_ns()}" + ("" if score is None else f"_{score:.2f}") + ".wav")
        with wave.open(str(path), "wb") as file:
            file.setnchannels(1)
            file.setsampwidth(2)
            file.setframerate(RATE)
            file.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        path.chmod(0o600)
        for old in sorted(folder.glob("*.wav"))[:-KEEP_CLIPS]:
            old.unlink()
    except OSError:
        pass  # a full disk shouldn't stop Rigved listening


def clip(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as file:
        return np.frombuffer(file.readframes(file.getnframes()), dtype="<i2").astype(np.float32) / 32767


def level(audio: np.ndarray) -> float:
    """How loud the speech is, in dBFS: the 90th percentile of its 32 ms frames, so pauses don't count."""
    frames = audio[: len(audio) // 512 * 512].reshape(-1, 512)
    if not len(frames):
        return -100.0
    return float(20 * np.log10(np.percentile(np.sqrt((frames ** 2).mean(axis=1)), 90) + 1e-9))


def features(fingerprint: np.ndarray, audio: np.ndarray) -> np.ndarray:
    """What the trained model looks at: whose voice it sounds like, and how loud it is (about 0 to 2 for speech)."""
    return np.r_[fingerprint, (level(audio) + 40) / 15].astype(np.float32)


def probability(fingerprints: np.ndarray, weights: np.ndarray, bias: float):
    value = 1 / (1 + np.exp(-(fingerprints @ weights + bias)))
    return float(value) if np.ndim(value) == 0 else value


def fit(you: np.ndarray, others: np.ndarray, strength: float = 0.01, steps: int = 25) -> tuple[np.ndarray, float]:
    """Logistic regression, you (1) against everyone else (0), each side weighed equally however many clips it has.
    Fitted by Newton's method on standardized features (loudness is on another scale from the fingerprint), then turned
    back into weights for the features as they come."""
    x = np.vstack([you, others]).astype(np.float64)
    mean, spread = x.mean(axis=0), x.std(axis=0) + 1e-6
    z = np.c_[(x - mean) / spread, np.ones(len(x))]
    y = np.r_[np.ones(len(you)), np.zeros(len(others))]
    balance = np.r_[np.full(len(you), 0.5 / len(you)), np.full(len(others), 0.5 / len(others))]
    penalty = np.full(z.shape[1], strength)
    penalty[-1] = 0.0  # the bias isn't held back
    w = np.zeros(z.shape[1])
    for _ in range(steps):
        p = 1 / (1 + np.exp(-np.clip(z @ w, -30, 30)))
        gradient = z.T @ ((p - y) * balance) + penalty * w
        hessian = (z * (balance * p * (1 - p))[:, None]).T @ z + np.diag(penalty + 1e-9)
        step = np.linalg.solve(hessian, gradient)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    weights = w[:-1] / spread
    return weights.astype(np.float32), float(w[-1] - weights @ mean)


def train(log=print, folds: int = 5, clips: Path = CLIPS, voiceprint: Voiceprint | None = None) -> bool:
    """Learn you against everything else from the kept clips, test it on clips it didn't learn from, and use it only if
    it tells you apart better than closeness to your fingerprint does. Whether it's now in use."""
    voiceprint = voiceprint or Voiceprint(log=log)
    yours = sorted((clips / "you").glob("*.wav"))
    others = sorted((clips / "rigved").glob("*.wav")) + sorted((clips / "others").glob("*.wav"))
    log(f"Clips: {len(yours)} of you, {len(others)} of others "
        f"({len(list((clips / 'rigved').glob('*.wav')))} of Rigved's own voice), "
        f"{len(list((clips / 'heard').glob('*.wav')))} not labelled yet.")
    if len(yours) < 20 or len(others) < 20:
        log("Not enough to learn from yet: it takes 20 of each. Hold Right Ctrl for a few more commands, leave it "
            "listening hands-free while a film or the TV plays, then label what it heard: `rigveda listen --label`.")
        return False
    you_audio, rest_audio = [clip(path) for path in yours], [clip(path) for path in others]
    you_print = np.vstack([voiceprint.fingerprint(audio) for audio in you_audio])
    rest_print = np.vstack([voiceprint.fingerprint(audio) for audio in rest_audio])
    you = np.vstack([features(found, audio) for found, audio in zip(you_print, you_audio)])
    rest = np.vstack([features(found, audio) for found, audio in zip(rest_print, rest_audio)])

    # Each clip scored by a model that never saw it.
    rng = np.random.default_rng(0)
    you_fold, rest_fold = rng.integers(folds, size=len(you)), rng.integers(folds, size=len(rest))
    you_p, rest_p = np.zeros(len(you)), np.zeros(len(rest))
    you_cos, rest_cos = np.zeros(len(you)), np.zeros(len(rest))
    for fold in range(folds):
        model = fit(you[you_fold != fold], rest[rest_fold != fold])
        you_p[you_fold == fold] = probability(you[you_fold == fold], *model)
        rest_p[rest_fold == fold] = probability(rest[rest_fold == fold], *model)
        mean = you_print[you_fold != fold].mean(axis=0)
        mean /= np.linalg.norm(mean) or 1.0
        you_cos[you_fold == fold] = you_print[you_fold == fold] @ mean
        rest_cos[rest_fold == fold] = rest_print[rest_fold == fold] @ mean

    def errors(you_scores, rest_scores) -> tuple[float, float]:
        return float((you_scores < MATCH).mean()), float((rest_scores >= MATCH).mean())

    missed, taken = errors(you_p, rest_p)
    missed_before, taken_before = errors(you_cos, rest_cos)
    log(f"Trained model: misses you {missed:.1%}, takes someone else for you {taken:.1%}.")
    log(f"Before (closeness to your fingerprint): misses you {missed_before:.1%}, takes someone else for you {taken_before:.1%}.")
    if missed + taken > missed_before + taken_before:
        log("Not better than before, so it isn't used. More clips will help.")
        return False
    weights, bias = fit(you, rest)
    partial = voiceprint.trained.with_name(voiceprint.trained.stem + ".part.npz")
    np.savez(partial, weights=weights, bias=bias, you=len(you), others=len(rest), at=time.time())
    partial.replace(voiceprint.trained)
    voiceprint.trained.chmod(0o600)
    log(f"In use now: Rigved picks it up on the next thing it hears ({voiceprint.trained}).")
    return True


def label(log=print, clips: Path = CLIPS, ask=input, play=None, service: str | None = "rigveda-listen.service") -> None:
    """Play each clip heard hands-free and say whose it is, for `rigveda listen --train` to learn from. The listener is
    paused meanwhile, or it would hear the clips again (and act on a "Hey Rigved" in one)."""
    import subprocess

    waiting = sorted((clips / "heard").glob("*.wav"))
    if not waiting:
        log("Nothing to label: no clips heard hands-free since the last time.")
        return
    play = play or (lambda path: subprocess.run(["pw-play", str(path)], capture_output=True, timeout=30))
    listening = service is not None and subprocess.run(["systemctl", "--user", "is-active", "-q", service]).returncode == 0
    if listening:
        subprocess.run(["systemctl", "--user", "stop", service], check=False)
    counts = {"you": 0, "others": 0}
    try:
        log(f"{len(waiting)} clips. After each: y = me, n = not me, r = again, s = skip, q = stop.")
        for number, path in enumerate(waiting, 1):
            while True:
                play(path)
                answer = ask(f"[{number}/{len(waiting)}] me? ").strip().lower()[:1]
                if answer != "r":
                    break
            if answer == "q":
                break
            if answer in ("y", "n"):
                kind = "you" if answer == "y" else "others"
                (clips / kind).mkdir(exist_ok=True)
                path.replace(clips / kind / path.name)
                counts[kind] += 1
    finally:
        if listening:
            subprocess.run(["systemctl", "--user", "start", service], check=False)
    log(f"Labelled {counts['you']} as you and {counts['others']} as others. Now: `rigveda listen --train`.")
