"""Headless keyboard/interaction tests against the real command loop and session.
No provider requests, API keys, audio hardware, or desktop window are used.
"""
import asyncio
import contextlib
import io
import os
import runpy
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.helpers import Sandbox, PROJECT_ROOT
from core.cli import KNOWN_COMMANDS
from core.overlay import OverlayBackend
from core.provider_registry import PROVIDERS
from core.terminal_bridge import TerminalBridge, Prompt, Ready, Finished
from core.terminal_ui import WosTerminal, ChoiceScreen, PromptScreen, command_choices
from core.tts import FishAudioSpeaker
from textual.widgets import Input, Markdown, Static


class CompletionTests(unittest.TestCase):
    def test_completion_uses_known_commands_and_subcommands(self):
        choices = dict(command_choices('/'))
        self.assertTrue(set(KNOWN_COMMANDS).issubset(choices))
        self.assertIn('/chat switch', choices)
        self.assertEqual([name for name, _ in command_choices('/pro')], ['/provider'])
        self.assertEqual([name for name, _ in command_choices('/chat sw')], ['/chat switch'])
        self.assertEqual(command_choices('hello /model'), [])
        self.assertEqual(command_choices('/model another-model'), [])
        self.assertEqual(command_choices('/model\nhello'), [])


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sandbox = self.enterContext(Sandbox())
        self.enterContext(mock.patch.dict(os.environ, {info['key_env']: '' for info in PROVIDERS.values() if info['key_env']} | {'FISH_AUDIO_API_KEY': ''}))
        with mock.patch.object(sys, 'argv', ['start.py', '--plain']), \
             mock.patch('builtins.input', side_effect=EOFError), contextlib.redirect_stdout(io.StringIO()):
            loaded = runpy.run_path(str(PROJECT_ROOT / 'start.py'), run_name='wos_terminal_test')
        # Functions resolve their module globals; runpy's returned dictionary is a copy.
        self.ns = loaded['run_loop'].__globals__
        # runpy's terminal has exited and closed its Session; mount a fresh one.
        self.session = self.ns['session'] = self.sandbox.make_session()
        self.speaker = FishAudioSpeaker({'enabled': False}, notify=lambda *_: None)
        self.addCleanup(self.speaker.shutdown)
        self.backend = OverlayBackend(lambda: self.ns['session'], self.speaker)
        self.speaker.listener = self.backend.on_speech_event
        self.ns.update(tts_speaker=self.speaker, overlay_backend=self.backend)
        self.app = WosTerminal(self.ns)
        self.addCleanup(self.app.bridge.close)

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1

    async def wait_for(self, condition, timeout=10):
        async def wait():
            while not condition():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), timeout)

    async def ready(self):
        await self.wait_for(lambda: not self.app.busy)

    async def type_text(self, pilot, text):
        self.app.composer.load_text(text)
        self.app.composer.move_cursor(self.app.composer.document.end)
        await pilot.pause()

    def test_plain_terminal_branding_keeps_error_severity_colors(self):
        color = self.ns['color_for_line']
        self.assertEqual(color('Auralis: Could not save chat.'), self.ns['RED'])
        self.assertEqual(color('Auralis: Conversation saved.'), self.ns['BLUE'])

    async def test_slash_popup_arrows_tab_and_escape_preserve_input(self):
        async with self.app.run_test(size=(110, 36)) as pilot:
            await self.ready()
            await pilot.press('/')
            self.assertTrue(self.app.suggestions.display)
            self.assertEqual(self.app.selected_command(), '/help')
            await pilot.press('down')
            self.assertEqual(self.app.selected_command(), '/chat')
            await pilot.press('up', 'tab')
            self.assertEqual(self.app.composer.text, '/help ')
            await self.type_text(pilot, '/pro')
            await pilot.press('escape')
            self.assertFalse(self.app.suggestions.display)
            self.assertEqual(self.app.composer.text, '/pro')
            await pilot.press('ctrl+q')
        self.assertFalse(self.app.bridge.thread.is_alive())

    async def test_enter_executes_slash_command_and_unknown_commands_never_call_model(self):
        async with self.app.run_test() as pilot:
            await self.ready()
            with mock.patch('core.session.chat') as chat:
                await pilot.press('/', 'enter')
                await self.ready()
                self.assertTrue(self.app.query_one('#notice-row').display)
                self.assertEqual(self.app.composer.text, '')
                await self.type_text(pilot, '/moodel')
                await pilot.press('enter')
                await self.ready()
                chat.assert_not_called()
                self.assertFalse(self.session.chat['messages'])
            await pilot.press('ctrl+l')
            self.assertFalse(self.app.query_one('#notice-row').display)

    async def test_provider_and_model_pickers_share_terminal_session_and_overlay(self):
        self.ns['fetch_installed_model_details'] = lambda: ([('local-test:latest', 1000)], None)
        async with self.app.run_test() as pilot:
            await self.ready()
            await pilot.press('f3')
            await self.wait_for(lambda: isinstance(self.app.screen, ChoiceScreen))
            await pilot.press(*'ollama', 'enter')
            await self.wait_for(lambda: isinstance(self.app.screen, ChoiceScreen) and 'Models' in self.app.screen.title_text)
            self.assertEqual(self.session.provider, 'cloud')
            await pilot.press(*'local-test', 'enter')
            await self.ready()
            self.assertEqual((self.session.provider, self.session.model), ('ollama', 'local-test:latest'))
            state = self.backend.get_state()
            self.assertEqual(state['provider'], 'ollama')
            self.assertEqual(state['model'], 'local-test:latest')
            self.assertEqual(self.sandbox.read_chat(self.session.session_id)['provider'], 'ollama')

    async def test_model_picker_is_searchable_and_cancel_changes_nothing(self):
        self.ns['ModelCatalog'] = lambda: SimpleNamespace(list=lambda *a, **k: {
            'models': [{'id': 'cloud-test-chat'}], 'source': 'live', 'warning': None})
        original = self.session.model
        async with self.app.run_test() as pilot:
            await self.ready()
            await pilot.press('f2')
            await self.wait_for(lambda: isinstance(self.app.screen, ChoiceScreen))
            await pilot.press('escape')
            await self.ready()
            self.assertEqual(self.session.model, original)
            await pilot.press('f2')
            await self.wait_for(lambda: isinstance(self.app.screen, ChoiceScreen))
            await pilot.press(*'cloud-test', 'enter')
            await self.ready()
            self.assertEqual(self.session.model, 'cloud-test-chat')

    async def test_empty_picker_search_cannot_select_a_hidden_option(self):
        async with self.app.run_test() as pilot:
            await self.ready()
            await pilot.press('f3')
            await self.wait_for(lambda: isinstance(self.app.screen, ChoiceScreen))
            await pilot.press(*'no-such-provider', 'enter')
            self.assertIsInstance(self.app.screen, ChoiceScreen)
            self.assertEqual(self.session.provider, 'cloud')
            await pilot.press('escape')
            await self.ready()

    async def test_chat_switch_preserves_each_draft(self):
        first = self.session.session_id
        async with self.app.run_test() as pilot:
            await self.ready()
            await self.type_text(pilot, 'A draft in the first chat')
            await pilot.press('ctrl+n')
            await self.ready()
            self.assertNotEqual(self.session.session_id, first)
            self.assertEqual(self.app.composer.text, '')
            await self.type_text(pilot, 'A draft in the second chat')
            await pilot.press('ctrl+o')
            await self.wait_for(lambda: isinstance(self.app.screen, ChoiceScreen))
            await pilot.press(*first, 'enter')
            await self.ready()
            self.assertEqual(self.session.session_id, first)
            self.assertEqual(self.app.composer.text, 'A draft in the first chat')
            self.assertIn('A draft in the second chat', self.app.drafts.values())

    async def test_destructive_command_requires_explicit_confirmation(self):
        self.session._record_exchange('keep this', 'a saved reply')
        async with self.app.run_test() as pilot:
            await self.ready()
            self.app.send('/clear')
            await self.wait_for(lambda: isinstance(self.app.screen, PromptScreen))
            await pilot.press('enter')  # Cancel has initial focus.
            await self.ready()
            self.assertEqual(len(self.session.chat['messages']), 2)
            self.app.send('/clear')
            await self.wait_for(lambda: isinstance(self.app.screen, PromptScreen))
            await pilot.pause()
            self.assertTrue(await pilot.click('#prompt-confirm'))
            await self.ready()
            self.assertEqual(self.session.chat['messages'], [])

    async def test_multiline_send_does_not_freeze_ui_and_escape_suppresses_late_speech(self):
        entered, resume = threading.Event(), threading.Event()
        def chat(*_):
            entered.set()
            resume.wait(5)
            return 'A reply after the wait'
        with mock.patch('core.session.chat', side_effect=chat), mock.patch.object(self.speaker, 'speak') as speak:
            try:
                async with self.app.run_test() as pilot:
                    await self.ready()
                    await pilot.press(*'hello', 'ctrl+j', *'again', 'enter')
                    await self.wait_for(entered.is_set)
                    self.assertTrue(self.app.busy)
                    self.assertEqual(self.backend.get_state()['phase'], 'thinking')
                    await pilot.press('ctrl+t')
                    self.assertEqual(self.app.theme, 'auralis-light')
                    await self.type_text(pilot, 'The next draft')
                    await pilot.press('enter')
                    self.assertEqual(self.app.composer.text, 'The next draft')
                    await pilot.press('escape')
                    resume.set()
                    await self.ready()
                    self.assertEqual(self.session.chat['messages'][0]['content'], 'hello\nagain')
                    speak.assert_not_called()
            finally:
                resume.set()
                self.app.bridge.close()

    async def test_quit_during_a_reply_closes_ui_without_cross_thread_lock_release(self):
        entered, resume = threading.Event(), threading.Event()
        def chat(*_):
            entered.set()
            resume.wait(5)
            return 'Late text'
        with mock.patch('core.session.chat', side_effect=chat):
            try:
                async with self.app.run_test() as pilot:
                    await self.ready()
                    await self.type_text(pilot, 'hello')
                    await pilot.press('enter')
                    await self.wait_for(entered.is_set)
                    await pilot.press('ctrl+q')
                self.assertTrue(self.app.bridge.closed.is_set())
            finally:
                resume.set()
                self.app.bridge.thread.join(2)
        self.assertFalse(self.app.bridge.thread.is_alive())
        self.assertTrue(self.backend._command_lock.acquire(blocking=False))
        self.backend._command_lock.release()

    async def test_quit_while_picker_is_open_releases_waiting_command_worker(self):
        async with self.app.run_test() as pilot:
            await self.ready()
            await pilot.press('f3')
            await self.wait_for(lambda: isinstance(self.app.screen, ChoiceScreen))
            await pilot.press('ctrl+q')
        self.app.bridge.thread.join(1)
        self.assertFalse(self.app.bridge.thread.is_alive())

    async def test_responsive_layout_keeps_composer_visible(self):
        async with self.app.run_test(size=(110, 36)) as pilot:
            await self.ready()
            for width, height in ((48, 18), (80, 24), (110, 36), (160, 48)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                self.assertGreater(self.app.composer.region.width, 20)
                self.assertLessEqual(self.app.composer.region.bottom, height - 2)
                self.assertFalse(self.app.sidebar_widget.display)
                await self.type_text(pilot, '/')
                self.assertTrue(self.app.suggestions.display)
                self.assertLessEqual(self.app.composer.region.bottom, height - 2)
                await self.type_text(pilot, '')

    async def test_earlier_history_and_background_updates_preserve_reading_position(self):
        self.session.chat['messages'] = [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': 'Message ' + str(i)} for i in range(130)]
        async with self.app.run_test(size=(80, 24)) as pilot:
            await self.ready()
            self.assertEqual(len(self.app.rows), 100)
            await pilot.pause(0.5)
            self.app.transcript.scroll_home(animate=False, immediate=True)
            await pilot.pause()
            self.assertTrue(await pilot.click('#load-older'))
            await pilot.pause()
            self.assertEqual(len(self.app.rows), 130)
            self.app.transcript.scroll_to(y=20, animate=False, immediate=True)
            await pilot.pause()
            position = self.app.transcript.scroll_y
            with mock.patch('core.session.chat', return_value='Overlay reply'):
                await asyncio.to_thread(self.backend.send_message, 'An overlay message', 'ui')
            await self.wait_for(lambda: len(self.app.rows) == 132)
            await pilot.pause()
            self.assertEqual(self.app.transcript.scroll_y, position)
            self.assertEqual(len(self.app.query(Markdown)), 132)

    async def test_restart_restores_ui_before_handling_process_restart(self):
        async with self.app.run_test() as pilot:
            await self.ready()
            await self.type_text(pilot, '/restart')
            await pilot.press('enter')
        self.assertEqual(self.app.return_value, 'restart')
        self.assertFalse(self.app.bridge.thread.is_alive())

    async def test_command_palette_preserves_draft_when_cancelled(self):
        async with self.app.run_test() as pilot:
            await self.ready()
            await self.type_text(pilot, 'An unsent thought')
            await pilot.press('ctrl+p')
            self.assertIsInstance(self.app.screen, ChoiceScreen)
            await pilot.press('escape')
            self.assertEqual(self.app.composer.text, 'An unsent thought')

    async def test_unexpected_command_error_is_visible_and_worker_recovers(self):
        original = self.ns['show_status']
        self.ns['show_status'] = mock.Mock(side_effect=ValueError('private-error-detail'))
        async with self.app.run_test() as pilot:
            await self.ready()
            self.app.send('/status')
            await self.ready()
            self.assertTrue(self.app.query_one('#notice-row').display)
            self.ns['show_status'] = original
            self.app.send('/status')
            await self.ready()
            self.assertFalse(self.app.bridge.closed.is_set())
            self.assertEqual(self.backend.get_state()['phase'], 'idle')

    async def test_voice_shortcut_changes_the_saved_setting(self):
        async with self.app.run_test() as pilot:
            await self.ready()
            await pilot.press('f6')
            await self.wait_for(lambda: not self.app._voice_toggle_busy)
            self.assertTrue(self.backend.get_state()['voice']['enabled'])
            self.assertTrue(self.ns['load_settings']()['tts']['enabled'])
            await pilot.press('f6')
            await self.wait_for(lambda: not self.app._voice_toggle_busy)
            self.assertFalse(self.backend.get_state()['voice']['enabled'])
            self.assertFalse(self.ns['load_settings']()['tts']['enabled'])

    async def test_input_prompt_can_be_cancelled_without_renaming_chat(self):
        original = self.session.chat['title']
        async with self.app.run_test() as pilot:
            await self.ready()
            self.app.send('/rename ' + self.session.session_id)
            await self.wait_for(lambda: isinstance(self.app.screen, PromptScreen))
            await pilot.press('escape')
            await self.ready()
            self.assertEqual(self.session.chat['title'], original)

    async def test_failed_clear_preserves_in_memory_and_saved_conversation(self):
        self.session._record_exchange('keep this', 'a saved reply')
        original = list(self.session.chat['messages'])
        original_context = list(self.session.messages)
        async with self.app.run_test() as pilot:
            await self.ready()
            with mock.patch('core.session.update_chat', side_effect=OSError('disk full')):
                self.app.send('/clear')
                await self.wait_for(lambda: isinstance(self.app.screen, PromptScreen))
                await pilot.pause()
                self.assertTrue(await pilot.click('#prompt-confirm'))
                await self.ready()
            self.assertEqual(self.session.chat['messages'], original)
            self.assertEqual(self.session.messages, original_context)
            self.assertEqual(self.sandbox.read_chat(self.session.session_id)['messages'], original)

    async def test_failed_reply_restores_submitted_text(self):
        with mock.patch('core.session.chat', return_value=None):
            async with self.app.run_test() as pilot:
                await self.ready()
                await self.type_text(pilot, 'A question to keep')
                await pilot.press('enter')
                await self.ready()
                self.assertEqual(self.app.composer.text, 'A question to keep')
                self.assertEqual(self.session.chat['messages'], [])

    async def test_failed_reply_does_not_overwrite_a_next_draft(self):
        entered, resume = threading.Event(), threading.Event()
        def chat(*_):
            entered.set()
            resume.wait(5)
            return None
        with mock.patch('core.session.chat', side_effect=chat):
            try:
                async with self.app.run_test() as pilot:
                    await self.ready()
                    await self.type_text(pilot, 'The first question')
                    await pilot.press('enter')
                    await self.wait_for(entered.is_set)
                    await self.type_text(pilot, 'The next question')
                    resume.set()
                    await self.ready()
                    self.assertEqual(self.app.composer.text, 'The next question')
                    self.assertTrue(self.app.query_one('#notice-row').display)
            finally:
                resume.set()
                self.app.bridge.close()

    async def test_background_reply_retains_existing_rendered_messages(self):
        for i in range(5):
            self.session._record_exchange('Question ' + str(i), 'Answer ' + str(i))
        async with self.app.run_test(size=(80, 24)) as pilot:
            await self.ready()
            await pilot.pause()
            before = list(self.app.query(Markdown))
            self.app.transcript.scroll_to(y=10, animate=False, immediate=True)
            await pilot.pause()
            position = self.app.transcript.scroll_y
            self.assertFalse(self.app.transcript.is_vertical_scroll_end)
            with mock.patch('core.session.chat', return_value='A background answer'):
                await asyncio.to_thread(self.backend.send_message, 'A background question', 'ui')
            await self.wait_for(lambda: len(self.app._history_nodes) == 12)
            await pilot.pause()
            after = list(self.app.query(Markdown))
            self.assertEqual(after[:len(before)], before)
            self.assertEqual(self.app.transcript.scroll_y, position)

    async def test_output_is_compact_and_details_preserve_draft(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            await self.ready()
            await self.type_text(pilot, 'Keep this draft')
            self.app.write_output('Auralis: Model changed.\nNew chats keep their defaults.')
            await pilot.pause()
            self.assertFalse(self.app.query('#command-output'))
            self.assertTrue(self.app.query_one('#notice-row').display)
            self.assertEqual(self.app.query_one('#notice-row').region.height, 1)
            await pilot.press('f8')
            self.assertEqual(type(self.app.screen).__name__, 'DetailsScreen')
            self.assertIn('Model changed.', '\n'.join(self.app.output_lines))
            await pilot.press('escape')
            self.assertEqual(self.app.composer.text, 'Keep this draft')
            self.assertTrue(self.app.composer.has_focus)
            await pilot.press('ctrl+l')
            self.assertFalse(self.app.query_one('#notice-row').display)
            self.assertIn('Model changed.', '\n'.join(self.app.output_lines))

    async def test_output_journal_is_bounded_and_sidebar_opt_in(self):
        async with self.app.run_test(size=(110, 36)) as pilot:
            await self.ready()
            self.assertFalse(self.app.sidebar_widget.display)
            for i in range(550):
                self.app.write_output('Line ' + str(i))
            self.assertEqual(len(self.app.output_lines), 500)
            self.assertEqual(self.app.output_lines[0], 'Line 50')
            await pilot.press('ctrl+b')
            self.assertTrue(self.app.sidebar_widget.display)
            await pilot.resize_terminal(48, 18)
            await pilot.pause()
            self.assertFalse(self.app.sidebar_widget.display)
            self.assertLessEqual(self.app.composer.region.bottom, 16)
            await pilot.press('f8')
            await pilot.pause()
            journal = self.app.screen.query_one('#details-log')
            self.assertGreater(journal.scroll_y, 0)
            position = journal.scroll_y
            await pilot.press('pageup')
            await pilot.pause()
            self.assertLess(journal.scroll_y, position)
            await pilot.press('pagedown')
            self.app.write_output('A new detail while reading')
            await pilot.pause()
            self.assertEqual(type(self.app.screen).__name__, 'DetailsScreen')
            await pilot.press('f8')
            self.assertTrue(self.app.composer.has_focus)

    async def test_brief_notice_does_not_erase_newer_notice(self):
        async with self.app.run_test() as pilot:
            await self.ready()
            self.app.write_output('First notice')
            old_timer = self.app._notice_timer
            self.app.write_output('Second notice')
            self.assertIsNot(old_timer, self.app._notice_timer)
            self.assertIn('Second notice', str(self.app.query_one('#notice', Static).render()))
