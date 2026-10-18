"""Command line: launch a UI, manage profiles, run tunnels and SFTP transfers."""

import argparse
import asyncio
import getpass
import json
import os
import sys
import threading
import traceback
from pathlib import PurePosixPath

from . import __version__
from .config import open_private, user_path
from .errors import ConnectError
from .profiles import (
    Profile, ProfileFileError, ProfileStore, SerialSettings, SSHSettings, check_option, describe_options, option_fields,
    parse_forward, parse_target, resolve_target,
)
from .session import HostKey, describe_error, is_expected, open_session


def _open_terminal():
    """The user's terminal, even when stdin is a pipe (like ssh); None when there is none (cron)."""
    try:
        return open("CONIN$" if sys.platform == "win32" else "/dev/tty", encoding="utf-8", errors="replace")
    except OSError:
        return None


class ConsolePrompter:
    """Asks on the controlling terminal. Prompts go to stderr, never stdout.

    Answers never come from stdin when it is a pipe: that is the session's data, and
    a "y" in it must not trust a host key. Without a terminal, questions fail.
    """

    async def ask(self, prompt: str, secret: bool = True) -> str | None:
        loop = asyncio.get_running_loop()
        answer = loop.create_future()

        def read():
            result = None
            try:
                terminal = _open_terminal()
                if terminal is None:
                    raise ConnectError(f"{prompt.strip()} needs an answer, and there is no terminal to ask on")
                with terminal:
                    if secret:  # getpass reads the terminal itself (/dev/tty, or the Windows console)
                        result = getpass.getpass(prompt, stream=sys.stderr)
                    else:
                        print(prompt, end="", file=sys.stderr, flush=True)
                        line = terminal.readline()
                        result = line.rstrip("\r\n") if line else None
            except (EOFError, KeyboardInterrupt):
                result = None
            except ConnectError as exc:
                result = exc

            def deliver():
                if not answer.done():
                    (answer.set_exception if isinstance(result, Exception) else answer.set_result)(result)

            try:
                loop.call_soon_threadsafe(deliver)
            except RuntimeError:
                pass  # the program is exiting

        # A daemon thread, not the executor: after Ctrl+C at a prompt, asyncio.run
        # must not wait for this read to finish before the program can exit.
        threading.Thread(target=read, name="termdeck-prompt", daemon=True).start()
        return await answer

    async def confirm_host_key(self, key: HostKey) -> bool:
        where = f"{key.host}:{key.port}"
        if key.changed:
            print(f"WARNING: the host key of {where} has CHANGED. Someone may be intercepting the connection,"
                  " or the server was reinstalled.", file=sys.stderr)
        else:
            print(f"{where} is not known yet. Check its fingerprint:", file=sys.stderr)
        print(f"Now:  {key.key_type} {key.fingerprint}", file=sys.stderr)
        for old in key.old_fingerprints:
            print(f"Was:  {old}", file=sys.stderr)
        answer = await self.ask(f"Trust this key (saved in {key.known_hosts}) and continue? [y/N] ", secret=False)
        return (answer or "").strip().lower() in ("y", "yes")

    async def notify(self, text: str) -> None:
        print(text, file=sys.stderr)


def _coerce(field, text: str):
    """Convert command-line text to the option's type (choices by key, e.g. parity=E)."""
    kind = field.type
    if kind is bool:
        if text.lower() in ("1", "true", "yes", "on"):
            return True
        if text.lower() in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"expected true or false, got {text!r}")
    if kind in (int, float):
        try:
            number = float(text)
        except ValueError:
            raise ValueError(f"expected a number, got {text!r}") from None
        return int(number) if number.is_integer() else number
    return text.replace("\\n", "\n")


def _settable(section: str) -> dict:
    """Options `--set` can change: every visible option except forwards (-L/-R/-D)."""
    return {f.name: f for f in option_fields(section) if f.metadata["widget"] != "forwards"}


def apply_settings(profile: Profile, assignments: list[str]) -> None:
    """Apply section.option=value assignments, e.g. ssh.keepalive=30."""
    for item in assignments:
        key, sep, value = item.partition("=")
        section_name, dot, name = key.partition(".")
        if not sep or not dot or section_name not in ("ssh", "serial", "terminal"):
            raise ValueError(f"invalid setting {item!r}, expected ssh|serial|terminal.option=value")
        settable = _settable(section_name)
        if name not in settable:
            raise ValueError(f"unknown option {key!r}; options: {', '.join(sorted(settable))} "
                             "(termdeck options lists them all)")
        try:
            converted = _coerce(settable[name], value)
            check_option(settable[name], converted)
        except ValueError as exc:
            raise ValueError(f"{key}: {exc}") from None
        setattr(getattr(profile, section_name), name, converted)
    profile.validate()


def _forwards(args) -> list:
    return (
        [parse_forward("local", s) for s in args.local or []]
        + [parse_forward("remote", s) for s in args.remote or []]
        + [parse_forward("dynamic", s) for s in args.dynamic or []]
    )


def _warn(store: ProfileStore) -> None:
    if warnings := store.warnings():
        print(f"termdeck: warning: {store.path} has entries that cannot be used:", file=sys.stderr)
        for line in warnings:
            print(f"  {line}", file=sys.stderr)


def cmd_list(args, store: ProfileStore) -> int:
    profiles = store.load().values()
    _warn(store)
    if args.json:
        print(json.dumps([p.to_dict() for p in profiles], indent=2))
    elif not profiles:
        print("No profiles. Create one with: termdeck add ssh NAME HOST, or termdeck add serial NAME DEVICE",
              file=sys.stderr)
    else:
        for p in profiles:
            group = f"[{p.group}] " if p.group else ""
            print(f"{p.name}\t{group}{p.summary()}")
    return 0


def _accepted(o: dict) -> str:
    """What `--set` accepts for an option, in the words of the command line."""
    if o["choices"]:
        return "|".join(str(k) for k, _ in o["choices"])
    if o["type"] == "bool":
        return "true|false"
    if o["type"] in ("int", "float"):
        low, high = o["min"] if o["min"] is not None else 0, o["max"]
        return f"{low}..{high}" if high is not None else f"number >= {low}"
    return "text (\\n for a new line)" if o["widget"] == "multiline" else "text"


def cmd_options(args, store: ProfileStore) -> int:
    """Every option for --set, from the same declarations the forms use."""
    rows = []
    for section, options in describe_options().items():
        if section == "profile":
            continue
        for o in options:
            if o["type"] == "forwards":
                continue
            default = json.dumps(o["default"]) if o["default"] not in ("", None) else "(empty)"
            rows.append((f"{section}.{o['key']}", _accepted(o), f"default {default}", o["label"]))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)) + "  " + row[3])
    print("\nSet them with: termdeck set NAME section.option=value, or --set on add and connect.")
    return 0


def cmd_show(args, store: ProfileStore) -> int:
    print(json.dumps(store.get(args.name).to_dict(), indent=2))
    return 0


def cmd_add(args, store: ProfileStore) -> int:
    if args.kind == "ssh":
        profile = Profile(args.name, "ssh", group=args.group, ssh=SSHSettings(
            host=args.target, port=args.port, username=args.user or "", key_file=args.identity or "",
            forwards=_forwards(args),
        ))
    else:
        profile = Profile(args.name, "serial", group=args.group, serial=SerialSettings(
            device=args.target, baudrate=args.baud, bytesize=args.databits, parity=args.parity,
            stopbits=args.stopbits, flow=args.flow,
        ))
    apply_settings(profile, args.set or [])
    store.add(profile)
    print(profile.summary())
    return 0


def cmd_set(args, store: ProfileStore) -> int:
    profile = store.get_raw(args.name)  # a profile that fails validation can be repaired here
    apply_settings(profile, args.settings)
    if args.local or args.remote or args.dynamic:
        profile.ssh.forwards = _forwards(args)
    if args.no_forwards:
        profile.ssh.forwards = []
    store.update(args.name, profile)
    print(profile.summary())
    return 0


def cmd_rename(args, store: ProfileStore) -> int:
    profile = store.get(args.name)
    profile.name = args.new_name
    store.update(args.name, profile)
    print(f"renamed {args.name!r} to {args.new_name!r}", file=sys.stderr)
    return 0


def cmd_rm(args, store: ProfileStore) -> int:
    store.delete(args.name)
    print(f"deleted {args.name!r}", file=sys.stderr)
    return 0


def cmd_forget_host(args, store: ProfileStore) -> int:
    from .ssh import forget_host

    target = parse_target(args.host if ":" in args.host or "." in args.host else f"{args.host}:22")
    if target is None or target.kind != "ssh":
        raise ValueError(f"expected HOST or HOST:PORT, got {args.host!r}")
    removed = forget_host(target.ssh.host, target.ssh.port)
    where = f"{target.ssh.host}:{target.ssh.port}"
    print(f"forgot {removed} key line(s) for {where}" if removed else f"no keys saved for {where}", file=sys.stderr)
    return 0


def cmd_ports(args, store: ProfileStore) -> int:
    from .serialport import available_ports

    ports = available_ports()
    if not ports:
        print("No serial ports found.", file=sys.stderr)
    for port in ports:
        print("\t".join((port["device"], port["description"], port["usb_id"])).rstrip())
    return 0


async def _ssh_connect(name: str, store: ProfileStore):
    from .ssh import SSHConnection

    profile = store.get(name)
    if profile.kind != "ssh":
        raise ValueError(f"{name!r} is not an SSH profile")
    return await SSHConnection.connect(profile, ConsolePrompter())


def resolve_profile(target: str, store: ProfileStore) -> Profile:
    """A saved profile by name, else a quick target such as user@host or COM3@9600."""
    try:
        profiles = store.load()
    except ProfileFileError as exc:  # quick targets (bob@host, COM3) still work
        print(f"termdeck: warning: {exc}", file=sys.stderr)
        profiles = {}
    _warn(store)
    return resolve_target(target, profiles)


class BatchPrompter(ConsolePrompter):
    """--batch: never ask anything; a question means the connection fails (plink -batch)."""

    async def ask(self, prompt: str, secret: bool = True) -> str | None:
        raise ConnectError(f"{prompt.strip()} needs an answer, and --batch never asks")

    async def confirm_host_key(self, key: HostKey) -> bool:
        where = f"{key.host}:{key.port}"
        if key.changed:  # loud in scripts' logs too: it may be an attack
            print(f"termdeck: WARNING: the host key of {where} has CHANGED: {key.key_type} {key.fingerprint}",
                  file=sys.stderr)
            for old in key.old_fingerprints:
                print(f"termdeck: was {old}", file=sys.stderr)
        else:
            print(f"termdeck: host key of {where} is not trusted ({key.key_type} {key.fingerprint})", file=sys.stderr)
        print("termdeck: --batch never asks; connect once without it to check and trust the key", file=sys.stderr)
        return False


class PasswordFilePrompter:
    """--password-file: the target's first password prompt is answered from a file, the rest asked as usual.

    Only the target's: a jump host, a key passphrase or a server-written question never gets it.
    """

    def __init__(self, inner, path: str, host: str):
        self._inner = inner
        self._for = f"@{host} password: "  # the prompt of ssh._Client.password_auth_requested
        self._kbdint = f"@{host}) "         # keyboard-interactive prompts are prefixed "(user@host) "
        lines = _read_text(path, "password file").splitlines()
        if not lines or not lines[0]:
            raise ValueError(f"password file {path} is empty")
        self._password: str | None = lines[0]

    async def ask(self, prompt: str, secret: bool = True) -> str | None:
        target = prompt.endswith(self._for) and not prompt.startswith("(")
        # Like plink -pwfile: also a hidden keyboard-interactive question (PAM, network devices).
        target = target or (prompt.startswith("(") and self._kbdint in prompt.split(" ", 1)[0] + " ")
        if self._password is not None and secret and target:
            password, self._password = self._password, None  # once: a wrong one must not loop
            return password
        return await self._inner.ask(prompt, secret)

    async def confirm_host_key(self, key: HostKey) -> bool:
        return await self._inner.confirm_host_key(key)

    async def notify(self, text: str) -> None:
        await self._inner.notify(text)


def _read_text(path: str, what: str) -> str:
    """A small text file given on the command line; UTF-8 with or without a BOM (Notepad, PowerShell 7)."""
    try:
        return user_path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(f"{what} {path} is not UTF-8 text (PowerShell 5 writes UTF-16: use "
                         "Out-File -Encoding utf8)") from None
    except OSError as exc:
        raise ValueError(f"cannot read {what} {path}: {exc.strerror}") from None


def _connect_profile(args, store: ProfileStore) -> Profile:
    """The profile for `connect`, with the ssh-like flags applied over it."""
    profile = resolve_profile(args.target, store)
    apply_settings(profile, args.set or [])
    command = " ".join(args.command)
    if args.command_file:
        command = _read_text(args.command_file, "command file")
    if args.nc and (command or args.no_shell or args.subsystem or _forwards(args)):
        raise ValueError("--nc replaces the shell: it cannot be combined with a command, -N, -s or -L/-R/-D")
    ssh_only = [name for name, used in (("a command", command), ("-N", args.no_shell), ("-s", args.subsystem),
                                         ("--nc", args.nc), ("-L/-R/-D", _forwards(args)),
                                         ("-4/-6", args.family)) if used]
    if profile.kind != "ssh":
        if ssh_only:
            raise ValueError(f"{', '.join(ssh_only)}: only for SSH connections")
        return profile
    s = profile.ssh
    if command or args.subsystem or args.no_shell:  # what to run on the server: the flags replace the profile's
        s.remote_command, s.subsystem, s.no_shell = command, args.subsystem or "", args.no_shell
    if args.family:
        s.address_family = args.family
    s.forwards += _forwards(args)
    if args.tty is not None:
        s.pty = args.tty
    elif command or args.subsystem or not sys.stdin.isatty():
        s.pty = False  # like plink and ssh: a command, a subsystem or piped input gets no terminal unless -t
    profile.validate()
    return profile


def _setup_logging(args) -> None:
    """-v shows connection steps on stderr, -vv protocol details; --ssh-log writes them to a file."""
    import logging

    import asyncssh

    if args.verbose:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("termdeck: %(name)s: %(message)s"))
        handler.addFilter(_hide_env_values)
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.DEBUG if args.verbose > 1 else logging.INFO)
    if args.ssh_log:
        handler = logging.StreamHandler(open_private(user_path(args.ssh_log), "a", own_dir=False))
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s: %(message)s"))
        handler.setLevel(logging.DEBUG)
        handler.addFilter(_hide_env_values)
        logging.getLogger("asyncssh").addHandler(handler)
        logging.getLogger("asyncssh").setLevel(logging.DEBUG)
    if args.verbose > 1 or args.ssh_log:
        asyncssh.set_debug_level(2)  # packets and algorithms; level 3 (raw data) could show typed secrets


def _hide_env_values(record) -> bool:
    """asyncssh logs environment variables with their values ("[chan=0]   Env: %s=%s"), which may be tokens.
    A handler filter: it must see records of every asyncssh child logger."""
    if isinstance(record.msg, str) and "Env: %s=%s" in record.msg and record.args:
        record.args = (*record.args[:-1], "(hidden)")
    return True


async def cmd_connect(args, store: ProfileStore) -> int:
    """Like ssh/plink: interactive in a terminal, scripted with pipes. Returns the remote exit status."""
    from .console import ESCAPE_HELP, interact, pipe, terminal_size

    profile = _connect_profile(args, store)
    if args.verbose or args.ssh_log:
        _setup_logging(args)
    prompter = BatchPrompter() if args.batch else ConsolePrompter()
    if args.password_file:
        prompter = PasswordFilePrompter(prompter, args.password_file, profile.ssh.host)
    # A terminal session needs a remote terminal; a command without one runs like `ssh host cmd`:
    # the local terminal stays as it is (its own echo and line editing) and data passes unchanged.
    remote_tty = profile.kind != "ssh" or profile.ssh.pty
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not args.nc and remote_tty
    if interactive:
        print(f"[termdeck] connecting to {profile.summary()}...", file=sys.stderr, flush=True)
    size = terminal_size()
    if args.nc:
        from .ssh import SSHConnection

        host, _, port = args.nc.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError(f"--nc expects HOST:PORT, got {args.nc!r}")
        conn = await SSHConnection.connect(profile, prompter)
        try:
            session = await conn.open_stream(host.strip("[]"), int(port))
        except BaseException:
            conn.close()
            raise
    else:
        # Scripted data is passed as it is: no Enter/Backspace mapping, echo or answerback.
        session = await open_session(profile, prompter, size.columns, size.lines, raw=not interactive)
    if not interactive:  # termdeck's own messages must not mix with the data on stdout
        session.on_notice(lambda data: (sys.stderr.buffer.write(data.strip() + b"\n"), sys.stderr.flush()))
    try:
        if interactive:
            await interact(session, f"[termdeck] connected to {profile.summary()}. {ESCAPE_HELP}")
        else:
            await pipe(session)
    finally:
        await session.close()
    reason = session.end_reason
    status = getattr(session, "exit_status", None)
    killed = getattr(getattr(session, "inner", session), "exit_signal", None)
    # A script learns a failing command from the exit status; only other endings are worth a line.
    if interactive or (reason and (status is None or killed)):
        print(f"\r\n[termdeck] Session ended{': ' + reason if reason else ''}", file=sys.stderr)
    return status if status is not None else (1 if reason else 0)


async def cmd_tunnel(args, store: ProfileStore) -> int:
    conn = await _ssh_connect(args.name, store)
    try:
        if not conn.profile.ssh.forwards:
            raise ValueError(f"{args.name!r} has no port forwards")
        for message in await conn.start_forwards():
            print(message, file=sys.stderr)
        if not conn.listeners:
            return 1
        for fwd in conn.profile.ssh.forwards:
            print(f"forwarding {fwd}", file=sys.stderr)
        print("tunnel open, Ctrl+C to stop", file=sys.stderr)
        await conn.wait_closed()
        print("connection closed by the server", file=sys.stderr)
        return 1
    finally:
        conn.close()


async def cmd_sftp(args, store: ProfileStore) -> int:
    conn = await _ssh_connect(args.name, store)
    try:
        async with conn.sftp() as sftp:
            if args.action == "ls":
                for name in sorted(await sftp.listdir(args.path or ".")):
                    if name not in (".", ".."):
                        print(name)
            elif args.action == "get":
                local = args.dest or PurePosixPath(args.source).name
                await sftp.get(args.source, local, recurse=args.recursive, preserve=True)
            elif args.action == "put":
                await sftp.put(args.source, args.dest or ".", recurse=args.recursive, preserve=True)
    finally:
        conn.close()
    return 0


def cmd_web(args, store: ProfileStore) -> int:
    from .web.server import serve

    serve(args.port, open_browser=not args.no_browser)
    return 0


def cmd_gui(args, store: ProfileStore) -> int:
    from .gui import run

    run()
    return 0


def cmd_tui(args, store: ProfileStore) -> int:
    from .tui.app import run

    # Resolved before the UI starts, so a typo is a plain error with a non-zero exit.
    run(resolve_profile(args.name, store) if args.name else None)
    return 0


def _add_forward_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-L", dest="local", action="append", metavar="[BIND:]PORT:HOST:HOSTPORT", help="local forward")
    parser.add_argument("-R", dest="remote", action="append", metavar="[BIND:]PORT:HOST:HOSTPORT", help="remote forward")
    parser.add_argument("-D", dest="dynamic", action="append", metavar="[BIND:]PORT", help="SOCKS proxy")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="termdeck", description="SSH and serial terminal with saved profiles.")
    parser.add_argument("--version", action="version", version=f"termdeck {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    parser.set_defaults(func=cmd_tui, name=None)

    p = sub.add_parser("tui", help="interactive full-screen terminal UI (the default)")
    p.add_argument("name", nargs="?", help="profile name, user@host[:port], or DEVICE[@baud] to open at start")
    p.set_defaults(func=cmd_tui)
    p = sub.add_parser("connect", help="session in this terminal, like ssh or plink (Ctrl+] q quits)",
                       description="Interactive in a terminal; with pipes, stdin and stdout pass through and the "
                                   "remote exit status is returned.")
    p.add_argument("target", help="profile name, user@host[:port], or DEVICE[@baud]")
    p.add_argument("command", nargs="*", help="remote command (SSH); options go before it, like plink")
    p.add_argument("--set", action="append", metavar="SECTION.OPTION=VALUE", help="override an option, repeatable")
    tty = p.add_mutually_exclusive_group()
    tty.add_argument("-t", dest="tty", action="store_const", const=True, help="allocate a terminal even with pipes")
    tty.add_argument("-T", dest="tty", action="store_const", const=False, help="never allocate a terminal")
    p.add_argument("-N", dest="no_shell", action="store_true", help="no shell, port forwards only")
    p.add_argument("-s", "--subsystem", metavar="NAME", help="start an SSH subsystem (e.g. sftp, netconf)")
    p.add_argument("-m", dest="command_file", metavar="FILE", help="read the remote command from FILE")
    p.add_argument("--nc", metavar="HOST:PORT", help="connect stdin/stdout to HOST:PORT through the server")
    family = p.add_mutually_exclusive_group()
    family.add_argument("-4", dest="family", action="store_const", const="ipv4", help="IPv4 only")
    family.add_argument("-6", dest="family", action="store_const", const="ipv6", help="IPv6 only")
    _add_forward_options(p)
    p.add_argument("--batch", action="store_true", help="never ask anything (passwords, host keys): fail instead")
    p.add_argument("--password-file", metavar="FILE", help="answer the password prompt from the first line of FILE")
    p.add_argument("-v", "--verbose", action="count", default=0, help="show connection steps (-vv: protocol)")
    p.add_argument("--ssh-log", metavar="FILE",
                   help="append SSH protocol details to FILE (commands, not typed data or environment values)")
    p.set_defaults(func=cmd_connect)
    parser.connect_parser = p
    p = sub.add_parser("gui", help="open the desktop window")
    p.set_defaults(func=cmd_gui)
    p = sub.add_parser("web", help="serve the UI in your browser")
    p.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: random)")
    p.add_argument("--no-browser", action="store_true", help="only print the URL")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("list", help="list profiles")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("show", help="print a profile as JSON")
    p.add_argument("name")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("add", help="create a profile")
    kinds = p.add_subparsers(dest="kind", required=True, metavar="TYPE")
    ssh = kinds.add_parser("ssh", help="SSH profile")
    ssh.add_argument("name")
    ssh.add_argument("target", metavar="HOST")
    ssh.add_argument("-p", "--port", type=int, default=SSHSettings().port)
    ssh.add_argument("-u", "--user")
    ssh.add_argument("-i", "--identity", metavar="KEY_FILE")
    _add_forward_options(ssh)
    serial = kinds.add_parser("serial", help="serial port profile")
    serial.add_argument("name")
    serial.add_argument("target", metavar="DEVICE")
    # Defaults and choices come from the option declarations (profiles.py).
    defaults, choices = SerialSettings(), {o["key"]: o["choices"] for o in describe_options()["serial"]}
    serial.add_argument("-b", "--baud", type=int, default=defaults.baudrate)
    serial.add_argument("--databits", type=int, default=defaults.bytesize, choices=[k for k, _ in choices["bytesize"]])
    serial.add_argument("--parity", default=defaults.parity, choices=[k for k, _ in choices["parity"]])
    serial.add_argument("--stopbits", type=float, default=defaults.stopbits, choices=[k for k, _ in choices["stopbits"]])
    serial.add_argument("--flow", default=defaults.flow, choices=[k for k, _ in choices["flow"]])
    for k in (ssh, serial):
        k.add_argument("-g", "--group", default="", help="sidebar folder")
        k.add_argument("--set", action="append", metavar="SECTION.OPTION=VALUE", help="any other option, repeatable")
        k.set_defaults(func=cmd_add)

    p = sub.add_parser("set", help="change options, e.g. termdeck set web ssh.keepalive=30 terminal.theme=dracula")
    p.add_argument("name")
    p.add_argument("settings", nargs="*", metavar="SECTION.OPTION=VALUE")
    _add_forward_options(p)
    p.add_argument("--no-forwards", action="store_true", help="remove all port forwards")
    p.set_defaults(func=cmd_set)
    p = sub.add_parser("rename", help="rename a profile")
    p.add_argument("name")
    p.add_argument("new_name")
    p.set_defaults(func=cmd_rename)
    p = sub.add_parser("rm", help="delete a profile")
    p.add_argument("name")
    p.set_defaults(func=cmd_rm)
    p = sub.add_parser("ports", help="list serial ports")
    p.set_defaults(func=cmd_ports)
    p = sub.add_parser("forget-host", help="forget the trusted key of a host (after a server was reinstalled)")
    p.add_argument("host", metavar="HOST[:PORT]")
    p.set_defaults(func=cmd_forget_host)
    p = sub.add_parser("options", help="list every option for --set and termdeck set")
    p.set_defaults(func=cmd_options)

    p = sub.add_parser("tunnel", help="open a profile's port forwards without a shell")
    p.add_argument("name")
    p.set_defaults(func=cmd_tunnel)
    p = sub.add_parser("sftp", help="file transfer over a saved SSH profile")
    p.add_argument("name")
    actions = p.add_subparsers(dest="action", required=True, metavar="ACTION")
    a = actions.add_parser("ls", help="list a remote directory")
    a.add_argument("path", nargs="?")
    for action, source, dest in (("get", "REMOTE", "LOCAL"), ("put", "LOCAL", "REMOTE")):
        a = actions.add_parser(action, help=f"copy {source.lower()} to {dest.lower()}")
        a.add_argument("source", metavar=source)
        a.add_argument("dest", nargs="?", metavar=dest)
        a.add_argument("-r", "--recursive", action="store_true")
    p.set_defaults(func=cmd_sftp)
    return parser


def _split_command(parser: argparse.ArgumentParser, argv: list[str]) -> tuple[list[str], list[str]]:
    """Split `connect` arguments like plink: options anywhere up to the first plain word
    after the target; that word and everything after it is the remote command."""
    takes_value = {flag: action.nargs != 0 for action in parser._actions for flag in action.option_strings}
    head, positionals, i = [], 0, 0
    while i < len(argv):
        word = argv[i]
        if word == "--":
            return head, argv[i + 1:]
        if word.startswith("-") and len(word) > 1:
            head.append(word)
            if takes_value.get(word.split("=")[0]) and "=" not in word and i + 1 < len(argv):
                head.append(argv[i + 1])
                i += 1
        elif positionals:
            return head, argv[i:]
        else:
            head.append(word)
            positionals += 1
        i += 1
    return head, []


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        # Redirected output would otherwise use the ANSI code page and fail on accents.
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["connect"]:
        head, command = _split_command(parser.connect_parser, argv[1:])
        args = parser.parse_args(["connect", *head])
        args.command = command
    else:
        args = parser.parse_args(argv)
    store = ProfileStore()
    tty = _save_tty()
    try:
        result = args.func(args, store)
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        return result
    except KeyboardInterrupt:
        _restore_tty(tty)  # Ctrl+C at a password prompt would leave echo off
        return 130
    except KeyError as exc:  # a profile or option that does not exist
        print(f"termdeck: {exc.args[0]}", file=sys.stderr)
        return 2
    except ValueError as exc:  # an invalid value given by the user
        print(f"termdeck: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"termdeck: {describe_error(exc)}", file=sys.stderr)
        if not is_expected(exc):  # a bug: give what is needed to report it
            traceback.print_exc()
            return 70
        return 1


def _save_tty():
    if os.name != "posix" or not sys.stdin.isatty():
        return None
    import termios

    return termios.tcgetattr(sys.stdin.fileno())


def _restore_tty(saved) -> None:
    if saved is not None:
        import termios

        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)


if __name__ == "__main__":
    sys.exit(main())
