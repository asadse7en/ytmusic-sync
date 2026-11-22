"""Render every redesigned screen at several terminal widths without a live terminal."""
from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ytmusic_sync import config, menus, ui
from ytmusic_sync.downloader import Failure
from ytmusic_sync.progress import ScanProgress, Tracker
from ytmusic_sync.sync import RunResult
from ytmusic_sync.theme import BORDER
from ytmusic_sync.widgets import SelectItem, _confirm_view, panel_with_title, render_select


class CapturingConsole(Console):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.renderables = []

    def print(self, *objects, **kwargs):
        self.renderables.extend(objects)


class CapturingLive:
    def __init__(self, renderable, captures, **kwargs):
        self.captures = captures
        self.captures.append(renderable)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def update(self, renderable, **kwargs):
        self.captures.append(renderable)

    def stop(self):
        pass

    def start(self, **kwargs):
        pass


class FakeLibrary:
    def __init__(self, path: Path):
        self.out = path
        self.songs = {
            "abcdefghijk": {
                "file": "existing-song.mp3",
                "artist": "Artist",
                "title": "A song with a deliberately long title for narrow terminals",
                "dur": 180,
            },
        }
        self.aliases = {}
        self.playlists = {}
        self.stats = {
            "downloads": 42,
            "runs": 7,
            "last_run": "2024-01-01 12:00",
            "new": 1,
            "failed": 1,
            "duration_seconds": 9.4,
            "last_playlists": [],
        }

    def canonical(self, video_id: str) -> str:
        return self.aliases.get(video_id, video_id)

    def total_size(self) -> int:
        return 1024


def _render(renderable, width: int) -> str:
    stream = io.StringIO()
    console = Console(
        width=width,
        height=60,
        file=stream,
        force_terminal=True,
        record=True,
        color_system="truecolor",
        soft_wrap=False,
    )
    console.print(renderable)
    return console.export_text(styles=False)


def _build_screens(width: int, music: Path):
    settings = config.Settings(out=str(music)).validated()
    lib = FakeLibrary(music)
    long_name = "A deliberately long playlist title that must be ellipsized without wrapping"
    lib.playlists = {long_name: ["abcdefghijk"]}
    lib.stats["last_playlists"] = [
        {"id": "PL1", "name": long_name, "songs": 95, "new": 7, "failed": 1},
        {"id": "PL2", "name": "Second playlist", "songs": 20, "new": 0, "failed": 0},
        {"id": "PL3", "name": "Third playlist", "songs": 3, "new": 0, "failed": 0},
    ]
    output = CapturingConsole(
        width=width,
        height=60,
        file=io.StringIO(),
        force_terminal=True,
        record=True,
        color_system="truecolor",
        soft_wrap=False,
    )
    live_renders = []
    live_type = type("TestLive", (CapturingLive,), {
        "__init__": lambda self, renderable, **kwargs: CapturingLive.__init__(
            self, renderable, live_renders, **kwargs
        ),
    })

    def capture_select(view_factory, items, start=0, **kwargs):
        live_renders.append(view_factory(start))
        return None

    with patch.object(ui, "console", return_value=output), \
        patch.object(ui, "open_library", return_value=lib), \
        patch.object(config.Settings, "load", return_value=settings), \
        patch.object(menus, "run_select", side_effect=capture_select), \
        patch.object(menus, "read_key", return_value="esc"), \
        patch.object(menus, "drain_pending", return_value=""), \
        patch.object(ui.keys, "read_key", return_value="esc"), \
        patch("rich.live.Live", live_type):
        screens = [("main menu header", ui.build_header())]

        menus.add_music(settings)
        screens.append(("Add Music", live_renders.pop()))

        menus.settings_menu(settings)
        screens.append(("Settings", live_renders.pop()))

        menus.paste_links(settings)
        screens.append(("Paste links", live_renders.pop()))

        scan = ScanProgress(
            ["https://www.youtube.com/playlist?list=PL1"],
            ["PL1"],
            library_before=1,
        )
        scan.finish(0, "Workout Mix", 15)
        scan.update_plan(1, {"https://www.youtube.com/playlist?list=PL1": 1}, 1, 2, ["Newest song"])
        screens.append(("scan", ui.ScanPanel(scan)))

        tracker = Tracker(3, 2)
        tracker.start(0, "A long song title")
        tracker.set_track(0, "A long song title", "Artist")
        tracker.update(0, {
            "status": "downloading",
            "downloaded_bytes": 50,
            "total_bytes": 100,
            "speed": 2048,
        })
        tracker.set_queue(["Next song"])
        screens.append(("downloading", ui.build_download_view(tracker, width, 60)))

        stopping = ui.StoppingProgress()
        stopping.set_step(0, "done")
        stopping.set_step(1, "done")
        stopping.set_step(2, "running")
        screens.append(("quitting", ui.StoppingView(stopping)))

        failure = Failure("Unavailable song", "Artist", "Video unavailable", "abcdefghijk")
        output.renderables.clear()
        ui.print_run_summary(
            RunResult(
                new=1,
                bytes=1024,
                elapsed=7.5,
                failures=[failure],
                songs_before=1,
                songs_after=2,
                size_before=1024,
                size_after=2048,
            ),
            lib,
            [{"id": "PL1", "name": long_name, "songs": 95, "added": 1, "failed": 1, "removed": 0}],
            1,
            show_removed=False,
        )
        screens.append(("results", output.renderables.pop()))

        ui.show_failures([failure])
        screens.append(("failures", live_renders.pop()))

        output.renderables.clear()
        ui.print_stats(lib)
        screens.append(("stats", output.renderables.pop()))

        screens.append((
            "confirm boxes",
            _confirm_view(Text("Reset all settings to defaults?"), None, True, output),
        ))

    screens.append((
        "Add Music selector",
        panel_with_title(
            render_select(
                [SelectItem("Paste links", "Playlists or songs"), SelectItem("Back")],
                0,
                variant="boxed",
                width=width - 4,
            ),
            "Add Music",
            border=BORDER,
        ),
    ))
    return screens


def run_render_smoke() -> None:
    forbidden = ("[Y/n]", "(yes/no)", "»", "Link #", "up to date", "cached")
    with tempfile.TemporaryDirectory(prefix="render-smoke-") as directory:
        music = Path(directory)
        music.mkdir(exist_ok=True)
        for width in (80, 100, 120, 200):
            screens = _build_screens(width, music)
            for name, renderable in screens:
                plain = _render(renderable, width)
                for line in plain.splitlines():
                    assert cell_len(line) <= width, (
                        f"{name} produced a {cell_len(line)}-cell line at width {width}: {line!r}"
                    )
                lowered = plain.casefold()
                for value in forbidden:
                    assert value.casefold() not in lowered, f"{name} contains forbidden text {value!r}"
                for line in plain.splitlines():
                    title_start = line.find("╭─ ")
                    if title_start >= 0:
                        assert cell_len(line[:title_start]) + 3 <= 8, (
                            f"{name} has a centered titled panel: {line!r}"
                        )

            items = [SelectItem("Chosen item"), SelectItem("Other item")]
            selected = _render(render_select(items, 0, "compact", width=width - 2), width)
            unselected = _render(render_select(items, 1, "compact", width=width - 2), width)
            selected_line = next(line for line in selected.splitlines() if "Chosen item" in line)
            unselected_line = next(line for line in unselected.splitlines() if "Chosen item" in line)
            selected_column = cell_len(selected_line[:selected_line.index("Chosen item")])
            unselected_column = cell_len(unselected_line[:unselected_line.index("Chosen item")])
            assert selected_column == unselected_column, (
                f"selector label column changed at width {width}"
            )


if __name__ == "__main__":
    run_render_smoke()
