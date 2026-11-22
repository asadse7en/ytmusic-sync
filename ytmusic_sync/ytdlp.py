"""Lazy access to yt-dlp and ffmpeg, plus the once-a-day update check."""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from functools import lru_cache
from pathlib import Path

from . import config

PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
CHECK_INTERVAL = 86400
# Errors that usually mean "your yt-dlp is too old" (kept narrow to avoid false alarms).
BLOCKED = re.compile(r"\b(403|429)\b|sign in|not a bot|nsig|unable to extract|player response", re.I)

log = logging.getLogger(__name__)


class QuietLogger:
    """Send yt-dlp's own output to the log file instead of the screen."""

    def debug(self, msg): log.debug(msg)
    def info(self, msg): log.debug(msg)
    def warning(self, msg): log.warning(msg)
    def error(self, msg): log.error(msg)


@lru_cache(maxsize=1)
def module():
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("yt-dlp is not installed. Run: pip install yt-dlp") from exc
    return yt_dlp


@lru_cache(maxsize=1)
def ffmpeg_path() -> str | None:
    """System ffmpeg if present, otherwise the copy bundled with imageio-ffmpeg."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return exe if exe and Path(exe).exists() else None
    except Exception as exc:
        log.debug("imageio-ffmpeg unavailable: %s", exc)
        return None


FFMPEG_HELP = ("ffmpeg not found and the bundled copy is unavailable.\n"
               "  Windows: winget install ffmpeg\n  macOS:   brew install ffmpeg\n  Linux:   sudo apt install ffmpeg")


# ---------- versions ----------
def installed_version() -> str:
    try:
        from importlib.metadata import version

        return version("yt-dlp")
    except Exception:
        return "0"


def _vtuple(value: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", value))


def _read_cache() -> dict:
    cache = config.read_json(config.CACHE_FILE, {})
    return cache if isinstance(cache, dict) else {}


def update_available() -> tuple[str, str] | None:
    """(installed, latest) if the *cached* check found a newer version. Never touches the network."""
    latest = _read_cache().get("latest")
    current = installed_version()
    if latest and _vtuple(latest) > _vtuple(current):
        return current, latest
    return None


def _refresh_cache() -> None:
    latest = ""
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=3) as response:
            latest = json.load(response)["info"]["version"]
    except Exception as exc:
        log.debug("yt-dlp update check failed: %s", exc)
    try:
        config.ensure_data_dir()
        config.atomic_write(config.CACHE_FILE, json.dumps({"t": time.time(), "latest": latest}))
    except OSError:
        pass


def check_for_updates_in_background() -> None:
    """Refresh the cache at most once a day, in a daemon thread that never delays startup or exit."""
    if time.time() - _read_cache().get("t", 0) >= CHECK_INTERVAL:
        threading.Thread(target=_refresh_cache, name="ytdlp-update-check", daemon=True).start()


def upgrade() -> bool:
    result = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"])
    if result.returncode == 0:
        _refresh_cache()
    return result.returncode == 0
