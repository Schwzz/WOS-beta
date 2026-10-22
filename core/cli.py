import builtins
import difflib
import re


# Every slash command WOS understands. Keep this in sync with show_help()
# and the command handling in start.py.
KNOWN_COMMANDS = (
    "/new",
    "/chat",
    "/chats",
    "/switch",
    "/rename",
    "/delete",
    "/clear",
    "/character",
    "/provider",
    "/model",
    "/defaults",
    "/memory",
    "/forget",
    "/context",
    "/status",
    "/stop",
    "/restart",
    "/help",
    "/exit",
    "/quit",
)

# Looks like a command word: "/model", "/modle", "/new-chat".
# Text such as "/usr/bin/python" or "/ hello" is not treated as a command.
_COMMAND_WORD = re.compile(r"/[A-Za-z][A-Za-z0-9_-]*")


def read_input(prompt, reader=None):
    """Read one line of input, or return None if the user pressed Ctrl+C
    or the input ended (EOF).

    This lets prompts inside commands be cancelled safely instead of
    crashing WOS with a traceback.
    """
    if reader is None:
        reader = builtins.input

    try:
        return reader(prompt)
    except (EOFError, KeyboardInterrupt):
        builtins.print()
        return None


def confirm(prompt, reader=None):
    """Ask a yes/no question. Only an explicit yes returns True.

    Ctrl+C, EOF, an empty answer, or anything else counts as no.
    """
    answer = read_input(prompt, reader)

    return answer is not None and answer.strip().lower() in ("y", "yes")


_NUMBER = re.compile(r"[0-9]+")


def select_option(
    options,
    title=None,
    prompt=None,
    allow_cancel=True,
    reader=None,
    writer=None,
):
    """Show a numbered menu and let the user pick one item by number.

    options       The items to show. Each one is displayed with str(); line
                  breaks inside a label are flattened so the list stays tidy.
    title         Optional heading, printed with a dashed underline like the
                  other WOS lists.
    prompt        Replaces the default prompt text.
    allow_cancel  If True, pressing Enter or typing 0 cancels. If False, the
                  user must pick a number.
    reader        Function used to read a line. Defaults to input().
    writer        Function used to print a line. Defaults to print().

    Returns the zero-based index of the chosen option, so the caller can use
    options[index]. Returns None if the user cancelled, pressed Ctrl+C, the
    input ended (EOF), or there was nothing to choose from. Invalid input does
    not cancel; the user is told what to enter and asked again.
    """
    options = list(options)

    if writer is None:
        writer = builtins.print

    if not options:
        writer("WOS: There is nothing to choose from.")
        return None

    count = len(options)
    width = len(str(count))

    writer()

    if title:
        writer(title)
        writer("-" * len(title))

    for number, option in enumerate(options, start=1):
        label = " ".join(str(option).split())
        writer(f"{number:>{width}}. {label}")

    writer()

    if prompt is None:
        range_text = "1" if count == 1 else f"1-{count}"

        if allow_cancel:
            prompt = f"WOS: Enter a number ({range_text}, Enter or 0 to cancel): "
        else:
            prompt = f"WOS: Enter a number ({range_text}): "

    if count == 1:
        invalid_message = "WOS: Please enter 1."
    else:
        invalid_message = f"WOS: Please enter a number from 1 to {count}."

    while True:
        answer = read_input(prompt, reader)

        if answer is None:
            return None

        answer = answer.strip()

        if allow_cancel and not answer:
            return None

        # ASCII digits only. Other characters that merely look like digits
        # (for example superscripts) are treated as invalid input.
        if _NUMBER.fullmatch(answer):
            number = int(answer)

            if number == 0 and allow_cancel:
                return None

            if 1 <= number <= count:
                return number - 1

        writer(invalid_message)


def check_unrecognized_command(message):
    """Return an error message if the text looks like a command that WOS
    did not handle, or None if it should be treated as normal chat.

    Call this only after every known command has had its chance to run.
    """
    parts = message.strip().split(maxsplit=1)

    if not parts or not _COMMAND_WORD.fullmatch(parts[0]):
        return None

    word = parts[0].lower()

    if word in KNOWN_COMMANDS:
        return (
            f"WOS: '{word}' was not used correctly. "
            "Type /help to see how to use it."
        )

    matches = difflib.get_close_matches(
        word, KNOWN_COMMANDS, n=1, cutoff=0.6
    )

    hint = f" Did you mean {matches[0]}?" if matches else ""

    return (
        f"WOS: Unknown command '{word}'.{hint} "
        "Type /help to see available commands."
    )
