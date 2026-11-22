"""Terminal output: live dashboard, summaries, stats. Rich is imported lazily."""
from __future__ import annotations

import threading
import time
from collections import Counter
from datetime import datetime

from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.style import Style
from rich.text import Text

from . import __version__, config, keys, ytdlp
from .library import Library, open_library, time_ago
from .progress import Tracker
from .theme import (
    ACCENT, BLUE, BORDER, BOX_BORDER, BRAND, CYAN, DARK_ORANGE, DIM, DIMMER, DIVIDER, GREEN, LINK,
    LINK_DIM, PATH, RED, TEXT, TRACK, WORKER_BORDER, get_console,
)
from .widgets import (
    SelectItem,
    clock_12h,
    divider,
    fmt_duration,
    fmt_size,
    fmt_speed,
    footer_hint,
    middle_truncate,
    panel_with_title,
    render_select,
    segmented_bar,
    sparkline,
    stage_meter,
    tile,
    truncate,
)

def console():
    return get_console()


def print_error(message: str, hint: str | None = None) -> None:
    out = console()
    line = Text("✗ ", style=Style(color=RED))
    line.append(truncate(" ".join(str(message).splitlines()), max(0, out.width - 2)),
                style=Style(color=RED))
    line.no_wrap = True
    line.overflow = "crop"
    out.print(line)
    if hint:
        note = Text(truncate(" ".join(str(hint).splitlines()), out.width), style=Style(color=DIM))
        note.no_wrap = True
        note.overflow = "crop"
        out.print(note)


def print_success(message: str) -> None:
    out = console()
    line = Text("✓ ", style=Style(color=GREEN))
    line.append(truncate(" ".join(str(message).splitlines()), max(0, out.width - 2)),
                style=Style(color=GREEN))
    line.no_wrap = True
    line.overflow = "crop"
    out.print(line)


def print_notice(message: str) -> None:
    line = Text(truncate(" ".join(str(message).splitlines()), console().width),
                style=Style(color=DIM))
    line.no_wrap = True
    line.overflow = "crop"
    console().print(line)


def print_path_message(prefix: str, path: str, suffix: str = "", *, success: bool = False) -> None:
    out = console()
    color = GREEN if success else DIM
    line = Text()
    if success:
        line.append("✓ ", style=Style(color=GREEN))
    line.append(prefix, style=Style(color=color))
    path_width = max(0, out.width - cell_len(line.plain) - cell_len(suffix))
    line.append(middle_truncate(path, path_width), style=Style(color=GREEN if success else PATH))
    line.append(suffix, style=Style(color=color))
    line.no_wrap = True
    line.overflow = "crop"
    out.print(line)


def print_path_error(prefix: str, path: str, hint: str) -> None:
    out = console()
    line = Text("✗ ", style=Style(color=RED))
    line.append(prefix, style=Style(color=RED))
    path_width = max(0, out.width - cell_len(line.plain))
    line.append(middle_truncate(path, path_width), style=Style(color=RED))
    line.no_wrap = True
    line.overflow = "crop"
    out.print(line)
    print_notice(hint)


def print_missing_dependency(name: str, fix: str) -> None:
    out = console()
    width = max(1, out.width - 6)
    lines = []
    for raw_line in fix.splitlines():
        if not raw_line.strip():
            continue
        line = Text(truncate(raw_line.strip(), width), style=Style(color=DIM))
        line.no_wrap = True
        line.overflow = "crop"
        lines.append(line)
    out.print(panel_with_title(
        Group(*lines),
        name=f"Missing {name}",
        border=RED,
        padding=(0, 1),
    ))


def build_header() -> RenderableType:
    settings = config.Settings.load()
    out_path = settings.out_path
    if out_path is None:
        raise ValueError("A music library folder is required to build the header.")
    lib = open_library(out_path)
    width = max(1, console().width - 4)

    def relative_time(stamp: str) -> str:
        try:
            elapsed = max(0, int((datetime.now() - datetime.strptime(stamp, "%Y-%m-%d %H:%M")).total_seconds()))
        except (TypeError, ValueError):
            return "never"
        if elapsed < 60:
            return "just now"
        if elapsed < 3600:
            minutes = elapsed // 60
            return f"{minutes} min ago"
        if elapsed < 86400:
            hours = elapsed // 3600
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        days = elapsed // 86400
        return f"{days} day{'s' if days != 1 else ''} ago"

    def status_line():
        path = str(out_path)
        stamp = lib.stats.get("last_run", "never")
        has_sync = stamp != "never"
        last_sync = relative_time(stamp)
        new_count = lib.stats.get("new")
        failed_count = lib.stats.get("failed")
        duration = lib.stats.get("duration_seconds")
        show_new = has_sync and isinstance(new_count, int) and not isinstance(new_count, bool)
        show_failed = has_sync and isinstance(failed_count, int) and not isinstance(failed_count, bool) and failed_count > 0
        show_duration = (has_sync and isinstance(duration, (int, float)) and not isinstance(duration, bool)
                         and duration >= 0)

        def suffix(include_duration: bool, include_new: bool, include_failed: bool) -> Text:
            parts: list[Text] = []
            last = Text()
            last.append("Last sync ", style=Style(color=DIM))
            last.append(last_sync, style=Style(color=TEXT))
            parts.append(last)
            if include_new:
                parts.append(Text(f"✓ {new_count} new", style=Style(color=GREEN)))
            if include_failed:
                parts.append(Text(f"✗ {failed_count} failed", style=Style(color=RED)))
            if include_duration:
                parts.append(Text(f"{duration:.1f}s", style=Style(color=DIM)))
            result = Text()
            for index, part in enumerate(parts):
                if index:
                    result.append("  ·  ", style=Style(color=DIM))
                result.append_text(part)
            return result

        def make_line(include_duration: bool, include_new: bool, include_failed: bool) -> Text:
            rest = suffix(include_duration, include_new, include_failed)
            available_path_width = max(0, width - cell_len(rest.plain) - cell_len("  ·  "))
            shortened_path = middle_truncate(path, available_path_width)
            line = Text()
            if shortened_path:
                line.append(shortened_path, style=Style(color=PATH))
                line.append("  ·  ", style=Style(color=DIM))
            line.append_text(rest)
            line.no_wrap = True
            line.overflow = "crop"
            return line

        line = make_line(show_duration, show_new, show_failed)
        if cell_len(line.plain) > width and show_duration:
            show_duration = False
            line = make_line(show_duration, show_new, show_failed)
        if cell_len(line.plain) > width and (show_new or show_failed):
            line = make_line(show_duration, False, False)
        return line

    tagline = Text("YouTube music + Local, always in sync", style=Style(color=DIM))
    tagline.no_wrap = True
    tagline.overflow = "crop"

    divider = Text("─" * width, style=Style(color=DIVIDER))
    totals = Text()
    totals.append(str(len(lib.songs)), style=Style(color=CYAN, bold=True))
    totals.append(" songs ", style=Style(color=TEXT))
    totals.append("·", style=Style(color=DIM))
    totals.append(" ", style=Style(color=TEXT))
    size, _, unit = fmt_size(lib.total_size()).partition(" ")
    totals.append(size, style=Style(color=CYAN, bold=True))
    totals.append(f" {unit} ", style=Style(color=TEXT))
    totals.append("·", style=Style(color=DIM))
    totals.append(" ", style=Style(color=TEXT))
    totals.append(str(len(lib.playlists)), style=Style(color=CYAN, bold=True))
    totals.append(" playlists", style=Style(color=TEXT))
    totals.no_wrap = True
    totals.overflow = "crop"

    body = Group(tagline, divider, status_line(), totals)
    return panel_with_title(
        body,
        name="♪ ytmusic-sync",
        dim=f"v{__version__}",
        border=BRAND,
        padding=(0, 1),
    )


def human_size(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def clock(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class ScanPanel:
    """Live playlist-scan view backed by the shared, locked scan state."""

    def __init__(self, progress):
        self.progress = progress

    def __rich_console__(self, console, options):
        from rich.table import Table

        snapshot = self.progress.snapshot()
        width = max(1, min(options.max_width, console.width) - 6)
        height = max(1, console.height - 9)
        completed = snapshot["finished"] + snapshot["failed"]
        elapsed = snapshot["elapsed"]
        eta = "—"
        if snapshot["finished"] >= 3 or elapsed >= 5:
            if completed:
                eta = clock(elapsed / completed * max(0, snapshot["total"] - completed))

        header = Table.grid(expand=True, padding=(0, 0))
        header.add_column(ratio=1)
        header.add_column(justify="right", no_wrap=True)
        left_header = Text()
        left_header.append("⠹", style=Style(color=GREEN))
        left_header.append(" Scanning playlists…", style=Style(color=BLUE))
        right_header = Text()
        right_header.append(f"{completed}", style=Style(color=TEXT))
        right_header.append(" / ", style=Style(color=DIM))
        right_header.append(f"{snapshot['total']}", style=Style(color=TEXT))
        right_header.append(" · Elapsed ", style=Style(color=DIM))
        right_header.append(clock(elapsed), style=Style(color=TEXT))
        right_header.append(" · ETA ", style=Style(color=DIM))
        right_header.append(eta, style=Style(color=ACCENT))
        header.add_row(left_header, right_header)

        bar = segmented_bar(
            width,
            [(snapshot["finished"], GREEN), (snapshot["failed"], RED),
             (snapshot["in_progress"], ACCENT)],
            snapshot["total"],
        )
        stacked = console.width < 100
        compact = console.width < 70
        rows_limit = max(1, height - 8) if stacked else height
        visible, more_queued = _visible_scan_rows(snapshot["playlists"], rows_limit)
        left_width = width if stacked else max(1, width - 50)
        left_table = _scan_playlist_table(visible, more_queued, left_width, compact)

        if stacked:
            remaining = max(8, height - len(visible) - (1 if more_queued else 0))
            right = _scan_summary(snapshot, min(width, 28), remaining)
            columns = Group(left_table, right)
        else:
            right_width = 28
            gap_width = 22
            columns = Table.grid(expand=True, padding=(0, 0))
            columns.add_column(width=left_width, no_wrap=True)
            columns.add_column(width=gap_width, no_wrap=True)
            columns.add_column(width=right_width, no_wrap=True)
            columns.add_row(left_table, Text(" " * gap_width), _scan_summary(snapshot, right_width, height))

        panel = panel_with_title(
            Group(header, bar, columns),
            name="Update library",
            border=BORDER,
        )
        yield Group(panel, footer_hint("q cancel scan"))


def _visible_scan_rows(playlists: list[dict], limit: int) -> tuple[list[tuple[dict, str]], int]:
    failures = [(index, row) for index, row in enumerate(playlists) if row["state"] == "failed"]
    finished = [(index, row) for index, row in enumerate(playlists) if row["state"] == "finished"]
    active = [(index, row) for index, row in enumerate(playlists) if row["state"] == "fetching"]
    queued = [(index, row) for index, row in enumerate(playlists) if row["state"] == "queued"]
    if len(playlists) <= limit:
        return [(row, "") for row in playlists], 0

    room = max(0, limit - len(active))
    if len(queued) > room:
        if room == 0:
            selected = active[:limit]
            return [(row, "") for _, row in selected], 0
        show_queued = max(0, room - 1)
        selected = sorted(active + queued[:show_queued], key=lambda item: item[0])
        return [(row, "") for _, row in selected], len(queued) - show_queued

    selected = active + queued
    terminal_room = max(0, limit - len(selected))
    if terminal_room:
        visible_failures = failures[-terminal_room:]
        selected.extend(visible_failures)
        terminal_room -= len(visible_failures)
    if terminal_room:
        selected.extend(finished[-terminal_room:])
    selected.sort(key=lambda item: item[0])
    return [(row, "") for _, row in selected], 0


def _scan_playlist_table(rows: list[tuple[dict, str]], more_queued: int, width: int, compact: bool):
    from rich.table import Table

    table = Table.grid(expand=True, padding=(0, 0))
    table.add_column(width=2, no_wrap=True)
    if compact:
        table.add_column(width=max(1, width - 2), no_wrap=True, overflow="ellipsis")
    else:
        table.add_column(width=max(1, width - 25), no_wrap=True, overflow="ellipsis")
        table.add_column(width=10, justify="right", no_wrap=True)
        table.add_column(width=13, justify="right", no_wrap=True)

    for row, _ in rows:
        state = row["state"]
        name = row["title"] or (row["id"][:13] + ("…" if len(row["id"]) > 13 else ""))
        name_color = DIMMER if state == "queued" else DIM if row["title"] is None else TEXT
        icon = {"finished": ("✓", GREEN), "fetching": ("●", ACCENT),
                "queued": ("○", DIMMER), "failed": ("✗", RED)}.get(state, ("○", DIMMER))
        if compact:
            table.add_row(Text(f"{icon[0]} ", style=Style(color=icon[1])),
                          Text(truncate(name, max(1, width - 2)), style=Style(color=name_color)))
            continue

        count = Text(f"{row['songs']} songs", style=Style(color=DIM)) if state == "finished" else Text("")
        if state == "finished":
            status = Text(f"+{row['new']} new", style=Style(color=GREEN)) if row["new"] else Text("")
        elif state == "fetching":
            status = Text("fetching…", style=Style(color=ACCENT))
        elif state == "queued":
            status = Text("queued", style=Style(color=DIMMER))
        else:
            status = Text(row["error"] or "unavailable", style=Style(color=RED))
        table.add_row(
            Text(f"{icon[0]} ", style=Style(color=icon[1])),
            Text(truncate(name, max(1, width - 25)), style=Style(color=name_color)),
            count,
            status,
        )

    if more_queued:
        summary = Text(f"+ {more_queued} more queued", style=Style(color=DIMMER))
        if compact:
            table.add_row(Text("  "), Text(truncate(summary.plain, max(1, width - 2)), style=Style(color=DIMMER)))
        else:
            table.add_row(Text("  "), Text(truncate(summary.plain, max(1, width - 25)), style=Style(color=DIMMER)),
                          Text(""), Text(""))
    return table


def _scan_summary(snapshot: dict, width: int, height: int):
    from rich.table import Table

    inner_width = max(1, width - 2)
    lines: list[Text] = []

    def append_line(text: Text) -> None:
        bordered = Text("│ ", style=Style(color=DIVIDER))
        bordered.append_text(text)
        bordered.truncate(width, overflow="ellipsis")
        lines.append(bordered)

    def label(text: str) -> Text:
        return Text(text, style=Style(color=DIM))

    def rule() -> Text:
        return Text("│ " + "─" * inner_width, style=Style(color=DIVIDER))

    append_line(label("New songs"))
    new_count = Text(str(snapshot["new_songs"]), style=Style(color=GREEN, bold=True))
    new_count.append(f" in {snapshot['new_playlists']} playlists", style=Style(color=DIM))
    append_line(new_count)
    lines.append(rule())
    append_line(label("Library after sync"))
    library = Text(str(snapshot["library_before"]), style=Style(color=DIM))
    library.append(" → ", style=Style(color=DIM))
    library.append(str(snapshot["library_after"]), style=Style(color=TEXT, bold=True))
    library.append(f" +{snapshot['library_after'] - snapshot['library_before']}", style=Style(color=GREEN))
    library.append(" songs", style=Style(color=DIM))
    append_line(library)
    failed = snapshot["failed"]
    append_line(Text(f"{failed} failed", style=Style(color=RED, bold=failed > 0) if failed else Style(color=DIM)))
    lines.append(rule())
    append_line(label("Latest new"))

    slots = max(0, height - len(lines))
    latest = snapshot["latest_new"]
    if latest and slots:
        visible_count = min(len(latest), slots)
        if len(latest) > slots:
            visible_count = max(0, slots - 1)
        for title in latest[:visible_count]:
            append_line(Text(truncate(title, inner_width), style=Style(color=TEXT)))
        remaining = max(0, snapshot["new_songs"] - visible_count)
        if remaining and len(lines) < height:
            append_line(Text(f"+{remaining} more", style=Style(color=DIMMER)))

    table = Table.grid(expand=False, padding=(0, 0))
    table.add_column(width=width, no_wrap=True)
    for line in lines[:max(1, height)]:
        table.add_row(line)
    return table


def print_scan_summary(snapshot: dict) -> None:
    out = console()
    line = Text()
    line.append("✓", style=Style(color=GREEN))
    line.append(
        f" Scanned {snapshot['total']} playlists · {snapshot['new_songs']} new · "
        f"{snapshot['failed']} failed · {snapshot['elapsed']:.1f}s",
        style=Style(color=TEXT),
    )
    line.truncate(out.width, overflow="crop")
    line.no_wrap = True
    line.overflow = "crop"
    out.clear()
    out.print(line)


# ---------- live dashboard ----------
def build_download_view(state, width: int, height: int) -> RenderableType:
    from rich.table import Table

    snapshot = state.snapshot()
    width = max(1, width)
    content_width = max(1, width - 6)
    completed = snapshot["done"] + snapshot["failed"]
    percent = completed * 100 / snapshot["total"] if snapshot["total"] else 100.0
    percent_text = Text(f"{percent:.1f}%", style=Style(color=ACCENT, bold=True))
    count_text = Text(f"{completed} / {snapshot['total']}", style=Style(color=DIM))
    bar_width = max(0, content_width - cell_len(percent_text.plain) - cell_len(count_text.plain) - 2)
    progress_row = Text()
    progress_row.append_text(percent_text)
    progress_row.append(" ")
    progress_row.append_text(segmented_bar(
        bar_width,
        [(snapshot["done"], GREEN), (snapshot["failed"], RED), (snapshot["active"], ACCENT)],
        snapshot["total"],
    ))
    progress_row.append(" ")
    progress_row.append_text(count_text)
    progress_row.no_wrap = True
    progress_row.overflow = "crop"

    speed_row = Text()
    speed_row.append("Speed ", style=Style(color=DIM))
    speed_row.append_text(sparkline(snapshot["speed_samples"], width=15))
    speed_row.append(f" {fmt_speed(snapshot['speed'])}", style=Style(color=TEXT))
    finishes = "—"
    eta_text = "—"
    if snapshot["elapsed"] >= 5 and snapshot["eta"] is not None:
        finishes = f"~{clock_12h(datetime.now().timestamp() + snapshot['eta'])}"
        eta_text = fmt_duration(snapshot["eta"])
    timing_row = Text()
    timing_row.append("Finishes ", style=Style(color=DIM))
    timing_row.append(finishes, style=Style(color=TEXT))
    timing_row.append(" · ETA ", style=Style(color=DIM))
    timing_row.append(eta_text, style=Style(color=ACCENT))

    timing = Table.grid(expand=True, padding=(0, 0))
    timing.add_column(ratio=1, no_wrap=True)
    timing.add_column(justify="right", no_wrap=True)
    timing.add_row(speed_row, timing_row)

    summary = Text()
    summary.append("● ", style=Style(color=GREEN))
    summary.append(str(snapshot["done"]), style=Style(color=TEXT))
    summary.append(" done  ", style=Style(color=DIM))
    summary.append("● ", style=Style(color=RED))
    summary.append(str(snapshot["failed"]), style=Style(color=TEXT))
    summary.append(" failed  ", style=Style(color=DIM))
    summary.append("● ", style=Style(color=ACCENT))
    summary.append(str(snapshot["active"]), style=Style(color=TEXT))
    summary.append(" active  ", style=Style(color=DIM))
    summary.append(fmt_size(snapshot["bytes_total"]), style=Style(color=TEXT))
    summary.append("  Elapsed ", style=Style(color=DIM))
    summary.append(clock(snapshot["elapsed"]), style=Style(color=TEXT))
    summary.no_wrap = True
    summary.overflow = "crop"
    summary.truncate(content_width, overflow="crop")

    overall = panel_with_title(
        Group(progress_row, timing, summary),
        name="Downloading",
        dim="Update library",
        dim_color=PATH,
        border=BORDER,
    )
    workers = _download_workers_panel(snapshot, width)
    failed_count = snapshot["failed"]
    footer = footer_hint(
        f"f failures ({failed_count}) · q stop safely · Ctrl+C force quit"
        if failed_count else "f failures · q stop safely · Ctrl+C force quit"
    )
    return Group(overall, workers, footer)


def _download_workers_panel(snapshot: dict, width: int) -> RenderableType:
    from rich.table import Table

    inner_width = max(1, width - 6)
    compact = width < 90
    if compact:
        track_width = max(1, inner_width - 38)
        columns = [4, track_width, 28]
    else:
        track_width = max(1, inner_width - 68)
        columns = [4, track_width, 14, 28, 12]

    table = Table(box=None, pad_edge=False, show_header=False, expand=True, padding=(0, 1))
    table.add_column(width=columns[0], no_wrap=True)
    table.add_column(width=columns[1], no_wrap=True)
    if compact:
        table.add_column(width=columns[2], no_wrap=True)
    else:
        table.add_column(width=columns[2], no_wrap=True)
        table.add_column(width=columns[3], no_wrap=True)
        table.add_column(width=columns[4], justify="right", no_wrap=True)

    header = [Text(""), Text("TRACK", style=Style(color=DIMMER))]
    if not compact:
        header.extend((
            Text("STAGE", style=Style(color=DIMMER)),
            Text("PROGRESS", style=Style(color=DIMMER)),
            Text("SPEED", style=Style(color=DIMMER)),
        ))
    else:
        header.append(Text("PROGRESS", style=Style(color=DIMMER)))
    table.add_row(*header)
    table.add_row(*(Text("─" * column, style=Style(color=DIVIDER)) for column in columns))

    stage_names = ("Fetch", "Download", "Convert", "Tag")
    progress_width = columns[-1] if compact else columns[3]
    for index, worker in enumerate(snapshot["workers"], 1):
        active = worker["state"] == "active"
        worker_label = Text(f"W{index}", style=Style(color=ACCENT if active else DIM))
        title = worker["title"] or ""
        track = Text(truncate(title, track_width), style=Style(color=TEXT, bold=True))
        track.append("\n")
        track.append(truncate(worker["artist"], track_width), style=Style(color=DIM))

        bytes_done, bytes_total = worker["bytes_done"], worker["bytes_total"]
        percent = min(100, int(bytes_done * 100 / bytes_total)) if bytes_total else 0
        fill = GREEN if bytes_total and percent == 100 else ACCENT if bytes_total and bytes_done else TRACK
        bar_width = max(1, progress_width - 5)
        bar = segmented_bar(bar_width, [(bytes_done if bytes_total else 0, fill)], bytes_total)
        progress = Text()
        progress.append_text(bar)
        progress.append(" ")
        progress.append(f"{percent:3d}%", style=Style(color=DIM if percent == 0 else TEXT))

        if compact:
            table.add_row(worker_label, track, progress)
            continue

        if active:
            stage = Text()
            stage.append_text(stage_meter(worker["stage_index"]))
            stage_name = "Alternate" if worker["stage"] == "alternate" else stage_names[worker["stage_index"]]
            stage.append(f" {stage_name}", style=Style(color=ACCENT))
        else:
            stage = Text("idle", style=Style(color=DIMMER))
        speed = (Text("waiting…", style=Style(color=DIMMER)) if bytes_done == 0
                 else Text(fmt_speed(worker["speed"]), style=Style(color=DIM)))
        table.add_row(worker_label, track, stage, progress, speed)

    upcoming = Text("Up next", style=Style(color=TEXT))
    titles = " · ".join(snapshot["upcoming"])
    remaining = snapshot["queue_remaining"]
    more = f" +{remaining} more" if remaining else ""
    available = max(0, inner_width - cell_len("Up next") - 2 - cell_len(more))
    if titles:
        upcoming.append("  ")
        upcoming.append(truncate(titles, available), style=Style(color=TEXT))
    if more:
        upcoming.append(more, style=Style(color=DIMMER))
    upcoming.no_wrap = True
    upcoming.overflow = "crop"

    body = Group(table, divider(inner_width), upcoming)
    return panel_with_title(
        body,
        name="Workers",
        dim=f"{snapshot['active']} active",
        border=WORKER_BORDER,
    )


def _failure_category(reason: str) -> str:
    value = reason.casefold()
    if "private" in value:
        return "private"
    if "age" in value:
        return "age restricted"
    if "region" in value or "geo" in value or "country" in value:
        return "region blocked"
    if "copyright" in value:
        return "copyright"
    if "timeout" in value or "timed out" in value:
        return "timeout"
    if "unavailable" in value or "not available" in value or "not found" in value:
        return "unavailable"
    return "other"


def _failure_url(failure) -> tuple[str, str]:
    url = getattr(failure, "url", "") or ""
    video_id = getattr(failure, "video_id", "") or getattr(failure, "id", "") or ""
    if not url and video_id:
        url = video_id if "://" in video_id else f"https://www.youtube.com/watch?v={video_id}"
    if not url:
        return "", ""
    display = url.split("://", 1)[-1]
    if display.startswith("www."):
        display = display[4:]
    if display.startswith("youtu.be/"):
        display = "youtube.com/watch?v=" + display.split("/", 1)[1].split("?", 1)[0]
    elif "youtube.com/watch?" in display:
        query = display.split("?", 1)[1]
        video_id = next((part[2:] for part in query.split("&") if part.startswith("v=")), "")
        if video_id:
            display = "youtube.com/watch?v=" + video_id
    return url, display


def _failure_list_summary(failures, width: int) -> Text:
    from collections import Counter

    labels = {
        "unavailable": "Video unavailable",
        "private": "Private",
        "age restricted": "Age restricted",
        "region blocked": "Region blocked",
        "copyright": "Copyright",
        "timeout": "Timeout",
        "other": "Other",
    }
    counts = Counter(_failure_category(getattr(item, "reason", "") or "") for item in failures)
    line = Text()
    for index, (category, count) in enumerate(
        sorted(counts.items(), key=lambda item: (-item[1], list(labels).index(item[0])))
    ):
        if index:
            line.append(" · ", style=Style(color=DIM))
        else:
            line.append("● ", style=Style(color=RED))
        line.append(labels[category], style=Style(color=DIM))
        line.append(f" {count}", style=Style(color=TEXT))
    prefix = " · details in "
    if line:
        line.append(prefix, style=Style(color=DIM))
    else:
        line.append("No failures yet. · details in ", style=Style(color=DIM))
    path = str(config.FAILED_FILE)
    path_width = max(0, width - cell_len(line.plain))
    line.append(middle_truncate(path, path_width), style=Style(color=DIM))
    line.no_wrap = True
    line.overflow = "crop"
    return line


def _failure_row(failure, selected: bool, width: int):
    from rich.table import Table

    category = _failure_category(getattr(failure, "reason", "") or "")
    reason = category
    reason_width = min(max(10, cell_len(reason)), max(10, width // 3))
    title_width = max(1, width - reason_width)
    title = getattr(failure, "title", "") or "Unknown title"
    selector = render_select(
        [SelectItem(label=title)],
        0 if selected else -1,
        variant="compact",
        label_width=max(0, title_width - 1),
        width=title_width,
    )
    row = Table.grid(expand=True, padding=(0, 0), pad_edge=False)
    row.add_column(ratio=1, no_wrap=True)
    row.add_column(width=reason_width, justify="right", no_wrap=True)
    row.add_row(selector, Text(reason, style=Style(color=RED)))

    url, display = _failure_url(failure)
    link = Text(f"  {display or 'link unavailable'}",
                style=Style(color=LINK_DIM if not selected else LINK, link=url or None))
    link.no_wrap = True
    link.overflow = "ellipsis"
    return Group(row, link)


def build_failures_view(failures, scroll: int, width: int, height: int) -> RenderableType:
    width = max(1, width)
    inner_width = max(1, width - 6)
    visible_count = max(0, (height - 6) // 2)
    max_scroll = max(0, len(failures) - visible_count)
    start = min(max(0, scroll), max_scroll)
    visible = failures[start:start + visible_count]
    content = [_failure_list_summary(failures, inner_width)]
    content.extend(_failure_row(item, False, inner_width) for item in visible)
    return Group(
        panel_with_title(
            Group(*content),
            name="Failures",
            dim=f"{len(failures)} tracks",
            border=BORDER,
        ),
        footer_hint("f back · ↑↓ scroll · q stop safely · Ctrl+C force quit"),
    )


def show_failures(failures) -> None:
    from rich.live import Live

    out = console()
    live = getattr(out, "_live", None)
    if live is not None:
        live.stop()
    selected = 0
    note = ""
    try:
        with out.screen():
            def view():
                width = max(1, out.width - 6)
                height = out.height
                summary = _failure_list_summary(failures, width)
                available_lines = max(0, height - 7 - (1 if note else 0))
                visible_count = max(1, available_lines // 2)
                start = max(0, min(selected - visible_count + 1, len(failures) - visible_count))
                end = min(len(failures), start + visible_count)
                content = [summary]
                if note:
                    content.append(Text(note, style=Style(color=DIM)))
                content.extend(_failure_row(item, index == selected, width)
                               for index, item in enumerate(failures)
                               if start <= index < end)
                if len(failures) > visible_count:
                    content.append(Text(f"+ {len(failures) - visible_count} more",
                                        style=Style(color=DIMMER)))
                return Group(
                    panel_with_title(
                        Group(*content),
                        name="Failures",
                        dim=f"{len(failures)} tracks",
                        border=BORDER,
                    ),
                    footer_hint("↑↓ move · enter open link · c copy link · esc back"),
                )

            with Live(view(), console=out, auto_refresh=False, transient=True) as failure_live:
                while True:
                    key = keys.read_key()
                    if key is None:
                        continue
                    if key in ("up", "down") and failures:
                        step = -1 if key == "up" else 1
                        selected = (selected + step) % len(failures)
                        note = ""
                    elif key == "enter" and failures:
                        url, _ = _failure_url(failures[selected])
                        note = "Could not open link." if url and not keys.open_url(url) else (
                            "Link is unavailable." if not url else "Opened link."
                        )
                    elif key == "c" and failures:
                        url, _ = _failure_url(failures[selected])
                        note = ("Link is unavailable." if not url else
                                "Link copied." if keys.copy_to_clipboard(url) else
                                "Clipboard is unavailable.")
                    elif key in ("esc", "ctrl-c"):
                        return
                    failure_live.update(view(), refresh=True)
    finally:
        if live is not None:
            live.start(refresh=True)


class Dashboard:
    """Rich renderable used by the download Live display."""

    def __init__(self, tracker: Tracker):
        self.tracker = tracker
        self.lock = threading.Lock()
        self.mode = "downloads"
        self.failures = []
        self.failure_scroll = 0

    def update_failures(self, failures) -> None:
        with self.lock:
            self.failures = list(failures)

    def handle_view_key(self, key: str, failures, height: int) -> None:
        with self.lock:
            if key == "f":
                if self.mode == "downloads":
                    self.failures = list(failures)
                    self.failure_scroll = 0
                    self.mode = "failures"
                else:
                    self.mode = "downloads"
                return
            if key == "esc" and self.mode == "failures":
                self.mode = "downloads"
                return
            if self.mode != "failures":
                return

            visible_count = max(0, (height - 6) // 2)
            step = max(1, visible_count) if key in ("pageup", "pagedown") else 1
            max_scroll = max(0, len(self.failures) - visible_count)
            if key in ("up", "pageup"):
                self.failure_scroll = max(0, self.failure_scroll - step)
            elif key in ("down", "pagedown"):
                self.failure_scroll = min(max_scroll, self.failure_scroll + step)

    def __rich_console__(self, console, options):
        with self.lock:
            mode = self.mode
            failures = list(self.failures)
            scroll = self.failure_scroll
        if mode == "failures":
            yield build_failures_view(failures, scroll, options.max_width, console.height)
        else:
            yield build_download_view(self.tracker, options.max_width, console.height)


class StoppingProgress:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.steps = ["running", "pending", "pending", "pending"]

    def set_step(self, index: int, state: str) -> None:
        with self.lock:
            self.steps[index] = state

    def snapshot(self) -> tuple[list[str], float]:
        with self.lock:
            return list(self.steps), max(0.0, time.monotonic() - self.started)


class StoppingView:
    def __init__(self, progress: StoppingProgress):
        self.progress = progress

    def __rich_console__(self, console, options):
        from rich.table import Table

        states, elapsed = self.progress.snapshot()
        spinner = ("⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏", "⠋", "⠙")
        frame = spinner[int(elapsed * 8) % len(spinner)]
        labels = (
            "Cancelling downloads",
            "Finishing conversions",
            "Saving library and playlists",
            "Cleaning up temp files",
        )
        lines = []
        for label, state in zip(labels, states):
            if state == "done":
                mark, color = "✓", GREEN
            elif state == "running":
                mark, color = frame, ACCENT
            else:
                mark, color = "○", DIMMER
            lines.append(Text(f"{mark} {label}", style=Style(color=color)))

        finished = sum(state == "done" for state in states)
        bar = segmented_bar(max(1, options.max_width - 6), [(finished, ACCENT)], len(states))
        timing = Table.grid(expand=True, padding=(0, 0))
        timing.add_column(ratio=1, no_wrap=True)
        timing.add_column(justify="right", no_wrap=True)
        message = Text("Your finished songs are saved.", style=Style(color=DIM))
        elapsed_text = Text()
        elapsed_text.append("Elapsed ", style=Style(color=DIM))
        elapsed_text.append(f"{elapsed:.1f}s", style=Style(color=TEXT))
        timing.add_row(message, elapsed_text)

        yield Group(
            panel_with_title(
                Group(*lines, bar, timing),
                name="Stopping safely",
                border=BORDER,
            ),
            footer_hint("Ctrl+C exit now"),
        )


def song_line(ok: bool, count: int, total: int, title: str, note: str) -> Text:
    width = max(1, console().width)
    mark = "✓" if ok else "✗"
    color = GREEN if ok else RED
    prefix = f"{mark} {count}/{total} "
    note_text = truncate(note, max(0, width // 3))
    title_width = max(0, width - cell_len(prefix) - cell_len(note_text) - (2 if note_text else 0))
    line = Text(prefix, style=Style(color=color))
    line.append(truncate(title, title_width), style=Style(color=TEXT))
    if note_text:
        line.append("  ")
        line.append(note_text, style=Style(color=DIM))
    line.no_wrap = True
    line.overflow = "crop"
    return line


# ---------- summaries ----------
def print_plan(plan, dry_run: bool = False) -> None:
    """Short pre-download overview (also used by the review screen)."""
    out = console()
    out.clear()
    scan = plan.scan
    summary = Text()
    summary.append(str(len(plan.wanted)), style=Style(color=TEXT, bold=True))
    summary.append(" unique songs · ", style=Style(color=DIM))
    summary.append(str(len(plan.todo)), style=Style(color=GREEN, bold=True))
    summary.append(" new · ", style=Style(color=DIM))
    summary.append(f"{len(plan.wanted) - len(plan.todo)} already in library", style=Style(color=DIM))
    summary.no_wrap = True
    summary.overflow = "crop"
    out.print(summary)
    for p in plan.playlists:
        line = Text("  • ", style=Style(color=DIM))
        line.append(truncate(p.name, max(0, out.width - 18)), style=Style(color=TEXT))
        line.append(f" ({len(p.items)})", style=Style(color=DIM))
        line.no_wrap = True
        line.overflow = "crop"
        out.print(line)
    notes = []
    if plan.duplicates:
        notes.append(f"{plan.duplicates} duplicate version(s) skipped")
    if scan.unavailable:
        notes.append(f"{scan.unavailable} private/deleted skipped")
    if notes:
        print_notice(" · ".join(notes))
    for url, reason in plan.errors:
        print_error(truncate(url, max(0, out.width - 2)), reason)
    if dry_run:
        for entry in plan.todo:
            line = Text("  + ", style=Style(color=GREEN))
            line.append(truncate(entry["title"], max(0, out.width - 4)), style=Style(color=TEXT))
            line.no_wrap = True
            line.overflow = "crop"
            out.print(line)
        print_notice(f"Dry run: {len(plan.todo)} new, nothing downloaded.")


def print_run_summary(
    result,
    lib: Library,
    playlist_rows: list[dict],
    playlist_count: int,
    *,
    show_removed: bool,
    wait_for_key: bool = False,
) -> None:
    out = console()
    width = max(1, out.width)
    content_width = max(1, width - 6)
    summary = Text()
    summary.append(f"{result.new} added", style=Style(color=GREEN, bold=True))
    summary.append(" · ", style=Style(color=DIM))
    if result.failures:
        summary.append(f"{len(result.failures)} failed", style=Style(color=RED, bold=True))
    else:
        summary.append("0 failed", style=Style(color=DIM))
    summary.append(" · ", style=Style(color=DIM))
    summary.append(fmt_size(result.bytes), style=Style(color=TEXT))
    summary.append(" · ", style=Style(color=DIM))
    summary.append(clock(result.elapsed), style=Style(color=TEXT))
    summary.no_wrap = True
    summary.overflow = "crop"

    body = [summary]
    if result.duplicates_skipped:
        duplicates = Text(f"{result.duplicates_skipped} duplicates skipped", style=Style(color=DIM))
        duplicates.no_wrap = True
        duplicates.overflow = "crop"
        body.append(duplicates)
    body.append(_playlist_results_table(
        playlist_rows,
        width,
        out.height,
        show_removed,
        duplicate_line=bool(result.duplicates_skipped),
        failure_line=bool(result.failures),
    ))

    library_line = Text("Library  ", style=Style(color=DIM))
    library_line.append(str(result.songs_before), style=Style(color=DIM))
    library_line.append(" → ", style=Style(color=DIM))
    library_line.append(str(result.songs_after), style=Style(color=TEXT, bold=True))
    library_line.append(" songs ", style=Style(color=DIM))
    song_delta = result.songs_after - result.songs_before
    library_line.append(f"{song_delta:+d}", style=Style(color=GREEN if song_delta >= 0 else RED))
    library_line.append(" · ", style=Style(color=DIM))
    library_line.append(fmt_size(result.size_before), style=Style(color=DIM))
    library_line.append(" → ", style=Style(color=DIM))
    library_line.append(fmt_size(result.size_after), style=Style(color=TEXT))
    library_line.append(" · ", style=Style(color=DIM))
    library_line.append(str(playlist_count), style=Style(color=TEXT))
    library_line.append(" playlists", style=Style(color=DIM))
    library_line.no_wrap = True
    library_line.overflow = "crop"
    body.append(library_line)

    all_time = Text("All-time ", style=Style(color=DIM))
    all_time.append(str(lib.stats["downloads"]), style=Style(color=TEXT))
    all_time.append(" downloads · ", style=Style(color=DIM))
    all_time.append(str(lib.stats["runs"]), style=Style(color=TEXT))
    all_time.append(" runs", style=Style(color=DIM))
    all_time.no_wrap = True
    all_time.overflow = "crop"
    body.append(all_time)

    if result.failures:
        body.append(_failure_summary(result.failures, content_width))
    run_again = Text("Run again anytime. Only new songs download.", style=Style(color=DIMMER))
    run_again.no_wrap = True
    run_again.overflow = "crop"
    body.append(run_again)

    panel = panel_with_title(
        Group(*body),
        name="Done",
        dim="Update library",
        border=BORDER,
    )
    result_view = Group(
        panel,
        footer_hint("enter back · f failures" if result.failures else "enter back"),
    )
    if wait_for_key:
        with out.screen():
            out.clear()
            out.print(result_view)
            while True:
                key = keys.read_key()
                if key == "f" and result.failures:
                    show_failures(result.failures)
                elif key in ("enter", "esc", "ctrl-c"):
                    break
    else:
        out.clear()
        out.print(result_view)


def _playlist_results_table(
    rows: list[dict],
    width: int,
    height: int,
    show_removed: bool,
    *,
    duplicate_line: bool,
    failure_line: bool,
):
    from rich.table import Table

    ordered = sorted(rows, key=lambda row: (-row["added"], -row["failed"], -row["songs"]))
    unchanged = [
        row for row in ordered
        if row["added"] == 0 and row["failed"] == 0
        and (not show_removed or row["removed"] == 0)
    ]
    display_rows = [
        row for row in ordered if row not in unchanged
    ]
    if unchanged:
        display_rows.append({"type": "unchanged", "count": len(unchanged)})

    column_widths = ([7] if width >= 70 else []) + [6, 6] + ([8] if show_removed else [])
    column_count = 1 + len(column_widths)
    inner_width = max(1, width - 6)
    name_width = max(1, inner_width - sum(column_widths) - column_count * 2)

    fixed_rows = 12 + int(duplicate_line) + int(failure_line)
    capacity = max(1, height - fixed_rows)
    if len(display_rows) > capacity:
        visible = display_rows[:max(0, capacity - 1)]
        hidden = display_rows[len(visible):]
        hidden_count = sum(row.get("count", 1) if row.get("type") == "unchanged" else 1
                           for row in hidden)
        display_rows = visible + [{"type": "more", "count": hidden_count}]

    table = Table(box=None, pad_edge=False, show_header=False, expand=True, padding=(0, 1))
    table.add_column("Playlist", ratio=1, no_wrap=True)
    if width >= 70:
        table.add_column("Songs", width=7, justify="right", no_wrap=True)
    table.add_column("Added", width=6, justify="right", no_wrap=True)
    table.add_column("Failed", width=6, justify="right", no_wrap=True)
    if show_removed:
        table.add_column("Removed", width=8, justify="right", no_wrap=True)

    headers = [Text("Playlist", style=Style(color=DIM))]
    if width >= 70:
        headers.append(Text("Songs", style=Style(color=DIM)))
    headers.extend((Text("Added", style=Style(color=DIM)), Text("Failed", style=Style(color=DIM))))
    if show_removed:
        headers.append(Text("Removed", style=Style(color=DIM)))
    table.add_row(*headers)
    separators = [Text("─" * name_width, style=Style(color=DIVIDER))]
    separators.extend(Text("─" * column, style=Style(color=DIVIDER)) for column in column_widths)
    table.add_row(*separators)

    for row in display_rows:
        kind = row.get("type")
        if kind in ("unchanged", "more"):
            label = (f"+ {row['count']} playlists unchanged" if kind == "unchanged"
                     else f"+ {row['count']} more")
            blank_cells = [Text("") for _ in column_widths]
            table.add_row(Text(label, style=Style(color=DIM)), *blank_cells)
            continue

        values = [Text(truncate(row["name"], name_width), style=Style(color=TEXT))]
        if width >= 70:
            values.append(Text(str(row["songs"]), style=Style(color=DIM)))
        values.append(Text(f"+{row['added']}" if row["added"] else "–",
                           style=Style(color=GREEN if row["added"] else DIM)))
        values.append(Text(str(row["failed"]) if row["failed"] else "–",
                           style=Style(color=RED if row["failed"] else DIM)))
        if show_removed:
            values.append(Text(str(row["removed"]) if row["removed"] else "–",
                               style=Style(color=CYAN if row["removed"] else DIM)))
        table.add_row(*values)
    return table


def _failure_summary(failures, width: int) -> Text:
    reasons = Counter(failure.reason for failure in failures if failure.reason)
    common = reasons.most_common(1)
    prefix = Text(f"✗ {len(failures)} failed", style=Style(color=RED))
    common_text = f" · most common: {common[0][0]} ×{common[0][1]}" if common else ""
    location = " · details saved to "
    path_width = max(0, width - cell_len(prefix.plain) - cell_len(common_text) - cell_len(location))
    path = middle_truncate(str(config.FAILED_FILE), path_width)
    line = Text()
    line.append_text(prefix)
    if common_text:
        line.append(common_text, style=Style(color=DIM))
    line.append(location, style=Style(color=DIM))
    line.append(path, style=Style(color=DIM))
    line.no_wrap = True
    line.overflow = "crop"
    return line


def _stat_count(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _stats_playlist_rows(lib: Library) -> list[dict]:
    saved = lib.stats.get("last_playlists")
    if isinstance(saved, list):
        source = saved
    else:
        source = [
            {
                "name": name,
                "songs": len({lib.canonical(value) for value in ids
                              if lib.canonical(value) in lib.songs}),
                "new": 0,
                "failed": 0,
            }
            for name, ids in lib.playlists.items()
        ]
    rows = []
    for item in source:
        if not isinstance(item, dict):
            continue
        rows.append({
            "name": str(item.get("name") or item.get("id") or "Unknown playlist"),
            "songs": _stat_count(item.get("songs")),
            "new": _stat_count(item.get("new", item.get("added", 0))),
            "failed": _stat_count(item.get("failed")),
        })
    return sorted(rows, key=lambda row: -row["songs"])


def _stats_sync_line(stats: dict) -> Text:
    stamp = stats.get("last_run")
    if not stamp or stamp == "never":
        return Text("Last sync never", style=Style(color=DIM))

    relative = time_ago(str(stamp))
    if relative == "never":
        return Text("Last sync never", style=Style(color=DIM))
    line = Text("Last sync ", style=Style(color=DIM))
    line.append(relative, style=Style(color=TEXT))
    details = []
    new_count = _stat_count(stats.get("new"))
    failed_count = _stat_count(stats.get("failed"))
    try:
        elapsed = float(stats.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        elapsed = 0
    if new_count:
        details.append(("+", f"{new_count} new", GREEN))
    if failed_count:
        details.append(("", f"{failed_count} failed", RED))
    if elapsed > 0:
        details.append(("", f"{elapsed:.1f}s", DIM))
    for sign, value, color in details:
        line.append(" · ", style=Style(color=DIM))
        line.append(f"{sign}{value}", style=Style(color=color))
    line.no_wrap = True
    line.overflow = "crop"
    return line


def _playlist_stats_panel(row: dict, rank: int, selected: bool, width: int):
    songs = Text()
    songs.append(str(row["songs"]), style=Style(
        color=ACCENT if rank < 3 else TEXT,
        bold=True,
    ))
    songs.append(" songs", style=Style(color=DIM))

    status = Text()
    parts = []
    if row["new"]:
        parts.append((f"+{row['new']} new", GREEN))
    if row["failed"]:
        parts.append((f"✗ {row['failed']} failed", RED))
    if not parts:
        status.append("no changes", style=Style(color=DIMMER))
    else:
        for index, (label, color) in enumerate(parts):
            if index:
                status.append(" · ", style=Style(color=DIM))
            status.append(label, style=Style(color=color))
    status.no_wrap = True
    status.overflow = "ellipsis"

    border = ACCENT if selected else DARK_ORANGE if rank < 3 else BOX_BORDER
    title = Text(
        truncate(row["name"], max(1, width - 4)),
        style=Style(color=TEXT, bold=rank < 3),
    )
    return panel_with_title(
        Group(songs, status),
        name=title,
        border=border,
        padding=(0, 1),
    )


def print_stats(lib: Library, *, interactive: bool = False) -> None:
    from math import ceil
    from rich.live import Live
    from rich.table import Table

    out = console()
    stats = lib.stats
    rows = _stats_playlist_rows(lib)
    selected = 0

    def view():
        width = max(1, out.width)
        inner_width = max(1, width - 6)
        metric_columns = 4 if width >= 80 else 2
        tile_width = max(4, (inner_width - 2 * (metric_columns - 1)) // metric_columns)
        metrics = [
            ("Songs", str(len(lib.songs))),
            ("Size", fmt_size(lib.total_size())),
            ("Downloads", str(_stat_count(stats.get("downloads")))),
            ("Runs", str(_stat_count(stats.get("runs")))),
        ]
        metric_grid = Table.grid(expand=True, padding=(0, 1), pad_edge=False)
        for _ in range(metric_columns):
            metric_grid.add_column(ratio=1)
        for start in range(0, len(metrics), metric_columns):
            metric_grid.add_row(*(
                tile(label, value, tile_width)
                for label, value in metrics[start:start + metric_columns]
            ))

        columns = 3 if width >= 100 else 2 if width >= 60 else 1
        card_width = max(8, (inner_width - 2 * (columns - 1)) // columns)
        total_rows = ceil(len(rows) / columns) if rows else 0
        tile_rows = ceil(len(metrics) / metric_columns)
        max_visible_rows = max(
            1,
            (out.height - 4 - 1 - tile_rows * 4 - 2 - 1) // 4,
        )
        visible_rows = min(total_rows, max_visible_rows) if total_rows else 0
        selected_row = selected // columns if rows else 0
        start_row = max(0, min(selected_row - visible_rows + 1, total_rows - visible_rows))
        start_index = start_row * columns
        end_index = min(len(rows), start_index + visible_rows * columns)
        grid = Table.grid(expand=True, padding=(0, 1), pad_edge=False)
        for _ in range(columns):
            grid.add_column(ratio=1)
        visible = rows[start_index:end_index]
        for offset in range(0, len(visible), columns):
            items = visible[offset:offset + columns]
            rendered = [
                _playlist_stats_panel(
                    item,
                    start_index + offset + index,
                    start_index + offset + index == selected,
                    card_width,
                )
                for index, item in enumerate(items)
            ]
            rendered.extend(Text("") for _ in range(columns - len(rendered)))
            grid.add_row(*rendered)

        body = [
            metric_grid,
            _stats_sync_line(stats),
            Text("Playlists", style=Style(color=BRAND, bold=True)),
        ]
        if rows:
            body.append(grid)
            hidden = len(rows) - len(visible)
            if hidden:
                body.append(Text(f"+ {hidden} more", style=Style(color=DIMMER)))
        return Group(
            panel_with_title(
                Group(*body),
                name="♪ Stats",
                dim="Library overview",
                border=BRAND,
            ),
            footer_hint("↑↓←→ move · enter or esc back"),
        )

    if not interactive:
        out.clear()
        out.print(view())
        return

    with Live(view(), console=out, auto_refresh=False, transient=True, screen=True) as live:
        while True:
            key = keys.read_key()
            if key in ("enter", "esc", "ctrl-c"):
                return
            if not rows:
                continue
            columns = 3 if out.width >= 100 else 2 if out.width >= 60 else 1
            if key == "up":
                selected = max(0, selected - columns)
            elif key == "down":
                selected = min(len(rows) - 1, selected + columns)
            elif key == "left":
                selected = max(0, selected - 1)
            elif key == "right":
                selected = min(len(rows) - 1, selected + 1)
            else:
                continue
            live.update(view(), refresh=True)


def print_update_hint() -> None:
    update = ytdlp.update_available()
    if update:
        print_notice(f"yt-dlp {update[0]} → {update[1]} available · Update yt-dlp in the menu, "
                     "or: ytmusic-sync --update-ytdlp")
