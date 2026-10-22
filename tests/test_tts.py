"""Tests for the optional Fish Audio text-to-speech.

No real request is ever made: the HTTP call and the audio player are replaced
by fakes, and httpx itself is replaced by a stub where the real call path is
checked. No API key, network or sound device is needed.
"""
import json
import os
import sys
import threading
import types as pytypes
import unittest
from unittest import mock

from tests.helpers import Sandbox, DEFAULT_SETTINGS, PROJECT_ROOT  # noqa: F401
from core import config, tts

KEY = "test-secret-key-123"


def make_wav(pcm=b"\x01\x02" * 50, riff_size=None, data_size=None,
             channels=1, rate=44100, fmt_size=16):
    """Synthetic 44-byte-header WAV. Sizes default to the correct values."""
    header = bytearray(b"RIFF" + bytes(4) + b"WAVEfmt ")
    header += fmt_size.to_bytes(4, "little")
    header += (1).to_bytes(2, "little") + channels.to_bytes(2, "little")
    header += rate.to_bytes(4, "little")
    header += (rate * channels * 2).to_bytes(4, "little")
    header += (channels * 2).to_bytes(2, "little") + (16).to_bytes(2, "little")
    header += b"data" + bytes(4)
    total = len(header) + len(pcm)
    header[4:8] = (total - 8 if riff_size is None else riff_size).to_bytes(
        4, "little")
    header[40:44] = (len(pcm) if data_size is None else data_size).to_bytes(
        4, "little")
    return bytes(header) + pcm


# What Fish Audio actually sends: streaming-style placeholder sizes.
PLACEHOLDER = 0xFFFFFFFF
WAV = make_wav(riff_size=PLACEHOLDER, data_size=PLACEHOLDER)


class FakeResponse:
    def __init__(self, status=200, content=WAV):
        self.status_code = status
        self.content = content


class FakePost:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))

        if self.error is not None:
            raise self.error

        return self.response


class FakePlayer:
    def __init__(self, error=None):
        self.error = error
        self.played = []
        self.stops = 0
        self.files_existed = []
        self.contents = []

    def play(self, path, *, cancelled=None, started=None):
        if cancelled is not None and cancelled.is_set():
            return
        if started:
            started()
        self.files_existed.append(os.path.exists(path))
        self.played.append(path)

        with open(path, "rb") as file:
            self.contents.append(file.read())

        if self.error is not None:
            raise self.error

    def stop(self):
        self.stops += 1


def make_speaker(settings=None, post=None, player=None, notices=None):
    settings = {"enabled": True} if settings is None else settings
    return tts.FishAudioSpeaker(
        settings,
        post=post or FakePost(),
        player=player or FakePlayer(),
        notify=(notices.append if notices is not None else (lambda m: None)),
    )


class WithKey(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {tts.API_KEY_ENV: KEY})
        patcher.start()
        self.addCleanup(patcher.stop)


class ConfigTests(unittest.TestCase):
    def test_defaults_when_missing_or_malformed(self):
        for raw in (None, {}, "yes", 5, [], {"enabled": "true"}):
            with self.subTest(raw=raw):
                result = tts.load_tts_config(raw)
                self.assertIs(result["enabled"], False)
                self.assertEqual(
                    result["voice_id"], "5a62b02a53954e13ad0d9a90464ef9b0"
                )
                self.assertEqual(result["model"], "s2.1-pro-free")

    def test_custom_values_and_blank_fallback(self):
        result = tts.load_tts_config(
            {"enabled": True, "voice_id": " abc ", "model": "other"}
        )
        self.assertEqual(
            result, {"enabled": True, "voice_id": "abc", "model": "other"}
        )

        blank = tts.load_tts_config(
            {"enabled": True, "voice_id": "  ", "model": 7}
        )
        self.assertEqual(blank["voice_id"], config.DEFAULT_TTS_SETTINGS["voice_id"])
        self.assertEqual(blank["model"], config.DEFAULT_TTS_SETTINGS["model"])

    def test_api_key_comes_from_environment_only(self):
        with mock.patch.dict(os.environ, {tts.API_KEY_ENV: " k "}):
            self.assertEqual(tts.get_api_key(), "k")

        for value in ("", "   ", "your_fish_audio_api_key_here"):
            with mock.patch.dict(os.environ, {tts.API_KEY_ENV: value}):
                self.assertIsNone(tts.get_api_key())

        with mock.patch.dict(os.environ, clear=True):
            self.assertIsNone(tts.get_api_key())

    def test_no_key_is_hardcoded_in_source(self):
        source = (PROJECT_ROOT / "core" / "tts.py").read_text("utf-8")
        self.assertNotIn(KEY, source)
        self.assertNotIn("Bearer " + "fa", source)


class SettingsFileTests(unittest.TestCase):
    def test_missing_tts_section_defaults_to_disabled(self):
        with Sandbox() as sandbox:
            result = config.load_settings()
            self.assertEqual(result["tts"], config.DEFAULT_TTS_SETTINGS)
            self.assertIs(result["tts"]["enabled"], False)

    def test_saved_tts_section_is_kept(self):
        with Sandbox() as sandbox:
            data = json.loads(json.dumps(DEFAULT_SETTINGS))
            data["tts"] = {"enabled": True, "model": "x"}
            sandbox.settings_path.write_text(json.dumps(data), "utf-8")
            self.assertEqual(
                config.load_settings()["tts"], {"enabled": True, "model": "x"}
            )

    def test_malformed_tts_section_does_not_stop_startup(self):
        for bad in ("loud", 3, None, [1]):
            with self.subTest(bad=bad), Sandbox() as sandbox:
                data = json.loads(json.dumps(DEFAULT_SETTINGS))
                data["tts"] = bad
                sandbox.settings_path.write_text(json.dumps(data), "utf-8")
                settings = config.load_settings()
                self.assertIs(
                    tts.load_tts_config(settings["tts"])["enabled"], False
                )

    def test_regenerated_settings_file_contains_tts(self):
        with Sandbox() as sandbox:
            sandbox.settings_path.unlink()
            config.load_settings()
            saved = json.loads(sandbox.settings_path.read_text("utf-8"))
            self.assertEqual(saved["tts"], config.DEFAULT_TTS_SETTINGS)

    def test_shipped_settings_file_has_tts_disabled(self):
        shipped = json.loads(
            (PROJECT_ROOT / "settings.json").read_text("utf-8-sig")
        )
        self.assertIs(shipped["tts"]["enabled"], False)


class DisabledTests(WithKey):
    def test_disabled_does_nothing(self):
        post, player = FakePost(), FakePlayer()
        speaker = tts.FishAudioSpeaker(
            {"enabled": False}, post=post, player=player
        )
        self.assertFalse(speaker.speak("Hello there"))
        speaker.wait()
        self.assertEqual(post.calls, [])
        self.assertEqual(player.played, [])

    def test_default_settings_are_disabled(self):
        post = FakePost()
        speaker = tts.FishAudioSpeaker(None, post=post, player=FakePlayer())
        self.assertFalse(speaker.speak("Hello there"))
        self.assertEqual(post.calls, [])

    def test_nothing_unspeakable_is_sent(self):
        post = FakePost()
        speaker = make_speaker(post=post)

        for text in (None, "", "   \n", 42, "WOS: something failed",
                     "I couldn't save this exchange: disk full"):
            with self.subTest(text=text):
                self.assertFalse(speaker.speak(text))

        speaker.wait()
        self.assertEqual(post.calls, [])


class SuccessTests(WithKey):
    def test_request_shape_and_playback(self):
        post, player = FakePost(), FakePlayer()
        speaker = make_speaker(post=post, player=player)

        self.assertTrue(speaker.speak("  Hello there  "))
        speaker.wait()

        (url, kwargs), = post.calls
        self.assertEqual(url, "https://api.fish.audio/v1/tts")
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertEqual(kwargs["headers"]["model"], "s2.1-pro-free")
        self.assertEqual(
            kwargs["json"],
            {
                "text": "Hello there",
                "reference_id": "5a62b02a53954e13ad0d9a90464ef9b0",
                "format": "wav",
            },
        )
        self.assertEqual(len(player.played), 1)

    def test_custom_voice_and_model_are_used(self):
        post = FakePost()
        speaker = make_speaker(
            {"enabled": True, "voice_id": "voice-x", "model": "model-y"},
            post=post,
        )
        speaker.speak("Hi")
        speaker.wait()
        _, kwargs = post.calls[0]
        self.assertEqual(kwargs["json"]["reference_id"], "voice-x")
        self.assertEqual(kwargs["headers"]["model"], "model-y")

    def test_temp_file_exists_during_playback_and_is_removed_after(self):
        player = FakePlayer()
        speaker = make_speaker(player=player)
        speaker.speak("Hello")
        speaker.wait()

        self.assertEqual(player.files_existed, [True])
        self.assertFalse(os.path.exists(player.played[0]))
        self.assertEqual(speaker._temp_files, set())

    def test_real_httpx_call_path_is_mocked(self):
        calls = []
        fake_httpx = pytypes.ModuleType("httpx")

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse()

        fake_httpx.post = fake_post
        player = FakePlayer()

        with mock.patch.dict(sys.modules, {"httpx": fake_httpx}):
            speaker = tts.FishAudioSpeaker(
                {"enabled": True}, player=player, notify=lambda m: None
            )
            speaker.speak("Hello")
            speaker.wait()

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(player.played), 1)


def sizes(wav):
    return (
        int.from_bytes(wav[4:8], "little"),
        int.from_bytes(wav[40:44], "little"),
    )


class NormalizeWavTests(unittest.TestCase):
    def test_placeholder_sizes_are_corrected(self):
        pcm = bytes(range(200))
        result = tts.normalize_wav(
            make_wav(pcm, riff_size=PLACEHOLDER, data_size=PLACEHOLDER)
        )
        self.assertEqual(sizes(result), (len(result) - 8, len(result) - 44))
        self.assertEqual(sizes(result), (len(pcm) + 36, len(pcm)))

    def test_other_placeholder_values_are_corrected(self):
        for riff, data in ((0, 0), (0x7FFFFFFF, 0), (36, PLACEHOLDER)):
            with self.subTest(riff=riff, data=data):
                wav = make_wav(riff_size=riff, data_size=data)
                result = tts.normalize_wav(wav)
                self.assertEqual(
                    sizes(result), (len(wav) - 8, len(wav) - 44)
                )

    def test_correct_sizes_are_left_unchanged(self):
        wav = make_wav()
        self.assertEqual(tts.normalize_wav(wav), wav)

    def test_normalizing_twice_changes_nothing(self):
        once = tts.normalize_wav(make_wav(riff_size=PLACEHOLDER))
        self.assertEqual(tts.normalize_wav(once), once)

    def test_only_the_two_size_fields_change(self):
        pcm = bytes(range(256)) * 3
        wav = make_wav(pcm, riff_size=PLACEHOLDER, data_size=PLACEHOLDER)
        result = tts.normalize_wav(wav)

        self.assertEqual(len(result), len(wav))
        self.assertEqual(result[44:], pcm)  # audio bytes untouched
        self.assertEqual(result[8:40], wav[8:40])
        self.assertEqual(result[0:4], wav[0:4])
        changed = [i for i in range(len(wav)) if wav[i] != result[i]]
        self.assertTrue(all(4 <= i < 8 or 40 <= i < 44 for i in changed))

    def test_accepts_bytearray_and_returns_bytes(self):
        result = tts.normalize_wav(bytearray(WAV))
        self.assertIsInstance(result, bytes)

    def test_input_is_not_modified_in_place(self):
        original = bytearray(WAV)
        tts.normalize_wav(original)
        self.assertEqual(bytes(original), WAV)

    def test_malformed_input_is_rejected(self):
        good = make_wav()
        cases = {
            "not bytes": "RIFF",
            "none": None,
            "empty": b"",
            "garbage": b"not wav at all" * 10,
            "header only": good[:44],
            "truncated header": good[:30],
            "wrong riff": b"RIFX" + good[4:],
            "wrong wave": good[:8] + b"AVI " + good[12:],
            "no fmt": good[:12] + b"junk" + good[16:],
            "no data at 36": good[:36] + b"LIST" + good[40:],
            "extended fmt": make_wav(fmt_size=18),
            "zero channels": make_wav(channels=0),
            "zero rate": make_wav(rate=0),
        }

        for name, value in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(tts.TTSError):
                    tts.normalize_wav(value)

    def test_rejection_messages_do_not_contain_audio_or_key(self):
        with self.assertRaises(tts.TTSError) as caught:
            tts.normalize_wav(b"RIFF" + KEY.encode() * 20)

        self.assertNotIn(KEY, str(caught.exception))


class PlaybackUsesNormalizedWavTests(WithKey):
    def test_player_receives_corrected_file(self):
        pcm = bytes(range(100))
        wav = make_wav(pcm, riff_size=PLACEHOLDER, data_size=PLACEHOLDER)
        player = FakePlayer()
        speaker = make_speaker(
            post=FakePost(FakeResponse(content=wav)), player=player
        )
        speaker.speak("Hello")
        speaker.wait()

        (played,) = player.contents
        self.assertEqual(sizes(played), (len(played) - 8, len(played) - 44))
        self.assertEqual(played[44:], pcm)
        self.assertFalse(os.path.exists(player.played[0]))

    def test_malformed_wav_is_isolated_and_never_played(self):
        good = make_wav()
        bad_responses = (
            b"RIFF" + bytes(60),          # right magic, wrong layout
            good[:44],                    # no audio
            good[:20],                    # truncated
            b"{\"message\": \"error\"}" * 5,
        )

        for content in bad_responses:
            with self.subTest(content=content[:12]):
                notices, player = [], FakePlayer()
                speaker = make_speaker(
                    post=FakePost(FakeResponse(content=content)),
                    player=player,
                    notices=notices,
                )
                self.assertTrue(speaker.speak("Hello"))
                speaker.wait()

                self.assertEqual(player.played, [])
                self.assertEqual(len(notices), 1)
                self.assertEqual(speaker._temp_files, set())

    def test_unexpected_error_in_normalizer_is_isolated(self):
        notices, player = [], FakePlayer()
        speaker = make_speaker(player=player, notices=notices)

        with mock.patch.object(
            tts, "normalize_wav", side_effect=RuntimeError("boom")
        ):
            self.assertTrue(speaker.speak("Hello"))
            speaker.wait()

        self.assertEqual(player.played, [])
        self.assertEqual(len(notices), 1)
        self.assertIn("RuntimeError", notices[0])
        self.assertNotIn(KEY, notices[0])


class FailureTests(WithKey):
    def run_failure(self, post=None, player=None):
        notices = []
        player = player or FakePlayer()
        speaker = make_speaker(post=post, player=player, notices=notices)
        result = speaker.speak("Hello")
        speaker.wait()
        return speaker, player, notices, result

    def test_missing_key_sends_nothing_and_notifies_once(self):
        post, notices = FakePost(), []
        speaker = make_speaker(post=post, notices=notices)

        with mock.patch.dict(os.environ, clear=True):
            self.assertFalse(speaker.speak("One"))
            self.assertFalse(speaker.speak("Two"))

        self.assertEqual(post.calls, [])
        self.assertEqual(len(notices), 1)
        self.assertIn(tts.API_KEY_ENV, notices[0])

    def test_network_failures_are_contained(self):
        for error in (ConnectionError("down"), TimeoutError("slow"),
                      OSError("boom"), RuntimeError("weird")):
            with self.subTest(error=type(error).__name__):
                post = FakePost(error=error)
                _, player, notices, result = self.run_failure(post=post)
                self.assertTrue(result)
                self.assertEqual(player.played, [])
                self.assertEqual(len(notices), 1)
                self.assertIn(type(error).__name__, notices[0])

    def test_api_error_statuses_are_contained(self):
        for status in (401, 402, 429, 500):
            with self.subTest(status=status):
                post = FakePost(FakeResponse(status=status, content=b"x"))
                _, player, notices, _ = self.run_failure(post=post)
                self.assertEqual(player.played, [])
                self.assertEqual(len(notices), 1)
                self.assertIn(str(status), notices[0])

    def test_bad_audio_is_not_played(self):
        for content in (b"", None, "text", b"not wav at all"):
            with self.subTest(content=content):
                post = FakePost(FakeResponse(content=content))
                _, player, notices, _ = self.run_failure(post=post)
                self.assertEqual(player.played, [])
                self.assertEqual(len(notices), 1)

    def test_missing_httpx_is_contained(self):
        notices, player = [], FakePlayer()
        speaker = tts.FishAudioSpeaker(
            {"enabled": True}, player=player, notify=notices.append
        )

        with mock.patch.dict(sys.modules, {"httpx": None}):
            speaker.speak("Hello")
            speaker.wait()

        self.assertEqual(player.played, [])
        self.assertIn("httpx", notices[0])

    def test_key_never_appears_in_any_notice(self):
        errors = [
            ConnectionError(f"Bearer {KEY} refused"),
            RuntimeError(KEY),
        ]
        for error in errors:
            _, _, notices, _ = self.run_failure(FakePost(error=error))
            self.assertTrue(notices)
            self.assertFalse(any(KEY in n for n in notices))

        for status in (401, 500):
            post = FakePost(FakeResponse(status=status, content=KEY.encode()))
            _, _, notices, _ = self.run_failure(post=post)
            self.assertFalse(any(KEY in n for n in notices))

    def test_repeated_identical_failure_is_reported_once(self):
        notices = []
        speaker = make_speaker(
            post=FakePost(error=ConnectionError("x")), notices=notices
        )

        for _ in range(3):
            speaker.speak("Hello")
            speaker.wait()

        self.assertEqual(len(notices), 1)


class ErrorIsolationTests(WithKey):
    def test_player_failure_never_raises_and_cleans_up(self):
        for error in (RuntimeError("no device"), OSError("busy"),
                      tts.TTSError("audio playback is only supported")):
            with self.subTest(error=type(error).__name__):
                notices, player = [], FakePlayer(error=error)
                speaker = make_speaker(player=player, notices=notices)
                self.assertTrue(speaker.speak("Hello"))
                speaker.wait()

                self.assertEqual(len(notices), 1)
                self.assertFalse(os.path.exists(player.played[0]))
                self.assertEqual(speaker._temp_files, set())

    def test_failing_notify_is_contained(self):
        def broken(message):
            raise RuntimeError("terminal gone")

        speaker = tts.FishAudioSpeaker(
            {"enabled": True},
            post=FakePost(error=ConnectionError("x")),
            player=FakePlayer(),
            notify=broken,
        )
        self.assertTrue(speaker.speak("Hello"))
        speaker.wait()

    def test_failing_stop_and_thread_start_are_contained(self):
        class BadPlayer(FakePlayer):
            def stop(self):
                raise RuntimeError("stop failed")

        speaker = make_speaker(player=BadPlayer())
        self.assertTrue(speaker.speak("Hello"))
        speaker.wait()

        with mock.patch.object(
            threading.Thread, "start", side_effect=RuntimeError("no threads")
        ):
            self.assertFalse(speaker.speak("Hello"))

        self.assertEqual(speaker._workers, set())

    def test_speak_returns_before_slow_request_finishes(self):
        release = threading.Event()

        def slow_post(url, **kwargs):
            release.wait(5)
            return FakeResponse()

        player = FakePlayer()
        speaker = make_speaker(post=slow_post, player=player)

        self.assertTrue(speaker.speak("Hello"))
        self.assertEqual(player.played, [])  # still waiting on the network
        release.set()
        speaker.wait()
        self.assertEqual(len(player.played), 1)

    def test_shutdown_removes_files_and_never_raises(self):
        speaker = make_speaker()
        path = speaker._write_temp(WAV)
        self.assertTrue(os.path.exists(path))
        speaker.shutdown()
        self.assertFalse(os.path.exists(path))
        speaker.shutdown()


class OverlapTests(WithKey):
    def test_newer_reply_replaces_older_one(self):
        release_first = threading.Event()
        started_first = threading.Event()
        order = []

        def post(url, **kwargs):
            text = kwargs["json"]["text"]
            order.append(text)

            if text == "first":
                started_first.set()
                release_first.wait(5)

            return FakeResponse()

        player = FakePlayer()
        speaker = make_speaker(post=post, player=player)

        speaker.speak("first")
        self.assertTrue(started_first.wait(5))
        stops_before = player.stops
        speaker.speak("second")
        self.assertGreater(player.stops, stops_before)  # old audio cut off
        release_first.set()
        speaker.wait()

        # Only the newest reply is played; the stale one is dropped.
        self.assertEqual(len(player.played), 1)
        self.assertEqual(order, ["first", "second"])
        self.assertEqual(speaker._temp_files, set())


class StartIntegrationTests(unittest.TestCase):
    """start.py speaks only successful character replies."""

    def test_start_py_speaks_replies_only(self):
        source = (PROJECT_ROOT / "start.py").read_text("utf-8")
        self.assertEqual(source.count("overlay_backend.speak_terminal_reply("), 2)
        # Both calls sit directly after the reply is printed.
        self.assertEqual(
            source.count(
                'print(f"{session.character_name}: {reply}")\n'
            ) + source.count(
                'print(f"{session.character_name}: {reply}")\r\n'
            ),
            2,
        )
        self.assertNotIn("overlay_backend.speak_terminal_reply(message", source)


if __name__ == "__main__":
    unittest.main()


# ---- start.py run as a real subprocess, with fake httpx and winsound ----

from tests.test_start_integration import WosProject, write_stub  # noqa: E402

FAKE_HTTPX = {
    "httpx/__init__.py": (
        "import json\n\n"
        f"WAV_HEX = {make_wav().hex()!r}\n\n\n"
        "class R:\n"
        "    status_code = 200\n"
        "    content = bytes.fromhex(WAV_HEX)\n\n\n"
        "def post(url, **kwargs):\n"
        "    with open('_tts_calls.json', 'a') as f:\n"
        "        f.write(json.dumps({'url': url,\n"
        "                            'text': kwargs['json']['text']}) + '\\n')\n"
        "    return R()\n"
    ),
    "winsound/__init__.py": (
        "SND_FILENAME = 2\nSND_ASYNC = 1\nSND_NODEFAULT = 4\n\n"
        "def PlaySound(sound, flags):\n"
        "    if sound is not None:\n"
        "        with open('_tts_played.txt', 'a') as f:\n"
        "            f.write('played\\n')\n"
    ),
}


class StartSubprocessTests(unittest.TestCase):
    def make_project(self, enabled):
        from tests.test_start_integration import FAKE_GENAI

        wos = WosProject(FAKE_GENAI)
        self.addCleanup(wos.close)
        write_stub(wos.stubs, FAKE_HTTPX)
        settings_path = wos.project / "settings.json"
        data = json.loads(settings_path.read_text("utf-8"))
        data["tts"] = {"enabled": enabled}
        settings_path.write_text(json.dumps(data), "utf-8")
        return wos

    def run_wos(self, wos, answers, key=True):
        env_patch = {tts.API_KEY_ENV: KEY} if key else {}
        with mock.patch.dict(os.environ, env_patch):
            if not key:
                os.environ.pop(tts.API_KEY_ENV, None)
            return wos.run_scripted(answers)

    def test_reply_is_printed_then_spoken_but_input_and_commands_are_not(self):
        wos = self.make_project(enabled=True)
        result = self.run_wos(
            wos, ["/forget that I like tea", "/status", "/help", "/exit"]
        )

        self.assertNotIn("Traceback", result.stderr, result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(KEY, result.stdout + result.stderr)

        calls = [
            json.loads(line) for line in
            (wos.project / "_tts_calls.json").read_text().splitlines()
        ]
        self.assertEqual(len(calls), 1)  # only the character reply
        self.assertIn(calls[0]["text"], result.stdout)  # it was shown too
        self.assertNotIn("/status", calls[0]["text"])

    def test_disabled_makes_no_request_and_text_chat_is_unchanged(self):
        wos = self.make_project(enabled=False)
        result = self.run_wos(wos, ["/forget that I like tea", "/exit"])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((wos.project / "_tts_calls.json").exists())
        self.assertIn("Nexis:", result.stdout)

    def test_enabled_without_key_still_prints_reply(self):
        wos = self.make_project(enabled=True)
        result = self.run_wos(
            wos, ["/forget that I like tea", "/exit"], key=False
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((wos.project / "_tts_calls.json").exists())
        self.assertIn("Nexis:", result.stdout)
        self.assertIn(tts.API_KEY_ENV, result.stdout)
