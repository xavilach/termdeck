"""Terminal colour themes, shared by every UI (the web page gets them from /api/options).

A palette has the 16 ANSI colours plus foreground, background, cursor and
selection. A profile picks a theme and may override any colour.
"""

import re

from .errors import ProfileError

ANSI = ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
        "brightBlack", "brightRed", "brightGreen", "brightYellow", "brightBlue", "brightMagenta", "brightCyan",
        "brightWhite")
COLOURS = ("foreground", "background", "cursor", "selectionBackground", *ANSI)

_XTERM = dict(zip(ANSI, ("#000000", "#cd0000", "#00cd00", "#cdcd00", "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
                         "#7f7f7f", "#ff0000", "#00ff00", "#ffff00", "#5c5cff", "#ff00ff", "#00ffff", "#ffffff")))

THEMES = {
    "dark": {**_XTERM, "foreground": "#e4e7ee", "background": "#0f1218", "cursor": "#5b9dff",
             "selectionBackground": "#2c4a7a", "black": "#1b1f27", "blue": "#3b8eea", "brightBlack": "#6b7280",
             "brightBlue": "#6cb0ff"},
    "light": {**_XTERM, "foreground": "#1b1f27", "background": "#ffffff", "cursor": "#2f6fe0",
              "selectionBackground": "#c8dbff", "black": "#1b1f27", "white": "#8b93a5", "brightWhite": "#667085",
              "yellow": "#a06b00", "brightYellow": "#8a5d00", "green": "#00870a", "brightGreen": "#00a30c",
              "cyan": "#008b8b", "brightCyan": "#007a7a"},
    "solarized": {"foreground": "#839496", "background": "#002b36", "cursor": "#93a1a1",
                  "selectionBackground": "#073642", "black": "#073642", "red": "#dc322f", "green": "#859900",
                  "yellow": "#b58900", "blue": "#268bd2", "magenta": "#d33682", "cyan": "#2aa198",
                  "white": "#eee8d5", "brightBlack": "#002b36", "brightRed": "#cb4b16", "brightGreen": "#586e75",
                  "brightYellow": "#657b83", "brightBlue": "#839496", "brightMagenta": "#6c71c4",
                  "brightCyan": "#93a1a1", "brightWhite": "#fdf6e3"},
    "dracula": {"foreground": "#f8f8f2", "background": "#282a36", "cursor": "#f8f8f2",
                "selectionBackground": "#44475a", "black": "#21222c", "red": "#ff5555", "green": "#50fa7b",
                "yellow": "#f1fa8c", "blue": "#bd93f9", "magenta": "#ff79c6", "cyan": "#8be9fd",
                "white": "#f8f8f2", "brightBlack": "#6272a4", "brightRed": "#ff6e6e", "brightGreen": "#69ff94",
                "brightYellow": "#ffffa5", "brightBlue": "#d6acff", "brightMagenta": "#ff92df",
                "brightCyan": "#a4ffff", "brightWhite": "#ffffff"},
    "putty": {**_XTERM, "foreground": "#bbbbbb", "background": "#000000", "cursor": "#00ff00",
              "selectionBackground": "#555555", "black": "#000000", "red": "#bb0000", "green": "#00bb00",
              "yellow": "#bbbb00", "blue": "#0000bb", "magenta": "#bb00bb", "cyan": "#00bbbb", "white": "#bbbbbb",
              "brightBlack": "#555555", "brightRed": "#ff5555", "brightGreen": "#55ff55", "brightYellow": "#ffff55",
              "brightBlue": "#5555ff", "brightMagenta": "#ff55ff", "brightCyan": "#55ffff",
              "brightWhite": "#ffffff"},
}
THEME_NAMES = {"dark": "Dark", "light": "Light", "solarized": "Solarized", "dracula": "Dracula", "putty": "PuTTY"}


def parse_colours(text: str) -> dict[str, str]:
    """Colour overrides from "name=#rrggbb" lines (names: foreground, background, cursor, red, brightBlue...)."""
    lowered = {name.lower(): name for name in COLOURS}
    colours = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        name, sep, value = line.partition("=")
        key = lowered.get(name.strip().lower().replace("_", "").replace(" ", "").replace("-", ""))
        if not sep or key is None or not re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()):
            raise ProfileError(f"invalid colour {line!r}, expected NAME=#rrggbb with NAME one of: {', '.join(COLOURS)}")
        colours[key] = value.strip().lower()
    return colours


def palette(theme: str, colours: str = "") -> dict[str, str]:
    """The theme's colours with the profile's overrides."""
    return {**THEMES.get(theme, THEMES["dark"]), **parse_colours(colours)}
