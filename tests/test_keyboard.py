import os
import threading
import time
from pathlib import Path

import pytest

from rigveda import keyboard
from rigveda.keyboard import EV_KEY, EVENT, Keyboard

DEVICES = """I: Bus=0003 Vendor=1234 Product=5678 Version=0110
N: Name="USB Keyboard"
H: Handlers=sysrq kbd event3 leds
B: EV=120013
B: KEY=1000000000007 ff9f207ac14057ff febeffdfffefffff fffffffffffffffe

I: Bus=0019 Vendor=0000 Product=0001 Version=0000
N: Name="Power Button"
H: Handlers=kbd event0
B: EV=3
B: KEY=10000000000000 0
"""


def test_finds_keyboards_that_have_the_key(tmp_path: Path) -> None:
    devices = tmp_path / "devices"
    devices.write_text(DEVICES)
    assert keyboard.keyboards(97, str(devices)) == [Path("/dev/input/event3")]  # Right Ctrl: not the power button
    assert keyboard.keyboards(116, str(devices)) == [Path("/dev/input/event3"), Path("/dev/input/event0")]  # both: power


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch):
    """A Keyboard reading from a pipe, and a way to press keys into it."""
    read, write = os.pipe()
    calls: list[str] = []
    monkeypatch.setattr(Keyboard, "open", lambda self: {read: Path("/dev/input/test")})
    watcher = Keyboard("KEY_RIGHTCTRL", lambda: calls.append("held"), lambda: calls.append("released"), log=lambda m: None)
    watcher.start()

    def key(code: int, value: int, then: float = 0.0) -> None:
        os.write(write, EVENT.pack(0, 0, EV_KEY, code, value))
        time.sleep(then)
    yield key, calls
    os.close(write)


def test_holding_the_key_alone_talks(keys) -> None:
    key, calls = keys
    key(97, 1, then=keyboard.HOLD + 0.15)
    assert calls == ["held"]
    key(97, 2, then=0.05)  # the key repeating changes nothing
    key(97, 0, then=0.05)
    assert calls == ["held", "released"]


def test_a_quick_tap_does_nothing(keys) -> None:
    key, calls = keys
    key(97, 1, then=0.05)
    key(97, 0, then=keyboard.HOLD + 0.1)
    assert calls == []


def test_a_shortcut_like_ctrl_c_does_nothing(keys) -> None:
    key, calls = keys
    key(97, 1, then=0.05)
    key(46, 1, then=0.02)  # C
    key(46, 0, then=keyboard.HOLD + 0.1)
    key(97, 0, then=0.05)
    assert calls == []
