"""Auralis Desktop: one native window owning its application runtime.

Run ``python desktop.pyw`` (or ``run-desktop.bat`` on Windows). The desktop
starts its own local server and never needs a terminal process.
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlsplit
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Written by core/overlay.py when Auralis starts with --overlay.
ENDPOINT = ROOT / "data" / "overlay_endpoint.json"
WIDTH, HEIGHT, EXPANDED_HEIGHT = 380, 480, 720
WAIT_SECONDS = 90

_window = None   # module level; storing it on the js_api object confuses pywebview
_height = HEIGHT
_expanded = False
_geometry_path = ROOT / "data" / "desktop_window.json"
_geometry = {"companion": {"width": 340, "height": 420},
             "chat": {"width": 480, "height": 720}, "on_top": True}
_geometry_lock = threading.RLock()
_save_timer = None
_screens = []
_desktop_mode = False
_active_url = None
_closing = False
DESKTOP_SIZE = {"width": 1000, "height": 760}


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
    height = min(max(300, height), max(1, screen.height - 64))
    return {"width": width, "height": height,
            "x": max(sx, min(x, sx + screen.width - width)),
            "y": max(sy, min(y, sy + screen.height - height - 48))}


def save_geometry(*_):
    global _save_timer
    if not _window:
        return
    with _geometry_lock:
        mode = "desktop" if _desktop_mode else "chat" if _expanded else "companion"
        try:
            values = {"width": int(_window.width), "height": int(_window.height),
                      "x": int(_window.x), "y": int(_window.y)}
            # Some native backends report zero while minimized. Keep the last
            # useful size so reopening never restores a collapsed window.
            if values["width"] <= 0 or values["height"] <= 0:
                return
            _geometry[mode] = values
            stored = {"desktop": values} if _desktop_mode else _geometry
            _geometry_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(dir=_geometry_path.parent, suffix=".tmp")
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                    json.dump(stored, file)
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
        if _closing:
            return
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
    """The port and token of the running Auralis, or None."""
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
    """True if a Auralis overlay server answers on this port."""
    try:
        url = f"http://127.0.0.1:{port}/api/ping"

        with urllib.request.urlopen(url, timeout=2) as response:
            return json.load(response).get("app") == "WOS-Overlay"
    except Exception:
        return False


def wait_for_backend(seconds=WAIT_SECONDS):
    """Wait for the Auralis terminal to publish a live endpoint."""
    deadline = time.monotonic() + seconds
    announced = False

    while time.monotonic() < deadline:
        endpoint = read_endpoint()

        if endpoint and is_running(endpoint["port"]):
            return endpoint

        if not announced:
            print("Waiting for the Auralis terminal (start.py --overlay)...")
            announced = True

        time.sleep(0.5)

    return None


class Api:
    """Methods here are callable from JS as pywebview.api.<name>()."""

    def close(self):
        if _window:
            try:
                _window.destroy()
            except Exception:
                return {"error": "The window could not close. Use its native Close button or Alt+F4."}

    def minimize(self):
        if _window:
            try:
                _window.minimize()
            except Exception:
                return {"error": "The window could not minimize. Use its native Minimize button."}

    def get_endpoint(self):
        """Fresh token after a Auralis /restart (the port stays the same)."""
        if _active_url:
            parsed = urlsplit(_active_url)
            return {"token": parsed.fragment, "port": parsed.port}
        endpoint = read_endpoint()
        return {"token": endpoint["token"], "port": endpoint["port"]} if endpoint else None

    def get_window_state(self):
        if _desktop_mode:
            return {"expanded": True, "on_top": False}
        return {"expanded": _expanded, "on_top": _geometry["on_top"]}

    def set_on_top(self, enabled):
        if _desktop_mode:
            return dict(self.get_window_state(), error="Auralis Desktop uses normal native window controls.")
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
        if _desktop_mode:
            return {"error": "Resize Auralis using the native window border."}
        if _window and type(width) is int and type(height) is int:
            try:
                size = clamp_geometry({"width": width, "height": height,
                                       "x": int(_window.x), "y": int(_window.y)}, _screens)
                _window.resize(size["width"], size["height"])
                schedule_geometry_save()
            except Exception:
                return {"error": "The window could not resize. Use its native window border."}

    def set_expanded(self, expanded):
        """Grow the window upward for the chat view, shrink it back for the
        avatar-only view, keeping the bottom edge where it is."""
        global _height, _expanded

        if _desktop_mode:
            return dict(self.get_window_state(), error="Auralis Desktop uses a single workspace.")

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
                _window.resize(values["width"], values["height"])
                _window.move(values["x"], values["y"])
                _height = values["height"]
                _expanded = expanded
        except Exception:
            return dict(self.get_window_state(), error="The window size could not be changed. Use its native window controls.")


def report_desktop_error(message):
    """Keep startup failures visible when pythonw has no console."""
    log_path = ROOT / "data" / "desktop.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
        message += f"\n\nDetails: {log_path}"
    except OSError:
        pass
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "Auralis Desktop", 0x10)
        except Exception:
            pass


def _desktop_geometry(screens):
    initial = dict(DESKTOP_SIZE)
    try:
        stored = json.loads(_geometry_path.read_text("utf-8"))
        if isinstance(stored.get("desktop"), dict):
            initial.update({name: value for name, value in stored["desktop"].items()
                            if name in ("width", "height", "x", "y") and type(value) is int})
    except (OSError, ValueError, AttributeError):
        pass
    if "x" not in initial and screens:
        screen = screens[0]
        initial.update(x=getattr(screen, "x", 0) + max(0, (screen.width - initial["width"]) // 2),
                       y=getattr(screen, "y", 0) + max(0, (screen.height - initial["height"]) // 2))
    if not screens:
        # Let the native host open on its primary display if enumeration is
        # unavailable, instead of trusting coordinates for a removed monitor.
        initial.update(x=32, y=32)
    return clamp_geometry(initial, screens)


def main(argv=None, runtime=None):
    """Run the desktop and close its owned runtime on every exit path.

    ``runtime`` is injectable for headless tests. Its ``start_server()`` returns
    a server with a URL; ``close()`` must suppress late speech without waiting
    for pending provider requests.
    """
    global _window, _screens, _desktop_mode, _active_url, _closing, _save_timer
    argv = list(argv or [])
    owned = runtime
    closed = False
    close_lock = threading.Lock()
    failed = False
    _window = None
    _desktop_mode = True
    _closing = False

    def close_owned(*_):
        nonlocal closed, failed
        global _closing, _save_timer
        with close_lock:
            if closed:
                return
            closed = True
        _closing = True
        with _geometry_lock:
            if _save_timer:
                _save_timer.cancel()
                _save_timer = None
            save_geometry()
        if owned is not None:
            try:
                owned.close()
            except Exception as error:
                failed = True
                report_desktop_error(f"Auralis Desktop could not finish shutting down: {error}")

    try:
        if "--browser" in argv:
            raise RuntimeError("Browser mode is no longer a Desktop launcher. Run run-desktop.bat for Desktop or run-terminal.bat for Terminal.")
        try:
            import webview
        except ImportError as error:
            raise RuntimeError("Auralis Desktop requires pywebview. Run python -m pip install -r requirements.txt, then launch Desktop again.") from error
        if owned is None:
            # Direct execution of overlay/main.py must also find the project.
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from core.runtime import ApplicationRuntime
            owned = ApplicationRuntime()
        server = owned.start_server()
        _active_url = server.url
        _screens = list(webview.screens)
        initial = _desktop_geometry(_screens)
        _window = webview.create_window(
            "Auralis", _active_url, js_api=Api(), **initial,
            frameless=False, easy_drag=False, on_top=False, resizable=True,
            min_size=(min(640, initial["width"]), min(480, initial["height"])),
            text_select=True, background_color="#101116",
        )
        _window.events.closing += close_owned
        _window.events.closed += close_owned
        _window.events.moved += schedule_geometry_save
        _window.events.resized += schedule_geometry_save
        webview.start(debug="--debug" in argv)
    except Exception as error:
        failed = True
        close_owned()
        report_desktop_error(
            f"Auralis Desktop could not start: {error}\n"
            "Check the dependencies and native WebView runtime, then try run-desktop.bat again."
        )
    finally:
        close_owned()
        _window = None
        _active_url = None
        _desktop_mode = False
        _closing = False
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
