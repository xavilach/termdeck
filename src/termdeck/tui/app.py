"""Full-screen terminal UI (Textual)."""

import asyncio
import copy
import re
import time
from dataclasses import asdict

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.content import Content
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Footer, Input, Label, ListItem, ListView, Select, Static, Switch, TabbedContent, TabPane, TextArea,
)

from ..errors import OptionError
from ..profiles import SECTION_TITLES, Profile, ProfileStore, describe_options, parse_forward, resolve_target
from ..serialport import available_ports
from ..session import HostKey
from .files import FilesScreen
from .terminal import TerminalView

FORWARD_FLAGS = {"L": "local", "R": "remote", "D": "dynamic"}


# ---------- dialogs ----------

class InputDialog(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, prompt: str, secret: bool = False, note: str = "", value: str = ""):
        super().__init__()
        self._title, self._prompt, self._secret, self._note, self._value = title, prompt, secret, note, value

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._title, classes="dialog-title", markup=False)
            if self._note:  # what the server said just before (MFA choices, "passwords do not match")
                yield Static(self._note, classes="note", markup=False)
            yield Label(self._prompt, markup=False)
            yield Input(self._value, password=self._secret, id="answer")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("OK", variant="primary", id="ok")

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def accept(self) -> None:
        self.dismiss(self.query_one("#answer", Input).value)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDialog(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, body: str, ok: str = "OK", danger: bool = False, cancel_default: bool | None = None,
                 arm: bool | None = None):
        """Dangerous questions focus Cancel. `arm` also keeps OK disabled for a moment, for questions
        that can open while the user is typing (by default: when Cancel is the default)."""
        super().__init__()
        self._title, self._body, self._ok, self._danger = title, body, ok, danger
        self._cancel_default = danger if cancel_default is None else cancel_default
        self._arm = self._cancel_default if arm is None else arm

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._title, classes="dialog-title", markup=False)
            yield Static(self._body, markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self._ok, variant="error" if self._danger else "primary", id="ok")

    def on_mount(self) -> None:
        self.query_one("#cancel" if self._cancel_default else "#ok").focus()
        if self._arm:
            # Like the web UI: a key typed into another tab (Tab, Enter) cannot accept at once.
            ok = self.query_one("#ok", Button)
            ok.disabled = True
            self.set_timer(1.5, lambda: setattr(ok, "disabled", False))

    @on(Button.Pressed, "#ok")
    def accept(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)


class TUIPrompter:
    def __init__(self, app: App, title: str):
        self._app = app
        self.title = title
        self.show = None  # set to the terminal's notice writer once it exists
        self._note = ""   # the last notice, repeated in the next question

    async def _show(self, screen):
        future = asyncio.get_running_loop().create_future()
        self._app.push_screen(screen, callback=lambda result: future.done() or future.set_result(result))
        return await future

    async def ask(self, prompt: str, secret: bool = True) -> str | None:
        note, self._note = self._note, ""
        return await self._show(InputDialog(self.title, prompt, secret, note))

    async def confirm_host_key(self, key: HostKey) -> bool:
        where = f"{key.host}:{key.port}"
        shown = f"{key.key_type} {key.fingerprint}"
        if key.changed:
            title, ok = "Host key CHANGED", "Trust new key"
            was = "".join(f"\nWas:  {old}" for old in key.old_fingerprints)
            body = (f"WARNING: the host key of {where} has changed. Someone may be intercepting the connection, "
                    f"or the server was reinstalled.\n\nNow:  {shown}{was}")
        else:
            title, ok = "Unknown host", "Trust and connect"
            body = f"{where} is not known yet. Check its fingerprint:\n\n{shown}"
        body += f"\n\nTrusted keys are saved in {key.known_hosts}"
        # Cancel is the default for both (the dialog can open while the user is typing);
        # only a changed key gets the red button.
        return await self._show(ConfirmDialog(title, body, ok, danger=key.changed, cancel_default=True))

    async def notify(self, text: str) -> None:
        self._note = text
        if self.show:
            self.show(text)


# ---------- profile editor ----------

def forwards_to_text(forwards) -> str:
    return ", ".join(str(f).lstrip("-") for f in forwards)


def text_to_forwards(text: str) -> list:
    """Parse "L 8080:db:5432, D 1080" (flags as in ssh)."""
    out = []
    for item in filter(None, (s.strip() for s in text.split(","))):
        m = re.match(r"^([LRD])\s*(\S+)$", item)
        if not m:
            raise ValueError(f"invalid forward {item!r}, expected e.g. L 8080:db:5432 or D 1080")
        out.append(parse_forward(FORWARD_FLAGS[m[1]], m[2]))
    return out


class FieldError(ValueError):
    """A value the editor could not read, with the widget to send the user to."""

    def __init__(self, widget_id: str, advanced: bool, message: str):
        super().__init__(message)
        self.widget_id, self.advanced = widget_id, advanced


class ProfileEditor(ModalScreen[Profile | None]):
    """Form built from describe_options(); shows only the selected type's options."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "save", "Save")]
    show_advanced = False

    def __init__(self, store: ProfileStore, profile: Profile | None = None, is_new: bool | None = None):
        super().__init__()
        self.store = store
        self.original = None if (is_new if is_new is not None else profile is None) else profile.name
        p = profile or Profile()
        self.values = {
            "name": p.name, "kind": p.kind, "group": p.group,
            "ssh": asdict(p.ssh), "serial": asdict(p.serial), "terminal": asdict(p.terminal),
        }
        self.values["ssh"]["forwards_text"] = forwards_to_text(p.ssh.forwards)
        self.options = describe_options()
        self._snapshot = None  # the values once the form shows them (Cancel asks only after a change)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog editor"):
            with Horizontal(classes="editor-head"):
                yield Label("New profile" if self.original is None else f"Edit {self.original}", classes="dialog-title",
                            markup=False)
                yield Label("Advanced ", classes="adv-label")
                yield Switch(value=self.show_advanced, id="advanced")
            with VerticalScroll(id="form"):
                yield from self._section(SECTION_TITLES["profile"], "profile", self.options["profile"], self.values)
                # Both connection types are built; CSS shows the selected one only.
                for kind in ("ssh", "serial"):
                    title = SECTION_TITLES[kind]
                    conn = [o for o in self.options[kind] if o["type"] != "forwards"]
                    with Vertical(classes=f"for-{kind} kind-section"):
                        yield from self._section(title, kind, [o for o in conn if not o["advanced"]], self.values[kind])
                        if kind == "ssh":
                            yield Label(SECTION_TITLES["forwards"], classes="section")
                            yield Input(self.values["ssh"]["forwards_text"],
                                        placeholder="L 8080:db:5432, R 9000:localhost:80, D 1080", id="ssh--forwards_text")
                            yield Label("Comma-separated, flags as in ssh -L/-R/-D", classes="help")
                        yield from self._section(f"{title} (advanced)", kind, [o for o in conn if o["advanced"]],
                                                 self.values[kind])
                yield from self._section(SECTION_TITLES["terminal"], "terminal", self.options["terminal"],
                                         self.values["terminal"])
            yield Label("", id="error")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="primary", id="save")

    def _section(self, title, section, options, values):
        if not options:
            return
        all_advanced = all(o["advanced"] for o in options)
        yield Label(title, classes="section advanced" if all_advanced else "section")
        group = ""
        for o in options:
            if o["group"] and o["group"] != group:  # related advanced options under a subheading
                group = o["group"]
                yield Label(group, classes="subsection advanced" if o["advanced"] else "subsection")
            classes = ["field"]
            if o["advanced"]:
                classes.append("advanced")
            if len(o["kinds"]) == 1:
                classes.append(f"for-{o['kinds'][0]}")
            wid = f"{section}--{o['key']}"
            value = values[o["key"]]
            with Vertical(classes=" ".join(classes), id=f"field-{wid}"):
                yield Label(o["label"])
                if o["choices"]:
                    yield Select([(label, key) for key, label in o["choices"]], value=value, allow_blank=False, id=wid)
                elif o["type"] == "bool":
                    yield Switch(value=bool(value), id=wid)
                elif o["widget"] == "multiline":
                    yield TextArea(value, id=wid)
                elif o["widget"] == "device":  # typed, or picked from the ports present now
                    with Horizontal(classes="device"):
                        yield Input(str(value), id=wid)
                        yield Button("Ports…", id=f"pick--{wid}")
                else:
                    kind = {"int": "integer", "float": "number"}.get(o["type"], "text")
                    yield Input(str(value), type=kind, password=o["widget"] == "secret", id=wid)
                if o["help"]:
                    yield Static(o["help"], classes="help", markup=False)  # wraps, unlike a Label
                if o["widget"] == "device" and values.get("usb_id"):
                    yield Static(f"Recorded adapter: {values['usb_id']}", classes="help", markup=False)

    def _apply_when(self) -> None:
        """Show a field only while the option it depends on has one of its values (proxy fields...)."""
        for section, options in self.options.items():
            values = self.values if section == "profile" else self.values[section]
            for o in options:
                if o["when"]:
                    value, wanted = values.get(o["when"]["key"]), o["when"]["values"]
                    shown = bool(value) if wanted is None else value in wanted
                    self.query_one(f"#field-{section}--{o['key']}").set_class(not shown, "unused")

    def _apply_kind(self) -> None:
        for kind in ("ssh", "serial"):
            self.set_class(self.values["kind"] == kind, f"kind-{kind}")

    def on_mount(self) -> None:
        self.set_class(self.show_advanced, "show-advanced")
        self._apply_kind()
        self._apply_when()
        self.query_one("#profile--name", Input).focus()
        # Taken once the fields have reported their values (numbers come back as text).
        self.call_after_refresh(lambda: setattr(self, "_snapshot", copy.deepcopy(self.values)))

    def _store(self, wid: str, value) -> None:
        section, _, key = wid.partition("--")
        target = self.values if section == "profile" else self.values[section]
        target[key] = value
        if self.is_mounted:
            self._apply_when()

    @on(Switch.Changed, "#advanced")
    def toggle_advanced(self, event: Switch.Changed) -> None:
        ProfileEditor.show_advanced = event.value
        self.set_class(event.value, "show-advanced")

    @on(Input.Changed)
    def input_changed(self, event: Input.Changed) -> None:
        # Numbers are kept as typed and parsed on save, so a cleared or half-typed
        # field is reported instead of saving whatever parsed last.
        self._store(event.input.id, event.value)

    def _numbers(self, section: str, values: dict) -> dict:
        values = dict(values)
        for o in self.options[section]:
            text = values.get(o["key"])
            if o["type"] in ("int", "float") and isinstance(text, str):
                try:
                    values[o["key"]] = int(text) if o["type"] == "int" else float(text)
                except ValueError:
                    raise FieldError(f"{section}--{o['key']}", o["advanced"], f"{o['label']}: enter a number") from None
        return values

    def _show_field(self, exc: "FieldError") -> None:
        if exc.advanced:
            self.query_one("#advanced", Switch).value = True
        for field in self.query(".invalid"):
            field.remove_class("invalid")
        widget = self.query_one(f"#{exc.widget_id}")
        if field := next((a for a in widget.ancestors if a.has_class("field")), None):
            field.add_class("invalid")
        widget.focus()
        widget.scroll_visible()

    def _field_error(self, exc: OptionError) -> "FieldError":
        """The editor's widget for an option the core found invalid."""
        if (exc.section, exc.key) == ("ssh", "forwards"):
            return FieldError("ssh--forwards_text", False, str(exc))
        option = next((o for o in self.options.get(exc.section, []) if o["key"] == exc.key), None)
        return FieldError(f"{exc.section}--{exc.key}", bool(option and option["advanced"]), str(exc))

    @on(Button.Pressed, ".device Button")
    def pick_port(self, event: Button.Pressed) -> None:
        target = self.query_one(f"#{event.button.id.removeprefix('pick--')}", Input)

        def chosen(device: str | None) -> None:
            if device:
                target.value = device
            target.focus()

        self.app.push_screen(PortPicker(), callback=chosen)

    @on(Select.Changed)
    def select_changed(self, event: Select.Changed) -> None:
        self._store(event.select.id, event.value)
        if event.select.id == "profile--kind":
            self._apply_kind()

    @on(Switch.Changed)
    def switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id != "advanced":
            self._store(event.switch.id, event.value)

    @on(TextArea.Changed)
    def area_changed(self, event: TextArea.Changed) -> None:
        self._store(event.text_area.id, event.text_area.text)

    @on(Button.Pressed, "#save")
    async def action_save(self) -> None:
        v = self.values
        try:
            data = {"name": v["name"].strip(), "kind": v["kind"], "group": v["group"].strip(),
                    v["kind"]: self._numbers(v["kind"], v[v["kind"]]),
                    "terminal": self._numbers("terminal", v["terminal"])}
            if v["kind"] == "ssh":
                data["ssh"]["forwards"] = [asdict(f) for f in text_to_forwards(data["ssh"].pop("forwards_text"))]
            profile = Profile.from_dict(data)
            # In a thread: saving a serial profile scans the USB ports, slow on Windows.
            if self.original is None:
                await asyncio.to_thread(self.store.add, profile)
            else:
                await asyncio.to_thread(self.store.update, self.original, profile)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            if isinstance(exc, KeyError):
                exc = ValueError(exc.args[0])
            if isinstance(exc, OptionError):
                exc = self._field_error(exc)
            self.query_one("#error", Label).update(Text(str(exc), style="red"))
            if isinstance(exc, FieldError):
                self._show_field(exc)
            return
        self.dismiss(profile)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        if self._snapshot is None or self.values == self._snapshot:
            self.dismiss(None)
            return
        self.app.push_screen(ConfirmDialog("Discard changes?", "The changes to this profile are not saved.",
                                           "Discard", danger=True, arm=False),
                             callback=lambda discard: discard and self.dismiss(None))


class PortPicker(ModalScreen[str | None]):
    """The serial ports present now, with their adapter; R scans again (an adapter plugged in meanwhile)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("r", "rescan", "Rescan")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Serial ports", classes="dialog-title")
            yield ListView(id="ports")
            yield Label("", id="ports-note")
            with Horizontal(classes="buttons"):
                yield Button("Rescan", id="rescan")
                yield Button("Cancel", id="cancel")

    async def on_mount(self) -> None:
        await self.action_rescan()

    @on(Button.Pressed, "#rescan")
    async def action_rescan(self) -> None:
        note = self.query_one("#ports-note", Label)
        note.update("Scanning…")
        ports = await asyncio.to_thread(available_ports)  # slow on Windows
        view = self.query_one("#ports", ListView)
        await view.clear()
        for port in ports:
            detail = " · ".join(filter(None, (port["description"], port["usb_id"])))
            item = ListItem(Label(Text.assemble((port["device"], "bold"), "  ", (detail, "dim"))))
            item.device = port["device"]
            await view.append(item)
        note.update(f"{len(ports)} port(s). Enter picks, R scans again." if ports else
                    "No serial port found. Plug the adapter in and press R.")
        view.index = 0 if ports else None  # Enter picks at once
        view.focus()

    @on(ListView.Selected, "#ports")
    def picked(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.device)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------- main app ----------

class ProfileItem(ListItem):
    def __init__(self, profile: Profile):
        kind = ("SSH", "bold cyan") if profile.kind == "ssh" else ("SER", "bold yellow")
        super().__init__(Label(Text.assemble(kind, " ", profile.name, "\n    ", (profile.summary(), "dim"))))
        self.profile = profile


# Shape and colour, so the state reads without colour too.
STATUS_ICON = {"connecting": ("◌", "yellow"), "connected": ("●", "green"), "failed": ("✕", "red"),
               "ended": ("○", "dim")}



def tab_title(status: str, name: str, title: str = "", bell: bool = False) -> Content:
    """Status dot, profile name, the remote title if any, and a mark when the bell rang unseen."""
    label = f"{name}: {title}" if title else name
    if len(label) > 30:  # the tab bar must hold several tabs
        label = label[:29] + "…"
    return Content.assemble(STATUS_ICON[status], " ", label, (" !", "bold yellow") if bell else "")


class TermdeckTUI(App, inherit_bindings=False):
    TITLE = "termdeck"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { layout: horizontal; }
    #sidebar { width: 34; max-width: 40%; border-right: solid $panel-lighten-2; }
    #sidebar.hidden { display: none; }
    #sidebar Input { margin: 0 1; }
    #sidebar .title { padding: 1 1 0 1; text-style: bold; color: $accent; }
    #profiles { height: 1fr; margin-top: 1; }
    .group { color: $text-muted; text-style: bold; padding: 0 1; }
    #main { width: 1fr; }
    TabbedContent, TabPane { height: 1fr; }
    TabPane { padding: 0; }
    #welcome { padding: 2 4; color: $text-muted; }
    #toolbar { height: 1; dock: top; background: $panel; padding: 0 1; }
    ModalScreen { align: center middle; }
    .dialog { width: 72; height: auto; max-height: 90%; background: $panel; border: round $accent; padding: 1 2; }
    .dialog-title { text-style: bold; width: 1fr; }
    .dialog Input { margin: 1 0; }
    .buttons { height: auto; align-horizontal: right; margin-top: 1; }
    .buttons Button { margin-left: 1; }
    .editor { width: 100%; max-width: 96; height: 100%; max-height: 50; padding: 0 1; }
    .device { height: auto; }
    .device Input { width: 1fr; }
    .field.invalid Input, .field.invalid Select, .field.invalid TextArea { border: tall $error; }
    #ports { height: auto; max-height: 16; }
    .files { width: 100%; max-width: 100; height: 90%; }
    #files-list { height: 1fr; }
    .editor-head { height: 3; }
    .adv-label { padding-top: 1; }
    #form { height: 1fr; }
    .section { text-style: bold; color: $accent; margin-top: 1; }
    .field { height: auto; margin-bottom: 1; }
    .field Input, .field Select { margin: 0; }
    .field TextArea { height: 5; }
    .help { color: $text-muted; width: 1fr; height: auto; }
    .subsection { text-style: bold; color: $text-muted; margin-top: 1; }
    ProfileEditor .unused { display: none; }
    .note { color: $warning; margin-top: 1; }
    #error { height: auto; }
    ProfileEditor .advanced { display: none; }
    ProfileEditor.show-advanced .advanced { display: block; }
    ProfileEditor.kind-ssh .for-serial, ProfileEditor.kind-serial .for-ssh { display: none; }
    .kind-section { height: auto; }
    """
    BINDINGS = [
        # Ctrl+] goes from the menus to the terminal and (handled by the terminal) back: the footer says which.
        Binding("ctrl+right_square_bracket", "focus_terminal", "Terminal", priority=False),
        Binding("ctrl+right_square_bracket", "menus", "Menus", priority=False),
        Binding("escape", "focus_terminal", "Terminal", show=False),
        Binding("n", "new_profile", "New"),
        Binding("e", "edit_profile", "Edit"),
        Binding("c", "duplicate_profile", "Duplicate"),
        Binding("delete", "delete_profile", "Delete"),
        Binding("w", "close_tab", "Close tab"),
        Binding("b", "send_break", "Break"),
        Binding("r", "reconnect", "Reconnect"),
        Binding("f", "find", "Find"),
        Binding("t", "files", "Files"),
        Binding("s", "toggle_sidebar", "Sidebar"),
        Binding("/", "focus_quick", "Quick connect"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+q", "quit_anywhere", "Quit", show=False),
    ]
    BREAK_AGAIN = 3.0  # seconds for the second B that sends the break

    def __init__(self, start_profile: Profile | None = None, store: ProfileStore | None = None):
        super().__init__()
        self.store = store or ProfileStore()
        self.start_profile = start_profile
        self._tab_ids = 0
        self._reported: set[str] = set()
        self._break_armed = 0.0

    def compose(self) -> ComposeResult:
        with Vertical(id="sidebar"):
            yield Label("▍termdeck", classes="title")
            yield Input(placeholder="user@host or COM3", id="quick", tooltip="Quick connect: user@host:port, "
                        "COM3@9600, /dev/ttyUSB0, or a profile name")
            yield ListView(id="profiles")
        with Vertical(id="main"):
            yield Static("", id="toolbar")
            with TabbedContent(id="tabs"):
                with TabPane("Welcome", id="welcome-pane"):
                    yield Static(
                        "Pick a profile and press Enter, or type a quick connect.\n\n"
                        "In a terminal every key goes to the remote side.\n"
                        "Press Ctrl+] to reach the menus, Esc to return, Ctrl+Q to quit.\n"
                        "In the menus: f finds in the scrollback, t opens files (SFTP), s hides the sidebar.\n"
                        "Shift+PgUp/PgDn or the mouse wheel scroll back; drag to select and copy.",
                        id="welcome",
                    )
            yield Footer()

    async def on_mount(self) -> None:
        count = await self.reload_profiles()
        if self.start_profile:
            self.open_tab(self.start_profile)
        elif count:
            self.main.query_one("#profiles").focus()
        else:
            self.main.query_one("#quick").focus()

    # ---- profiles ----

    def load_profiles(self) -> dict[str, Profile]:
        """Saved profiles; problems with the file are shown, never fatal."""
        try:
            profiles = self.store.load()
        except ValueError as exc:  # unreadable profiles.json: start empty, write nothing
            self._notify_once(str(exc), "Profiles not loaded", "error")
            return {}
        for line in self.store.warnings():
            self._notify_once(line, f"Skipped in {self.store.path.name}", "warning")
        return profiles

    def _notify_once(self, message: str, title: str, severity: str) -> None:
        """Problems with the profile file are reported once, not on every reload."""
        if message not in self._reported:
            self._reported.add(message)
            self.notify(message, title=title, severity=severity, timeout=20, markup=False)

    async def reload_profiles(self) -> int:
        profiles = sorted(self.load_profiles().values(), key=lambda p: (p.group == "", p.group.lower(), p.name.lower()))
        items, group = [], None
        for p in profiles:
            if p.group != group and (p.group or group):
                items.append(ListItem(Label(p.group or "Other", classes="group", markup=False), disabled=True))
            group = p.group
            items.append(ProfileItem(p))
        view = self.main.query_one("#profiles", ListView)
        await view.clear()
        await view.extend(items)
        return len(profiles)

    def selected_profile(self) -> Profile | None:
        item = self.main.query_one("#profiles", ListView).highlighted_child
        return item.profile if isinstance(item, ProfileItem) else None

    @on(ListView.Selected, "#profiles")
    def profile_chosen(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ProfileItem):
            self.open_tab(event.item.profile, saved=True)

    @on(Input.Submitted, "#quick")
    def quick_connect(self, event: Input.Submitted) -> None:
        if not event.value.strip():
            return
        profiles = self.load_profiles()
        try:
            profile = resolve_target(event.value, profiles)
        except KeyError as exc:
            self.notify(exc.args[0], severity="error", markup=False)
            return
        event.input.value = ""
        self.open_tab(profile, saved=event.value.strip() in profiles)

    async def _edit(self, profile: Profile | None, is_new: bool | None = None) -> None:
        editor = ProfileEditor(self.store, profile, is_new)

        async def done(result):
            if result:
                if editor.original is not None and editor.original != result.name:
                    for view in self.main.query(TerminalView):  # open tabs reconnect under the new name
                        view.controller.rename(editor.original, result.name)
                        if view.profile.name == result.name:
                            view.prompter.title = result.name
                        self._relabel(view)
                await self.reload_profiles()
                self.notify(f"Saved {result.name}", markup=False)

        self.push_screen(editor, callback=done)

    async def action_new_profile(self) -> None:
        await self._edit(None)

    async def action_edit_profile(self) -> None:
        if profile := self.selected_profile():
            await self._edit(profile)

    async def action_duplicate_profile(self) -> None:
        if profile := self.selected_profile():
            copy = Profile.from_dict(profile.to_dict())
            copy.name = f"{profile.name} copy"
            await self._edit(copy, is_new=True)

    async def action_delete_profile(self) -> None:
        profile = self.selected_profile()
        if not profile:
            return

        async def done(confirmed):
            if confirmed:
                try:
                    await asyncio.to_thread(self.store.delete, profile.name)
                except (KeyError, ValueError, OSError) as exc:
                    self.notify(str(exc.args[0] if isinstance(exc, KeyError) else exc), severity="error", markup=False)
                await self.reload_profiles()

        self.push_screen(ConfirmDialog("Delete profile", f"Delete {profile.name}?", "Delete", danger=True), callback=done)

    # ---- tabs ----

    def open_tab(self, profile: Profile, saved: bool = False) -> None:
        """`saved`: a profile from the store, read again on each (re)connect."""
        self._tab_ids += 1
        pane_id = f"tab-{self._tab_ids}"
        prompter = TUIPrompter(self, profile.name)
        view = TerminalView(profile, prompter, self.store, saved, id=f"term-{self._tab_ids}")
        prompter.show = view.show_notice
        tabs = self.main.query_one("#tabs", TabbedContent)
        tabs.add_pane(TabPane(tab_title("connecting", profile.name), view, id=pane_id))
        tabs.active = pane_id
        self.call_after_refresh(self._start, view)

    def _start(self, view: TerminalView) -> None:
        view.focus()
        view.connect()

    @property
    def main(self):
        """The main screen: queries must not depend on which dialog is open (App.query searches the top one)."""
        return self.screen_stack[0]

    def active_view(self) -> TerminalView | None:
        tabs = self.main.query("#tabs") if self.screen_stack else None
        if not tabs:  # the app is closing
            return None
        try:
            pane = tabs.first(TabbedContent).active_pane
        except NoMatches:  # the active pane is being removed (close on exit)
            return None
        views = pane.query(TerminalView) if pane else []
        return views.first() if views else None

    @on(TerminalView.StatusChanged)
    def status_changed(self, event: TerminalView.StatusChanged) -> None:
        self._relabel(event.view)
        self.update_toolbar()
        if self.screen_stack:
            self.refresh_bindings()  # the footer shows the actions the session now allows

    @on(TerminalView.TitleChanged)
    def title_changed(self, event: TerminalView.TitleChanged) -> None:
        self._relabel(event.view)

    def _relabel(self, view: TerminalView) -> None:
        pane = view.parent
        if isinstance(pane, TabPane) and self.screen_stack:
            tabs = self.main.query_one("#tabs", TabbedContent)
            tabs.get_tab(pane.id).label = tab_title(view.status, view.profile.name, view.remote_title, view.bell)

    @on(TerminalView.CloseRequested)
    async def close_requested(self, event: TerminalView.CloseRequested) -> None:
        pane = event.view.parent
        if isinstance(pane, TabPane) and self.screen_stack:
            await self.main.query_one("#tabs", TabbedContent).remove_pane(pane.id)
            self.notify(f"{event.view.profile.name}: session ended, tab closed", markup=False)
            self.update_toolbar()

    @on(TabbedContent.TabActivated)
    def tab_activated(self) -> None:
        if not self.screen_stack:  # the app is closing
            return
        self.update_toolbar()
        self.refresh_bindings()
        if view := self.active_view():
            if view.bell:
                view.bell = False
                self._relabel(view)
            view.focus()

    def update_toolbar(self) -> None:
        if not (self.screen_stack and self.main.query("#toolbar")):  # the app is closing
            return
        view = self.active_view()
        bar = self.main.query_one("#toolbar", Static)
        if not view:
            bar.update(Text("No session", style="dim"))
            return
        snap = view.controller.snapshot()
        caps = snap.get("capabilities", {})
        keys = "  ".join(k for k, ok in (("b b: break", caps.get("send_break")), ("t: files", caps.get("sftp"))) if ok)
        # The most useful first: on a narrow screen the profile summary is what gets cut.
        line = Text.assemble(STATUS_ICON[snap["state"]], f" {snap['status']}  ",
                             (snap.get("forwards_summary", "") + "  ", "red" if snap.get("forwards_failed") else "dim")
                             if snap.get("forwards_summary") else "",
                             (f"{keys}  " if keys else "", "dim"), (view.profile.summary(), "dim"))
        line.truncate(max(bar.size.width - 2, 20), overflow="ellipsis")
        bar.update(line)

    @on(TerminalView.EscapePressed)
    def leave_terminal(self) -> None:
        sidebar = self.main.query_one("#sidebar")
        if sidebar.has_class("hidden"):  # the menus are the sidebar: show it rather than focus something unseen
            sidebar.remove_class("hidden")
        self._focus_profiles()

    def _focus_profiles(self) -> None:
        """Focus the profile list with a profile selected, so its keys act at once."""
        profiles = self.main.query_one("#profiles", ListView)
        if profiles.index is None:
            profiles.index = next((i for i, item in enumerate(profiles.children) if isinstance(item, ProfileItem)), None)
        profiles.focus()

    def action_focus_terminal(self) -> None:
        if view := self.active_view():
            view.focus()

    def action_focus_quick(self) -> None:
        self.main.query_one("#quick").focus()

    async def action_close_tab(self) -> None:
        view = self.active_view()
        if not view:
            return
        pane_id = self.main.query_one("#tabs", TabbedContent).active

        async def close(confirmed: bool = True) -> None:
            if confirmed:
                await view.disconnect()
                await self.main.query_one("#tabs", TabbedContent).remove_pane(pane_id)
                self.update_toolbar()

        # Like PuTTY's Warn on close (the profile's Ask before closing).
        if view.status == "connected" and view.profile.terminal.warn_on_close:
            self.push_screen(ConfirmDialog("Close session?", f"{view.profile.name} is still connected. Close it?",
                                           "Close", danger=True, arm=False), callback=close)
        else:
            await close()

    async def action_send_break(self) -> None:
        """B twice: a break can reset a device, so one stray key does not send it."""
        view = self.active_view()
        if not (view and view.controller.capabilities.send_break):
            return
        if time.monotonic() - self._break_armed > self.BREAK_AGAIN:
            self._break_armed = time.monotonic()
            self.notify("Press B again to send a break", timeout=self.BREAK_AGAIN)
            return
        self._break_armed = 0.0
        sent, text = await view.send_break()
        self.notify(text, severity="information" if sent else "error", markup=False)
        view.focus()

    def action_reconnect(self) -> None:
        view = self.active_view()
        if view and not view.live:
            view.focus()
            view.connect()

    def action_menus(self) -> None:
        self.leave_terminal()

    def action_toggle_sidebar(self) -> None:
        sidebar = self.main.query_one("#sidebar")
        sidebar.toggle_class("hidden")
        if sidebar.has_class("hidden"):
            if view := self.active_view():
                view.focus()
            else:
                self.set_focus(None)  # nothing in the hidden sidebar may keep the keys
        else:
            self._focus_profiles()

    def action_find(self) -> None:
        view = self.active_view()
        if not view:
            return

        def search(text: str | None) -> None:
            view.focus()
            if text and not view.find(text):
                self.notify(f"No match for {text}", severity="warning", markup=False)

        self.push_screen(InputDialog("Find", "Text to find in the scrollback (Enter again finds the one above):",
                                     value=view.last_find), callback=search)

    def action_files(self) -> None:
        view = self.active_view()
        if view and view.status == "connected" and view.controller.capabilities.sftp:
            self.push_screen(FilesScreen(view.session.sftp(), view.profile.name))

    async def action_quit_anywhere(self) -> None:
        await self.action_quit()

    async def action_quit(self) -> None:
        connected = [v for v in self.main.query(TerminalView) if v.status == "connected"]

        async def leave(confirmed: bool = True) -> None:
            if confirmed:
                for view in self.main.query(TerminalView):
                    await view.disconnect()
                self.exit()

        if connected:
            names = ", ".join(v.profile.name for v in connected)
            self.push_screen(ConfirmDialog("Quit termdeck?", f"Still connected: {names}. Close and quit?", "Quit",
                                           danger=True, arm=False), callback=leave)
        else:
            await leave()

    def check_action(self, action: str, parameters) -> bool | None:
        in_terminal = isinstance(self.focused, TerminalView)
        if action == "quit_anywhere":
            return not in_terminal  # there Ctrl+Q is XON for the device
        if action == "focus_terminal":
            return not in_terminal
        if action == "menus":
            return in_terminal
        # Single-letter shortcuts must not fire while typing in an input.
        if action in {"new_profile", "edit_profile", "duplicate_profile", "delete_profile", "close_tab",
                      "send_break", "reconnect", "focus_quick", "quit", "find", "files", "toggle_sidebar"}:
            if isinstance(self.focused, (Input, TextArea, TerminalView)):
                return False
        # Session actions show in the footer only when they apply to the tab shown (False hides them).
        session_actions = {"close_tab", "send_break", "reconnect", "find", "files"}
        if action in session_actions and self.screen_stack:
            view = self.active_view()
            if view is None:
                return False
            caps = view.controller.capabilities
            connected = view.status == "connected"
            return {"close_tab": True, "find": True, "reconnect": not view.live,
                    "send_break": connected and caps.send_break, "files": connected and caps.sftp}[action]
        return True


def run(start_profile: Profile | None = None) -> None:
    TermdeckTUI(start_profile).run()
