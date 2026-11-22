"""One sync run: scan -> plan -> download -> write playlists -> (optional) prune -> summary."""
from __future__ import annotations

import logging
import logging.handlers
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, links, ui, ytdlp
from .downloader import Failure, run_downloads
from .library import open_library
from .planner import Plan, make_plan
from .progress import Tracker
from .scanner import scan_links


@dataclass
class RunResult:
    new: int = 0
    bytes: int = 0
    elapsed: float = 0.0
    failures: list[Failure] = field(default_factory=list)
    stopped: bool = False


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
    out = ui.console()
    with out.status("[cyan]Loading library..."):
        lib = open_library(settings.out_path)
        lib.refresh_files()
    with out.status("[cyan]Scanning links...") as status:
        scan = scan_links(urls, settings,
                          progress=lambda done, total: status.update(f"[cyan]Scanning links... {done}/{total}"))
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
        out.print(f"[red]{ytdlp.FFMPEG_HELP}[/]")
        return None

    size_before = lib.total_size()
    started = time.monotonic()
    result = RunResult()
    stop = threading.Event()

    if plan.todo:
        tracker = Tracker(len(plan.todo), settings.jobs)

        def on_result(ok: bool, title: str, note: str) -> None:
            if compact:
                out.print(ui.song_line(ok, tracker.done + tracker.failed, tracker.total, title, note))

        if compact:
            result.failures = run_downloads(plan.todo, lib, settings, tracker, stop, on_result)
        else:
            from rich.live import Live

            with Live(ui.Dashboard(tracker), console=out, refresh_per_second=4, transient=True):
                result.failures = run_downloads(plan.todo, lib, settings, tracker, stop)
        result.new, result.bytes = tracker.done, tracker.bytes
    result.stopped = stop.is_set()

    rows = _write_playlists(settings, plan)
    if settings.prune and prune_ok and not result.stopped and not plan.errors:
        _prune(plan)
    lib.cleanup_partials()
    lib.record_run(result.new, result.bytes)
    result.elapsed = time.monotonic() - started
    lib.stats["new"] = result.new
    lib.stats["failed"] = len(result.failures)
    lib.stats["duration_seconds"] = result.elapsed
    lib.save(force=True)
    _write_failed_file(result.failures)

    ui.print_run_summary(result, lib, rows, size_before)
    if sum(1 for f in result.failures if ytdlp.BLOCKED.search(f.reason)) >= 2:
        out.print("[yellow]These errors usually mean yt-dlp is outdated.[/]")
        if interactive and ui.confirm("Update yt-dlp now?", default=True):
            if ytdlp.upgrade():
                out.print("[green]yt-dlp updated. Restart ytmusic-sync to use it.[/]")
        else:
            out.print("[dim]Run: ytmusic-sync --update-ytdlp[/]")
    return result


def _write_playlists(settings, plan: Plan) -> list[tuple[str, int, int]]:
    lib = plan.lib
    downloaded = {e["id"] for e in plan.todo if e["id"] in lib.songs}
    rows = []
    for playlist in plan.playlists:
        ids = [e["id"] for e in playlist.items]
        if settings.playlist_files:
            lib.write_playlist(playlist.name, ids)
        lib.playlists[playlist.name] = ids
        canonical = {lib.canonical(i) for i in ids}
        rows.append((playlist.name, len(canonical & lib.songs.keys()), len(canonical & downloaded)))
    return rows


def _prune(plan: Plan) -> None:
    lib = plan.lib
    stale = [vid for vid in lib.songs if vid not in plan.wanted]
    if stale and ui.confirm(f"Delete {len(stale)} song(s) no longer in any playlist?"):
        lib.remove_songs(stale)
        # rewrite playlist files so they no longer point at deleted songs
        for name, ids in lib.playlists.items():
            if (lib.out / f"{name}.m3u8").exists():
                lib.write_playlist(name, ids)


def _write_failed_file(failures: list[Failure]) -> None:
    if not failures:
        config.FAILED_FILE.unlink(missing_ok=True)
        return
    clean = lambda text: " ".join(str(text).split())
    config.atomic_write(config.FAILED_FILE, "\n".join(
        f"{clean(f.title)}\t{clean(f.artist)}\t{clean(f.reason)}" for f in failures) + "\n")
