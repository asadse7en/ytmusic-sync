"""Offline tests: everything except real YouTube access (yt-dlp is replaced by a fake)."""
import io
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.cells import cell_len
from rich.console import Console

HOME = tempfile.mkdtemp(prefix="ytsync-test-")
os.environ["YTMUSIC_SYNC_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ytmusic_sync import config, downloader, links, planner, progress, sync, tags, ui, ytdlp  # noqa: E402
from ytmusic_sync.library import Library  # noqa: E402
from ytmusic_sync.progress import Tracker  # noqa: E402
from ytmusic_sync.scanner import ScanResult, ScannedPlaylist, scan_links  # noqa: E402

PL = "https://www.youtube.com/playlist?list=PLgym"
PL2 = "https://www.youtube.com/playlist?list=PLchill"
CATALOG = {
    PL: {"title": "Gym", "entries": [
        {"id": "aaaaaaaaaaa", "title": "Artist - Song One (Official Video)", "duration": 200},
        {"id": "bbbbbbbbbbb", "title": "Artist - Song Two [HD]", "duration": 180},
        {"id": "ccccccccccc", "title": "[Private video]", "duration": 0},
        {"id": "fffffffffff", "title": "Broken Song", "duration": 90}]},
    PL2: {"title": "Chill", "entries": [
        {"id": "ddddddddddd", "title": "Artist - Song One (Lyrics)", "duration": 201},  # duplicate of aaaa
        {"id": "bbbbbbbbbbb", "title": "Artist - Song Two [HD]", "duration": 180}]},   # same id twice
}


class FakeYDL:
    def __init__(self, opts):
        self.opts, self.params, self.pps = opts, dict(opts), []

    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def close(self): pass
    def add_post_processor(self, pp, when=None): self.pps.append(pp)

    def extract_info(self, url, download=False):
        if self.opts.get("extract_flat"):
            if self.params.get("playlist_items") == "1":
                if url not in CATALOG:
                    raise ValueError("ERROR: [youtube] zzz: Video unavailable")
                return {"playlist_count": len(CATALOG[url]["entries"])}
            if url not in CATALOG:
                raise ValueError("ERROR: [youtube] zzz: Video unavailable")
            return CATALOG[url]
        vid = url.split("v=")[1]
        if vid == "fffffffffff":
            raise ValueError("ERROR: [youtube] fff: This video is not available")
        titles = {"aaaaaaaaaaa": "Artist - Song One (Official Video)", "bbbbbbbbbbb": "Artist - Song Two [HD]"}
        info = {"id": vid, "title": titles[vid], "duration": 200, "channel": "Someone - Topic"}
        for pp in self.pps:
            if type(pp).__name__ == "CleanTags":
                info = pp.run(info)[1]
        for hook in self.opts["progress_hooks"]:
            hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100, "speed": 10})
        path = Path(self.opts["outtmpl"]).parent / f"{info['fname']}.mp3"
        path.write_bytes(b"x" * 1000)
        info["requested_downloads"] = [{"filepath": str(path)}]
        return info


def install_fake():
    fake = types.SimpleNamespace(YoutubeDL=FakeYDL, utils=types.SimpleNamespace(parse_bytes=lambda s: 1))
    ytdlp.module = lambda: fake
    ytdlp.ffmpeg_path = lambda: "/usr/bin/true"


class TagTests(unittest.TestCase):
    def test_clean_title_and_artist(self):
        self.assertEqual(tags.clean_tags({"id": "x", "title": "Artist - Song (Official Video) 🔥"}),
                         ("Artist", "Song"))

    def test_channel_fallback_strips_topic(self):
        self.assertEqual(tags.clean_tags({"id": "x", "title": "Song", "channel": "Band - Topic"}), ("Band", "Song"))

    def test_youtube_music_metadata_wins(self):
        self.assertEqual(tags.clean_tags({"id": "x", "title": "junk", "track": "Real", "artist": "Me"}), ("Me", "Real"))

    def test_safe_name(self):
        self.assertEqual(tags.safe_name('a/b:c*d?'), "abcd")
        self.assertEqual(tags.safe_name("CON"), "CON_")
        self.assertEqual(tags.safe_name("..."), "track")


class LinkTests(unittest.TestCase):
    def test_validation_and_dedupe(self):
        valid, bad = links.parse_pasted("https://music.youtube.com/playlist?list=PL1 https://youtu.be/abcdefghijk "
                                        "nonsense https://example.com/x https://www.youtube.com/playlist?list=PL1")
        self.assertEqual(len(valid), 2)
        self.assertEqual(bad, ["nonsense", "https://example.com/x"])

    def test_normalization_and_query_independent_identity(self):
        playlist = "https://music.youtube.com/playlist?list=PL1&si=first"
        same_playlist = "https://www.youtube.com/playlist?list=PL1&si=second"
        video = "https://www.youtube.com/watch?v=abcdefghijk&si=first"
        same_video = "https://youtu.be/abcdefghijk?si=second"

        self.assertEqual(
            links.normalize_url(playlist),
            "https://www.youtube.com/playlist?list=PL1&si=first",
        )
        self.assertEqual(links.link_key(playlist), links.link_key(same_playlist))
        self.assertEqual(links.link_key(video), links.link_key(same_video))
        self.assertEqual(
            links.dedupe([playlist, same_playlist, video, same_video]),
            [links.canonical_url(playlist), video],
        )
        valid, invalid = links.parse_pasted("https://example.com/watch?v=abcdefghijk " + video)
        self.assertEqual(valid, [video])
        self.assertEqual(invalid, ["https://example.com/watch?v=abcdefghijk"])

    def test_save_links_no_duplicates_and_names(self):
        config.LINKS_FILE.unlink(missing_ok=True)
        self.assertEqual(links.save_links([(PL, "Gym"), (PL, "Gym again")]), 1)
        self.assertEqual(links.save_links([("https://music.youtube.com/playlist?list=PLgym", "x")]), 0)
        self.assertIn("# Gym\n" + PL, config.LINKS_FILE.read_text())
        self.assertEqual(links.read_links(), [PL])


class ConfigTests(unittest.TestCase):
    def test_validation(self):
        s = config.Settings(format="bogus", jobs=99, retries="x", cookies=" Chrome ").validated()
        self.assertEqual((s.format, s.jobs, s.retries, s.cookies), ("mp3", 6, 5, "chrome"))

    def test_roundtrip_and_unknown_keys(self):
        config.ensure_data_dir()
        config.CONFIG_FILE.write_text('{"format": "flac", "urls": ["old"], "files": []}')
        self.assertEqual(config.Settings.load().format, "flac")


class EndToEnd(unittest.TestCase):
    def setUp(self):
        install_fake()
        self.music = Path(tempfile.mkdtemp(prefix="music-"))
        (self.music / "Song One.mp3").write_bytes(b"existing user file")  # name collision on purpose
        self.settings = config.Settings(out=str(self.music), retries=1, jobs=2).validated()

    def test_full_run(self):
        plan = sync.prepare(self.settings, [PL, PL2, "https://www.youtube.com/playlist?list=PLnope"])
        self.assertEqual(len(plan.errors), 1)
        self.assertEqual(plan.scan.unavailable, 1)
        self.assertEqual(plan.duplicates, 1)  # dddd is a duplicate of aaaa
        self.assertEqual(sorted(e["id"] for e in plan.todo), ["aaaaaaaaaaa", "bbbbbbbbbbb", "fffffffffff"])

        result = sync.execute(self.settings, plan, compact=True)
        self.assertEqual((result.new, len(result.failures)), (2, 1))
        self.assertEqual(result.failures[0].reason, "[youtube] fff: This video is not available")

        files = sorted(p.name for p in self.music.glob("*.mp3"))
        self.assertIn("Song One.mp3", files)                      # user's file untouched
        self.assertIn("Song One - Artist.mp3", files)             # collision -> "Title - Artist" fallback
        self.assertIn("Song Two.mp3", files)
        gym = (self.music / "Gym.m3u8").read_text().splitlines()
        self.assertEqual(gym[0], "#EXTM3U")
        self.assertEqual([l for l in gym if l.endswith(".mp3")], ["Song One - Artist.mp3", "Song Two.mp3"])
        chill = [l for l in (self.music / "Chill.m3u8").read_text().splitlines() if l.endswith(".mp3")]
        self.assertEqual(chill, ["Song One - Artist.mp3", "Song Two.mp3"])   # duplicate maps to the kept song
        self.assertTrue(config.FAILED_FILE.exists())
        self.assertEqual(sorted(p.name for p in self.music.iterdir() if p.suffix in (".part", ".jpg")), [])

        # second run: nothing new to download, playlists rewritten, stats counted
        plan2 = sync.prepare(self.settings, [PL, PL2])
        self.assertEqual([e["id"] for e in plan2.todo], ["fffffffffff"])  # only the failed one is retried
        sync.execute(self.settings, plan2, compact=True)
        lib = Library(self.music)                                   # reload from disk
        self.assertEqual(len(lib.songs), 2)
        self.assertEqual(lib.stats["runs"], 2)
        self.assertEqual(lib.stats["downloads"], 2)

    def test_dashboard_dry_run_and_prune(self):
        plan = sync.prepare(self.settings, [PL])
        sync.execute(self.settings, plan, compact=False)          # Live dashboard path
        lib = Library(self.music)
        lib.remove_songs(["bbbbbbbbbbb"])
        self.assertFalse((self.music / "Song Two.mp3").exists())
        self.assertNotIn("bbbbbbbbbbb", lib.playlists["Gym"])

    def test_cancelled_run_downloads_nothing(self):
        lib = Library(self.music)
        stop = threading.Event()
        stop.set()
        failures = downloader.run_downloads([{"id": "aaaaaaaaaaa", "title": "x", "dur": 1}], lib, self.settings,
                                            Tracker(1, 1), stop)
        self.assertEqual(failures, [])
        self.assertEqual(len(lib.songs), 0)


class LibraryTests(unittest.TestCase):
    def test_name_reservation_is_unique_under_threads(self):
        music = Path(tempfile.mkdtemp())
        lib = Library(music)
        names = []
        def work(i): names.append(lib.reserve_name(f"v{i}", "A", "Same Title", "title"))
        threads = [threading.Thread(target=work, args=(i,)) for i in range(20)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(len({n.lower() for n in names}), 20)

    def test_missing_files_are_dropped_on_load(self):
        music = Path(tempfile.mkdtemp())
        lib = Library(music)
        (music / "a.mp3").write_bytes(b"1")
        lib.add_song("v1", {"file": "a.mp3", "artist": "x", "title": "a", "dur": 1})
        lib.add_song("v2", {"file": "gone.mp3", "artist": "x", "title": "g", "dur": 1})
        lib.save(force=True)
        self.assertEqual(list(Library(music).songs), ["v1"])


class UiTests(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(ui.clock(3725), "1:02:05")
        self.assertEqual(ui.human_size(1536), "1.5 KB")


class ProgressTests(unittest.TestCase):
    def test_eta_waits_for_five_seconds(self):
        with patch.object(progress.time, "monotonic", side_effect=[100.0, 104.0, 105.0]):
            tracker = Tracker(3, 1)
            tracker.done = 1
            self.assertIsNone(tracker.snapshot()["eta"])
            self.assertEqual(tracker.snapshot()["eta"], 10.0)

    def test_speed_samples_are_bounded_to_fifteen(self):
        with patch.object(progress.time, "monotonic", side_effect=[100.0, 120.0]):
            tracker = Tracker(2, 1)
            self.assertEqual(len(tracker.snapshot()["speed_samples"]), 15)


class ResultAggregationTests(unittest.TestCase):
    def test_run_results_attribute_playlists_and_count_unique_downloads(self):
        with tempfile.TemporaryDirectory(prefix="aggregation-") as directory:
            music = Path(directory)
            lib = Library(music)
            successful = [
                {"id": f"done{i:02}", "title": f"Song {i}", "dur": 180}
                for i in range(35)
            ]
            failed = [
                {"id": f"failed{i:02}", "title": f"Failed {i}", "dur": 180}
                for i in range(35)
            ]
            for entry in successful:
                filename = f"{entry['id']}.mp3"
                (music / filename).write_bytes(b"audio")
                lib.add_song(entry["id"], {
                    "file": filename, "artist": "Artist", "title": entry["title"], "dur": entry["dur"],
                })

            playlists = [
                ScannedPlaylist("Gym", PL, successful + failed),
                ScannedPlaylist("Chill", PL2, successful[:1]),
            ]
            plan = planner.Plan(lib, ScanResult(playlists=playlists))
            results = [
                downloader.TrackResult(
                    entry["id"], entry["title"], ("PLgym",) + (("PLchill",) if index == 0 else ()),
                    "done", bytes=5,
                )
                for index, entry in enumerate(successful)
            ]
            results.extend(
                downloader.TrackResult(entry["id"], entry["title"], ("PLgym",), "failed", reason="unavailable")
                for entry in failed
            )
            result = sync.RunResult(
                track_results=results,
                failures=[downloader.Failure(entry["title"], "Artist", "unavailable", entry["id"], ("PLgym",))
                          for entry in failed],
            )
            sync._set_result_totals(result)
            rows = sync._finish_run(
                config.Settings(out=str(music), playlist_files=False).validated(),
                plan,
                result,
                time.monotonic(),
            )

            by_id = {row["id"]: row for row in rows}
            self.assertEqual(result.new, 35)
            self.assertEqual(by_id["PLgym"]["added"], 35)
            self.assertEqual(by_id["PLgym"]["failed"], 35)
            self.assertEqual(by_id["PLchill"]["added"], 1)
            self.assertEqual(lib.stats["downloads"], 35)
            self.assertEqual(lib.stats["runs"], 1)


class DashboardViewTests(unittest.TestCase):
    def test_failures_view_replaces_downloads_and_scrolls_by_page(self):
        tracker = Tracker(20, 2)
        dashboard = ui.Dashboard(tracker)
        failures = [
            downloader.Failure(f"Track {index}", "Artist", "Video unavailable", f"video{index:07}")
            for index in range(12)
        ]
        dashboard.update_failures(failures)

        def render(height):
            output = Console(
                width=80,
                height=height,
                file=io.StringIO(),
                force_terminal=True,
                record=True,
                color_system="truecolor",
            )
            output.print(dashboard)
            text = output.export_text(styles=False)
            self.assertTrue(all(cell_len(line) <= 80 for line in text.splitlines()))
            self.assertLessEqual(len(text.splitlines()), height)
            return text

        downloads = render(40)
        self.assertIn("Downloading", downloads)
        self.assertIn("f failures (0) · q stop safely · Ctrl+C force quit", downloads)
        dashboard.handle_view_key("f", failures, 16)
        first_page = render(16)
        self.assertIn("Failures", first_page)
        self.assertIn("Video unavailable", first_page)
        self.assertIn("f back · ↑↓ scroll · q stop safely · Ctrl+C force quit", first_page)
        self.assertIn("Track 0", first_page)
        self.assertNotIn("Downloading", first_page)
        self.assertNotIn("Workers", first_page)

        dashboard.handle_view_key("pagedown", failures, 16)
        second_page = render(16)
        self.assertIn("Track 5", second_page)
        self.assertNotIn("Track 0", second_page)
        dashboard.handle_view_key("pageup", failures, 16)
        self.assertIn("Track 0", render(16))

        dashboard.handle_view_key("f", failures, 16)
        downloads = render(40)
        self.assertIn("Downloading", downloads)
        self.assertIn("Workers", downloads)
        self.assertNotIn("Failures", downloads)
        dashboard.handle_view_key("f", failures, 16)

        dashboard.handle_view_key("esc", failures, 16)
        downloads = render(40)
        self.assertIn("Downloading", downloads)
        self.assertIn("Workers", downloads)
        self.assertNotIn("Failures", downloads)


class RenderSmokeTests(unittest.TestCase):
    def test_all_screens_render_at_supported_widths(self):
        from render_smoke import run_render_smoke

        run_render_smoke()


if __name__ == "__main__":
    unittest.main()
