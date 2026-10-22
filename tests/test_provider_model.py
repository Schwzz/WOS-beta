"""End-to-end tests for the /provider and /model menus.

WOS runs as a real subprocess. A fake `requests` answers Ollama's model list
(see FAKE_REQUESTS in test_start_integration.py), so these tests never depend
on a real Ollama being installed or running.
"""
import json
import unittest

from tests.test_start_integration import IntegrationTestCase

INSTALLED = ["qwen2.5-coder:7b", "llama3:latest", "gemma3:4b"]


def lines(*commands):
    return "\n".join(commands) + "\n"


class ProviderModelTestCase(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.config_before = self.wos.config_path.read_bytes()

    def chat_settings(self, session_id="session_001"):
        chat = self.wos.read_chat(session_id)

        return chat["provider"], chat["model"]

    def assertDefaultsUntouched(self):
        """/provider and /model never change the defaults for new chats."""
        self.assertEqual(self.wos.config_path.read_bytes(), self.config_before)

    def flat(self, result):
        return " ".join(result.stdout.split())


# ------------------------------------------------------------------
# /provider menu
# ------------------------------------------------------------------

class ProviderMenuTests(ProviderModelTestCase):
    def test_menu_shows_the_current_settings_and_every_provider(self):
        result = self.wos.run(lines("/provider", "0", "/exit"))

        self.assertNoCrash(result)
        for text in ("Current provider: cloud",
                     "Current model: gemini-3.5-flash-lite",
                     "Providers",
                     "1. cloud (current)",
                     "2. ollama",
                     "(Enter or 0 to cancel)"):
            with self.subTest(text=text):
                self.assertIn(text, result.stdout)

    def test_enter_and_zero_cancel_without_changing_anything(self):
        for answer in ("", "0"):
            with self.subTest(answer=repr(answer)):
                result = self.wos.run(lines("/provider", answer, "/exit"))

                self.assertNoCrash(result)
                self.assertIn("Provider was not changed", result.stdout)
                self.assertEqual(self.chat_settings(),
                                 ("cloud", "gemini-3.5-flash-lite"))

        self.assertDefaultsUntouched()

    def test_eof_at_the_provider_menu_changes_nothing(self):
        result = self.wos.run("/provider\n")

        self.assertNoCrash(result)
        self.assertIn("Provider was not changed", result.stdout)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_ctrl_c_at_the_provider_menu_changes_nothing_and_wos_continues(self):
        result = self.wos.run_scripted(
            ["/provider", "<CTRL-C>", "/status", "/exit"]
        )

        self.assertNoCrash(result)
        self.assertIn("Provider was not changed", result.stdout)
        self.assertIn("WOS Status", result.stdout)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_invalid_choices_ask_again(self):
        result = self.wos.run(lines("/provider", "9", "abc", "-1", "0", "/exit"))

        self.assertNoCrash(result)
        self.assertEqual(
            result.stdout.count("Please enter a number from 1 to 7."), 3
        )
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_choosing_ollama_lists_the_models_actually_installed(self):
        self.wos.ollama = INSTALLED

        result = self.wos.run(lines("/provider", "2", "0", "/exit"))

        self.assertNoCrash(result)
        self.assertIn("Models for ollama:", result.stdout)
        self.assertIn("These are installed on this computer.", result.stdout)
        self.assertIn("Available Models", result.stdout)
        # Sorted, and exactly the installed models.
        self.assertIn("1. gemma3:4b\n2. llama3:latest\n3. qwen2.5-coder:7b\n",
                      result.stdout)
        self.assertNotIn("Other (type a model name)", result.stdout)

    def test_picking_a_provider_and_model_changes_both_for_this_chat(self):
        self.wos.ollama = INSTALLED

        result = self.wos.run(lines("/provider", "2", "2", "/status", "/exit"))

        self.assertNoCrash(result)
        self.assertIn(
            "Provider changed to ollama. Current model: llama3:latest.",
            result.stdout,
        )
        self.assertRegex(result.stdout, r"Provider:\s+ollama\nModel:\s+llama3:latest")
        self.assertEqual(self.chat_settings(), ("ollama", "llama3:latest"))
        self.assertDefaultsUntouched()

    def test_success_says_it_affects_the_current_chat_not_new_chats(self):
        self.wos.ollama = INSTALLED

        result = self.wos.run(lines("/provider", "2", "1", "/exit"))

        self.assertIn(
            "This applies to the current chat (session_001) only.",
            result.stdout,
        )
        self.assertIn(
            "New chats use your defaults (cloud / gemini-3.5-flash-lite). "
            "Change those with /defaults.",
            result.stdout,
        )

    def test_new_chats_still_use_the_defaults_afterwards(self):
        self.wos.ollama = INSTALLED

        result = self.wos.run(
            lines("/provider", "2", "1", "/new", "/status", "/exit")
        )

        self.assertNoCrash(result)
        self.assertEqual(self.chat_settings("session_001"),
                         ("ollama", "gemma3:4b"))
        self.assertEqual(self.chat_settings("session_002"),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_cancelling_the_model_menu_leaves_the_provider_unchanged(self):
        self.wos.ollama = INSTALLED

        for answer in ("", "0"):
            with self.subTest(answer=repr(answer)):
                result = self.wos.run(lines("/provider", "2", answer, "/exit"))

                self.assertNoCrash(result)
                self.assertIn("Provider was not changed", result.stdout)
                self.assertNotIn("Provider changed to", result.stdout)
                self.assertEqual(self.chat_settings(),
                                 ("cloud", "gemini-3.5-flash-lite"))

    def test_eof_and_ctrl_c_at_the_model_menu_leave_the_provider_unchanged(self):
        self.wos.ollama = INSTALLED

        eof = self.wos.run("/provider\n2\n")
        ctrl_c = self.wos.run_scripted(
            ["/provider", "2", "<CTRL-C>", "/status", "/exit"]
        )

        self.assertNoCrash(eof)
        self.assertNoCrash(ctrl_c)
        self.assertIn("Provider was not changed", eof.stdout)
        self.assertIn("Provider was not changed", ctrl_c.stdout)
        self.assertIn("WOS Status", ctrl_c.stdout)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_invalid_model_choices_ask_again(self):
        self.wos.ollama = INSTALLED

        result = self.wos.run(
            lines("/provider", "2", "9", "x", "1", "/exit")
        )

        self.assertNoCrash(result)
        self.assertEqual(
            result.stdout.count("Please enter a number from 1 to 3."), 2
        )
        self.assertEqual(self.chat_settings(), ("ollama", "gemma3:4b"))

    def test_choosing_the_current_provider_still_offers_its_models(self):
        result = self.wos.run(lines("/provider", "1", "1", "/exit"))

        self.assertNoCrash(result)
        self.assertIn("Models for cloud:", result.stdout)
        self.assertIn(
            "1. gemini-3.5-flash-lite (current, default for new chats)",
            result.stdout,
        )
        self.assertIn("Already using cloud with model "
                      "gemini-3.5-flash-lite. Nothing was changed.",
                      result.stdout)
        self.assertNotIn("This applies to", result.stdout)

    def test_switching_back_to_cloud_picks_a_cloud_model(self):
        self.wos.run(lines("/provider ollama llama3", "/exit"))

        result = self.wos.run(lines("/provider", "1", "1", "/exit"))

        self.assertNoCrash(result)
        self.assertIn(
            "Provider changed to cloud. Current model: gemini-3.5-flash-lite.",
            result.stdout,
        )
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))


class ProviderOtherModelTests(ProviderModelTestCase):
    """The cloud menu ends with an Other choice for typing a model name."""

    def test_cloud_menu_offers_known_names_plus_other_and_invents_none(self):
        result = self.wos.run(lines("/provider", "1", "0", "/exit"))

        self.assertNoCrash(result)
        self.assertIn("2. Other (type a model name)", result.stdout)
        self.assertIn("model names WOS already knows", result.stdout)
        self.assertNotIn("3. ", result.stdout.split("Available Models")[1]
                         .split("WOS: Number of the model")[0])

    def test_typed_model_is_validated_until_it_is_acceptable(self):
        result = self.wos.run(lines(
            "/provider", "1", "2", "has a space", "<model_name>", "../etc",
            "my-model-id", "/exit",
        ))

        self.assertNoCrash(result)
        self.assertIn("Model names cannot contain spaces.", result.stdout)
        self.assertIn("looks like a placeholder", result.stdout)
        self.assertIn("is not a valid model name", result.stdout)
        self.assertIn(
            "Provider changed to cloud. Current model: my-model-id.",
            result.stdout,
        )
        self.assertEqual(self.chat_settings(), ("cloud", "my-model-id"))

    def test_cancelling_the_name_prompt_changes_nothing(self):
        for answer in ("", "   "):
            with self.subTest(answer=repr(answer)):
                result = self.wos.run(lines("/provider", "1", "2", answer, "/exit"))

                self.assertNoCrash(result)
                self.assertIn("Provider was not changed", result.stdout)
                self.assertEqual(self.chat_settings(),
                                 ("cloud", "gemini-3.5-flash-lite"))

    def test_eof_and_ctrl_c_at_the_name_prompt_change_nothing(self):
        eof = self.wos.run("/provider\n1\n2\n")
        ctrl_c = self.wos.run_scripted(
            ["/provider", "1", "2", "<CTRL-C>", "/status", "/exit"]
        )

        self.assertNoCrash(eof)
        self.assertNoCrash(ctrl_c)
        self.assertIn("WOS Status", ctrl_c.stdout)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))


# ------------------------------------------------------------------
# Ollama is not available
# ------------------------------------------------------------------

class OllamaUnavailableTests(ProviderModelTestCase):
    def test_not_running_explains_and_lets_the_user_type_a_name(self):
        self.wos.ollama = "down"

        result = self.wos.run(
            # 2 (Ollama), then 2 = "type a model name myself"
            lines("/provider", "2", "2", "<model_name>", "llama3", "/exit")
        )

        self.assertNoCrash(result)
        self.assertIn(
            "Could not get the list of installed models: Ollama is not running.",
            result.stdout,
        )
        self.assertIn("cannot be checked until Ollama is running",
                      self.flat(result))
        self.assertIn("looks like a placeholder", result.stdout)
        self.assertEqual(self.chat_settings(), ("ollama", "llama3"))

    def test_each_kind_of_failure_is_explained_and_nothing_is_changed(self):
        reasons = {
            "down": "Ollama is not running.",
            "timeout": "Ollama took too long to answer.",
            "garbage": "Ollama sent an answer WOS could not read.",
            "error": "Ollama returned an error",
        }

        for mode, reason in reasons.items():
            with self.subTest(mode=mode):
                self.wos.ollama = mode

                result = self.wos.run(lines("/provider", "2", "", "/exit"))

                self.assertNoCrash(result)
                self.assertIn(reason, result.stdout)
                self.assertIn("Provider was not changed", result.stdout)
                self.assertEqual(self.chat_settings(),
                                 ("cloud", "gemini-3.5-flash-lite"))

    def test_no_models_installed_says_how_to_install_one(self):
        self.wos.ollama = []

        result = self.wos.run(lines("/provider", "2", "/exit"))

        self.assertNoCrash(result)
        self.assertIn("Ollama has no models installed yet.", result.stdout)
        self.assertIn("ollama pull <model>", result.stdout)
        self.assertIn("Provider was not changed", result.stdout)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))


# ------------------------------------------------------------------
# Direct commands keep working
# ------------------------------------------------------------------

class DirectProviderCommandTests(ProviderModelTestCase):
    def test_provider_and_installed_model_switch_without_a_menu(self):
        self.wos.ollama = ["llama3:latest"]

        result = self.wos.run(lines("/provider ollama llama3", "/exit"))

        self.assertNoCrash(result)
        self.assertNotIn("Available Models", result.stdout)
        self.assertIn("Provider changed to ollama. Current model: llama3.",
                      result.stdout)
        self.assertIn("This applies to the current chat", result.stdout)
        self.assertEqual(self.chat_settings(), ("ollama", "llama3"))
        self.assertDefaultsUntouched()

    def test_a_model_that_is_not_installed_is_rejected_with_help(self):
        self.wos.ollama = ["mistral:latest", "gemma3:4b"]

        result = self.wos.run(lines("/provider ollama llama3", "/exit"))

        self.assertNoCrash(result)
        self.assertIn("Model 'llama3' is not installed in Ollama.",
                      result.stdout)
        self.assertIn("Installed models: gemma3:4b, mistral:latest.",
                      result.stdout)
        self.assertIn("ollama pull llama3", result.stdout)
        self.assertIn("Provider was not changed", result.stdout)
        self.assertNotIn("This applies to", result.stdout)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_a_near_miss_suggests_the_installed_name(self):
        self.wos.ollama = ["llama3:latest"]

        result = self.wos.run(lines("/provider ollama lama3", "/exit"))

        self.assertIn("Did you mean 'llama3:latest'?", result.stdout)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_when_ollama_cannot_be_asked_the_model_is_accepted_with_a_warning(self):
        self.wos.ollama = "down"

        result = self.wos.run(lines("/provider ollama llama3", "/exit"))

        self.assertNoCrash(result)
        self.assertIn("Could not check which models Ollama has installed",
                      result.stdout)
        self.assertIn("'llama3' was not verified", result.stdout)
        self.assertEqual(self.chat_settings(), ("ollama", "llama3"))

    def test_provider_without_a_model_opens_the_model_menu(self):
        self.wos.ollama = INSTALLED

        result = self.wos.run(lines("/provider ollama", "1", "/exit"))

        self.assertNoCrash(result)
        self.assertIn("Available Models", result.stdout)
        self.assertEqual(self.chat_settings(), ("ollama", "gemma3:4b"))

    def test_cancelling_that_menu_changes_nothing(self):
        self.wos.ollama = INSTALLED

        result = self.wos.run(lines("/provider ollama", "0", "/exit"))

        self.assertIn("Provider was not changed", result.stdout)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_a_remembered_model_is_reused_without_a_menu(self):
        self.wos.ollama = ["llama3:latest"]

        result = self.wos.run(lines(
            "/provider ollama llama3", "/provider cloud", "/provider ollama",
            "/exit",
        ))

        self.assertNoCrash(result)
        self.assertNotIn("Available Models", result.stdout)
        self.assertEqual(self.chat_settings(), ("ollama", "llama3"))

    def test_a_remembered_model_that_is_gone_falls_back_to_the_menu(self):
        self.wos.ollama = "down"
        self.wos.run(lines("/provider ollama llama3", "/exit"))
        self.wos.ollama = ["mistral:latest"]  # llama3 has since been removed

        result = self.wos.run(lines(
            "/provider cloud", "/provider ollama", "1", "/exit",
        ))

        self.assertNoCrash(result)
        self.assertIn("Model 'llama3' is not installed in Ollama.",
                      result.stdout)
        self.assertIn("1. mistral:latest", result.stdout)
        self.assertEqual(self.chat_settings(), ("ollama", "mistral:latest"))

    def test_cloud_provider_with_a_known_model_switches_directly(self):
        self.wos.run(lines("/provider ollama llama3", "/exit"))

        result = self.wos.run(lines("/provider cloud", "/exit"))

        self.assertNoCrash(result)
        self.assertNotIn("Available Models", result.stdout)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_unknown_provider_and_too_many_arguments(self):
        result = self.wos.run(lines(
            "/provider skynet", "/provider ollama llama3 extra", "/exit"
        ))

        self.assertNoCrash(result)
        self.assertIn("Unknown provider 'skynet'", result.stdout)
        self.assertIn("openrouter", result.stdout)
        self.assertIn("ollama", result.stdout)
        self.assertIn("Usage: /provider [provider] [model]", result.stdout)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_a_malformed_direct_model_is_explained_and_nothing_changes(self):
        result = self.wos.run(lines("/provider ollama <model_name>", "/exit"))

        self.assertNoCrash(result)
        self.assertIn("looks like a placeholder", result.stdout)
        self.assertNotIn("This applies to", result.stdout)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))


# ------------------------------------------------------------------
# /model menu
# ------------------------------------------------------------------

class ModelMenuTests(ProviderModelTestCase):
    def test_cloud_menu_shows_current_settings_known_models_and_other(self):
        result = self.wos.run(lines("/model", "0", "/exit"))

        self.assertNoCrash(result)
        for text in ("Current provider: cloud",
                     "Current model: gemini-3.5-flash-lite",
                     "Models for cloud:",
                     "Available Models",
                     "1. gemini-3.5-flash-lite (current, default for new chats)",
                     "2. Other (type a model name)"):
            with self.subTest(text=text):
                self.assertIn(text, result.stdout)

    def test_other_changes_the_model_and_says_it_affects_this_chat_only(self):
        result = self.wos.run(lines("/model", "2", "my-model-id", "/exit"))

        self.assertNoCrash(result)
        self.assertIn("Model changed to my-model-id.", result.stdout)
        self.assertIn("This applies to the current chat (session_001) only.",
                      result.stdout)
        self.assertEqual(self.chat_settings(), ("cloud", "my-model-id"))
        self.assertDefaultsUntouched()

    def test_a_typed_model_is_offered_next_time(self):
        self.wos.run(lines("/model", "2", "my-model-id", "/exit"))

        result = self.wos.run(lines("/model", "0", "/exit"))

        self.assertIn("1. my-model-id (current)", result.stdout)
        self.assertIn("2. gemini-3.5-flash-lite (default for new chats)",
                      result.stdout)
        self.assertIn("3. Other (type a model name)", result.stdout)

    def test_choosing_a_listed_model(self):
        self.wos.run(lines("/model", "2", "my-model-id", "/exit"))

        result = self.wos.run(lines("/model", "2", "/exit"))

        self.assertNoCrash(result)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_choosing_the_current_model_changes_nothing(self):
        result = self.wos.run(lines("/model", "1", "/exit"))

        self.assertIn("Already using model gemini-3.5-flash-lite. "
                      "Nothing was changed.", result.stdout)
        self.assertNotIn("This applies to", result.stdout)

    def test_enter_and_zero_cancel(self):
        for answer in ("", "0"):
            with self.subTest(answer=repr(answer)):
                result = self.wos.run(lines("/model", answer, "/exit"))

                self.assertNoCrash(result)
                self.assertIn("Model was not changed", result.stdout)
                self.assertEqual(self.chat_settings(),
                                 ("cloud", "gemini-3.5-flash-lite"))

        self.assertDefaultsUntouched()

    def test_eof_and_ctrl_c_at_the_menu_change_nothing(self):
        eof = self.wos.run("/model\n")
        ctrl_c = self.wos.run_scripted(
            ["/model", "<CTRL-C>", "/status", "/exit"]
        )

        self.assertNoCrash(eof)
        self.assertNoCrash(ctrl_c)
        self.assertIn("Model was not changed", eof.stdout)
        self.assertIn("Model was not changed", ctrl_c.stdout)
        self.assertIn("WOS Status", ctrl_c.stdout)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_invalid_choices_ask_again(self):
        result = self.wos.run(lines("/model", "9", "abc", "0", "/exit"))

        self.assertNoCrash(result)
        self.assertEqual(
            result.stdout.count("Please enter a number from 1 to 2."), 2
        )

    def test_name_prompt_rejects_bad_names_and_can_be_cancelled(self):
        rejected = self.wos.run(lines(
            "/model", "2", "two words", "<name>", "", "/exit"
        ))

        self.assertNoCrash(rejected)
        self.assertIn("Model names cannot contain spaces.", rejected.stdout)
        self.assertIn("looks like a placeholder", rejected.stdout)
        self.assertIn("Model was not changed", rejected.stdout)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_eof_and_ctrl_c_at_the_name_prompt_change_nothing(self):
        eof = self.wos.run("/model\n2\n")
        ctrl_c = self.wos.run_scripted(
            ["/model", "2", "<CTRL-C>", "/status", "/exit"]
        )

        self.assertNoCrash(eof)
        self.assertNoCrash(ctrl_c)
        self.assertIn("WOS Status", ctrl_c.stdout)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_models_used_in_other_saved_chats_are_offered(self):
        result = self.wos.run(lines(
            "/model used-before", "/new", "/model", "0", "/exit",
        ))

        self.assertNoCrash(result)
        menu = result.stdout.split("Available Models")[-1]
        self.assertIn("used-before", menu)
        self.assertIn("gemini-3.5-flash-lite (current, default for new chats)",
                      menu)

    def test_models_listed_in_config_are_offered_and_bad_entries_ignored(self):
        config = json.loads(self.wos.config_path.read_text("utf-8"))
        config["suggested_models"] = {
            "cloud": ["my-listed-model", "<model_name>", "has space"]
        }
        self.wos.config_path.write_text(json.dumps(config), encoding="utf-8")

        result = self.wos.run(lines("/model", "0", "/exit"))

        self.assertNoCrash(result)
        self.assertIn("my-listed-model", result.stdout)
        self.assertNotIn("<model_name>\n", result.stdout.split("Available")[1])
        self.assertNotIn("has space", result.stdout)
        self.assertIn("Other (type a model name)", result.stdout)


class OllamaModelMenuTests(ProviderModelTestCase):
    def test_lists_installed_models_and_marks_the_current_one(self):
        self.wos.ollama = ["llama3:latest", "qwen:7b"]

        result = self.wos.run(lines(
            "/provider ollama llama3", "/model", "0", "/exit",
        ))

        self.assertNoCrash(result)
        self.assertIn("These are installed on this computer.", result.stdout)
        # "llama3" and "llama3:latest" are the same Ollama model.
        self.assertIn("1. llama3:latest (current)", result.stdout)
        self.assertIn("2. qwen:7b", result.stdout)
        self.assertNotIn("Other (type a model name)", result.stdout)

    def test_picking_an_installed_model(self):
        self.wos.ollama = ["llama3:latest", "qwen:7b"]

        result = self.wos.run(lines(
            "/provider ollama llama3", "/model", "2", "/status", "/exit",
        ))

        self.assertNoCrash(result)
        self.assertIn("Model changed to qwen:7b.", result.stdout)
        self.assertEqual(self.chat_settings(), ("ollama", "qwen:7b"))
        self.assertDefaultsUntouched()

    def test_falls_back_to_known_models_when_ollama_is_down(self):
        self.wos.ollama = "down"

        result = self.wos.run(lines(
            "/provider ollama llama3", "/model", "2", "0", "/exit",
        ))

        self.assertNoCrash(result)
        self.assertIn("Could not get the list of installed models",
                      result.stdout)
        self.assertIn("1. llama3 (current)", result.stdout)
        self.assertIn("2. Other (type a model name)", result.stdout)

    def test_no_models_installed(self):
        self.wos.ollama = "down"
        self.wos.run(lines("/provider ollama llama3", "/exit"))
        self.wos.ollama = []

        result = self.wos.run(lines("/model", "/exit"))

        self.assertNoCrash(result)
        self.assertIn("Ollama has no models installed yet.", result.stdout)
        self.assertIn("Model was not changed", result.stdout)
        self.assertEqual(self.chat_settings(), ("ollama", "llama3"))


class DirectModelCommandTests(ProviderModelTestCase):
    def test_cloud_model_is_changed_directly(self):
        result = self.wos.run(lines("/model any-cloud-model", "/exit"))

        self.assertNoCrash(result)
        self.assertNotIn("Available Models", result.stdout)
        self.assertIn("Model changed to any-cloud-model.", result.stdout)
        self.assertIn("This applies to the current chat (session_001) only.",
                      result.stdout)
        self.assertEqual(self.chat_settings(), ("cloud", "any-cloud-model"))
        self.assertDefaultsUntouched()

    def test_installed_ollama_model_is_accepted(self):
        self.wos.ollama = ["llama3:latest", "qwen:7b"]

        result = self.wos.run(lines(
            "/provider ollama qwen:7b", "/model llama3", "/exit",
        ))

        self.assertNoCrash(result)
        self.assertIn("Model changed to llama3.", result.stdout)
        self.assertEqual(self.chat_settings(), ("ollama", "llama3"))

    def test_uninstalled_ollama_model_is_rejected_clearly(self):
        self.wos.ollama = ["qwen:7b"]

        result = self.wos.run(lines(
            "/provider ollama qwen:7b", "/model llama3", "/exit",
        ))

        self.assertNoCrash(result)
        self.assertIn("Model 'llama3' is not installed in Ollama.",
                      result.stdout)
        self.assertIn("ollama pull llama3", result.stdout)
        self.assertIn("Model was not changed", result.stdout)
        self.assertEqual(self.chat_settings(), ("ollama", "qwen:7b"))

    def test_ollama_model_is_accepted_with_a_warning_when_ollama_is_down(self):
        self.wos.ollama = "down"

        result = self.wos.run(lines(
            "/provider ollama qwen:7b", "/model llama3", "/exit",
        ))

        self.assertNoCrash(result)
        self.assertIn("'llama3' was not verified", result.stdout)
        self.assertEqual(self.chat_settings(), ("ollama", "llama3"))

    def test_malformed_names_are_explained(self):
        for bad in ("<model_name>", "two words", "../x"):
            with self.subTest(model=bad):
                result = self.wos.run(lines(f"/model {bad}", "/exit"))

                self.assertNoCrash(result)
                self.assertIn("Couldn't change the model", result.stdout)
                self.assertEqual(self.chat_settings(),
                                 ("cloud", "gemini-3.5-flash-lite"))

    def test_the_same_model_is_reported_as_unchanged(self):
        result = self.wos.run(lines("/model gemini-3.5-flash-lite", "/exit"))

        self.assertIn("Already using model gemini-3.5-flash-lite. "
                      "Nothing was changed.", result.stdout)


# ------------------------------------------------------------------
# /defaults stays separate, and older behavior is preserved
# ------------------------------------------------------------------

class SeparationAndRegressionTests(ProviderModelTestCase):
    def test_defaults_commands_do_not_change_the_current_chat(self):
        result = self.wos.run(lines(
            "/defaults provider ollama llama3", "/status", "/exit",
        ))

        self.assertNoCrash(result)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))
        self.assertEqual(
            json.loads(self.wos.config_path.read_text("utf-8"))["provider"],
            "ollama",
        )

    def test_defaults_provider_still_asks_for_a_model_by_typing(self):
        result = self.wos.run(lines("/defaults provider ollama", "llama3", "/exit"))

        self.assertNoCrash(result)
        self.assertIn("Provider 'ollama' needs a model.", result.stdout)
        self.assertNotIn("Available Models", result.stdout)

    def test_provider_and_model_changes_survive_a_restart(self):
        self.wos.ollama = INSTALLED
        self.wos.run(lines("/provider", "2", "2", "/model", "3", "/exit"))

        result = self.wos.run(lines("/status", "/exit"))

        self.assertRegex(result.stdout, r"Provider:\s+ollama")
        self.assertRegex(result.stdout, r"Model:\s+qwen2\.5-coder:7b")
        self.assertDefaultsUntouched()

    def test_other_chats_keep_their_own_settings(self):
        self.wos.ollama = INSTALLED

        self.wos.run(lines("/new", "/provider", "2", "1", "/exit"))

        self.assertEqual(self.chat_settings("session_001"),
                         ("cloud", "gemini-3.5-flash-lite"))
        self.assertEqual(self.chat_settings("session_002"),
                         ("ollama", "gemma3:4b"))

    def test_help_documents_the_menus(self):
        result = self.wos.run(lines("/help provider", "/help model", "/exit"))

        self.assertNoCrash(result)
        flat = self.flat(result)
        for text in ("/provider <provider> [model]", "/model <model>",
                     "numbered list of providers",
                     "numbered list of models",
                     "installed on this computer",
                     "Other choice to type any name",
                     "current chat only"):
            with self.subTest(text=text):
                self.assertIn(text, flat)

    def test_chat_commands_are_unaffected(self):
        result = self.wos.run(lines("/chat list", "/chats", "/exit"))

        self.assertNoCrash(result)
        self.assertEqual(result.stdout.count("session_001: Chat 1"), 2)


# ------------------------------------------------------------------
# v0.2.5c: model sizes, and a retry/cancel path when Ollama is unavailable
# ------------------------------------------------------------------

WITH_SIZES = [
    {"name": "qwen2.5-coder:7b"},                       # size not reported
    {"name": "llama3:latest", "size": 4_661_224_676},
    {"name": "gemma3:4b", "size": 3_338_801_804},
]


class OllamaModelSizeTests(ProviderModelTestCase):
    def test_sizes_are_shown_when_ollama_reports_them(self):
        self.wos.ollama = WITH_SIZES

        result = self.wos.run(lines("/provider", "2", "0", "/exit"))

        self.assertNoCrash(result)
        self.assertIn("1. gemma3:4b - 3.3 GB\n", result.stdout)
        self.assertIn("2. llama3:latest - 4.7 GB\n", result.stdout)
        # No size reported: just the name, nothing invented.
        self.assertIn("3. qwen2.5-coder:7b\n", result.stdout)

    def test_a_model_with_a_size_is_selected_by_number(self):
        self.wos.ollama = WITH_SIZES

        result = self.wos.run(lines("/provider", "2", "2", "/exit"))

        self.assertNoCrash(result)
        self.assertIn("Current model: llama3:latest.", result.stdout)
        self.assertEqual(self.chat_settings(), ("ollama", "llama3:latest"))

    def test_sizes_do_not_end_up_in_the_saved_model_name(self):
        self.wos.ollama = WITH_SIZES

        self.wos.run(lines("/provider", "2", "1", "/exit"))

        self.assertEqual(self.chat_settings(), ("ollama", "gemma3:4b"))

    def test_model_menu_shows_sizes_and_the_current_marker(self):
        self.wos.ollama = WITH_SIZES

        result = self.wos.run(lines(
            "/provider ollama llama3", "/model", "0", "/exit",
        ))

        self.assertNoCrash(result)
        self.assertIn("2. llama3:latest - 4.7 GB (current)", result.stdout)

    def test_picking_from_the_model_menu_changes_the_model(self):
        self.wos.ollama = WITH_SIZES

        result = self.wos.run(lines(
            "/provider ollama llama3", "/model", "1", "/exit",
        ))

        self.assertNoCrash(result)
        self.assertEqual(self.chat_settings(), ("ollama", "gemma3:4b"))

    def test_models_without_any_sizes_still_list_normally(self):
        self.wos.ollama = INSTALLED

        result = self.wos.run(lines("/provider", "2", "0", "/exit"))

        self.assertIn("1. gemma3:4b\n2. llama3:latest\n3. qwen2.5-coder:7b\n",
                      result.stdout)


class OllamaUnavailableMenuTests(ProviderModelTestCase):
    def setUp(self):
        super().setUp()
        self.wos.ollama = "down"

    def test_explains_the_problem_and_offers_retry_and_manual_entry(self):
        result = self.wos.run(lines("/provider", "2", "0", "/exit"))

        self.assertNoCrash(result)
        flat = self.flat(result)
        for text in (
            "Could not get the list of installed models: Ollama is not running.",
            "Make sure Ollama is running (start the Ollama app, or run: "
            "ollama serve), then choose Try again.",
            "https://ollama.com",
            "Ollama Unavailable",
            "1. Try again",
            "2. Type a model name myself",
            "cannot check it while Ollama is unavailable",
            "(Enter or 0 to cancel)",
        ):
            with self.subTest(text=text):
                self.assertIn(text, flat)

    def test_cancelling_changes_neither_provider_nor_model(self):
        for answer in ("", "0"):
            with self.subTest(answer=repr(answer)):
                result = self.wos.run(lines("/provider", "2", answer, "/exit"))

                self.assertNoCrash(result)
                self.assertIn("Provider was not changed", result.stdout)
                self.assertNotIn("Provider changed to", result.stdout)
                # Cancelling ends the flow; it must not fall through to
                # asking for a model name.
                self.assertNotIn("Model name (Enter to cancel)", result.stdout)
                self.assertEqual(self.chat_settings(),
                                 ("cloud", "gemini-3.5-flash-lite"))

        self.assertDefaultsUntouched()

    def test_eof_and_ctrl_c_change_nothing_and_ctrl_c_keeps_wos_running(self):
        eof = self.wos.run("/provider\n2\n")
        ctrl_c = self.wos.run_scripted(
            ["/provider", "2", "<CTRL-C>", "/status", "/exit"]
        )

        self.assertNoCrash(eof)
        self.assertNoCrash(ctrl_c)
        self.assertIn("Provider was not changed", eof.stdout)
        self.assertIn("Provider was not changed", ctrl_c.stdout)
        self.assertIn("WOS Status", ctrl_c.stdout)
        self.assertNotIn("Model name (Enter to cancel)", ctrl_c.stdout)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_invalid_choices_ask_again(self):
        result = self.wos.run(lines("/provider", "2", "9", "abc", "0", "/exit"))

        self.assertNoCrash(result)
        self.assertEqual(
            result.stdout.count("Please enter a number from 1 to 2."), 2
        )
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_try_again_works_once_ollama_is_running(self):
        self.wos.ollama = "seq:" + json.dumps(["down", WITH_SIZES])

        result = self.wos.run(lines("/provider", "2", "1", "2", "/exit"))

        self.assertNoCrash(result)
        self.assertEqual(
            result.stdout.count("Ollama is not running."), 1
        )
        self.assertIn("2. llama3:latest - 4.7 GB", result.stdout)
        self.assertEqual(self.chat_settings(), ("ollama", "llama3:latest"))

    def test_try_again_can_be_repeated_and_then_cancelled(self):
        result = self.wos.run(
            lines("/provider", "2", "1", "1", "0", "/exit")
        )

        self.assertNoCrash(result)
        self.assertEqual(result.stdout.count("Ollama Unavailable\n"), 3)
        self.assertIn("Provider was not changed", result.stdout)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_try_again_with_no_models_installed_changes_nothing(self):
        self.wos.ollama = "seq:" + json.dumps(["down", []])

        result = self.wos.run(lines("/provider", "2", "1", "/exit"))

        self.assertNoCrash(result)
        self.assertIn("Ollama has no models installed yet.", result.stdout)
        self.assertEqual(self.chat_settings(),
                         ("cloud", "gemini-3.5-flash-lite"))

    def test_manual_entry_is_still_possible_and_validated(self):
        result = self.wos.run(lines(
            "/provider", "2", "2", "<model_name>", "llama3", "/exit",
        ))

        self.assertNoCrash(result)
        self.assertIn("looks like a placeholder", result.stdout)
        self.assertIn("cannot be checked until Ollama is running",
                      self.flat(result))
        self.assertEqual(self.chat_settings(), ("ollama", "llama3"))

    def test_cancelling_the_manual_name_prompt_changes_nothing(self):
        for script in (lines("/provider", "2", "2", "", "/exit"),
                       "/provider\n2\n2\n"):
            with self.subTest(script=script):
                result = self.wos.run(script)

                self.assertNoCrash(result)
                self.assertIn("Provider was not changed", result.stdout)
                self.assertEqual(self.chat_settings(),
                                 ("cloud", "gemini-3.5-flash-lite"))

    def test_the_same_menu_appears_for_model(self):
        self.wos.run(lines("/provider ollama llama3", "/exit"))

        cancelled = self.wos.run(lines("/model", "0", "/exit"))
        retried = self.wos.run(lines("/model", "1", "0", "/exit"))

        self.assertNoCrash(cancelled)
        self.assertIn("Ollama Unavailable", cancelled.stdout)
        self.assertIn("Model was not changed", cancelled.stdout)
        self.assertNotIn("Model name (Enter to cancel)", cancelled.stdout)
        self.assertEqual(retried.stdout.count("Ollama Unavailable\n"), 2)
        self.assertEqual(self.chat_settings(), ("ollama", "llama3"))

    def test_model_try_again_then_pick_by_number(self):
        self.wos.ollama = "seq:" + json.dumps(["down", "down", WITH_SIZES])

        result = self.wos.run(lines(
            "/provider ollama llama3",   # first check: down, accepted unverified
            "/model", "1",               # menu: try again -> now running
            "1", "/exit",
        ))

        self.assertNoCrash(result)
        self.assertIn("1. gemma3:4b - 3.3 GB", result.stdout)
        self.assertEqual(self.chat_settings(), ("ollama", "gemma3:4b"))

    def test_direct_commands_do_not_show_the_menu(self):
        result = self.wos.run(lines(
            "/provider ollama llama3", "/model qwen:7b", "/exit",
        ))

        self.assertNoCrash(result)
        self.assertNotIn("Ollama Unavailable", result.stdout)
        self.assertIn("was not verified", result.stdout)
        self.assertEqual(self.chat_settings(), ("ollama", "qwen:7b"))


class CloudWordingTests(ProviderModelTestCase):
    def test_instructions_are_plain_and_do_not_claim_live_verification(self):
        result = self.wos.run(lines("/model", "0", "/exit"))

        self.assertNoCrash(result)
        flat = self.flat(result)
        self.assertIn("These are model names WOS already knows "
                      "(from your settings and chats).", flat)
        self.assertIn("WOS cannot check them with your provider", flat)
        self.assertIn("To use a different model, choose Other and type its "
                      "exact name.", flat)
        for claim in ("verified", "supported models", "catalog", "live api",
                      "up to date"):
            with self.subTest(claim=claim):
                self.assertNotIn(claim, flat.lower())

    def test_typing_a_cloud_name_explains_what_is_and_is_not_checked(self):
        result = self.wos.run(lines("/model", "2", "", "/exit"))

        self.assertNoCrash(result)
        self.assertIn(
            "Type the exact model name. WOS can check that it is written "
            "correctly, but not that your provider offers it.",
            self.flat(result),
        )

    def test_the_ollama_prompt_does_not_show_the_cloud_hint(self):
        self.wos.ollama = "down"

        result = self.wos.run(lines("/provider", "2", "2", "", "/exit"))

        self.assertNotIn("that your provider offers it", self.flat(result))

    def test_cloud_menu_and_manual_entry_still_work(self):
        result = self.wos.run(lines("/model", "2", "my-model-id", "/exit"))

        self.assertNoCrash(result)
        self.assertIn("Other (type a model name)", result.stdout)
        self.assertEqual(self.chat_settings(), ("cloud", "my-model-id"))
        self.assertDefaultsUntouched()


if __name__ == "__main__":
    unittest.main()
