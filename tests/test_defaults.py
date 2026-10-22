import json
import unittest
from unittest import mock

from core import config

from tests.helpers import Sandbox

from core import conversation


class SaveDefaultsValidationTests(unittest.TestCase):
    def test_placeholder_model_is_rejected_and_config_is_untouched(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            before = sandbox.config_path.read_bytes()

            for bad in ("<model_name>", "<name>", "gemini 3 flash",
                        "../../etc", "model_name", "x" * 500):
                with self.subTest(model=bad[:30]):
                    result = session.save_defaults(model=bad)

                    self.assertNotEqual(result, "Default settings saved.")
                    self.assertEqual(sandbox.config_path.read_bytes(), before)
                    self.assertEqual(
                        session.default_model, "gemini-3.5-flash-lite"
                    )

    def test_empty_model_is_rejected(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()

            self.assertEqual(
                session.save_defaults(model="   "),
                "Model name cannot be empty.",
            )

    def test_unknown_provider_is_rejected_and_lists_valid_ones(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            before = sandbox.config_path.read_bytes()

            result = session.save_defaults(provider="skynet")

            self.assertIn("Unsupported provider", result)
            self.assertIn(", ".join(sorted(config.SUPPORTED_PROVIDERS)), result)
            self.assertEqual(sandbox.config_path.read_bytes(), before)

    def test_nonexistent_character_is_rejected(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            before = sandbox.config_path.read_bytes()

            for bad in ("ghost", "../nexis", "", "   "):
                with self.subTest(character=bad):
                    result = session.save_defaults(character=bad)

                    self.assertNotEqual(result, "Default settings saved.")
                    self.assertEqual(sandbox.config_path.read_bytes(), before)
                    self.assertEqual(session.default_character_id, "nexis")

    def test_valid_character_and_model_are_saved(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()

            self.assertEqual(
                session.save_defaults(model="gemini-x", character="tester"),
                "Default settings saved.",
            )
            self.assertEqual(sandbox.read_config()["model"], "gemini-x")
            self.assertEqual(sandbox.read_config()["character"], "tester")
            self.assertEqual(session.default_character_id, "tester")

    def test_failed_write_leaves_session_and_file_unchanged(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            before = sandbox.config_path.read_bytes()

            with mock.patch(
                "core.session.save_config", side_effect=OSError("disk full")
            ):
                result = session.save_defaults(model="gemini-x")

            self.assertIn("Couldn't save default settings", result)
            self.assertEqual(sandbox.config_path.read_bytes(), before)
            self.assertEqual(session.default_model, "gemini-3.5-flash-lite")


class DefaultsProviderModelTests(unittest.TestCase):
    def test_provider_without_known_model_is_refused_not_mismatched(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            before = sandbox.config_path.read_bytes()

            result = session.save_defaults(provider="ollama")

            self.assertIn("needs a model", result)
            self.assertEqual(sandbox.config_path.read_bytes(), before)

    def test_provider_and_model_are_saved_together(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()

            self.assertEqual(
                session.save_defaults(provider="ollama", model="llama3"),
                "Default settings saved.",
            )
            self.assertEqual(sandbox.read_config()["provider"], "ollama")
            self.assertEqual(sandbox.read_config()["model"], "llama3")

    def test_returning_to_a_provider_restores_a_compatible_model(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            session.save_defaults(provider="ollama", model="llama3")

            # The cloud provider has a built-in default model.
            session.save_defaults(provider="cloud")
            self.assertEqual(
                sandbox.read_config()["model"], "gemini-3.5-flash-lite"
            )

            # Ollama remembers the model chosen earlier in this run.
            session.save_defaults(provider="ollama")
            self.assertEqual(sandbox.read_config()["model"], "llama3")

    def test_invalid_model_with_provider_saves_nothing(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            before = sandbox.config_path.read_bytes()

            result = session.save_defaults(
                provider="ollama", model="<model_name>"
            )

            self.assertIn("placeholder", result)
            self.assertEqual(sandbox.config_path.read_bytes(), before)


class NewChatUsesDefaultsTests(unittest.TestCase):
    def test_new_chat_uses_configured_defaults_not_current_chat(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            first_id = session.session_id

            session.save_defaults(
                provider="ollama", model="llama3", character="tester"
            )
            notes = session.new_chat()

            self.assertEqual(notes, [])
            self.assertNotEqual(session.session_id, first_id)
            self.assertEqual(
                (session.provider, session.model, session.character_id),
                ("ollama", "llama3", "tester"),
            )
            self.assertEqual(session.character_name, "Tester")

            saved = sandbox.read_chat(session.session_id)
            self.assertEqual(saved["provider"], "ollama")
            self.assertEqual(saved["model"], "llama3")
            self.assertEqual(saved["character_id"], "tester")

    def test_messages_in_the_new_chat_go_to_the_default_provider(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            session.save_defaults(provider="ollama", model="llama3")
            session.new_chat()

            with mock.patch(
                "core.session.chat", return_value="hi there"
            ) as fake_chat:
                reply = session.send_message("hello")

            self.assertEqual(reply, "hi there")
            args = fake_chat.call_args[0]
            self.assertEqual(args[0], "ollama")
            self.assertEqual(args[1], "llama3")

    def test_changing_defaults_does_not_change_existing_chats(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            first_id = session.session_id

            session.save_defaults(
                provider="ollama", model="llama3", character="tester"
            )

            self.assertEqual(session.provider, "cloud")
            self.assertEqual(session.model, "gemini-3.5-flash-lite")
            saved = sandbox.read_chat(first_id)
            self.assertEqual(saved["provider"], "cloud")
            self.assertEqual(saved["model"], "gemini-3.5-flash-lite")
            self.assertEqual(saved["character_id"], "nexis")

    def test_resumed_and_switched_chats_keep_their_own_settings(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            first_id = session.session_id
            session.change_provider("ollama", "llama3")

            session.save_defaults(provider="cloud", model="gemini-x")
            session.new_chat()
            self.assertEqual(session.model, "gemini-x")

            # Switching back restores the first chat's own provider/model.
            self.assertTrue(session.switch_chat(first_id))
            self.assertEqual((session.provider, session.model),
                             ("ollama", "llama3"))

            # Starting WOS again resumes the active chat, not the defaults.
            resumed = sandbox.make_session()
            self.assertEqual(resumed.session_id, first_id)
            self.assertEqual((resumed.provider, resumed.model),
                             ("ollama", "llama3"))

    def test_missing_default_character_falls_back_with_a_note(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            session.save_defaults(character="tester")
            (sandbox.characters_dir / "tester.json").unlink()

            notes = session.new_chat()

            self.assertEqual(len(notes), 1)
            self.assertIn("tester", notes[0])
            self.assertEqual(session.character_id, "nexis")

    def test_incomplete_chat_settings_fall_back_to_defaults_as_a_pair(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            session.new_chat()
            second_id = session.session_id

            # A legacy chat with a provider but no model.
            chat_file = sandbox.chats_dir / f"{second_id}.json"
            data = json.loads(chat_file.read_text("utf-8"))
            data["provider"] = "ollama"
            data.pop("model")
            chat_file.write_text(json.dumps(data), encoding="utf-8")

            self.assertTrue(session.switch_chat(second_id))

            self.assertEqual(
                (session.provider, session.model),
                ("cloud", "gemini-3.5-flash-lite"),
            )

    def test_chat_with_an_invalid_saved_model_uses_defaults(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            session.new_chat()
            second_id = session.session_id

            chat_file = sandbox.chats_dir / f"{second_id}.json"
            data = json.loads(chat_file.read_text("utf-8"))
            data["model"] = "<model_name>"
            chat_file.write_text(json.dumps(data), encoding="utf-8")

            self.assertTrue(session.switch_chat(second_id))

            self.assertEqual(session.model, "gemini-3.5-flash-lite")
            self.assertEqual(session.chat["model"], "gemini-3.5-flash-lite")


class ChangeProviderAndModelTests(unittest.TestCase):
    def test_provider_without_known_model_is_refused_and_unchanged(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()

            result = session.change_provider("ollama")

            self.assertIn("needs a model", result)
            self.assertEqual(session.provider, "cloud")
            self.assertEqual(session.model, "gemini-3.5-flash-lite")
            self.assertEqual(
                sandbox.read_chat(session.session_id)["provider"], "cloud"
            )

    def test_provider_with_model_changes_both_and_saves_them(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()

            result = session.change_provider("OLLAMA", "llama3")

            self.assertIn("Provider changed to ollama", result)
            self.assertEqual((session.provider, session.model),
                             ("ollama", "llama3"))
            saved = sandbox.read_chat(session.session_id)
            self.assertEqual((saved["provider"], saved["model"]),
                             ("ollama", "llama3"))

    def test_switching_back_selects_a_compatible_model_automatically(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()
            session.change_provider("ollama", "llama3")

            session.change_provider("cloud")
            self.assertEqual(session.model, "gemini-3.5-flash-lite")

            session.change_provider("ollama")
            self.assertEqual(session.model, "llama3")

    def test_unknown_provider_lists_the_valid_ones(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()

            result = session.change_provider("skynet")

            self.assertIn("Unknown provider", result)
            self.assertIn(", ".join(sorted(config.SUPPORTED_PROVIDERS)), result)
            self.assertEqual(session.provider, "cloud")

    def test_provider_with_invalid_model_changes_nothing(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()

            result = session.change_provider("ollama", "<model_name>")

            self.assertIn("placeholder", result)
            self.assertEqual(session.provider, "cloud")
            self.assertEqual(session.model, "gemini-3.5-flash-lite")

    def test_failed_save_rolls_back_provider_and_model(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()

            with mock.patch(
                "core.session.update_chat", side_effect=OSError("disk full")
            ):
                result = session.change_provider("ollama", "llama3")

            self.assertIn("Couldn't save", result)
            self.assertEqual(session.provider, "cloud")
            self.assertEqual(session.model, "gemini-3.5-flash-lite")
            self.assertEqual(session.chat["provider"], "cloud")
            self.assertEqual(session.chat["model"], "gemini-3.5-flash-lite")

    def test_change_model_rejects_placeholders(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()

            for bad in ("<model_name>", "gemini 3", "../x", "   "):
                with self.subTest(model=bad):
                    result = session.change_model(bad)

                    self.assertNotIn("Model changed", result)
                    self.assertEqual(session.model, "gemini-3.5-flash-lite")
                    self.assertEqual(
                        sandbox.read_chat(session.session_id)["model"],
                        "gemini-3.5-flash-lite",
                    )

    def test_change_model_accepts_a_real_name(self):
        with Sandbox() as sandbox:
            session = sandbox.make_session()

            self.assertEqual(
                session.change_model("gemini-x"), "Model changed to gemini-x."
            )
            self.assertEqual(
                sandbox.read_chat(session.session_id)["model"], "gemini-x"
            )


class InitializeActiveChatTests(unittest.TestCase):
    def test_missing_provider_or_model_is_filled_in_as_a_pair(self):
        with Sandbox() as sandbox:
            chat = conversation.create_chat()
            chat["provider"] = "ollama"
            conversation.save_chat(chat)

            result = conversation.initialize_active_chat(
                provider="cloud", model="gemini-x", character_id="nexis"
            )

            self.assertEqual(
                (result["provider"], result["model"]), ("cloud", "gemini-x")
            )

    def test_complete_chat_settings_are_never_overwritten(self):
        with Sandbox() as sandbox:
            conversation.create_chat(
                provider="ollama", model="llama3", character_id="tester"
            )

            result = conversation.initialize_active_chat(
                provider="cloud", model="gemini-x", character_id="nexis"
            )

            self.assertEqual(
                (result["provider"], result["model"], result["character_id"]),
                ("ollama", "llama3", "tester"),
            )


if __name__ == "__main__":
    unittest.main()
