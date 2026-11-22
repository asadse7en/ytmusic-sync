"""Interactive arrow-key menus. Every menu has "‹ Back"; Ctrl+C goes back (or quits from home)."""
from __future__ import annotations

import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from rich.console import Group
from rich.text import Text

from . import config, links, sync, ui, ytdlp
from .config import Settings
from .keys import read_key
from .library import open_library
from .scanner import short_error
from .theme import ACCENT, BORDER, CYAN, DIM, GREEN, PATH, TEXT
from .widgets import (
    SelectItem,
    confirm_boxes,
    divider,
    footer_hint,
    panel_with_title,
    render_select,
    ruled_heading,
    truncate,
    run_select,
)

BACK = "__back__"
SETTING_ROWS = [
    ("format", "Format"), ("quality", "Quality"), ("cover", "Cover art"), ("square", "Square cover"),
    ("name_style", "Filename style"), ("clean", "Clean titles"), ("playlist_files", "Playlist .m3u8 files"),
    ("prune", "Prune removed songs"), ("skip_duplicate_songs", "Skip duplicate songs"),
    ("jobs", "Parallel downloads"), ("rate_limit", "Speed limit"), ("retries", "Retries"),
    ("cookies", "Browser cookies"),
]
CHOICES = {"format": config.FORMATS, "quality": config.QUALITIES, "name_style": tuple(config.NAME_STYLES),
           "jobs": tuple(range(1, config.MAX_JOBS + 1))}
SETTING_GROUPS = (
    ("Audio", (("format", "Format"), ("quality", "Quality"), ("cover", "Cover art"), ("square", "Square cover"))),
    ("Library", (("name_style", "Filename style"), ("clean", "Clean titles"),
                 ("playlist_files", "Playlist .m3u8 files"), ("prune", "Prune removed songs"),
                 ("skip_duplicate_songs", "Skip duplicate songs"))),
    ("Downloads", (("jobs", "Parallel downloads"), ("rate_limit", "Speed limit"),
                   ("retries", "Retries"), ("cookies", "Browser cookies"))),
    ("Folders", (("_library", "Library folder"), ("_data", "Data folder"), ("_links", "Edit Playlists.txt"),
                 ("_reset", "Reset to defaults"), (BACK, "Back"))),
)
SETTING_DESCRIPTIONS = {
    "format": "output file type. Options: mp3 · m4a · opus · flac · wav · aac",
    "quality": "audio quality. Options: best · 320 · 256 · 192 · 128",
    "cover": "embed cover art in downloaded files.",
    "square": "make embedded cover art square.",
    "name_style": "Options: Title · Artist - Title · Title - Artist",
    "clean": "clean titles before using them in filenames and tags.",
    "playlist_files": "write playlist .m3u8 files in the library.",
    "prune": "remove songs no longer present in synced playlists.",
    "skip_duplicate_songs": "skip songs already downloaded in the library.",
    "jobs": f"number of parallel downloads. Options: 1–{config.MAX_JOBS}",
    "rate_limit": "maximum download speed; empty means unlimited.",
    "retries": "number of retries after a failed download. Options: 0–20",
    "cookies": "browser used for cookies; empty means none (e.g. chrome, firefox, edge).",
    "_library": "choose the music library folder.",
    "_data": "open the data folder and browse its files.",
    "_links": "open Playlists.txt for editing saved playlist links.",
    "_reset": "restore defaults while keeping the library folder.",
    BACK: "return to the previous menu.",
}


@lru_cache(maxsize=1)
def _q():
    try:
        import questionary
    except ImportError as exc:
        raise RuntimeError("Missing package 'questionary'. Run: pip install questionary") from exc
    return questionary


def _style():
    return _q().Style([("pointer", "fg:#ff9f1a bold"), ("highlighted", "fg:#ff9f1a bold"),
                       ("selected", "fg:#ff9f1a"), ("question", "bold")])


def pick(message: str, items: list[tuple[str, object]], back: bool = True):
    """Arrow-key menu. Returns the chosen value, or None for Back / Ctrl+C."""
    q = _q()
    choices = [q.Choice(title=label, value=value) for label, value in items]
    if back:
        choices.append(q.Choice(title="‹ Back", value=BACK))
    answer = q.select(message, choices=choices, style=_style(), qmark="", instruction=" ").ask()
    return None if answer in (None, BACK) else answer


def ask_text(message: str, default: str = "") -> str | None:
    return _q().text(message, default=default, style=_style(), qmark="").ask()


def pause() -> None:
    try:
        ui.console().input("[dim]Press Enter to continue[/]")
    except (EOFError, KeyboardInterrupt):
        pass


def open_path(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 - opens the user's own file in its default app
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        ui.console().print(f"[red]Could not open {path}: {exc}[/]")


# ---------- entry point ----------
def run(settings: Settings) -> int:
    out = ui.console()
    sync.setup_logging()
    ytdlp.check_for_updates_in_background()
    try:
        if not _library_ready(settings):
            if not choose_library(settings):
                return 0
        if _first_run(settings) and not welcome(settings):
            return 0
        home(settings)
    except KeyboardInterrupt:
        pass
    out.print("[dim]Bye.[/]")
    return 0


def _library_ready(settings: Settings) -> bool:
    return bool(settings.out) and Path(settings.out).is_dir()


def _first_run(settings: Settings) -> bool:
    return not links.read_links() and not config.library_db_path(Path(settings.out)).exists()


def choose_library(settings: Settings) -> bool:
    out = ui.console()
    if settings.out and not Path(settings.out).is_dir():
        out.print(f"[yellow]Music folder is unavailable: {settings.out}. Reconnect the drive or choose a folder.[/]")
    default = settings.out or str(Path.home() / "Music" / "YouTube")
    answer = _q().path("Music folder:", default=default, only_directories=True, style=_style(), qmark="").ask()
    if not answer:
        return False
    path = Path(answer).expanduser()
    if not path.exists():
        if not path.parent.is_dir():
            out.print(f"[red]Parent folder does not exist: {path.parent}[/]")
            return False
        path.mkdir()
    settings.out = str(path.resolve())
    settings.save()
    return True


def welcome(settings: Settings) -> bool:
    """First-run screen. Returns False when the user quits."""
    choice = pick("Welcome", [("Use defaults & start", "go"), ("Customize", "custom"), ("Quit", "quit")], back=False)
    if choice in (None, "quit"):
        return False
    if choice == "custom":
        settings_menu(settings)
    add_music(settings)
    return True


# ---------- home ----------
def home(settings: Settings) -> None:
    while True:
        items = [
            SelectItem("Update library", "Download new songs", "u", value="update"),
            SelectItem("Add Music", "Paste links", "a", value="add"),
            SelectItem("Settings", f"{settings.format} · {settings.quality}", "s", value="settings"),
            SelectItem("Stats", "Playlists overview", "t", value="stats"),
            SelectItem("Quit", hotkey="q", value="quit"),
        ]

        def view_factory(selected: int):
            return Group(
                ui.build_header(),
                Text(""),
                render_select(items, selected, variant="boxed"),
                footer_hint("↑↓ move · enter select · press the letter to jump"),
            )

        selected = run_select(view_factory, items, variant="boxed")
        choice = items[selected].value if selected is not None else None
        if choice in (None, "quit"):
            return
        try:
            if choice == "update":
                update_library(settings)
            elif choice == "add":
                add_music(settings)
            elif choice == "settings":
                settings_menu(settings)
            elif choice == "stats":
                ui.print_stats(open_library(settings.out_path))
                pause()
            elif choice == "ytdlp":
                ui.console().print("[green]yt-dlp updated. Restart ytmusic-sync to use it.[/]" if ytdlp.upgrade()
                                   else "[red]Update failed. Try: pip install -U yt-dlp[/]")
                pause()
        except KeyboardInterrupt:
            ui.console().print("\n[yellow]Cancelled.[/]")
        except Exception as exc:
            _error(exc)


def _error(exc: Exception) -> None:
    import logging

    logging.getLogger(__name__).exception("Menu action failed")
    ui.console().print(f"[red]Error: {short_error(exc)}[/]")
    pause()


def update_library(settings: Settings) -> None:
    urls = links.read_links()
    if not urls:
        ui.console().print(f"[yellow]No saved links yet. Use Add Music, or put links in {config.LINKS_FILE}[/]")
        pause()
        return
    run_plan(settings, urls, review=False, prune_ok=True)


# ---------- add music ----------
def add_music(settings: Settings) -> None:
    while True:
        saved = len(links.read_links())
        items = [
            SelectItem("Paste links", "Playlists or songs", value="paste"),
            SelectItem("Load from Playlists.txt", f"{saved} links", value="load"),
            SelectItem("Edit Playlists.txt", "Open in editor", value="edit"),
            SelectItem("Back", value=BACK),
        ]

        def view_factory(selected: int):
            content = Group(
                render_select(items, selected, variant="boxed"),
                footer_hint("↑↓ move · enter select · esc back"),
            )
            return panel_with_title(content, name="Add Music", border=BORDER)

        selected = run_select(view_factory, items, variant="boxed")
        choice = items[selected].value if selected is not None else None
        if choice in (None, BACK):
            return
        if choice == "paste":
            urls = paste_links()
            if urls:
                run_plan(settings, urls, review=True, offer_save=True)
                return
        elif choice == "load":
            if not links.read_links():
                _ensure_links_file()
                ui.console().print(f"[yellow]No links found. Add one link per line in {config.LINKS_FILE}.[/]")
                continue
            run_plan(settings, links.read_links(), review=True, prune_ok=True)
            return
        elif choice == "edit":
            _ensure_links_file()
            open_path(config.LINKS_FILE)


def _ensure_links_file() -> None:
    config.ensure_data_dir()
    if not config.LINKS_FILE.exists():
        config.LINKS_FILE.write_text("# One link per line. Put a '# name' line above a link to label it.\n",
                                     encoding="utf-8")


def paste_links() -> list[str]:
    """Read links one per line until an empty line. Only valid, new links are counted."""
    out = ui.console()
    out.print("[dim]Paste YouTube links (playlists or songs), one per line. Empty line = done. Ctrl+C = back.[/]")
    urls: list[str] = []
    seen: set = set()
    while True:
        try:
            line = out.input(f"[{ui.ACCENT}]{len(urls) + 1}[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            return []
        if not line:
            return urls
        valid, rejected = links.parse_pasted(line)
        for url in valid:
            key = links.link_key(url)
            if key in seen:
                out.print("  [dim]already added[/]")
            else:
                seen.add(key)
                urls.append(url)
        for piece in rejected:
            out.print(f"  [red]not a YouTube link:[/] [dim]{piece[:60]}[/]")


def run_plan(settings: Settings, urls: list[str], *, review: bool, prune_ok: bool = False,
             offer_save: bool = False) -> None:
    plan = sync.prepare(settings, urls)
    out = ui.console()
    if not plan.wanted and not plan.todo:
        for url, reason in plan.errors:
            out.print(f"[red]✗[/] {url}  [dim]{reason}[/]")
        out.print("[yellow]Nothing to do.[/]")
        pause()
        return
    save = False
    if review:
        while True:
            ui.print_plan(plan)
            items = [("Start" if plan.todo else "Update playlists", "start")]
            if offer_save and any(links.is_playlist_url(u) for u in plan.scan.names):
                items.append(("Save playlist links & start", "save"))
            items.append(("Edit settings", "settings"))
            choice = pick("Ready", items)
            if choice is None:
                return
            if choice == "settings":
                settings_menu(settings)
                continue
            save = choice == "save"
            break
    if save:
        count = sync.remember_links(plan, only_playlists=True)
        out.print(f"[green]Saved {count} link(s) to {config.LINKS_FILE}[/]")
    sync.execute(settings, plan, prune_ok=prune_ok, interactive=True)
    pause()


# ---------- settings ----------
def settings_menu(settings: Settings) -> None:
    from rich.live import Live
    from rich.style import Style

    out = ui.console()
    rows = [row for _, group in SETTING_GROUPS for row in group]
    selected = 0

    def view() -> Group:
        width = max(1, out.width - 6)
        content = []
        index = 0
        for group_index, (heading, group_rows) in enumerate(SETTING_GROUPS):
            if group_index:
                content.append(Text())
            content.append(ruled_heading(heading, width))
            for key, label in group_rows:
                is_selected = index == selected
                value, color = _setting_value(settings, key)
                content.append(_settings_row(out, label, value, color, is_selected, width,
                                             dim_label=key in ("_reset", BACK)))
                index += 1

        key, label = rows[selected]
        description = Text()
        description.append(label, style=Style(color=TEXT))
        description.append(" · ", style=Style(color=DIM))
        description.append(truncate(SETTING_DESCRIPTIONS[key], max(0, width - len(label) - 3)),
                           style=Style(color=DIM))
        content.extend((divider(width), description))
        panel = panel_with_title(
            Group(*content),
            "Settings",
            dim=f"Data: {config.DATA_DIR}",
            border=BORDER,
            dim_color=PATH,
        )
        return Group(panel, footer_hint("↑↓ move · ←→ change value · enter edit · esc back"))

    with Live(view(), console=out, auto_refresh=False) as live:
        while True:
            key = read_key()
            if key == "up":
                selected = (selected - 1) % len(rows)
            elif key == "down":
                selected = (selected + 1) % len(rows)
            elif key in ("left", "right"):
                changed = _change_setting(settings, rows[selected][0], -1 if key == "left" else 1)
                if changed:
                    live.update(view(), refresh=True)
                    continue
            elif key == "enter":
                setting_key = rows[selected][0]
                if setting_key == BACK:
                    return
                live.stop()
                _enter_setting(settings, setting_key)
                live.start(refresh=True)
            elif key in ("esc", "ctrl-c"):
                return
            live.update(view(), refresh=True)


def _setting_value(settings: Settings, key: str) -> tuple[str, str]:
    if key == "_library":
        return settings.out or "not set", PATH
    if key == "_data":
        return str(config.DATA_DIR), PATH
    if key in ("_links", "_reset", BACK):
        return "", DIM

    value = getattr(settings, key)
    if isinstance(value, bool):
        return ("on", GREEN) if value else ("off", DIM)
    if key == "name_style":
        return config.NAME_STYLES[value], CYAN
    if key == "rate_limit":
        return (value, CYAN) if value else ("unlimited", DIM)
    if key == "cookies":
        return (value, CYAN) if value else ("none", DIM)
    return str(value), CYAN


def _settings_row(out, label: str, value: str, color: str, selected: bool, width: int,
                  *, dim_label: bool = False):
    from rich.style import Style
    from rich.text import Text

    display_value = f"‹ {value} ›" if selected and value else value
    row_renderable = render_select(
        [SelectItem(label=label, desc=display_value)],
        0 if selected else -1,
        variant="compact",
        label_width=24,
        width=width + 1,
    )
    segments = out.render_lines(row_renderable, out.options.update(width=width + 1), pad=False)[0]
    row = Text("".join(segment.text for segment in segments)[1:])
    label_style = Style(color=ACCENT, bold=True) if selected else Style(color=DIM if dim_label else TEXT)
    row.stylize(label_style, 0, min(24, len(row.plain)))
    if len(row.plain) > 24:
        row.stylize(Style(color=ACCENT if selected else color, bold=selected), 24, len(row.plain))
    return row


def _change_setting(settings: Settings, key: str, direction: int) -> bool:
    if key in ("_library", "_data", "_links", "_reset", BACK, "rate_limit", "cookies"):
        return False
    current = getattr(settings, key)
    if isinstance(current, bool):
        setattr(settings, key, not current)
    elif key in ("jobs", "retries"):
        low, high = (1, config.MAX_JOBS) if key == "jobs" else (0, 20)
        setattr(settings, key, max(low, min(high, current + direction)))
    elif key in CHOICES:
        choices = CHOICES[key]
        setattr(settings, key, choices[(choices.index(current) + direction) % len(choices)])
    else:
        return False
    settings.validated().save()
    return True


def _enter_setting(settings: Settings, key: str) -> None:
    if key == "_library":
        choose_library(settings)
    elif key == "_data":
        data_folder_menu()
    elif key == "_links":
        _ensure_links_file()
        open_path(config.LINKS_FILE)
    elif key == "_reset":
        if confirm_boxes("Reset all settings to defaults?"):
            defaults = Settings(out=settings.out)
            for setting, _ in SETTING_ROWS:
                setattr(settings, setting, getattr(defaults, setting))
            settings.save()
    elif key in ("rate_limit", "cookies"):
        _edit_setting(settings, key)
    elif key not in ("format", "quality", "name_style", "cover", "square", "clean",
                     "playlist_files", "prune", "skip_duplicate_songs", "jobs", "retries"):
        raise ValueError(f"Unknown settings row: {key}")


def _edit_setting(settings: Settings, key: str) -> None:
    label = dict(SETTING_ROWS)[key]
    current = getattr(settings, key)
    if isinstance(current, bool):
        setattr(settings, key, not current)
    elif key in CHOICES:
        items = [(config.NAME_STYLES[v] if key == "name_style" else str(v), v) for v in CHOICES[key]]
        value = pick(label, items)
        if value is None:
            return
        setattr(settings, key, value)
    else:
        hint = {"rate_limit": " (e.g. 2M, empty = unlimited)",
                "cookies": " (chrome, firefox, edge..., empty = none)"}
        value = ask_text(f"{label}{hint.get(key, '')}:", str(current))
        if value is None:
            return
        setattr(settings, key, value)
    settings.validated().save()


def data_folder_menu() -> None:
    while True:
        files = sorted(p for p in config.DATA_DIR.rglob("*") if p.is_file())
        items = [("Open in file explorer", "_open")] + [
            (f"{p.relative_to(config.DATA_DIR)}  ({ui.human_size(p.stat().st_size)})", p) for p in files]
        choice = pick(f"Data folder  {config.DATA_DIR}", items)
        if choice is None:
            return
        open_path(config.DATA_DIR if choice == "_open" else choice)
