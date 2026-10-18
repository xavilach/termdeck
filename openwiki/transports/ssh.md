---
type: subsystem
title: SSH transport
description: How termdeck's SSH transport (ssh.py, on asyncssh) builds a connection from a profile — username, authentication methods and password changes, host key verification against termdeck's known_hosts with confirmation, pinning and revocation, algorithms, jump hosts and proxies — then opens port forwards and a shell, command, subsystem or forwards-only session, shares connections between tabs, and reports exit statuses and errors.
tags: [ssh, asyncssh, authentication, host-keys, known-hosts, port-forwarding, jump-host, connection-sharing]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-18T13:56:19.871Z
sources:
  - id: openwiki-source-29d66090aef4ef6e672b9810
    resource: repo://src/termdeck/ssh.py
generated: { by: "claude-code", at: "2026-10-18T13:56:19.871Z" }
---

# SSH transport

`src/termdeck/ssh.py` implements the transport for `kind == "ssh"` on top of asyncssh. It is
imported lazily ([Architecture](../architecture/overview.md)) and exposes `open_session`,
`describe_error` and `ERRORS = (asyncssh.Error,)`. Proxies are in
[Proxies and proxy commands](proxies.md); SFTP in [SFTP](sftp.md).

## `open_session(profile, prompter, cols, rows, scripted)`

1. With `share_connection`, look up a live connection of the same login (below) and open a new
   channel on it.
2. Otherwise `SSHConnection.connect(profile, prompter)`.
3. `start_forwards()`; its messages (failures, public listeners) become session notices.
4. `open_shell(cols, rows, notices, separate_stderr=scripted)`.

Any failure after connecting releases the connection.

## Connecting (`SSHConnection.connect`)

With a jump host, `jump_profile` derives the jump host's profile (`[user@]host[:port]`, the
target's connection options inherited, but like `ssh -J` no agent/X11 forwarding, environment,
command, forwards, logical host or pinned keys) and connects to it first, recursively; the jump
is closed if the target fails.

`_connect` then builds asyncssh options:

- **Username**: the profile's, else the local user if `username_from_env`, else asked
  (`login as (host):`); cancelling fails with `ConnectError`. With `use_ssh_config`
  `~/.ssh/config` is read (`config=()`; otherwise nothing is read, like PuTTY) and options left
  at their default are *not* passed, so the config file's values apply; the port is left to the
  config when it is 22.
- **Connection**: connect timeout, keepalive interval, TCP keepalive, address family,
  compression (`zlib@openssh.com, zlib, none` or `none`), rekey after N minutes / MB (0 =
  never), agent forwarding.
- **Authentication**: agent (`agent_path=None` when `use_agent` is off), private key file
  loaded in a thread and asked for a passphrase if encrypted, certificate, keyboard-interactive,
  GSSAPI auth/key exchange/credential delegation, refusing trivial authentication.
- **Algorithms**: cipher, key exchange, MAC, host key and signature lists checked against
  asyncssh; legacy choices (CBC, 3DES, arcfour, SHA-1, `ssh-rsa`, `ssh-dss`…) produce a
  "weak … allowed" notice.
- **Tunnel**: jump command, jump connection, or proxy — see [Proxies](proxies.md).
- `logical_host` → `host_key_alias`.

### Authentication callbacks (`_Client`)

- Passwords: at most `MAX_PASSWORD_TRIES` (3) prompts `user@host[:port] password:`. The last
  password is kept only to answer an expired-password change (asked twice, must match) and
  cleared after authentication or connection loss.
- Keyboard-interactive: the challenge's name and instructions are shown through `notify`;
  each prompt is prefixed `(user@host) ` like OpenSSH, because the text comes from the server,
  which could otherwise pretend to be termdeck; echo flags decide secret input; same 3-try limit.
- The authentication banner is shown through `notify` before the prompts. All server text is
  passed through `printable`.

### Host keys

termdeck keeps its own `known_hosts` in the config folder (created private on first use) and
also reads `~/.ssh/known_hosts`. asyncssh calls `validate_host_public_key` only for keys not
already trusted; termdeck records the key, where asyncssh looked it up (after ssh config and
logical host), the known keys for that host and whether any key or CA exists (`changed`), and
refuses — the handshake fails. `connect` then builds a `HostKey` (type, SHA-256 fingerprint,
old fingerprints, `known_hosts` path) and asks `prompter.confirm_host_key`. If trusted,
`remember_host_key` saves it (under a file lock, atomically) and the connection is retried once.

- `remember_host_key` writes `host` (or `[host]:port`) with the key, drops the host's previous
  line and any revocation of this key, and appends the replaced keys of the **same type** as
  `@revoked`, so a copy still trusted elsewhere is refused; other key types stay valid.
- `forget_host(host, port)` removes every line for that host, revocations included
  (`termdeck forget-host`). A revoked key leads `describe_error` to suggest exactly that.
- **Pinned keys** (`trusted_keys`, `connect --hostkey`): `known_hosts` is not used at all; a key
  whose SHA-256 or MD5 fingerprint is listed is accepted, anything else fails with
  "not one of the profile's Trusted host keys", without asking or saving.

## Port forwards

`start_forwards()` opens each profile forward: local (`forward_local_port`), remote
(`forward_remote_port`) or dynamic SOCKS (`forward_socks`). Each gets a `ForwardStatus`
(`active`, actual `port` — useful for port 0 — or `error`); failures do not abort the
connection. Forwards on non-loopback addresses produce a warning. UIs show these via
`Session.forwards()` and the controller's summary ([SessionController](../core/session-controller.md)).

## Sessions

`open_shell` returns, for the tab's own profile:

- **`SSHForwards`** when `no_shell` (like `ssh -N`): `interactive = False`, a notice listing the
  forwards, `read()` waits until the connection closes, input is ignored, and EOF on stdin does
  not close it.
- **`SSHShell`** otherwise, via `create_process` with the remote command, subsystem or login
  shell, `env` from the profile, X11 forwarding (display from the profile or `$DISPLAY`; on
  Windows `:0` becomes `localhost:0`, and forwarding without an X server is refused with a
  hint), and a pty when `pty` is on: terminal type, size and `term_modes` — speed, `VERASE`
  following the Backspace setting and `IUTF8` following the character set unless the profile's
  `tty_modes` override them (PuTTY's "Auto"). Without a pty and in scripted mode, stderr is kept
  separate (`read_stderr`); otherwise merged.
  - When output ends, `_ended` records `exit_status`; a signal becomes `128 + n` and
    "Remote program killed by signal X"; a non-zero status becomes "Remote program exited with
    status N" (an unclean end); no status means the connection went away, with the server's
    disconnect reason if any.
  - `resize` only with a pty; `write` waits for the channel to drain (piped input).
- **`SSHStream`** from `open_stream(host, port)` (`connect --nc`, like `ssh -W`).

All three expose `sftp()` on the same connection and the connection's forward statuses.

## Connection sharing

With `share_connection`, `open_session` reuses a live connection in this process whose
`_share_key` matches: username, host (case-insensitive) and every SSH setting except
per-channel ones (`CHANNEL_SETTINGS`: command, subsystem, no shell, pty, tty modes, speed, env,
X11 display/auth, the sharing flag, username, host). So tabs that differ in agent or X11
forwarding or port forwards get their own connection. A profile without a known username is
never shared. The shared connection counts `users`; closing a tab closes only its channel and
`release()` closes the connection (and its jump host) when the last user is gone. The new tab
gets a "sharing the connection of …" notice.

## Error messages

`describe_error` maps asyncssh errors: untrusted host key ("connection aborted", or the revoked
key hint), `PermissionDenied` → "Authentication failed", connection lost / disconnected with
the reason, and key import errors. Other exceptions fall back to the core rules
([Sessions](../core/sessions.md)).

## Tests

`tests/test_ssh.py` and `tests/test_ssh_options.py` run against the local asyncssh server
fixture ([Test suite](../testing/test-suite.md)): password, keyboard-interactive, banners,
expired passwords, keys and passphrases, host key confirmation, changes, revocation and
pinning, forwards, sharing, jump hosts and jump commands, proxies, terminal modes, environment,
exit statuses and every SSH option's effect.

## Related

- [Security model](../security/model.md)
- [Profiles and the option registry](../core/profiles-and-options.md)
