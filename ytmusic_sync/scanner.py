"""Resolve links into playlists and songs (metadata only, nothing is downloaded)."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from . import config, links, ytdlp
from .config import Settings
from .library import open_library
from .progress import ScanProgress
from .tags import first_text, safe_name

SCAN_THREADS = 5
SCAN_CACHE_FILE = config.DATA_DIR / "scan-cache.json"
CACHE_TTL = 24 * 60 * 60
UNAVAILABLE = ("[Private video]", "[Deleted video]")
log = logging.getLogger(__name__)
_CACHE_LOCK = threading.Lock()


@dataclass
class ScannedPlaylist:
    name: str
    url: str
    items: list[dict]


@dataclass
class ScanResult:
    playlists: list[ScannedPlaylist] = field(default_factory=list)
    singles: list[dict] = field(default_factory=list)   # single-video links
    names: dict = field(default_factory=dict)           # url -> display name (for playlists.txt)
    errors: list[tuple[str, str]] = field(default_factory=list)
    unavailable: int = 0                                # private/deleted videos skipped
    progress: ScanProgress | None = None


def short_error(exc) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).replace("ERROR: ", "").strip()
    return (text.splitlines() or ["Unknown error"])[-1][:80]


def _item(entry: dict | None) -> dict | None:
    if not entry or not entry.get("id"):
        return None
    title = first_text(entry.get("title"), entry["id"])
    if title in UNAVAILABLE:
        return None
    return {"id": entry["id"], "title": title, "dur": int(entry.get("duration") or 0)}


def scan_links(urls: list[str], settings: Settings, progress=None) -> ScanResult:
    """Scan links in parallel. `progress(done, total)` is called as each link finishes."""
    urls = links.dedupe(urls)
    if not urls:
        return ScanResult()

    yt_dlp = ytdlp.module()
    options = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
               "skip_download": True, "logger": ytdlp.QuietLogger(),
               "socket_timeout": 10, "retries": 2}
    if settings.cookies:
        options["cookiesfrombrowser"] = (settings.cookies,)

    cache = _read_scan_cache()
    worker_local = threading.local()
    instances = []
    instances_lock = threading.Lock()

    def worker_ydl():
        instance = getattr(worker_local, "ydl", None)
        if instance is None:
            instance = yt_dlp.YoutubeDL(options)
            worker_local.ydl = instance
            with instances_lock:
                instances.append(instance)
        return instance

    def scan_one(index: int, url: str, stopped: threading.Event):
        if stopped.is_set():
            raise InterruptedError("Scan cancelled")
        scan_progress = result.progress
        if scan_progress:
            scan_progress.start(index)
        ydl = worker_ydl()
        pid = links.playlist_id(url)
        cached = _cached_playlist(pid, cache.get(pid), url, ydl) if pid else None
        if cached is not None:
            title, items, count = cached
            return index, url, "playlist", (title, items), count - len(items), None

        info = ydl.extract_info(url, download=False)
        if not info:
            raise ValueError("No playlist or song information found")
        if info.get("entries") is None:
            item = _item(info)
            if not item:
                raise ValueError("Video is private or unavailable")
            return index, url, "single", item, 0, None
        entries = list(info["entries"] or [])
        items = [i for i in map(_item, entries) if i]
        title = safe_name(first_text(info.get("title"), "playlist"))
        cache_entry = {
            "title": title,
            "ids": [item["id"] for item in items],
            "items": items,
            "count": len(entries),
            "timestamp": time.time(),
        } if pid else None
        return index, url, "playlist", (title, items), len(entries) - len(items), cache_entry

    result = ScanResult()
    library = None
    if progress is not None:
        identifiers = [links.playlist_id(url) or links.link_key(url)[1] for url in urls]
        if settings.out_path:
            library = open_library(settings.out_path)
        result.progress = ScanProgress(urls, identifiers, len(library.songs) if library else 0)

    completed: dict[int, tuple] = {}
    failures: dict[int, str] = {}
    cache_updates = {}
    stopped = threading.Event()
    provisional_items: dict[str, dict] = {}
    provisional_playlists: dict[str, set[str]] = {}
    provisional_titles: dict[int, list[str]] = {}
    provisioned: set[int] = set()

    def update_provisional(index: int) -> None:
        if result.progress is None or library is None or index in provisioned:
            return
        _, url, kind, payload, _, _ = completed[index]
        items = payload[1] if kind == "playlist" else [payload]
        new_ids = set()
        ordered_ids = []
        with library.lock:
            existing = set(library.songs)
            for item in items:
                canonical = library.canonical(item["id"])
                if canonical not in existing:
                    provisional_items.setdefault(canonical, item)
                    new_ids.add(canonical)
                    ordered_ids.append(canonical)
        if kind == "playlist":
            provisional_playlists[url] = new_ids
        provisional_titles[index] = ordered_ids
        provisioned.add(index)
        latest = []
        seen_ids = set()
        for playlist_index in sorted(provisional_titles):
            for video_id in provisional_titles[playlist_index]:
                if video_id not in seen_ids:
                    seen_ids.add(video_id)
                    latest.append(provisional_items[video_id]["title"])
                    if len(latest) >= ScanProgress.LATEST_LIMIT:
                        break
            if len(latest) >= ScanProgress.LATEST_LIMIT:
                break
        before = result.progress.snapshot()["library_before"]
        result.progress.update_plan(
            len(provisional_items),
            {url: len(ids) for url, ids in provisional_playlists.items()},
            before,
            before + len(provisional_items),
            latest,
        )

    def run_workers() -> None:
        done_count = 0
        with ThreadPoolExecutor(max_workers=SCAN_THREADS, thread_name_prefix="scan") as pool:
            future_indexes = {pool.submit(scan_one, index, url, stopped): index
                              for index, url in enumerate(urls)}
            pending = set(future_indexes)
            try:
                while pending:
                    if result.progress is not None:
                        from .keys import drain_pending, read_key

                        key = read_key(timeout=0)
                        queued_keys = drain_pending()
                        if key in ("q", "ctrl-c") or "q" in queued_keys.lower() or "\x03" in queued_keys:
                            stopped.set()
                            for future in pending:
                                future.cancel()
                            raise KeyboardInterrupt
                    finished, pending = wait(
                        pending,
                        timeout=0.125 if result.progress is not None else None,
                        return_when=FIRST_COMPLETED,
                    )
                    for future in finished:
                        index = future_indexes[future]
                        try:
                            value = future.result()
                        except Exception as exc:
                            error = short_error(exc)
                            failures[index] = error
                            if result.progress:
                                result.progress.fail(index, _scan_error_label(error))
                            log.warning("Scan failed for %s: %s", urls[index], error)
                        else:
                            completed[index] = value
                            _, _, kind, payload, _, cache_entry = value
                            if cache_entry:
                                cache_updates[links.playlist_id(urls[index])] = cache_entry
                            if result.progress:
                                title = payload[0] if kind == "playlist" else payload["title"]
                                songs = len(payload[1]) if kind == "playlist" else 1
                                result.progress.finish(index, title, songs)
                            update_provisional(index)
                        done_count += 1
                        if progress:
                            progress(done_count, len(urls))
            except KeyboardInterrupt:
                stopped.set()
                for future in pending:
                    future.cancel()
                raise

    try:
        if result.progress is not None:
            from rich.live import Live

            from . import ui

            with Live(ui.ScanPanel(result.progress), console=ui.console(), refresh_per_second=8, transient=True):
                run_workers()
        else:
            run_workers()
    finally:
        for instance in instances:
            try:
                instance.close()
            except Exception as exc:
                log.warning("Could not close yt-dlp scanner: %s", exc)

    used_names: set[str] = set()
    for index, url in enumerate(urls):
        value = completed.get(index)
        if value is None:
            if index in failures:
                result.errors.append((url[:60], failures[index]))
            continue
        _, _, kind, payload, skipped, _ = value
        if kind == "single":
            result.singles.append(payload)
            result.names[url] = payload["title"]
        else:
            name = _unique_name(payload[0], used_names)
            result.playlists.append(ScannedPlaylist(name, url, payload[1]))
            result.names[url] = name
            result.unavailable += skipped

    if cache_updates:
        _write_scan_cache(cache_updates)
    return result


def _read_scan_cache() -> dict:
    value = config.read_json(SCAN_CACHE_FILE, {})
    return value if isinstance(value, dict) else {}


def _write_scan_cache(updates: dict) -> None:
    with _CACHE_LOCK:
        cache = _read_scan_cache()
        cache.update(updates)
        try:
            config.atomic_write(SCAN_CACHE_FILE, json.dumps(cache, ensure_ascii=False))
        except OSError as exc:
            log.warning("Could not write playlist scan cache: %s", exc)


def _cached_playlist(pid: str, cached: dict | None, url: str, ydl) -> tuple[str, list[dict], int] | None:
    if not isinstance(cached, dict):
        return None
    try:
        age = time.time() - float(cached["timestamp"])
        count = int(cached["count"])
        title = str(cached["title"])
        items = cached["items"]
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(items, list) or age < 0 or age >= CACHE_TTL or count < 0 or count < len(items):
        return None
    normalized_items = []
    try:
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                return None
            normalized_items.append({
                "id": str(item["id"]),
                "title": first_text(item.get("title"), str(item["id"])),
                "dur": int(item.get("dur") or 0),
            })
    except (TypeError, ValueError):
        return None

    previous = ydl.params.get("playlist_items")
    had_previous = "playlist_items" in ydl.params
    ydl.params["playlist_items"] = "1"
    try:
        info = ydl.extract_info(url, download=False)
    except Exception as exc:
        log.debug("Playlist count check failed for %s: %s", pid, exc)
        return None
    finally:
        if had_previous:
            ydl.params["playlist_items"] = previous
        else:
            ydl.params.pop("playlist_items", None)
    try:
        current_count = int(info.get("playlist_count"))
    except (AttributeError, TypeError, ValueError):
        return None
    return (title, normalized_items, count) if current_count == count else None


def _scan_error_label(reason: str) -> str:
    lowered = reason.lower()
    if "private" in lowered:
        return "private"
    if "unavailable" in lowered or "deleted" in lowered:
        return "unavailable"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    return reason


def _unique_name(name: str, used: set[str]) -> str:
    candidate, n = name, 2
    while candidate.lower() in used:
        candidate, n = f"{name} ({n})", n + 1
    used.add(candidate.lower())
    return candidate
