"""One tab's connection, the same in every UI: its state, what it can do, how it ended,
and reconnecting with the profile as it is saved now."""

import asyncio
from dataclasses import asdict, dataclass
from enum import StrEnum

from .errors import ProfileNotFound
from .profiles import Profile, ProfileStore
from .session import ForwardStatus, Prompter, Session, describe_error, end_message, open_session, send_break


class State(StrEnum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"   # never connected: refused, cancelled, bad key...
    ENDED = "ended"     # was connected; `clean` says whether the remote program ended normally


STATUS_TEXT = {State.CONNECTING: "Connecting…", State.CONNECTED: "Connected", State.FAILED: "Failed",
               State.ENDED: "Ended"}


@dataclass(frozen=True)
class Capabilities:
    """What the UIs offer for the session (buttons, keys), from the session itself."""

    send_break: bool = False
    sftp: bool = False
    typing: bool = True     # False: a forwards-only connection, nothing to type into

    def to_dict(self) -> dict:
        return asdict(self)


class SessionController:
    """Connects a profile, follows the session to its end and reconnects.

    `saved`: the profile is the store's profile of that name; each connect reads it again,
    so a reconnect uses the options as they were last saved (or a renamed profile's new name).
    `on_change` is called after every state change.
    """

    def __init__(self, profile: Profile, prompter: Prompter, store: ProfileStore | None = None,
                 saved: bool = False, on_change=None):
        self.profile = profile
        self.prompter = prompter
        self.store = store
        self.saved = saved and store is not None
        self.on_change = on_change
        self.state = State.CONNECTING
        self.session: Session | None = None
        self.message = ""    # why it failed or ended, for the UI
        self.clean = False   # ended because the remote program ended normally
        self.closed_locally = False  # the user closed it: never "closes the tab" on its own
        self.capabilities = Capabilities()
        self._epoch = 0  # bumped by close(): a connect that finishes afterwards is dropped

    def rename(self, old: str, new: str) -> None:
        """The saved profile was renamed: reconnect under its new name."""
        if self.saved and self.profile.name == old:
            self.profile = Profile.from_dict({**self.profile.to_dict(), "name": new}, validate=False)

    def _set(self, state: State, message: str = "") -> None:
        self.state, self.message = state, message
        if self.on_change:
            self.on_change(self)

    async def connect(self, cols: int = 80, rows: int = 24) -> bool:
        """Open the session (closing a previous one); False if that failed (`message` says why)."""
        if self.session:
            await self.close()
        epoch = self._epoch
        self.clean, self.closed_locally, self.capabilities = False, False, Capabilities()
        self._set(State.CONNECTING)
        try:
            if self.saved:  # in a thread: the store takes a file lock
                self.profile = await asyncio.to_thread(self.store.get, self.profile.name)
            session = await open_session(self.profile, self.prompter, cols, rows)
        except asyncio.CancelledError:
            if self._epoch == epoch:
                self._set(State.FAILED, "Cancelled")
            raise
        except ProfileNotFound as exc:
            self._set(State.FAILED, f"{exc}: it was deleted or renamed in another window")
            return False
        except Exception as exc:
            self._set(State.FAILED, describe_error(exc))
            return False
        if self._epoch != epoch:  # closed while connecting: the user no longer wants it
            await session.close()
            return False
        self.session = session
        self.capabilities = Capabilities(send_break=session.can_break, sftp=session.has_sftp,
                                         typing=session.interactive)
        self._set(State.CONNECTED)
        return True

    async def read(self) -> bytes:
        """The session's next output; b"" once it ended (the state is ENDED then)."""
        session = self.session
        data = await session.read() if session else b""
        if not data and self.state is State.CONNECTED:
            reason = session.end_reason if session else None
            self.clean = reason is None
            self._set(State.ENDED, end_message(reason))
        return data

    @property
    def closes_tab(self) -> bool:
        """Close tab on exit: always once ended, or only after a clean end."""
        when = self.profile.terminal.close_on_exit
        return (self.state is State.ENDED and not self.closed_locally
                and (when == "always" or (when == "clean" and self.clean)))

    async def send_break(self) -> tuple[bool, str]:
        """Send a serial break: (sent, what to tell the user)."""
        if not (self.session and self.capabilities.send_break):
            return False, "This connection cannot send a break"
        return await send_break(self.session)

    def forwards(self) -> list[ForwardStatus]:
        return self.session.forwards() if self.session else []

    def forwards_failed(self) -> int:
        return sum(not f.active for f in self.forwards())

    def forwards_summary(self) -> str:
        """"2 forwards" / "1 of 2 forwards failed", for a status bar; empty without forwards."""
        forwards = self.forwards()
        if not forwards:
            return ""
        failed = self.forwards_failed()
        noun = "forward" if len(forwards) == 1 else "forwards"
        return f"{failed} of {len(forwards)} {noun} failed" if failed else f"{len(forwards)} {noun}"

    def snapshot(self) -> dict:
        """The state as every UI shows it (JSON-ready): the web page gets it as is, the TUI reads it."""
        out = {"state": str(self.state), "status": STATUS_TEXT[self.state], "message": self.message,
               "live": self.state in (State.CONNECTING, State.CONNECTED)}
        if self.state is State.CONNECTED:
            out.update(title=self.session.title, capabilities=self.capabilities.to_dict(),
                       forwards=[{**asdict(f), "text": f.describe()} for f in self.forwards()],
                       forwards_summary=self.forwards_summary(), forwards_failed=self.forwards_failed())
        elif self.state is State.ENDED:
            out.update(clean=self.clean, close_tab=self.closes_tab)
        return out

    async def close(self) -> None:
        """Disconnect (the user closed the tab, or reconnects). The state changes first, so output
        that ends meanwhile is not taken for the remote side ending."""
        session, self.session = self.session, None
        self._epoch += 1
        if self.state in (State.CONNECTING, State.CONNECTED):
            self.closed_locally = True
            self._set(State.ENDED if session else State.FAILED, "Disconnected")
        if session:
            await session.close()
