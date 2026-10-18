# termdeck

SSH + serial terminal. Python ≥3.11, `uv` (`uv sync --all-extras`, `uv run pytest -q`).

## Layout

- Core (no UI code): `profiles.py` (options and validation), `session.py` (Session, Prompter, terminal settings, transports), `errors.py`, `ssh.py`, `proxy.py`, `sftp.py`, `serialport.py`, `lineedit.py`, `config.py`.
- UIs: `cli.py` with `console.py` (the `connect` terminal), `tui/`, `web/` (server + static), `gui.py` (pywebview around the web UI).
- `tools/screenshots.py`: TUI and web screenshots with sample profiles, for interface reviews (web needs `uv run playwright install chromium`).

## Rules

- Every profile option is declared once with `option()` in `profiles.py`. UIs build their forms from `describe_options()`; never hand-code a form field.
- Mark an option `advanced=True` unless most users need it; restrict it with `kinds=` when it only applies to SSH or serial. Forms show only the selected type's options.
- Behaviour shared by all UIs belongs in the core (e.g. `TerminalSession`), not in a UI.
- Remote or user text never goes into Textual markup or innerHTML unescaped.
- Tests: no network, no hardware (local asyncssh server, `loop://`). Every fixed bug gets a test.
- Requests from the owner go in `REQUIREMENTS.md`.

## Agents (`.claude/agents/`)

- `conventions-reviewer`: after every change.
- `test-writer`: adds tests after a feature or fix.
- `security-reviewer`: after changes to auth, host keys, the web server, SFTP, forwarding, logs, serial control lines or secrets.
- `windows-reviewer`: after changes to serial, the console, paths and permissions, the GUI, packaging or asyncio.
- `features-reviewer`: after adding features or options, and before a release.
- `interface-reviewer`: after UI changes.
- `architecture-reviewer`: before large features, after big refactors, or periodically.
- `putty-reference`: to ask how PuTTY does something, instead of reading its source here.

Reviewers that apply to a change run in parallel; fix what they find, then run the tests again.
