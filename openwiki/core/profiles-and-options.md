---
type: subsystem
title: Profiles and the option registry
description: How a termdeck Profile and its SSH, serial and terminal sections are declared once with option() metadata, validated with OptionError naming the faulty field, described to UIs by describe_options(), and how quick targets like bob@host:2222 or COM3@9600 become unsaved profiles.
tags: [profiles, options, validation, forms, quick-connect]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-18T13:56:19.871Z
sources:
  - id: openwiki-source-40dcfdf1b085605444d2f636
    resource: repo://src/termdeck/cli.py
  - id: openwiki-source-90432da0367e8c0e89155e47
    resource: repo://src/termdeck/errors.py
  - id: openwiki-source-4003fa8570daad2a67aaf127
    resource: repo://src/termdeck/profiles.py
generated: { by: "claude-code", at: "2026-10-18T13:56:19.871Z" }
---

# Profiles and the option registry

`src/termdeck/profiles.py` is the single source of truth for what a connection can be
configured with. Every option is a dataclass field declared with `option()`, and every UI
builds its settings form from that declaration instead of hand-coding fields (a project rule,
see [Development conventions](../development/conventions.md)). Storage is covered in
[Profile storage](profile-store.md).

## Shape of a profile

```
Profile
├── name, kind ("ssh" | "serial"), group        ← section "profile"
├── ssh: SSHSettings                            ← section "ssh"
├── serial: SerialSettings                      ← section "serial"
└── terminal: TerminalSettings                  ← section "terminal" (both kinds)
```

- `SSHSettings`: host, port, username, key file, port forwards (`list[Forward]`), then
  advanced groups — what runs on the server (remote command, subsystem, no shell, pty, tty
  modes, environment), connection (jump host or jump command, timeouts, keepalives, address
  family, compression, rekeying, `~/.ssh/config`, connection sharing), proxy settings,
  authentication (agent, keyboard-interactive, GSSAPI, certificate, trivial-login refusal),
  host keys (pinned fingerprints, logical host name) and algorithm lists.
- `SerialSettings`: device, baud rate, follow-the-device and the hidden `usb_id`, data bits,
  parity, stop bits, flow control, DTR/RTS.
- `TerminalSettings`: font size, theme, line editing, implicit CR/LF, bell, close-on-exit;
  advanced keys and characters (terminal type, character set, Enter/Backspace, Home/End,
  function keys, answerback), appearance, scrollback, "remote may …" permissions, printer
  file and session logging.

`KINDS = {"ssh": "SSH", "serial": "Serial"}`; `TRANSPORTS` maps each kind to its transport
module (see [Architecture overview](../architecture/overview.md)).

## `option()` metadata

`option(default, label, help, *, advanced, choices, widget, kinds, hidden, min, max, wide,
group, when)` returns a `dataclasses.field` whose `metadata` drives forms and validation:

| Key | Meaning |
|---|---|
| `label`, `help` | Field label and help text, the same in every UI |
| `advanced` | Shown only behind the **Advanced** switch |
| `choices` | `{stored value: label}`; the value must be one of the keys |
| `widget` | Input refinement: `path`, `secret`, `multiline`, `device`, `forwards` |
| `kinds` | Connection types the option applies to; defaults to the section's (`SECTION_KINDS`) |
| `hidden` | Stored and preserved, never shown (e.g. `serial.usb_id`) |
| `min`/`max` | Numeric bounds |
| `wide`, `group` | Full-width field; subheading for related advanced options |
| `when` | `(other key, values)`: show only while that option has one of the values, or (values `None`) while it is set at all |

List defaults get a `default_factory`. A field without `label` metadata is not an option.

## Describing options to UIs

`describe_options()` returns, per section (`profile`, `ssh`, `serial`, `terminal`), a JSON-ready
list of `{key, type, default, label, help, advanced, widget, wide, min, max, choices, kinds,
group, when}`. `type` is the Python type name (or `forwards`); `wide` is forced for path,
multiline and device widgets; `kinds` falls back to the section's kinds. `option_fields(section)`
gives the raw fields, excluding hidden ones unless asked. `SECTION_TITLES` holds the form
headings shared by all UIs.

Consumers:
- The web UI serves it at `/api/options` and renders forms in `app.js` ([Web UI](../interfaces/web-ui.md)).
- The TUI's `ProfileEditor` builds Textual inputs from it ([TUI](../interfaces/tui.md)).
- The CLI's `termdeck options` lists it, and `termdeck set NAME section.option=value` /
  `--set` coerce text with `cli._coerce` (bools as true/false/yes/no/on/off/1/0, numbers,
  `\n` escapes) then `check_option` ([CLI](../interfaces/cli-and-console.md)). Forwards are
  not settable this way; they use `-L/-R/-D`.

## Validation

`Profile.validate()` checks every field generically with `check_option` (value in `choices`;
type match with bools never accepted as numbers; `min`/`max`; negative numbers refused when no
minimum is declared), then cross-field rules:

- name not empty, no control characters in name or group; known `kind`;
- SSH: host required and safe for `known_hosts` (`check_host` rejects whitespace, `,*?!#|[]`
  and a leading `@`); jump host parses; forwards valid; `env` is `NAME=value` lines; only one
  of no-shell / subsystem / remote command; tty mode names known to asyncssh; trusted keys
  normalized to `SHA256:`/`MD5:` fingerprints (a pasted public key blob is hashed); algorithm
  lists checked against asyncssh's supported names; proxy host or command present for the
  chosen proxy type; agent forwarding requires using the agent;
- serial: device normalized (`com3`/`\\.\COM3` → `COM3`), required, and URL schemes limited
  to `rfc2217`, `socket`, `loop`, `cp2110`;
- terminal: colour overrides parse, answerback has no control characters (an ENQ would make
  it type a command), the character set is a known codec.

Validation also *normalizes* values (device names, fingerprints), so it mutates the profile.
Errors are raised as `OptionError(message, section, key)` — the `_option(section, key)` context
manager converts any `ProfileError` raised inside — so editors can mark and focus the field.
`OptionError` is a `ProfileError`, which is a `TermdeckError` and a `ValueError`
(`errors.py`), so the CLI reports it as invalid input (exit code 2).

`Profile.from_dict(data, validate=True)` checks structure, ignores unknown keys, fills missing
ones with defaults, turns forward dicts into `Forward`s (empty listen host becomes
`localhost`), and validates unless `validate=False` (used to repair invalid stored entries).

## Forwards

`Forward(kind, listen_port, listen_host="localhost", dest_host, dest_port)` with kinds
`local`, `remote`, `dynamic`. `parse_forward(kind, spec)` accepts OpenSSH syntax
`[bind:]port:host:hostport` (`[bind:]port` for dynamic; IPv6 in brackets). `Forward.public`
is true when the listen host is not loopback; UIs flag such forwards.

## Quick targets

`resolve_target(text, profiles)` returns a saved profile by exact name, else
`parse_target(text)`, else raises `ProfileNotFound` with a suggestion from `unknown_target`
(closest profile name, or how to write a dotless host).

`parse_target` builds an unsaved profile from:
- serial: `COM3`, `\\.\COM3`, `/dev/…` or a URL, optionally `@baud` (`COM3@9600`);
- SSH: `[user@]host[:port]`, but **only** with a user, a port, a dot, or a bracketed IPv6
  address. A bare word is refused because it is more likely a mistyped profile name, and
  connecting would send a password to whatever host it resolves to.

## Related

- [Profile storage and private files](profile-store.md)
- [SSH transport](../transports/ssh.md), [Serial transport](../transports/serial.md)
- [Sessions and TerminalSession](sessions.md) — where terminal settings take effect
