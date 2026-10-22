"""End-to-end tests that run start.py as a real subprocess.

A fake google.genai (or a deliberately broken one) is supplied through
PYTHONPATH, the project is copied to a temporary folder, and commands are fed
through stdin. No real API key, network or Ollama is needed.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import PROJECT_ROOT, DEFAULT_CONFIG, DEFAULT_SETTINGS


def write_stub(root, files):
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


# A working fake of google.genai. Any attempt to call the model fails loudly,
# which proves a message never reached the AI.
FAKE_GENAI = {
    "google/__init__.py": "",
    "google/genai/__init__.py": (
        "class Client:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        raise RuntimeError('fake genai was called')\n"
    ),
    "google/genai/types.py": "",
}

# A fake 'requests' so no test ever talks to a real Ollama. What /api/tags
# answers is chosen per test through the FAKE_OLLAMA environment variable:
#   "down"                        connection refused (Ollama is not running)
#   "timeout"                     Ollama does not answer in time
#   "garbage"                     an answer that is not JSON
#   "error"                       an HTTP 500
#   a JSON list of model names    those models are installed; an entry may
#                                 also be {"name": ..., "size": <bytes>}
#   "seq:" + a JSON list of the above   answers differ call by call (the last
#                                 one repeats), to test "try again"
FAKE_REQUESTS = {
    "requests/exceptions.py": (
        "class RequestException(IOError):\n    pass\n\n\n"
        "class ConnectionError(RequestException):\n    pass\n\n\n"
        "class Timeout(RequestException):\n    pass\n\n\n"
        "class HTTPError(RequestException):\n    pass\n"
    ),
    "requests/__init__.py": (
        "import json, os\n"
        "from requests import exceptions\n\n\n"
        "class Response:\n"
        "    def __init__(self, status, body):\n"
        "        self.status_code = status\n"
        "        self.body = body\n\n"
        "    def raise_for_status(self):\n"
        "        if self.status_code >= 400:\n"
        "            raise exceptions.HTTPError(str(self.status_code))\n\n"
        "    def json(self):\n"
        "        return json.loads(self.body)\n\n\n"
        "def get(url, timeout=None, **kwargs):\n"
        "    mode = os.environ.get('FAKE_OLLAMA', 'down')\n"
        "    if mode.startswith('seq:'):\n"
        "        states = json.loads(mode[4:])\n"
        "        try:\n"
        "            count = int(open('_fake_ollama_calls').read())\n"
        "        except OSError:\n"
        "            count = 0\n"
        "        open('_fake_ollama_calls', 'w').write(str(count + 1))\n"
        "        mode = states[min(count, len(states) - 1)]\n"
        "        mode = mode if isinstance(mode, str) else json.dumps(mode)\n"
        "    if mode == 'down':\n"
        "        raise exceptions.ConnectionError('refused')\n"
        "    if mode == 'timeout':\n"
        "        raise exceptions.Timeout('slow')\n"
        "    if mode == 'garbage':\n"
        "        return Response(200, 'this is not json')\n"
        "    if mode == 'error':\n"
        "        return Response(500, '{}')\n"
        "    names = json.loads(mode)\n"
        "    return Response(200, json.dumps({'models': [\n"
        "        n if isinstance(n, dict) else {'name': n} for n in names]}))\n\n\n"
        "def post(url, **kwargs):\n"
        "    raise exceptions.ConnectionError('refused')\n"
    ),
}

# A 'google' package exists, but google.genai does not. This reproduces
# "ImportError: cannot import name 'genai' from 'google'".
GOOGLE_WITHOUT_GENAI = {"google/__init__.py": ""}

# python-dotenv missing entirely.
MISSING_DOTENV = {
    **FAKE_GENAI,
    "dotenv/__init__.py": (
        "raise ModuleNotFoundError(\"No module named 'dotenv'\", "
        "name='dotenv')\n"
    ),
}


# Runs start.py with input() replaced by a script. An answer of "<CTRL-C>"
# raises a real KeyboardInterrupt and "<EOF>" raises a real EOFError at that
# prompt, which piping text through stdin cannot do (a pipe can only end).
SCRIPTED_RUNNER = """\
import builtins, json, runpy, sys

answers = json.loads(sys.argv[1])


def scripted_input(prompt=""):
    sys.stdout.write(prompt)

    if not answers:
        sys.stdout.write("\\n")
        raise EOFError

    answer = answers.pop(0)

    if answer == "<CTRL-C>":
        sys.stdout.write("^C\\n")
        raise KeyboardInterrupt

    if answer == "<EOF>":
        sys.stdout.write("^D\\n")
        raise EOFError

    sys.stdout.write(answer + "\\n")
    return answer


builtins.input = scripted_input
sys.argv = ["start.py"]
runpy.run_path("start.py", run_name="__main__")
"""


class WosProject:
    """A throwaway copy of the project that can be started repeatedly."""

    def __init__(self, stub_files=None, max_chats=5):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.project = root / "WOS copy"  # a space, like a real Windows path
        self.stubs = root / "stubs"
        self.stubs.mkdir()

        self.project.mkdir()
        shutil.copy(PROJECT_ROOT / "start.py", self.project)
        shutil.copytree(
            PROJECT_ROOT / "core",
            self.project / "core",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        shutil.copytree(PROJECT_ROOT / "characters",
                        self.project / "characters")

        (self.project / "config.json").write_text(
            json.dumps(DEFAULT_CONFIG), encoding="utf-8"
        )
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        settings["sessions"]["max_chats"] = max_chats
        (self.project / "settings.json").write_text(
            json.dumps(settings), encoding="utf-8"
        )

        write_stub(self.stubs, stub_files or FAKE_GENAI)
        write_stub(self.stubs, FAKE_REQUESTS)

        # What the fake Ollama reports: "down", or a list of installed models.
        self.ollama = "down"

    def close(self):
        self._tmp.cleanup()

    def _env(self):
        # Each run starts counting fake Ollama calls from zero.
        (self.project / "_fake_ollama_calls").unlink(missing_ok=True)

        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.stubs)
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("GEMINI_API_KEY", None)
        env["FAKE_OLLAMA"] = (
            self.ollama if isinstance(self.ollama, str)
            else json.dumps(self.ollama)
        )
        return env

    def run(self, stdin_text):
        env = self._env()

        result = subprocess.run(
            [sys.executable, "start.py"],
            cwd=self.project,
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            env=env,
        )
        return result

    def run_scripted(self, answers):
        """Run WOS with the given list of answers, one per prompt."""
        (self.project / "_scripted_runner.py").write_text(
            SCRIPTED_RUNNER, encoding="utf-8"
        )

        env = self._env()

        return subprocess.run(
            [sys.executable, "_scripted_runner.py", json.dumps(answers)],
            cwd=self.project,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            env=env,
        )

    @property
    def config_path(self):
        return self.project / "config.json"

    @property
    def chats_dir(self):
        return self.project / "data" / "chats"

    def chat_ids(self):
        return sorted(
            p.stem for p in self.chats_dir.glob("session_*.json")
        )

    def read_chat(self, session_id):
        return json.loads(
            (self.chats_dir / f"{session_id}.json").read_text("utf-8")
        )


class IntegrationTestCase(unittest.TestCase):
    stub_files = None
    max_chats = 5

    def setUp(self):
        self.wos = WosProject(self.stub_files, self.max_chats)
        self.addCleanup(self.wos.close)

    def assertNoCrash(self, result):
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        self.assertNotIn("Traceback", result.stdout, result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)


class StartupImportErrorTests(unittest.TestCase):
    def run_with(self, stub_files):
        wos = WosProject(stub_files)
        self.addCleanup(wos.close)
        return wos.run("")

    def test_google_without_genai_shows_a_clear_message(self):
        result = self.run_with(GOOGLE_WITHOUT_GENAI)

        self.assertEqual(result.returncode, 1)
        self.assertIn("missing package 'google-genai'", result.stdout)
        self.assertIn("pip install google-genai", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_dotenv_still_shows_a_clear_message(self):
        result = self.run_with(MISSING_DOTENV)

        self.assertEqual(result.returncode, 1)
        self.assertIn("missing package 'python-dotenv'", result.stdout)
        self.assertNotIn("Traceback", result.stderr)


class UnknownCommandTests(IntegrationTestCase):
    def test_unknown_commands_are_not_sent_to_the_ai(self):
        result = self.wos.run("/modle gemini\n/foobar\n/new extra\n/exit\n")

        self.assertNoCrash(result)
        self.assertIn("Unknown command '/modle'", result.stdout)
        self.assertIn("Did you mean /model?", result.stdout)
        self.assertIn("Unknown command '/foobar'", result.stdout)
        self.assertIn("'/new' was not used correctly", result.stdout)
        # The fake AI raises if it is ever called.
        self.assertNotIn("fake genai", result.stdout)
        self.assertNotIn("WLLM", result.stdout)

        chat = self.wos.read_chat("session_001")
        self.assertEqual(chat["messages"], [])
        self.assertEqual(self.wos.chat_ids(), ["session_001"])

    def test_normal_text_still_reaches_the_ai(self):
        result = self.wos.run("hello there\n/exit\n")

        # No API key in this test, so the provider reports that instead.
        self.assertIn("GEMINI_API_KEY", result.stdout)
        self.assertNotIn("Unknown command", result.stdout)

    def test_help_lists_every_command(self):
        from core.cli import KNOWN_COMMANDS

        result = self.wos.run("/help\n/exit\n")

        for command in KNOWN_COMMANDS:
            with self.subTest(command=command):
                self.assertIn(command, result.stdout)


class SafePromptTests(IntegrationTestCase):
    def test_eof_at_a_delete_confirmation_cancels_without_crashing(self):
        result = self.wos.run("/delete session_001\n")  # input ends here

        self.assertNoCrash(result)
        self.assertIn("Deletion cancelled", result.stdout)
        self.assertEqual(self.wos.chat_ids(), ["session_001"])

    def test_eof_at_memory_confirmations_cancels_without_crashing(self):
        for command in ("/memory delete anything", "/memory clear"):
            with self.subTest(command=command):
                result = self.wos.run(command + "\n")

                self.assertNoCrash(result)
                self.assertIn("cancelled", result.stdout)

    def test_declining_a_delete_keeps_the_chat(self):
        result = self.wos.run("/delete session_001\nn\n/exit\n")

        self.assertNoCrash(result)
        self.assertEqual(self.wos.chat_ids(), ["session_001"])

    def test_eof_at_the_model_prompt_cancels_a_provider_change(self):
        result = self.wos.run("/provider ollama\n")  # input ends at the prompt

        self.assertNoCrash(result)
        self.assertIn("Could not get the list of installed models",
                      result.stdout)
        self.assertIn("Provider was not changed", result.stdout)
        self.assertEqual(
            self.wos.read_chat("session_001")["provider"], "cloud"
        )


class ProviderCommandTests(IntegrationTestCase):
    def test_provider_menu_is_generated_from_supported_providers(self):
        from core.config import SUPPORTED_PROVIDERS

        result = self.wos.run("/provider\n0\n/exit\n")

        self.assertNoCrash(result)
        for number, name in enumerate(["cloud", "ollama"] + sorted(SUPPORTED_PROVIDERS - {"cloud", "ollama"}), start=1):
            with self.subTest(provider=name):
                self.assertRegex(result.stdout, rf"{number}\. {name}\b")

    def test_provider_change_requests_a_model_then_applies_both(self):
        result = self.wos.run(
            "/provider ollama\n2\nllama3\n/status\n/exit\n"  # 2 = type a name
        )

        self.assertNoCrash(result)
        self.assertIn("Provider changed to ollama", result.stdout)
        self.assertRegex(result.stdout, r"Provider:\s+ollama")
        self.assertRegex(result.stdout, r"Model:\s+llama3")

        chat = self.wos.read_chat("session_001")
        self.assertEqual((chat["provider"], chat["model"]),
                         ("ollama", "llama3"))

    def test_provider_with_model_argument_needs_no_prompt(self):
        result = self.wos.run("/provider ollama llama3\n/exit\n")

        self.assertNoCrash(result)
        self.assertNotIn("needs a model", result.stdout)
        self.assertIn("Current model: llama3", result.stdout)

    def test_placeholder_model_is_rejected_by_the_model_command(self):
        result = self.wos.run("/model <model_name>\n/status\n/exit\n")

        self.assertNoCrash(result)
        self.assertIn("placeholder", result.stdout)
        self.assertRegex(result.stdout, r"Model:\s+gemini-3.5-flash-lite")


class DefaultsCommandTests(IntegrationTestCase):
    def test_placeholder_default_model_is_rejected_and_config_unchanged(self):
        before = self.wos.config_path.read_bytes()

        result = self.wos.run("/defaults model <model_name>\n/exit\n")

        self.assertNoCrash(result)
        self.assertIn("placeholder", result.stdout)
        self.assertEqual(self.wos.config_path.read_bytes(), before)

    def test_unknown_default_character_is_rejected(self):
        before = self.wos.config_path.read_bytes()

        result = self.wos.run("/defaults character ghost\n/exit\n")

        self.assertNoCrash(result)
        self.assertIn("was not found", result.stdout)
        self.assertEqual(self.wos.config_path.read_bytes(), before)

    def test_defaults_apply_to_new_chats_but_not_existing_ones(self):
        result = self.wos.run(
            "/defaults provider ollama llama3\n"
            "/status\n"       # the current chat must be unchanged
            "/new\n"
            "/status\n"       # the new chat must use the defaults
            "/exit\n"
        )

        self.assertNoCrash(result)
        self.assertIn("New chats will use these settings", result.stdout)

        statuses = re.findall(
            r"Provider:\s+(\w+)\s+Model:\s+(\S+)", result.stdout
        )
        self.assertEqual(statuses[-2:], [
            ("cloud", "gemini-3.5-flash-lite"),
            ("ollama", "llama3"),
        ])

        self.assertEqual(
            json.loads(self.wos.config_path.read_text("utf-8")),
            {"provider": "ollama", "model": "llama3", "character": "nexis"},
        )

        first = self.wos.read_chat("session_001")
        second = self.wos.read_chat("session_002")
        self.assertEqual(first["provider"], "cloud")
        self.assertEqual((second["provider"], second["model"]),
                         ("ollama", "llama3"))

    def test_restart_resumes_the_existing_chats_own_settings(self):
        self.wos.run("/defaults provider ollama llama3\n/exit\n")

        result = self.wos.run("/status\n/exit\n")

        self.assertNoCrash(result)
        self.assertRegex(result.stdout, r"Provider:\s+cloud")
        self.assertRegex(result.stdout, r"Model:\s+gemini-3.5-flash-lite")

    def test_defaults_provider_without_known_model_asks_for_one(self):
        result = self.wos.run(
            "/defaults provider ollama\nllama3\n/exit\n"
        )

        self.assertNoCrash(result)
        self.assertIn("needs a model", result.stdout)
        self.assertEqual(
            json.loads(self.wos.config_path.read_text("utf-8"))["model"],
            "llama3",
        )

    def test_defaults_provider_cancelled_at_the_model_prompt(self):
        before = self.wos.config_path.read_bytes()

        result = self.wos.run("/defaults provider ollama\n")

        self.assertNoCrash(result)
        self.assertIn("Default settings were not changed", result.stdout)
        self.assertEqual(self.wos.config_path.read_bytes(), before)


class MaxChatsTests(IntegrationTestCase):
    max_chats = 2

    def test_new_chat_at_the_limit_requires_choosing_a_chat_to_delete(self):
        result = self.wos.run(
            "/new\n"      # session_002, now at the limit of 2
            "/new\n"      # blocked: choose a chat to delete
            "1\n"         # the first listed chat (session_001)
            "y\n"         # confirm
            "/exit\n"
        )

        self.assertNoCrash(result)
        self.assertIn("Chat limit reached (2 saved chats)", result.stdout)
        self.assertIn("Number of the chat to delete", result.stdout)
        self.assertIn("Deleted session_001", result.stdout)
        self.assertIn("Started session_003", result.stdout)
        self.assertEqual(self.wos.chat_ids(), ["session_002", "session_003"])

    def test_cancelling_keeps_every_chat_and_creates_nothing(self):
        result = self.wos.run("/new\n/new\n\n/exit\n")

        self.assertNoCrash(result)
        self.assertIn("No new chat was created", result.stdout)
        self.assertEqual(self.wos.chat_ids(), ["session_001", "session_002"])

    def test_declining_the_confirmation_keeps_every_chat(self):
        result = self.wos.run("/new\n/new\n1\nn\n/exit\n")

        self.assertNoCrash(result)
        self.assertIn("Deletion cancelled", result.stdout)
        self.assertIn("No new chat was created", result.stdout)
        self.assertEqual(self.wos.chat_ids(), ["session_001", "session_002"])

    def test_an_invalid_choice_asks_again_and_deletes_nothing(self):
        result = self.wos.run("/new\n/new\n9\nabc\n-1\n0\n/exit\n")

        self.assertNoCrash(result)
        self.assertEqual(
            result.stdout.count("Please enter a number from 1 to 2."), 3
        )
        self.assertIn("No new chat was created", result.stdout)
        self.assertEqual(self.wos.chat_ids(), ["session_001", "session_002"])

    def test_zero_cancels_without_deleting_or_creating_anything(self):
        result = self.wos.run("/new\n/new\n0\n/exit\n")

        self.assertNoCrash(result)
        self.assertIn("No new chat was created", result.stdout)
        self.assertEqual(self.wos.chat_ids(), ["session_001", "session_002"])

    def test_ctrl_c_at_the_selection_prompt_deletes_nothing(self):
        result = self.wos.run_scripted(
            ["/new", "/new", "<CTRL-C>", "/status", "/exit"]
        )

        self.assertNoCrash(result)
        self.assertIn("No new chat was created", result.stdout)
        self.assertIn("WOS Status", result.stdout)  # WOS kept running
        self.assertEqual(self.wos.chat_ids(), ["session_001", "session_002"])

    def test_eof_at_the_selection_prompt_deletes_nothing(self):
        result = self.wos.run("/new\n/new\n")  # input ends at the prompt

        self.assertNoCrash(result)
        self.assertEqual(self.wos.chat_ids(), ["session_001", "session_002"])

    def test_deleting_the_active_chat_to_make_room_works(self):
        result = self.wos.run(
            "/new\n"      # session_002 is now the active chat
            "/new\n"
            "2\n"         # delete the active chat
            "y\n"
            "/status\n"
            "/exit\n"
        )

        self.assertNoCrash(result)
        self.assertIn("Deleted session_002", result.stdout)
        # WOS numbers chats as "highest existing + 1", so the freed number
        # is reused. This was already the behavior before the limit change.
        self.assertIn("Started session_002", result.stdout)
        self.assertEqual(self.wos.chat_ids(), ["session_001", "session_002"])

    def test_chats_survive_across_restarts_when_the_limit_is_hit(self):
        self.wos.run("/new\n/exit\n")

        result = self.wos.run("/new\n\n/exit\n")

        self.assertNoCrash(result)
        self.assertEqual(self.wos.chat_ids(), ["session_001", "session_002"])


if __name__ == "__main__":
    unittest.main()
