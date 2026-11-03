"""Shared application lifetime for Terminal and Desktop; no interactive input loop."""
import threading
from pathlib import Path

from core import conversation
from core.config import load_settings
from core.session import Session
from core.tts import FishAudioSpeaker
from core.overlay import OverlayBackend, OverlayServer, overlay_port


class ApplicationRuntime:
    def __init__(self, session=None, notify=None):
        self._lifecycle = threading.RLock()
        self._closed = False
        self.server = None
        self._ownership = None
        self._ownership_released = threading.Event()
        try:
            self._acquire_ownership()
            self.session = session if session is not None else Session()
            try:
                voice = load_settings().get('tts')
            except (ValueError, OSError):
                voice = None
            self.speaker = FishAudioSpeaker(voice, notify=notify)
            self.backend = OverlayBackend(lambda: self.session, self.speaker, notify=notify)
            self.speaker.listener = self.backend.on_speech_event
        except Exception:
            if hasattr(self, 'speaker'):
                self.speaker.shutdown()
            self._release_ownership()
            raise

    def _acquire_ownership(self):
        """One writer per data directory, even across Desktop/Terminal processes."""
        path = Path(conversation.DATA_DIR) / 'runtime.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        file = path.open('a+b')
        try:
            import os
            if os.name == 'nt':
                import msvcrt
                if path.stat().st_size == 0:
                    file.write(b'0')
                    file.flush()
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            file.close()
            raise RuntimeError('Auralis is already running for this project. Close it before opening another interface.') from None
        self._ownership = file

    def _release_ownership(self):
        if self._ownership is not None:
            self._ownership.close()
            self._ownership = None
        self._ownership_released.set()

    def _release_after_commands(self):
        lock = self.backend._command_lock
        if lock.acquire(blocking=False):
            try:
                self._release_ownership()
            finally:
                lock.release()
        else:
            def finish():
                with lock:
                    self._release_ownership()
            threading.Thread(target=finish, name="auralis-shutdown", daemon=True).start()

    def start_server(self, *, port=None, token=None):
        with self._lifecycle:
            if self._closed:
                raise RuntimeError('Auralis has already shut down.')
            if self.server is not None:
                return self.server
            server = OverlayServer(self.backend,
                port=overlay_port(load_settings().get('overlay')) if port is None else port,
                token=token, endpoint_path=Path(conversation.DATA_DIR) / 'overlay_endpoint.json')
            try:
                server.start()
            except Exception:
                server.close()
                raise
            self.server = server
            return server

    def close(self):
        with self._lifecycle:
            if self._closed:
                return
            self._closed = True
            try:
                # Block only short persistence commits, never provider HTTP.
                self.backend.close()
                self.session.close()
                self.speaker.shutdown()
            finally:
                try:
                    if self.server is not None:
                        self.server.close()
                finally:
                    self._release_after_commands()
