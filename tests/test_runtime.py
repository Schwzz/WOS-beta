"""Shared application lifetime, without real provider requests or native windows."""
import unittest
import threading
from unittest import mock
from tests.helpers import Sandbox


class RuntimeTests(unittest.TestCase):
    def test_closed_runtime_rejects_queued_model_and_voice_mutations(self):
        from core.runtime import ApplicationRuntime
        from core.overlay import BackendError
        with Sandbox(), mock.patch('core.runtime.FishAudioSpeaker'):
            runtime = ApplicationRuntime()
            model = runtime.session.model
            runtime.close()
            with self.assertRaises(BackendError):
                runtime.backend.set_model('another-model')
            with self.assertRaises(BackendError):
                runtime.backend.set_voice(True)
            self.assertEqual(runtime.session.model, model)

    def test_shutdown_between_generation_and_commit_does_not_overwrite_reopened_chat(self):
        from core.runtime import ApplicationRuntime
        from core import conversation
        from core.overlay import BackendError
        entered, resume = threading.Event(), threading.Event()
        with Sandbox(), mock.patch('core.runtime.FishAudioSpeaker'), mock.patch('core.session.chat', return_value='reply'):
            first = ApplicationRuntime()
            original = first.session._record_exchange
            def delayed(*args):
                entered.set()
                resume.wait(5)
                return original(*args)
            first.session._record_exchange = delayed
            def send():
                try: first.backend.send_message('old message')
                except BackendError: pass
            worker = threading.Thread(target=send)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                first.close()
                # A closed UI may retain a worker, but must retain file ownership
                # until it leaves the shared mutation lock.
                self.assertRaisesRegex(RuntimeError, 'already running', ApplicationRuntime)
            finally:
                resume.set(); worker.join(3); first.close()
            self.assertFalse(worker.is_alive())
            self.assertTrue(first._ownership_released.wait(2))
            second = ApplicationRuntime()
            try:
                second.backend.send_message('new message')
                saved = conversation.load_chat(second.session.session_id)['messages']
                self.assertEqual([m['content'] for m in saved], ['new message', 'reply'])
            finally:
                second.close()

    def test_second_runtime_is_rejected_and_ownership_released_on_close(self):
        from core.runtime import ApplicationRuntime
        with Sandbox(), mock.patch('core.runtime.FishAudioSpeaker'):
            first = ApplicationRuntime()
            self.addCleanup(first.close)
            self.assertRaisesRegex(RuntimeError, 'already running', ApplicationRuntime)
            first.close()
            second = ApplicationRuntime()
            second.close()

    def test_reply_finishing_after_close_is_not_saved_or_spoken(self):
        from core.runtime import ApplicationRuntime
        from core.overlay import BackendError
        entered, resume = threading.Event(), threading.Event()
        def generate(*_):
            entered.set()
            resume.wait(5)
            return 'Late reply'
        with Sandbox(), mock.patch('core.runtime.FishAudioSpeaker') as speaker, mock.patch('core.session.chat', side_effect=generate):
            runtime = ApplicationRuntime()
            def send():
                try:
                    runtime.backend.send_message('Hello there')
                except BackendError:
                    pass
            worker = threading.Thread(target=send)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                runtime.close()
            finally:
                resume.set()
                worker.join(3)
                runtime.close()
            self.assertFalse(worker.is_alive())
            self.assertEqual(runtime.session.chat['messages'], [])
            speaker.return_value.speak.assert_not_called()

    def test_runtime_owns_session_and_server_and_closes_once(self):
        from core.runtime import ApplicationRuntime
        with Sandbox():
            with mock.patch('core.runtime.FishAudioSpeaker') as factory, mock.patch('core.runtime.OverlayServer') as servers:
                runtime = ApplicationRuntime()
                self.assertIs(runtime.backend._get_session(), runtime.session)
                self.assertIs(factory.return_value.listener.__self__, runtime.backend)
                self.assertIs(runtime.start_server(), servers.return_value)
                self.assertIs(runtime.start_server(), servers.return_value)
                servers.return_value.start.assert_called_once()
                runtime.close()
                runtime.close()
                factory.return_value.shutdown.assert_called_once()
                servers.return_value.close.assert_called_once()
                self.assertRaises(RuntimeError, runtime.start_server)

    def test_failed_server_start_closes_partial_server(self):
        from core.runtime import ApplicationRuntime
        with Sandbox(), mock.patch('core.runtime.FishAudioSpeaker'), mock.patch('core.runtime.OverlayServer') as servers:
            runtime = ApplicationRuntime()
            try:
                servers.return_value.start.side_effect = OSError('port busy')
                self.assertRaises(OSError, runtime.start_server)
                servers.return_value.close.assert_called_once()
                self.assertIsNone(runtime.server)
            finally:
                # Windows cannot remove the Sandbox while runtime.lock is open.
                runtime.close()
            self.assertTrue(runtime._ownership_released.is_set())

    def test_close_suppresses_inflight_reply_without_waiting_for_session_lock(self):
        from core.runtime import ApplicationRuntime
        with Sandbox(), mock.patch('core.runtime.FishAudioSpeaker') as factory:
            runtime = ApplicationRuntime()
            before = runtime.backend._response_epoch
            runtime.close()
            self.assertGreater(runtime.backend._response_epoch, before)
            factory.return_value.shutdown.assert_called_once()
