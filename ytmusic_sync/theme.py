"""Shared terminal theme and console setup."""
from __future__ import annotations

import os
import sys

from rich.console import Console

BRAND = "#f5a623"
ACCENT = "#ff9f1a"
DARK_ORANGE = "#8a5a12"
SEL_FILL = "#2a2417"
SEL_EDGE = "#5a4318"
TILE_BORDER = "#4a3a1a"
WORKER_BORDER = "#6b4a12"
BORDER = "#5a5a5a"
BOX_BORDER = "#3a3a3a"
DIVIDER = "#333333"
EDGE_OFF = "#2a2a2a"
BAR_OFF = "#2f2f2f"
TRACK = "#333333"
GREEN = "#3fb950"
RED = "#f85149"
CYAN = "#4dd0e1"
BLUE = "#3b9ae1"
LINK = "#3b9ae1"
LINK_DIM = "#5a7a99"
TEXT = "#e6e6e6"
DIM = "#8b8b8b"
DIMMER = "#5a5a5a"
PATH = "#6b6b6b"
PLAYLIST_COLORS = [
    "#ff9f1a", "#2dd4bf", "#3b82f6", "#a78bfa", "#f472b6",
    "#facc15", "#ef4444", "#4ade80", "#22d3ee", "#a3e635",
]
ARTIST_COLORS = [
    "#facc15", "#fb7185", "#4ade80", "#60a5fa", "#c084fc",
    "#fb923c", "#2dd4bf", "#f472b6", "#a3e635", "#22d3ee",
]

_console: Console | None = None
_terminal_initialized = False


def get_console() -> Console:
    """Return the shared Rich console."""
    global _console
    if _console is None:
        _console = Console(highlight=False, soft_wrap=False)
    return _console


def init_terminal() -> None:
    """Configure UTF-8 output and Windows ANSI support once."""
    global _terminal_initialized
    if _terminal_initialized:
        return
    _terminal_initialized = True
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    if os.name == "nt":
        os.system("")
