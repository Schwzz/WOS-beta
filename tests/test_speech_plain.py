"""Exercise the real plain command loop during native dictation."""
import contextlib
import io
import os
import runpy
import sys
import threading
import unittest
from unittest import mock

from core.overlay import OverlayBackend
from core.stt import SpeechController
from core.tts import FishAudioSpeaker
from tests.helpers import PROJECT_ROOT, Sandbox
from tests.test_stt import wav_bytes


class PlainSpeechTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = self.enterContext(Sandbox())
        self.enterContext(mock.patch.dict(os.environ, {'GROQ_API_KEY': 'synthetic-key'}, clear=True))
        with mock.patch.object(sys, 'argv', ['start.py', '--plain']), \
             mock.patch('builtins.input', side_effect=EOFError), \
             contextlib.redirect_stdout(io.StringIO()):
            loaded = runpy.run_path(str(PROJECT_ROOT / 'start.py'), run_name='plain_speech_test')
        self.ns = loaded['run_loop'].__globals__
        self.session = self.ns['session'] = self.sandbox.make_session()
        self.speaker = FishAudioSpeaker({'enabled': False}, notify=lambda *_: None)
        self.addCleanup(self.speaker.shutdown)
        self.backend = OverlayBackend(lambda: self.session, self.speaker)
        self.addCleanup(self.backend.close)
        self.ns.update(tts_speaker=self.speaker, overlay_backend=self.backend)
        self.transcribing = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)

        class Recorder:
            def record(recorder, stop, cancelled, progress):
                stop.wait(5)
                return wav_bytes()

            def cancel(recorder):
                pass

        def transcribe(*_):
            self.transcribing.set()
            self.release.wait(5)
            return 'Spoken draft'

        self.backend.speech = SpeechController(
            lambda: self.session.session_id,
            recorder_factory=Recorder, transcribe_fn=transcribe,
        )

    def run_commands(self, commands):
        output = io.StringIO()
        try:
            with mock.patch('builtins.input', side_effect=commands), contextlib.redirect_stdout(output):
                self.ns['run_loop']()
        finally:
            self.backend.release_command()
        return output.getvalue()

    def assert_capture_blocks_chat(self):
        original = self.session.session_id
        with mock.patch('core.session.chat') as generate:
            output = self.run_commands(['Do not send', '/new', '/character switch tester', '/exit'])
            generate.assert_not_called()
        self.assertEqual(self.session.session_id, original)
        self.assertEqual(self.session.character_id, 'nexis')
        self.assertEqual(self.session.chat['messages'], [])
        self.assertIn('Finish or cancel speech input', output)

    def test_plain_recording_rejects_chat_without_crashing_or_changing_session(self):
        self.backend.speech.start(self.session.session_id)
        self.assert_capture_blocks_chat()

    def test_plain_transcribing_rejects_chat_without_crashing_or_changing_session(self):
        self.backend.speech.start(self.session.session_id)
        self.backend.speech.stop()
        self.assertTrue(self.transcribing.wait(3))
        self.assert_capture_blocks_chat()

    def test_plain_ctrl_c_cancels_microphone_and_keeps_prompt_available(self):
        self.backend.speech.start(self.session.session_id)
        output = self.run_commands([KeyboardInterrupt(), '/status', '/exit'])
        self.assertEqual(self.backend.speech.state()['phase'], 'idle')
        self.assertIn('Speech stopped', output)
        self.assertIn('Conversation saved', output)
