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
