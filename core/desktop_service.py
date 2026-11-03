"""Desktop management of the existing Session, using the shared command lock."""
from contextlib import contextmanager
from copy import deepcopy

from core import character, conversation
from core.credentials import CredentialError, credential_status, save_credential
from core.overlay import BackendBusy, BackendError


class DesktopService:
    def __init__(self, backend):
        self.backend = backend

    def get_chats(self):
        return {"chats": conversation.load_index(),
                "session_id": self.backend._get_session().session_id}

    def get_characters(self):
        return {"characters": character.list_characters(),
                "current": self.backend._get_session().character_id}

    def get_credentials(self):
        return credential_status()

    @contextmanager
    def _mutation(self, expected=None, check_session=True):
        backend = self.backend
        if not backend._command_lock.acquire(blocking=False):
            raise BackendBusy("Auralis is busy. Try again when the reply or command finishes.")
        try:
            backend._ensure_open()
            with backend._lock:
                if backend._processing:
                    raise BackendBusy("Auralis is replying. Try again when the reply finishes.")
            session = backend._get_session()
            if check_session and (not isinstance(expected, str) or expected != session.session_id):
                raise BackendError("The active chat changed. Refresh before applying this action.", 409)
            with session._commit_lock:
                if session._shutdown.is_set():
                    raise BackendError("Auralis has shut down.", 409)
                snapshot = session.__dict__.copy()
                # Runtime resources (notably Session._shutdown's Event) must retain
                # their identity. Only these command-owned containers are mutated.
                for name in ("chat", "messages", "_provider_models", "character"):
                    if name in snapshot:
                        snapshot[name] = deepcopy(snapshot[name])
                try:
                    yield session
                except Exception:
                    # Several Session commands update attributes before their final
                    # save. Keep failed desktop actions from changing live state.
                    session.__dict__.clear()
                    session.__dict__.update(snapshot)
                    raise
        finally:
            backend._command_lock.release()

    def _changed(self, reset_requests=False):
        if reset_requests:
            self.backend._delivered.clear()
        with self.backend._lock:
            self.backend._chat_version += 1
        return self.backend.get_state()

    @staticmethod
    def _target(value):
        try:
            conversation.get_chat_file(value)
        except (ValueError, TypeError):
            raise BackendError("Choose a saved chat.") from None
        chat = conversation.load_chat(value)
        if chat is None:
            raise BackendError("That saved chat could not be loaded. Refresh the chat list.", 404)
        return chat

    def mutate_chat(self, data):
        action = data.get("action")
        if action not in ("new", "switch", "rename", "delete", "clear"):
            raise BackendError("Choose a supported chat action.")
        if action in ("delete", "clear") and data.get("confirm") is not True:
            raise BackendError("Confirm this action before deleting saved messages.")
        try:
            with self._mutation(data.get("expected_session_id")) as session:
                target = data.get("session_id")
                was_active = target == session.session_id
                chat = self._target(target) if action != "new" else None
                if action == "clear" and target != session.session_id:
                    raise BackendError("Switch to this chat before clearing its messages.", 409)
                if action == "new":
                    self.backend.stop_speech()
                    session.new_chat()
                elif action == "switch":
                    self.backend.stop_speech()
                    if not session.switch_chat(target):
                        raise BackendError("That chat or its character could not be loaded.")
                elif action == "rename":
                    title = data.get("title")
                    if not isinstance(title, str) or not title.strip() or len(title) > 200 or any(ord(c) < 32 for c in title):
                        raise BackendError("Enter a chat title on one line, up to 200 characters.")
                    chat["title"] = title.strip()
                    conversation.update_chat(chat)
                    if target == session.session_id:
                        session.chat = chat
                elif action == "clear":
                    self.backend.stop_speech()
                    session.clear()
                else:
                    self._delete(session, target, chat)
                return self._changed(action in ("new", "switch", "clear") or action == "delete" and was_active)
        except conversation.ChatLimitError as error:
            raise BackendError(str(error)) from None
        except (OSError, ValueError, TypeError):
            raise BackendError("Could not save the chat change. Check access to your saved chats.", 500) from None

    def _delete(self, session, target, chat):
        was_active = target == session.session_id
        previous_index = conversation.load_index()
        if was_active:
            self.backend.stop_speech()
        if not conversation.delete_chat(target):
            raise BackendError("That chat could not be deleted. Refresh the chat list.", 409)
        if was_active:
            try:
                for item in reversed(conversation.load_index()):
                    if session.switch_chat(item["session_id"]):
                        break
                else:
                    session.new_chat()
            except Exception:
                # If selecting a replacement fails, restore the chat we just
                # deleted before propagating the safe failure to the UI.
                conversation.save_chat(chat)
                conversation.save_index(previous_index)
                conversation.set_active_session(target)
                raise

    def set_character(self, data):
        choice = data.get("character")
        try:
            character.load_character(choice)
        except (OSError, ValueError, TypeError):
            raise BackendError("Choose an available character.") from None
        try:
            with self._mutation(data.get("session_id")) as session:
                self.backend.stop_speech()
                result = session.switch_character(choice)
                if not isinstance(result, str) or not result.startswith("Character switched to "):
                    raise BackendError("Could not save the character change.", 500)
                return self._changed()
        except (OSError, ValueError, TypeError):
            raise BackendError("Could not save the character change.", 500) from None

    def set_credential(self, data):
        with self._mutation(check_session=False):
            try:
                return save_credential(data.get("provider"), data.get("key"))
            except CredentialError as error:
                status = 500 if str(error).startswith("Could not save") else 400
                raise BackendError(str(error), status) from None
