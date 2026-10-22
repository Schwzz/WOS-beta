"""Optional Fish Audio text-to-speech for WOS.

Nothing in here may ever stop WOS or delay a text reply:

* speak() never raises, and does its network and audio work on a background
  thread, so the reply is already on screen and the prompt stays usable.
* The API key is read from the FISH_AUDIO_API_KEY environment variable only.
  It is sent in the request header and is never stored, printed or logged;
  error messages only ever contain a short reason such as "HTTP 401".
* Only one reply is spoken at a time. A new reply stops the previous one.

Audio is requested as WAV so it can be played with the standard library's
winsound module on Windows, with no extra dependency. httpx is imported only
when a request is made, so a missing httpx cannot break startup.
"""
import atexit
import os
import sys
import tempfile
import threading
import time
import wave
from array import array

from core.config import DEFAULT_TTS_SETTINGS


FISH_AUDIO_URL = "https://api.fish.audio/v1/tts"
API_KEY_ENV = "FISH_AUDIO_API_KEY"
REQUEST_TIMEOUT_SECONDS = 30

# send_message() returns a few failures as ordinary reply text (for example
# "I couldn't save this exchange: ..."). Those are errors, not speech.
_NON_SPEECH_PREFIXES = ("WOS:", "I couldn't save")


class TTSError(Exception):
    """A failure whose message is safe to show (it never contains the key)."""


def _clean_text(value, default):
    if isinstance(value, str) and value.strip():
        return value.strip()

    return default


def load_tts_config(raw):
    """Return a valid {"enabled", "voice_id", "model"} dict from the "tts"
    value in settings.json. Anything missing or invalid falls back to the
    defaults, and TTS is only enabled by a real JSON true."""
    raw = raw if isinstance(raw, dict) else {}

    return {
        "enabled": raw.get("enabled") is True,
        "voice_id": _clean_text(
            raw.get("voice_id"), DEFAULT_TTS_SETTINGS["voice_id"]
        ),
        "model": _clean_text(raw.get("model"), DEFAULT_TTS_SETTINGS["model"]),
    }


def get_api_key():
    """The key from the environment, or None if it is unset or a placeholder."""
    key = os.getenv(API_KEY_ENV, "").strip()

    if not key or key.lower().startswith("your_"):
        return None

    return key


def clean_speech_text(text):
    """The text to speak, or None for anything that must stay silent."""
    if not isinstance(text, str):
        return None

    text = text.strip()

    if not text or text.startswith(_NON_SPEECH_PREFIXES):
        return None

    return text


def _describe_status(status):
    if status in (401, 403):
        return f"HTTP {status}: the API key was rejected"

    if status == 402:
        return "HTTP 402: the Fish Audio account has no credit or quota"

    if status == 429:
        return "HTTP 429: too many requests"

    return f"HTTP {status}"


_WAV_HEADER_SIZE = 44


def normalize_wav(audio):
    """Return audio with correct RIFF and data size fields, or raise TTSError.

    Fish Audio sends WAV with streaming-style placeholder sizes, which makes
    winsound play a Windows ding instead of the speech. Only the canonical
    44-byte layout is accepted (RIFF/WAVE, a 16-byte "fmt " chunk, then
    "data"); anything else is rejected rather than guessed at. Only the two
    size fields are rewritten; every other byte is returned unchanged.
    """
    if not isinstance(audio, (bytes, bytearray)):
        raise TTSError("the response was not WAV audio")

    audio = bytes(audio)

    if len(audio) <= _WAV_HEADER_SIZE:
        raise TTSError("the WAV audio was empty or truncated")

    if (
        audio[0:4] != b"RIFF"
        or audio[8:12] != b"WAVE"
        or audio[12:16] != b"fmt "
        or audio[36:40] != b"data"
    ):
        raise TTSError("the response was not WAV audio")

    if int.from_bytes(audio[16:20], "little") != 16:
        raise TTSError("the WAV layout is not supported")

    channels = int.from_bytes(audio[22:24], "little")
    sample_rate = int.from_bytes(audio[24:28], "little")

    if channels < 1 or sample_rate < 1:
        raise TTSError("the WAV audio format is invalid")

    fixed = bytearray(audio)
    fixed[4:8] = (len(audio) - 8).to_bytes(4, "little")
    fixed[40:44] = (len(audio) - _WAV_HEADER_SIZE).to_bytes(4, "little")

    return bytes(fixed)


def synthesize(text, api_key, voice_id, model, post=None):
    """Return WAV bytes for text, or raise TTSError."""
    if post is None:
        try:
            import httpx
        except ImportError:
            raise TTSError(
                "the 'httpx' package is not installed (pip install httpx)"
            ) from None

        post = httpx.post

    try:
        response = post(
            FISH_AUDIO_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "model": model,
            },
            json={
                "text": text,
                "reference_id": voice_id,
                "format": "wav",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except Exception as error:
        # Only the exception type is reported, never its text.
        raise TTSError(f"network error ({type(error).__name__})") from None

    status = getattr(response, "status_code", None)

    if status != 200:
        raise TTSError(_describe_status(status))

    audio = getattr(response, "content", None)

    if not isinstance(audio, (bytes, bytearray)) or not audio:
        raise TTSError("the response contained no audio")

    return normalize_wav(audio)


def wav_envelope(wav, fps=50):
    """Loudness over time of the audio that is about to be played.

    Takes a WAV that normalize_wav() accepted and returns
    {"fps", "duration", "levels"}: one RMS value per 1/fps second window,
    scaled so the loudest window is 1.0. The desktop overlay animates from
    these numbers, so the avatar follows the real generated speech. Only
    16-bit PCM is analysed; any other format gives an empty "levels" list
    (the speech still plays) and never raises.
    """
    empty = {"fps": fps, "duration": 0.0, "levels": []}

    try:
        channels = int.from_bytes(wav[22:24], "little")
        rate = int.from_bytes(wav[24:28], "little")
        bits = int.from_bytes(wav[34:36], "little")

        if bits != 16 or channels < 1 or rate < 1:
            return empty

        data = bytes(wav[_WAV_HEADER_SIZE:])
        samples = array("h")
        samples.frombytes(data[: len(data) // 2 * 2])

        if sys.byteorder == "big":
            samples.byteswap()

        frames = len(samples) // channels
        window = max(1, rate // fps) * channels
        levels = []

        for start in range(0, len(samples), window):
            chunk = samples[start:start + window]
            levels.append(
                (sum(value * value for value in chunk) / len(chunk)) ** 0.5
            )

        peak = max(levels) if levels else 0

        return {
            "fps": fps,
            "duration": frames / rate,
            "levels": [
                round(level / peak, 3) if peak > 0 else 0.0
                for level in levels
            ],
        }
    except Exception:
        return empty


class WinsoundPlayer:
    """Plays a WAV file on Windows with the standard library."""

    def __init__(self):
        try:
            import winsound
        except ImportError:
            winsound = None

        self._winsound = winsound
        self._lock = threading.Lock()
        self._playing = None

    def play(self, path, *, cancelled=None, started=None):
        """Play path and return when it has finished (or was stopped)."""
        if self._winsound is None:
            raise TTSError("audio playback is only supported on Windows")

        cancelled = cancelled if cancelled is not None else threading.Event()
        with wave.open(path, "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()

        # Starting and stopping the native player must be serialized. The
        # cancellation event is checked INSIDE this lock, so a late worker
        # cannot start after stop() has already returned.
        with self._lock:
            if cancelled.is_set():
                return
            self._winsound.PlaySound(
                path, self._winsound.SND_FILENAME
                | self._winsound.SND_ASYNC | self._winsound.SND_NODEFAULT
            )
            self._playing = cancelled
        try:
            if started:
                started()
            cancelled.wait(duration + 0.03)
        finally:
            with self._lock:
                if self._playing is cancelled:
                    self._playing = None

    def stop(self):
        if self._winsound is not None:
            # A None sound stops any waveform sound this process is playing.
            with self._lock:
                if self._playing is not None:
                    self._playing.set()
                self._winsound.PlaySound(None, 0)
                self._playing = None


class FishAudioSpeaker:
    """Speaks replies with Fish Audio. Every failure is contained here."""

    def __init__(self, tts_settings=None, post=None, player=None,
                 notify=print, listener=None):
        config = load_tts_config(tts_settings)

        self.enabled = config["enabled"]
        self.voice_id = config["voice_id"]
        self.model = config["model"]

        self._post = post
        self._player = player if player is not None else WinsoundPlayer()
        self._notify = notify

        # Optional callback listener(event, generation, **info) used by the
        # desktop overlay. Events: "queued", "start" (audio is about to
        # play, with its envelope) and "end" (finished, failed or replaced).
        self.listener = listener

        self._lock = threading.Lock()
        self._generation = 0
        self._control_lock = threading.RLock()
        self._workers = set()
        self._jobs = {}
        self._closed = False
        self._temp_files = set()
        self._last_notice = None
        self._atexit_registered = False

        if self.enabled:
            self._register_atexit()

    def _register_atexit(self):
        if not self._atexit_registered:
            self._atexit_registered = True
            atexit.register(self.shutdown)

    def _emit(self, event, generation, **info):
        listener = self.listener

        if listener is None:
            return

        try:
            listener(event, generation, **info)
        except Exception:
            pass

    # -- public API --------------------------------------------------------

    def set_enabled(self, enabled):
        """Turn speech on or off at runtime. Turning it off also stops any
        speech that is in progress. Never raises."""
        try:
            enabled = enabled is True
            with self._lock:
                self.enabled = enabled

            if enabled:
                self._register_atexit()
            else:
                self.cancel()
        except Exception:
            pass

    def speak(self, text):
        with self._control_lock:
            return self._speak(text)

    def _speak(self, text):
        """Start speaking text in the background. Returns True if a request
        was started. Never raises."""
        try:
            with self._lock:
                if not self.enabled or self._closed:
                    return False
            text = clean_speech_text(text)

            if text is None:
                return False

            api_key = get_api_key()

            if api_key is None:
                self._notice(
                    f"{API_KEY_ENV} is not set, so replies are not spoken"
                )
                return False

            with self._lock:
                if not self.enabled or self._closed:
                    return False
                self._generation += 1
                generation = self._generation
                previous = list(self._jobs.values())
                for event in previous:
                    event.set()
                cancelled = threading.Event()
                self._jobs[generation] = cancelled

            # Cut off whatever is still playing so replies never overlap.
            self._stop_playback()
            if previous:
                self._emit("cancel", generation)

            worker = threading.Thread(
                target=self._run,
                args=(generation, text, api_key, cancelled),
                name="wos-tts",
                daemon=True,
            )

            with self._lock:
                self._workers.add(worker)

            self._emit("queued", generation)

            try:
                worker.start()
            except Exception:
                with self._lock:
                    self._workers.discard(worker)
                    self._jobs.pop(generation, None)

                self._emit("end", generation)
                raise

            return True

        except Exception:
            return False

    @property
    def active(self):
        with self._lock:
            return any(not event.is_set() for event in self._jobs.values())

    def cancel(self):
        """Silence current/pending speech without changing the voice setting."""
        with self._control_lock:
            with self._lock:
                self._generation += 1
                generation = self._generation
                for event in self._jobs.values():
                    event.set()
            self._stop_playback()
            self._emit("cancel", generation)

    def shutdown(self):
        """Stop playback and remove leftover temporary files. Never raises."""
        try:
            with self._lock:
                self._closed = True
                workers = list(self._workers)

            self.cancel()

            deadline = time.monotonic() + 1
            for worker in workers:
                if worker is not threading.current_thread():
                    worker.join(timeout=max(0, deadline - time.monotonic()))

            for path in list(self._temp_files):
                self._remove_temp(path)
        except Exception:
            pass

    def wait(self, timeout=10):
        """Wait for background work to finish (used by tests)."""
        with self._lock:
            workers = list(self._workers)

        for worker in workers:
            worker.join(timeout=timeout)

    # -- background work ---------------------------------------------------

    def _run(self, generation, text, api_key, cancelled):
        path = None
        failure = None

        try:
            if cancelled.is_set():
                return
            audio = synthesize(
                text, api_key, self.voice_id, self.model, post=self._post
            )

            if not self._is_current(generation):
                return

            path = self._write_temp(audio)

            if not self._is_current(generation):
                return

            envelope = wav_envelope(audio)
            if cancelled.is_set():
                return
            self._player.play(
                path, cancelled=cancelled,
                started=lambda: self._emit("start", generation, envelope=envelope),
            )

            if self._is_current(generation):
                self._last_notice = None

        except TTSError as error:
            if self._is_current(generation):
                failure = str(error)
                self._notice(failure)
        except Exception as error:
            if self._is_current(generation):
                failure = f"unexpected error ({type(error).__name__})"
                self._notice(failure)
        finally:
            if path is not None:
                self._remove_temp(path)

            with self._lock:
                self._workers.discard(threading.current_thread())
                self._jobs.pop(generation, None)

            self._emit("end", generation, error=failure)

    def _is_current(self, generation):
        with self._lock:
            return generation == self._generation

    def _stop_playback(self):
        try:
            self._player.stop()
        except Exception:
            pass

    def _write_temp(self, audio):
        descriptor, path = tempfile.mkstemp(prefix="wos_tts_", suffix=".wav")

        with self._lock:
            self._temp_files.add(path)

        with os.fdopen(descriptor, "wb") as file:
            file.write(audio)

        return path

    def _remove_temp(self, path):
        for attempt in range(3):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                # Windows can hold the file for a moment after playback.
                time.sleep(0.1)
                continue

            # Removed (or already gone). A file that could not be removed
            # stays listed so shutdown() tries again.
            with self._lock:
                self._temp_files.discard(path)

            return

    def _notice(self, reason):
        """Tell the user once per distinct problem, never raising."""
        message = f"WOS: Voice unavailable: {reason}."

        with self._lock:
            if message == self._last_notice:
                return

            self._last_notice = message

        try:
            self._notify(message)
        except Exception:
            pass
