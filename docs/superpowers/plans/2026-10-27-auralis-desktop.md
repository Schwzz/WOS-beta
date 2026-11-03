# Auralis Desktop Implementation Plan

**Goal:** Implement approved roadmap 7–8 and terminal shortcut/spacing tweaks from merged main (5979dce).

**Architecture:** A shared ApplicationRuntime owns Session, one speaker, and OverlayBackend/Server. Terminal keeps its existing command loop; Desktop hosts the same services without importing/running that loop. A standard pywebview window hosts a chat-first web interface. Existing file formats and providers remain compatible.

## Scope and acceptance

- Footer advertises Ctrl+B; reduce spacing between messages without changing Markdown paragraph content.
- Normal desktop frame, native minimize/maximize/resize, no pin/custom corner; persist screen-clamped geometry.
- Separate Desktop (default run.bat) and Terminal launchers. No remembered chooser yet.
- Desktop owns startup, voice suppression, server shutdown, and cleanup without a terminal; missing dependencies/errors must be visible even under pythonw.
- CRUD chats with destructive confirmations, character switch, provider/model discovery, masked locally saved API keys, voice toggle/Stop, optional existing avatar.
- Drafts and selections survive cancelled/failed operations; stale state mutations rejected. Reuse the session lock and API token checks.
- No STT, vision, tools, avatar redesign, or unrelated persistence migration. The four prior reliability fixes were recommended but not explicitly approved; do not silently expand into the save/index fixes. Native obsolete resize path is replaced and frontend history retry addressed as part of touched code.

## Tasks

- [x] Runtime: write failing lifecycle/ownership tests; implement core/runtime.py and wire start.py, preserving terminal commands and restart.
- [x] Management API: test shared chat actions, stale/busy behavior, credentials security and save failures; implement desktop_service and credential persistence; expose authenticated routes.
- [x] Window: test normal frame, geometry, shutdown and missing dependency behavior with fake native host; implement self-contained window and launchers.
- [x] Frontend: build full Desktop chat, keep shared send/provider/voice flow, test CRUD/settings/draft preservation with synthetic backend.
- [x] Terminal: exercise visible sidebar binding and compact messages; keep keyboard/history/draft regressions.
- [x] Integrate, run complete Python and JS suite, render/inspect desktop and terminal, review diff, fix introduced important regressions, publish draft PR and report.

## Review focus

- Partial writes, chat limits and active-chat deletion; existing persistence findings remain documented.
- GUI close while requests/pickers/voice are active must suppress late speech and free endpoint.
- API keys must never appear in returned JSON/logs/history or repository files; save before updating process state.
- Stale async history/models/chat responses and per-chat drafts after failed actions.
- Windows launch without a console must surface startup failures; native tests are simulated in Linux and need desktop smoke testing later.
