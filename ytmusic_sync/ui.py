"""Terminal output: live dashboard, summaries, stats. Rich is imported lazily."""
from __future__ import annotations

import sys
from functools import lru_cache

from . import ytdlp
from .library import Library, time_ago
from .progress import Tracker

ACCENT, GOOD, BAD, DIM = "#ff9f1a", "#3fb950", "#f85149", "#8b8b8b"


@lru_cache(maxsize=1)
def console():
    from rich.console import Console

    try:  # box-drawing and ✓ need UTF-8 (Windows consoles default to cp1252 when redirected)
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    return Console()


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
    from rich.panel import Panel
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
    out.print(Panel("\n".join(body), border_style=ACCENT, title="Done", expand=False))

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
