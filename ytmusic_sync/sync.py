"""One sync run: scan -> plan -> download -> write playlists -> (optional) prune -> summary."""
from __future__ import annotations

import logging
import logging.handlers
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, links, ui, ytdlp
from .downloader import Failure, TrackResult, run_downloads
from .library import open_library
from .planner import Plan, make_plan
from .progress import Tracker
from .scanner import scan_links
from .widgets import confirm_boxes


@dataclass
class RunResult:
    new: int = 0
    bytes: int = 0
    elapsed: float = 0.0
    failures: list[Failure] = field(default_factory=list)
    stopped: bool = False
    track_results: list[TrackResult] = field(default_factory=list)
    duplicates_skipped: int = 0
    songs_before: int = 0
    size_before: int = 0
    songs_after: int = 0
    size_after: int = 0


def setup_logging() -> None:
    config.ensure_data_dir()
    root = logging.getLogger()
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        return
    handler = logging.handlers.RotatingFileHandler(config.LOG_FILE, maxBytes=512_000, backupCount=1,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def prepare(settings: config.Settings, urls: list[str]) -> Plan:
    """Open the library, scan the links and work out what is new."""
    lib = open_library(settings.out_path)
    lib.refresh_files()
    scan = scan_links(urls, settings, progress=lambda *_: None)
    return make_plan(lib, scan, settings.skip_duplicate_songs)


def remember_links(plan: Plan, only_playlists: bool = False) -> int:
    """Save the scanned links (with their names) to playlists.txt."""
    entries = [(url, name) for url, name in plan.scan.names.items()
               if not only_playlists or links.is_playlist_url(url)]
    return links.save_links(entries)


def execute(settings: config.Settings, plan: Plan, *, compact: bool = False, prune_ok: bool = False,
            interactive: bool = False) -> RunResult | None:
    """Run the plan. Returns None only when ffmpeg is missing."""
    out, lib = ui.console(), plan.lib
    if plan.todo and not ytdlp.ffmpeg_path():
        ui.print_missing_dependency("ffmpeg", ytdlp.FFMPEG_HELP)
        return None

    result = RunResult(
        track_results=[
            TrackResult(entry["id"], entry["title"], tuple(entry["playlist_ids"]), "skipped")
            for entry in plan.skipped
        ],
        duplicates_skipped=plan.duplicates,
        songs_before=len(lib.songs),
        size_before=lib.total_size(),
    )
    started = time.monotonic()
    stop = threading.Event()
    playlist_rows: list[dict] | None = None

    if plan.todo:
        tracker = Tracker(len(plan.todo), settings.jobs)

        def on_result(ok: bool, title: str, note: str) -> None:
            if compact:
                out.print(ui.song_line(ok, tracker.done + tracker.failed, tracker.total, title, note))

        if compact:
            result.failures = run_downloads(
                plan.todo, lib, settings, tracker, stop, on_result,
                track_results=result.track_results,
            )
        else:
            from rich.live import Live

            with Live(ui.Dashboard(tracker), console=out, refresh_per_second=8,
                      transient=True, screen=True) as live:
                stopping = {"progress": None}

                def show_stopping() -> None:
                    progress = ui.StoppingProgress()
                    stopping["progress"] = progress
                    live.update(ui.StoppingView(progress), refresh=True)

                result.failures = run_downloads(
                    plan.todo, lib, settings, tracker, stop,
                    track_results=result.track_results,
                    on_stopping=show_stopping,
                )
                _finish_unreported_tracks(plan, result)
                _set_result_totals(result)
                result.stopped = stop.is_set()
                progress = stopping["progress"]
                if progress is not None:
                    progress.set_step(0, "done")
                    progress.set_step(1, "done")
                    progress.set_step(2, "running")
                    live.update(ui.StoppingView(progress), refresh=True)
                    playlist_rows = _finish_run(settings, plan, result, started, progress, live)
        if compact:
            _finish_unreported_tracks(plan, result)
            _set_result_totals(result)
    result.stopped = stop.is_set()
    if playlist_rows is None:
        _finish_unreported_tracks(plan, result)
        _set_result_totals(result)
        playlist_rows = _finish_run(settings, plan, result, started, prune_ok=prune_ok)
    assert playlist_rows is not None

    if sum(1 for f in result.failures if ytdlp.BLOCKED.search(f.reason)) >= 2:
        ui.print_notice("These errors usually mean yt-dlp is outdated.")
        if interactive and confirm_boxes("Update yt-dlp now?", default=True):
            if ytdlp.upgrade():
                ui.print_success("yt-dlp updated. Restart ytmusic-sync to use it.")
            else:
                ui.print_error("Could not update yt-dlp.", "Try: pip install -U yt-dlp")
        else:
            ui.print_notice("Run: ytmusic-sync --update-ytdlp")
    ui.print_run_summary(
        result,
        lib,
        playlist_rows,
        len(plan.playlists),
        show_removed=settings.prune,
        wait_for_key=interactive,
    )
    return result


def _finish_unreported_tracks(plan: Plan, result: RunResult) -> None:
    recorded = {item.id for item in result.track_results}
    for entry in plan.todo:
        if entry["id"] not in recorded:
            result.track_results.append(
                TrackResult(entry["id"], entry["title"], tuple(entry["playlist_ids"]), "skipped")
            )


def _set_result_totals(result: RunResult) -> None:
    successful = {item.id: item for item in result.track_results if item.status == "done"}
    result.new = len(successful)
    result.bytes = sum(item.bytes for item in successful.values())


def _finish_run(settings: config.Settings, plan: Plan, result: RunResult, started: float,
                stopping=None, live=None, prune_ok: bool = False) -> list[dict]:
    lib = plan.lib
    previous_playlists = {}
    for playlist in plan.playlists:
        playlist_id = links.playlist_id(playlist.url) or playlist.url
        previous_name = lib.playlist_names.get(playlist_id, playlist.name)
        previous_playlists[playlist_id] = {
            lib.canonical(vid) for vid in lib.playlists.get(previous_name, [])
        }
    _write_playlists(settings, plan)
    removed_ids: set[str] = set()
    if settings.prune and prune_ok and not result.stopped and not plan.errors:
        removed_ids = _prune(plan)
    if stopping is not None:
        stopping.set_step(3, "running")
        live.update(ui.StoppingView(stopping), refresh=True)
    lib.cleanup_partials()
    if stopping is not None:
        stopping.set_step(3, "done")
        live.update(ui.StoppingView(stopping), refresh=True)

    result.elapsed = time.monotonic() - started
    result.songs_after, result.size_after = lib.recount()
    lib.record_run(result.new, result.bytes)
    lib.stats["new"] = result.new
    lib.stats["failed"] = len(result.failures)
    lib.stats["duration_seconds"] = result.elapsed

    added: dict[str, int] = {}
    failed: dict[str, int] = {}
    for item in result.track_results:
        counter = added if item.status == "done" else failed if item.status == "failed" else None
        if counter is not None:
            for playlist_id in set(item.playlist_ids):
                counter[playlist_id] = counter.get(playlist_id, 0) + 1
    removed = {
        playlist_id: len(previous_playlists.get(playlist_id, set()) & removed_ids)
        for playlist_id in previous_playlists
    }

    rows = []
    for playlist in plan.playlists:
        playlist_id = links.playlist_id(playlist.url) or playlist.url
        ids = [e["id"] for e in playlist.items]
        canonical = {lib.canonical(i) for i in ids}
        rows.append({
            "id": playlist_id,
            "name": playlist.name,
            "songs": len(canonical & lib.songs.keys()),
            "added": added.get(playlist_id, 0),
            "failed": failed.get(playlist_id, 0),
            "removed": removed.get(playlist_id, 0),
        })
    lib.stats["last_playlists"] = [
        {
            "id": row["id"],
            "name": row["name"],
            "songs": row["songs"],
            "new": row["added"],
            "failed": row["failed"],
        }
        for row in rows
    ]
    lib.save(force=True)
    _write_failed_file(result.failures)
    if stopping is not None:
        stopping.set_step(2, "done")
        live.update(ui.StoppingView(stopping), refresh=True)
    return rows


def _write_playlists(settings: config.Settings, plan: Plan) -> None:
    lib = plan.lib
    for playlist in plan.playlists:
        playlist_id = links.playlist_id(playlist.url) or playlist.url
        ids = [entry["id"] for entry in playlist.items]
        if settings.playlist_files:
            lib.write_playlist(playlist.name, ids)
        lib.playlists[playlist.name] = ids
        lib.playlist_names[playlist_id] = playlist.name


def _prune(plan: Plan) -> set[str]:
    lib = plan.lib
    stale = [vid for vid in lib.songs if vid not in plan.wanted]
    if stale and confirm_boxes(f"Delete {len(stale)} song(s) no longer in any playlist?", default=False):
        removed = {lib.canonical(vid) for vid in stale}
        lib.remove_songs(stale)
        # rewrite playlist files so they no longer point at deleted songs
        for name, ids in lib.playlists.items():
            if (lib.out / f"{name}.m3u8").exists():
                lib.write_playlist(name, ids)
        return removed
    return set()


def _write_failed_file(failures: list[Failure]) -> None:
    if not failures:
        config.FAILED_FILE.unlink(missing_ok=True)
        return
    clean = lambda text: " ".join(str(text).split())
    config.atomic_write(config.FAILED_FILE, "\n".join(
        f"{clean(f.title)}\t{clean(f.artist)}\t{clean(f.reason)}\t"
        f"{clean(f'https://www.youtube.com/watch?v={f.id}' if f.id else '')}"
        for f in failures) + "\n")
