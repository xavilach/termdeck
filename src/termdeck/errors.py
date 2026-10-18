"""Errors the user can act on. Anything else reaching a UI is a bug.

The UIs show a TermdeckError's message as it is; they report other exceptions
as internal errors. The classes also derive from the matching built-in
exception, so code catching ValueError or KeyError keeps working.
"""


class TermdeckError(Exception):
    """A failure with a message meant for the user."""


class ProfileError(TermdeckError, ValueError):
    """An option value or profile is invalid."""


class ProfileNotFound(TermdeckError, KeyError):
    """No saved profile has this name."""

    def __str__(self) -> str:  # KeyError would quote the message
        return str(self.args[0]) if self.args else ""


class ConnectError(TermdeckError):
    """Connecting failed for a reason the message explains (no username, cancelled...)."""


class SFTPFailure(TermdeckError):
    """A file transfer or remote file operation failed (the message names the path)."""
