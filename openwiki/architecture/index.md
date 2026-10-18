# Files

- [Architecture overview](overview.md) - termdeck is one asyncio core (profiles, sessions, transports, a per-tab controller) with four interfaces on top — CLI/connect console, Textual TUI, local web UI and a pywebview desktop window. This page maps the layers, the dependency direction and the flow from a profile to bytes on screen.
