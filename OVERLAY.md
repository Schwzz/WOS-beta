# Companion overlay replaced by Desktop

The old floating Companion window has been replaced by **Auralis Desktop**.
See [DESKTOP.md](DESKTOP.md) for installation, native window controls, saved chats,
provider/API-key setup, voice, and troubleshooting. `run.bat` now opens Desktop;
`run-terminal.bat` opens Terminal independently.

The existing avatar is available in Desktop's optional Companion panel. Its
redesign is separate future work. Internal protocol/file names containing
`overlay` remain for compatibility; they do not mean an always-on-top window is
still launched. `python start.py --overlay` remains a development option to serve
the local chat page from a terminal, but `overlay/main.py` now starts Desktop's
own runtime and should not be launched alongside it.
