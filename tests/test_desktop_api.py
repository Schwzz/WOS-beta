"""Desktop management over the real local API, isolated from keys and providers."""
import http.client
import json
import os
import threading
import unittest
from unittest import mock

from tests.helpers import Sandbox
from tests.test_overlay import FakeSpeaker
from core import config, conversation, overlay


class DesktopApiTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = self.enterContext(Sandbox())
        self.enterContext(mock.patch.dict(os.environ, {}, clear=True))
        self.enterContext(mock.patch.object(config, "PROJECT_ROOT", self.sandbox.root))
        self.session = self.sandbox.make_session()
        self.speaker = FakeSpeaker()
        self.backend = overlay.OverlayBackend(lambda: self.session, self.speaker)
        self.server = overlay.OverlayServer(self.backend, port=0,
            endpoint_path=self.sandbox.root / "endpoint.json")
        self.server.start()
        self.addCleanup(self.server.close)

    def request(self, path, data=None, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=3)
        headers = {"X-WOS-Token": self.server.token} if token else {}
        body = json.dumps(data) if data is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request("POST" if data is not None else "GET", path, body, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def action(self, action, target=None, **fields):
        return self.request("/api/chats", {"action": action,
            "session_id": target or self.session.session_id,
            "expected_session_id": self.session.session_id, **fields})

    def test_interpolation_syntax_is_rejected_without_changing_key(self):
        previous = os.environ.get("OPENAI_API_KEY")
        status, result = self.request("/api/credentials", {"provider": "openai", "key": "a${AURALIS_TEST_INTERP}z"})
        self.assertEqual(status, 400)
        self.assertNotIn("a${AURALIS_TEST_INTERP}z", json.dumps(result))
        self.assertEqual(os.environ.get("OPENAI_API_KEY"), previous)

    def test_management_routes_require_existing_token(self):
        for path in ("/api/chats", "/api/characters", "/api/credentials"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, token=False)[0], 401)
                self.assertEqual(self.request(path, {}, token=False)[0], 401)

    def test_lists_saved_chats_and_valid_characters(self):
        status, result = self.request("/api/chats")
        self.assertEqual(status, 200)
        self.assertEqual(result["session_id"], "session_001")
        self.assertEqual([(c["session_id"], c["title"]) for c in result["chats"]],
                         [("session_001", "Chat 1")])
        status, result = self.request("/api/characters")
        self.assertEqual(status, 200)
        self.assertEqual(result, {"characters": [{"id": "nexis", "name": "Nexis"},
            {"id": "tester", "name": "Tester"}], "current": "nexis"})

    def test_new_switch_rename_clear_and_delete_share_terminal_session(self):
        self.assertEqual(self.action("new")[0], 200)
        self.assertEqual(self.session.session_id, "session_002")
        self.assertEqual(self.action("switch", "session_001")[0], 200)
        self.assertEqual(self.session.session_id, "session_001")
        self.assertEqual(self.action("rename", title="A shared chat")[0], 200)
        self.assertEqual(self.session.chat["title"], "A shared chat")
        self.assertEqual(self.sandbox.read_chat("session_001")["title"], "A shared chat")
        self.session.chat["messages"] = [{"role": "user", "content": "Keep until confirmed"}]
        conversation.update_chat(self.session.chat)
        self.assertEqual(self.action("clear")[0], 400)
        self.assertEqual(len(self.session.chat["messages"]), 1)
        self.assertEqual(self.action("clear", confirm=True)[0], 200)
        self.assertEqual(self.session.chat["messages"], [])
        self.assertEqual(self.action("delete")[0], 400)
        self.assertEqual(self.action("delete", confirm=True)[0], 200)
        self.assertEqual(self.session.session_id, "session_002")
        self.assertFalse((self.sandbox.chats_dir / "session_001.json").exists())
        self.assertEqual(conversation.get_active_session(), "session_002")
        self.assertEqual(self.action("delete", confirm=True)[0], 200)
        self.assertTrue((self.sandbox.chats_dir / f"{self.session.session_id}.json").exists())
        self.assertEqual(conversation.get_active_session(), self.session.session_id)

    def test_stale_or_missing_current_chat_never_changes_session(self):
        for expected in ("session_999", None):
            status, result = self.request("/api/chats", {"action": "new", "expected_session_id": expected})
            self.assertEqual(status, 409)
        self.assertEqual(self.session.session_id, "session_001")
        self.assertEqual(self.sandbox.chat_files(), ["session_001.json"])
        self.assertEqual(self.request("/api/character", {"character": "tester", "session_id": "session_999"})[0], 409)
        self.assertEqual(self.session.character_id, "nexis")

    def test_chat_limit_failure_is_actionable_and_keeps_active_chat(self):
        with mock.patch.object(conversation, "MAX_CHATS", 1):
            status, result = self.action("new")
        self.assertEqual(status, 400)
        self.assertIn("Delete a chat", result["error"])
        self.assertEqual(self.session.session_id, "session_001")
        self.assertEqual(conversation.get_active_session(), "session_001")

    def test_failed_clear_and_rename_preserve_in_memory_content(self):
        self.session.chat["messages"] = [{"role": "user", "content": "Keep me"}]
        conversation.update_chat(self.session.chat)
        with mock.patch.object(conversation, "save_chat", side_effect=OSError("private details")):
            status, result = self.action("clear", confirm=True)
            self.assertEqual(status, 500)
            self.assertNotIn("private details", json.dumps(result))
            self.assertEqual(self.session.chat["messages"][0]["content"], "Keep me")
            self.assertEqual(self.action("rename", title="Cannot save")[0], 500)
        self.assertEqual(self.session.chat["title"], "Chat 1")

    def test_character_change_persists_and_failed_change_is_not_success(self):
        status, result = self.request("/api/character", {"character": "tester", "session_id": "session_001"})
        self.assertEqual(status, 200)
        self.assertEqual(result["character"], "Tester")
        self.assertEqual(self.sandbox.read_chat("session_001")["character_id"], "tester")
        with mock.patch.object(conversation, "save_chat", side_effect=OSError("secret details")):
            status, result = self.request("/api/character", {"character": "nexis", "session_id": "session_001"})
        self.assertEqual(status, 500)
        self.assertNotIn("secret details", json.dumps(result))
        self.assertEqual(self.session.character_id, "tester")

    def test_busy_model_reply_rejects_management_without_queuing(self):
        entered, release = threading.Event(), threading.Event()
        def model(*args):
            entered.set()
            release.wait(3)
            return "A reply"
        with mock.patch("core.session.chat", side_effect=model):
            worker = threading.Thread(target=lambda: self.backend.send_message("hello"))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.action("new")[0], 409)
                self.assertEqual(self.request("/api/character", {"character": "tester", "session_id": "session_001"})[0], 409)
            finally:
                release.set()
                worker.join(3)
        self.assertEqual(self.session.session_id, "session_001")

    def test_new_chat_suppresses_pending_terminal_speech(self):
        self.backend._terminal_reply_epoch = self.backend._response_epoch
        self.assertEqual(self.action("new")[0], 200)
        self.backend.speak_terminal_reply("Old chat reply")
        self.assertEqual(self.speaker.spoken, [])

    def test_chat_change_does_not_replay_a_delivered_reply_from_old_chat(self):
        with mock.patch("core.session.chat", return_value="First response"):
            self.backend.send_message("First message", request_id="same-browser-request", session_id="session_001")
        self.assertEqual(self.action("new")[0], 200)
        with mock.patch("core.session.chat", return_value="Second response"):
            reply = self.backend.send_message("First message", request_id="same-browser-request", session_id="session_002")
        self.assertEqual(reply, "Second response")
        self.assertEqual(len(self.sandbox.read_chat("session_002")["messages"]), 2)

    def test_renaming_keeps_duplicate_message_protection_in_current_chat(self):
        with mock.patch("core.session.chat", return_value="Saved response"):
            self.backend.send_message("One message", request_id="delayed-browser-retry", session_id="session_001")
        self.assertEqual(self.action("rename", title="Still the same chat")[0], 200)
        with mock.patch("core.session.chat", return_value="Duplicate response"):
            reply = self.backend.send_message("One message", request_id="delayed-browser-retry", session_id="session_001")
        self.assertEqual(reply, "Saved response")
        self.assertEqual(len(self.sandbox.read_chat("session_001")["messages"]), 2)

    def test_failed_switch_does_not_claim_success_or_change_active_chat(self):
        self.action("new")
        (self.sandbox.characters_dir / "nexis.json").unlink()
        self.assertEqual(self.action("switch", "session_001")[0], 400)
        self.assertEqual(self.session.session_id, "session_002")
        self.assertEqual(conversation.get_active_session(), "session_002")

    def test_failed_delete_replacement_restores_the_last_saved_chat(self):
        with mock.patch.object(self.session, "new_chat", side_effect=OSError("private details")):
            status, result = self.action("delete", confirm=True)
        self.assertEqual(status, 500)
        self.assertNotIn("private details", json.dumps(result))
        self.assertEqual(self.sandbox.read_chat("session_001")["title"], "Chat 1")
        self.assertEqual(conversation.get_active_session(), "session_001")
        self.assertEqual(self.session.session_id, "session_001")

    def test_credentials_save_preserves_comments_and_never_echoes_key(self):
        path = self.sandbox.root / ".env"
        path.write_text("# Local settings\nOTHER=keep\nOPENAI_API_KEY=old # Keep this comment\n", "utf-8")
        key = "test-only-not-a-real-credential"
        status, result = self.request("/api/credentials", {"provider": "openai", "key": key})
        self.assertEqual(status, 200)
        self.assertNotIn(key, json.dumps(result))
        self.assertEqual(os.environ["OPENAI_API_KEY"], key)
        saved = path.read_text("utf-8")
        self.assertIn("# Local settings\nOTHER=keep\n", saved)
        self.assertIn("# Keep this comment", saved)
        from dotenv import dotenv_values
        self.assertEqual(dotenv_values(path)["OPENAI_API_KEY"], key)
        status, result = self.request("/api/credentials")
        self.assertEqual(status, 200)
        self.assertTrue(next(p for p in result["providers"] if p["id"] == "openai")["configured"])
        self.assertFalse(result["voice"]["configured"])
        self.assertNotIn(key, json.dumps(result))
        status, result = self.request("/api/credentials", {"provider": "fish_audio", "key": key})
        self.assertEqual(status, 200)
        self.assertTrue(result["voice"]["configured"])

    def test_bad_credentials_and_failed_atomic_save_leave_environment_unchanged(self):
        for provider, key in (("unknown", "test"), ("ollama", "test"), ("openai", "bad\nINJECT=yes"),
                              ("openai", "bad\rINJECT=yes"), ("openai", "x" * 9000), ("openai", "")):
            self.assertEqual(self.request("/api/credentials", {"provider": provider, "key": key})[0], 400)
        os.environ["OPENAI_API_KEY"] = "previous-test-value"
        path = self.sandbox.root / ".env"
        path.write_text("OPENAI_API_KEY=previous-test-value\n", "utf-8")
        with mock.patch("os.replace", side_effect=OSError("private-key-test-value")):
            status, result = self.request("/api/credentials", {"provider": "openai", "key": "private-key-test-value"})
        self.assertEqual(status, 500)
        self.assertNotIn("private-key-test-value", json.dumps(result))
        self.assertEqual(os.environ["OPENAI_API_KEY"], "previous-test-value")
        self.assertEqual(path.read_text("utf-8"), "OPENAI_API_KEY=previous-test-value\n")


if __name__ == "__main__":
    unittest.main()
