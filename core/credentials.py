"""Write only recognized local credentials without exposing their values."""
import os
import re
import tempfile
from io import StringIO
from pathlib import Path

from dotenv.parser import parse_stream

from core import config
from core.provider_registry import PROVIDERS, provider_options
from core.tts import API_KEY_ENV, get_api_key


MAX_KEY_CHARS = 8192


class CredentialError(ValueError):
    """A credential failure with a message safe for the desktop."""


def credential_status():
    return {"providers": provider_options(), "voice": {"configured": get_api_key() is not None},
            "speech": {"configured": bool(os.getenv("GROQ_API_KEY", "").strip()) and not os.getenv("GROQ_API_KEY", "").strip().lower().startswith("your_")}}


def _comment_suffix(value):
    """Keep an existing inline comment, respecting quoted # characters."""
    quote = None
    escaped = False
    for position, character in enumerate(value):
        if escaped:
            escaped = False
        elif character == "\\" and quote:
            escaped = True
        elif quote:
            if character == quote:
                quote = None
        elif character in ("'", '"'):
            quote = character
        elif character == "#" and position and value[position - 1].isspace():
            start = position
            while start and value[start - 1] in " \t":
                start -= 1
            return value[start:].rstrip("\r\n")
    return ""


def save_credential(provider, key):
    if not isinstance(provider, str):
        raise CredentialError("Choose a supported credential provider.")
    env_name = "GROQ_API_KEY" if provider == "groq" else API_KEY_ENV if provider == "fish_audio" else PROVIDERS.get(provider, {}).get("key_env")
    if not env_name:
        raise CredentialError("Choose a provider that uses an API key.")
    if (not isinstance(key, str) or not key.strip() or len(key) > MAX_KEY_CHARS
            or any(ord(character) < 32 or ord(character) == 127 for character in key)):
        raise CredentialError("Enter an API key on one line, up to 8192 characters.")
    key = key.strip()
    if '${' in key:
        raise CredentialError("Paste a literal API key, without environment-variable interpolation.")
    path = config.PROJECT_ROOT / ".env"
    temporary = None
    try:
        try:
            original = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            original = ""
        quoted = "'" + key.replace("\\", "\\\\").replace("'", "\\'") + "'"
        pieces, found = [], False
        for binding in parse_stream(StringIO(original)):
            text = binding.original.string
            if binding.key == env_name:
                # Keep comment-only lines, whitespace and comments belonging to
                # this entry; leave every unrelated binding byte-for-byte.
                match = re.search(r"(?m)^[ \t]*(?:export[ \t]+)?" + re.escape(env_name) + r"[ \t]*=", text)
                prefix = text[:match.start()] if match else ""
                comment = _comment_suffix(text[match.end():]) if match else ""
                ending = "\n" if text.endswith("\n") else ""
                pieces.append(prefix + env_name + "=" + quoted + comment + ending)
                found = True
            else:
                pieces.append(text)
        updated = "".join(pieces)
        if not found:
            updated += ("\n" if updated and not updated.endswith("\n") else "") + env_name + "=" + quoted + "\n"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                prefix=".env-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            file.write(updated)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
        os.environ[env_name] = key
    except (OSError, ValueError, TypeError):
        raise CredentialError("Could not save the API key. Check access to the local .env file.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return credential_status()
