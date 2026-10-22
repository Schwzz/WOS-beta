import json
import os
import re
import tempfile
from pathlib import Path

from core.character import load_character
from core.provider_registry import PROVIDERS


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

SUPPORTED_PROVIDERS = set(PROVIDERS)

# A model that is known to work with a provider, used when switching to that
# provider and no model has been chosen for it yet. Ollama has no entry because
# the installed models differ on every machine, so the user must pick one.
DEFAULT_PROVIDER_MODELS = {
    "cloud": "gemini-3.5-flash-lite",
}

MAX_MODEL_NAME_LENGTH = 200

# Letters, digits and . _ : / @ + - cover names such as "gemini-3.5-flash-lite",
# "llama3.2:3b" and "hf.co/user/model:Q4_K_M".
_MODEL_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*")

_PLACEHOLDER_CHARACTERS = set("<>{}[]")

_PLACEHOLDER_MODEL_NAMES = {
    "model",
    "name",
    "model_name",
    "modelname",
    "your_model",
    "your_model_name",
    "your_model_here",
    "model_here",
}


def get_suggested_models(config, provider):
    """Model names the user listed for a provider under "suggested_models" in
    config.json, for example {"cloud": ["some-model-id"]}.

    WOS ships no model list of its own, because model names change. Anything
    that is not a valid model name is ignored rather than stopping WOS.
    """
    suggestions = config.get("suggested_models") if isinstance(config, dict) else None
    listed = suggestions.get(provider) if isinstance(suggestions, dict) else None
    models = []

    for model in listed if isinstance(listed, list) else []:
        try:
            model = validate_model_name(model)
        except ValueError:
            continue

        if model not in models:
            models.append(model)

    return models


def format_provider_list():
    """Return the supported providers as a readable, sorted string."""
    return ", ".join(sorted(SUPPORTED_PROVIDERS))


def validate_model_name(model):
    """Return the cleaned model name or raise ValueError.

    This checks the format only. WOS cannot know offline whether a provider
    actually offers the model, but it can reject empty values, placeholders such
    as <model_name>, and anything that cannot be a model name.
    """
    if not isinstance(model, str) or not model.strip():
        raise ValueError("The configured model must be a non-empty string.")

    model = model.strip()

    if len(model) > MAX_MODEL_NAME_LENGTH:
        raise ValueError(
            f"The model name is too long "
            f"(maximum {MAX_MODEL_NAME_LENGTH} characters)."
        )

    if any(character.isspace() for character in model):
        raise ValueError("Model names cannot contain spaces.")

    placeholder_like = (
        any(character in _PLACEHOLDER_CHARACTERS for character in model)
        or model.lower().replace("-", "_") in _PLACEHOLDER_MODEL_NAMES
    )

    if placeholder_like:
        raise ValueError(
            f"'{model}' looks like a placeholder. "
            "Enter the real name of a model."
        )

    if (
        not _MODEL_NAME_PATTERN.fullmatch(model)
        or any(part in (".", "..") for part in model.split("/"))
    ):
        raise ValueError(
            f"'{model}' is not a valid model name. Use only letters, "
            "numbers, and . _ : / @ + - characters."
        )

    return model


def validate_character_choice(character_id):
    """Return the cleaned character ID or raise ValueError.

    The character must have a valid ID and an existing, valid character file.
    """
    if not isinstance(character_id, str) or not character_id.strip():
        raise ValueError("The configured character must be a non-empty string.")

    character_id = character_id.strip()

    try:
        load_character(character_id)
    except FileNotFoundError:
        raise ValueError(
            f"Character '{character_id}' was not found. "
            "Use /character list to see available characters."
        )
    except ValueError as error:
        message = str(error)

        if message.startswith("WLLM: "):
            message = message[len("WLLM: "):]

        raise ValueError(message)

    return character_id


def load_config():
    try:
        # utf-8-sig also accepts files saved with a BOM (some Windows editors).
        with CONFIG_PATH.open("r", encoding="utf-8-sig") as file:
            config = json.load(file)

    except FileNotFoundError as error:
        raise FileNotFoundError(
            f"Configuration file was not found: {CONFIG_PATH}"
        ) from error

    except json.JSONDecodeError as error:
        raise ValueError(
            f"config.json contains invalid JSON "
            f"(line {error.lineno}, column {error.colno})."
        ) from error

    if not isinstance(config, dict):
        raise ValueError("config.json must contain a JSON object.")

    provider = config.get("provider")
    model = config.get("model")
    character = config.get("character")

    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported provider '{provider}'. "
            f"Supported providers: {', '.join(sorted(SUPPORTED_PROVIDERS))}."
        )

    config["model"] = validate_model_name(model)

    if not isinstance(character, str) or not character.strip():
        raise ValueError("The configured character must be a non-empty string.")

    return config


def save_config(config):
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a dictionary.")

    provider = config.get("provider")

    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported provider '{provider}'. "
            f"Supported providers: {', '.join(sorted(SUPPORTED_PROVIDERS))}."
        )

    # Validate everything before touching the file, so invalid input can
    # never overwrite a working configuration.
    config = dict(config)
    config["model"] = validate_model_name(config.get("model"))
    config["character"] = validate_character_choice(config.get("character"))

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=CONFIG_PATH.parent,
            suffix=".tmp",
            delete=False,
        ) as file:
            temp_path = Path(file.name)
            json.dump(config, file, indent=4)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        os.replace(temp_path, CONFIG_PATH)

    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()

    return config


SETTINGS_PATH = PROJECT_ROOT / "settings.json"

DEFAULT_SETTINGS = {
    "terminal": {
        "default_name_color": "white"
    },
    "sessions": {
        "max_chats": 5
    },
    "context": {
        "max_context_messages": 20,
        "max_historical_chats": 4,
        "max_history_chars": 10000
    }
}

# Optional text-to-speech. Kept out of DEFAULT_SETTINGS on purpose: a damaged
# "tts" value must never stop WOS from starting, so it is passed through as
# saved and core.tts falls back to these defaults for anything invalid.
DEFAULT_TTS_SETTINGS = {
    "enabled": False,
    "voice_id": "5a62b02a53954e13ad0d9a90464ef9b0",
    "model": "s2.1-pro-free"
}

# Desktop overlay (WOS-Overlay). Passed through like "tts": a damaged value
# must never stop WOS, so core.overlay validates it and falls back.
DEFAULT_OVERLAY_SETTINGS = {
    "port": 47615
}


def load_settings():
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    settings["user_name"] = ""
    settings["tts"] = dict(DEFAULT_TTS_SETTINGS)
    settings["overlay"] = dict(DEFAULT_OVERLAY_SETTINGS)

    try:
        with SETTINGS_PATH.open("r", encoding="utf-8-sig") as file:
            saved_settings = json.load(file)
    except FileNotFoundError:
        save_settings(settings)
        return settings
    except json.JSONDecodeError as error:
        raise ValueError(
            f"settings.json contains invalid JSON "
            f"(line {error.lineno}, column {error.colno})."
        ) from error

    if not isinstance(saved_settings, dict):
        raise ValueError("settings.json must contain a JSON object.")

    for section, values in DEFAULT_SETTINGS.items():
        saved_section = saved_settings.get(section, {})

        if not isinstance(saved_section, dict):
            raise ValueError(
                f"The '{section}' setting must be a JSON object."
            )

        settings[section].update(saved_section)

    settings["user_name"] = saved_settings.get("user_name", "")
    settings["tts"] = saved_settings.get("tts", settings["tts"])
    settings["overlay"] = saved_settings.get("overlay", settings["overlay"])

    color = settings["terminal"]["default_name_color"]
    if color not in {
        "white", "cyan", "green", "magenta",
        "blue", "yellow", "red"
    }:
        raise ValueError(
            f"Unsupported default terminal color: {color}"
        )

    for key in (
        "max_chats",
    ):
        value = settings["sessions"][key]
        if type(value) is not int or value < 1:
            raise ValueError(f"sessions.{key} must be a positive integer.")

    for key in (
        "max_context_messages",
        "max_historical_chats",
        "max_history_chars",
    ):
        value = settings["context"][key]
        if type(value) is not int or value < 1:
            raise ValueError(f"context.{key} must be a positive integer.")

    return settings


def save_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError("Settings must be a dictionary.")

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=SETTINGS_PATH.parent,
            suffix=".tmp",
            delete=False,
        ) as file:
            temp_path = Path(file.name)
            json.dump(settings, file, indent=4)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        os.replace(temp_path, SETTINGS_PATH)

    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
