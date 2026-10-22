
import builtins
import ctypes
import difflib
import os
import subprocess
import sys

from core.config import load_settings

# core.session and core.conversation load settings.json when they are imported,
# so check it first and report problems cleanly instead of with a traceback.
try:
    load_settings()
except (ValueError, OSError) as error:
    print(f"WOS: Could not start: {error}")
    raise SystemExit(1)

MISSING_PACKAGE_NAMES = {
    "google": "google-genai",
    "dotenv": "python-dotenv",
    "requests": "requests",
}

try:
    from core.session import Session
except ImportError as error:
    # ModuleNotFoundError is a kind of ImportError. A plain ImportError such as
    # "cannot import name 'genai' from 'google'" happens when another package
    # already provides the top-level 'google' name but google-genai is missing.
    top_level_name = (error.name or "").split(".")[0]
    package = MISSING_PACKAGE_NAMES.get(top_level_name)

    if package is None:
        raise

    print(
        f"WOS: Could not start: missing package '{package}'. "
        f"Install it with: pip install {package}"
    )
    raise SystemExit(1)

from core.character import (
    list_characters,
    list_skipped_characters,
    load_character,
)
from core.memory import (
    show_memory,
    edit_memory,
    forget_memory,
    clear_memory,
)
from core.conversation import (
    ChatLimitError,
    load_index,
    load_chat,
    update_chat,
    delete_chat,
)
from core.config import (
    SUPPORTED_PROVIDERS,
    load_settings,
    validate_model_name,
)
from core.cli import (
    check_unrecognized_command,
    confirm,
    read_input,
    select_option,
)
from core.models import (
    OTHER_LABEL,
    check_ollama_model,
    label_model,
    menu_models,
)
from core.providers.ollama import (
    OllamaUnavailable,
    list_installed_model_details,
)
from core.help_text import (
    help_lines,
    overview_lines,
    suggest_topic,
)
from core.tts import FishAudioSpeaker
from core.model_catalog import ModelCatalog
from core.provider_registry import PROVIDERS, api_key
from core.overlay import OverlayBackend, OverlayServer, overlay_port


# ------------------------------
# Terminal colors
# ------------------------------

RESET = "\033[0m"
WHITE = "\033[97m"
CYAN = "\033[96m"
GREEN = "\033[92m"
MAGENTA = "\033[95m"
BLUE = "\033[94m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[90m"

CHARACTER_COLORS = {
    "white": WHITE,
    "cyan": CYAN,
    "green": GREEN,
    "magenta": MAGENTA,
    "blue": BLUE,
    "yellow": YELLOW,
    "red": RED,
}

COLORS_ENABLED = False


def enable_terminal_colors():
    """Enable ANSI colors on supported Windows terminals."""
    global COLORS_ENABLED

    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            stdout_handle = kernel32.GetStdHandle(-11)

            mode = ctypes.c_uint()
            if kernel32.GetConsoleMode(
                stdout_handle,
                ctypes.byref(mode),
            ):
                ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                if kernel32.SetConsoleMode(
                    stdout_handle,
                    mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING,
                ):
                    COLORS_ENABLED = True
        except (AttributeError, OSError):
            COLORS_ENABLED = False
    else:
        COLORS_ENABLED = sys.stdout.isatty()


def get_default_name_color():
    """Get the default character-name color from settings.json."""
    try:
        settings = load_settings()
        color_name = (
            settings.get("terminal", {})
            .get("default_name_color", "white")
        )

        if isinstance(color_name, str):
            return CHARACTER_COLORS.get(
                color_name.lower(),
                WHITE,
            )

    except (OSError, ValueError, AttributeError, TypeError):
        pass

    return WHITE


def get_character_name_color():
    """Prefer the active character's color, then the global default."""
    default_color = get_default_name_color()
    active_session = globals().get("session")

    if active_session is None:
        return default_color

    character_id = getattr(active_session, "character_id", None)

    if not character_id:
        return default_color

    try:
        character = load_character(character_id)
        terminal = character.get("terminal", {})
        color_name = terminal.get("name_color")

        if isinstance(color_name, str):
            color = CHARACTER_COLORS.get(color_name.lower())

            if color:
                return color

    except (OSError, ValueError, AttributeError, TypeError):
        pass

    return default_color


def color_for_line(line):
    """Choose colors for WOS output."""
    stripped = line.strip()
    lowered = stripped.lower()

    if not stripped:
        return None

    error_phrases = (
        "error:",
        "failed",
        "could not",
        "couldn't",
        "not found",
        "did not return",
        "unsupported provider",
        "unknown provider",
        "unknown command",
        "limit reached",
        "is not installed",
        "invalid ",
    )

    if stripped.startswith("You:"):
        return GREEN

    active_session = globals().get("session")
    character_name = getattr(active_session, "character_name", "Nexis")

    if stripped.startswith(f"{character_name}:"):
        return "CHARACTER"

    if stripped.startswith("WLLM:"):
        return YELLOW

    if stripped.startswith("WOS:"):
        return (
            RED
            if any(phrase in lowered for phrase in error_phrases)
            else BLUE
        )

    if any(phrase in lowered for phrase in error_phrases):
        return RED

    if (
        stripped == "WOS"
        or stripped.startswith("===")
        or stripped.startswith("---")
        or stripped in (
            "Saved Chats",
            "Chat Actions",
            "Providers",
            "Available Models",
            "Ollama Unavailable",
            "WOS commands:",
            "Memory phrases:",
            "WOS Status",
            "WOS Context",
            "WOS Default Settings",
            "Available Characters",
        )
    ):
        return CYAN

    if stripped.startswith((
        "Character:",
        "Provider:",
        "Model:",
        "Memory:",
        "Session:",
        "Saved messages:",
        "Messages in context:",
        "Context limit:",
        "Saved chats:",
        "Saved memories:",
        "Default provider:",
        "Default model:",
        "Default character:",
    )):
        return DIM

    return None


def print(*values, sep=" ", end="\n", file=None, flush=False):
    """Color WOS output while preserving normal print behavior."""
    destination = file if file is not None else sys.stdout
    text = sep.join(str(value) for value in values)

    if not COLORS_ENABLED or destination is not sys.stdout:
        builtins.print(
            text,
            end=end,
            file=destination,
            flush=flush,
        )
        return

    lines = text.split("\n")
    colored_lines = []

    active_session = globals().get("session")
    character_name = getattr(active_session, "character_name", "Nexis")
    character_prefix = f"{character_name}:"

    # Detect a character response that spans multiple lines.
    is_character_response = (
        bool(lines)
        and lines[0].lstrip().startswith(character_prefix)
    )

    for index, line in enumerate(lines):
        if is_character_response:
            if index == 0:
                position = line.find(character_prefix)

                if position >= 0:
                    prefix_end = position + len(character_prefix)
                    prefix = line[position:prefix_end]
                    response = line[prefix_end:]
                    name_color = get_character_name_color()

                    colored_lines.append(
                        f"{line[:position]}"
                        f"{name_color}{prefix}{RESET}"
                        f"{WHITE}{response}{RESET}"
                    )
                else:
                    colored_lines.append(f"{WHITE}{line}{RESET}")
            else:
                colored_lines.append(f"{WHITE}{line}{RESET}")

            continue

        color = color_for_line(line)

        if color:
            colored_lines.append(f"{color}{line}{RESET}")
        else:
            colored_lines.append(line)

    builtins.print(
        "\n".join(colored_lines),
        end=end,
        file=destination,
        flush=flush,
    )


def input(prompt=""):
    """Color interactive prompts."""
    if not COLORS_ENABLED:
        return builtins.input(prompt)

    color = GREEN if prompt.startswith("You:") else BLUE

    sys.stdout.write(f"{color}{prompt}")
    sys.stdout.flush()

    try:
        result = builtins.input()
    finally:
        sys.stdout.write(RESET)
        sys.stdout.flush()

    return result


enable_terminal_colors()


# ------------------------------
# Safe prompts
# ------------------------------

def ask(prompt):
    """Ask for input. Returns None if the user pressed Ctrl+C or input ended."""
    return read_input(prompt, input)


def ask_yes_no(prompt):
    """Ask a yes/no question. Ctrl+C, EOF, or anything but yes means no."""
    return confirm(prompt, input)


# ------------------------------
# Help
# ------------------------------

def show_help():
    for line in overview_lines():
        print(line)


def show_command_help(arguments):
    lines = help_lines(arguments)

    if lines is None:
        arguments = arguments.strip()
        print(f"WOS: No help is available for '{arguments}'.")

        suggestion = suggest_topic(arguments.split()[0]) if arguments else None

        if suggestion:
            print(f"Did you mean /help {suggestion[1:]}?")

        print("Type /help to see all commands.")
        return

    for line in lines:
        print(line)


# ------------------------------
# Initialize WOS
# ------------------------------

try:
    session = Session()
except (ValueError, FileNotFoundError, KeyError) as error:
    print(f"WOS: Could not start: {error}")
    raise SystemExit(1)


def show_header():
    print()
    print("==============================")
    print("             WOS")
    print("==============================")
    print(f"Character: {session.character_name}")
    print(f"Provider:  {session.provider}")
    print(f"Model:     {session.model}")
    print(f"Memory:    {len(session.memory)} item(s)")
    print(f"Session:   {session.session_id}")
    print("==============================")
    print("Type /help for commands.")
    print()


def describe_chat(chat_info):
    """One line describing a saved chat, shared by lists and menus."""
    chat_id = chat_info.get("session_id", "Unknown")
    title = chat_info.get("title", "Untitled")
    provider = chat_info.get("provider", "unknown")
    model = chat_info.get("model", "unknown")
    active = " (active)" if chat_id == session.session_id else ""

    return f"{chat_id}: {title} [{provider} / {model}]{active}"


def show_chats():
    chats = load_index()

    print()
    print("Saved Chats")
    print("-----------")

    if not chats:
        print("No saved chats.")
        print()
        return

    for chat_info in chats:
        print(describe_chat(chat_info))

    print()


def show_character():
    print(
        f"WOS: Active character: "
        f"{session.character_name} ({session.character_id})"
    )


def show_characters():
    characters = list_characters()

    print()
    print("Available Characters")
    print("--------------------")

    if not characters:
        print("No valid characters found.")

    for character in characters:
        active = " (active)" if character["id"] == session.character_id else ""
        print(f'{character["id"]}: {character["name"]}{active}')

    skipped = list_skipped_characters()

    if skipped:
        print()
        print("Skipped files (they cannot be used until fixed):")

        for file_name, reason in skipped:
            print(f"{file_name}: {reason}")

    print()


def manage_character(arguments):
    arguments = arguments.strip()

    if not arguments:
        show_character()
        return

    action, separator, remainder = arguments.partition(" ")
    action = action.lower()
    remainder = remainder.strip()

    if action == "list" and not separator:
        show_characters()
        return

    if action == "switch":
        if not remainder:
            remainder = choose_character()

            if remainder is None:
                return

        elif " " in remainder:
            print("WOS: Usage: /character switch [character_id]")
            print("Use /character list to see available characters.")
            return

        result = session.switch_character(remainder)
        print(f"WOS: {result}")
        return

    print("WOS: Unknown character command.")
    print("Use /character, /character list, or /character switch [id].")


def choose_character():
    """Let the user pick a character from a numbered list. Returns its id,
    or None if nothing was chosen."""
    characters = list_characters()

    if not characters:
        print("WOS: No valid characters found.")
        return None

    labels = []

    for character in characters:
        active = " (active)" if character["id"] == session.character_id else ""
        labels.append(f'{character["id"]}: {character["name"]}{active}')

    index = select_option(
        labels,
        title="Available Characters",
        prompt=(
            "WOS: Number of the character to switch to "
            "(Enter or 0 to cancel): "
        ),
        reader=input,
        writer=print,
    )

    if index is None:
        print("WOS: Character was not changed.")
        return None

    return characters[index]["id"]


# ------------------------------
# Chat commands
# ------------------------------

CHAT_ACTIONS = (
    ("List saved chats", "list"),
    ("Switch to another chat", "switch"),
    ("Rename a chat", "rename"),
    ("Delete a chat", "delete"),
    ("Clear the current chat", "clear"),
)

CHAT_SUBCOMMANDS = tuple(action for _, action in CHAT_ACTIONS)


def choose_chat(
    prompt,
    cancelled_message="WOS: Cancelled. Nothing was changed.",
):
    """Let the user pick a saved chat from a numbered list.

    Returns the chat's session ID, or None if nothing was chosen because the
    user cancelled, pressed Ctrl+C, the input ended, or there are no chats.
    """
    chats = load_index()

    if not chats:
        print("WOS: There are no saved chats.")
        return None

    index = select_option(
        [describe_chat(chat_info) for chat_info in chats],
        title="Saved Chats",
        prompt=prompt,
        reader=input,
        writer=print,
    )

    if index is None:
        if cancelled_message:
            print(cancelled_message)

        return None

    return chats[index].get("session_id")


def report_chat_not_found(target_id, command):
    print(f"WOS: Chat '{target_id}' was not found.")
    print("Use /chat list to see your saved chats.")
    print(f"To choose from a list instead, type /chat {command} on its own.")


def switch_to_chat(target_id):
    if session.switch_chat(target_id):
        print(f"WOS: Switched to {session.session_id}.")
        print(f"Character: {session.character_name}")
        print(f"Provider: {session.provider}")
        print(f"Model: {session.model}")
    else:
        report_chat_not_found(target_id, "switch")


def switch_chat_command(arguments):
    target_id = arguments.strip()

    if not target_id:
        target_id = choose_chat(
            prompt=(
                "WOS: Number of the chat to switch to "
                "(Enter or 0 to cancel): "
            ),
            cancelled_message=(
                f"WOS: Switch cancelled. "
                f"You are still in {session.session_id}."
            ),
        )

        if target_id is None:
            return

    switch_to_chat(target_id)


def rename_chat(arguments):
    arguments = arguments.strip()
    new_name = None

    if not arguments:
        target_id = choose_chat(
            prompt=(
                "WOS: Number of the chat to rename "
                "(Enter or 0 to cancel): "
            ),
            cancelled_message="WOS: Rename cancelled.",
        )

        if target_id is None:
            return
    else:
        parts = arguments.split(maxsplit=1)
        target_id = parts[0]

        if len(parts) == 2:
            new_name = parts[1].strip()

    chat_data = load_chat(target_id)

    if chat_data is None:
        report_chat_not_found(target_id, "rename")
        return

    if not new_name:
        current_title = chat_data.get("title", "Untitled")
        answer = ask(
            f"WOS: New title for '{current_title}' (Enter to cancel): "
        )

        if answer is None or not answer.strip():
            print("WOS: Rename cancelled.")
            return

        new_name = answer.strip()

    chat_data["title"] = new_name

    try:
        update_chat(chat_data)
    except (OSError, ValueError) as error:
        print(f"WOS: Could not rename chat: {error}")
        return

    if target_id == session.session_id:
        session.chat = chat_data

    print(f"WOS: Renamed {target_id} to '{new_name}'.")


def delete_chat_command(arguments):
    target_id = arguments.strip()

    if not target_id:
        target_id = choose_chat(
            prompt=(
                "WOS: Number of the chat to delete "
                "(Enter or 0 to cancel): "
            ),
            cancelled_message="WOS: Deletion cancelled.",
        )

        if target_id is None:
            return

    elif " " in target_id:
        print("WOS: Usage: /chat delete [session_id]")
        return

    remove_chat(target_id)


def remove_chat(target_id):
    chat_data = load_chat(target_id)

    if chat_data is None:
        report_chat_not_found(target_id, "delete")
        return

    title = chat_data.get("title", "Untitled")

    if not ask_yes_no(
        f"WOS: Delete '{title}' ({target_id}) permanently? (y/n): "
    ):
        print("WOS: Deletion cancelled.")
        return

    was_active = target_id == session.session_id

    try:
        deleted = delete_chat(target_id)
    except (OSError, ValueError) as error:
        print(f"WOS: Could not delete chat: {error}")
        return

    if deleted is False:
        print(f"WOS: Chat '{target_id}' could not be deleted.")
        return

    print(f"WOS: Deleted {target_id}.")

    if was_active:
        remaining_chats = load_index()

        try:
            if remaining_chats:
                next_id = remaining_chats[-1].get("session_id")

                if not next_id or not session.switch_chat(next_id):
                    session.new_chat()
            else:
                session.new_chat()
        except ChatLimitError as error:
            print(f"WOS: {error}")
            return

        print(f"WOS: Active session is now {session.session_id}.")


def clear_chat():
    messages = session.chat.get("messages", [])

    if not messages:
        print("WOS: This chat has no messages to clear.")
        return

    title = session.chat.get("title", "Untitled")

    if not ask_yes_no(
        f"WOS: Clear all {len(messages)} message(s) in '{title}' "
        f"({session.session_id})? This cannot be undone. (y/n): "
    ):
        print("WOS: Clear cancelled.")
        return

    try:
        session.clear()
    except (OSError, ValueError) as error:
        print(f"WOS: Could not clear the conversation: {error}")
        return

    print("WOS: Conversation cleared.")


def manage_chat(arguments):
    parts = arguments.strip().split(maxsplit=1)

    if not parts:
        index = select_option(
            [label for label, _ in CHAT_ACTIONS],
            title="Chat Actions",
            prompt="WOS: What would you like to do? (Enter or 0 to cancel): ",
            reader=input,
            writer=print,
        )

        if index is None:
            print("WOS: Cancelled. Nothing was changed.")
            return

        action = CHAT_ACTIONS[index][1]
        remainder = ""
    else:
        action = parts[0].lower()
        remainder = parts[1].strip() if len(parts) > 1 else ""

    if action in ("list", "clear") and remainder:
        print(
            f"WOS: '/chat {action}' does not take any arguments. "
            "Type /help chat for usage."
        )
    elif action == "list":
        show_chats()
    elif action == "switch":
        switch_chat_command(remainder)
    elif action == "rename":
        rename_chat(remainder)
    elif action == "delete":
        delete_chat_command(remainder)
    elif action == "clear":
        clear_chat()
    else:
        matches = difflib.get_close_matches(
            action, CHAT_SUBCOMMANDS, n=1, cutoff=0.6
        )
        hint = f" Did you mean /chat {matches[0]}?" if matches else ""

        print(
            f"WOS: Unknown command '/chat {action}'.{hint} "
            "Type /help chat to see the subcommands."
        )


def show_context():
    info = session.context_info()

    print()
    print("WOS Context")
    print("-----------")
    print(f"Session:             {info['session_id']}")
    print(f"Saved messages:      {info['saved_messages']}")
    print(f"Messages in context: {info['messages_in_context']}")
    print(f"Context limit:       {info['context_limit']}")
    print()


def show_status():
    info = session.context_info()

    print()
    print("WOS Status")
    print("----------")
    print(f"Character:      {session.character_name}")
    print(f"Provider:       {session.provider}")
    print(f"Model:          {session.model}")
    print(f"Session:        {session.session_id}")
    print(f"Saved chats:    {len(load_index())}")
    print(f"Saved memories: {len(session.memory)} item(s)")
    print(
        f"Context:        {info['messages_in_context']}"
        f"/{info['context_limit']} messages"
    )
    print()


def refresh_memory_context():
    session.refresh_system_prompt()
    session.messages = session.create_messages()


def manage_memory(arguments):
    arguments = arguments.strip()

    if not arguments:
        show_memory(session.memory)
        return

    action, separator, remainder = arguments.partition(" ")
    action = action.lower()
    remainder = remainder.strip()

    if action == "edit":
        if not separator or not remainder:
            print("WOS: Usage: /memory edit <key> <new_value>")
            print("Use /memory to view saved memory keys.")
            return

        matches = []

        for item in session.memory:
            key = item.get("key", "")
            displayed_key = key.replace("_", " ")

            if remainder.startswith(displayed_key + " "):
                value = remainder[len(displayed_key):].strip()

                if value:
                    matches.append((key, value))

        if matches:
            key, new_value = max(
                matches,
                key=lambda match: len(match[0]),
            )
        else:
            parts = remainder.split(maxsplit=1)

            if len(parts) != 2 or not parts[1].strip():
                print("WOS: Usage: /memory edit <key> <new_value>")
                print("Use /memory to view saved memory keys.")
                return

            key, new_value = parts[0], parts[1].strip()

        result = edit_memory(session.memory, key, new_value)
        print(f"WOS: {result}")

        if result == "Memory updated successfully.":
            refresh_memory_context()

        return

    if action == "delete":
        if not separator or not remainder:
            print("WOS: Usage: /memory delete <key>")
            print("Use /memory to view saved memory keys.")
            return

        target = remainder

        if not ask_yes_no(
            f"WOS: Delete memory '{target}' permanently? (y/n): "
        ):
            print("WOS: Deletion cancelled.")
            return

        result = forget_memory(session.memory, f"forget that {target}")
        print(f"WOS: {result}")

        failed = (
            result is None
            or "couldn't find" in result.lower()
            or "couldn't save" in result.lower()
        )

        if not failed:
            refresh_memory_context()

        return

    if action == "clear":
        if not ask_yes_no(
            "WOS: Delete ALL saved memories permanently? (y/n): "
        ):
            print("WOS: Memory clearing cancelled.")
            return

        result = clear_memory(session.memory, confirmed=True)
        print(f"WOS: {result}")

        if result == "All saved memories have been cleared.":
            refresh_memory_context()

        return

    print("WOS: Unknown memory command.")
    print("Use /memory, /memory edit, /memory delete, or /memory clear.")


def ask_for_model(provider):
    """Ask which model to use with a provider. Returns None if cancelled."""
    print(f"WOS: Provider '{provider}' needs a model.")
    answer = ask(f"WOS: Model name for {provider} (Enter to cancel): ")

    if answer is None or not answer.strip():
        return None

    return answer.strip()


def fetch_installed_models():
    """Ask Ollama which models are installed.

    Returns (models, None) on success or (None, reason) if Ollama could not
    be asked.
    """
    details, reason = fetch_installed_model_details()

    if details is None:
        return None, reason

    return [name for name, _ in details], None


def fetch_installed_model_details():
    """Like fetch_installed_models(), but returns (name, size) pairs."""
    try:
        return list_installed_model_details(), None
    except OllamaUnavailable as error:
        return None, str(error)


def ask_when_ollama_unavailable(reason):
    """Explain that Ollama cannot be reached and offer a way forward.

    Returns "retry", "manual", or None if the user cancelled.
    """
    print(f"WOS: Could not get the list of installed models: {reason}")
    print(
        "Make sure Ollama is running (start the Ollama app, or run: "
        "ollama serve), then choose Try again."
    )
    print("If Ollama is not installed yet, you can get it from https://ollama.com")

    index = select_option(
        [
            "Try again",
            "Type a model name myself "
            "(WOS cannot check it while Ollama is unavailable)",
        ],
        title="Ollama Unavailable",
        prompt="WOS: Number of your choice (Enter or 0 to cancel): ",
        reader=input,
        writer=print,
    )

    if index is None:
        return None

    return "retry" if index == 0 else "manual"


def check_ollama_model_choice(provider, model):
    """Check a model against what Ollama has installed.

    Returns a problem message if the model is known not to be installed.
    Returns None if it is fine, or if it could not be checked (a warning is
    shown in that case, since Ollama may simply not be running).
    """
    if provider != "ollama":
        return None

    try:
        model = validate_model_name(model)
    except ValueError:
        return None  # the session explains badly formed names

    installed, unavailable = fetch_installed_models()

    if installed is None:
        print(
            f"WOS: Could not check which models Ollama has installed "
            f"({unavailable}) so '{model}' was not verified."
        )
        return None

    return check_ollama_model(model, installed)


def show_current_settings():
    print(f"WOS: Current provider: {session.provider}")
    print(f"Current model: {session.model}")


def print_chat_scope():
    print(f"WOS: This applies to the current chat ({session.session_id}) only.")
    print(
        f"New chats use your defaults ({session.default_provider} / "
        f"{session.default_model}). Change those with /defaults."
    )


def ask_model_name(provider, installed):
    """Ask the user to type a model name. Returns None if they cancel."""
    if provider != "ollama":
        print(
            "WOS: Type the exact model name. WOS can check that it is "
            "written correctly, but not that your provider offers it."
        )

    while True:
        answer = ask("WOS: Model name (Enter to cancel): ")

        if answer is None or not answer.strip():
            return None

        try:
            model = validate_model_name(answer)
        except ValueError as error:
            print(f"WOS: {error}")
            continue

        if provider == "ollama" and installed is not None:
            problem = check_ollama_model(model, installed)

            if problem:
                print(f"WOS: {problem}")
                continue

        return model


def choose_provider():
    """Numbered menu of providers. Returns the provider or None."""
    show_current_settings()

    providers = ["cloud", "ollama"] + sorted(SUPPORTED_PROVIDERS - {"cloud", "ollama"})
    labels = [
        f"{name} (current)" if name == session.provider else name
        for name in providers
    ]

    index = select_option(
        labels,
        title="Providers",
        prompt="WOS: Number of the provider to use (Enter or 0 to cancel): ",
        reader=input,
        writer=print,
    )

    return None if index is None else providers[index]


def choose_model(provider):
    """Numbered menu of models for a provider. Returns the model or None.

    For Ollama these are the models actually installed. For other providers
    they are model names WOS already knows about, plus an Other choice to
    type any name. Model names are never made up.
    """
    installed = None
    sizes = {}
    cloud_listing = None

    if provider != "ollama":
        cloud_listing = ModelCatalog().list(provider, session.known_models(provider), refresh=True)
        if cloud_listing["warning"]:
            print(f"WOS: {cloud_listing['warning']}")

    if provider == "ollama":
        while True:
            details, unavailable = fetch_installed_model_details()

            if details is not None:
                installed = [name for name, _ in details]
                sizes = dict(details)
                break

            choice = ask_when_ollama_unavailable(unavailable)

            if choice is None:
                return None

            if choice == "manual":
                break

        if installed is not None and not installed:
            print("WOS: Ollama has no models installed yet.")
            print("Install one first, for example: ollama pull <model>")
            return None

    models = ([item["id"] for item in cloud_listing["models"]] if cloud_listing else
              menu_models(provider, installed, session.known_models(provider)))
    labels = [
        label_model(
            model, provider, session.provider, session.model,
            session.default_provider, session.default_model,
            size_bytes=sizes.get(model),
        )
        for model in models
    ]

    # With a real installed list, only installed models make sense, so there
    # is no Other choice. Otherwise let the user type a name.
    allow_other = not (provider == "ollama" and installed is not None)

    if not models:
        print("WOS: Type the name of the model to use. It cannot be checked until Ollama is running."
              if provider == "ollama" else
              "WOS: No models were discovered. Type an exact model name, or cancel.")
        return ask_model_name(provider, installed)

    if allow_other:
        labels.append(OTHER_LABEL)

    print(f"WOS: Models for {provider}:")

    if provider == "ollama" and installed is not None:
        print("These are installed on this computer.")
    elif cloud_listing and cloud_listing["source"] in ("live", "cached"):
        print(f"Models listed by {PROVIDERS[provider]['label']}."
              if cloud_listing["source"] == "live" else "Using the cached provider model list.")
        print("Listing a model does not guarantee account quota or access.")
    else:
        print("These are model names WOS already knows "
              "(from your settings and chats).")
        print("WOS cannot check them with your provider, so a typo only "
              "shows up when you chat.")
        print("To use a different model, choose Other and type its exact name.")

    index = select_option(
        labels,
        title="Available Models",
        prompt="WOS: Number of the model to use (Enter or 0 to cancel): ",
        reader=input,
        writer=print,
    )

    if index is None:
        return None

    if index >= len(models):
        return ask_model_name(provider, installed)

    return models[index]


def apply_provider_change(provider, model, check=True):
    if provider == session.provider and model == session.model:
        print(
            f"WOS: Already using {provider} with model {model}. "
            "Nothing was changed."
        )
        return

    if check:
        problem = check_ollama_model_choice(provider, model)

        if problem:
            print(f"WOS: {problem}")
            print("WOS: Provider was not changed.")
            return

    result = session.change_provider(provider, model)
    print(f"WOS: {result}")

    if session.provider == provider and session.model == model.strip():
        print_chat_scope()


def apply_model_change(model, check=True):
    if model.strip() == session.model:
        print(f"WOS: Already using model {session.model}. Nothing was changed.")
        return

    if check:
        problem = check_ollama_model_choice(session.provider, model)

        if problem:
            print(f"WOS: {problem}")
            print("WOS: Model was not changed.")
            return

    result = session.change_model(model)
    print(f"WOS: {result}")

    if session.model == model.strip():
        print_chat_scope()


def manage_provider(arguments):
    arguments = arguments.strip()

    if not arguments:
        provider = choose_provider()

        if provider is None:
            print("WOS: Provider was not changed.")
            return

        model = choose_model(provider)

        if model is None:
            print("WOS: Provider was not changed.")
            return

        apply_provider_change(provider, model, check=False)
        return

    parts = arguments.split()

    if len(parts) > 2:
        print("WOS: Usage: /provider [provider] [model]")
        return

    provider = parts[0].lower()
    model = parts[1] if len(parts) == 2 else None

    if provider not in SUPPORTED_PROVIDERS:
        print(f"WOS: {session.change_provider(provider)}")
        return

    if model is None:
        if provider == session.provider:
            model = session.model
        else:
            # Reuse a model that suits the provider if there is one and it
            # is still usable. Otherwise let the user pick.
            model = session.suggest_model(provider)

            if model is not None:
                problem = check_ollama_model_choice(provider, model)

                if problem:
                    print(f"WOS: {problem}")
                    model = None

            if model is None:
                model = choose_model(provider)

                if model is None:
                    print("WOS: Provider was not changed.")
                    return

                apply_provider_change(provider, model, check=False)
                return

    apply_provider_change(provider, model)


def manage_model(arguments):
    arguments = arguments.strip()

    if not arguments:
        show_current_settings()
        model = choose_model(session.provider)

        if model is None:
            print("WOS: Model was not changed.")
            return

        apply_model_change(model, check=False)
        return

    apply_model_change(arguments)


def show_defaults_usage():
    print("Usage: /defaults provider <provider> [model]")
    print("       /defaults model <model>")
    print("       /defaults character <id>")


def manage_defaults(arguments):
    arguments = arguments.strip()

    if not arguments:
        print("WOS Default Settings")
        print("--------------------")
        print(f"Default provider:  {session.default_provider}")
        print(f"Default model:     {session.default_model}")
        print(f"Default character: {session.default_character_id}")
        print("These are used when you start a new chat with /new.")
        print("Existing chats keep their own settings.")
        show_defaults_usage()
        return

    parts = arguments.split()
    action = parts[0].lower()
    values = parts[1:]

    if action == "provider" and 1 <= len(values) <= 2:
        provider = values[0].lower()
        model = values[1] if len(values) == 2 else None

        if (
            provider in SUPPORTED_PROVIDERS
            and model is None
            and provider != session.default_provider
            and session.suggest_model(provider) is None
        ):
            model = ask_for_model(provider)

            if model is None:
                print("WOS: Default settings were not changed.")
                return

        result = session.save_defaults(provider=provider, model=model)

    elif action == "model" and len(values) == 1:
        result = session.save_defaults(model=values[0])

    elif action == "character" and len(values) == 1:
        result = session.save_defaults(character=values[0])

    else:
        print("WOS: Usage: /defaults provider <provider> [model]")
        print("       /defaults model <model>")
        print("       /defaults character <id>")
        return

    print(f"WOS: {result}")

    if result == "Default settings saved.":
        print(
            "WOS: New chats will use these settings. "
            "The current chat is unchanged."
        )


def start_new_chat():
    """Start a new chat. If the chat limit is reached, ask the user which
    saved chat to delete instead of deleting anything automatically."""
    while True:
        try:
            notes = session.new_chat()
        except ChatLimitError as error:
            print(f"WOS: {error}")

            if not free_up_chat_slot():
                print("WOS: No new chat was created.")
                return

            continue

        for note in notes:
            print(f"WOS: {note}")

        print(f"WOS: Started {session.session_id}.")
        return


def free_up_chat_slot():
    """Let the user pick one saved chat to delete. Returns True if a chat
    was deleted."""
    chats = load_index()

    if not chats:
        return False

    target_id = choose_chat(
        prompt="WOS: Number of the chat to delete (Enter or 0 to cancel): ",
        cancelled_message=None,
    )

    if target_id is None:
        return False

    target = next(
        (chat for chat in chats if chat.get("session_id") == target_id), {}
    )
    title = target.get("title", "Untitled")

    if not ask_yes_no(
        f"WOS: Delete '{title}' ({target_id}) permanently? (y/n): "
    ):
        print("WOS: Deletion cancelled.")
        return False

    was_active = target_id == session.session_id

    try:
        deleted = delete_chat(target_id)
    except (OSError, ValueError) as error:
        print(f"WOS: Could not delete chat: {error}")
        return False

    if deleted is False:
        print(f"WOS: Chat '{target_id}' could not be deleted.")
        return False

    print(f"WOS: Deleted {target_id}.")

    if was_active:
        # The deleted chat is gone, so move to another saved chat. If none
        # are left, the new chat that follows becomes the active one.
        remaining_chats = load_index()

        if remaining_chats:
            session.switch_chat(remaining_chats[-1].get("session_id"))

    return True


# ------------------------------
# Main loop
# ------------------------------

show_header()

def choose_new_cloud_key_model():
    if session.provider != "ollama" and api_key(session.provider):
        import hashlib
        import json
        from core.config import PROJECT_ROOT
        from core.conversation import _atomic_write_json
        setup_name = "cloud_model_setup.json" if session.provider == "cloud" else f"{session.provider}_model_setup.json"
        setup_path = PROJECT_ROOT / "data" / setup_name
        fingerprint = hashlib.sha256(api_key(session.provider).encode()).hexdigest()
        try:
            selected_key = json.loads(setup_path.read_text("utf-8")).get("key_hash")
        except (OSError, ValueError, AttributeError):
            selected_key = None
        if selected_key != fingerprint:
            print(f"WOS: A new {PROVIDERS[session.provider]['label']} key is configured. Choose a model for this chat.")
            choice = choose_model(session.provider)
            if choice is not None:
                apply_model_change(choice, check=False)
                if session.model == choice:
                    try:
                        _atomic_write_json(setup_path, {"key_hash": fingerprint})
                    except OSError:
                        pass

# Optional voice. speak() never raises and never blocks the prompt.
try:
    tts_speaker = FishAudioSpeaker(load_settings().get("tts"), notify=print)
except (ValueError, OSError):
    tts_speaker = FishAudioSpeaker(None, notify=print)

# Shared access to this one session and speaker for the desktop overlay.
overlay_backend = OverlayBackend(lambda: session, tts_speaker, notify=print)
tts_speaker.listener = overlay_backend.on_speech_event
overlay_server = None

if "--overlay" in sys.argv:
    try:
        # A controlled restart retains the endpoint for browser clients too.
        previous_port = os.environ.pop("WOS_RESTART_PORT", "")
        overlay_server = OverlayServer(
            overlay_backend,
            port=int(previous_port) if previous_port.isdigit() else overlay_port(load_settings().get("overlay")),
            token=os.environ.pop("WOS_RESTART_TOKEN", None),
        )
        overlay_server.start()
    except (ValueError, OSError) as error:
        overlay_server = None
        print(f"WOS: The desktop overlay could not start: {error}")

def run_loop():
    while True:
        # The previous command is finished; let the overlay use the session.
        overlay_backend.release_command()

        try:
            message = input("You: ").strip()
        except KeyboardInterrupt:
            if tts_speaker.active:
                overlay_backend.stop_speech()
                print("\nWOS: Speech stopped.")
                continue
            print("\nWOS: Goodbye.")
            break
        except EOFError:
            print("\nWOS: Goodbye.")
            break

        if not message:
            continue

        overlay_backend.hold_command()

        command = message.lower()

        if command.startswith(("/new", "/switch", "/chat switch", "/chat clear", "/clear", "/delete", "/chat delete", "/character switch", "/model", "/provider")):
            overlay_backend.stop_speech()

        if command == "/stop":
            overlay_backend.stop_speech()
            print("WOS: Speech stopped.")
            continue

        if command in ("/exit", "/quit"):
            print("WOS: Conversation saved.")
            break

        if command == "/help":
            show_help()
            continue

        if command.startswith("/help "):
            show_command_help(message[len("/help "):])
            continue

        if command == "/restart":
            print("WOS: Restarting...")
            tts_speaker.shutdown()

            if overlay_server is not None:
                os.environ["WOS_RESTART_PORT"] = str(overlay_server.port)
                os.environ["WOS_RESTART_TOKEN"] = overlay_server.token
                overlay_server.close()  # free the port for the new process

            restart_args = [sys.executable] + sys.argv

            if os.name == "nt":
                # os.execv on Windows does not quote arguments that contain spaces
                # and returns control to the shell immediately. Start the new
                # process with subprocess instead and wait for it to finish.
                process = subprocess.Popen(restart_args)

                try:
                    exit_code = process.wait()
                except KeyboardInterrupt:
                    # Ctrl+C is delivered to the new process as well. Give it a
                    # few seconds to exit on its own, then stop it.
                    try:
                        exit_code = process.wait(timeout=5)
                    except (subprocess.TimeoutExpired, KeyboardInterrupt):
                        process.terminate()
                        exit_code = process.wait()

                raise SystemExit(exit_code)

            os.execv(sys.executable, restart_args)

        if command == "/new":
            start_new_chat()
            continue

        if command == "/chat":
            manage_chat("")
            continue

        if command.startswith("/chat "):
            manage_chat(message[len("/chat "):])
            continue

        if command == "/chats":
            show_chats()
            continue

        if command == "/character":
            manage_character("")
            continue

        if command.startswith("/character "):
            manage_character(message[len("/character "):])
            continue

        if command == "/switch":
            switch_chat_command("")
            continue

        if command.startswith("/switch "):
            switch_chat_command(message[len("/switch "):])
            continue

        if command == "/rename":
            rename_chat("")
            continue

        if command.startswith("/rename "):
            rename_chat(message[len("/rename "):])
            continue

        if command == "/delete":
            delete_chat_command("")
            continue

        if command.startswith("/delete "):
            delete_chat_command(message[len("/delete "):])
            continue

        if command == "/provider":
            manage_provider("")
            continue

        if command.startswith("/provider "):
            manage_provider(message[len("/provider "):])
            continue

        if command == "/model":
            manage_model("")
            continue

        if command.startswith("/model "):
            manage_model(message[len("/model "):])
            continue

        if command == "/defaults":
            manage_defaults("")
            continue

        if command.startswith("/defaults "):
            manage_defaults(message[len("/defaults "):])
            continue

        if command == "/forget":
            print("WOS: Usage: /forget <memory>")
            print("Example: /forget that I am learning Python")
            continue

        if command.startswith("/forget "):
            target = message[len("/forget "):].strip()

            if not target:
                print("WOS: Usage: /forget <memory>")
                continue

            if target.lower().startswith(("that ", "about ")):
                forget_request = f"Forget {target}"
            else:
                forget_request = f"Forget that {target}"

            reply = overlay_backend.send_message(forget_request, "terminal")

            if reply is None:
                print("WOS: The memory operation did not return a response.")
            else:
                print(f"{session.character_name}: {reply}")
                overlay_backend.speak_terminal_reply(reply)

            continue

        if command == "/memory":
            show_memory(session.memory)
            continue

        if command.startswith("/memory "):
            manage_memory(message[len("/memory "):])
            continue

        if command == "/status":
            show_status()
            continue

        if command == "/context":
            show_context()
            continue

        if command == "/clear":
            clear_chat()
            continue

        problem = check_unrecognized_command(message)

        if problem:
            print(problem)
            continue

        reply = overlay_backend.send_message(message, "terminal")

        if reply is None:
            print("WOS: The model did not return a response.")
            continue

        print(f"{session.character_name}: {reply}")
        overlay_backend.speak_terminal_reply(reply)


try:
    overlay_backend.hold_command()
    try:
        choose_new_cloud_key_model()
    finally:
        overlay_backend.release_command()
    run_loop()
except KeyboardInterrupt:
    print("\nWOS: Interrupted. Goodbye.")
finally:
    tts_speaker.shutdown()
    overlay_backend.release_command()
    if overlay_server is not None:
        overlay_server.close()
