"""Data folder, settings and small file helpers. Standard library only (fast to import)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path

DATA_DIR = Path(os.environ.get("YTMUSIC_SYNC_HOME") or Path.home() / ".ytmusic-sync")
CONFIG_FILE = DATA_DIR / "config.json"
LINKS_FILE = DATA_DIR / "playlists.txt"
FAILED_FILE = DATA_DIR / "failed.txt"
LOG_FILE = DATA_DIR / "sync.log"
CACHE_FILE = DATA_DIR / "cache.json"
LIBS_DIR = DATA_DIR / "libs"

FORMATS = ("mp3", "m4a", "opus", "flac", "wav", "aac")
QUALITIES = ("best", "320", "256", "192", "128")
NAME_STYLES = {"title": "Title", "artist-title": "Artist - Title", "title-artist": "Title - Artist"}
AUDIO_EXT = frozenset({".m4a", ".mp3", ".opus", ".flac", ".wav", ".aac", ".ogg", ".mp4"})
MAX_JOBS = 6


# ---------- file helpers ----------
def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same folder, then rename (never leaves a half-written file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        logging.getLogger(__name__).warning("Could not read %s: %s", path, exc)
        return default


def ensure_data_dir() -> Path:
    created = not DATA_DIR.exists()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if created and sys.platform == "win32":
        _hide_on_windows(DATA_DIR)
    return DATA_DIR


def _hide_on_windows(path: Path) -> None:
    try:
        import ctypes

        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs != -1:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs | 0x2)  # FILE_ATTRIBUTE_HIDDEN
    except Exception as exc:  # cosmetic only
        logging.getLogger(__name__).debug("Could not hide %s: %s", path, exc)


def library_db_path(out: Path) -> Path:
    """One database per music folder. Same hash as earlier versions, so existing data keeps working."""
    resolved = os.path.normcase(str(Path(out).expanduser().resolve()))
    return LIBS_DIR / f"{hashlib.sha1(resolved.encode('utf-8')).hexdigest()}.json"


# ---------- settings ----------
@dataclass
class Settings:
    out: str = ""
    format: str = "mp3"
    quality: str = "best"
    name_style: str = "title"
    cover: bool = True
    square: bool = True
    clean: bool = True
    playlist_files: bool = True
    prune: bool = False
    skip_duplicate_songs: bool = True
    jobs: int = 3
    retries: int = 5
    rate_limit: str = ""
    cookies: str = ""
    fallback: bool = True

    @classmethod
    def load(cls) -> "Settings":
        raw = read_json(CONFIG_FILE, {})
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in raw.items() if k in known} if isinstance(raw, dict) else {}
        return cls(**values).validated()

    def validated(self) -> "Settings":
        defaults = Settings()
        if self.format not in FORMATS:
            self.format = defaults.format
        if self.quality not in QUALITIES:
            self.quality = defaults.quality
        if self.name_style not in NAME_STYLES:
            self.name_style = defaults.name_style
        for name in ("cover", "square", "clean", "playlist_files", "prune", "skip_duplicate_songs", "fallback"):
            setattr(self, name, bool(getattr(self, name)))
        self.jobs = _clamp(self.jobs, 1, MAX_JOBS, defaults.jobs)
        self.retries = _clamp(self.retries, 0, 20, defaults.retries)
        self.rate_limit = str(self.rate_limit or "").strip()
        self.cookies = str(self.cookies or "").strip().lower()
        self.out = str(Path(self.out).expanduser()) if self.out else ""
        return self

    def save(self) -> None:
        ensure_data_dir()
        atomic_write(CONFIG_FILE, json.dumps(asdict(self), indent=2) + "\n")

    @property
    def out_path(self) -> Path | None:
        return Path(self.out).expanduser().resolve() if self.out else None


def _clamp(value, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default
