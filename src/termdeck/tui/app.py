"""Full-screen terminal UI (Textual)."""

import asyncio
import re
from dataclasses import asdict

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.content import Content
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Footer, Input, Label, ListItem, ListView, Select, Static, Switch, TabbedContent, TabPane, TextArea,
)

from ..profiles import Profile, ProfileStore, describe_options, parse_forward, resolve_target
from ..session import HostKey
from .terminal import TerminalView

FORWARD_FLAGS = {"L": "local", "R": "remote", "D": "dynamic"}


# ---------- dialogs ----------

class InputDialog(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, prompt: str, secret: bool = False, note: str = ""):
        super().__init__()
        self._title, self._prompt, self._secret, self._note = title, prompt, secret, note

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._title, classes="dialog-title", markup=False)
            if self._note:  # what the server said just before (MFA choices, "passwords do not match")
                yield Static(self._note, classes="note", markup=False)
            yield Label(self._prompt, markup=False)
            yield Input(password=self._secret, id="answer")
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

    def __init__(self, title: str, body: str, ok: str = "OK", danger: bool = False, cancel_default: bool | None = None):
        super().__init__()
        self._title, self._body, self._ok, self._danger = title, body, ok, danger
        self._cancel_default = danger if cancel_default is None else cancel_default

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._title, classes="dialog-title", markup=False)
            yield Static(self._body, markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self._ok, variant="error" if self._danger else "primary", id="ok")

    def on_mount(self) -> None:
        self.query_one("#cancel" if self._cancel_default else "#ok").focus()
        if self._cancel_default:
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
        self._title = title
        self.show = None  # set to the terminal's notice writer once it exists
        self._note = ""   # the last notice, repeated in the next question

    async def _show(self, screen):
        future = asyncio.get_running_loop().create_future()
        self._app.push_screen(screen, callback=lambda result: future.done() or future.set_result(result))
        return await future

    async def ask(self, prompt: str, secret: bool = True) -> str | None:
        note, self._note = self._note, ""
        return await self._show(InputDialog(self._title, prompt, secret, note))

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

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog editor"):
            with Horizontal(classes="editor-head"):
                yield Label("New profile" if self.original is None else f"Edit {self.original}", classes="dialog-title",
                            markup=False)
                yield Label("Advanced ", classes="adv-label")
                yield Switch(value=self.show_advanced, id="advanced")
            with VerticalScroll(id="form"):
                yield from self._section("General", "profile", self.options["profile"], self.values)
                # Both connection types are built; CSS shows the selected one only.
                for kind, title in (("ssh", "SSH connection"), ("serial", "Serial port")):
                    conn = [o for o in self.options[kind] if o["type"] != "forwards"]
                    with Vertical(classes=f"for-{kind} kind-section"):
                        yield from self._section(title, kind, [o for o in conn if not o["advanced"]], self.values[kind])
                        if kind == "ssh":
                            yield Label("Port forwards", classes="section")
                            yield Input(self.values["ssh"]["forwards_text"],
                                        placeholder="L 8080:db:5432, R 9000:localhost:80, D 1080", id="ssh--forwards_text")
                            yield Label("Comma-separated, flags as in ssh -L/-R/-D", classes="help")
                        yield from self._section(f"{title} (advanced)", kind, [o for o in conn if o["advanced"]],
                                                 self.values[kind])
                yield from self._section("Terminal", "terminal", self.options["terminal"], self.values["terminal"])
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
                else:
                    kind = {"int": "integer", "float": "number"}.get(o["type"], "text")
                    yield Input(str(value), type=kind, password=o["widget"] == "secret", id=wid)
                if o["help"]:
                    yield Static(o["help"], classes="help", markup=False)  # wraps, unlike a Label

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
        self.query_one(f"#{exc.widget_id}").focus()

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
            self.query_one("#error", Label).update(Text(str(exc), style="red"))
            if isinstance(exc, FieldError):
                self._show_field(exc)
            return
        self.dismiss(profile)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------- main app ----------

class ProfileItem(ListItem):
    def __init__(self, profile: Profile):
        kind = ("SSH", "bold cyan") if profile.kind == "ssh" else ("COM", "bold yellow")
        super().__init__(Label(Text.assemble(kind, " ", profile.name, "\n    ", (profile.summary(), "dim"))))
        self.profile = profile


STATUS_ICON = {"connecting": ("●", "yellow"), "connected": ("●", "green"), "closed": ("●", "red")}


def tab_title(status: str, name: str) -> Content:
    return Content.assemble(STATUS_ICON[status], " ", name)


class TermdeckTUI(App, inherit_bindings=False):
    TITLE = "termdeck"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { layout: horizontal; }
    #sidebar { width: 34; border-right: solid $panel-lighten-2; }
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
    .editor { width: 90; height: 90%; }
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
        Binding("ctrl+right_square_bracket", "focus_terminal", "Back to terminal", priority=False),
        Binding("escape", "focus_terminal", "Back to terminal", show=False),
        Binding("n", "new_profile", "New"),
        Binding("e", "edit_profile", "Edit"),
        Binding("c", "duplicate_profile", "Copy"),
        Binding("delete", "delete_profile", "Delete"),
        Binding("w", "close_tab", "Close tab"),
        Binding("b", "send_break", "Break"),
        Binding("r", "reconnect", "Reconnect"),
        Binding("/", "focus_quick", "Quick connect"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+q", "quit_anywhere", "Quit", show=False),
    ]

    def __init__(self, start_profile: Profile | None = None, store: ProfileStore | None = None):
        super().__init__()
        self.store = store or ProfileStore()
        self.start_profile = start_profile
        self._tab_ids = 0
        self._reported: set[str] = set()

    def compose(self) -> ComposeResult:
        with Vertical(id="sidebar"):
            yield Label("▍termdeck", classes="title")
            yield Input(placeholder="user@host:port or COM3@9600", id="quick")
            yield ListView(id="profiles")
        with Vertical(id="main"):
            yield Static("", id="toolbar")
            with TabbedContent(id="tabs"):
                with TabPane("Welcome", id="welcome-pane"):
                    yield Static(
                        "Pick a profile and press Enter, or type a quick connect.\n\n"
                        "In a terminal every key goes to the remote side.\n"
                        "Press Ctrl+] to reach the menus, Esc to return, Ctrl+Q to quit.\n"
                        "Shift+PgUp/PgDn or the mouse wheel scroll back.",
                        id="welcome",
                    )
            yield Footer()

    async def on_mount(self) -> None:
        count = await self.reload_profiles()
        if self.start_profile:
            self.open_tab(self.start_profile)
        elif count:
            self.query_one("#profiles").focus()
        else:
            self.query_one("#quick").focus()

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
        view = self.query_one("#profiles", ListView)
        await view.clear()
        await view.extend(items)
        return len(profiles)

    def selected_profile(self) -> Profile | None:
        item = self.query_one("#profiles", ListView).highlighted_child
        return item.profile if isinstance(item, ProfileItem) else None

    @on(ListView.Selected, "#profiles")
    def profile_chosen(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ProfileItem):
            self.open_tab(event.item.profile)

    @on(Input.Submitted, "#quick")
    def quick_connect(self, event: Input.Submitted) -> None:
        if not event.value.strip():
            return
        try:
            profile = resolve_target(event.value, self.load_profiles())
        except KeyError as exc:
            self.notify(exc.args[0], severity="error", markup=False)
            return
        event.input.value = ""
        self.open_tab(profile)

    async def _edit(self, profile: Profile | None, is_new: bool | None = None) -> None:
        async def done(result):
            if result:
                await self.reload_profiles()
                self.notify(f"Saved {result.name}", markup=False)

        self.push_screen(ProfileEditor(self.store, profile, is_new), callback=done)

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

    def open_tab(self, profile: Profile) -> None:
        self._tab_ids += 1
        pane_id = f"tab-{self._tab_ids}"
        prompter = TUIPrompter(self, profile.name)
        view = TerminalView(profile, prompter, id=f"term-{self._tab_ids}")
        prompter.show = view.show_notice
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.add_pane(TabPane(tab_title("connecting", profile.name), view, id=pane_id))
        tabs.active = pane_id
        self.call_after_refresh(self._start, view)

    def _start(self, view: TerminalView) -> None:
        view.focus()
        view.connect()

    def active_view(self) -> TerminalView | None:
        pane = self.query_one("#tabs", TabbedContent).active_pane
        views = pane.query(TerminalView) if pane else []
        return views.first() if views else None

    @on(TerminalView.StatusChanged)
    def status_changed(self, event: TerminalView.StatusChanged) -> None:
        pane = event.view.parent
        if isinstance(pane, TabPane):
            tabs = self.query_one("#tabs", TabbedContent)
            tabs.get_tab(pane.id).label = tab_title(event.status, event.view.profile.name)
        self.update_toolbar()

    @on(TabbedContent.TabActivated)
    def tab_activated(self) -> None:
        self.update_toolbar()
        if view := self.active_view():
            view.focus()

    def update_toolbar(self) -> None:
        view = self.active_view()
        bar = self.query_one("#toolbar", Static)
        if not view:
            bar.update("[dim]No session[/]")
            return
        extra = "  b: send break" if view.profile.kind == "serial" else ""
        bar.update(Text.assemble(STATUS_ICON[view.status], f" {view.status}  ",
                                 (f"{view.profile.summary()}{extra}  Ctrl+] menus", "dim")))

    @on(TerminalView.EscapePressed)
    def leave_terminal(self) -> None:
        self.query_one("#profiles").focus()

    def action_focus_terminal(self) -> None:
        if view := self.active_view():
            view.focus()

    def action_focus_quick(self) -> None:
        self.query_one("#quick").focus()

    async def action_close_tab(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        view = self.active_view()
        if view:
            await view.disconnect()
            await tabs.remove_pane(tabs.active)
            self.update_toolbar()

    async def action_send_break(self) -> None:
        view = self.active_view()
        if view and view.profile.kind == "serial":
            await view.send_break()
            self.notify("Break sent")
            view.focus()

    def action_reconnect(self) -> None:
        view = self.active_view()
        if view and view.status == "closed":
            view.focus()
            view.connect()

    async def action_quit_anywhere(self) -> None:
        await self.action_quit()

    async def action_quit(self) -> None:
        for view in self.query(TerminalView):
            await view.disconnect()
        self.exit()

    def check_action(self, action: str, parameters) -> bool | None:
        if action == "quit_anywhere":
            return not isinstance(self.focused, TerminalView)  # there Ctrl+Q is XON for the device
        # Single-letter shortcuts must not fire while typing in an input.
        if action in {"new_profile", "edit_profile", "duplicate_profile", "delete_profile", "close_tab",
                      "send_break", "reconnect", "focus_quick", "quit"}:
            return not isinstance(self.focused, (Input, TextArea, TerminalView))
        return True


def run(start_profile: Profile | None = None) -> None:
    TermdeckTUI(start_profile).run()
