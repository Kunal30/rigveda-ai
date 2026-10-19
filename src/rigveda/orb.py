"""The floating assistant: a small always-on-top orb that opens into a translucent panel with your stats and a chat.

The look lives in the server's /orb page; this file only gives it a frameless, see-through window that stays above
other windows. GNOME on Wayland won't let an app keep itself on top or choose where it sits, so the window runs
through XWayland, where both work. It needs GTK 3 and WebKitGTK from the system Python (python3-gi,
gir1.2-webkit2-4.1), which is why it's started as a script by `rigveda orb` rather than imported.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ["GDK_BACKEND"] = "x11"  # XWayland: keep-above and window positions work there
os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")  # avoids a blank window on NVIDIA drivers

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, GLib, Gtk, WebKit2  # noqa: E402

ORB = 168  # the window's side: the orb plus room for its glow and bounce
ORB_RANGE = (96, 480)  # the mouse wheel over the orb resizes it within these
PANEL = (440, 780)
PANEL_MIN = (360, 520)  # smaller and the chat has no room
MARGIN = 24
STALL = 6  # seconds without a drawn frame before the window counts as frozen
STATE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "rigveda" / "orb.json"
VOICE = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "rigveda-voice.json"  # written by `rigveda listen`
VOICE_STATES = {"idle", "listening", "thinking", "speaking"}
OFFLINE = """<!doctype html><html><body style="margin:0;background:transparent;display:grid;place-items:center;height:100vh">
<div title="Rigveda AI isn't running" style="width:64px;height:64px;border-radius:50%;border:2px solid rgba(255,92,122,.7);
box-shadow:0 0 24px rgba(255,92,122,.45),inset 0 0 18px rgba(255,92,122,.3);background:radial-gradient(circle at 35% 30%,
rgba(255,255,255,.18),rgba(40,8,18,.75) 60%)"></div></body></html>"""
EDGES = {"n": Gdk.WindowEdge.NORTH, "ne": Gdk.WindowEdge.NORTH_EAST, "e": Gdk.WindowEdge.EAST, "se": Gdk.WindowEdge.SOUTH_EAST,
         "s": Gdk.WindowEdge.SOUTH, "sw": Gdk.WindowEdge.SOUTH_WEST, "w": Gdk.WindowEdge.WEST, "nw": Gdk.WindowEdge.NORTH_WEST}


def load_state() -> dict:
    """Where the orb sits and how big the orb and the panel are, from the last time."""
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


class Orb(Gtk.Window):
    def __init__(self, url: str) -> None:
        super().__init__(title="Rigveda AI")
        self.url = url
        self.expanded = False
        self.state = load_state()
        try:
            self.orb = min(max(int(self.state["orb"]), ORB_RANGE[0]), ORB_RANGE[1])
        except (KeyError, ValueError, TypeError):
            self.orb = ORB
        try:
            self.panel = tuple(max(int(n), least) for n, least in zip(self.state["panel"], PANEL_MIN, strict=True))
        except (KeyError, ValueError, TypeError):
            self.panel = PANEL
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.stick()  # on every workspace
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        css = Gtk.CssProvider()
        css.load_from_data(b"window, webview { background: transparent; }")
        Gtk.StyleContext.add_provider_for_screen(self.get_screen(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        content = WebKit2.UserContentManager()
        content.register_script_message_handler("orb")
        content.connect("script-message-received::orb", self.message)
        self.view = WebKit2.WebView.new_with_user_content_manager(content)
        self.view.set_background_color(Gdk.RGBA(0, 0, 0, 0))
        # Draw on the CPU: keeps the orb off the NVIDIA card, whose video decoder froze VLC once while it started.
        self.view.get_settings().set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.NEVER)
        self.view.connect("load-failed", self.offline)
        self.view.connect("context-menu", lambda *args: True)  # no "Reload / Inspect" menu on right-click
        self.add(self.view)

        self.view.set_size_request(1, 1)  # WebKit asks for 200×200 otherwise, bigger than the orb
        self.fit((self.orb, self.orb))
        self.home = self.saved_position()
        self.move(*self.home)
        self.connect("configure-event", self.remember)
        self.connect("destroy", Gtk.main_quit)
        self.pointer = None
        self.target = (*self.home, self.orb, self.orb)  # where the window should be and how big, to put it back after a reset
        self.frame, self.stalled, self.stuck = -1, 0, False
        GLib.timeout_add(40, self.track)
        GLib.timeout_add_seconds(1, self.watch)
        self.voice_seen, self.voice_state, self.voice_at = 0, "idle", 0.0
        GLib.timeout_add(40, self.voice)
        self.view.load_uri(url)

    def track(self) -> bool:
        """Tell the page where the pointer is, relative to the orb, so the orb can lean towards it across the screen."""
        if not self.expanded and self.get_window():
            _, x, y = Gdk.Display.get_default().get_default_seat().get_pointer().get_position()
            left, top = self.get_position()
            width, height = self.get_size()
            pointer = (x - left - width // 2, y - top - height // 2)
            if pointer != self.pointer:
                self.pointer = pointer
                self.view.evaluate_javascript(f"window.look && look({pointer[0]}, {pointer[1]})", -1, None, None, None, None, None)
        return True

    def voice(self) -> bool:
        """Pass the voice listener's state and loudness to the page, so the orb pops while you talk and pulses while
        it answers. A state that stops updating (the listener crashed mid-sentence) goes back to idle."""
        try:
            changed = VOICE.stat().st_mtime_ns
        except OSError:
            changed = self.voice_seen
        if changed != self.voice_seen:
            self.voice_seen = changed
            try:
                data = json.loads(VOICE.read_text())
                state, level = str(data["state"]), float(data.get("level", 0))
            except (OSError, ValueError, KeyError, TypeError):
                return True
            self.voice_state, self.voice_at = (state if state in VOICE_STATES else "idle"), time.monotonic()
            self.view.evaluate_javascript(f"window.voice && voice('{self.voice_state}', {min(max(level, 0), 1):.3f})",
                                          -1, None, None, None, None, None)
        elif self.voice_state != "idle" and time.monotonic() - self.voice_at > 30:
            self.voice_state = "idle"
            self.view.evaluate_javascript("window.voice && voice('idle', 0)", -1, None, None, None, None, None)
        return True

    def watch(self) -> bool:
        """Notice when the window stops drawing and reset it. GNOME sometimes loses track of a frame from an XWayland
        window (it logs "Frame has assigned frame counter but no frame drawn time"), and GTK then waits forever for that
        frame before drawing the next, so the orb freezes while everything behind it keeps working. Hiding and showing
        the window starts it afresh; if that doesn't help, quit with an error so the service starts a new one."""
        clock = self.get_frame_clock()
        if not clock or not self.get_mapped():
            return True
        frame = clock.get_frame_counter()
        if frame != self.frame:
            self.frame, self.stalled = frame, 0
        else:
            self.stalled += 1
            if self.stalled == STALL:
                print("The orb stopped drawing; resetting its window.", file=sys.stderr, flush=True)
                self.reset()
            elif self.stalled == 2 * STALL:
                print("The orb is still frozen; quitting so the service restarts it.", file=sys.stderr, flush=True)
                self.stuck = True
                Gtk.main_quit()
                return False
        self.queue_draw()  # ask for a frame each second, so a still page doesn't look frozen
        return True

    def reset(self) -> None:
        self.hide()
        x, y, width, height = self.target
        self.fit((width, height), PANEL_MIN if self.expanded else None)
        self.move(x, y)
        self.show_all()
        self.set_keep_above(True)
        self.stick()

    def fit(self, size: tuple[int, int], smallest: tuple[int, int] | None = None) -> None:
        """Resize the window; `smallest` is as far as dragging an edge may shrink it."""
        self.set_size_request(*(smallest or size))
        self.resize(*size)

    def workarea(self) -> Gdk.Rectangle:
        display = Gdk.Display.get_default()
        monitor = display.get_monitor_at_window(self.get_window()) if self.get_window() else display.get_primary_monitor()
        return (monitor or display.get_monitor(0)).get_workarea()

    def saved_position(self) -> tuple[int, int]:
        area = self.workarea()
        try:
            x, y = self.state["position"]
        except (KeyError, ValueError, TypeError):  # bottom-right corner by default
            return area.x + area.width - self.orb - MARGIN, area.y + area.height - self.orb - MARGIN
        return self.on_screen(int(x), int(y), area)

    def on_screen(self, x: int, y: int, area: Gdk.Rectangle) -> tuple[int, int]:
        """Keep the orb on screen, e.g. after it grew or a monitor was unplugged."""
        return min(max(x, area.x), area.x + area.width - self.orb), min(max(y, area.y), area.y + area.height - self.orb)

    def remember(self, widget, event) -> bool:
        size = (event.width, event.height)
        if not self.expanded:
            self.home = (event.x, event.y)
            self.target = (*self.home, self.orb, self.orb)
        elif size != (self.orb, self.orb) and size[0] >= PANEL_MIN[0] and size[1] >= PANEL_MIN[1]:  # not mid-grow/shrink
            self.panel = size
            self.target = (event.x, event.y, *size)
        else:
            return False
        self.state.update(position=self.home, orb=self.orb, panel=self.panel)
        try:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(self.state))
        except OSError:
            pass
        return False

    def zoom(self, step: int) -> None:
        """Grow or shrink the orb by a tenth per step, around its centre."""
        size = min(max(round(self.orb * 1.1**step), ORB_RANGE[0]), ORB_RANGE[1])
        if self.expanded or size == self.orb:
            return
        shift = (size - self.orb) // 2
        self.orb = size
        self.home = self.on_screen(self.home[0] - shift, self.home[1] - shift, self.workarea())
        self.fit((size, size))
        self.move(*self.home)  # the configure event that follows saves the new size and place

    def message(self, manager, result) -> None:
        try:
            request = json.loads(result.get_js_value().to_string())
        except (ValueError, AttributeError):
            return
        command = request.get("cmd")
        if command == "drag" and not self.expanded:
            seat = Gdk.Display.get_default().get_default_seat()
            _, x, y = seat.get_pointer().get_position()
            self.begin_move_drag(1, x, y, Gtk.get_current_event_time())
        elif command == "zoom":
            self.zoom(1 if request.get("step", 0) > 0 else -1)
        elif command == "resize" and self.expanded and request.get("edge") in EDGES:
            _, x, y = Gdk.Display.get_default().get_default_seat().get_pointer().get_position()
            self.begin_resize_drag(EDGES[request["edge"]], 1, x, y, Gtk.get_current_event_time())
        elif command == "expand":
            self.expand()
        elif command == "collapse":
            self.collapse()
        elif command == "open":
            Gtk.show_uri_on_window(self, request.get("url", self.url), Gtk.get_current_event_time())
        elif command == "quit":
            self.destroy()

    def expand(self) -> None:
        """Grow into the panel towards the middle of the screen, so it stays fully visible wherever the orb sits."""
        if self.expanded:
            return
        self.expanded = True
        area = self.workarea()
        width, height = min(self.panel[0], area.width - 16), min(self.panel[1], area.height - 16)
        x, y = self.home
        if x + self.orb / 2 > area.x + area.width / 2:
            x = x + self.orb - width
        if y + self.orb / 2 > area.y + area.height / 2:
            y = y + self.orb - height
        x = min(max(x, area.x + 8), area.x + area.width - width - 8)
        y = min(max(y, area.y + 8), area.y + area.height - height - 8)
        self.target = (x, y, width, height)
        self.set_resizable(True)  # drag the panel's edges and corners
        self.fit((width, height), PANEL_MIN)
        self.move(x, y)
        self.present()

    def collapse(self) -> None:
        if not self.expanded:
            return
        self.target = (*self.home, self.orb, self.orb)
        self.set_resizable(False)
        self.fit((self.orb, self.orb))
        self.move(*self.home)
        GLib.timeout_add(200, self.settled)

    def settled(self) -> bool:
        self.expanded = False  # only now, so the shrink itself isn't saved as the orb's position
        return False

    def offline(self, view, event, uri, error) -> bool:
        """Show a dim red orb while the server is down, and try again every 10 seconds."""
        self.view.load_html(OFFLINE, None)
        GLib.timeout_add_seconds(10, lambda: self.view.load_uri(self.url) and False)
        return True


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    orb = Orb(f"http://127.0.0.1:{port}/orb?native=1")
    orb.show_all()
    Gtk.main()
    if orb.stuck:
        sys.exit(1)  # a failure, so the service's Restart=on-failure brings it back


if __name__ == "__main__":
    main()
