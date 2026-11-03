"""Speech input tests use memory audio and fake devices; never contact Groq."""
import io
import json
import os
import struct
import sys
import threading
import time
import types
import unittest
import wave
from unittest import mock

from tests.helpers import Sandbox
from core import config


def wav_bytes(seconds=0.5, amplitude=3000, rate=16000):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(struct.pack("<h", amplitude) * int(seconds * rate))
    return output.getvalue()


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.005)
    raise AssertionError("background speech work did not reach expected state")


class SpeechTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox().__enter__()
        self.addCleanup(self.sandbox.__exit__, None, None, None)
        self.env = mock.patch.dict(os.environ, {"GROQ_API_KEY": "secret-test-key"})
        self.env.start()
        self.addCleanup(self.env.stop)
        from core import stt
        self.stt = stt
        self.session = "chat-a"
        self.controllers = []

    def controller(self, recorder=None, transcribe=None, before=None):
        recorder = recorder or FakeRecorder()
        controller = self.stt.SpeechController(
            lambda: self.session, before_record=before,
            recorder_factory=lambda: recorder,
            transcribe_fn=transcribe or (lambda wav, key, model: " Hello there "),
        )
        self.controllers.append(controller)
        self.addCleanup(controller.close)
        return controller, recorder

    def finish(self, controller):
        controller.stop()
        eventually(lambda: controller.state()["phase"] != "recording"
                   and controller.state()["phase"] != "transcribing")
        return controller.state()

    def test_missing_key_never_opens_microphone(self):
        controller, recorder = self.controller()
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": ""}):
            with self.assertRaises(self.stt.STTError):
                controller.start(self.session)
        self.assertFalse(recorder.started.is_set())
        self.assertEqual(controller.state()["phase"], "idle")

    def test_capture_is_async_and_transcript_requires_matching_ack(self):
        seen = []
        controller, recorder = self.controller(
            transcribe=lambda audio, key, model: seen.append((audio, key, model)) or "  Hello  "
        )
        self.assertEqual(controller.start(self.session)["phase"], "recording")
        self.assertTrue(recorder.started.wait(1))
        self.assertEqual(controller.state()["level"], 0.5)
        with self.assertRaises(self.stt.STTError):
            controller.start(self.session)
        state = self.finish(controller)
        self.assertEqual(state["result"]["session_id"], "chat-a")
        self.assertEqual(state["result"]["text"], "Hello")
        self.assertEqual(seen[0][1:], ("secret-test-key", "whisper-large-v3-turbo"))
        with self.assertRaises(self.stt.STTError):
            controller.start(self.session)
        result_id = state["result"]["id"]
        self.assertIsNotNone(controller.acknowledge(result_id + 100)["result"])
        self.assertIsNone(controller.acknowledge(result_id)["result"])

    def test_before_record_runs_without_controller_lock(self):
        entered = threading.Event()
        holder = {}

        def before():
            holder["controller"].state()
            entered.set()

        controller, recorder = self.controller(before=before)
        holder["controller"] = controller
        controller.start(self.session)
        self.assertTrue(entered.wait(1))
        self.assertTrue(recorder.started.wait(1))
        self.finish(controller)

    def test_silence_and_short_capture_never_upload(self):
        for audio in (wav_bytes(amplitude=0), wav_bytes(seconds=0.05)):
            with self.subTest(audio_length=len(audio)):
                seen = []
                controller, recorder = self.controller(
                    FakeRecorder(audio), lambda *args: seen.append(args) or "incorrect"
                )
                controller.start(self.session)
                self.assertTrue(recorder.started.wait(1))
                state = self.finish(controller)
                self.assertEqual(seen, [])
                self.assertEqual(state["phase"], "error")
                self.assertIsNone(state["result"])

    def test_cancel_during_network_discards_late_result_and_allows_new_capture(self):
        entered, release = threading.Event(), threading.Event()

        def slow(*args):
            entered.set()
            release.wait(2)
            return "old result"

        controller, recorder = self.controller(transcribe=slow)
        controller.start(self.session)
        self.assertTrue(recorder.started.wait(1))
        controller.stop()
        self.assertTrue(entered.wait(1))
        self.assertEqual(controller.cancel()["phase"], "idle")
        controller._transcribe = lambda *args: "new result"
        second = FakeRecorder()
        controller._recorder_factory = lambda: second
        controller.start(self.session)
        self.assertTrue(second.started.wait(1))
        release.set()
        self.assertEqual(self.finish(controller)["result"]["text"], "new result")
        self.assertTrue(second.cancelled)

    def test_session_switch_discards_late_transcript(self):
        entered, release = threading.Event(), threading.Event()

        def slow(*args):
            entered.set()
            release.wait(2)
            return "belongs to chat-a"

        controller, recorder = self.controller(transcribe=slow)
        controller.start(self.session)
        self.assertTrue(recorder.started.wait(1))
        controller.stop()
        self.assertTrue(entered.wait(1))
        self.session = "chat-b"
        release.set()
        eventually(lambda: controller.state()["phase"] == "error")
        self.assertIsNone(controller.state()["result"])
        self.assertIn("session", controller.state()["error"].lower())

    def test_session_switch_also_clears_already_finished_result(self):
        controller, recorder = self.controller()
        controller.start(self.session)
        self.assertTrue(recorder.started.wait(1))
        self.assertIsNotNone(self.finish(controller)["result"])
        self.session = "chat-b"
        self.assertIsNone(controller.state()["result"])
        self.assertEqual(controller.state()["phase"], "error")

    def test_cancel_and_close_release_capture_without_waiting_for_worker(self):
        controller, recorder = self.controller()
        controller.start(self.session)
        self.assertTrue(recorder.started.wait(1))
        controller.close()
        controller.close()
        self.assertTrue(recorder.cancelled)
        self.assertEqual(controller.state()["phase"], "idle")
        with self.assertRaises(self.stt.STTError):
            controller.start(self.session)

    def test_close_does_not_wait_for_inflight_http_and_discards_response(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def slow(*args):
            entered.set()
            release.wait(2)
            finished.set()
            return "late result"

        controller, recorder = self.controller(transcribe=slow)
        controller.start(self.session)
        self.assertTrue(recorder.started.wait(1))
        controller.stop()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        controller.close()
        self.assertLess(time.monotonic() - started, 0.2)
        release.set()
        self.assertTrue(finished.wait(1))
        self.assertIsNone(controller.state()["result"])
        self.assertEqual(controller.state()["phase"], "idle")

    def test_stale_capture_cleanup_cannot_close_new_capture(self):
        release, old_returned = threading.Event(), threading.Event()

        class SlowRecorder(FakeRecorder):
            def record(self, stop_event, cancelled_event, progress):
                self.started.set()
                release.wait(2)
                old_returned.set()
                return self.audio

        first, second = SlowRecorder(), FakeRecorder()
        recorders = iter((first, second))
        controller = self.stt.SpeechController(
            lambda: self.session, recorder_factory=lambda: next(recorders),
            transcribe_fn=lambda *args: "new capture",
        )
        self.addCleanup(controller.close)
        controller.start(self.session)
        self.assertTrue(first.started.wait(1))
        controller.cancel()
        controller.start(self.session)
        self.assertTrue(second.started.wait(1))
        release.set()
        self.assertTrue(old_returned.wait(1))
        self.assertFalse(second.cancelled)
        self.assertEqual(self.finish(controller)["result"]["text"], "new capture")

    def test_published_idle_result_is_not_still_busy_during_recorder_cleanup(self):
        cleanup_entered, cleanup_release = threading.Event(), threading.Event()

        class CleanupRecorder(FakeRecorder):
            releases = 0

            def cancel(self):
                self.releases += 1
                if self.releases == 2:
                    cleanup_entered.set()
                    cleanup_release.wait(2)
                super().cancel()

        controller, recorder = self.controller(CleanupRecorder())
        self.addCleanup(cleanup_release.set)
        controller.start(self.session)
        self.assertTrue(recorder.started.wait(1))
        controller.stop()
        self.assertTrue(cleanup_entered.wait(1))
        state = controller.state()
        self.assertEqual(state["phase"], "idle")
        controller.acknowledge(state["result"]["id"])
        self.assertEqual(controller.configure({"shortcut": "f9"})["shortcut"], "f9")

    def test_device_and_injected_errors_never_echo_secrets(self):
        controller, recorder = self.controller(FakeRecorder(error=RuntimeError("secret-test-key")))
        controller.start(self.session)
        eventually(lambda: controller.state()["phase"] == "error")
        self.assertNotIn("secret-test-key", controller.state()["error"])
        self.assertTrue(recorder.cancelled)

    def test_settings_merge_preserves_other_settings_and_validates_choices(self):
        saved = json.loads(self.sandbox.settings_path.read_text())
        saved["custom"] = {"keep": True}
        saved["stt"] = {"model": "bogus", "shortcut": "ctrl+c", "key": "must-not-persist"}
        self.sandbox.settings_path.write_text(json.dumps(saved))
        controller, _ = self.controller()
        self.assertEqual(controller.settings(), {
            "provider": "groq", "model": "whisper-large-v3-turbo", "shortcut": "f7"
        })
        controller.configure({"model": "whisper-large-v3", "shortcut": "f10"})
        after = json.loads(self.sandbox.settings_path.read_text())
        self.assertEqual(after["custom"], {"keep": True})
        self.assertEqual(after["terminal"], saved["terminal"])
        self.assertEqual(after["stt"], {
            "provider": "groq", "model": "whisper-large-v3", "shortcut": "f10"
        })
        for values in ({"model": "unknown"}, {"shortcut": "f1"}, {"api_key": "secret"}):
            with self.subTest(values=values), self.assertRaises(self.stt.STTError):
                controller.configure(values)

    def test_configure_busy_and_save_failure_leave_settings_and_state_unchanged(self):
        controller, recorder = self.controller()
        old_settings = controller.settings()
        old_state = controller.state()
        with mock.patch.object(config, "save_settings", side_effect=OSError("secret-test-key")):
            with self.assertRaises(self.stt.STTError) as caught:
                controller.configure({"shortcut": "f9"})
        self.assertNotIn("secret-test-key", str(caught.exception))
        self.assertEqual(controller.settings(), old_settings)
        self.assertEqual(controller.state(), old_state)
        controller.start(self.session)
        self.assertTrue(recorder.started.wait(1))
        with self.assertRaises(self.stt.STTError):
            controller.configure({"shortcut": "f9"})
        self.assertEqual(controller.settings(), old_settings)

    def test_transcribe_posts_wav_without_language_redirects_or_retries(self):
        calls = []
        response = types.SimpleNamespace(status_code=200, json=lambda: {"text": " Hello "})

        def post(url, **kwargs):
            calls.append((url, kwargs))
            return response

        audio = wav_bytes()
        self.assertEqual(self.stt.transcribe_audio(audio, "the-key", "whisper-large-v3", post=post), "Hello")
        (url, kwargs), = calls
        self.assertEqual(url, "https://api.groq.com/openai/v1/audio/transcriptions")
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer the-key"})
        self.assertEqual(kwargs["data"], {"model": "whisper-large-v3", "response_format": "json"})
        self.assertEqual(kwargs["files"]["file"], ("speech.wav", audio, "audio/wav"))
        self.assertEqual(kwargs["timeout"], (3, 30))
        self.assertIs(kwargs["allow_redirects"], False)

    def test_provider_errors_do_not_include_body_or_exception_message(self):
        for status in (302, 401, 429, 500):
            response = types.SimpleNamespace(status_code=status, text="secret-test-key")
            with self.subTest(status=status), self.assertRaises(self.stt.STTError) as caught:
                self.stt.transcribe_audio(wav_bytes(), "secret-test-key", "whisper-large-v3", post=lambda *a, **kw: response)
            self.assertNotIn("secret-test-key", str(caught.exception))
        with self.assertRaises(self.stt.STTError) as caught:
            self.stt.transcribe_audio(wav_bytes(), "secret-test-key", "whisper-large-v3", post=mock.Mock(side_effect=OSError("secret-test-key")))
        self.assertNotIn("secret-test-key", str(caught.exception))
        for body in ({"text": ""}, {"text": 42}, {}, ["secret-test-key"]):
            response = types.SimpleNamespace(status_code=200, json=lambda: body)
            with self.subTest(body=body), self.assertRaises(self.stt.STTError):
                self.stt.transcribe_audio(wav_bytes(), "secret-test-key", "whisper-large-v3", post=lambda *a, **kw: response)

    def test_optional_sounddevice_is_loaded_only_for_capture(self):
        with mock.patch.dict(sys.modules, {"sounddevice": None}):
            controller = self.stt.SpeechController(lambda: self.session)
            self.addCleanup(controller.close)
            self.assertEqual(controller.state()["phase"], "idle")
            controller.start(self.session)
            eventually(lambda: controller.state()["phase"] == "error")
            self.assertIn("sounddevice", controller.state()["error"])

    def test_native_capture_uses_device_rate_caps_sixty_seconds_and_closes(self):
        streams = []

        class Stream:
            def __init__(self, **kwargs):
                self.options = kwargs
                self.closed = False
                streams.append(self)

            def start(self):
                self.options["callback"](struct.pack("<h", 3000) * (8000 * 61), 8000 * 61, None, None)

            def abort(self):
                pass

            def close(self):
                self.closed = True

        device = types.SimpleNamespace(query_devices=lambda **kw: {"default_samplerate": 8000}, RawInputStream=Stream)
        with mock.patch.dict(sys.modules, {"sounddevice": device}):
            recorder = self.stt.MicrophoneRecorder()
            audio = recorder.record(threading.Event(), threading.Event(), lambda *args: None)
        self.assertTrue(streams[0].closed)
        self.assertEqual(streams[0].options["samplerate"], 8000)
        self.assertEqual(streams[0].options["channels"], 1)
        self.assertEqual(streams[0].options["dtype"], "int16")
        with wave.open(io.BytesIO(audio)) as captured:
            self.assertEqual(captured.getnframes(), 8000 * 60)
            self.assertEqual(captured.getframerate(), 8000)

    def test_native_cancel_promptly_closes_stream_while_record_waits(self):
        opened, closed = threading.Event(), threading.Event()

        class Stream:
            def __init__(self, **kwargs):
                pass

            def start(self):
                opened.set()

            def abort(self):
                pass

            def close(self):
                closed.set()

        device = types.SimpleNamespace(query_devices=lambda **kw: {"default_samplerate": 48000}, RawInputStream=Stream)
        recorder = self.stt.MicrophoneRecorder()
        result = []
        with mock.patch.dict(sys.modules, {"sounddevice": device}):
            worker = threading.Thread(target=lambda: result.append(recorder.record(
                threading.Event(), threading.Event(), lambda *args: None)))
            worker.start()
            self.assertTrue(opened.wait(1))
            recorder.cancel()
            self.assertTrue(closed.is_set())
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [b""])

    def test_device_start_error_closes_stream_and_default_rate_falls_back(self):
        options, closed = [], threading.Event()

        class Stream:
            def __init__(self, **kwargs):
                options.append(kwargs)

            def start(self):
                raise OSError("secret-test-key")

            def abort(self):
                pass

            def close(self):
                closed.set()

        device = types.SimpleNamespace(query_devices=mock.Mock(side_effect=OSError("no device")), RawInputStream=Stream)
        with mock.patch.dict(sys.modules, {"sounddevice": device}):
            with self.assertRaises(self.stt.STTError) as caught:
                self.stt.MicrophoneRecorder().record(threading.Event(), threading.Event(), lambda *args: None)
        self.assertTrue(closed.is_set())
        self.assertEqual(options[0]["samplerate"], 16000)
        self.assertNotIn("secret-test-key", str(caught.exception))


class FakeRecorder:
    def __init__(self, audio=None, error=None):
        self.audio = wav_bytes() if audio is None else audio
        self.error = error
        self.started = threading.Event()
        self.cancelled = False

    def record(self, stop_event, cancelled_event, progress):
        self.started.set()
        if self.error:
            raise self.error
        progress(0.5, 0.5)
        while not stop_event.wait(0.005):
            if cancelled_event.is_set():
                return b""
        return self.audio

    def cancel(self):
        self.cancelled = True


if __name__ == "__main__":
    unittest.main()
