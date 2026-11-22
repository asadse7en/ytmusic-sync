"""Thread-safe progress state shared by download workers and the dashboard."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class Slot:
    title: str = ""
    artist: str = ""
    stage: str = "fetch"
    stage_index: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    speed: float = 0.0
    state: str = "idle"

    @property
    def downloaded(self) -> int:
        return self.bytes_done

    @property
    def total(self) -> int:
        return self.bytes_total


class Tracker:
    def __init__(self, total: int, jobs: int):
        self.total, self.jobs = total, jobs
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.start_time = time.time()
        self.done = self.failed = self.bytes = 0
        self.slots: dict[int, Slot] = {slot: Slot() for slot in range(jobs)}
        self.queue: list[str] = []
        self.speed_samples = deque(maxlen=15)
        self._smoothed_speed = 0.0
        self._last_speed_sample = self.started

    def start(self, slot_id: int, title: str) -> None:
        with self.lock:
            slot = self.slots.setdefault(slot_id, Slot())
            slot.title, slot.artist = title, ""
            slot.stage, slot.stage_index = "fetch", 0
            slot.bytes_done = slot.bytes_total = 0
            slot.speed, slot.state = 0.0, "active"
            try:
                self.queue.remove(title)
            except ValueError:
                pass

    def set_queue(self, titles: list[str]) -> None:
        with self.lock:
            self.queue = list(titles)

    def set_track(self, slot_id: int, title: str, artist: str) -> None:
        with self.lock:
            slot = self.slots.get(slot_id)
            if slot:
                slot.title, slot.artist = title, artist
                slot.stage, slot.stage_index = "tag", 3

    def artist(self, slot_id: int) -> str:
        with self.lock:
            slot = self.slots.get(slot_id)
            return slot.artist if slot else ""

    def set_stage(self, slot_id: int, stage: str) -> None:
        stages = {"fetch": 0, "retrying": 0, "downloading": 1, "converting": 2, "tag": 3}
        normalized = "fetch" if stage == "retrying" else stage
        with self.lock:
            slot = self.slots.get(slot_id)
            if slot:
                slot.stage = normalized
                slot.stage_index = stages.get(normalized, slot.stage_index)

    def update(self, slot_id: int, info: dict) -> None:
        """yt-dlp progress hook payload."""
        with self.lock:
            slot = self.slots.get(slot_id)
            if slot and info.get("status") == "downloading":
                slot.state = "active"
                slot.stage, slot.stage_index = "downloading", 1
                slot.bytes_done = info.get("downloaded_bytes") or 0
                slot.bytes_total = info.get("total_bytes") or info.get("total_bytes_estimate") or 0
                slot.speed = info.get("speed") or 0.0

    def finish(self, slot_id: int, ok: bool, size: int = 0) -> None:
        with self.lock:
            slot = self.slots.setdefault(slot_id, Slot())
            if ok:
                self.done += 1
                self.bytes += size
            else:
                self.failed += 1
            slot.title = slot.artist = ""
            slot.stage, slot.stage_index = "fetch", 0
            slot.bytes_done = slot.bytes_total = 0
            slot.speed, slot.state = 0.0, "idle"

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self.lock:
            slots = [Slot(**vars(s)) for _, s in sorted(self.slots.items())]
            done, failed, completed_bytes = self.done, self.failed, self.bytes
            active = sum(slot.state == "active" for slot in slots)
            speed = sum(slot.speed for slot in slots if slot.state == "active")
            elapsed = now - self.started
            sample_count = int(now - self._last_speed_sample)
            if sample_count:
                for _ in range(min(sample_count, 15)):
                    self._smoothed_speed = (
                        speed if not self.speed_samples else 0.35 * speed + 0.65 * self._smoothed_speed
                    )
                    self.speed_samples.append(self._smoothed_speed)
                self._last_speed_sample += sample_count
            samples = list(self.speed_samples)
            smoothed_speed = self._smoothed_speed
            upcoming = self.queue[:3]
            queue_remaining = max(0, len(self.queue) - len(upcoming))
        finished = done + failed
        eta = None
        if finished and elapsed >= 5 and finished < self.total:
            eta = elapsed / finished * (self.total - finished)
        bytes_total = completed_bytes + sum(slot.bytes_done for slot in slots if slot.state == "active")
        workers = [
            {
                "title": slot.title,
                "artist": slot.artist,
                "stage": slot.stage,
                "stage_index": slot.stage_index,
                "bytes_done": slot.bytes_done,
                "bytes_total": slot.bytes_total,
                "speed": slot.speed,
                "state": slot.state,
            }
            for slot in slots
        ]
        return {
            "done": done,
            "failed": failed,
            "active": active,
            "total": self.total,
            "bytes": bytes_total,
            "bytes_total": bytes_total,
            "speed": smoothed_speed,
            "speed_samples": samples,
            "elapsed": elapsed,
            "eta": eta,
            "start_time": self.start_time,
            "slots": slots,
            "workers": workers,
            "upcoming": upcoming,
            "queue_remaining": queue_remaining,
        }


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
