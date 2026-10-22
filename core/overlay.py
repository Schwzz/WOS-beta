"""Bridge between the running WOS terminal and the WOS-Overlay desktop UI.

The overlay is a second window onto the SAME WOS process: this module lets it
send messages to the one existing Session (so provider, character, memory
and chat files are shared) and read what the avatar should be doing. It
never starts a Session, a speaker or an input loop of its own.

* OverlayBackend  - thread-safe wrapper around the shared session and
                    speaker. One lock serializes UI messages against the
                    terminal's commands, so the two can never mutate the
                    session at the same time.
* OverlayServer   - a tiny stdlib HTTP server on 127.0.0.1 that serves the
                    UI files and a JSON API. Every API call needs a random
                    token that only the local overlay process can read from
                    data/overlay_endpoint.json.

Nothing here may break WOS: the server runs on daemon threads and start.py
treats a failure to start it as a warning.
"""
import atexit
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from core import config
from core.tts import DEFAULT_TTS_SETTINGS, get_api_key
from core.companion import Preferences
from core.model_catalog import ModelCatalog
from core.provider_registry import PROVIDERS, provider_options


APP_NAME = "WOS-Overlay"
WEB_DIR = config.PROJECT_ROOT / "overlay" / "web"
# overlay/main.py reads this same file to find the port and token.
ENDPOINT_PATH = config.PROJECT_ROOT / "data" / "overlay_endpoint.json"

MAX_MESSAGE_CHARS = 4000
MAX_BODY_BYTES = 64 * 1024
BUSY_TIMEOUT_SECONDS = 3
HISTORY_LIMIT = 100

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/sage.js": ("sage.js", "text/javascript; charset=utf-8"),
    "/chat.js": ("chat.js", "text/javascript; charset=utf-8"),
}


class BackendBusy(Exception):
    """The terminal is busy with a command; try again in a moment."""


class BackendError(Exception):
    """A failure whose message is safe to show in the UI."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def overlay_port(raw):
    """The configured port (0 means any free port), or the default."""
    port = raw.get("port") if isinstance(raw, dict) else None

    if type(port) is int and 0 <= port <= 65535:
        return port

    return config.DEFAULT_OVERLAY_SETTINGS["port"]


def save_voice_setting(enabled):
    """Persist tts.enabled in settings.json, keeping every other setting."""
    settings = config.load_settings()
    raw = settings.get("tts")
    tts = dict(raw) if isinstance(raw, dict) else dict(DEFAULT_TTS_SETTINGS)
    tts["enabled"] = enabled
    settings["tts"] = tts
    config.save_settings(settings)


class OverlayBackend:
    """Shared access to the one WOS session and speaker."""

    def __init__(self, get_session, speaker, notify=None):
        self._get_session = get_session
        self._speaker = speaker
        self._notify = notify

        # Held while a UI message or a terminal command is being processed.
        self._command_lock = threading.RLock()
        self._terminal_holding = False

        self._lock = threading.Lock()
        self._settings_lock = threading.Lock()
        self._speech_gate = threading.RLock()
        self._processing = 0
        self._pending = set()      # speech requests not yet finished
        self._speaking = None      # the request whose audio is playing
        self._audio = None
        self._chat_version = 0
        self._speech_floor = 0
        self._response_epoch = 0
        self._voice_error = None
        self._session_epoch = secrets.token_hex(8)
        self._terminal_reply_epoch = 0
        self._delivered = OrderedDict()
        self.preferences = Preferences()
        self.catalog = ModelCatalog()

    # -- terminal side -----------------------------------------------------

    def hold_command(self):
        """Called by the terminal loop once a command has been typed, so the
        UI cannot change the session while the command runs."""
        if not self._terminal_holding:
            self._command_lock.acquire()
            self._terminal_holding = True

    def release_command(self):
        """Called by the terminal loop before it waits for the next input."""
        if self._terminal_holding:
            # Commands can change the chat, character, model or its messages
            # without sending a message through this bridge.
            with self._lock:
                self._chat_version += 1
            self._terminal_holding = False
            self._command_lock.release()

    # -- messages ----------------------------------------------------------

    def send_message(self, text, source="ui", request_id=None, session_id=None):
        """Send text through the shared session and return the reply.

        source="terminal" is the unchanged terminal path (the terminal prints
        and speaks the reply itself). source="ui" validates the text, shows
        the exchange in the terminal and speaks the reply through the single
        shared speaker.
        """
        from_ui = source == "ui"

        if from_ui:
            if not isinstance(text, str) or not text.strip():
                raise BackendError("Type a message first.")

            text = text.strip()

            if len(text) > MAX_MESSAGE_CHARS:
                raise BackendError(
                    f"Messages can be at most {MAX_MESSAGE_CHARS} characters."
                )

            if text.startswith("/"):
                raise BackendError(
                    "Commands such as /model run in the WOS terminal."
                )
            if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 80):
                raise BackendError("Invalid message identifier.")

            if not self._command_lock.acquire(timeout=BUSY_TIMEOUT_SECONDS):
                raise BackendBusy("WOS is busy with a terminal command.")
        else:
            self._command_lock.acquire()

        try:
            session = self._get_session()
            if from_ui and request_id in self._delivered:
                previous_text, previous_reply = self._delivered[request_id]
                if previous_text != text:
                    raise BackendError("That message identifier was already used.")
                return previous_reply
            if from_ui and session_id is not None and session_id != getattr(session, "session_id", None):
                raise BackendError("The active chat changed. Review the chat before sending again.", 409)
            self.stop_speech()

            with self._lock:
                self._processing += 1
                response_epoch = self._response_epoch

            try:
                if from_ui:
                    self._say(f"You (overlay): {text}")

                try:
                    reply = session.send_message(text)
                except Exception:
                    if not from_ui:
                        raise

                    raise BackendError(
                        "WOS could not process that message.", 500
                    ) from None

                if from_ui:
                    if not isinstance(reply, str) or not reply.strip():
                        self._say("WOS: The model did not return a response.")
                        raise BackendError(
                            getattr(session, "last_provider_error", None) or "The model did not return a response.", 502
                        )

                    name = getattr(session, "character_name", "Nexis")
                    self._say(f"{name}: {reply}")
                    # Started before "processing" ends, so the avatar goes
                    # straight from thinking to waiting for audio.
                    with self._speech_gate:
                        with self._lock:
                            may_speak = response_epoch == self._response_epoch
                        if may_speak:
                            self._speaker.speak(reply)
                else:
                    self._terminal_reply_epoch = response_epoch

                if from_ui and request_id:
                    self._delivered[request_id] = (text, reply)
                    while len(self._delivered) > 128:
                        self._delivered.popitem(last=False)
                return reply
            finally:
                with self._lock:
                    self._processing -= 1
                    self._chat_version += 1
        finally:
            self._command_lock.release()

    def _say(self, message):
        if self._notify is None:
            return

        try:
            self._notify(message)
        except Exception:
            pass

    # -- speech events (called from the speaker's worker threads) ----------

    def on_speech_event(self, event, generation, **info):
        with self._lock:
            if generation < self._speech_floor:
                return
            if event in ("queued", "cancel"):
                self._speech_floor = generation
                self._pending = {g for g in self._pending if g >= generation}
                if self._speaking is not None and self._speaking < generation:
                    self._speaking = None
                    self._audio = None
            if event == "queued":
                self._pending.add(generation)
                self._voice_error = None
            elif event == "start":
                envelope = info.get("envelope")
                envelope = envelope if isinstance(envelope, dict) else {}
                levels = envelope.get("levels")

                self._pending.add(generation)
                self._speaking = generation
                self._audio = {
                    "id": generation,
                    "started": time.monotonic(),
                    "fps": envelope.get("fps", 50),
                    "duration": envelope.get("duration", 0.0),
                    "levels": levels if isinstance(levels, list) else [],
                }
            elif event == "end":
                self._pending.discard(generation)
                if info.get("error"):
                    self._voice_error = info["error"]

                if self._speaking == generation:
                    self._speaking = None
                    self._audio = None

    # -- state for the UI --------------------------------------------------

    def get_state(self, have_audio_id=None):
        """What the overlay shows. The (long) loudness list is only included
        when the UI does not already have it (have_audio_id)."""
        session = self._get_session()

        with self._lock:
            if self._speaking is not None:
                state = "speaking"
            elif self._processing or self._pending:
                state = "thinking"
            else:
                state = "idle"

            audio = None

            if self._audio is not None:
                current = self._audio
                audio = {
                    "id": current["id"],
                    "elapsed": time.monotonic() - current["started"],
                    "fps": current["fps"],
                    "duration": current["duration"],
                }

                if current["id"] != have_audio_id:
                    audio["levels"] = list(current["levels"])

            chat_version = self._chat_version
            phase = "speaking" if self._speaking is not None else (
                "thinking" if self._processing else (
                    "preparing" if self._pending else "idle"
                )
            )
            voice_error = self._voice_error

        return {
            "state": state,
            "character": getattr(session, "character_name", ""),
            "voice": {
                "enabled": bool(self._speaker.enabled),
                "available": get_api_key() is not None,
            },
            "chat_version": chat_version,
            "epoch": self._session_epoch,
            "session_id": getattr(session, "session_id", None),
            "chat_title": (getattr(session, "chat", None) or {}).get("title", "Current chat"),
            "provider": getattr(session, "provider", "cloud"),
            "model": getattr(session, "model", ""),
            "phase": phase,
            "voice_error": voice_error,
            "audio": audio,
        }

    def get_history(self, before=None):
        session = self._get_session()
        chat = getattr(session, "chat", None)
        saved = chat.get("messages", []) if isinstance(chat, dict) else []
        history = []

        end = len(saved) if before is None else max(0, min(before, len(saved)))
        start = max(0, end - HISTORY_LIMIT)
        for item in list(saved)[start:end]:
            if (
                isinstance(item, dict)
                and item.get("role") in ("user", "assistant")
                and isinstance(item.get("content"), str)
            ):
                history.append(
                    {"role": item["role"], "content": item["content"]}
                )

        return {
            "character": getattr(session, "character_name", ""),
            "messages": history,
            "before": start,
            "has_more": start > 0,
            "session_id": getattr(session, "session_id", None),
        }

    def stop_speech(self):
        """Also suppress speech for a reply that is still being generated."""
        with self._speech_gate:
            with self._lock:
                self._response_epoch += 1
            cancel = getattr(self._speaker, "cancel", None)
            if cancel:
                cancel()

    def speak_terminal_reply(self, reply):
        # Terminal replies use the same cancellation epoch as UI replies.
        with self._speech_gate:
            with self._lock:
                may_speak = self._terminal_reply_epoch == self._response_epoch
            if may_speak:
                self._speaker.speak(reply)

    # -- voice output ------------------------------------------------------

    def get_models(self, refresh=False, provider=None):
        session = self._get_session()
        current_provider = getattr(session, "provider", "cloud")
        provider = provider or current_provider
        if provider not in PROVIDERS:
            raise BackendError("Unknown provider.")
        known = session.known_models(provider) if hasattr(session, "known_models") else []
        result = self.catalog.list(provider, known, refresh=refresh)
        return dict(result, provider=provider, current=getattr(session, "model", "") if provider == current_provider else None)

    def set_provider(self, provider, model, expected_provider=None, session_id=None):
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise BackendError("Unknown provider.")
        try:
            model = config.validate_model_name(model)
        except ValueError as error:
            raise BackendError(str(error)) from None
        if not self._command_lock.acquire(timeout=BUSY_TIMEOUT_SECONDS):
            raise BackendBusy("WOS is busy. Change providers when the reply is finished.")
        try:
            session = self._get_session()
            if (expected_provider is not None and expected_provider != session.provider) or (
                session_id is not None and session_id != getattr(session, 'session_id', None)
            ):
                raise BackendError("The chat or provider changed. Refresh before applying your selection.", 409)
            self.stop_speech()
            result = session.change_provider(provider, model)
            if session.provider != provider or session.model != model:
                raise BackendError(result)
            with self._lock:
                self._chat_version += 1
            return self.get_state()
        finally:
            self._command_lock.release()

    def set_model(self, model, provider=None, session_id=None):
        try:
            model = config.validate_model_name(model)
        except ValueError as error:
            raise BackendError(str(error)) from None
        if not self._command_lock.acquire(timeout=BUSY_TIMEOUT_SECONDS):
            raise BackendBusy("WOS is busy. Try changing the model when the reply is finished.")
        try:
            session = self._get_session()
            if provider is not None and provider != session.provider:
                raise BackendError("The provider changed. Refresh the model list.", 409)
            if session_id is not None and session_id != getattr(session, 'session_id', None):
                raise BackendError("The active chat changed. Refresh before changing its model.", 409)
            self.stop_speech()
            result = session.change_model(model)
            if session.model != model:
                raise BackendError(result)
            with self._lock:
                self._chat_version += 1
            return self.get_state()
        finally:
            self._command_lock.release()

    def set_voice(self, enabled):
        """Turn voice output on or off, saving it to settings.json first so
        a failed save never leaves the UI and the file disagreeing."""
        if not isinstance(enabled, bool):
            raise BackendError("'enabled' must be true or false.")

        with self._settings_lock:
            try:
                save_voice_setting(enabled)
            except (OSError, ValueError):
                raise BackendError(
                    "Could not save the voice setting.", 500
                ) from None

            self._speaker.set_enabled(enabled)
            if not enabled:
                self.stop_speech()


class OverlayServer:
    """Serves the overlay UI and its JSON API on 127.0.0.1."""

    def __init__(self, backend, port=None, web_dir=None, endpoint_path=None, token=None):
        self.backend = backend
        self.web_dir = Path(web_dir) if web_dir else WEB_DIR
        self.endpoint_path = (
            Path(endpoint_path) if endpoint_path else ENDPOINT_PATH
        )
        self.token = token if isinstance(token, str) and len(token) >= 32 else secrets.token_urlsafe(32)
        self._requested_port = (
            config.DEFAULT_OVERLAY_SETTINGS["port"] if port is None else port
        )
        self._httpd = None
        self._thread = None
        self.port = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/#{self.token}"

    def start(self):
        """Start serving. Raises OSError if the port cannot be used."""
        handler = _make_handler(self)

        class Httpd(ThreadingHTTPServer):
            daemon_threads = True
            # On Windows SO_REUSEADDR would let a second server share the
            # port, so only enable it elsewhere.
            allow_reuse_address = os.name != "nt"

        self._httpd = Httpd(("127.0.0.1", self._requested_port), handler)
        self.port = self._httpd.server_address[1]

        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="wos-overlay-server",
            daemon=True,
        )
        self._thread.start()

        try:
            self._write_endpoint()
        except OSError:
            self.close()
            raise

        atexit.register(self.close)
        return self.port

    def close(self):
        """Stop serving and remove the endpoint file. Never raises."""
        httpd, self._httpd = self._httpd, None

        if httpd is None:
            return

        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass

        try:
            current = json.loads(self.endpoint_path.read_text("utf-8"))

            # Only remove our own file, never a newer WOS process's.
            if current.get("token") == self.token:
                self.endpoint_path.unlink()
        except (OSError, ValueError, AttributeError):
            pass

    def _write_endpoint(self):
        self.endpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.endpoint_path.parent,
                suffix=".tmp",
                delete=False,
            ) as file:
                temp_path = Path(file.name)
                json.dump(
                    {
                        "app": APP_NAME,
                        "port": self.port,
                        "token": self.token,
                        "pid": os.getpid(),
                    },
                    file,
                )

            os.replace(temp_path, self.endpoint_path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()


def _make_handler(server):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WOSOverlay"

        def log_message(self, format, *args):
            pass  # keep the terminal clean; never log URLs or headers

        # -- helpers -------------------------------------------------------

        def _allowed_hosts(self):
            port = server.port
            return {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _send(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _error(self, status, message):
            self._json(status, {"error": message})

        def _guard(self, api):
            """Reject requests from other sites or without the token."""
            if self.headers.get("Host", "") not in self._allowed_hosts():
                self._error(403, "Forbidden.")
                return False

            if not api:
                return True

            origin = self.headers.get("Origin")

            if origin is not None and origin not in {
                f"http://{host}" for host in self._allowed_hosts()
            }:
                self._error(403, "Forbidden.")
                return False

            token = self.headers.get("X-WOS-Token", "")

            if not hmac.compare_digest(
                token.encode("utf-8"), server.token.encode("utf-8")
            ):
                self._error(401, "Missing or wrong token.")
                return False

            return True

        def _read_json(self):
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._error(411, "Content-Length is required.")
                return None

            if length < 0 or length > MAX_BODY_BYTES:
                self._error(413, "The request is too large.")
                return None

            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._error(400, "The request body must be JSON.")
                return None

            if not isinstance(data, dict):
                self._error(400, "The request body must be a JSON object.")
                return None

            return data

        # -- routes --------------------------------------------------------

        def do_GET(self):
            try:
                self._handle_get()
            except Exception:
                self._safe_500()

        def do_POST(self):
            try:
                self._handle_post()
            except Exception:
                self._safe_500()

        def _safe_500(self):
            try:
                self._error(500, "Internal error.")
            except Exception:
                pass

        def _handle_get(self):
            parts = urlsplit(self.path)
            path = parts.path

            if path in STATIC_FILES:
                if not self._guard(api=False):
                    return

                name, content_type = STATIC_FILES[path]

                try:
                    body = (server.web_dir / name).read_bytes()
                except OSError:
                    self._error(404, "Not found.")
                    return

                self._send(200, body, content_type)
                return

            if path == "/api/ping":
                if self._guard(api=False):
                    self._json(200, {"ok": True, "app": APP_NAME})

                return

            if not path.startswith("/api/"):
                self._error(404, "Not found.")
                return

            if not self._guard(api=True):
                return

            if path == "/api/state":
                have = parse_qs(parts.query).get("have", [""])[0]

                try:
                    have = int(have)
                except ValueError:
                    have = None

                self._json(200, server.backend.get_state(have))
            elif path == "/api/history":
                before = parse_qs(parts.query).get("before", [None])[0]
                try:
                    before = int(before) if before is not None else None
                except ValueError:
                    self._error(400, "Invalid history position.")
                    return
                self._json(200, server.backend.get_history(before))
            elif path == "/api/models":
                refresh = parse_qs(parts.query).get("refresh", ["0"])[0] == "1"
                provider = parse_qs(parts.query).get("provider", [None])[0]
                try:
                    self._json(200, server.backend.get_models(refresh, provider))
                except BackendError as error:
                    self._error(error.status, str(error))
            elif path == "/api/providers":
                self._json(200, {"providers": provider_options()})
            elif path == "/api/preferences":
                self._json(200, server.backend.preferences.get())
            else:
                self._error(404, "Not found.")

        def _handle_post(self):
            path = urlsplit(self.path).path

            if not path.startswith("/api/"):
                self._error(404, "Not found.")
                return

            if not self._guard(api=True):
                return

            if path not in ("/api/message", "/api/voice", "/api/stop", "/api/model", "/api/provider", "/api/preferences"):
                self._error(404, "Not found.")
                return

            data = self._read_json()

            if data is None:
                return

            try:
                if path == "/api/message":
                    reply = server.backend.send_message(
                        data.get("text"), source="ui", request_id=data.get("request_id"),
                        session_id=data.get("session_id"),
                    )
                    self._json(200, {"reply": reply})
                elif path == "/api/voice":
                    server.backend.set_voice(data.get("enabled"))
                    self._json(200, server.backend.get_state())
                elif path == "/api/stop":
                    server.backend.stop_speech()
                    self._json(200, server.backend.get_state())
                elif path == "/api/model":
                    self._json(200, server.backend.set_model(data.get("model"), data.get("provider"), data.get("session_id")))
                elif path == "/api/provider":
                    self._json(200, server.backend.set_provider(data.get("provider"), data.get("model"),
                        data.get("expected_provider"), data.get("session_id")))
                else:
                    try:
                        result = server.backend.preferences.update(data)
                    except ValueError as error:
                        raise BackendError(str(error)) from None
                    except OSError:
                        raise BackendError("Could not save appearance settings.", 500) from None
                    self._json(200, result)
            except BackendBusy as error:
                self._error(409, str(error))
            except BackendError as error:
                self._error(error.status, str(error))

    return Handler
