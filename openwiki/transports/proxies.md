---
type: subsystem
title: Proxies and proxy commands
description: How termdeck reaches an SSH server through a proxy — when the profile's proxy applies (exclusion patterns, local hosts), the SOCKS 4/4a, SOCKS 5, HTTP CONNECT and PuTTY-style Telnet proxy handshakes, local proxy commands, jump-host commands, and how each plugs into asyncssh as a tunnel object.
tags: [ssh, proxy, socks, http-connect, proxy-command, jump-host, networking]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-18T13:56:19.871Z
sources:
  - id: openwiki-source-b5c22fc8e89663ffbd0c9a05
    resource: repo://src/termdeck/proxy.py
  - id: openwiki-source-29d66090aef4ef6e672b9810
    resource: repo://src/termdeck/ssh.py
generated: { by: "claude-code", at: "2026-10-18T13:56:19.871Z" }
---

# Proxies and proxy commands

`src/termdeck/proxy.py` provides the ways the **first hop** of an SSH connection can avoid a
direct TCP connection. Every mechanism is an object with
`create_connection(protocol_factory, host, port)` that `ssh.SSHConnection._connect` passes to
asyncssh as its `tunnel` option. Once a jump host is in use, the jump connection carries the
rest and the proxy settings apply only to reaching the jump host (see
[SSH transport](ssh.md)).

## Choosing the tunnel

In `SSHConnection._connect` (in this order):
1. a jump host with `jump_command` → `ssh.JumpCommandTunnel`;
2. a jump host → the jump connection itself;
3. `uses_proxy(settings, host)` → `CommandTunnel` for `proxy_type == "command"`, else
   `ProxyTunnel`;
4. otherwise a direct connection.

When a proxy is used, the prompter is notified "connecting through the …" (like PuTTY's proxy
diagnostics).

`uses_proxy` returns false for `proxy_type == "none"`. For `localhost`, `127.0.0.1`, `::1`
and `*.localhost` it follows `proxy_local` (off by default). Otherwise the host is proxied
unless it matches a `proxy_exclude` pattern — patterns separated by commas, semicolons or
spaces, as in PuTTY: CIDR networks (`10.0.0.0/8`, matched for IP addresses only), shell
wildcards (`*.corp`), or a leading dot for a domain suffix.

## `ProxyTunnel`: SOCKS and HTTP

Connects a non-blocking socket to the proxy (`proxy_port` or the default: 1080 for SOCKS,
3128 for HTTP, 23 for Telnet), performs the handshake, then hands the socket to
`loop.create_connection`. The socket is closed on any failure. All failures are
`ConnectError` with a message naming the proxy.

- **Name resolution** (`proxy_dns`): `yes` lets the proxy resolve host names, `no` resolves
  locally, `auto` (default) lets the proxy resolve except for SOCKS 4, whose servers often lack
  4a. Locally resolved names prefer IPv4 for SOCKS 4.
- **SOCKS 4/4a**: IPv6 targets are refused (use SOCKS 5); names go as 4a requests; reply code
  90 means success.
- **SOCKS 5**: offers no-auth, plus username/password when a proxy username is set (both
  limited to 255 bytes); explains when the proxy accepts none of the methods (hinting that a
  username may be needed); maps reply codes to readable reasons.
- **HTTP**: `CONNECT host:port` (IPv6 bracketed) with Basic `Proxy-Authorization` when a
  username is set; the reply is read byte by byte up to the blank line so no SSH bytes are
  consumed, capped at 16 KB; non-200 replies are reported, with a hint for 407 without a
  username.
- **Telnet** (PuTTY's): sends `proxy_telnet_command` (default `connect %host %port\n`) with
  `%host`, `%port`, `%user`, `%pass` (asks for the password only if used), `%%`, `\n`, `\r`
  substituted; afterwards the connection is the target's.

The proxy password is asked through the prompter at most once per connect — also across the
host-key retry, which reconnects.

## `CommandTunnel`: local proxy command

`proxy_command(settings, host, port, username)` splits the command (POSIX rules elsewhere; on
Windows without backslash escapes, surrounding quotes removed) and replaces `%host`, `%port`,
`%user`, `%proxyhost`, `%proxyport` and `%%`. `CommandTunnel` starts it with
`loop.subprocess_exec` — in its own session on POSIX, with `CREATE_NO_WINDOW` on Windows so no
console window flashes from the desktop app. Its stdin/stdout carry SSH; stderr is collected
separately (last 10 lines) instead of being fed into the SSH stream, and when the command's
output ends the connection is lost with "proxy command X ended: <its stderr>". `connect
--proxycmd` sets this from the command line.

## `JumpCommandTunnel` (in `ssh.py`)

PuTTY's "SSH to proxy and execute a command": over an authenticated jump connection, runs
`jump_command` (`%host`, `%port`, `%user`, `%%` substituted; e.g. `nc %host %port`) or a
subsystem (`subsystem:NAME`), and uses that process's stdin/stdout as the transport for the
target connection.

## Tests

`tests/test_ssh_options.py` runs the local SSH server behind small in-test proxies
(`proxy_for` fixture): SOCKS 5 with and without passwords, SOCKS 4 and 4a, `proxy_dns`,
HTTP CONNECT with Basic auth, 407 and refusals, local-host bypass, a proxy that is not
listening, and proxy commands (including `nc`) with their substitution rules.

## Related

- [SSH transport](ssh.md)
- [Profiles and the option registry](../core/profiles-and-options.md) (proxy options and validation)
