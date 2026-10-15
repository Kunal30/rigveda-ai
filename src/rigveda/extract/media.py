"""Audio and video, transcribed locally with faster-whisper."""

from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from . import ExtractionError, Segment

AUDIO = (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma")
VIDEO = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v")
WINDOW_SECONDS = 60  # transcript text is grouped into one-minute segments


def timestamp(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def group(pieces: list[tuple[float, str]], window: float = WINDOW_SECONDS) -> list[Segment]:
    """Group (start_seconds, text) pieces into segments labelled with their start time."""
    segments, start, texts = [], None, []
    for begin, text in pieces:
        if start is not None and begin - start >= window:
            segments.append(Segment(" ".join(texts), timestamp(start)))
            start, texts = None, []
        if start is None:
            start = begin
        texts.append(text.strip())
    if texts:
        segments.append(Segment(" ".join(texts), timestamp(start)))
    return segments


def _preload_cuda_libraries() -> None:
    """Load cuBLAS/cuDNN from the pip `nvidia-*` wheels so CTranslate2 can find them."""
    try:
        import nvidia
    except ImportError:
        return
    libraries = [lib for root in nvidia.__path__ for lib in Path(root).glob("*/lib/lib*.so*")]
    for _ in range(len(libraries)):  # retry until dependencies between libraries resolve
        remaining = []
        for library in libraries:
            try:
                ctypes.CDLL(str(library), mode=ctypes.RTLD_GLOBAL)
            except OSError:
                remaining.append(library)
        if not remaining or len(remaining) == len(libraries):
            return
        libraries = remaining


@lru_cache(maxsize=1)
def _model(name: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise ExtractionError("install faster-whisper to transcribe audio and video (pip install -e '.[media]')") from error
    _preload_cuda_libraries()
    try:
        return WhisperModel(name, device="cuda", compute_type="float16")
    except Exception as error:  # no GPU, or CUDA libraries missing
        print(f"Whisper GPU unavailable ({error}); using the CPU, which is much slower.", file=sys.stderr)
        return WhisperModel(name, device="cpu", compute_type="int8")


def transcribe(samples: bytes, model_name: str) -> list[tuple[float, str]]:
    """Transcribe 16 kHz mono float32 samples."""
    import numpy

    audio = numpy.frombuffer(samples, dtype=numpy.float32)
    pieces, _info = _model(model_name).transcribe(audio, beam_size=5, vad_filter=True)
    return [(piece.start, piece.text) for piece in pieces]


def read_media(path: Path, config) -> list[Segment]:
    if not shutil.which("ffmpeg"):
        raise ExtractionError("install ffmpeg to read audio and video (sudo apt install ffmpeg)")
    # Decode straight to 16 kHz mono float32 samples, the format Whisper expects.
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
        capture_output=True,
    )
    error = result.stderr.decode(errors="replace")
    if result.returncode != 0:
        if "does not contain any stream" in error:
            return []  # a video without sound
        raise ValueError(f"ffmpeg could not read the file: {error.strip()[-300:]}")
    if not result.stdout:
        return []
    return group(transcribe(result.stdout, config.whisper_model))


EXTRACTORS = [(suffix, "audio", read_media) for suffix in AUDIO] + [(suffix, "video", read_media) for suffix in VIDEO]
