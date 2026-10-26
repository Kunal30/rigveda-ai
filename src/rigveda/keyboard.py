"""Hold-to-talk: notice when one key (Right Ctrl by default) is held down on its own, and when it's let go.

GNOME only tells apps a shortcut was pressed, never released, so this reads the keyboards directly from /dev/input,
which needs you in the `input` group. That access could read every key you type, so this keeps nothing: it looks at
one key code, notes whether any other key went down while it was held (then it was a shortcut like Ctrl+C, not
talking), and drops every event the moment it has looked at it. Nothing is stored or logged.
"""

from __future__ import annotations

import os
import select
import struct
import threading
import time
from pathlib import Path

KEYS = {"KEY_RIGHTCTRL": 97, "KEY_RIGHTALT": 100, "KEY_PAUSE": 119, "KEY_SCROLLLOCK": 70, "KEY_COMPOSE": 127,
        "KEY_F13": 183, "KEY_F14": 184, "KEY_F15": 185}
EVENT = struct.Struct("llHHi")  # struct input_event: time (two longs), type, code, value
EV_KEY = 1
HOLD = 0.25  # seconds the key must be held alone before it counts as talking
RESCAN = 5.0  # seconds between looks for keyboards plugged (back) in


def name(key: str) -> str:
    """How a key is written for people: KEY_RIGHTCTRL → Right Ctrl."""
    return key.removeprefix("KEY_").replace("RIGHT", "Right ").replace("CTRL", "Ctrl").replace("ALT", "Alt").title()


def keyboards(code: int, devices: str = "/proc/bus/input/devices") -> list[Path]:
    """The event devices that have this key."""
    found = []
    for block in Path(devices).read_text().split("\n\n"):
        handlers = next((line for line in block.splitlines() if line.startswith("H: Handlers=")), "")
        bits = next((line.split("=", 1)[1].split() for line in block.splitlines() if line.startswith("B: KEY=")), [])
        event = next((word for word in handlers.split() if word.startswith("event")), None)
        if not event or not bits:
            continue
        words = [int(word, 16) for word in reversed(bits)]  # least significant word first
        if code // 64 < len(words) and words[code // 64] >> (code % 64) & 1:
            found.append(Path("/dev/input") / event)
    return found


class Keyboard(threading.Thread):
    """Calls `held()` once the key has been down alone for HOLD seconds, then `released()` when it comes up (or another
    key goes down, which makes it a shortcut after all)."""

    def __init__(self, key: str, held, released, log=print) -> None:
        super().__init__(daemon=True, name="keyboard")
        self.code = KEYS.get(key) or int(key)
        self.held, self.released, self.log = held, released, log

    def open(self, already: set[Path]) -> dict[int, Path]:
        """Open the keyboards that have the key and aren't open yet."""
        opened = {}
        for path in keyboards(self.code):
            if path in already:
                continue
            try:
                opened[os.open(path, os.O_RDONLY | os.O_NONBLOCK)] = path
            except OSError:
                pass
        return opened

    def run(self) -> None:
        down_at: float | None = None  # when the key went down, while it's down
        other = holding = warned = False
        devices: dict[int, Path] = {}
        scanned = 0.0
        while True:
            # Look again every few seconds: a keyboard that's replugged comes back as a new device, and while the
            # mouse (which also reports a Right Ctrl key) stays open, "no devices left" never happens.
            if time.monotonic() - scanned >= RESCAN:
                devices |= self.open(set(devices.values()))
                scanned = time.monotonic()
            if not devices:
                if not warned:
                    self.log("Hold-to-talk can't read the keyboard: add yourself to the input group "
                             "(sudo usermod -aG input $USER), then log out and back in. Super+R still works.")
                    warned = True
                time.sleep(RESCAN)  # a keyboard plugged in, or the permission granted, is noticed in time
                continue
            wait = RESCAN if down_at is None or holding else min(RESCAN, max(0.0, down_at + HOLD - time.monotonic()))
            ready, _, _ = select.select(list(devices), [], [], wait)
            if down_at is not None and not holding and not other and time.monotonic() - down_at >= HOLD:
                holding = True
                self.held()
            for descriptor in ready:
                try:
                    data = os.read(descriptor, EVENT.size * 64)
                except OSError:  # unplugged
                    os.close(descriptor)
                    devices.pop(descriptor)
                    continue
                for offset in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    _, _, kind, code, value = EVENT.unpack_from(data, offset)
                    if kind != EV_KEY or value == 2:  # not a key, or a key repeating
                        continue
                    if code == self.code and value == 1:
                        down_at, other, holding = time.monotonic(), False, False
                    elif code == self.code and value == 0:
                        if holding:
                            self.released()
                        down_at, holding = None, False
                    elif value == 1 and down_at is not None:  # another key: a shortcut such as Ctrl+C
                        other = True
                        if holding:
                            self.released()
                            holding = False


SPACE = 57  # what Claude Code's dictation is held on
DICTATING = 0.35  # seconds Space must be held to be dictation rather than typing


class Holds(threading.Thread):
    """When a key (Space) has been held down rather than tapped, so speech said meanwhile, which is for Claude Code's
    dictation, isn't taken as said to Rigved. Like `Keyboard`, it looks at that one key code and keeps nothing else:
    only when it went down and came up, for the last minute."""

    KEEP = 60.0  # seconds of holds remembered

    def __init__(self, code: int = SPACE, longer: float = DICTATING) -> None:
        super().__init__(daemon=True, name="holds")
        self.code, self.longer = code, longer
        self.down: float | None = None  # when the key went down, while it's down
        self.holds: list[tuple[float, float]] = []  # (down, up), long enough to count
        self.lock = threading.Lock()

    def during(self, start: float, end: float, margin: float = 1.0) -> bool:
        """Whether the key was held at any time from `start` to `end` (time.monotonic()), give or take `margin`:
        you may start talking just before pressing it, or finish just after letting go."""
        now = time.monotonic()
        with self.lock:
            spans = list(self.holds)
            if self.down is not None and now - self.down >= self.longer:
                spans.append((self.down, now))
        return any(down - margin <= end and up + margin >= start for down, up in spans)

    def pressed(self, value: int, now: float) -> None:
        with self.lock:
            if value == 1:
                self.down = now
            elif value == 0 and self.down is not None:
                if now - self.down >= self.longer:
                    self.holds = [hold for hold in self.holds if hold[1] > now - self.KEEP] + [(self.down, now)]
                self.down = None

    def run(self) -> None:
        devices: dict[int, Path] = {}
        scanned = 0.0
        while True:
            if time.monotonic() - scanned >= RESCAN:
                for path in keyboards(self.code):
                    if path not in devices.values():
                        try:
                            devices[os.open(path, os.O_RDONLY | os.O_NONBLOCK)] = path
                        except OSError:
                            pass
                scanned = time.monotonic()
            if not devices:
                time.sleep(RESCAN)
                continue
            ready, _, _ = select.select(list(devices), [], [], RESCAN)
            for descriptor in ready:
                try:
                    data = os.read(descriptor, EVENT.size * 64)
                except OSError:
                    os.close(descriptor)
                    devices.pop(descriptor)
                    continue
                for offset in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    _, _, kind, code, value = EVENT.unpack_from(data, offset)
                    if kind == EV_KEY and code == self.code and value in (0, 1):
                        self.pressed(value, time.monotonic())
