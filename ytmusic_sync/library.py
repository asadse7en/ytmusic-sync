"""The song database for one music folder, plus .m3u8 playlist writing."""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config
from .tags import filename_candidates

YT_ID = re.compile(r"(?:v=|youtu\.be/)([\w-]{11})")
SAVE_INTERVAL = 5.0  # seconds between background database writes
PARTIAL_SUFFIXES = {".part", ".ytdl", ".temp"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

_open: dict[str, "Library"] = {}


def open_library(out: Path) -> "Library":
    """One Library object per folder per process."""
    key = os.path.normcase(str(Path(out).expanduser().resolve()))
    if key not in _open:
        _open[key] = Library(out)
    return _open[key]


class Library:
    def __init__(self, out: Path, on_status=None):
        self.out = Path(out).expanduser().resolve()
        self.path = config.library_db_path(self.out)
        self.lock = threading.RLock()
        self._reserved: dict[str, str] = {}
        self._taken: set[str] = set()
        self._dirty = False
        self._last_save = 0.0

        data = config.read_json(self.path, None)
        fresh = not isinstance(data, dict)
        data = {} if fresh else data
        self.songs: dict = _dict(data.get("songs"))
        self.aliases: dict = _dict(data.get("aliases"))
        self.playlists: dict = _dict(data.get("playlists"))
        self.playlist_names: dict[str, str] = _dict(data.get("playlist_names"))
        self.stats: dict = {"runs": 0, "downloads": 0, "bytes": 0, "last_run": "never", **_dict(data.get("stats"))}

        files = self.refresh_files()
        if fresh and files:
            if on_status:
                on_status("Reading existing files...")
            self.rebuild_from_tags(files)
            self.save(force=True)
        self._drop_missing(files)

    # ---------- files on disk ----------
    def refresh_files(self) -> dict[str, int]:
        """name -> size for every audio file in the folder (one directory scan)."""
        files = {}
        with os.scandir(self.out) as entries:
            for entry in entries:
                if entry.is_file() and Path(entry.name).suffix.lower() in config.AUDIO_EXT:
                    files[entry.name] = entry.stat().st_size
        self._taken = {Path(name).stem.lower() for name in files} | {n.lower() for n in self._reserved.values()}
        return files

    def _drop_missing(self, files: dict[str, int]) -> None:
        self.songs = {vid: s for vid, s in self.songs.items()
                      if isinstance(s, dict) and s.get("file") in files}
        self.aliases = {a: k for a, k in self.aliases.items() if k in self.songs}

    def rebuild_from_tags(self, files: dict[str, int]) -> None:
        """Recover video IDs from the source URL stored in each file's tags (new/lost database)."""
        import mutagen
        from concurrent.futures import ThreadPoolExecutor

        def read(name: str):
            try:
                audio = mutagen.File(self.out / name)
                if not audio or not audio.tags:
                    return None
                vid = next((m[1] for k, v in audio.tags.items() if _tag_has_url(k, v)
                            for m in [YT_ID.search(str(v))] if m), None)
                if not vid:
                    return None
                easy = mutagen.File(self.out / name, easy=True)
                tags = easy.tags if easy and easy.tags else {}
                return vid, {"file": name, "artist": (tags.get("artist") or [""])[0],
                             "title": (tags.get("title") or [Path(name).stem])[0],
                             "dur": int(audio.info.length)}
            except Exception:
                return None

        with ThreadPoolExecutor(8) as pool:
            for result in pool.map(read, sorted(files)):
                if result:
                    self.songs.setdefault(result[0], result[1])

    # ---------- naming ----------
    def reserve_name(self, vid: str, artist: str, title: str, style: str) -> str:
        """Pick a unique filename stem for a new song (thread-safe, O(1) per call)."""
        with self.lock:
            if vid in self._reserved:
                return self._reserved[vid]
            candidates = filename_candidates(artist, title, style)
            name = next((c for c in candidates if c.lower() not in self._taken), None)
            if name is None:
                n = 2
                while f"{candidates[0]} ({n})".lower() in self._taken:
                    n += 1
                name = f"{candidates[0]} ({n})"
            self._taken.add(name.lower())
            self._reserved[vid] = name
            return name

    # ---------- songs, aliases ----------
    def canonical(self, vid: str) -> str:
        seen = set()
        while vid in self.aliases and vid not in seen:
            seen.add(vid)
            vid = self.aliases[vid]
        return vid

    def add_song(self, vid: str, record: dict) -> None:
        with self.lock:
            self.songs[vid] = record
            self._dirty = True

    def total_size(self) -> int:
        total = 0
        for song in list(self.songs.values()):
            try:
                total += (self.out / song["file"]).stat().st_size
            except OSError:
                pass
        return total

    def recount(self) -> tuple[int, int]:
        """Reconcile the in-memory song records with files on disk and return fresh totals."""
        files = self.refresh_files()
        with self.lock:
            previous_songs = self.songs.copy()
            previous_aliases = self.aliases.copy()
            self._drop_missing(files)
            if self.songs != previous_songs or self.aliases != previous_aliases:
                self._dirty = True
            songs = len(self.songs)
            size = sum(files.get(song.get("file"), 0) for song in self.songs.values())
        return songs, size

    def remove_songs(self, vids) -> int:
        removed = 0
        with self.lock:
            for vid in vids:
                song = self.songs.pop(vid, None)
                if song:
                    (self.out / song["file"]).unlink(missing_ok=True)
                    removed += 1
            gone = set(vids)
            self.aliases = {a: k for a, k in self.aliases.items() if k not in gone and a not in gone}
            self.playlists = {n: [v for v in ids if v not in gone] for n, ids in self.playlists.items()}
            self._dirty = True
        return removed

    def cleanup_partials(self) -> None:
        """Delete leftovers (.part, thumbnails, half-converted files) of songs that did not finish."""
        with self.lock:
            known = {s["file"] for s in self.songs.values()}
            reserved = {n.casefold() for n in self._reserved.values()}
        for path in self.out.iterdir():
            if not path.is_file() or path.name in known:
                continue
            suffix = path.suffix.lower()
            stem = Path(path.name[:-len(suffix)] if suffix in PARTIAL_SUFFIXES else path.name).stem
            stem = stem[:-3] if stem.lower().endswith(".sq") else stem
            if stem.casefold() in reserved and (
                    suffix in PARTIAL_SUFFIXES | IMAGE_SUFFIXES | config.AUDIO_EXT | {".webm"}):
                path.unlink(missing_ok=True)

    # ---------- persistence ----------
    def save(self, force: bool = False) -> None:
        """Write the database. Without force, at most once every few seconds."""
        with self.lock:
            if not force and (not self._dirty or time.monotonic() - self._last_save < SAVE_INTERVAL):
                return
            import json

            config.atomic_write(self.path, json.dumps(
                {"songs": self.songs, "aliases": self.aliases, "playlists": self.playlists,
                 "playlist_names": self.playlist_names, "stats": self.stats},
                indent=1, ensure_ascii=False))
            self._dirty, self._last_save = False, time.monotonic()

    def record_run(self, downloads: int, nbytes: int) -> None:
        with self.lock:
            self.stats["runs"] += 1
            self.stats["downloads"] += downloads
            self.stats["bytes"] += nbytes
            self.stats["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            self._dirty = True

    # ---------- playlists ----------
    def write_playlist(self, name: str, ids: list[str]) -> None:
        lines = ["#EXTM3U"]
        for vid in ids:
            song = self.songs.get(self.canonical(vid))
            if song:
                lines += [f"#EXTINF:{song.get('dur', 0)},{song.get('artist', '')} - {song.get('title', '')}",
                          song["file"]]
        config.atomic_write(self.out / f"{name}.m3u8", "\n".join(lines) + "\n")


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _tag_has_url(key, value) -> bool:
    key, value = str(key).lower(), str(value)
    if any(part in key for part in ("cmt", "comm", "purl")):
        return True
    return "description" in key and bool(re.fullmatch(r"\W*https?://[^\s'\"]+\W*", value))


def time_ago(stamp: str) -> str:
    try:
        seconds = (datetime.now() - datetime.strptime(stamp, "%Y-%m-%d %H:%M")).total_seconds()
    except (TypeError, ValueError):
        return "never"
    if seconds < 3600:
        return "just now"
    return f"{int(seconds // 3600)}h ago" if seconds < 86400 else f"{int(seconds // 86400)}d ago"
