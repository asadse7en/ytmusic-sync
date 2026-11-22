"""Terminal output: live dashboard, summaries, stats. Rich is imported lazily."""
from __future__ import annotations

from datetime import datetime

from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.style import Style
from rich.text import Text

from . import __version__, config, ytdlp
from .library import Library, open_library, time_ago
from .progress import Tracker
from .theme import ACCENT, BLUE, BORDER, BRAND, CYAN, DIM, DIMMER, DIVIDER, GREEN, PATH, RED, TEXT, get_console
from .widgets import fmt_size, footer_hint, panel_with_title, segmented_bar, truncate

def console():
    return get_console()


GOOD, BAD = GREEN, RED


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

    def middle_truncate(value: str, max_width: int) -> str:
        if max_width <= 0:
            return ""
        if cell_len(value) <= max_width:
            return value
        if max_width == 1:
            return "…"
        available = max_width - 1
        left_width = (available + 1) // 2
        right_width = available - left_width

        def take_left(text: str, limit: int) -> str:
            result = ""
            for char in text:
                if cell_len(result + char) > limit:
                    break
                result += char
            return result

        left = take_left(value, left_width)
        right = take_left(value[::-1], right_width)[::-1]
        return left + "…" + right

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
            available_path_width = max(0, width - cell_len(rest) - cell_len("  ·  "))
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
        if cell_len(line) > width and show_duration:
            show_duration = False
            line = make_line(show_duration, show_new, show_failed)
        if cell_len(line) > width and (show_new or show_failed):
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
        name_color=BRAND,
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


def confirm(question: str, default: bool = False) -> bool:
    """Plain y/n prompt (no extra imports, works in any terminal)."""
    try:
        answer = console().input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return default if not answer else answer.startswith("y")


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
    terminal = [(index, row) for index, row in enumerate(playlists)
                if row["state"] in ("finished", "failed")]
    active = [(index, row) for index, row in enumerate(playlists) if row["state"] == "fetching"]
    queued = [(index, row) for index, row in enumerate(playlists) if row["state"] == "queued"]
    if len(playlists) <= limit:
        return [(row, "") for row in playlists], 0

    room = max(0, limit - len(active))
    if len(queued) > room:
        if room == 0:
            selected = active[:limit]
            return [(row, "") for _, row in selected], len(queued)
        show_queued = max(0, room - 1)
        selected = sorted(active + queued[:show_queued], key=lambda item: item[0])
        return [(row, "") for _, row in selected], len(queued) - show_queued

    selected = active + queued
    terminal_room = max(0, limit - len(selected))
    if terminal_room:
        selected.extend(terminal[-terminal_room:])
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
    line = Text()
    line.append("✓", style=Style(color=GREEN))
    line.append(
        f" Scanned {snapshot['total']} playlists · {snapshot['new_songs']} new · "
        f"{snapshot['failed']} failed · {snapshot['elapsed']:.1f}s",
        style=Style(color=TEXT),
    )
    console().print(line)


# ---------- live dashboard ----------
class Dashboard:
    """Rich renderable: progress bar, totals and one line per active download."""

    def __init__(self, tracker: Tracker):
        self.tracker = tracker

    def __rich_console__(self, console, options):
        from rich.text import Text

        snap = self.tracker.snapshot()
        width = max(options.max_width, 40)
        total, done, failed, active = snap["total"], snap["done"], snap["failed"], snap["active"]
        left = max(total - done - failed - active, 0)
        finished = done + failed
        percent = f"{finished / total:.0%}" if total else "100%"
        count = f"{finished}/{total}"
        bar_width = max(width - len(percent) - len(count) - 4, 10)

        bar = Text(f"{percent} ", style=f"bold {ACCENT}")
        segments = [done, failed, active, left]
        cells = _split(segments, bar_width)
        for n, color in zip(cells, (GOOD, BAD, ACCENT, "#333333")):
            bar.append("━" * n, style=color)
        bar.append(f" {count}", style=DIM)
        yield bar

        speed = snap["speed"]
        stats = Text()
        stats.append(f"✓ {done}", style=GOOD)
        stats.append(f"  ✗ {failed}", style=BAD if failed else DIM)
        stats.append(f"  ● {active} active", style=ACCENT)
        stats.append(f"  ·  {human_size(snap['bytes'])}  ·  {human_size(speed)}/s", style=DIM)
        stats.append(f"  ·  ETA {clock(snap['eta']) if snap['eta'] else '—'}  ·  {clock(snap['elapsed'])}", style=DIM)
        yield stats
        yield Text("")

        for slot in snap["slots"]:
            line = Text()
            line.append("● ", style=ACCENT)
            label = slot.title or "…"
            if slot.artist:
                label += f" — {slot.artist}"
            label = label[:max(width - 24, 10)]
            line.append(label.ljust(max(width - 24, 10)))
            if slot.stage == "downloading" and slot.total:
                line.append(f"  {slot.downloaded / slot.total:>4.0%}", style=DIM)
                line.append(f"  {human_size(slot.speed)}/s", style=DIM)
            else:
                line.append(f"  {slot.stage}", style=DIM)
            yield line


def _split(counts: list[int], width: int) -> list[int]:
    """Divide `width` cells between segments proportionally (largest remainder)."""
    total = sum(counts)
    if not total:
        return [0, 0, 0, width]
    cells = [c * width // total for c in counts]
    order = sorted(range(len(counts)), key=lambda i: (counts[i] * width) % total, reverse=True)
    for i in order[:width - sum(cells)]:
        cells[i] += 1
    return cells


def song_line(ok: bool, count: int, total: int, title: str, note: str) -> str:
    mark = "[green]✓[/]" if ok else "[red]✗[/]"
    return f"{mark} {count}/{total} {title[:50]}  [dim]{note}[/]"


# ---------- summaries ----------
def print_plan(plan, dry_run: bool = False) -> None:
    """Short pre-download overview (also used by the review screen)."""
    out = console()
    scan = plan.scan
    out.print(f"[bold]{len(plan.wanted)}[/] unique songs · [bold {GOOD}]{len(plan.todo)}[/] new · "
              f"{len(plan.wanted) - len(plan.todo)} already in library")
    for p in plan.playlists:
        out.print(f"  [dim]•[/] {p.name} [dim]({len(p.items)})[/]")
    notes = []
    if plan.duplicates:
        notes.append(f"{plan.duplicates} duplicate version(s) skipped")
    if scan.unavailable:
        notes.append(f"{scan.unavailable} private/deleted skipped")
    if notes:
        out.print("[dim]" + " · ".join(notes) + "[/]")
    for url, reason in plan.errors:
        out.print(f"[red]✗[/] {url}  [dim]{reason}[/]")
    if dry_run:
        for entry in plan.todo:
            out.print(f"  [green]+[/] {entry['title']}")
        out.print(f"\n[yellow]Dry run: {len(plan.todo)} new, nothing downloaded.[/]")


def print_run_summary(result, lib: Library, playlist_rows: list[tuple], size_before: int) -> None:
    from rich.table import Table

    out = console()
    head = f"[bold {GOOD}]✓ {result.new} new[/]"
    if result.failures:
        head += f"  [bold {BAD}]✗ {len(result.failures)} failed[/]"
    if result.stopped:
        head += "  [yellow]stopped early (progress saved)[/]"
    after = lib.total_size()
    body = [head,
            f"[dim]{human_size(result.bytes)} downloaded in {clock(result.elapsed)}[/]",
            f"[dim]Library: {len(lib.songs)} songs · {human_size(after)} "
            f"({'+' if after >= size_before else '-'}{human_size(abs(after - size_before))})[/]"]
    out.print(panel_with_title("\n".join(body), "Done", border=ACCENT, name_color=ACCENT, padding=(0, 1)))

    if playlist_rows:
        table = Table(box=None, pad_edge=False, show_header=True, header_style=DIM)
        table.add_column("Playlist")
        table.add_column("Songs", justify="right")
        table.add_column("New", justify="right")
        for name, songs, added in playlist_rows:
            table.add_row(name, str(songs), f"+{added}" if added else "·")
        out.print(table)

    if result.failures:
        out.print("\n[bold]Failed[/]")
        for failure in result.failures[:10]:
            out.print(f"  [red]✗[/] {failure.title[:50]}  [dim]{failure.reason}[/]")
        if len(result.failures) > 10:
            out.print(f"  [dim]… and {len(result.failures) - 10} more (see failed.txt)[/]")


def print_stats(lib: Library) -> None:
    from rich.table import Table

    out = console()
    s = lib.stats
    out.print(f"[bold {ACCENT}]Library[/]  {lib.out}")
    out.print(f"  Songs [bold]{len(lib.songs)}[/]  ·  Size [bold]{human_size(lib.total_size())}[/]  ·  "
              f"Downloaded all-time [bold]{s['downloads']}[/] ({human_size(s['bytes'])})")
    out.print(f"  Runs [bold]{s['runs']}[/]  ·  Last sync [bold]{time_ago(s['last_run'])}[/]")
    if lib.playlists:
        table = Table(box=None, pad_edge=False, header_style=DIM)
        table.add_column("Playlist")
        table.add_column("Songs", justify="right")
        counts = {n: len({lib.canonical(v) for v in ids if lib.canonical(v) in lib.songs})
                  for n, ids in lib.playlists.items()}
        for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            table.add_row(name, str(count))
        out.print()
        out.print(table)


def print_update_hint() -> None:
    update = ytdlp.update_available()
    if update:
        console().print(f"[yellow]yt-dlp {update[0]} → {update[1]} available[/] [dim](Update yt-dlp in the menu, "
                        f"or: ytmusic-sync --update-ytdlp)[/]")
