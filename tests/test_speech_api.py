"""Authenticated speech controls; no real microphone or provider requests."""
import http.client
import json
import os
import unittest
from unittest import mock
from tests.helpers import Sandbox
from tests.test_overlay import FakeSpeaker
from core import config, overlay


class SpeechApiTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = self.enterContext(Sandbox())
        self.enterContext(mock.patch.dict(os.environ, {}, clear=True))
        self.enterContext(mock.patch.object(config, 'PROJECT_ROOT', self.sandbox.root))
        self.session = self.sandbox.make_session()
        self.backend = overlay.OverlayBackend(lambda: self.session, FakeSpeaker())
        self.state = {'phase': 'idle', 'shortcut': 'f7', 'model': 'whisper-large-v3-turbo', 'result': None}
        self.speech = mock.Mock()
        self.speech.state.return_value = self.state
        self.speech.start.return_value = self.state
        self.speech.cancel.return_value = self.state
        self.backend.speech = self.speech
        self.server = overlay.OverlayServer(self.backend, port=0, endpoint_path=self.sandbox.root/'endpoint.json')
        self.server.start()
        self.addCleanup(self.server.close)

    def request(self, path, data=None, token=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=3)
        try:
            headers = {'X-WOS-Token': self.server.token} if token else {}
            if data is not None: headers['Content-Type'] = 'application/json'
            connection.request('POST' if data is not None else 'GET', path, json.dumps(data) if data is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_state_and_controls_require_token_and_bind_to_current_chat(self):
        self.assertEqual(self.request('/api/stt', token=False)[0], 401)
        self.assertEqual(self.request('/api/stt')[1]['phase'], 'idle')
        self.assertEqual(self.request('/api/state')[1]['speech'], self.state)
        self.assertEqual(self.request('/api/stt', {'action': 'start', 'session_id': 'stale'})[0], 409)
        self.speech.start.assert_not_called()
        self.assertEqual(self.request('/api/stt', {'action': 'start', 'session_id': self.session.session_id})[0], 200)
        self.speech.start.assert_called_once_with(self.session.session_id)

    def test_stop_cancels_dictation_and_unknown_action_is_rejected(self):
        self.assertEqual(self.request('/api/stop', {})[0], 200)
        self.speech.cancel.assert_called_once()
        self.assertEqual(self.request('/api/stt', {'action': 'arbitrary'})[0], 400)

    def test_busy_session_rejects_recording_and_microphone_blocks_chat_actions(self):
        self.backend.hold_command()
        try:
            self.assertEqual(self.request('/api/stt', {'action': 'start', 'session_id': self.session.session_id})[0], 409)
        finally:
            self.backend.release_command()
        self.speech.start.assert_not_called()
        self.state['phase'] = 'recording'
        status, _ = self.request('/api/chats', {'action': 'new', 'expected_session_id': self.session.session_id})
        self.assertEqual(status, 409)
        with mock.patch('core.session.chat') as generate:
            self.assertEqual(self.request('/api/message', {'text': 'Do not send'})[0], 409)
            generate.assert_not_called()

    def test_groq_key_is_saved_locally_but_not_returned(self):
        key = 'fake-speech-key-for-unit-test'
        status, result = self.request('/api/credentials', {'provider': 'groq', 'key': key})
        self.assertEqual(status, 200)
        self.assertTrue(result['speech']['configured'])
        self.assertNotIn(key, json.dumps(result))
        self.assertEqual(os.environ['GROQ_API_KEY'], key)
        self.assertIn('GROQ_API_KEY', (self.sandbox.root/'.env').read_text())
