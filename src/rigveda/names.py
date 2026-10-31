"""Files by name, for "open Iron Man 3": every file in your home folder (and drives under /media and /mnt), matched
forgivingly, the way a launcher's search does. Dots and underscores count as spaces, the order of words and a release's
tags ("1080p BluRay x265") don't matter, "three" is 3 and "III" is 3, and a word misheard by a letter or two still counts.

Rigved's index holds what files say, which is the way to find a letter about the deposit, but a film says nothing:
"Iron Man 3" found the release notes next to it, which weren't the film, so Rigved said there was no such file
(2026-10-30). Outside hidden folders your home holds about 1,200 files, so they're read afresh, a few milliseconds'
work, and there is no index of names to keep.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROOTS = (Path.home(), Path("/media"), Path("/mnt"))
SKIP = {"node_modules", "__pycache__", "site-packages", "dist-packages", "venv", "snap", "target", "build"}
FRESH = 30.0  # seconds a reading of the folders is good for
MOST = 200_000  # files read at most, should a drive full of them be plugged in

NUMBERS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
           "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "first": "1", "second": "2", "third": "3",
           "ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8", "ix": "9"}
UNASKED = {"the", "a", "an", "my", "file", "files", "movie", "film", "video", "song", "track", "document", "please",
           "of", "called", "named", "that", "one", "me", "for", "up", "uh", "um", "er", "erm", "okay", "ok", "so"}
EXTRA = {"trailer", "trailers", "teaser", "sample", "samples", "featurette", "featurettes", "extras", "bonus",
         "deleted", "interview", "interviews", "behind"}  # what comes with a film: never the film itself
# A release's tags: they say how it was made, not what it is.
TAG = re.compile(r"\d{3,4}p|[xh]26[45]|hevc|avc|bluray|blu|ray|brrip|bdrip|webrip|web|dl|hdtv|dvdrip|hdr|hdr10|sdr|"
                 r"\d+bit|bit|aac|ac3|eac3|ddp\d*|dd\d*|dts|truehd|atmos|flac|mp3|yify|yts|rarbg|tgx|\d+ch|"
                 r"remux|proper|repack|uhd|4k|ds4k|galaxyrg\d*|x264|x265|10|5|7|1")
VIDEO = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv"}
AUDIO = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac"}
ASIDE = {".srt", ".sub", ".idx", ".ass", ".vtt", ".nfo", ".sfv", ".torrent", ".part", ".crdownload", ".txt", ".url",
         ".exe", ".jpg", ".jpeg", ".png"}  # what comes with a film or an album; never the thing itself when that's there
UNFINISHED = {".part", ".crdownload", ".fdmdownload", ".download", ".partial", ".tmp", ".!qb", ".aria2"}  # can't be opened yet
PLAIN = {"bit", "bluray", "brrip", "bdrip", "webrip", "remux", "galaxyrg", "aac", "ddp", "hevc", "avc", "truehd",
         "atmos", "dts", "yify", "yts", "tgx", "rarbg", "mkv", "mp4"}  # what doesn't tell copies of a film apart
CLOSE = 0.03  # two files this close in how well they match are both meant: Rigved asks which
GOOD = 0.75  # how well a name must match to be the file asked for


def tokens(text: str) -> list[str]:
    """The words of a name or of what was said, lower case, with numbers as digits: "Iron.Man.III" → iron man 3."""
    found = re.findall(r"[a-z0-9]+", text.lower().replace("'", ""))
    return [NUMBERS.get(word, word) for word in found]


def title(path: Path | str) -> list[str]:
    """The words of a file's name before its year or tags: "Iron.Man.3.2013.REMASTERED.1080p…" → iron man 3."""
    words = tokens(Path(path).stem)
    for index, word in enumerate(words):
        if index and (re.fullmatch(r"(?:19|20)\d\d", word) or re.fullmatch(r"\d{3,4}p", word)):
            return words[:index]
    return words


def spoken(path: Path | str) -> str:
    """A file's name as Rigved should say it: "Iron Man 3 (2013)", not every tag of the release."""
    words = tokens(Path(path).stem)
    name = title(path)
    year = words[len(name)] if len(name) < len(words) and re.fullmatch(r"(?:19|20)\d\d", words[len(name)]) else ""
    written = re.findall(r"[A-Za-z0-9]+", Path(path).stem.replace("'", ""))  # as the name writes them
    said = " ".join(written[:len(name)]) if len(written) >= len(name) else " ".join(name)
    said = f"{said} ({year})" if year else said
    extra = [word for word in words[len(name):] if word in EXTRA]
    return f"{said}, the {extra[0]}" if extra else said  # "Mirzapur The Movie (2026), the trailer"


_read: tuple[float, list[Path]] = (0.0, [])
_lock = threading.Lock()


def files(roots=None, fresh: float = FRESH) -> list[Path]:
    """Every file under `roots` (your home and drives) outside hidden folders and the clutter of code (node_modules
    and the like)."""
    global _read
    usual = roots is None
    roots = ROOTS if usual else roots
    with _lock:
        if usual and time.monotonic() - _read[0] < fresh:
            return _read[1]
        found: list[Path] = []
        folders = [Path(root) for root in roots]
        while folders and len(found) < MOST:
            folder = folders.pop()
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        if entry.name.startswith(".") or entry.name in SKIP:
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                folders.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() not in UNFINISHED:
                                found.append(Path(entry.path))
                        except OSError:
                            continue
            except OSError:
                continue
        if usual:
            _read = (time.monotonic(), found)
        return found


def _word(wanted: str, words: list[str]) -> float:
    """How well one word asked for appears among a name's words: 1 the same, less for the start of one ("rem" for
    "remastered") or one misheard by a letter. Numbers must be the same number."""
    if wanted in words:
        return 1.0
    if wanted.isdigit():
        return 0.0
    from difflib import SequenceMatcher

    best = 0.0
    for word in words:
        if len(wanted) >= 3 and (word.startswith(wanted) or wanted.startswith(word) and len(word) >= 3):
            best = max(best, 0.85)
        elif len(wanted) >= 4 and not word.isdigit() and SequenceMatcher(None, wanted, word).ratio() >= 0.8:
            best = max(best, 0.8)
        elif len(wanted) == len(word) == 3 and sum(a != b for a, b in zip(wanted, word)) == 1:
            best = max(best, 0.75)  # "men" for "man"
    return best


def _joined(words: list[str]) -> list[str]:
    """A name's words, and each two together: "ironman" is "iron man"."""
    return words + [a + b for a, b in zip(words, words[1:]) if a.isalpha() and b.isalpha()]


def score(asked: str, path: Path) -> float:
    """How well a file's name (or, less so, its folder's) matches what was asked for, 0 to about 1."""
    wanted = [word for word in tokens(asked) if word not in UNASKED]
    if not wanted:
        return 0.0
    name, folder = tokens(path.stem), tokens(path.parent.name)
    found = [max(_word(word, _joined(name)), 0.7 * _word(word, _joined(folder))) for word in wanted]
    if min(found) < 0.5:  # every word asked for must be there
        return 0.0
    value = sum(found) / len(found)
    titled = title(path)
    words = " ".join(name)
    if f" {' '.join(wanted)} " in f" {words} ":
        value += 0.05  # in the order said
        after = name[name.index(wanted[-1]) + 1:] if wanted[-1] in name else []
        if after and re.fullmatch(r"[2-9]", after[0]) and not any(word.isdigit() for word in wanted):
            value -= 0.15  # "Iron Man" isn't "Iron Man 3"
    value -= 0.02 * min(len([word for word in titled if word not in wanted and not TAG.fullmatch(word)]), 5)
    suffix = path.suffix.lower()
    if suffix in ASIDE:
        value -= 0.25
    if EXTRA & (set(name) | set(tokens(" ".join(path.parent.parts[-2:])))) and not EXTRA & set(wanted):
        value -= 0.3  # "Trailers/Mirzapur- The Movie (2026)-trailer.mkv" isn't the film (2026-10-30)
    return value


@dataclass(frozen=True)
class Match:
    path: Path
    score: float


def matches(asked: str, candidates: list[Path] | None = None, most: int = 5) -> list[Match]:
    """The files whose names best match what was asked for, best first, the good ones only."""
    candidates = files() if candidates is None else candidates
    scored = [Match(path, value) for path in candidates if (value := score(asked, path)) >= GOOD]
    scored.sort(key=lambda match: (-match.score, -_modified(match.path)))
    return scored[:most]


def _modified(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def meant(found: list[Match]) -> list[Match]:
    """The files among the best matches that were all equally meant: one, or two or three to choose from."""
    if not found:
        return []
    top = found[0].score
    return [match for match in found if top - match.score <= CLOSE][:3]


def distinct(paths: list[Path]) -> list[list[str]]:
    """The words of each file's name that none of the others has, but for a release's plain tags."""
    words = [set(tokens(path.stem)) for path in paths]
    return [[word for word in dict.fromkeys(tokens(path.stem)) if word not in set().union(*(words[:i] + words[i + 1:]))
             and word.isalpha() and len(word) >= 3 and word not in PLAIN] for i, path in enumerate(paths)]


def differences(paths: list[Path]) -> list[str]:
    """What tells each file from the others, to ask which one: "remastered", "HDR WeSLeY", or else its folder."""
    told = []
    for path, own in zip(paths, distinct(paths)):
        written = {word.lower(): word if not (word.isupper() and len(word) > 3) else word.lower()  # its own capitals,
                   for word in reversed(re.findall(r"[A-Za-z0-9]+", path.stem))}  # but HDR and WeSLeY, not REMASTERED
        told.append(" ".join(written.get(word, word) for word in own[:2]) or f"in {path.parent.name}")
    return told


ORDINAL = {"1": 0, "2": 1, "3": 2, "last": -1, "latter": -1, "former": 0, "either": 0, "any": 0, "whichever": 0}


def choose(said: str, choices: list[str]) -> str | None:
    """Which of the files Rigved asked about you picked: "the second one", "the remastered one", "either"; None if
    what you said doesn't pick one."""
    said = re.sub(r"\bones?\b", " ", said, flags=re.IGNORECASE)  # "the MKV one" isn't "the MKV 1"
    words = [word for word in tokens(said) if word not in UNASKED and word not in {"open", "play", "watch", "it"}]
    if not words or len(words) > 6:
        return None
    for word in words:
        if word in ORDINAL and (len(words) == 1 or word not in tokens(" ".join(choices))):
            index = ORDINAL[word]
            return choices[index] if -len(choices) <= index < len(choices) else None
    paths = [Path(choice) for choice in choices]
    picked = [which for which, own in zip(choices, distinct(paths)) if set(words) & set(own)]
    if len(picked) == 1:
        return picked[0]
    folders = [which for which in choices  # "the one in multimedia"
               if set(words) & set(tokens(" ".join(Path(which).parent.parts[-3:]))) - set(tokens(Path(which).stem))]
    if len(folders) == 1:
        return folders[0]
    shared = set.intersection(*(set(tokens(path.name)) for path in paths))
    if set(words) <= shared:  # "the MKV one" when they all are: any will do, so the best match (2026-10-30)
        return choices[0]
    return None
