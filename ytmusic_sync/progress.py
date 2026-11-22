"""Thread-safe progress state shared by download workers and the dashboard."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class Slot:
    title: str = ""
    artist: str = ""
    stage: str = "downloading"   # downloading | converting | retrying
    downloaded: int = 0
    total: int = 0
    speed: float = 0.0


class Tracker:
    def __init__(self, total: int, jobs: int):
        self.total, self.jobs = total, jobs
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.done = self.failed = self.bytes = 0
        self.slots: dict[int, Slot] = {}

    def start(self, slot_id: int, title: str) -> None:
        with self.lock:
            self.slots[slot_id] = Slot(title=title)

    def set_track(self, slot_id: int, title: str, artist: str) -> None:
        with self.lock:
            slot = self.slots.get(slot_id)
            if slot:
                slot.title, slot.artist = title, artist

    def artist(self, slot_id: int) -> str:
        with self.lock:
            slot = self.slots.get(slot_id)
            return slot.artist if slot else ""

    def set_stage(self, slot_id: int, stage: str) -> None:
        with self.lock:
            slot = self.slots.get(slot_id)
            if slot:
                slot.stage = stage

    def update(self, slot_id: int, info: dict) -> None:
        """yt-dlp progress hook payload."""
        with self.lock:
            slot = self.slots.get(slot_id)
            if slot and info.get("status") == "downloading":
                slot.stage = "downloading"
                slot.downloaded = info.get("downloaded_bytes") or 0
                slot.total = info.get("total_bytes") or info.get("total_bytes_estimate") or 0
                slot.speed = info.get("speed") or 0.0

    def finish(self, slot_id: int, ok: bool, size: int = 0) -> None:
        with self.lock:
            self.slots.pop(slot_id, None)
            if ok:
                self.done += 1
                self.bytes += size
            else:
                self.failed += 1

    def snapshot(self) -> dict:
        with self.lock:
            slots = [Slot(**vars(s)) for _, s in sorted(self.slots.items())]
            done, failed, nbytes = self.done, self.failed, self.bytes
        elapsed = time.monotonic() - self.started
        finished = done + failed
        eta = None
        if finished and elapsed >= 5 and finished < self.total:
            eta = elapsed / finished * (self.total - finished)
        return {"done": done, "failed": failed, "active": len(slots), "total": self.total,
                "bytes": nbytes + sum(s.downloaded for s in slots),
                "speed": sum(s.speed for s in slots), "elapsed": elapsed, "eta": eta, "slots": slots}


@dataclass
class ScannedPlaylist:
    id: str
    url: str
    title: str | None = None
    state: str = "queued"
    songs: int = 0
    new: int = 0
    error: str = ""


class ScanProgress:
    """Thread-safe playlist scan state shared by scanner workers and the live view."""

    LATEST_LIMIT = 100

    def __init__(self, urls: list[str], playlist_ids: list[str], library_before: int = 0):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.playlists = [ScannedPlaylist(playlist_id, url) for url, playlist_id in zip(urls, playlist_ids)]
        self.new_songs = 0
        self.new_playlists = 0
        self.latest_new: list[str] = []
        self.library_before = library_before
        self.library_after = library_before

    def start(self, index: int) -> None:
        with self.lock:
            if self.playlists[index].state == "queued":
                self.playlists[index].state = "fetching"

    def finish(self, index: int, title: str, songs: int) -> None:
        with self.lock:
            playlist = self.playlists[index]
            playlist.title = title
            playlist.state = "finished"
            playlist.songs = songs

    def fail(self, index: int, error: str) -> None:
        with self.lock:
            playlist = self.playlists[index]
            playlist.state = "failed"
            playlist.error = error

    def update_plan(
        self,
        new_songs: int,
        playlist_new: dict[str, int],
        library_before: int,
        library_after: int,
        latest_new: list[str],
    ) -> None:
        with self.lock:
            self.new_songs = new_songs
            self.new_playlists = sum(count > 0 for count in playlist_new.values())
            self.library_before = library_before
            self.library_after = library_after
            self.latest_new = list(latest_new[:self.LATEST_LIMIT])
            for playlist in self.playlists:
                playlist.new = playlist_new.get(playlist.url, 0)

    def snapshot(self) -> dict:
        with self.lock:
            playlists = [
                {
                    "id": playlist.id,
                    "url": playlist.url,
                    "title": playlist.title,
                    "state": playlist.state,
                    "songs": playlist.songs,
                    "new": playlist.new,
                    "error": playlist.error,
                }
                for playlist in self.playlists
            ]
            finished = sum(playlist.state == "finished" for playlist in self.playlists)
            failed = sum(playlist.state == "failed" for playlist in self.playlists)
            in_progress = sum(playlist.state == "fetching" for playlist in self.playlists)
            values = {
                "playlists": playlists,
                "total": len(playlists),
                "finished": finished,
                "failed": failed,
                "in_progress": in_progress,
                "new_songs": self.new_songs,
                "new_playlists": self.new_playlists,
                "latest_new": list(self.latest_new),
                "library_before": self.library_before,
                "library_after": self.library_after,
            }
        values["elapsed"] = time.monotonic() - self.started
        return values
