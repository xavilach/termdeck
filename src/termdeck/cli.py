"""Command line: launch a UI, manage profiles, run tunnels and SFTP transfers."""

import argparse
import asyncio
import getpass
import json
import shutil
import sys
from pathlib import PurePosixPath

from . import __version__
from .profiles import (
    Profile, ProfileFileError, ProfileStore, SerialSettings, SSHSettings, check_option, describe_options, option_fields,
    parse_forward, resolve_target,
)
from .session import describe_error, open_session


class ConsolePrompter:
    """Asks on the controlling terminal. Prompts go to stderr, never stdout."""

    async def ask(self, prompt: str, secret: bool = True) -> str | None:
        def read():
            try:
                if secret:
                    return getpass.getpass(prompt, stream=sys.stderr)
                print(prompt, end="", file=sys.stderr, flush=True)
                return input()
            except EOFError:
                return None

        return await asyncio.to_thread(read)

    async def confirm_host_key(self, host: str, port: int, fingerprint: str, changed: bool) -> bool:
        if changed:
            print(f"WARNING: the host key for {host}:{port} has CHANGED.", file=sys.stderr)
        else:
            print(f"The host {host}:{port} is not known yet.", file=sys.stderr)
        print(f"Key fingerprint: {fingerprint}", file=sys.stderr)
        answer = await self.ask("Trust this key and continue? [y/N] ", secret=False)
        return (answer or "").strip().lower() in ("y", "yes")


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


async def cmd_connect(args, store: ProfileStore) -> int:
    from .console import ESCAPE_HELP, interact

    profile = resolve_profile(args.target, store)
    apply_settings(profile, args.set or [])
    size = shutil.get_terminal_size()
    session = await open_session(profile, ConsolePrompter(), size.columns, size.lines)
    try:
        await interact(session, f"[termdeck] connected to {profile.summary()}. {ESCAPE_HELP}")
    finally:
        await session.close()
    print("\r\n[termdeck] session closed", file=sys.stderr)
    return 0


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
    p = sub.add_parser("connect", help="session in this terminal, like ssh (Ctrl+] q quits)")
    p.add_argument("target", help="profile name, user@host[:port], or DEVICE[@baud]")
    p.add_argument("--set", action="append", metavar="SECTION.OPTION=VALUE", help="override an option, repeatable")
    p.set_defaults(func=cmd_connect)
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


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        # Redirected output would otherwise use the ANSI code page and fail on accents.
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    store = ProfileStore()
    try:
        result = args.func(args, store)
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        return result
    except KeyboardInterrupt:
        return 130
    except KeyError as exc:
        print(f"termdeck: {exc.args[0]}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"termdeck: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"termdeck: {describe_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
