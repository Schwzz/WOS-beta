"""Optional microphone dictation through Groq; audio stays in memory.

Importing this module opens no devices and imports no audio dependency.
Each capture owns its recorder, cancellation events and generation, so a late
HTTP response cannot affect a newer capture or a different chat session.
"""
import io
import json
import math
import os
import sys
import threading
import time
import wave
from array import array

from core import config


GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
API_KEY_ENV = "GROQ_API_KEY"
MODELS = frozenset({"whisper-large-v3-turbo", "whisper-large-v3"})
SHORTCUTS = frozenset({"f7", "f9", "f10"})
MAX_CAPTURE_SECONDS = 60
MIN_CAPTURE_SECONDS = 0.3
SILENCE_RMS_THRESHOLD = 0.003
DEFAULT_STT_SETTINGS = {
    "provider": "groq", "model": "whisper-large-v3-turbo", "shortcut": "f7"
}

_ERRORS = {
    "failed": "Speech input failed. Please try again.",
    "no_key": "Set GROQ_API_KEY before using speech input.",
    "busy": "Speech input is already recording or transcribing.",
    "pending": "Use or clear the previous transcript before recording again.",
    "closed": "Speech input has been closed.",
    "session": "The chat session changed; the transcript was discarded.",
    "settings": "Choose a supported speech model and shortcut (F7, F9 or F10).",
    "save": "Speech settings could not be saved.",
    "microphone": "The microphone is unavailable. Check its connection and permissions.",
    "sounddevice": "Microphone input requires the optional sounddevice package.",
    "requests": "Speech input requires the requests package.",
    "short": "The recording was too short. Please speak for a little longer.",
    "silent": "No speech was detected. Check the microphone and try again.",
    "audio": "The microphone returned invalid audio.",
    "network": "The speech service could not be reached. Please try again.",
    "unauthorized": "The Groq API key was rejected. Check GROQ_API_KEY.",
    "quota": "Groq speech input is currently rate limited or out of quota.",
    "provider": "The speech service returned an error. Please try again.",
    "response": "The speech service returned no transcript. Please try again.",
}


class STTError(Exception):
    """Only fixed, safe messages can cross the speech input boundary."""

    def __init__(self, code="failed"):
        self.code = code if code in _ERRORS else "failed"
        super().__init__(_ERRORS[self.code])


def get_api_key():
    key = os.getenv(API_KEY_ENV, "").strip()
    return key if key and not key.lower().startswith("your_") else None


def load_stt_config(raw):
    raw = raw if isinstance(raw, dict) else {}
    model, shortcut = raw.get("model"), raw.get("shortcut")
    return {
        "provider": "groq",
        "model": model if isinstance(model, str) and model in MODELS else DEFAULT_STT_SETTINGS["model"],
        "shortcut": shortcut if isinstance(shortcut, str) and shortcut in SHORTCUTS else DEFAULT_STT_SETTINGS["shortcut"],
    }


def _rms(pcm):
    values = array("h")
    values.frombytes(pcm[:len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        values.byteswap()
    return math.sqrt(sum(value * value for value in values) / len(values)) / 32768 if values else 0.0


def _validate_audio(audio):
    """Reject empty/silent/unbounded audio before any provider request."""
    try:
        if not isinstance(audio, bytes):
            raise STTError("audio")
        with wave.open(io.BytesIO(audio), "rb") as wav:
            rate = wav.getframerate()
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or rate < 1:
                raise STTError("audio")
            frames = wav.getnframes()
            duration = frames / rate
            if duration < MIN_CAPTURE_SECONDS:
                raise STTError("short")
            if duration > MAX_CAPTURE_SECONDS:
                raise STTError("audio")
            pcm = wav.readframes(frames)
            if len(pcm) != frames * 2:
                raise STTError("audio")
            if _rms(pcm) < SILENCE_RMS_THRESHOLD:
                raise STTError("silent")
    except STTError:
        raise
    except Exception:
        raise STTError("audio") from None


def transcribe_audio(audio, api_key, model, post=None):
    """Return a transcript; never expose an exception or raw provider body."""
    _validate_audio(audio)
    if not isinstance(model, str) or model not in MODELS:
        raise STTError("settings")
    if not api_key:
        raise STTError("no_key")
    if post is None:
        try:
            import requests
        except ImportError:
            raise STTError("requests") from None
        post = requests.post
    try:
        response = post(
            GROQ_TRANSCRIPTION_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": ("speech.wav", audio, "audio/wav")},
            data={"model": model, "response_format": "json"},
            timeout=(3, 30),
            allow_redirects=False,
        )
    except Exception:
        raise STTError("network") from None
    status = getattr(response, "status_code", None)
    if status in (401, 403):
        raise STTError("unauthorized")
    if status in (402, 429):
        raise STTError("quota")
    if status != 200:
        raise STTError("provider")
    try:
        result = response.json()
        text = result.get("text") if isinstance(result, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise STTError("response")
        return text.strip()
    except Exception:
        raise STTError("response") from None


class MicrophoneRecorder:
    """One native capture. cancel() releases only this recorder's stream.

    Injectable recorders implement record(stop_event, cancelled_event,
    progress_callback) -> WAV bytes, plus an idempotent cancel(). The progress
    callback receives (level: 0..1, elapsed_seconds).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._stream = None
        self._cancelled = threading.Event()

    def cancel(self):
        self._cancelled.set()
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    def record(self, stop_event, cancelled_event, progress_callback):
        try:
            import sounddevice
        except ImportError:
            raise STTError("sounddevice") from None
        if cancelled_event.is_set() or self._cancelled.is_set():
            return b""
        try:
            rate = float(sounddevice.query_devices(kind="input")["default_samplerate"])
            if not math.isfinite(rate) or not 1 <= rate <= 192000:
                rate = 16000
            rate = int(rate)
        except Exception:
            rate = 16000
        limit = int(rate * MAX_CAPTURE_SECONDS)
        chunks, callback_error = [], threading.Event()
        frames_recorded = 0

        def on_audio(indata, frames, timestamp, status):
            nonlocal frames_recorded
            try:
                if stop_event.is_set() or cancelled_event.is_set() or self._cancelled.is_set():
                    return
                if status:
                    callback_error.set()
                    stop_event.set()
                    return
                remaining = limit - frames_recorded
                chunk = bytes(indata)[:max(0, remaining) * 2]
                if sys.byteorder == "big":
                    samples = array("h")
                    samples.frombytes(chunk)
                    samples.byteswap()
                    chunk = samples.tobytes()
                chunks.append(chunk)
                frames_recorded += len(chunk) // 2
                progress_callback(min(1.0, _rms(chunk) * 8), frames_recorded / rate)
                if frames_recorded >= limit:
                    stop_event.set()
            except Exception:
                callback_error.set()
                stop_event.set()

        try:
            stream = sounddevice.RawInputStream(
                samplerate=rate, channels=1, dtype="int16", callback=on_audio,
            )
            with self._lock:
                if self._cancelled.is_set() or cancelled_event.is_set():
                    stream.close()
                    return b""
                self._stream = stream
            stream.start()
            deadline = time.monotonic() + MAX_CAPTURE_SECONDS
            while not stop_event.wait(0.02):
                if cancelled_event.is_set() or self._cancelled.is_set():
                    return b""
                if time.monotonic() >= deadline:
                    break
            if callback_error.is_set():
                raise STTError("microphone")
            if cancelled_event.is_set() or self._cancelled.is_set():
                return b""
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(b"".join(chunks))
            return output.getvalue()
        except STTError:
            raise
        except Exception:
            raise STTError("microphone") from None
        finally:
            self.cancel()


def _read_settings():
    try:
        with config.SETTINGS_PATH.open("r", encoding="utf-8-sig") as file:
            values = json.load(file)
    except FileNotFoundError:
        return config.load_settings()
    if not isinstance(values, dict):
        raise STTError("save")
    return values


class SpeechController:
    """Thread-safe optional dictation state shared by Desktop and Terminal."""

    def __init__(self, get_session_id, before_record=None, recorder_factory=None,
                 transcribe_fn=None):
        self._get_session_id = get_session_id
        self._before_record = before_record
        self._recorder_factory = recorder_factory or MicrophoneRecorder
        self._transcribe = transcribe_fn or transcribe_audio
        self._lock = threading.Lock()
        self._generation = 0
        self._next_result_id = 0
        self._job = None
        self._closed = False
        self._phase, self._level, self._elapsed = "idle", 0.0, 0.0
        self._error = self._result = self._session_id = None
        try:
            self._settings = load_stt_config(_read_settings().get("stt"))
        except Exception:
            self._settings = dict(DEFAULT_STT_SETTINGS)

    def _session(self):
        # Callbacks can acquire backend locks. Never call one under our lock.
        try:
            return self._get_session_id()
        except Exception:
            return None

    def settings(self):
        with self._lock:
            return dict(self._settings)

    def configure(self, values):
        if not isinstance(values, dict) or set(values) - {"provider", "model", "shortcut"}:
            raise STTError("settings")
        if ("provider" in values and values["provider"] != "groq"
                or "model" in values and (not isinstance(values["model"], str) or values["model"] not in MODELS)
                or "shortcut" in values and (not isinstance(values["shortcut"], str) or values["shortcut"] not in SHORTCUTS)):
            raise STTError("settings")
        with self._lock:
            if self._closed:
                raise STTError("closed")
            if self._job is not None:
                raise STTError("busy")
            settings = dict(self._settings, **values)
            try:
                saved = _read_settings()
                saved["stt"] = settings
                config.save_settings(saved)
            except Exception:
                raise STTError("save") from None
            self._settings = settings
            return dict(settings)

    def _snapshot_locked(self):
        return {
            "phase": self._phase, "level": self._level,
            "elapsed": self._elapsed, "error": self._error,
            "result": dict(self._result) if self._result is not None else None,
            "shortcut": self._settings["shortcut"], "model": self._settings["model"],
            "provider": "groq", "configured": get_api_key() is not None,
        }

    def _invalidate_locked(self):
        self._generation += 1
        job, self._job = self._job, None
        if job is not None:
            job["stop"].set()
            job["cancelled"].set()
        self._level = self._elapsed = 0.0
        self._result = self._session_id = None
        return job.get("recorder") if job is not None else None

    @staticmethod
    def _release(recorder):
        if recorder is not None:
            try:
                recorder.cancel()
            except Exception:
                pass

    def state(self):
        current = self._session()
        recorder = None
        with self._lock:
            if (self._session_id is not None and current != self._session_id
                    and (self._job is not None or self._result is not None)):
                recorder = self._invalidate_locked()
                self._phase, self._error = "error", str(STTError("session"))
            state = self._snapshot_locked()
        self._release(recorder)
        return state

    def start(self, session_id):
        if not isinstance(session_id, str) or not session_id or self._session() != session_id:
            raise STTError("session")
        key = get_api_key()
        if key is None:
            raise STTError("no_key")
        with self._lock:
            if self._closed:
                raise STTError("closed")
            if self._job is not None:
                raise STTError("busy")
            if self._result is not None:
                raise STTError("pending")
            self._generation += 1
            job = {
                "generation": self._generation, "session": session_id,
                "stop": threading.Event(), "cancelled": threading.Event(),
                "recorder": None,
            }
            self._job, self._session_id = job, session_id
            self._phase, self._error = "recording", None
            self._level = self._elapsed = 0.0
            model = self._settings["model"]
            state = self._snapshot_locked()
        worker = threading.Thread(target=self._run, args=(job, key, model),
                                  name="auralis-stt", daemon=True)
        try:
            worker.start()
        except Exception:
            with self._lock:
                if self._job is job:
                    self._job = None
                    self._phase, self._error = "error", str(STTError())
            raise STTError() from None
        return state

    def stop(self):
        with self._lock:
            if self._job is not None and self._phase == "recording":
                self._job["stop"].set()
        return self.state()

    def cancel(self):
        with self._lock:
            recorder = self._invalidate_locked()
            self._phase, self._error = "idle", None
            state = self._snapshot_locked()
        self._release(recorder)
        return state

    def acknowledge(self, result_id):
        with self._lock:
            if (type(result_id) is int and self._result is not None
                    and self._result["id"] == result_id):
                self._result = self._session_id = None
        return self.state()

    def close(self):
        with self._lock:
            self._closed = True
        return self.cancel()

    def _current_locked(self, job):
        return self._job is job and not self._closed and not job["cancelled"].is_set()

    def _run(self, job, key, model):
        recorder = None
        try:
            with self._lock:
                if not self._current_locked(job):
                    return
            if self._before_record is not None:
                self._before_record()
            with self._lock:
                if not self._current_locked(job):
                    return
            recorder = self._recorder_factory()
            with self._lock:
                if not self._current_locked(job):
                    return
                job["recorder"] = recorder

            def progress(level, elapsed):
                with self._lock:
                    if self._current_locked(job) and self._phase == "recording":
                        self._level = max(0.0, min(1.0, float(level)))
                        self._elapsed = max(0.0, min(MAX_CAPTURE_SECONDS, float(elapsed)))

            audio = recorder.record(job["stop"], job["cancelled"], progress)
            self._release(recorder)
            current = self._session()
            with self._lock:
                if not self._current_locked(job):
                    return
                if current != job["session"]:
                    raise STTError("session")
            _validate_audio(audio)
            with self._lock:
                if not self._current_locked(job):
                    return
                self._phase, self._level = "transcribing", 0.0
            text = self._transcribe(audio, key, model)
            if not isinstance(text, str) or not text.strip():
                raise STTError("response")
            current = self._session()
            with self._lock:
                if not self._current_locked(job):
                    return
                if current != job["session"]:
                    raise STTError("session")
                self._next_result_id += 1
                self._result = {"id": self._next_result_id, "session_id": job["session"], "text": text.strip()}
                self._phase, self._error = "idle", None
                self._job = None
        except Exception as error:
            with self._lock:
                if self._current_locked(job):
                    self._phase = "error"
                    self._error = str(error) if isinstance(error, STTError) else str(STTError())
                    self._result = None
                    self._level = 0.0
                    self._job = None
        finally:
            self._release(recorder)
            with self._lock:
                if self._job is job:
                    self._job = None
