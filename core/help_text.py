import difflib
import textwrap


# Help for every WOS command, in one place. /help and /help <command> are
# built from this table, so a command only needs to be described once.
#
# Each entry has:
#   summary      one line shown in the /help list
#   purpose      what the command is for
#   usage        the ways to type it ([ ] means optional, < > means required)
#   subcommands  (name, usage, description) for commands that have them
#   examples     things the user can copy
#   notes        extra lines of explanation (optional)
#   aliases      (older command, what it maps to) shown under the canonical
#                command (optional)
#   canonical    set for aliases: the command this one is another name for
#
# Keep the commands here in sync with KNOWN_COMMANDS in core/cli.py. A test
# checks that they match.

_MENU_NOTE = (
    "Numbered lists: type a number and press Enter. Press Enter or type 0 "
    "to cancel; nothing is changed."
)


def _alias(canonical, usage, example):
    return {
        "summary": f"Alias for {canonical}",
        "canonical": canonical,
        "purpose": (
            f"An older name for {canonical}, kept so existing habits keep "
            f"working. Use {canonical} in new instructions."
        ),
        "usage": usage,
        "subcommands": [],
        "examples": example,
    }


HELP_TOPICS = {
    "/stop": {
        "summary": "Stop current and pending speech",
        "purpose": "Stops the current spoken reply and cancels pending speech without muting future replies.",
        "usage": ["/stop"], "subcommands": [], "examples": ["/stop"],
        "notes": ["Ctrl+C at the main prompt also stops active speech. With no speech active it exits WOS."],
    },
    "/character": {
        "summary": "Show or change the character",
        "purpose": (
            "Shows which character you are talking to, lists the characters "
            "WOS can load, or changes the character used by the current "
            "chat. If a file in the characters folder cannot be used, the "
            "list says which file and why. The character that new chats "
            "start with is set with /defaults character."
        ),
        "usage": [
            "/character",
            "/character list",
            "/character switch [id]",
        ],
        "subcommands": [
            (
                "list",
                "/character list",
                "List the available characters, and any files that were "
                "skipped, with the reason",
            ),
            (
                "switch",
                "/character switch [id]",
                "Switch the current chat's character. Without an id, "
                "choose from a numbered list.",
            ),
        ],
        "examples": ["/character switch", "/character switch nexis"],
        "notes": [_MENU_NOTE],
    },
    "/chat": {
        "summary": "Manage saved chats",
        "purpose": (
            "Everything to do with your saved chats: see them, switch "
            "between them, rename them, delete them, or clear the current "
            "one. Type /chat on its own to pick an action from a numbered "
            "menu. When a chat is needed and you don't give its id, WOS "
            "shows your chats as a numbered list."
        ),
        "usage": [
            "/chat",
            "/chat list",
            "/chat switch [id]",
            "/chat rename [id] [new title]",
            "/chat delete [id]",
            "/chat clear",
        ],
        "subcommands": [
            ("list", "/chat list", "List saved chats"),
            (
                "switch",
                "/chat switch [id]",
                "Switch to a saved chat. Without an id, choose from a "
                "numbered list.",
            ),
            (
                "rename",
                "/chat rename [id] [new title]",
                "Rename a saved chat. Without an id, choose from a list. "
                "Without a title, WOS asks for one.",
            ),
            (
                "delete",
                "/chat delete [id]",
                "Delete a saved chat after you confirm. Without an id, "
                "choose from a numbered list.",
            ),
            (
                "clear",
                "/chat clear",
                "Remove every message from the current chat after you "
                "confirm. Saved memories are kept.",
            ),
        ],
        "examples": [
            "/chat",
            "/chat switch",
            "/chat rename session_002 Python practice",
            "/chat delete session_003",
        ],
        "notes": [
            _MENU_NOTE,
            "Deleting and clearing always ask you to confirm first.",
        ],
        "aliases": [
            ("/chats", "/chat list"),
            ("/switch", "/chat switch"),
            ("/rename", "/chat rename"),
            ("/delete", "/chat delete"),
            ("/clear", "/chat clear"),
        ],
    },
    "/chats": _alias("/chat list", ["/chats"], ["/chats"]),
    "/clear": _alias("/chat clear", ["/clear"], ["/clear"]),
    "/context": {
        "summary": "Show current context usage",
        "purpose": (
            "Shows how many saved messages are included in the context "
            "sent to the model, and the limit."
        ),
        "usage": ["/context"],
        "subcommands": [],
        "examples": ["/context"],
    },
    "/defaults": {
        "summary": "Show or change defaults for new chats",
        "purpose": (
            "Shows or changes the provider, model, and character that new "
            "chats (/new) start with. Chats you already have keep their "
            "own settings."
        ),
        "usage": [
            "/defaults",
            "/defaults provider <provider> [model]",
            "/defaults model <model>",
            "/defaults character <id>",
        ],
        "subcommands": [
            (
                "provider",
                "/defaults provider <provider> [model]",
                "Change the default provider. A provider needs a model, "
                "so WOS asks for one when it has none to suggest.",
            ),
            ("model", "/defaults model <model>",
             "Change the default model"),
            ("character", "/defaults character <id>",
             "Change the default character"),
        ],
        "examples": [
            "/defaults",
            "/defaults provider ollama llama3",
            "/defaults character nexis",
        ],
    },
    "/delete": _alias("/chat delete", ["/delete [id]"], ["/delete session_002"]),
    "/exit": {
        "summary": "Close WOS",
        "purpose": "Saves the conversation and closes WOS. /quit does the same.",
        "usage": ["/exit"],
        "subcommands": [],
        "examples": ["/exit"],
    },
    "/forget": {
        "summary": "Forget a saved memory",
        "purpose": (
            "Asks WOS to forget something it remembered about you. You can "
            "also say it in a normal sentence, such as \"Forget that ...\"."
        ),
        "usage": ["/forget <memory>"],
        "subcommands": [],
        "examples": ["/forget that I am learning Python"],
    },
    "/help": {
        "summary": "Show commands and how to use them",
        "purpose": (
            "Lists every command, or explains one command in detail: what "
            "it is for, how to type it, and its subcommands."
        ),
        "usage": [
            "/help",
            "/help <command>",
            "/help <command> <subcommand>",
        ],
        "subcommands": [],
        "examples": ["/help chat", "/help /model", "/help chat switch"],
    },
    "/memory": {
        "summary": "Show or manage saved memories",
        "purpose": (
            "Shows what WOS has remembered about you, or lets you edit, "
            "delete, or clear memories."
        ),
        "usage": [
            "/memory",
            "/memory edit <key> <value>",
            "/memory delete <key>",
            "/memory clear",
        ],
        "subcommands": [
            ("edit", "/memory edit <key> <value>", "Edit a saved memory"),
            ("delete", "/memory delete <key>",
             "Delete a saved memory after you confirm"),
            ("clear", "/memory clear",
             "Delete all saved memories after you confirm"),
        ],
        "examples": ["/memory", "/memory delete favorite_color"],
        "notes": [
            "WOS also saves memories from sentences such as "
            "\"Remember that ...\", \"Keep in mind that ...\", and "
            "\"Don't forget that ...\".",
        ],
    },
    "/model": {
        "summary": "Show or change the model",
        "purpose": (
            "Shows the model the current chat uses and lets you change it. "
            "Type /model on its own to choose from a numbered list of "
            "models for the current provider, or type the name yourself. "
            "The change applies to the current chat only; use /defaults "
            "model to choose what new chats start with."
        ),
        "usage": ["/model", "/model <model>"],
        "subcommands": [],
        "examples": ["/model", "/model llama3"],
        "notes": [
            "For Ollama, the list shows the models installed on this "
            "computer, with their sizes when Ollama reports them, and WOS "
            "only accepts one of those. If Ollama is not running, WOS "
            "explains how to start it and lets you try again, type a name "
            "yourself, or cancel. For cloud providers, WOS fetches model IDs using the corresponding configured key. If discovery fails, it uses a cached list or model names "
            "WOS already knows (your chats, your defaults, and "
            "\"suggested_models\" in config.json) plus an Other choice to "
            "type any name. Listing a model does not guarantee account access or quota. Manual names are checked only to ensure that "
            "they are well formed.",
            _MENU_NOTE,
        ],
    },
    "/new": {
        "summary": "Start a new chat",
        "purpose": (
            "Starts a new chat with the defaults (see /help defaults). If "
            "you already have the maximum number of saved chats, WOS shows "
            "them so you can choose one to delete first. Nothing is "
            "deleted without your confirmation."
        ),
        "usage": ["/new"],
        "subcommands": [],
        "examples": ["/new"],
    },
    "/provider": {
        "summary": "Show or change the provider",
        "purpose": (
            "Shows the current provider and lets you change it. Type "
            "/provider on its own to choose from a numbered list of "
            "providers, then from a numbered list of models for the one "
            "you picked. The change applies to the current chat only; use "
            "/defaults provider to choose what new chats start with."
        ),
        "usage": ["/provider", "/provider <provider> [model]"],
        "subcommands": [],
        "examples": ["/provider", "/provider ollama llama3", "/provider cloud"],
        "notes": [
            "A provider needs a model. If you give only the provider, WOS "
            "reuses a model that suits it when it can, and otherwise shows "
            "the model list. Nothing changes until you have chosen both.",
            "For Ollama, the list shows the models installed on this "
            "computer, with their sizes when available. If Ollama is not "
            "running, you can try again or cancel. Cloud providers fetch model lists, with a cached or saved fallback. It also "
            "shows model names WOS already knows, plus an Other choice to "
            "type any name.",
            _MENU_NOTE,
        ],
    },
    "/quit": _alias("/exit", ["/quit"], ["/quit"]),
    "/rename": _alias(
        "/chat rename",
        ["/rename [id] [new title]"],
        ["/rename session_002 Python practice"],
    ),
    "/restart": {
        "summary": "Restart WOS",
        "purpose": (
            "Closes and reopens WOS, which reloads its configuration "
            "files. Your chats are saved."
        ),
        "usage": ["/restart"],
        "subcommands": [],
        "examples": ["/restart"],
    },
    "/status": {
        "summary": "Show current WOS status",
        "purpose": (
            "Shows the character, provider, model, and session, plus how "
            "many chats and memories are saved."
        ),
        "usage": ["/status"],
        "subcommands": [],
        "examples": ["/status"],
    },
    "/switch": _alias(
        "/chat switch", ["/switch [id]"], ["/switch", "/switch session_002"]
    ),
}


_WIDTH = 74


def command_names():
    """All commands that have help, in alphabetical order."""
    return sorted(HELP_TOPICS, key=str.lower)


def normalize_topic(text):
    """Turn 'chat', 'CHAT' or '/chat' into '/chat'. Returns None if the text
    is not a command that has help."""
    text = (text or "").strip().lower()

    if not text:
        return None

    if not text.startswith("/"):
        text = "/" + text

    return text if text in HELP_TOPICS else None


def suggest_topic(text):
    """Return the closest command name to what was typed, or None."""
    text = (text or "").strip().lower()

    if not text:
        return None

    if not text.startswith("/"):
        text = "/" + text

    matches = difflib.get_close_matches(text, HELP_TOPICS, n=1, cutoff=0.6)

    return matches[0] if matches else None


def overview_lines():
    """The /help list: every command, alphabetically, with a summary."""
    names = command_names()
    width = max(len(name) for name in names) + 2

    lines = ["", "WOS commands:"]

    for name in names:
        lines.append(f"{name:<{width}}{HELP_TOPICS[name]['summary']}")

    lines.append("")
    lines.append("Type /help <command> to learn more, for example /help chat.")
    lines.append("")

    return lines


def _wrapped(text, indent="  "):
    return textwrap.wrap(
        text,
        width=_WIDTH,
        initial_indent=indent,
        subsequent_indent=indent,
    )


def help_lines(arguments):
    """The text for '/help <arguments>', or None if there is nothing to show.

    'chat' explains a command. 'chat switch' explains one subcommand.
    """
    words = (arguments or "").split()

    if not words or len(words) > 2:
        return None

    topic = normalize_topic(words[0])

    if topic is None:
        return None

    entry = HELP_TOPICS[topic]

    if len(words) == 2:
        return _subcommand_lines(topic, entry, words[1].lower())

    lines = ["", f"{topic} - {entry['summary']}", "", "Purpose:"]
    lines += _wrapped(entry["purpose"])

    lines += ["", "Usage:"]
    lines += [f"  {usage}" for usage in entry["usage"]]

    if entry["subcommands"]:
        width = max(len(name) for name, _, _ in entry["subcommands"]) + 2
        lines += ["", "Subcommands:"]

        for name, _, description in entry["subcommands"]:
            lines += textwrap.wrap(
                description,
                width=_WIDTH,
                initial_indent=f"  {name:<{width}}",
                subsequent_indent="  " + " " * width,
            )

    if entry.get("aliases"):
        width = max(len(old) for old, _ in entry["aliases"]) + 2
        lines += ["", "Older commands that still work:"]
        lines += [
            f"  {old:<{width}}same as {new}" for old, new in entry["aliases"]
        ]

    lines += ["", "Examples:"]
    lines += [f"  {example}" for example in entry["examples"]]

    for note in entry.get("notes", []):
        lines += [""] + _wrapped(note, indent="")

    if entry.get("canonical"):
        lines += ["", f"See also: /help {entry['canonical'].split()[0][1:]}"]

    lines.append("")

    return lines


def _subcommand_lines(topic, entry, name):
    for sub_name, usage, description in entry["subcommands"]:
        if sub_name == name:
            lines = ["", f"{usage}", ""]
            lines += _wrapped(description)
            lines.append("")
            return lines

    return None
