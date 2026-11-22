"""Command line entry point. Heavy imports (yt-dlp and Rich UI) happen only when needed."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, config
from .theme import init_terminal

EXAMPLES = """examples:
  ytmusic-sync                     interactive menu
  ytmusic-sync -u                  update library (saved links + settings)
  ytmusic-sync <url> --save        download a link and remember it
  ytmusic-sync <url> -f flac       one-off format
  ytmusic-sync -p links.txt        use a text file of links
  ytmusic-sync -s                  library stats
"""


class _Version(argparse.Action):
    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        from . import ui, ytdlp

        ui.print_notice(f"ytmusic-sync {__version__} (yt-dlp {ytdlp.installed_version()})")
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ytmusic-sync", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EXAMPLES,
        description="Download YouTube songs/playlists with clean tags and keep .m3u8 playlists in sync.\n"
                    "Run with no arguments for the interactive menu.")
    ap.add_argument("urls", nargs="*", help="playlist or song links (this run only)")
    run = ap.add_argument_group("run")
    run.add_argument("-u", "--update", action="store_true", help="sync saved links with saved settings, no menus")
    run.add_argument("-p", "--playlists", metavar="FILE", help="text file with one link per line")
    run.add_argument("--save", action="store_true", help="remember the given links for future --update runs")
    run.add_argument("-d", "--dry-run", action="store_true", help="show what would download")
    run.add_argument("-s", "--stats", action="store_true", help="show library stats")
    run.add_argument("-o", "--out", "--library", metavar="DIR", help="music library folder (remembered)")
    out = ap.add_argument_group("output (this run only, not saved)")
    out.add_argument("-f", "--format", choices=config.FORMATS)
    out.add_argument("-q", "--quality", choices=config.QUALITIES, help="kbps for lossy formats")
    out.add_argument("-n", "--name-style", dest="name_style", choices=list(config.NAME_STYLES))
    out.add_argument("--no-cover", action="store_true", help="skip cover art")
    out.add_argument("--no-playlist-files", action="store_true", help="do not write .m3u8 files")
    out.add_argument("--prune", action="store_true", help="with -u: delete songs no longer in any playlist (asks first)")
    net = ap.add_argument_group("network")
    net.add_argument("--cookies-from-browser", dest="cookies", metavar="NAME", help="chrome, firefox, edge... (private playlists)")
    net.add_argument("--rate-limit", dest="rate_limit", metavar="RATE", help="for example 2M")
    net.add_argument("--retries", type=int, metavar="N")
    net.add_argument("--jobs", type=int, choices=range(1, config.MAX_JOBS + 1), metavar="1-6", help="parallel downloads")
    other = ap.add_argument_group("other")
    other.add_argument("--compact", action="store_true", help="one line per song instead of the dashboard (for logs)")
    other.add_argument("--where", action="store_true", help="print the data folder and library folder")
    other.add_argument("--update-ytdlp", action="store_true", help="upgrade yt-dlp and exit")
    other.add_argument("-V", "--version", action=_Version, help="show versions and exit")
    return ap


def main(argv: list[str] | None = None) -> int:
    init_terminal()
    parser = build_parser()
    args = parser.parse_args(argv)
    direct = bool(args.urls or args.playlists or args.update or args.stats or args.dry_run
                  or args.where or args.update_ytdlp)
    if not direct and not sys.stdin.isatty():
        parser.print_help()
        return 0
    try:
        return _run(args, direct)
    except KeyboardInterrupt:
        from . import ui

        ui.print_notice("Cancelled.")
        return 130
    except OSError as exc:
        from . import ui

        if exc.filename:
            ui.print_path_error("Could not access the music or data folder: ",
                                str(exc.filename),
                                "Check that the music folder is connected and writable.")
        else:
            ui.print_error("Could not access the music or data folder.",
                           "Check that the music folder is connected and writable.")
        return 1
    except RuntimeError as exc:
        from . import ui

        if "yt-dlp is not installed" in str(exc):
            ui.print_missing_dependency("yt-dlp", "Install yt-dlp with: pip install yt-dlp")
        else:
            ui.print_error(str(exc), "See the sync log for details.")
        return 1


def _run(args, direct: bool) -> int:
    config.ensure_data_dir()
    if args.update_ytdlp:
        from . import ui, ytdlp

        ok = ytdlp.upgrade()
        if ok:
            ui.print_success("yt-dlp updated.")
        else:
            ui.print_error("Could not update yt-dlp.", "Try: pip install -U yt-dlp")
        return 0 if ok else 1

    settings = config.Settings.load()
    if args.out:
        settings.out = str(Path(args.out).expanduser())
        settings.validated()
        if _ensure_folder(settings.out_path):
            settings.save()
    if args.where:
        from . import ui

        ui.print_path_message("Data folder:    ", str(config.DATA_DIR))
        ui.print_path_message("Library folder: ", settings.out or "(not selected)")
        return 0

    if not direct:
        from . import menus

        return menus.run(settings)

    # one-off overrides (never saved)
    for key in ("format", "quality", "name_style", "cookies", "rate_limit", "retries", "jobs"):
        if getattr(args, key) is not None:
            setattr(settings, key, getattr(args, key))
    settings.cover = settings.cover and not args.no_cover
    settings.playlist_files = settings.playlist_files and not args.no_playlist_files
    settings.prune = settings.prune or args.prune
    settings.validated()

    out = settings.out_path
    if out is None or not out.is_dir():
        from . import ui

        ui.print_path_error("No usable music folder: ", settings.out or "none selected",
                            "Run ytmusic-sync once to pick one, or pass -o FOLDER.")
        return 1

    from . import sync, ui, ytdlp
    from .library import open_library

    sync.setup_logging()
    if args.stats:
        ui.print_stats(open_library(out))
        return 0

    from . import links

    urls = list(args.urls)
    if args.playlists:
        urls += links.read_links(Path(args.playlists))
    if args.update:
        urls += links.read_links()
    urls = links.dedupe(urls)
    if not urls:
        ui.print_path_message("No links yet. Add links to ", str(config.LINKS_FILE), " or pass a URL.")
        return 0

    ytdlp.check_for_updates_in_background()
    plan = sync.prepare(settings, urls)
    if args.dry_run:
        ui.print_plan(plan, dry_run=True)
        return 0
    if args.save:
        sync.remember_links(plan)
    result = sync.execute(settings, plan, compact=args.compact, prune_ok=args.update)
    return 1 if result is None else 0


def _ensure_folder(path: Path | None) -> bool:
    if path is None:
        return False
    if path.is_dir():
        return True
    if path.parent.is_dir():
        path.mkdir()
        return True
    raise OSError(f"Music folder is unavailable: {path}")


if __name__ == "__main__":
    sys.exit(main())
