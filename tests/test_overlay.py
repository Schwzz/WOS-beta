"""Tests for the WOS-Overlay backend bridge, HTTP server and speech events.

Nothing here talks to Fish Audio, a model or a real window. The HTTP server
is the real one, bound to 127.0.0.1 on a free port; the session, speaker
post/player and model are fakes (or the real Session with a mocked model).
"""
import http.client
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from array import array
from pathlib import Path
from unittest import mock

from tests.helpers import Sandbox, DEFAULT_SETTINGS, PROJECT_ROOT
from tests.test_tts import (
    FakePlayer, FakePost, FakeResponse, KEY, make_wav, make_speaker,
)
from core import config, overlay, tts


def pcm(amplitudes, window=882, rate=44100):
    """16-bit mono PCM: one sine burst of the given amplitude per window."""
    samples = array("h")

    for amp in amplitudes:
        for i in range(window):
            samples.append(int(amp * math.sin(2 * math.pi * 440 * i / rate)))

    return samples.tobytes()


class FakeSession:
    def __init__(self, reply="Hello from Nexis"):
        self.reply = reply
        self.character_name = "Nexis"
        self.chat = {"messages": []}
        self.calls = []
        self.gate = None          # an Event the model call waits for
        self.error = None

    def send_message(self, text):
        self.calls.append(text)

        if self.gate is not None:
            self.gate.wait(5)

        if self.error is not None:
            raise self.error

        if isinstance(self.reply, str):
            self.chat["messages"] += [
                {"role": "user", "content": text},
                {"role": "assistant", "content": self.reply},
            ]

        return self.reply


class FakeSpeaker:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.spoken = []
        self.listener = None

    def speak(self, text):
        self.spoken.append(text)
        return True

    def set_enabled(self, value):
        self.enabled = value


def make_backend(session=None, speaker=None, notices=None):
    session = session or FakeSession()
    speaker = speaker or FakeSpeaker(enabled=True)
    backend = overlay.OverlayBackend(
        lambda: session, speaker,
        notify=(notices.append if notices is not None else None),
    )
    return backend, session, speaker


# ---------------------------------------------------------------- envelope

class WavEnvelopeTests(unittest.TestCase):
    def test_silence_is_all_zero_with_correct_duration(self):
        wav = make_wav(pcm([0] * 10))
        result = tts.wav_envelope(wav)
        self.assertEqual(result["levels"], [0.0] * 10)
        self.assertAlmostEqual(result["duration"], 10 * 882 / 44100, places=3)
        self.assertEqual(result["fps"], 50)

    def test_levels_follow_the_audio_and_peak_is_one(self):
        wav = make_wav(pcm([20000, 20000, 2000, 2000, 0]))
        levels = tts.wav_envelope(wav)["levels"]
        self.assertEqual(len(levels), 5)
        self.assertEqual(max(levels), 1.0)
        self.assertGreater(levels[0], 0.9)
        self.assertLess(levels[2], 0.2)
        self.assertGreater(levels[2], 0.0)
        self.assertEqual(levels[4], 0.0)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in levels))

    def test_louder_speech_gives_higher_levels_than_quieter(self):
        wav = make_wav(pcm([30000, 6000]))
        loud, quiet = tts.wav_envelope(wav)["levels"]
        self.assertGreater(loud, quiet * 3)

    def test_stereo_duration(self):
        wav = make_wav(pcm([10000] * 4) * 2, channels=2)
        result = tts.wav_envelope(wav)
        self.assertAlmostEqual(result["duration"], 4 * 882 / 44100, places=3)

    def test_unsupported_or_garbage_audio_gives_empty_levels_never_raises(self):
        eight_bit = bytearray(make_wav(pcm([1000])))
        eight_bit[34:36] = (8).to_bytes(2, "little")

        for wav in (bytes(eight_bit), b"", b"junk", None, 5):
            with self.subTest(wav=type(wav).__name__):
                self.assertEqual(tts.wav_envelope(wav)["levels"], [])


# ------------------------------------------------------- speaker events

class Collector:
    def __init__(self):
        self.events = []

    def __call__(self, event, generation, **info):
        self.events.append((event, generation, info))

    def names(self):
        return [e[0] for e in self.events]


class SpeakerEventTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {tts.API_KEY_ENV: KEY})
        patcher.start()
        self.addCleanup(patcher.stop)

    def speaker(self, post=None, player=None, **kw):
        collector = Collector()
        speaker = make_speaker(post=post, player=player, **kw)
        speaker.listener = collector
        return speaker, collector

    def test_success_emits_queued_start_end_with_real_envelope(self):
        wav = make_wav(pcm([20000, 10000, 0]))
        player = FakePlayer()
        speaker, events = self.speaker(
            post=FakePost(FakeResponse(content=wav)), player=player
        )
        speaker.speak("Hello")
        speaker.wait()

        self.assertEqual(events.names(), ["queued", "start", "end"])
        envelope = events.events[1][2]["envelope"]
        self.assertEqual(len(envelope["levels"]), 3)
        self.assertEqual(envelope["levels"][0], 1.0)
        self.assertEqual(len(player.played), 1)

    def test_start_is_emitted_before_playback_and_end_after(self):
        order = []
        player = FakePlayer()
        original = player.play

        def play(path, **kwargs):
            order.append("play")
            original(path, **kwargs)

        player.play = play
        speaker, _ = self.speaker(player=player)
        speaker.listener = lambda event, gen, **info: order.append(event)
        speaker.speak("Hello")
        speaker.wait()
        self.assertEqual(order, ["queued", "play", "start", "end"])

    def test_failures_end_without_a_start(self):
        for post in (FakePost(error=ConnectionError("x")),
                     FakePost(FakeResponse(status=500, content=b"x")),
                     FakePost(FakeResponse(content=b"not wav"))):
            with self.subTest(post=post.error or post.response.status_code):
                speaker, events = self.speaker(post=post)
                speaker.speak("Hello")
                speaker.wait()
                self.assertEqual(events.names(), ["queued", "end"])

    def test_player_failure_still_ends(self):
        speaker, events = self.speaker(player=FakePlayer(error=OSError("x")))
        speaker.speak("Hello")
        speaker.wait()
        self.assertEqual(events.names(), ["queued", "start", "end"])

    def test_superseded_request_ends_every_queued_id(self):
        release = threading.Event()
        started = threading.Event()

        def post(url, **kwargs):
            if kwargs["json"]["text"] == "first":
                started.set()
                release.wait(5)

            return FakeResponse()

        player = FakePlayer()
        speaker, events = self.speaker(post=post, player=player)
        speaker.speak("first")
        started.wait(5)
        speaker.speak("second")
        release.set()
        speaker.wait()

        queued = [g for e, g, _ in events.events if e == "queued"]
        ended = [g for e, g, _ in events.events if e == "end"]
        self.assertEqual(sorted(queued), sorted(ended))
        self.assertEqual(len(queued), 2)
        self.assertEqual(len(player.played), 1)

    def test_nothing_is_emitted_when_disabled_or_unspeakable(self):
        speaker, events = self.speaker(settings={"enabled": False})
        speaker.speak("Hello")
        speaker.set_enabled(True)
        speaker.speak("   ")
        speaker.speak("I couldn't save this exchange: x")
        speaker.wait()
        self.assertEqual(events.events, [])

    def test_set_enabled_switches_speech_on_and_off(self):
        post = FakePost()
        speaker, events = self.speaker(post=post, settings={"enabled": False})

        speaker.speak("one")
        self.assertEqual(post.calls, [])

        speaker.set_enabled(True)
        speaker.speak("two")
        speaker.wait()
        self.assertEqual(len(post.calls), 1)

        speaker.set_enabled(False)
        speaker.speak("three")
        speaker.wait()
        self.assertEqual(len(post.calls), 1)

    def test_turning_voice_off_cuts_off_speech_in_progress(self):
        release = threading.Event()
        started = threading.Event()

        def post(url, **kwargs):
            started.set()
            release.wait(5)
            return FakeResponse()

        player = FakePlayer()
        speaker, events = self.speaker(post=post, player=player)
        speaker.speak("Hello")
        started.wait(5)
        speaker.set_enabled(False)
        release.set()
        speaker.wait()

        self.assertEqual(player.played, [])
        self.assertIn("end", events.names())
        self.assertGreater(player.stops, 0)

    def test_set_enabled_only_accepts_a_real_true(self):
        speaker, _ = self.speaker(settings={"enabled": False})
        speaker.set_enabled("yes")
        self.assertFalse(speaker.enabled)

    def test_failing_listener_is_contained(self):
        speaker, _ = self.speaker()

        def broken(event, generation, **info):
            raise RuntimeError("ui gone")

        speaker.listener = broken
        self.assertTrue(speaker.speak("Hello"))
        speaker.wait()

    def test_thread_start_failure_emits_end(self):
        speaker, events = self.speaker()

        with mock.patch.object(
            threading.Thread, "start", side_effect=RuntimeError("no threads")
        ):
            self.assertFalse(speaker.speak("Hello"))

        self.assertEqual(events.names(), ["queued", "end"])


# ------------------------------------------------------------- backend

class BackendMessageTests(unittest.TestCase):
    def test_ui_message_reaches_session_once_and_is_spoken_once(self):
        notices = []
        backend, session, speaker = make_backend(notices=notices)
        reply = backend.send_message("  hi there  ")

        self.assertEqual(reply, "Hello from Nexis")
        self.assertEqual(session.calls, ["hi there"])
        self.assertEqual(speaker.spoken, ["Hello from Nexis"])
        self.assertEqual(
            notices, ["You (overlay): hi there", "Nexis: Hello from Nexis"]
        )

    def test_terminal_path_is_unchanged_and_does_not_speak_or_print(self):
        notices = []
        backend, session, speaker = make_backend(notices=notices)
        reply = backend.send_message("hello", "terminal")

        self.assertEqual(reply, "Hello from Nexis")
        self.assertEqual(session.calls, ["hello"])
        self.assertEqual(speaker.spoken, [])
        self.assertEqual(notices, [])

    def test_terminal_none_reply_is_returned_as_is(self):
        backend, session, _ = make_backend(FakeSession(reply=None))
        self.assertIsNone(backend.send_message("hello", "terminal"))

    def test_ui_rejects_bad_input_without_touching_the_session(self):
        backend, session, speaker = make_backend()
        cases = [None, "", "   ", 5, "/model", "  /help", "x" * 4001]

        for text in cases:
            with self.subTest(text=str(text)[:10]):
                with self.assertRaises(overlay.BackendError):
                    backend.send_message(text)

        self.assertEqual(session.calls, [])
        self.assertEqual(speaker.spoken, [])

    def test_model_failure_is_reported_and_never_spoken(self):
        backend, session, speaker = make_backend(FakeSession(reply=None))

        with self.assertRaises(overlay.BackendError) as caught:
            backend.send_message("hi")

        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(speaker.spoken, [])
        self.assertEqual(backend.get_state()["state"], "idle")

    def test_session_exception_is_contained_for_ui_and_kept_for_terminal(self):
        session = FakeSession()
        session.error = RuntimeError("secret detail")
        backend, _, speaker = make_backend(session)

        with self.assertRaises(overlay.BackendError) as caught:
            backend.send_message("hi")

        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(backend.get_state()["state"], "idle")

        with self.assertRaises(RuntimeError):
            backend.send_message("hi", "terminal")

    def test_chat_version_increases_for_both_sources(self):
        backend, _, _ = make_backend()
        v0 = backend.get_state()["chat_version"]
        backend.send_message("a")
        v1 = backend.get_state()["chat_version"]
        backend.send_message("b", "terminal")
        v2 = backend.get_state()["chat_version"]
        self.assertTrue(v0 < v1 < v2)


class BackendStateTests(unittest.TestCase):
    def test_idle_thinking_speaking_idle_follows_real_events(self):
        session = FakeSession()
        session.gate = threading.Event()
        backend, _, _ = make_backend(session)
        self.assertEqual(backend.get_state()["state"], "idle")

        result = {}
        thread = threading.Thread(
            target=lambda: result.update(r=backend.send_message("hi"))
        )
        thread.start()

        deadline = time.time() + 5
        while not session.calls and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(backend.get_state()["state"], "thinking")
        session.gate.set()
        thread.join(5)
        self.assertEqual(backend.get_state()["state"], "idle")

        # The speaker reports the speech lifecycle.
        backend.on_speech_event("queued", 1)
        self.assertEqual(backend.get_state()["state"], "thinking")
        backend.on_speech_event(
            "start", 1, envelope={"fps": 50, "duration": 1.0, "levels": [1, 0.5]}
        )
        state = backend.get_state()
        self.assertEqual(state["state"], "speaking")
        self.assertEqual(state["audio"]["id"], 1)
        backend.on_speech_event("end", 1)
        state = backend.get_state()
        self.assertEqual(state["state"], "idle")
        self.assertIsNone(state["audio"])

    def test_failed_speech_returns_to_idle_without_speaking(self):
        backend, _, _ = make_backend()
        backend.on_speech_event("queued", 7)
        self.assertEqual(backend.get_state()["state"], "thinking")
        backend.on_speech_event("end", 7)
        self.assertEqual(backend.get_state()["state"], "idle")

    def test_stale_end_does_not_stop_newer_speech(self):
        backend, _, _ = make_backend()
        env = {"fps": 50, "duration": 1.0, "levels": [1]}
        backend.on_speech_event("queued", 1)
        backend.on_speech_event("queued", 2)
        backend.on_speech_event("start", 2, envelope=env)
        backend.on_speech_event("end", 1)
        self.assertEqual(backend.get_state()["state"], "speaking")
        backend.on_speech_event("end", 2)
        self.assertEqual(backend.get_state()["state"], "idle")

    def test_levels_are_sent_only_when_the_ui_does_not_have_them(self):
        backend, _, _ = make_backend()
        backend.on_speech_event(
            "start", 3, envelope={"fps": 50, "duration": 0.04, "levels": [1, 0]}
        )
        first = backend.get_state()["audio"]
        self.assertEqual(first["levels"], [1, 0])
        self.assertEqual(first["fps"], 50)
        self.assertGreaterEqual(first["elapsed"], 0)
        again = backend.get_state(have_audio_id=3)["audio"]
        self.assertNotIn("levels", again)
        other = backend.get_state(have_audio_id=2)["audio"]
        self.assertIn("levels", other)

    def test_missing_envelope_still_reports_speaking(self):
        backend, _, _ = make_backend()
        backend.on_speech_event("start", 1, envelope={"levels": []})
        backend.on_speech_event("start", 2)
        self.assertEqual(backend.get_state()["state"], "speaking")
        self.assertEqual(backend.get_state()["audio"]["levels"], [])

    def test_voice_availability_reflects_the_environment(self):
        backend, _, speaker = make_backend(speaker=FakeSpeaker(enabled=True))

        with mock.patch.dict(os.environ, {tts.API_KEY_ENV: KEY}):
            voice = backend.get_state()["voice"]
            self.assertEqual(voice, {"enabled": True, "available": True})

        with mock.patch.dict(os.environ, clear=True):
            self.assertFalse(backend.get_state()["voice"]["available"])

    def test_state_never_contains_the_api_key(self):
        backend, _, _ = make_backend()

        with mock.patch.dict(os.environ, {tts.API_KEY_ENV: KEY}):
            backend.send_message("hi")
            text = json.dumps(
                [backend.get_state(), backend.get_history()]
            )

        self.assertNotIn(KEY, text)


class BackendSharingTests(unittest.TestCase):
    def test_terminal_command_blocks_ui_messages_until_released(self):
        backend, session, _ = make_backend()

        outcome = []

        def from_ui_thread():
            try:
                backend.send_message("hi")
                outcome.append("sent")
            except overlay.BackendBusy:
                outcome.append("busy")

        with mock.patch.object(overlay, "BUSY_TIMEOUT_SECONDS", 0.1):
            backend.hold_command()
            backend.hold_command()                 # idempotent

            # The UI runs on another thread, like the HTTP handler does.
            thread = threading.Thread(target=from_ui_thread)
            thread.start()
            thread.join(5)
            self.assertEqual(outcome, ["busy"])
            self.assertEqual(session.calls, [])

            backend.release_command()
            backend.release_command()              # idempotent
            thread = threading.Thread(target=from_ui_thread)
            thread.start()
            thread.join(5)
            self.assertEqual(outcome, ["busy", "sent"])
            self.assertEqual(session.calls, ["hi"])

    def test_terminal_send_works_while_the_terminal_holds_the_lock(self):
        backend, session, _ = make_backend()
        backend.hold_command()
        backend.send_message("hi", "terminal")
        backend.release_command()
        self.assertEqual(session.calls, ["hi"])

    def test_messages_from_ui_and_terminal_never_overlap(self):
        active, peak = [0], [0]
        guard = threading.Lock()

        class Slow(FakeSession):
            def send_message(self, text):
                with guard:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])

                time.sleep(0.03)

                with guard:
                    active[0] -= 1

                return "ok"

        backend, _, _ = make_backend(Slow())
        threads = [
            threading.Thread(target=backend.send_message, args=("a", "ui")),
            threading.Thread(target=backend.send_message, args=("b", "terminal")),
            threading.Thread(target=backend.send_message, args=("c", "ui")),
        ]
        [t.start() for t in threads]
        [t.join(5) for t in threads]
        self.assertEqual(peak[0], 1)

    def test_history_returns_only_chat_messages(self):
        session = FakeSession()
        session.chat["messages"] = [
            {"role": "system", "content": "secret prompt"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": 5},
            "junk",
        ]
        backend, _, _ = make_backend(session)
        history = backend.get_history()
        self.assertEqual(
            history["messages"],
            [{"role": "user", "content": "hi"},
             {"role": "assistant", "content": "hello"}],
        )
        self.assertEqual(history["character"], "Nexis")

    def test_history_is_limited(self):
        session = FakeSession()
        session.chat["messages"] = [
            {"role": "user", "content": str(i)} for i in range(300)
        ]
        backend, _, _ = make_backend(session)
        self.assertEqual(
            len(backend.get_history()["messages"]), overlay.HISTORY_LIMIT
        )


class RealSessionSharingTests(unittest.TestCase):
    """The real Session, with only the model call mocked."""

    def test_ui_and_terminal_messages_land_in_one_chat_and_one_model_call(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            backend = overlay.OverlayBackend(lambda: session, FakeSpeaker())

            with mock.patch(
                "core.session.chat", side_effect=["from ui", "from terminal"]
            ) as model:
                backend.send_message("first", "ui")
                backend.send_message("second", "terminal")

            self.assertEqual(model.call_count, 2)       # no double processing
            saved = sandbox.read_chat(session.session_id)["messages"]
            self.assertEqual(
                [(m["role"], m["content"]) for m in saved],
                [("user", "first"), ("assistant", "from ui"),
                 ("user", "second"), ("assistant", "from terminal")],
            )
            history = backend.get_history()["messages"]
            self.assertEqual(len(history), 4)

    def test_memory_command_text_through_the_ui_uses_shared_memory(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            backend = overlay.OverlayBackend(lambda: session, FakeSpeaker())
            backend.send_message("remember that I like tea")
            self.assertTrue(
                any("tea" in str(item.get("value")) for item in session.memory)
            )


# ------------------------------------------------------ voice setting

class VoiceSettingTests(unittest.TestCase):
    def test_default_is_off(self):
        with Sandbox():
            raw = config.load_settings().get("tts")
            self.assertIs(tts.load_tts_config(raw)["enabled"], False)

    def test_toggle_persists_and_keeps_other_settings(self):
        with Sandbox() as sandbox:
            data = json.loads(json.dumps(DEFAULT_SETTINGS))
            data["user_name"] = "Sam"
            data["tts"] = {"enabled": False, "voice_id": "v1", "model": "m1"}
            data["overlay"] = {"port": 4242}
            sandbox.settings_path.write_text(json.dumps(data), "utf-8")

            backend, _, speaker = make_backend(speaker=FakeSpeaker())
            backend.set_voice(True)
            saved = json.loads(sandbox.settings_path.read_text("utf-8"))
            self.assertIs(saved["tts"]["enabled"], True)
            self.assertEqual(saved["tts"]["voice_id"], "v1")
            self.assertEqual(saved["tts"]["model"], "m1")
            self.assertEqual(saved["user_name"], "Sam")
            self.assertEqual(saved["overlay"], {"port": 4242})
            self.assertEqual(saved["sessions"], data["sessions"])
            self.assertTrue(speaker.enabled)

            # A new start sees the saved choice.
            self.assertIs(
                tts.load_tts_config(config.load_settings()["tts"])["enabled"],
                True,
            )

            backend.set_voice(False)
            saved = json.loads(sandbox.settings_path.read_text("utf-8"))
            self.assertIs(saved["tts"]["enabled"], False)
            self.assertFalse(speaker.enabled)

    def test_toggle_works_when_settings_have_no_or_broken_tts_section(self):
        for tts_value in (None, "loud", 5):
            with self.subTest(tts=tts_value), Sandbox() as sandbox:
                data = json.loads(json.dumps(DEFAULT_SETTINGS))

                if tts_value is not None:
                    data["tts"] = tts_value

                sandbox.settings_path.write_text(json.dumps(data), "utf-8")
                backend, _, _ = make_backend(speaker=FakeSpeaker())
                backend.set_voice(True)
                saved = json.loads(sandbox.settings_path.read_text("utf-8"))
                self.assertEqual(
                    saved["tts"],
                    {**config.DEFAULT_TTS_SETTINGS, "enabled": True},
                )

    def test_invalid_value_is_rejected(self):
        backend, _, speaker = make_backend(speaker=FakeSpeaker(enabled=False))

        for value in ("true", 1, None):
            with self.subTest(value=value):
                with self.assertRaises(overlay.BackendError):
                    backend.set_voice(value)

        self.assertFalse(speaker.enabled)

    def test_failed_save_leaves_speaker_unchanged(self):
        with Sandbox():
            backend, _, speaker = make_backend(
                speaker=FakeSpeaker(enabled=False)
            )

            with mock.patch.object(
                config, "save_settings", side_effect=OSError("disk")
            ):
                with self.assertRaises(overlay.BackendError):
                    backend.set_voice(True)

            self.assertFalse(speaker.enabled)

    def test_toggle_controls_real_tts_requests(self):
        with Sandbox(), mock.patch.dict(os.environ, {tts.API_KEY_ENV: KEY}):
            post = FakePost()
            player = FakePlayer()
            speaker = tts.FishAudioSpeaker(
                {"enabled": False}, post=post, player=player,
                notify=lambda m: None,
            )
            session = FakeSession()
            backend = overlay.OverlayBackend(lambda: session, speaker)
            speaker.listener = backend.on_speech_event

            backend.send_message("one")                  # voice off: text only
            speaker.wait()
            self.assertEqual(post.calls, [])
            self.assertEqual(backend.get_state()["state"], "idle")

            backend.set_voice(True)
            backend.send_message("two")
            speaker.wait()
            self.assertEqual(len(post.calls), 1)         # spoken exactly once
            self.assertEqual(len(player.played), 1)
            self.assertEqual(backend.get_state()["state"], "idle")

            backend.set_voice(False)
            backend.send_message("three")
            speaker.wait()
            self.assertEqual(len(post.calls), 1)


# -------------------------------------------------------------- config

class OverlayConfigTests(unittest.TestCase):
    def test_port_validation(self):
        default = config.DEFAULT_OVERLAY_SETTINGS["port"]
        self.assertEqual(overlay.overlay_port({"port": 5000}), 5000)
        self.assertEqual(overlay.overlay_port({"port": 0}), 0)

        for raw in (None, {}, "x", {"port": "80"}, {"port": True},
                    {"port": -1}, {"port": 70000}, {"port": 1.5}):
            with self.subTest(raw=raw):
                self.assertEqual(overlay.overlay_port(raw), default)

    def test_settings_passthrough_and_defaults(self):
        with Sandbox() as sandbox:
            self.assertEqual(
                config.load_settings()["overlay"],
                config.DEFAULT_OVERLAY_SETTINGS,
            )
            data = json.loads(json.dumps(DEFAULT_SETTINGS))
            data["overlay"] = "broken"
            sandbox.settings_path.write_text(json.dumps(data), "utf-8")
            self.assertEqual(config.load_settings()["overlay"], "broken")
            self.assertEqual(
                overlay.overlay_port(config.load_settings()["overlay"]),
                config.DEFAULT_OVERLAY_SETTINGS["port"],
            )

    def test_no_sage_companion_config_key(self):
        shipped = (PROJECT_ROOT / "settings.json").read_text("utf-8")
        self.assertNotIn("sage-companion", shipped.lower())
        self.assertNotIn("sage_companion", shipped.lower())
        self.assertNotIn("sage-companion", (PROJECT_ROOT / "core" / "config.py").read_text("utf-8").lower())


# -------------------------------------------------------------- server

class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = FakeSession()
        self.speaker = FakeSpeaker(enabled=False)
        self.backend = overlay.OverlayBackend(lambda: self.session, self.speaker)
        self.endpoint = Path(self.tmp.name) / "data" / "overlay_endpoint.json"
        self.server = overlay.OverlayServer(
            self.backend, port=0, endpoint_path=self.endpoint
        )
        self.port = self.server.start()
        self.addCleanup(self.server.close)

    def request(self, method, path, body=None, token=True, headers=None,
                host=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})

        if token is True:
            hdrs["X-WOS-Token"] = self.server.token
        elif token:
            hdrs["X-WOS-Token"] = token

        if host is not None:
            hdrs["Host"] = host

        payload = raw
        if body is not None:
            payload = json.dumps(body)
            hdrs["Content-Type"] = "application/json"

        conn.request(method, path, body=payload, headers=hdrs)
        response = conn.getresponse()
        data = response.read()
        conn.close()

        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = None

        return response.status, parsed, data, response


class ServerRoutingTests(ServerCase):
    def test_ping_needs_no_token(self):
        status, body, _, _ = self.request("GET", "/api/ping", token=False)
        self.assertEqual((status, body), (200, {"ok": True, "app": "WOS-Overlay"}))

    def test_static_ui_files_are_served(self):
        expect = {
            "/": b"chat-form", "/index.html": b"btn-voice",
            "/sage.js": b"window.Sage", "/chat.js": b"/api/state",
            "/style.css": b".chat-log",
        }

        for path, marker in expect.items():
            with self.subTest(path=path):
                status, _, data, response = self.request(
                    "GET", path, token=False
                )
                self.assertEqual(status, 200)
                self.assertIn(marker, data)
                self.assertEqual(response.getheader("Cache-Control"), "no-store")

    def test_other_files_and_traversal_are_not_served(self):
        for path in ("/core/config.py", "/../core/config.py",
                     "/%2e%2e/core/config.py", "/settings.json",
                     "/data/overlay_endpoint.json", "/main.py", "/nope"):
            with self.subTest(path=path):
                status, _, data, _ = self.request("GET", path, token=False)
                self.assertEqual(status, 404)
                self.assertNotIn(self.server.token.encode(), data)

    def test_api_requires_the_token(self):
        for token in (False, "wrong", ""):
            for method, path, body in (
                ("GET", "/api/state", None), ("GET", "/api/history", None),
                ("POST", "/api/message", {"text": "hi"}),
                ("POST", "/api/voice", {"enabled": True}),
            ):
                with self.subTest(token=token, path=path):
                    status, _, _, _ = self.request(
                        method, path, body, token=token
                    )
                    self.assertEqual(status, 401)

        self.assertEqual(self.session.calls, [])
        self.assertFalse(self.speaker.enabled)

    def test_foreign_host_and_origin_are_rejected(self):
        for host in ("evil.example", "127.0.0.1:1", "localhost"):
            with self.subTest(host=host):
                status, _, _, _ = self.request(
                    "GET", "/api/state", host=host
                )
                self.assertEqual(status, 403)
                status, _, _, _ = self.request("GET", "/", token=False, host=host)
                self.assertEqual(status, 403)

        status, _, _, _ = self.request(
            "POST", "/api/message", {"text": "hi"},
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.session.calls, [])

        status, _, _, _ = self.request(
            "GET", "/api/state", host=f"localhost:{self.port}"
        )
        self.assertEqual(status, 200)

    def test_unknown_api_paths_and_methods(self):
        self.assertEqual(self.request("GET", "/api/nope")[0], 404)
        self.assertEqual(self.request("POST", "/api/nope", {})[0], 404)
        self.assertEqual(self.request("POST", "/", {})[0], 404)
        self.assertEqual(self.request("PUT", "/api/state")[0], 501)


class ServerApiTests(ServerCase):
    def test_state_and_history(self):
        status, state, _, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["state"], "idle")
        self.assertEqual(state["character"], "Nexis")
        self.assertIs(state["voice"]["enabled"], False)

        status, history, _, _ = self.request("GET", "/api/history")
        self.assertEqual(history["messages"], [])

    def test_state_have_parameter(self):
        self.backend.on_speech_event(
            "start", 5, envelope={"fps": 50, "duration": 1, "levels": [1]}
        )
        _, with_levels, _, _ = self.request("GET", "/api/state?have=4")
        _, without, _, _ = self.request("GET", "/api/state?have=5")
        _, junk, _, _ = self.request("GET", "/api/state?have=abc")
        self.assertIn("levels", with_levels["audio"])
        self.assertNotIn("levels", without["audio"])
        self.assertIn("levels", junk["audio"])

    def test_message_flow(self):
        status, body, _, _ = self.request("POST", "/api/message", {"text": "hi"})
        self.assertEqual((status, body), (200, {"reply": "Hello from Nexis"}))
        self.assertEqual(self.session.calls, ["hi"])

        _, history, _, _ = self.request("GET", "/api/history")
        self.assertEqual(
            [m["content"] for m in history["messages"]],
            ["hi", "Hello from Nexis"],
        )

    def test_message_errors_map_to_status_codes(self):
        cases = [
            ({"text": ""}, 400), ({"text": "/model"}, 400), ({}, 400),
            ({"text": 5}, 400), ({"text": "x" * 4001}, 400),
        ]

        for body, expected in cases:
            with self.subTest(body=str(body)[:20]):
                self.assertEqual(
                    self.request("POST", "/api/message", body)[0], expected
                )

        self.session.reply = None
        self.assertEqual(
            self.request("POST", "/api/message", {"text": "hi"})[0], 502
        )
        self.session.error = RuntimeError("x")
        status, body, data, _ = self.request(
            "POST", "/api/message", {"text": "hi"}
        )
        self.assertEqual(status, 500)
        self.assertNotIn(b"Traceback", data)

    def test_busy_terminal_gives_409(self):
        self.backend.hold_command()

        try:
            with mock.patch.object(overlay, "BUSY_TIMEOUT_SECONDS", 0.1):
                status, body, _, _ = self.request(
                    "POST", "/api/message", {"text": "hi"}
                )
        finally:
            self.backend.release_command()

        self.assertEqual(status, 409)
        self.assertIn("busy", body["error"])

    def test_bad_request_bodies(self):
        self.assertEqual(
            self.request("POST", "/api/message", raw="not json",
                         headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(
            self.request("POST", "/api/message", raw="[1, 2]")[0], 400)
        self.assertEqual(
            self.request("POST", "/api/message", raw="\xff\xfe")[0], 400)
        big = "x" * (overlay.MAX_BODY_BYTES + 10)
        self.assertEqual(
            self.request("POST", "/api/message", raw=big)[0], 413)

    def test_voice_toggle_over_http_persists(self):
        with Sandbox() as sandbox:
            status, state, _, _ = self.request(
                "POST", "/api/voice", {"enabled": True}
            )
            self.assertEqual(status, 200)
            self.assertIs(state["voice"]["enabled"], True)
            self.assertTrue(self.speaker.enabled)
            saved = json.loads(sandbox.settings_path.read_text("utf-8"))
            self.assertIs(saved["tts"]["enabled"], True)

            status, state, _, _ = self.request(
                "POST", "/api/voice", {"enabled": False}
            )
            self.assertIs(state["voice"]["enabled"], False)

            for bad in ({"enabled": "yes"}, {}, {"enabled": None}):
                self.assertEqual(
                    self.request("POST", "/api/voice", bad)[0], 400)

    def test_responses_never_contain_the_token_or_api_key(self):
        with mock.patch.dict(os.environ, {tts.API_KEY_ENV: KEY}):
            blobs = [
                self.request("GET", "/api/state")[2],
                self.request("GET", "/api/history")[2],
                self.request("POST", "/api/message", {"text": "hi"})[2],
                self.request("GET", "/api/state", token=False)[2],
            ]

        for data in blobs:
            self.assertNotIn(KEY.encode(), data)
            self.assertNotIn(self.server.token.encode(), data)

        self.assertNotIn(KEY, self.endpoint.read_text("utf-8"))


class ServerLifecycleTests(unittest.TestCase):
    def make(self, tmp, port=0):
        backend = overlay.OverlayBackend(lambda: FakeSession(), FakeSpeaker())
        return overlay.OverlayServer(
            backend, port=port,
            endpoint_path=Path(tmp) / "data" / "overlay_endpoint.json",
        )

    def test_endpoint_file_is_written_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = self.make(tmp)
            port = server.start()
            data = json.loads(server.endpoint_path.read_text("utf-8"))
            self.assertEqual(data["port"], port)
            self.assertEqual(data["token"], server.token)
            self.assertEqual(data["app"], "WOS-Overlay")
            self.assertTrue(server.url.endswith("#" + server.token))
            self.assertEqual(
                [p.name for p in server.endpoint_path.parent.iterdir()],
                ["overlay_endpoint.json"],           # no temp files left
            )

            server.close()
            server.close()                            # idempotent
            self.assertFalse(server.endpoint_path.exists())

            with self.assertRaises(OSError):
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/ping", timeout=2
                )

    def test_close_does_not_remove_a_newer_processs_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = self.make(tmp)
            server.start()
            server.endpoint_path.write_text(
                json.dumps({"port": 1, "token": "newer"}), "utf-8"
            )
            server.close()
            self.assertTrue(server.endpoint_path.exists())

    def test_port_in_use_raises_oserror_and_leaves_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self.make(tmp)
            port = first.start()
            self.addCleanup(first.close)
            second = overlay.OverlayServer(
                overlay.OverlayBackend(lambda: FakeSession(), FakeSpeaker()),
                port=port,
                endpoint_path=Path(tmp) / "other" / "overlay_endpoint.json",
            )

            with self.assertRaises(OSError):
                second.start()

            self.assertFalse(second.endpoint_path.exists())

    def test_server_listens_on_loopback_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = self.make(tmp)
            server.start()
            self.addCleanup(server.close)
            self.assertEqual(server._httpd.server_address[0], "127.0.0.1")


# ---------------------------------------- start.py as a real subprocess

from tests.test_start_integration import WosProject, FAKE_GENAI  # noqa: E402


class StartOverlayProcessTests(unittest.TestCase):
    def make_project(self, port=0):
        wos = WosProject(FAKE_GENAI)
        self.addCleanup(wos.close)
        settings_path = wos.project / "settings.json"
        data = json.loads(settings_path.read_text("utf-8"))
        data["overlay"] = {"port": port}
        settings_path.write_text(json.dumps(data), "utf-8")
        return wos

    def launch(self, wos, *args):
        env = wos._env()
        env.pop("FISH_AUDIO_API_KEY", None)
        return subprocess.Popen(
            [sys.executable, "start.py", *args],
            cwd=wos.project, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env,
        )

    def endpoint(self, wos, proc):
        path = wos.project / "data" / "overlay_endpoint.json"
        deadline = time.time() + 20

        while time.time() < deadline:
            if proc.poll() is not None:
                self.fail("start.py exited early: " + proc.stderr.read())

            try:
                return json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                time.sleep(0.1)

        self.fail("the overlay endpoint file never appeared")

    def call(self, endpoint, method, path, body=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{endpoint['port']}{path}",
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"X-WOS-Token": endpoint["token"],
                     "Content-Type": "application/json"},
        )

        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)

    def test_overlay_and_terminal_share_one_session(self):
        wos = self.make_project()
        proc = self.launch(wos, "--overlay")

        try:
            endpoint = self.endpoint(wos, proc)
            ping = urllib.request.urlopen(
                f"http://127.0.0.1:{endpoint['port']}/api/ping", timeout=5
            )
            self.assertEqual(json.load(ping)["app"], "WOS-Overlay")

            state = self.call(endpoint, "GET", "/api/state")
            self.assertEqual(state["state"], "idle")
            self.assertEqual(state["character"], "Nexis")
            self.assertIs(state["voice"]["enabled"], False)   # default off

            reply = self.call(
                endpoint, "POST", "/api/message",
                {"text": "remember that I like tea"},
            )["reply"]
            self.assertIn("remember", reply.lower())

            voice = self.call(
                endpoint, "POST", "/api/voice", {"enabled": True}
            )
            self.assertIs(voice["voice"]["enabled"], True)

            out, err = proc.communicate("/memory\n/chats\n/exit\n", timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()

        self.assertEqual(proc.returncode, 0, err)
        self.assertNotIn("Traceback", out + err)
        # The UI's message was shown in the terminal...
        self.assertIn("You (overlay): remember that I like tea", out)
        # ...and the terminal sees what the UI saved: one shared session.
        self.assertIn("tea", out.split("You (overlay)")[1])
        memory_listing = out.split("You: /memory")[-1] if "You: /memory" in out else out
        self.assertIn("tea", memory_listing)

        saved = json.loads((wos.project / "settings.json").read_text("utf-8"))
        self.assertIs(saved["tts"]["enabled"], True)           # persisted
        self.assertFalse(
            (wos.project / "data" / "overlay_endpoint.json").exists()
        )

    def test_without_the_flag_nothing_is_started(self):
        wos = self.make_project()
        proc = self.launch(wos)
        out, err = proc.communicate("/exit\n", timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        self.assertFalse(
            (wos.project / "data" / "overlay_endpoint.json").exists()
        )

    def test_port_in_use_is_only_a_warning(self):
        import socket

        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        self.addCleanup(blocker.close)
        wos = self.make_project(port=blocker.getsockname()[1])
        proc = self.launch(wos, "--overlay")
        out, err = proc.communicate("/exit\n", timeout=30)

        self.assertEqual(proc.returncode, 0, err)
        self.assertNotIn("Traceback", out + err)
        self.assertIn("The desktop overlay could not start", out)
        self.assertIn("Conversation saved", out)


# -------------------------------------------------- packaging / launcher

class ProjectFilesTests(unittest.TestCase):
    def test_run_bat_launches_overlay_and_terminal(self):
        data = (PROJECT_ROOT / "run.bat").read_bytes()
        self.assertIn(b"\r\n", data)
        text = data.decode().lower()
        self.assertIn("overlay\\main.py", text)
        self.assertIn("python start.py --overlay", text)
        self.assertNotIn("sage-companion", text)

    def test_requirements_add_only_what_is_needed(self):
        lines = [
            l.strip().lower() for l in
            (PROJECT_ROOT / "requirements.txt").read_text().splitlines()
            if l.strip() and not l.startswith("#")
        ]
        names = {l.split(">")[0].split("=")[0] for l in lines}
        self.assertEqual(
            names,
            {"requests", "python-dotenv", "google-genai", "httpx", "pywebview"},
        )

    def test_ui_files_exist_and_have_no_credentials(self):
        web = PROJECT_ROOT / "overlay" / "web"

        for name in ("index.html", "style.css", "sage.js", "chat.js"):
            text = (web / name).read_text("utf-8")
            self.assertNotIn("FISH_AUDIO_API_KEY=", text)
            self.assertNotIn("Bearer ", text)

        self.assertNotIn("localStorage", (web / "chat.js").read_text("utf-8"))

    def test_voice_defaults_off_in_shipped_settings(self):
        shipped = json.loads(
            (PROJECT_ROOT / "settings.json").read_text("utf-8-sig")
        )
        self.assertIs(shipped["tts"]["enabled"], False)


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class ChatUiLogicTests(unittest.TestCase):
    """Runs the real chat.js against a stub DOM and fake backend."""

    def test_chat_js_harness(self):
        result = subprocess.run(
            ["node", str(PROJECT_ROOT / "tests" / "js" / "chat_harness.js")],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("all checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
