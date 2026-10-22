"""Provider protocol, credential isolation, and window-failure regressions."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.helpers import Sandbox
from tests.test_overlay import FakeSession, FakeSpeaker, ServerCase
from tests.test_companion_upgrade import shell
from core import config, model_catalog, overlay, provider_registry
from core.providers import remote

KEY = 'secret-not-for-logs'
MESSAGES = [{'role': 'system', 'content': 'Be helpful'}, {'role': 'user', 'content': 'Hello'}]
REMOTE = ('openai', 'anthropic', 'openrouter', 'deepseek', 'xai')


def response(data, status=200):
    return SimpleNamespace(status_code=status, json=lambda: data)


class RemoteChatTests(unittest.TestCase):
    def test_each_provider_uses_its_own_key_url_and_protocol(self):
        env = {info['key_env']: name + KEY for name, info in provider_registry.PROVIDERS.items() if info['key_env']}
        with mock.patch.dict(os.environ, env):
            for provider in REMOTE:
                with self.subTest(provider=provider):
                    data = {'content': [{'type': 'thinking', 'thinking': 'private'}, {'type': 'text', 'text': ' hi '}]} \
                        if provider == 'anthropic' else {'choices': [{'message': {'content': ' hi ', 'reasoning_content': 'private'}}]}
                    post = mock.Mock(return_value=response(data))
                    self.assertEqual(remote.chat(provider, 'test-chat', MESSAGES, post=post), 'hi')
                    url = post.call_args.args[0]
                    kw = post.call_args.kwargs
                    self.assertTrue(url.startswith(provider_registry.PROVIDERS[provider]['base'] + '/'))
                    self.assertNotIn(KEY, url + json.dumps(kw['json']))
                    expected_key = provider + KEY
                    self.assertIn(expected_key, str(kw['headers']))
                    self.assertEqual(kw['timeout'], (5, 120))
                    self.assertFalse(kw['allow_redirects'])
                    if provider == 'anthropic':
                        self.assertEqual(kw['json']['system'], 'Be helpful')
                        self.assertEqual(kw['json']['messages'], MESSAGES[1:])
                        self.assertEqual(kw['headers']['anthropic-version'], '2023-06-01')
                        self.assertGreater(kw['json']['max_tokens'], 0)
                    else:
                        self.assertEqual(kw['json']['messages'], MESSAGES)

    def test_missing_and_placeholder_keys_never_call_provider(self):
        for provider in REMOTE:
            for key in ('', 'your_api_key_here'):
                with self.subTest(provider=provider, key=key), mock.patch.dict(os.environ, {
                    provider_registry.PROVIDERS[provider]['key_env']: key
                }):
                    post = mock.Mock()
                    with self.assertRaises(remote.ProviderError):
                        remote.chat(provider, 'test-chat', MESSAGES, post=post)
                    post.assert_not_called()

    def test_http_errors_and_malformed_responses_never_expose_keys_or_reasoning(self):
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': KEY}):
            for code in (301, 400, 401, 402, 403, 404, 429, 500):
                with self.subTest(status=code), self.assertRaises(remote.ProviderError) as caught:
                    remote.chat('openai', 'test-chat', MESSAGES,
                                post=lambda *a, **k: response({'error': KEY}, code))
                self.assertNotIn(KEY, str(caught.exception))
                self.assertIn(str(code), str(caught.exception))
            for data in ({}, [], {'choices': []}, {'choices': [{'message': {'content': None, 'reasoning_content': KEY}}]}):
                with self.subTest(data=data), self.assertRaises(remote.ProviderError) as caught:
                    remote.chat('openai', 'test-chat', MESSAGES, post=lambda *a, **k: response(data))
                self.assertNotIn(KEY, str(caught.exception))
            with self.assertRaises(remote.ProviderError) as caught:
                remote.chat('openai', 'test-chat', MESSAGES, post=mock.Mock(side_effect=RuntimeError(KEY)))
            self.assertNotIn(KEY, str(caught.exception))

    def test_timeout_is_actionable_and_request_is_not_retried(self):
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': KEY}):
            post = mock.Mock(side_effect=remote.requests.exceptions.Timeout(KEY))
            with self.assertRaisesRegex(remote.ProviderError, 'too long'):
                remote.chat('openai', 'test-chat', MESSAGES, post=post)
            self.assertEqual(post.call_count, 1)

    def test_failure_does_not_save_a_partial_exchange_and_is_visible_to_overlay(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            session.change_provider('openai', 'test-chat')
            backend = overlay.OverlayBackend(lambda: session, FakeSpeaker())
            original = list(session.chat['messages'])
            with mock.patch('core.session.chat', side_effect=remote.ProviderError('OpenAI: API key rejected (HTTP 401).')), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(overlay.BackendError, 'API key rejected'):
                    backend.send_message('hello')
            self.assertEqual(session.chat['messages'], original)
            self.assertEqual(backend.get_state()['state'], 'idle')
            self.assertFalse(any(m['content'] == 'hello' for m in session.messages))

    def test_dispatches_all_added_providers(self):
        from core.providers import chat
        with mock.patch('core.providers.remote_chat', return_value='hello') as call:
            for provider in REMOTE:
                self.assertEqual(chat(provider, 'test-chat', MESSAGES), 'hello')
                call.assert_called_with(provider, 'test-chat', MESSAGES)


class RemoteModelTests(unittest.TestCase):
    def test_each_provider_listing_uses_correct_endpoint_and_headers(self):
        for provider in REMOTE:
            with self.subTest(provider=provider):
                data = {'models' if provider == 'xai' else 'data': [
                    {'id': 'gpt-chat-example', 'display_name': 'Example', 'architecture': {'output_modalities': ['text']}},
                    {'id': 'gpt-image-example', 'architecture': {'output_modalities': ['image']}},
                    {'id': '../bad'}, {'id': ''}, None,
                ]}
                get = mock.Mock(return_value=response(data))
                models = model_catalog.list_remote_models(provider, get=get, key=KEY)
                self.assertEqual([m['id'] for m in models], ['gpt-chat-example'])
                self.assertNotIn(KEY, get.call_args.args[0])
                self.assertIn(KEY, str(get.call_args.kwargs['headers']))
                self.assertFalse(get.call_args.kwargs['allow_redirects'])
                endpoint = '/language-models' if provider == 'xai' else '/models/user' if provider == 'openrouter' else '/models'
                self.assertTrue(get.call_args.args[0].endswith(endpoint))

    def test_anthropic_pagination_is_complete_and_repeated_cursor_is_rejected(self):
        get = mock.Mock(side_effect=[response({'data': [{'id': 'claude-one'}], 'has_more': True, 'last_id': 'claude-one'}),
                                    response({'data': [{'id': 'claude-two'}], 'has_more': False})])
        self.assertEqual(len(model_catalog.list_remote_models('anthropic', get=get, key=KEY)), 2)
        self.assertEqual(get.call_args.kwargs['params']['after_id'], 'claude-one')
        repeated = mock.Mock(return_value=response({'data': [], 'has_more': True, 'last_id': 'same'}))
        with self.assertRaises(model_catalog.ModelDiscoveryError):
            model_catalog.list_remote_models('anthropic', get=repeated, key=KEY)

    def test_openai_filters_obvious_nonchat_models(self):
        data = {'data': [{'id': name} for name in ('gpt-chat-example', 'gpt-image-1', 'tts-1', 'whisper-1',
            'text-embedding-3-small', 'gpt-realtime', 'gpt-codex-example', 'gpt-audio-example')]}
        found = model_catalog.list_remote_models('openai', get=lambda *a, **k: response(data), key=KEY)
        self.assertEqual([m['id'] for m in found], ['gpt-chat-example'])

    def test_cache_isolated_by_provider_and_key_and_contains_no_plaintext_keys(self):
        env = {'OPENAI_API_KEY': KEY, 'ANTHROPIC_API_KEY': KEY}
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, env):
            path = Path(root) / 'models.json'
            catalog = model_catalog.ModelCatalog(path)
            with mock.patch.object(model_catalog, 'list_remote_models', side_effect=lambda p, **k: [{'id': p + '-chat', 'label': p}]):
                self.assertEqual(catalog.list('openai')['models'][0]['id'], 'openai-chat')
                self.assertEqual(catalog.list('anthropic')['models'][0]['id'], 'anthropic-chat')
            with mock.patch.object(model_catalog, 'list_remote_models', side_effect=model_catalog.ModelDiscoveryError('offline')):
                self.assertEqual(catalog.list('openai')['source'], 'cached')
                self.assertEqual(catalog.list('anthropic', refresh=True)['models'][0]['id'], 'anthropic-chat')
                with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'different-secret'}):
                    self.assertEqual(catalog.list('openai', ['saved-model'])['source'], 'saved')
            for file in Path(root).glob('*.json'):
                self.assertNotIn(KEY, file.read_text())

    def test_bad_json_error_and_missing_key_are_safe(self):
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            get = mock.Mock()
            with self.assertRaisesRegex(model_catalog.ModelDiscoveryError, 'OPENAI_API_KEY'):
                model_catalog.list_remote_models('openai', get=get)
            get.assert_not_called()
        for data in ({}, {'data': 'invalid'}, []):
            with self.assertRaises(model_catalog.ModelDiscoveryError):
                model_catalog.list_remote_models('openai', get=lambda *a, **k: response(data), key=KEY)
        with self.assertRaises(model_catalog.ModelDiscoveryError) as caught:
            model_catalog.list_remote_models('openai', get=lambda *a, **k: response({'error': KEY}, 401), key=KEY)
        self.assertNotIn(KEY, str(caught.exception))


class ProviderPersistenceTests(unittest.TestCase):
    def test_each_added_provider_survives_chat_and_default_reload(self):
        for provider in REMOTE:
            with self.subTest(provider=provider), Sandbox() as sandbox:
                session = sandbox.make_session()
                session.change_provider(provider, 'test-chat')
                session.save_defaults(provider=provider, model='test-chat')
                loaded = sandbox.make_session()
                self.assertEqual((loaded.provider, loaded.model), (provider, 'test-chat'))
                self.assertEqual(loaded.default_provider, provider)

    def test_provider_change_rejects_stale_chat_and_rolls_back_on_save_failure(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            backend = overlay.OverlayBackend(lambda: session, FakeSpeaker())
            with self.assertRaisesRegex(overlay.BackendError, 'chat or provider changed'):
                backend.set_provider('openai', 'test-chat', session_id='old-chat')
            with self.assertRaisesRegex(overlay.BackendError, 'active chat changed'):
                backend.set_model('test-chat', provider='cloud', session_id='old-chat')
            with mock.patch('core.session.update_chat', side_effect=OSError('disk full')):
                with self.assertRaises(overlay.BackendError):
                    backend.set_provider('openai', 'test-chat')
            self.assertEqual(session.provider, 'cloud')
            backend.set_provider('openrouter', 'vendor/chat-example', expected_provider='cloud')
            self.assertEqual(session.provider, 'openrouter')


class ProviderEndpointTests(ServerCase):
    def test_provider_endpoints_require_auth_and_never_return_keys(self):
        for method, path, data in [('GET', '/api/providers', None),
                ('POST', '/api/provider', {'provider': 'openai', 'model': 'test-chat'})]:
            self.assertEqual(self.request(method, path, data, token=False)[0], 401)
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': KEY}):
            status, body, *_ = self.request('GET', '/api/providers')
        self.assertEqual(status, 200)
        self.assertNotIn(KEY, json.dumps(body))
        self.assertTrue(next(p for p in body['providers'] if p['id'] == 'openai')['configured'])
        self.assertEqual(self.request('GET', '/api/models?provider=unknown')[0], 400)
        self.assertEqual(self.request('POST', '/api/provider', {'provider': [], 'model': 'test-chat'})[0], 400)

    def test_models_can_be_previewed_without_changing_active_provider(self):
        self.session.provider = "cloud"
        with mock.patch.object(self.backend.catalog, 'list', return_value={'models': [], 'source': 'saved', 'warning': None}) as listing:
            status, body, *_ = self.request('GET', '/api/models?provider=openai')
        self.assertEqual(status, 200)
        self.assertEqual(body['provider'], 'openai')
        self.assertIsNone(body['current'])
        self.assertEqual(self.session.provider, 'cloud')
        listing.assert_called_once_with('openai', [], refresh=False)


class FirstRemoteKeyTests(unittest.TestCase):
    def test_new_openai_key_discovers_models_once_at_startup(self):
        from tests.test_start_integration import WosProject, write_stub, FAKE_REQUESTS
        project = WosProject()
        self.addCleanup(project.close)
        settings = json.loads(project.config_path.read_text())
        settings.update(provider='openai', model='gpt-saved-chat')
        project.config_path.write_text(json.dumps(settings))
        original = project._env
        project._env = lambda: dict(original(), OPENAI_API_KEY=KEY)
        stub = FAKE_REQUESTS['requests/__init__.py'] + """
_original_get = get
def get(url, **kwargs):
    if url.endswith('/models'):
        return Response(200, json.dumps({'data': [{'id': 'gpt-discovered-chat'}]}))
    return _original_get(url, **kwargs)
"""
        write_stub(project.stubs, {'requests/__init__.py': stub})
        first = project.run_scripted(['1', '/status', '/exit'])
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn('A new OpenAI key is configured', first.stdout)
        self.assertIn('gpt-discovered-chat', first.stdout)
        self.assertNotIn(KEY, first.stdout + first.stderr)
        again = project.run_scripted(['/status', '/exit'])
        self.assertNotIn('A new OpenAI key is configured', again.stdout)
        self.assertIn('gpt-discovered-chat', again.stdout)


class WindowFailureTests(unittest.TestCase):
    def test_failed_pin_does_not_change_saved_state_or_raise_to_bridge(self):
        class BrokenWindow:
            @property
            def on_top(self): return True
            @on_top.setter
            def on_top(self, enabled): raise RuntimeError('native failure')
        with mock.patch.object(shell, '_window', BrokenWindow()), \
             mock.patch.object(shell, '_geometry', {'on_top': True}), \
             mock.patch.object(shell, 'schedule_geometry_save') as save:
            result = shell.Api().set_on_top(False)
            self.assertTrue(result['on_top'])
            self.assertIn('error', result)
            save.assert_not_called()

    def test_successful_pin_does_not_query_native_geometry_synchronously(self):
        class Window:
            on_top = True
            @property
            def width(self): raise AssertionError('extra native call')
        window = Window()
        with mock.patch.object(shell, '_window', window), \
             mock.patch.object(shell, '_geometry', {'on_top': True}), \
             mock.patch.object(shell, 'schedule_geometry_save') as save:
            self.assertFalse(shell.Api().set_on_top(False)['on_top'])
            self.assertFalse(window.on_top)
            save.assert_called_once()
