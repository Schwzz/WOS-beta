"""Regressions for cancellation, model discovery and Companion lifecycle.
No credentials, provider requests, sound hardware or desktop are required.
"""
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.helpers import Sandbox, PROJECT_ROOT
from tests.test_overlay import FakeSession, FakeSpeaker, ServerCase
from tests.test_tts import FakePlayer, FakePost, FakeResponse, KEY, make_wav
from core import companion, model_catalog, overlay, tts


def thread_call(fn):
    result = {}
    def run():
        try:
            result['value'] = fn()
        except BaseException as error:
            result['error'] = error
    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


class CancellationRegressionTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {tts.API_KEY_ENV: KEY})
        patcher.start()
        self.addCleanup(patcher.stop)

    def speaker(self, **kw):
        speaker = tts.FishAudioSpeaker({'enabled': True}, notify=lambda _: None,
                                      post=kw.get('post', FakePost()), player=kw.get('player', FakePlayer()))
        self.addCleanup(speaker.shutdown)
        return speaker

    def test_disable_during_envelope_preparation_never_starts_audio(self):
        entered, resume = threading.Event(), threading.Event()
        player = FakePlayer()
        speaker = self.speaker(player=player)
        backend = overlay.OverlayBackend(lambda: FakeSession(), speaker)
        speaker.listener = backend.on_speech_event
        def envelope(_):
            entered.set()
            resume.wait(3)
            return {'levels': [1], 'fps': 50, 'duration': .02}
        with mock.patch.object(tts, 'wav_envelope', side_effect=envelope):
            speaker.speak('A delayed reply')
            self.assertTrue(entered.wait(2))
            speaker.set_enabled(False)
            self.assertEqual(backend.get_state()['state'], 'idle')
            self.assertIsNone(backend.get_state()['audio'])
            resume.set()
            speaker.wait()
        self.assertEqual(player.played, [])

    def test_stop_clears_pending_without_waiting_for_http(self):
        entered, resume = threading.Event(), threading.Event()
        def post(*args, **kw):
            entered.set()
            resume.wait(3)
            return FakeResponse()
        speaker = self.speaker(post=post)
        backend = overlay.OverlayBackend(lambda: FakeSession(), speaker)
        speaker.listener = backend.on_speech_event
        speaker.speak('Still downloading')
        self.assertTrue(entered.wait(2))
        self.assertEqual(backend.get_state()['phase'], 'preparing')
        backend.stop_speech()
        self.assertFalse(speaker.active)
        self.assertTrue(speaker.enabled)
        self.assertEqual(backend.get_state()['state'], 'idle')
        backend.on_speech_event('start', 1, envelope={'levels': [1]})
        self.assertIsNone(backend.get_state()['audio'])
        resume.set()
        speaker.wait()
        self.assertEqual(speaker._player.played, [])

    def test_superseded_slow_request_does_not_keep_avatar_thinking(self):
        backend = overlay.OverlayBackend(lambda: FakeSession(), FakeSpeaker())
        backend.on_speech_event('queued', 1)
        backend.on_speech_event('queued', 2)
        backend.on_speech_event('start', 2)
        backend.on_speech_event('end', 2)
        self.assertEqual(backend.get_state()['state'], 'idle')
        backend.on_speech_event('start', 1)
        self.assertEqual(backend.get_state()['state'], 'idle')

    def test_stop_while_model_generates_suppresses_late_speech(self):
        for source in ('ui', 'terminal'):
            with self.subTest(source=source):
                session, speaker = FakeSession(), FakeSpeaker(enabled=True)
                session.gate = threading.Event()
                backend = overlay.OverlayBackend(lambda: session, speaker)
                thread, result = thread_call(lambda: backend.send_message('hello', source))
                deadline = time.monotonic() + 2
                while not session.calls and time.monotonic() < deadline:
                    time.sleep(.005)
                backend.stop_speech()
                session.gate.set()
                thread.join(2)
                self.assertNotIn('error', result)
                if source == 'terminal':
                    backend.speak_terminal_reply(result['value'])
                self.assertEqual(speaker.spoken, [])

    def test_real_player_starts_async_and_stop_wakes_waiter(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'audio.wav'
            path.write_bytes(make_wav(pcm=b'\x01\x02' * 100000, rate=1000))
            player = tts.WinsoundPlayer()
            calls, started = [], threading.Event()
            player._winsound = SimpleNamespace(SND_FILENAME=2, SND_ASYNC=1, SND_NODEFAULT=4,
                PlaySound=lambda sound, flags: calls.append((sound, flags)))
            cancelled = threading.Event()
            thread, result = thread_call(lambda: player.play(str(path), cancelled=cancelled, started=started.set))
            self.assertTrue(started.wait(2))
            player.stop()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertNotIn('error', result)
            self.assertTrue(cancelled.is_set())
            self.assertEqual(calls, [(str(path), 7), (None, 0)])
            calls.clear()
            player.play(str(path), cancelled=cancelled)
            self.assertEqual(calls, [])

    def test_shutdown_prevents_any_new_speech(self):
        speaker = self.speaker()
        speaker.shutdown()
        self.assertFalse(speaker.speak('Too late'))


class ModelDiscoveryTests(unittest.TestCase):
    def test_filters_paginates_deduplicates_and_keeps_key_out_of_url(self):
        get = mock.Mock(side_effect=[
            SimpleNamespace(status_code=200, json=lambda: {'models': [
                {'name': 'models/chat-a', 'displayName': 'Chat A', 'supportedGenerationMethods': ['generateContent']},
                {'name': 'models/embedding', 'supportedGenerationMethods': ['embedContent']},
                {'name': 'models/bad', 'supportedGenerationMethods': None},
            ], 'nextPageToken': 'page-two'}),
            SimpleNamespace(status_code=200, json=lambda: {'models': [
                {'name': 'models/chat-b', 'supportedGenerationMethods': ['generateContent']},
                {'name': 'models/chat-a', 'displayName': 'Chat A', 'supportedGenerationMethods': ['generateContent']},
            ]}),
        ])
        result = model_catalog.list_gemini_models(get=get, api_key=KEY)
        self.assertEqual([item['id'] for item in result], ['chat-a', 'chat-b'])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs['params']['pageToken'], 'page-two')
        for args, kw in get.call_args_list:
            self.assertNotIn(KEY, args[0])
            self.assertEqual(kw['headers']['x-goog-api-key'], KEY)
            self.assertEqual(kw['timeout'], (3, 10))

    def test_errors_never_include_exception_text_or_key(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status), self.assertRaises(model_catalog.ModelDiscoveryError) as caught:
                model_catalog.list_gemini_models(api_key=KEY, get=lambda *a, **k: SimpleNamespace(status_code=status))
            self.assertNotIn(KEY, str(caught.exception))
        with self.assertRaises(model_catalog.ModelDiscoveryError) as caught:
            model_catalog.list_gemini_models(api_key=KEY, get=mock.Mock(side_effect=RuntimeError(KEY)))
        self.assertNotIn(KEY, str(caught.exception))

    def test_missing_key_never_calls_provider(self):
        with mock.patch.dict(os.environ, {'GEMINI_API_KEY': ''}):
            get = mock.Mock()
            with self.assertRaises(model_catalog.ModelDiscoveryError):
                model_catalog.list_gemini_models(get=get)
            get.assert_not_called()

    def test_cache_survives_restart_and_is_scoped_to_key(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {'GEMINI_API_KEY': KEY}):
            path = Path(root) / 'models.json'
            models = [{'id': 'chat-a', 'label': 'Chat A'}]
            with mock.patch.object(model_catalog, 'list_gemini_models', return_value=models) as listing:
                self.assertEqual(model_catalog.ModelCatalog(path).list('cloud')['source'], 'live')
                self.assertEqual(model_catalog.ModelCatalog(path).list('cloud')['source'], 'cached')
                self.assertEqual(listing.call_count, 1)
            self.assertNotIn(KEY, path.read_text())
            with mock.patch.object(model_catalog, 'list_gemini_models', side_effect=model_catalog.ModelDiscoveryError('offline')):
                result = model_catalog.ModelCatalog(path).list('cloud', refresh=True)
                self.assertEqual(result['source'], 'cached')
                self.assertEqual(result['models'], models)
                self.assertEqual(result['warning'], 'offline')
                with mock.patch.dict(os.environ, {'GEMINI_API_KEY': 'different-key'}):
                    result = model_catalog.ModelCatalog(path).list('cloud', ['saved-a'], refresh=True)
                    self.assertEqual(result['source'], 'saved')
                    self.assertEqual(result['models'][0]['id'], 'saved-a')

    def test_malformed_or_repeated_pagination_fails_safely(self):
        for data in ([], {'models': 'bad'}, {'models': [], 'nextPageToken': 'same'}):
            response = SimpleNamespace(status_code=200, json=lambda: data)
            with self.subTest(data=data), self.assertRaises(model_catalog.ModelDiscoveryError):
                model_catalog.list_gemini_models(api_key=KEY, get=lambda *a, **k: response)


class FirstKeySetupTests(unittest.TestCase):
    def project(self):
        from tests.test_start_integration import WosProject, write_stub, FAKE_REQUESTS
        project = WosProject()
        self.addCleanup(project.close)
        original_env = project._env
        project._env = lambda: dict(original_env(), GEMINI_API_KEY=KEY)
        stub = FAKE_REQUESTS['requests/__init__.py'] + '''
_original_get = get
def get(url, **kwargs):
    if url.endswith('/models'):
        return Response(200, json.dumps({'models': [
            {'name': 'models/live-chat', 'displayName': 'Live Chat',
             'supportedGenerationMethods': ['generateContent']}
        ]}))
    return _original_get(url, **kwargs)
'''
        write_stub(project.stubs, {'requests/__init__.py': stub})
        return project

    def test_new_key_offers_live_models_once_then_remembers_selection(self):
        project = self.project()
        result = project.run_scripted(['1', '/status', '/exit'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('live-chat', result.stdout)
        self.assertIn('A new Gemini key is configured', result.stdout)
        self.assertNotIn(KEY, result.stdout + result.stderr)
        again = project.run_scripted(['/status', '/exit'])
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertNotIn('A new Gemini key is configured', again.stdout)
        self.assertIn('live-chat', again.stdout)

    def test_cancelling_first_key_picker_keeps_current_model(self):
        project = self.project()
        result = project.run_scripted(['0', '/exit'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        chat = json.loads((project.project / 'data/chats/session_001.json').read_text())
        self.assertEqual(chat['model'], 'gemini-3.5-flash-lite')
        self.assertFalse((project.project / 'data/cloud_model_setup.json').exists())


class PreferenceTests(unittest.TestCase):
    def test_saved_preferences_merge_and_survive_recreation(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'companion.json'
            store = companion.Preferences(path)
            store.update({'accent': '#123456', 'preset': 'wave'})
            store.update({'theme': 'light'})
            saved = companion.Preferences(path).get()
            self.assertEqual(saved['accent'], '#123456')
            self.assertEqual(saved['theme'], 'light')
            self.assertEqual(saved['preset'], 'wave')

    def test_invalid_values_never_overwrite_last_good_settings(self):
        with tempfile.TemporaryDirectory() as root:
            store = companion.Preferences(Path(root) / 'prefs.json')
            store.update({'preset': 'rings'})
            for bad in ({'preset': 'script'}, {'accent': 'red'}, {'size': True}, {'glow': float('nan')},
                        {'motion': 4}, {'unknown': 1}, {'theme': 'oops'}, []):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    store.update(bad)
                self.assertEqual(store.get()['preset'], 'rings')


class BackendRegressionTests(unittest.TestCase):
    def test_lost_response_retry_is_delivered_only_once(self):
        session = FakeSession()
        backend = overlay.OverlayBackend(lambda: session, FakeSpeaker())
        first = backend.send_message('hello', request_id='message-1')
        second = backend.send_message('hello', request_id='message-1')
        self.assertEqual(first, second)
        self.assertEqual(session.calls, ['hello'])
        with self.assertRaises(overlay.BackendError):
            backend.send_message('different', request_id='message-1')

    def test_changed_chat_rejects_pending_message(self):
        session = FakeSession()
        session.session_id = 'new-chat'
        backend = overlay.OverlayBackend(lambda: session, FakeSpeaker())
        with self.assertRaises(overlay.BackendError):
            backend.send_message('hello', session_id='old-chat')
        self.assertEqual(session.calls, [])

    def test_terminal_changes_refresh_overlay_without_new_message(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            backend = overlay.OverlayBackend(lambda: session, FakeSpeaker())
            for command in (session.new_chat, session.clear, lambda: session.switch_chat('session_001')):
                before = backend.get_state()['chat_version']
                backend.hold_command()
                try:
                    command()
                finally:
                    backend.release_command()
                self.assertGreater(backend.get_state()['chat_version'], before)
                self.assertEqual(backend.get_state()['session_id'], session.session_id)

    def test_history_has_accessible_earlier_pages(self):
        session = FakeSession()
        session.chat = {'messages': [{'role': 'user', 'content': str(i)} for i in range(230)]}
        backend = overlay.OverlayBackend(lambda: session, FakeSpeaker())
        last = backend.get_history()
        previous = backend.get_history(last['before'])
        first = backend.get_history(previous['before'])
        self.assertTrue(last['has_more'])
        self.assertFalse(first['has_more'])
        all_messages = first['messages'] + previous['messages'] + last['messages']
        self.assertEqual([item['content'] for item in all_messages], [str(i) for i in range(230)])


class CompanionApiTests(ServerCase):
    def test_new_endpoints_require_authentication(self):
        for method, path, data in (('GET', '/api/models', None), ('GET', '/api/preferences', None),
                                  ('POST', '/api/stop', {}), ('POST', '/api/model', {'model': 'x'}),
                                  ('POST', '/api/preferences', {'preset': 'wave'})):
            self.assertEqual(self.request(method, path, data, token=False)[0], 401)

    def test_preferences_are_validated_and_persisted(self):
        self.backend.preferences = companion.Preferences(Path(self.tmp.name) / 'prefs.json')
        self.assertEqual(self.request('POST', '/api/preferences', {'preset': 'wave'})[0], 200)
        self.assertEqual(self.request('GET', '/api/preferences')[1]['preset'], 'wave')
        self.assertEqual(self.request('POST', '/api/preferences', {'glow': -5})[0], 400)

    def test_stop_does_not_change_voice_setting(self):
        with mock.patch.object(self.backend, 'stop_speech') as cancel:
            self.assertEqual(self.request('POST', '/api/stop', {})[0], 200)
            cancel.assert_called_once()
        self.assertFalse(self.speaker.enabled)

    def test_history_invalid_position_returns_400(self):
        self.assertEqual(self.request('GET', '/api/history?before=bad')[0], 400)


spec = importlib.util.spec_from_file_location('wos_shell', PROJECT_ROOT / 'overlay' / 'main.py')
shell = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shell)


class WindowLifecycleTests(unittest.TestCase):
    def test_restored_window_stays_visible_on_negative_coordinate_monitor(self):
        screens = [SimpleNamespace(x=-1920, y=0, width=1920, height=1080),
                   SimpleNamespace(x=0, y=0, width=1920, height=1080)]
        size = shell.clamp_geometry({'x': -1800, 'y': 50, 'width': 500, 'height': 700}, screens)
        self.assertEqual(size['x'], -1800)
        size = shell.clamp_geometry({'x': 9000, 'y': 9000, 'width': 99999, 'height': 99999}, screens)
        self.assertLessEqual(size['width'], 1920)
        self.assertGreaterEqual(size['x'], -1920)
        self.assertLessEqual(size['y'] + size['height'], 1080)

    def test_native_close_sends_authenticated_stop(self):
        with mock.patch.object(shell, 'save_geometry'), mock.patch.object(shell, 'read_endpoint',
                return_value={'port': 1234, 'token': 'secret'}), mock.patch.object(shell.urllib.request, 'urlopen') as request:
            shell.stop_backend_speech()
            sent = request.call_args.args[0]
            self.assertEqual(sent.full_url, 'http://127.0.0.1:1234/api/stop')
            self.assertEqual(sent.get_header('X-wos-token'), 'secret')
            self.assertEqual(sent.data, b'{}')

    def test_mode_switch_keeps_custom_sizes(self):
        fake = SimpleNamespace(width=410, height=440, x=30, y=100)
        fake.resize = lambda w, h: (setattr(fake, 'width', w), setattr(fake, 'height', h))
        fake.move = lambda x, y: (setattr(fake, 'x', x), setattr(fake, 'y', y))
        with tempfile.TemporaryDirectory() as root, mock.patch.object(shell, '_window', fake), \
             mock.patch.object(shell, '_geometry_path', Path(root) / 'geometry.json'), \
             mock.patch.object(shell, '_geometry', {'companion': {'width': 340, 'height': 420},
                                                   'chat': {'width': 600, 'height': 800}, 'on_top': True}), \
             mock.patch.object(shell, '_expanded', False):
            shell.Api().set_expanded(True)
            self.assertEqual((fake.width, fake.height), (600, 800))
            shell.Api().set_expanded(False)
            self.assertEqual((fake.width, fake.height), (410, 440))
