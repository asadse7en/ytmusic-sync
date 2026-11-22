"""Cross-platform terminal input and clipboard helpers."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser


_WINDOWS_KEYS = {
    "H": "up",
    "P": "down",
    "K": "left",
    "M": "right",
}
_ANSI_KEYS = {
    b"\x1b[A": "up",
    b"\x1b[B": "down",
    b"\x1b[D": "left",
    b"\x1b[C": "right",
}


def _normalize_char(char: str) -> str:
    if char in ("\r", "\n"):
        return "enter"
    if char in ("\x08", "\x7f"):
        return "backspace"
    if char == "\t":
        return "tab"
    if char == "\x1b":
        return "esc"
    if char == "\x03":
        return "ctrl-c"
    return char


def read_key(timeout: float | None = None) -> str | None:
    """Read one terminal key, returning its normalized name or typed character."""
    if os.name == "nt":
        import msvcrt

        if timeout is not None:
            import time

            end = time.monotonic() + timeout
            while not msvcrt.kbhit():
                if time.monotonic() >= end:
                    return None
                time.sleep(min(0.01, max(0, end - time.monotonic())))
        first = msvcrt.getwch()
        if first in ("\x00", "\xe0"):
            return _WINDOWS_KEYS.get(msvcrt.getwch(), "")
        return _normalize_char(first)

    import select
    import termios
    import tty

    stream = sys.stdin
    fd = stream.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        readable, _, _ = select.select([fd], [], [], timeout)
        if not readable:
            return None
        first = os.read(fd, 1)
        if not first:
            return None
        if first != b"\x1b":
            byte = first[0]
            length = 1 if byte < 0x80 else 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            char_bytes = first + (os.read(fd, length - 1) if length > 1 else b"")
            char = char_bytes.decode("utf-8", errors="replace")
            return _normalize_char(char)

        sequence = first
        while len(sequence) < 3:
            readable, _, _ = select.select([fd], [], [], 0.03)
            if not readable:
                break
            sequence += os.read(fd, 1)
        if sequence in _ANSI_KEYS:
            return _ANSI_KEYS[sequence]
        return "esc"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def drain_pending() -> str:
    """Return all characters immediately available from the terminal."""
    if os.name == "nt":
        import msvcrt

        chars = []
        while msvcrt.kbhit():
            char = msvcrt.getwch()
            if char in ("\x00", "\xe0") and msvcrt.kbhit():
                chars.append(char)
                chars.append(msvcrt.getwch())
            else:
                chars.append(char)
        return "".join(chars)

    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        chars = []
        while select.select([fd], [], [], 0)[0]:
            char = os.read(fd, 1)
            if not char:
                break
            chars.append(char)
        return b"".join(chars).decode("utf-8", errors="replace")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def copy_to_clipboard(text: str) -> bool:
    """Copy text with a system clipboard command when one is available."""
    if os.name == "nt":
        command = shutil.which("clip.exe") or shutil.which("clip")
        commands = [[command]] if command else []
    elif sys.platform == "darwin":
        command = shutil.which("pbcopy")
        commands = [[command]] if command else []
    else:
        commands = []
        for name, args in (("wl-copy", []), ("xclip", ["-selection", "clipboard"])):
            command = shutil.which(name)
            if command:
                commands.append([command, *args])

    for command in commands:
        try:
            subprocess.run(command, input=text, text=True, check=True)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def open_url(url: str) -> bool:
    """Open a URL in the user's default browser."""
    return webbrowser.open(url)
