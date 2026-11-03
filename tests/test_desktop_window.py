"""Desktop ownership and native-window contracts without a GUI or providers."""
import importlib.util
import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("desktop_shell_tests", ROOT / "overlay" / "main.py")
shell = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shell)


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        for handler in self.handlers:
            handler()


class NativeWindow:
    def __init__(self, options):
        self.__dict__.update(options)
        self.events = SimpleNamespace(closing=Event(), closed=Event(), moved=Event(), resized=Event())

    def destroy(self):
        self.events.closing.fire()


class NativeHost:
    screens = [SimpleNamespace(x=0, y=0, width=1920, height=1080)]

    def __init__(self, action=None):
        self.action = action
        self.created = None
        self.window = None

    def create_window(self, title, url, **options):
        self.created = (title, url, options)
        self.window = NativeWindow(options)
        return self.window

    def start(self, **options):
        if self.action:
            self.action(self.window)


class OwnedRuntime:
    def __init__(self):
        self.starts = 0
        self.closes = 0
        self.live = False
        self.server = SimpleNamespace(url="http://127.0.0.1:49123/#owned-token")

    def start_server(self):
        self.starts += 1
        self.live = True
        return self.server

    def close(self):
        self.closes += 1
        self.live = False


class DesktopWindowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.geometry_path = Path(self.directory.name) / "desktop_window.json"
        self.patches = [
            mock.patch.object(shell, "_geometry_path", self.geometry_path),
            mock.patch.object(shell, "wait_for_backend", side_effect=AssertionError("must own its backend")),
            mock.patch.object(shell, "report_desktop_error", create=True),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_desktop(self, native, runtime):
        with mock.patch.dict(sys.modules, {"webview": native}):
            return shell.main([], runtime=runtime)

    def test_desktop_starts_its_backend_and_uses_normal_native_frame(self):
        runtime, native = OwnedRuntime(), NativeHost()
        self.assertEqual(self.run_desktop(native, runtime), 0)
        title, url, options = native.created
        self.assertEqual(title, "Auralis")
        self.assertEqual(url, "http://127.0.0.1:49123/#owned-token")
        self.assertIs(options["frameless"], False)
        self.assertIs(options["on_top"], False)
        self.assertIs(options["resizable"], True)
        self.assertEqual(runtime.starts, 1)
        self.assertEqual(runtime.closes, 1)
        self.assertFalse(runtime.live)

    def test_native_close_and_event_loop_return_close_runtime_once(self):
        runtime = OwnedRuntime()
        def close(window):
            self.assertTrue(runtime.live)
            window.events.closing.fire()
            window.events.closed.fire()
            self.assertFalse(runtime.live)
        self.assertEqual(self.run_desktop(NativeHost(close), runtime), 0)
        self.assertEqual(runtime.closes, 1)

    def test_native_start_failure_closes_runtime_and_surfaces_error(self):
        def fail(_):
            raise RuntimeError("WebView2 could not initialize")
        runtime = OwnedRuntime()
        self.assertEqual(self.run_desktop(NativeHost(fail), runtime), 1)
        self.assertEqual(runtime.closes, 1)
        self.assertIn("WebView2", shell.report_desktop_error.call_args.args[0])

    def test_startup_failure_stops_runtime_before_displaying_modal_error(self):
        runtime = OwnedRuntime()
        def fail(_):
            raise RuntimeError("native startup failed")
        def show_error(_):
            self.assertFalse(runtime.live, "an error dialog must not keep voice or server alive")
        shell.report_desktop_error.side_effect = show_error
        self.assertEqual(self.run_desktop(NativeHost(fail), runtime), 1)

    def test_create_window_failure_closes_runtime_and_surfaces_error(self):
        runtime, native = OwnedRuntime(), NativeHost()
        native.create_window = mock.Mock(side_effect=RuntimeError("native host unavailable"))
        self.assertEqual(self.run_desktop(native, runtime), 1)
        self.assertEqual(runtime.closes, 1)
        self.assertIn("native host unavailable", shell.report_desktop_error.call_args.args[0])

    def test_missing_dependency_is_actionable_without_opening_browser(self):
        runtime = OwnedRuntime()
        with mock.patch.dict(sys.modules, {"webview": None}), mock.patch.object(shell.webbrowser, "open") as browser:
            self.assertEqual(shell.main([], runtime=runtime), 1)
        self.assertEqual(runtime.starts, 0)
        self.assertEqual(runtime.closes, 1)
        self.assertIn("pywebview", shell.report_desktop_error.call_args.args[0])
        self.assertIn("requirements.txt", shell.report_desktop_error.call_args.args[0])
        browser.assert_not_called()

    def test_backend_start_failure_is_reported_and_cleaned_up(self):
        runtime = OwnedRuntime()
        runtime.start_server = mock.Mock(side_effect=OSError("port is occupied"))
        self.assertEqual(self.run_desktop(NativeHost(), runtime), 1)
        self.assertEqual(runtime.closes, 1)
        self.assertIn("port is occupied", shell.report_desktop_error.call_args.args[0])

    def test_geometry_round_trip_tracks_native_drag_and_resize(self):
        def move_and_resize(window):
            window.width, window.height = 1024, 760
            window.x, window.y = 120, 90
            window.events.moved.fire()
            window.events.resized.fire()
            window.events.closing.fire()
        self.assertEqual(self.run_desktop(NativeHost(move_and_resize), OwnedRuntime()), 0)
        saved = json.loads(self.geometry_path.read_text())
        self.assertEqual(saved["desktop"], {"width": 1024, "height": 760, "x": 120, "y": 90})
        native = NativeHost()
        self.assertEqual(self.run_desktop(native, OwnedRuntime()), 0)
        options = native.created[2]
        self.assertEqual((options["width"], options["height"], options["x"], options["y"]),
                         (1024, 760, 120, 90))

    def test_restore_ignores_old_companion_sizes_and_pin_preference(self):
        self.geometry_path.write_text(json.dumps({
            "companion": {"width": 280, "height": 300}, "on_top": True,
        }))
        native = NativeHost()
        self.assertEqual(self.run_desktop(native, OwnedRuntime()), 0)
        options = native.created[2]
        self.assertGreaterEqual(options["width"], 800)
        self.assertGreaterEqual(options["height"], 600)
        self.assertFalse(options["on_top"])

    def test_restored_window_fits_small_screen_after_monitor_disconnect(self):
        self.geometry_path.write_text(json.dumps({"desktop": {
            "x": -3000, "y": 400, "width": 1400, "height": 1200,
        }}))
        native = NativeHost()
        native.screens = [SimpleNamespace(x=0, y=0, width=800, height=480)]
        self.assertEqual(self.run_desktop(native, OwnedRuntime()), 0)
        options = native.created[2]
        self.assertGreaterEqual(options["x"], 0)
        self.assertGreaterEqual(options["y"], 0)
        self.assertLessEqual(options["x"] + options["width"], 800)
        self.assertLessEqual(options["y"] + options["height"], 480)

    def test_invalid_saved_geometry_uses_safe_defaults(self):
        self.geometry_path.write_text(json.dumps({"desktop": {
            "x": "yesterday", "y": None, "width": "wide", "height": False,
        }}))
        native = NativeHost()
        self.assertEqual(self.run_desktop(native, OwnedRuntime()), 0)
        options = native.created[2]
        self.assertGreaterEqual(options["width"], 800)
        self.assertGreaterEqual(options["height"], 600)

    def test_saved_geometry_without_position_tolerates_invalid_size(self):
        self.geometry_path.write_text(json.dumps({"desktop": {"width": "wide"}}))
        native = NativeHost()
        self.assertEqual(self.run_desktop(native, OwnedRuntime()), 0)
        self.assertGreaterEqual(native.created[2]["width"], 800)

    def test_empty_screen_list_does_not_restore_far_offscreen_coordinates(self):
        self.geometry_path.write_text(json.dumps({"desktop": {
            "x": -30000, "y": -20000, "width": 1000, "height": 760,
        }}))
        native = NativeHost()
        native.screens = []
        self.assertEqual(self.run_desktop(native, OwnedRuntime()), 0)
        options = native.created[2]
        self.assertGreaterEqual(options["x"], 0)
        self.assertGreaterEqual(options["y"], 0)

    def test_actual_desktop_does_not_expose_companion_pin_or_resize_modes(self):
        def check(window):
            api = window.js_api
            self.assertIn("error", api.set_on_top(True))
            self.assertIn("error", api.set_expanded(False))
            self.assertIn("error", api.resize(300, 400))
            self.assertFalse(api.get_window_state()["on_top"])
            self.assertEqual(api.get_endpoint(), {"port": 49123, "token": "owned-token"})
        self.assertEqual(self.run_desktop(NativeHost(check), OwnedRuntime()), 0)

    def test_legacy_resize_failure_does_not_publish_expanded_state(self):
        window = SimpleNamespace(width=410, height=440, x=30, y=100)
        window.resize = mock.Mock(side_effect=RuntimeError("native resize failed"))
        with mock.patch.object(shell, "_window", window), \
             mock.patch.object(shell, "_expanded", False), \
             mock.patch.object(shell, "_geometry", {
                 "companion": {"width": 340, "height": 420},
                 "chat": {"width": 600, "height": 800}, "on_top": True,
             }):
            result = shell.Api().set_expanded(True)
            self.assertFalse(shell._expanded)
            self.assertIn("error", result)


class DesktopEntrypointTests(unittest.TestCase):
    def test_windowed_entrypoint_runs_desktop_with_arguments_and_returns_failure(self):
        captured = []
        def main(argv):
            captured.extend(argv)
            return 1
        with mock.patch.dict(sys.modules, {"overlay.main": SimpleNamespace(main=main)}), \
             mock.patch.object(sys, "argv", ["desktop.pyw", "--debug"]):
            with self.assertRaises(SystemExit) as exited:
                runpy.run_path(str(ROOT / "desktop.pyw"), run_name="__main__")
        self.assertEqual(exited.exception.code, 1)
        self.assertEqual(captured, ["--debug"])


if __name__ == "__main__":
    unittest.main()
