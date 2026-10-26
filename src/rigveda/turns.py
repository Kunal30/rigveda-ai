"""Turn-taking: whether you've finished what you were saying, judged from how you said it, not from a fixed pause.

A pause after "remind me to call my mom and" isn't the end of your turn; one after "what time is it?" is. Smart Turn
(Pipecat's, BSD licence; 8 MB, downloaded the first time) hears that from the last 8 seconds of your speech in about
50 ms on the CPU, so Rigved can answer a finished thought quickly and wait out a pause in the middle of one.
"""

from __future__ import annotations

from urllib.request import urlopen

import numpy as np

from .config import _xdg

RATE = 16_000
MODEL = "smart-turn-v3.2-cpu.onnx"
MODEL_URL = "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/"
DIR = _xdg("XDG_DATA_HOME", ".local/share") / "rigveda" / "voice"
SECONDS = 8  # what the model hears: the end of the speech, with silence before it if there's less


class Turns:
    def __init__(self, log=print) -> None:
        import onnxruntime
        from faster_whisper.feature_extractor import FeatureExtractor

        model = DIR / MODEL
        if not model.exists():
            DIR.mkdir(parents=True, exist_ok=True)
            log(f"Downloading the turn-taking model ({MODEL}) to {DIR}…")
            partial = model.with_suffix(".part")
            with urlopen(MODEL_URL + MODEL, timeout=60) as response, partial.open("wb") as file:
                while chunk := response.read(1 << 20):
                    file.write(chunk)
            partial.rename(model)
        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = options.intra_op_num_threads = 1
        options.log_severity_level = 4
        self.session = onnxruntime.InferenceSession(str(model), providers=["CPUExecutionProvider"], sess_options=options)
        self.features = FeatureExtractor(feature_size=80, chunk_length=SECONDS)  # Whisper's, as the model was trained on

    def finished(self, audio: np.ndarray) -> float:
        """How likely it is that you've finished your turn, 0 to 1."""
        audio = audio[-SECONDS * RATE:].astype(np.float32)
        audio = np.pad(audio, (SECONDS * RATE - len(audio), 0))  # silence first, the way the model was trained
        audio = (audio - audio.mean()) / np.sqrt(audio.var() + 1e-7)
        mel = self.features(audio, padding=0)[:, : SECONDS * 100]
        return float(self.session.run(None, {"input_features": mel[None].astype(np.float32)})[0].ravel()[0])
