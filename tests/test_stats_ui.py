import io
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from ytmusic_sync import ui


class FakeLibrary:
    def __init__(self, with_songs=True):
        self.out = Path("music")
        self.songs = {}
        self.aliases = {}
        self.playlists = {}
        self.stats = {
            "downloads": 42,
            "runs": 7,
            "last_run": "2024-01-01 12:00",
            "new": 1,
            "failed": 0,
            "duration_seconds": 9.4,
        }
        if with_songs:
            self.songs = {
                f"song-{index:02}": {
                    "artist": f"Artist {index:02}, Guest & Other",
                    "dur": 60,
                }
                for index in range(15)
            }
            self.aliases = {"alias-00": "song-00"}
            self.playlists = {
                "Playlist 00": ["song-00", "alias-00", "song-01"],
                **{
                    f"Playlist {index:02}": [f"song-{index:02}"]
                    for index in range(1, 12)
                },
            }

    def canonical(self, video_id):
        return self.aliases.get(video_id, video_id)

    def total_size(self):
        return 1024


def _render(renderable, width=120):
    output = io.StringIO()
    console = Console(
        width=width,
        height=60,
        file=output,
        force_terminal=True,
        record=True,
        color_system="truecolor",
        soft_wrap=False,
    )
    console.print(renderable)
    return console.export_text(styles=False)


class CapturingLive:
    instances = []

    def __init__(self, renderable, **kwargs):
        self.renders = [renderable]
        self.kwargs = kwargs
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def update(self, renderable, **kwargs):
        self.renders.append(renderable)


class StatsScreenTests(unittest.TestCase):
    def setUp(self):
        CapturingLive.instances.clear()

    def test_stats_uses_canonical_playlist_memberships_and_remaining_totals(self):
        library = FakeLibrary()
        output = Console(
            width=120,
            height=60,
            file=io.StringIO(),
            force_terminal=True,
            record=True,
            color_system="truecolor",
        )
        share_calls = []
        share_column = ui._share_column

        def capture_share(*args):
            share_calls.append(args)
            return share_column(*args)

        with patch.object(ui, "console", return_value=output), \
             patch.object(ui, "_share_column", side_effect=capture_share):
            ui.print_stats(library)

        playlist = next(call for call in share_calls if call[0] == "Top playlists")
        artist = next(call for call in share_calls if call[0] == "Top artists")
        self.assertEqual(playlist[1], "share of all playlist songs")
        self.assertEqual(playlist[4:7], (13, "2 playlists", 2))
        self.assertEqual(playlist[2][0], ("Playlist 00", 2))
        self.assertEqual(artist[4:7], (15, "5 artists", 5))
        self.assertEqual(len(playlist[2]), 10)
        self.assertEqual(len(artist[2]), 10)

        rendered = output.export_text(styles=False)
        self.assertIn("Other · 2 playlists", rendered)
        self.assertIn("Other · 5 artists", rendered)
        self.assertNotIn("%", rendered)

    def test_empty_library_shows_empty_columns_without_bars(self):
        library = FakeLibrary(with_songs=False)
        output = Console(
            width=120,
            height=60,
            file=io.StringIO(),
            force_terminal=True,
            record=True,
            color_system="truecolor",
        )
        with patch.object(ui, "console", return_value=output):
            ui.print_stats(library)

        rendered = output.export_text(styles=False)
        self.assertEqual(rendered.count("Nothing to show yet"), 2)
        self.assertNotIn("━", rendered)

    def test_interactive_stats_scrolls_and_shows_cutoff_markers(self):
        library = FakeLibrary()
        output = Console(
            width=100,
            height=14,
            file=io.StringIO(),
            force_terminal=True,
            color_system="truecolor",
        )
        with patch.object(ui, "console", return_value=output), \
             patch.object(ui.keys, "read_key", side_effect=["pagedown", "pageup", "esc"]), \
             patch("rich.live.Live", CapturingLive):
            ui.print_stats(library, interactive=True)

        live = CapturingLive.instances[-1]
        self.assertTrue(live.kwargs["screen"])
        self.assertEqual(len(live.renders), 3)
        initial = _render(live.renders[0], width=100)
        paged_down = _render(live.renders[1], width=100)
        paged_up = _render(live.renders[2], width=100)
        self.assertIn("↓ more", initial)
        self.assertIn("↑ more / ↓ more", paged_down)
        self.assertIn("↓ more", paged_up)
        self.assertNotIn("↑ more", paged_up)


if __name__ == "__main__":
    unittest.main()
