"""Interactive arrow-key menus. Every menu has "‹ Back"; Ctrl+C goes back (or quits from home)."""
from __future__ import annotations

import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from . import __version__, config, links, sync, ui, ytdlp
from .config import Settings
from .library import open_library, time_ago
from .scanner import short_error

BACK = "__back__"
SETTING_ROWS = [
    ("format", "Format"), ("quality", "Quality"), ("name_style", "Filename style"), ("cover", "Cover art"),
    ("square", "Square cover"), ("clean", "Clean titles"), ("playlist_files", "Playlist .m3u8 files"),
    ("prune", "Prune removed songs"), ("skip_duplicate_songs", "Skip duplicate songs"),
    ("jobs", "Parallel downloads"), ("rate_limit", "Speed limit"), ("retries", "Retries"),
    ("cookies", "Browser cookies"),
]
CHOICES = {"format": config.FORMATS, "quality": config.QUALITIES, "name_style": tuple(config.NAME_STYLES),
           "jobs": tuple(range(1, config.MAX_JOBS + 1))}


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
def header(settings: Settings) -> None:
    out = ui.console()
    lib = open_library(settings.out_path)
    out.print(f"\n[bold {ui.ACCENT}]ytmusic-sync[/] [dim]{__version__}[/]")
    out.print(f"[dim]{settings.out}  ·  {len(lib.songs)} songs  ·  synced {time_ago(lib.stats['last_run'])}[/]")
    ui.print_update_hint()


def home(settings: Settings) -> None:
    while True:
        header(settings)
        items = [("Update library", "update"), ("Add Music", "add"), ("Settings", "settings"), ("Stats", "stats")]
        if ytdlp.update_available():
            items.append(("Update yt-dlp", "ytdlp"))
        items.append(("Quit", "quit"))
        choice = pick("What now?", items, back=False)
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
        choice = pick("Add Music", [("Paste links", "paste"), (f"Load from Playlists.txt  ({saved} links)", "load"),
                                    ("Edit Playlists.txt", "edit")])
        if choice is None:
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
def _show(settings: Settings, key: str) -> str:
    value = getattr(settings, key)
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value) if value not in ("", None) else "off" if key in ("rate_limit", "cookies") else "-"


def settings_menu(settings: Settings) -> None:
    while True:
        items = [(f"{label:<22}{_show(settings, key)}", key) for key, label in SETTING_ROWS]
        items += [(f"{'Library folder':<22}{settings.out}", "_library"), ("Data folder", "_data"),
                  ("Edit Playlists.txt", "_links"), ("Reset to defaults", "_reset")]
        choice = pick("Settings", items)
        if choice is None:
            return
        if choice == "_library":
            choose_library(settings)
        elif choice == "_data":
            data_folder_menu()
        elif choice == "_links":
            _ensure_links_file()
            open_path(config.LINKS_FILE)
        elif choice == "_reset":
            defaults = Settings(out=settings.out)
            for key, _ in SETTING_ROWS:
                setattr(settings, key, getattr(defaults, key))
            settings.save()
        else:
            _edit_setting(settings, choice)


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
        hint = {"rate_limit": " (e.g. 2M, empty = off)", "cookies": " (chrome, firefox, edge..., empty = off)"}
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
