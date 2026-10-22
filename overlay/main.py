"""WOS-Overlay desktop window.

Opens the overlay UI as a small frameless, always-on-top window (pywebview)
that talks to the running WOS terminal. It does not run WOS itself: start WOS
with `python start.py --overlay` (run.bat does both).

Usage:
    python overlay/main.py            # desktop window (needs pywebview)
    python overlay/main.py --browser  # open in the default browser instead
    python overlay/main.py --debug    # desktop window with dev tools
If pywebview is not installed the browser is used automatically.
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Written by core/overlay.py when WOS starts with --overlay.
ENDPOINT = ROOT / "data" / "overlay_endpoint.json"
WIDTH, HEIGHT, EXPANDED_HEIGHT = 380, 480, 720
WAIT_SECONDS = 90

_window = None   # module level; storing it on the js_api object confuses pywebview
_height = HEIGHT
_expanded = False
_geometry_path = ROOT / "data" / "companion_window.json"
_geometry = {"companion": {"width": 340, "height": 420},
             "chat": {"width": 480, "height": 720}, "on_top": True}
_geometry_lock = threading.RLock()
_save_timer = None
_screens = []


def clamp_geometry(values, screens):
    """Keep the whole window on an attached screen, including negative X/Y."""
    def number(name, default):
        value = values.get(name)
        return value if type(value) is int else default
    width, height = number("width", 340), number("height", 420)
    x, y = number("x", 32), number("y", 32)
    if not screens:
        return {"width": max(280, min(width, 1600)), "height": max(300, min(height, 1400)), "x": x, "y": y}
    def overlap(screen):
        sx, sy = getattr(screen, "x", 0), getattr(screen, "y", 0)
        return max(0, min(x + width, sx + screen.width) - max(x, sx)) * max(0, min(y + height, sy + screen.height) - max(y, sy))
    screen = max(screens, key=overlap)
    sx, sy = getattr(screen, "x", 0), getattr(screen, "y", 0)
    width = min(max(280, width), screen.width)
    height = min(max(300, height), max(300, screen.height - 64))
    return {"width": width, "height": height,
            "x": max(sx, min(x, sx + screen.width - width)),
            "y": max(sy, min(y, sy + screen.height - height - 48))}


def save_geometry(*_):
    global _save_timer
    if not _window:
        return
    with _geometry_lock:
        mode = "chat" if _expanded else "companion"
        try:
            _geometry[mode] = {"width": int(_window.width), "height": int(_window.height),
                               "x": int(_window.x), "y": int(_window.y)}
            _geometry_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(dir=_geometry_path.parent, suffix=".tmp")
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                    json.dump(_geometry, file)
                os.replace(name, _geometry_path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        except Exception:
            # Native geometry access may fail while minimizing/closing. Saving
            # a preference must not break a window control or the close hook.
            pass


def schedule_geometry_save(*_):
    global _save_timer
    with _geometry_lock:
        if _save_timer:
            _save_timer.cancel()
        _save_timer = threading.Timer(0.3, save_geometry)
        _save_timer.daemon = True
        _save_timer.start()


def stop_backend_speech(*_):
    """Native close (including Alt+F4) silences the shared backend too."""
    save_geometry()
    endpoint = read_endpoint()
    if endpoint:
        request = urllib.request.Request(
            f"http://127.0.0.1:{endpoint['port']}/api/stop", data=b"{}",
            headers={"X-WOS-Token": endpoint["token"], "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=1):
                pass
        except Exception:
            pass


def read_endpoint():
    """The port and token of the running WOS, or None."""
    try:
        data = json.loads(ENDPOINT.read_text("utf-8"))
    except (OSError, ValueError):
        return None

    if (
        isinstance(data, dict)
        and type(data.get("port")) is int
        and 0 < data["port"] <= 65535
        and isinstance(data.get("token"), str)
    ):
        return data

    return None


def is_running(port):
    """True if a WOS overlay server answers on this port."""
    try:
        url = f"http://127.0.0.1:{port}/api/ping"

        with urllib.request.urlopen(url, timeout=2) as response:
            return json.load(response).get("app") == "WOS-Overlay"
    except Exception:
        return False


def wait_for_backend(seconds=WAIT_SECONDS):
    """Wait for the WOS terminal to publish a live endpoint."""
    deadline = time.monotonic() + seconds
    announced = False

    while time.monotonic() < deadline:
        endpoint = read_endpoint()

        if endpoint and is_running(endpoint["port"]):
            return endpoint

        if not announced:
            print("Waiting for the WOS terminal (start.py --overlay)...")
            announced = True

        time.sleep(0.5)

    return None


class Api:
    """Methods here are callable from JS as pywebview.api.<name>()."""

    def close(self):
        if _window:
            _window.destroy()

    def minimize(self):
        if _window:
            _window.minimize()

    def get_endpoint(self):
        """Fresh token after a WOS /restart (the port stays the same)."""
        endpoint = read_endpoint()
        return {"token": endpoint["token"], "port": endpoint["port"]} if endpoint else None

    def get_window_state(self):
        return {"expanded": _expanded, "on_top": _geometry["on_top"]}

    def set_on_top(self, enabled):
        # A native window failure must remain a window-control error. Do not
        # query geometry here: that crosses the GUI thread again unnecessarily.
        if not _window or not isinstance(enabled, bool):
            return dict(self.get_window_state(), error="The window is unavailable.")
        try:
            _window.on_top = enabled
        except Exception:
            return dict(self.get_window_state(), error="Always on top could not be changed.")
        with _geometry_lock:
            _geometry["on_top"] = enabled
        schedule_geometry_save()
        return self.get_window_state()

    def resize(self, width, height):
        if _window and type(width) is int and type(height) is int:
            size = clamp_geometry({"width": width, "height": height,
                                   "x": int(_window.x), "y": int(_window.y)}, _screens)
            _window.resize(size["width"], size["height"])
            schedule_geometry_save()

    def set_expanded(self, expanded):
        """Grow the window upward for the chat view, shrink it back for the
        avatar-only view, keeping the bottom edge where it is."""
        global _height, _expanded

        if not _window:
            return

        if not isinstance(expanded, bool) or expanded == _expanded:
            return

        try:
            with _geometry_lock:
                save_geometry()
                values = dict(_geometry["chat" if expanded else "companion"])
                normalized = clamp_geometry(values, _screens)
                values.update(width=normalized["width"], height=normalized["height"])
                values.setdefault("x", int(_window.x))
                values.setdefault("y", int(_window.y + _window.height - values["height"]))
                values = clamp_geometry(values, _screens)
                _expanded = expanded
                _window.resize(values["width"], values["height"])
                _window.move(values["x"], values["y"])
                _height = values["height"]
        except Exception:
            pass


def main(argv):
    global _window, _screens

    endpoint = wait_for_backend()

    if endpoint is None:
        print("The WOS terminal did not start in time. Run run.bat again.")
        return 1

    url = f"http://127.0.0.1:{endpoint['port']}/#{endpoint['token']}"

    if "--browser" in argv:
        webbrowser.open(url)
        return 0

    try:
        import webview
    except ImportError:
        print("pywebview is not installed (pip install pywebview).")
        print("Opening in your browser instead.")
        webbrowser.open(url)
        return 0

    try:
        stored = json.loads(_geometry_path.read_text("utf-8"))
        for mode in ("companion", "chat"):
            if isinstance(stored.get(mode), dict):
                _geometry[mode].update(stored[mode])
        _geometry["on_top"] = stored.get("on_top") is not False
    except (OSError, ValueError, AttributeError):
        pass
    _screens = list(webview.screens)
    initial = dict(_geometry["companion"])
    if "x" not in initial and _screens:
        screen = _screens[0]
        initial.update(x=getattr(screen, "x", 0) + screen.width - 372,
                       y=getattr(screen, "y", 0) + screen.height - 492)
    initial = clamp_geometry(initial, _screens)

    _window = webview.create_window(
        "WOS Companion",
        url,
        js_api=Api(),
        **initial,
        frameless=True,
        easy_drag=False,
        on_top=_geometry["on_top"],
        resizable=True,
        min_size=(280, 300),
        text_select=True,
        background_color="#101116",
    )
    _window.events.closing += stop_backend_speech
    _window.events.moved += schedule_geometry_save
    _window.events.resized += schedule_geometry_save
    webview.start(debug="--debug" in argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
