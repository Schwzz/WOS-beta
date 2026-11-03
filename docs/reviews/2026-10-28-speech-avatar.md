# Auralis speech input and Astral lattice review

## Delivered behavior

- Optional native microphone capture and Groq Whisper cloud transcription, shared by Desktop and Terminal. Default F7 toggles start/finish while focused; settings offer F9/F10 and either supported Whisper model.
- Captures use the default input device, mono PCM, a 60-second bound and an in-memory WAV. No audio file is saved. Short/silent/invalid captures fail before upload; key/quota/network/device errors use safe messages.
- Desktop provides microphone, level/time/status, cancellation and speech settings controls. Groq has a separate masked key entry, without becoming a chat provider. Terminal exposes a Mic footer hint and `/stt` settings; the basic prompt has explicit start/stop/cancel/result commands.
- Transcripts append once to the originating editable draft and never automatically send. Typed text remains intact. Active input blocks sends and session changes. Cancellation, session changes and shutdown invalidate old generations and suppress late HTTP results.
- Astral lattice replaces the old crystal/orb/rings/wave styles with layered projected wireframes, a point-star core, depth shading and rays. TTS envelopes and microphone input drive expansion/light; quiet audio decays smoothly. Existing accent/theme/size/motion/glow preferences survive migration.
- Reduced motion freezes geometric movement and expansion. Zero glow removes halos/rays/motes while preserving the essential wireframe and star. Hidden or suspended views do not replay expired audio peaks on reopening.

## Review corrections

Independent review found three gaps, reproduced before correction:

1. The basic command loop could raise an uncaught busy error for a normal message during dictation, and session commands could bypass the input guard. It now rejects those actions with a notice while retaining speech/exit controls. Ctrl+C cancels recording/transcription and keeps the prompt available.
2. Terminal Escape cancellation depended on composer focus, and the active-input guard also blocked `/stop`. A fallback application binding now handles sidebar focus without overriding composer/modal Escape behavior; stop/cancel/quit commands remain usable during dictation.
3. The renderer skipped envelope release while hidden/zero-sized and clamped away long suspended-frame intervals. Release now advances using elapsed wall time before deciding whether to draw, while rotation still uses bounded frame time.

## Verification

**645 Python tests pass locally**, along with all three deterministic JS harnesses, syntax/compile checks and a real headless Chromium smoke. Independent review has no unresolved findings after the corrections above. The PR records Linux/Windows CI outcomes. New checks exercise controller generations, capture shutdown, model/settings validation, sanitized HTTP errors, authenticated STT routes, draft insertion/ack retry, shortcut rebinding, cancellation from different focus targets, plain-prompt safety, preference migration, audio expansion, hidden-view decay and reduced motion.

Tests use temporary project data, fake microphone streams, synthetic WAVs and provider responses. No microphone recording, live Groq call, paid chat request or user credential is required. Desktop screenshots use synthetic data. Windows audio permissions, physical devices, native WebView2 focus/function keys and real transcription need a device smoke test.

## Remaining limits and follow-ups

- Cloud only; no offline fallback, input-device picker, global hotkey or hold-to-talk mode. Quotas/access are determined by the Groq account.
- Cancellation discards results and releases capture; an HTTP request already sent may finish at the service. Speech HTTP timeouts are bounded and no upload is retried automatically.
- One input consumer per runtime. Desktop and Terminal have separate launchers and project ownership prevents concurrent instances. Extra development pages using `--overlay` share one transcript queue.
- The existing chat/index transaction and corrupt-index recovery findings remain unfixed. They are separate from this feature; see [the earlier review](2026-10-27-auralis.md).
- API keys remain in the ignored local `.env`, without OS vault encryption. Real input devices/permissions remain a manual verification item.
- Vision, tool execution, remembered interface choice and 3D VTuber models remain outside this update.
