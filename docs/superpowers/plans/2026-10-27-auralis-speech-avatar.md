# Auralis speech input and geometric avatar implementation plan

**Goal:** Implement roadmap 6 (cloud speech-to-text with a shortcut) and 4 (geometric, speech-reactive 2D companion) on the Desktop code from PR #5.

**Architecture:** One native Python SpeechController per ApplicationRuntime owns lazy microphone capture and background Groq Whisper transcription. Both Desktop and Terminal consume the same controller state/results. Canvas2D draws layered projected geometry from actual TTS envelopes and microphone levels; no 3D model/runtime is introduced.

## Decisions within the approved roadmap

- Cloud-first Groq Whisper, default whisper-large-v3-turbo with whisper-large-v3 as a choice. GROQ_API_KEY configured locally via API keys; no new chat provider. Free-plan availability/quota depends on the user's Groq account.
- F7 toggles recording/stop while Auralis has focus. Users can choose F9/F10 instead. No global keyboard hooks or always-on microphone.
- Native default microphone, maximum 60 seconds, in-memory WAV, no saved audio. Recording and transcription remain cancellable; late results are suppressed on cancel/close/chat change.
- Append the transcript to the originating chat's editable draft exactly once, then acknowledge it; never automatically send it. Errors preserve existing text.
- New Astral lattice avatar replaces old preset choices; migrate old saved preset names while preserving appearance settings. Hollow layered wireframes, luminous core and radial rays, restrained idle and stronger audio-driven speech reaction. Respect reduced motion and motion/glow preferences.
- No vision/tools/VTuber work; no unapproved save/index migration.

## Interfaces

SpeechController(get_session_id, before_record=None, recorder_factory=None, transcribe_fn=None): settings/configure, state, start(session_id), stop, cancel, acknowledge(id), close. State includes phase, level, elapsed, safe error, result{id,session_id,text}, model, shortcut and configured status.

GET /api/stt and POST /api/stt actions start/stop/cancel/ack/configure; start binds to session_id. /api/state includes speech status. /api/stop cancels both playback and speech input. settings.json stt is independent from chat provider; credentials status adds speech.

## Tasks

- [x] STT engine: failing capture/transcription/cancel/config/security tests; lazy optional microphone backend and bounded cloud adapter.
- [x] Runtime/API: startup/shutdown lifecycle, model/mic concurrency guards, authenticated routes, safe credential setup, optional settings compatibility.
- [x] Desktop: mic button, F7 hint, input settings, status/cancel, transcript ack and draft/session guards; synthetic JS and Chromium checks.
- [x] Terminal: mic shortcut, /stt settings picker, transcript insertion/ack, cancellation/status, preserve command/history behavior.
- [x] Avatar: depth-sorted layered geometry/core/rays; deterministic audio/motion tests and preference migration.
- [x] Integrate full Python/JS tests, visual inspection, independent review/fixes and updated docs.

Delivery: publish a focused draft PR and verify Linux/Windows CI. Publication and CI status are tracked in that PR.

## Review focus

- Capture resources released on stop/cancel/error/close, including when HTTP returns late.
- Old generation cleanup cannot stop a newer microphone recording or insert stale text.
- Results go once to the correct draft and never trigger an automatic chat request.
- Capture configuration failures preserve working settings; secrets/audio never leak to logs, URLs or responses.
- Frame-rate-independent audio response, no stale pulses, reduced motion and hidden canvas behavior.

## Sources

Official API details checked 2026-10-27: https://console.groq.com/docs/speech-to-text and https://console.groq.com/docs/rate-limits . Capture uses mono PCM WAV; requests are bounded below the documented file-size limits.
