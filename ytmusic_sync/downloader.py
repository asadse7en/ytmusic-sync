"""Parallel downloads with yt-dlp: options, post-processors, retries, cancellation."""
from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import ytdlp
from .config import Settings
from .library import Library
from .progress import Tracker
from .scanner import short_error
from .tags import clean_tags, first_text, safe_name

log = logging.getLogger(__name__)
TRANSIENT = re.compile(r"\b429\b|sign in|timed out|timeout|connection|temporar|HTTP Error 5\d\d", re.I)
UNAVAILABLE = re.compile(r"unavailable|private video|removed|not available|no longer|blocked|copyright", re.I)
ALTERNATE_EXCLUSIONS = (
    re.compile(r"\blive\b", re.I),
    re.compile(r"\bcover\b", re.I),
    re.compile(r"\bremix\b", re.I),
    re.compile(r"\breaction\b", re.I),
    re.compile(r"\bslowed\b", re.I),
    re.compile(r"\bsped[\s-]+up\b", re.I),
    re.compile(r"\bkaraoke\b", re.I),
)


class Cancelled(Exception):
    """Raised inside yt-dlp hooks to abort the current download."""


@dataclass
class Failure:
    title: str
    artist: str
    reason: str
    id: str = ""
    playlist_ids: tuple[str, ...] = ()


@dataclass
class TrackResult:
    id: str
    title: str
    playlist_ids: tuple[str, ...]
    status: str
    bytes: int = 0
    reason: str = ""


def watch_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


def _find_alternate(entry, settings) -> str | None:
    try:
        yt_dlp = ytdlp.module()
        options = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "logger": ytdlp.QuietLogger(),
        }
        if settings.cookies:
            options["cookiesfrombrowser"] = (settings.cookies,)

        with yt_dlp.YoutubeDL(options) as search:
            info = search.extract_info(f"ytsearch5:{entry['title']}", download=False) or {}

        original_title = str(entry.get("title") or "")
        original_id = str(entry.get("id") or "")
        duration = float(entry.get("dur") or 0)
        for result in info.get("entries") or []:
            if not isinstance(result, dict):
                continue
            result_id = str(result.get("id") or "")
            if not result_id or result_id == original_id:
                continue
            title = str(result.get("title") or "")
            if any(pattern.search(title) and not pattern.search(original_title)
                   for pattern in ALTERNATE_EXCLUSIONS):
                continue
            if duration > 0:
                try:
                    result_duration = float(result.get("duration"))
                except (TypeError, ValueError):
                    continue
                if abs(result_duration - duration) > 5:
                    continue
            return result_id
    except Exception:
        log.exception("Alternate search failed for %s", entry.get("title", ""))
    return None


def build_options(settings: Settings, out: Path, hooks: dict) -> dict:
    yt_dlp = ytdlp.module()
    codec = settings.format
    options = {
        "outtmpl": str(out / "%(fname)s.%(ext)s"), "quiet": True, "no_warnings": True, "noprogress": True,
        "logger": ytdlp.QuietLogger(), "windowsfilenames": True,
        "concurrent_fragment_downloads": 4, "http_chunk_size": 10 * 1024 * 1024,
        "retries": settings.retries, "fragment_retries": settings.retries,
        "format": "bestaudio[ext=m4a]/bestaudio/best" if codec in ("m4a", "aac") else "bestaudio/best",
        **hooks,
    }
    processors = [{"key": "FFmpegExtractAudio", "preferredcodec": codec,
                   "preferredquality": "0" if settings.quality == "best" else settings.quality}]
    cover = settings.cover and codec != "wav"
    if cover:
        options["writethumbnail"] = True
        processors.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
    processors.append({"key": "FFmpegMetadata", "add_metadata": True})
    if cover:
        processors.append({"key": "EmbedThumbnail"})
    options["postprocessors"] = processors
    if settings.rate_limit:
        options["ratelimit"] = yt_dlp.utils.parse_bytes(settings.rate_limit)
    if settings.cookies:
        options["cookiesfrombrowser"] = (settings.cookies,)
    if ffmpeg := ytdlp.ffmpeg_path():
        options["ffmpeg_location"] = ffmpeg
    return options


@lru_cache(maxsize=1)
def _postprocessor_classes():
    """Defined lazily because they subclass a yt-dlp class (importing yt-dlp is the slow part)."""
    ytdlp.module()
    from yt_dlp.postprocessor.common import PostProcessor

    class CleanTags(PostProcessor):
        """Fix artist/title and choose the final filename before the download starts."""

        def __init__(self, lib: Library, settings: Settings, tracker: Tracker, slot: int, stop):
            super().__init__()
            self.lib, self.settings, self.tracker, self.slot, self.stop = lib, settings, tracker, slot, stop

        def run(self, info):
            if self.stop.is_set():
                raise Cancelled()
            artist, title = clean_tags(info, self.settings.clean)
            info.update(artist=artist, track=title, title=title, album_artist=artist.partition(",")[0].strip())
            info["fname"] = self.lib.reserve_name(info["id"], artist, title, self.settings.name_style)
            self.tracker.set_track(self.slot, title, artist)
            return [], info

    class SquareCover(PostProcessor):
        """Crop the cover art to a square with ffmpeg."""

        def __init__(self, stop):
            super().__init__()
            self.stop = stop

        def run(self, info):
            exe = ytdlp.ffmpeg_path()
            for thumb in info.get("thumbnails") or []:
                if self.stop.is_set():
                    raise Cancelled()
                src = thumb.get("filepath")
                if not exe or not src or not os.path.exists(src):
                    continue
                tmp = src + ".sq.jpg"
                result = subprocess.run(
                    [exe, "-y", "-loglevel", "error", "-i", src, "-vf", "crop='min(iw,ih)':'min(iw,ih)'",
                     "-c:v", "mjpeg", "-q:v", "2", tmp], capture_output=True, timeout=60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode == 0 and os.path.exists(tmp):
                    os.replace(tmp, src)
                else:
                    log.warning("cover crop failed: %s", result.stderr.decode(errors="ignore")[-200:])
                    Path(tmp).unlink(missing_ok=True)
            return [], info

    return CleanTags, SquareCover


def run_downloads(todo: list[dict], lib: Library, settings: Settings, tracker: Tracker,
                  stop: threading.Event, on_result=None,
                  track_results: list[TrackResult] | None = None, on_stopping=None,
                  on_view_key=None, on_failure_update=None) -> list[Failure]:
    """Download `todo` with settings.jobs workers. Returns the failures.

    `stop` is checked inside every yt-dlp hook, so setting it aborts running downloads quickly.
    """
    work: queue.Queue = queue.Queue()
    for entry in todo:
        work.put(entry)
    tracker.set_queue([entry["title"] for entry in todo])
    failures: list[Failure] = []
    failures_lock = threading.Lock()
    track_results_lock = threading.Lock()
    jobs = max(1, min(settings.jobs, len(todo)))
    interactive_dashboard = on_result is None

    def worker(slot: int) -> None:
        yt_dlp = ytdlp.module()
        CleanTags, SquareCover = _postprocessor_classes()

        def check(_info):
            if stop.is_set():
                raise Cancelled()

        hooks = {"progress_hooks": [lambda d: (check(d), tracker.update(slot, d))],
                 "postprocessor_hooks": [lambda d: (check(d), tracker.set_stage(slot, "converting"))]}
        with yt_dlp.YoutubeDL(build_options(settings, lib.out, hooks)) as ydl:
            ydl.add_post_processor(CleanTags(lib, settings, tracker, slot, stop), when="pre_process")
            if settings.cover and settings.square and settings.format != "wav":
                ydl.add_post_processor(SquareCover(stop), when="before_dl")
            while not stop.is_set():
                try:
                    entry = work.get_nowait()
                except queue.Empty:
                    return
                _download_one(
                    ydl, entry, slot, lib, settings, tracker, stop, failures, failures_lock, on_result,
                    track_results, track_results_lock, on_failure_update,
                )

    with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="dl") as pool:
        futures = [pool.submit(worker, slot) for slot in range(jobs)]
        try:
            while wait(futures, timeout=0.075).not_done:  # keep keyboard stop and Ctrl+C responsive
                if not interactive_dashboard:
                    continue
                from .keys import read_key

                key = read_key(timeout=0)
                if key == "q":
                    if not stop.is_set():
                        stop.set()
                        if on_stopping is not None:
                            on_stopping()
                elif key == "f":
                    with failures_lock:
                        current_failures = list(failures)
                    if on_view_key is not None:
                        on_view_key(key, current_failures)
                    else:
                        from . import ui

                        ui.show_failures(current_failures)
                elif key == "esc" and on_view_key is not None:
                    with failures_lock:
                        current_failures = list(failures)
                    on_view_key(key, current_failures)
                elif key == "ctrl-c":
                    raise KeyboardInterrupt
        except KeyboardInterrupt:
            stop.set()
            try:  # let running conversions finish; a second Ctrl+C quits immediately
                wait(futures)
            except KeyboardInterrupt:
                lib.save(force=True)
                os._exit(130)
        for future in futures:
            if future.exception():
                log.error("worker crashed", exc_info=future.exception())
    return failures


def _download_one(ydl, entry, slot, lib, settings, tracker, stop, failures, failures_lock, on_result,
                  track_results, track_results_lock, on_failure_update):
    vid, started = entry["id"], time.time()
    tried_alternate = False
    download_id = vid
    playlist_ids = tuple(entry.get("playlist_ids", ()))
    tracker.start(slot, entry["title"])
    attempt = 0
    while True:
        try:
            info = ydl.extract_info(watch_url(download_id), download=True)
            file = _output_file(info, lib, vid, settings)
            size = file.stat().st_size
            lib.add_song(vid, {"file": file.name, "artist": first_text(info.get("artist"), "Unknown"),
                               "title": first_text(info.get("title"), entry["title"]),
                               "dur": int(info.get("duration") or entry["dur"] or 0),
                               "source_id": download_id})
            lib.save()
        except Cancelled:
            tracker.finish(slot, False)
            return
        except Exception as exc:
            if stop.is_set():
                tracker.finish(slot, False)
                return
            if (TRANSIENT.search(str(exc)) or isinstance(exc, IndexError)) and attempt < max(1, settings.retries):
                attempt += 1
                tracker.set_stage(slot, "retrying")
                log.warning("Retry %s for %s: %s", attempt, vid, exc)
                if stop.wait(min(2 ** (attempt - 1), 30)):
                    tracker.finish(slot, False)
                    return
                continue
            if settings.fallback and not tried_alternate and UNAVAILABLE.search(str(exc)) and entry["title"]:
                alternate = _find_alternate(entry, settings)
                if alternate:
                    tried_alternate = True
                    tracker.set_stage(slot, "alternate")
                    log.info("Using alternate %s for unavailable %s", alternate, vid)
                    download_id = alternate
                    continue
            reason = short_error(exc)
            log.exception("Download failed for %s", vid)
            failure = Failure(entry["title"], tracker.artist(slot) or "Unknown", reason, vid, playlist_ids)
            with failures_lock:
                failures.append(failure)
                current_failures = list(failures)
            if on_failure_update is not None:
                on_failure_update(current_failures)
            _record_track_result(
                track_results, track_results_lock,
                TrackResult(vid, entry["title"], playlist_ids, "failed", reason=reason),
            )
            tracker.finish(slot, False)
            if on_result:
                on_result(False, entry["title"], reason)
            return
        tracker.finish(slot, True, size)
        _record_track_result(
            track_results, track_results_lock,
            TrackResult(vid, entry["title"], playlist_ids, "done", bytes=size),
        )
        if on_result:
            on_result(True, entry["title"], f"{size / 1048576:.1f} MB  {time.time() - started:.1f}s")
        return


def _record_track_result(results: list[TrackResult] | None, lock, result: TrackResult) -> None:
    if results is not None:
        with lock:
            results.append(result)


def _output_file(info: dict, lib: Library, vid: str, settings: Settings) -> Path:
    downloads = info.get("requested_downloads") or []
    path = next((d.get("filepath") for d in downloads if isinstance(d, dict) and d.get("filepath")), None)
    if path:
        return Path(path)
    name = info.get("fname") or lib._reserved.get(vid) or safe_name(info.get("title") or vid)
    return lib.out / f"{name}.{settings.format}"
