# Requirements

What the owner asked for. `features-reviewer` checks the code against this list and against PuTTY; `interface-reviewer` checks the UIs against the UI part. Add new requests here.

## Features

- SSH sessions.
- Serial console sessions.
- Saved sessions (profiles).
- Port forwarding: local, remote, dynamic (SOCKS).
- SFTP file transfer.
- Cross-platform, Windows first (COM ports).
- Recognize a serial device after its COM port number changes (USB identity).
- Local line editing for primitive serial devices: edit and history locally, send on Enter.
- Every option PuTTY offers for SSH and serial, unless listed as out of scope below.

## Interfaces

- One core, several interfaces: CLI, interactive TUI, local web UI, desktop GUI.
- `termdeck` opens the TUI; `termdeck connect` works like `ssh` in the current terminal.
- Complete options, rarer ones behind an "Advanced" switch.
- Only options for the selected profile type are shown (no SSH options for serial).
- Professional look, like an established desktop tool, not a generic AI-generated web page.

## Out of scope (unless requested)

- Telnet, Rlogin, Raw, SUPDUP connections.
- PuTTYgen-style key generation and a Pageant-style agent (use the system's tools).
