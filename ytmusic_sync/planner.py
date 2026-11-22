"""Turn scan results into a download plan: what is new, what is a duplicate."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import links
from .library import Library
from .scanner import ScanResult, ScannedPlaylist
from .tags import song_key

DUPLICATE_SECONDS = 3  # same cleaned title and length within 3 s = same song under another video ID


@dataclass
class Plan:
    lib: Library
    scan: ScanResult
    wanted: dict = field(default_factory=dict)      # canonical id -> entry, every song the links contain
    todo: list = field(default_factory=list)        # entries not in the library yet
    entries: int = 0                                # songs across all links, counting repeats
    duplicates: int = 0                             # video IDs mapped onto an existing/earlier song
    skipped: list[dict] = field(default_factory=list)

    @property
    def playlists(self) -> list[ScannedPlaylist]:
        return self.scan.playlists

    @property
    def errors(self):
        return self.scan.errors


def make_plan(lib: Library, scan: ScanResult, skip_duplicates: bool = True) -> Plan:
    entries = [e for p in scan.playlists for e in p.items] + scan.singles
    unique: dict[str, dict] = {}
    for entry in entries:
        unique.setdefault(entry["id"], entry)

    with lib.lock:
        by_title: dict[str, list[tuple[int, str]]] = {}
        for vid, song in lib.songs.items():
            key, seconds = song_key(song.get("title", "")), int(song.get("dur") or 0)
            if key and seconds > 0:
                by_title.setdefault(key, []).append((seconds, vid))

        fresh: dict[str, dict] = {}
        for vid, entry in unique.items():
            if vid in lib.aliases:
                continue
            key, seconds = song_key(entry["title"]), entry["dur"]
            match = None
            if skip_duplicates and key and seconds > 0:
                match = next((kept for s, kept in by_title.get(key, [])
                              if kept != vid and abs(seconds - s) <= DUPLICATE_SECONDS), None)
            if match:
                lib.aliases[vid] = match
                continue
            fresh[vid] = entry
            if key and seconds > 0:
                by_title.setdefault(key, []).append((seconds, vid))

        wanted: dict[str, dict] = {}
        for vid, entry in unique.items():
            kept = lib.canonical(vid)
            if kept in fresh:
                wanted.setdefault(kept, fresh[kept])
            elif kept in lib.songs:
                song = lib.songs[kept]
                wanted.setdefault(kept, {"id": kept, "title": song.get("title", entry["title"]),
                                         "dur": int(song.get("dur") or entry["dur"] or 0)})
            else:
                wanted.setdefault(vid, entry)

        todo = [dict(e) for vid, e in wanted.items() if vid not in lib.songs]
        duplicates = sum(1 for vid in unique if vid in lib.aliases)
        skipped = [dict(id=vid, title=e.get("title", ""), playlist_ids=())
                   for vid, e in wanted.items() if vid in lib.songs]

    playlist_memberships: dict[str, set[str]] = {}
    for playlist in scan.playlists:
        playlist_id = links.playlist_id(playlist.url) or playlist.url
        for item in playlist.items:
            canonical = lib.canonical(item["id"])
            playlist_memberships.setdefault(canonical, set()).add(playlist_id)

    for entry in todo:
        entry["playlist_ids"] = tuple(sorted(playlist_memberships.get(entry["id"], ())))
    for entry in skipped:
        entry["playlist_ids"] = tuple(sorted(playlist_memberships.get(entry["id"], ())))

    plan = Plan(lib, scan, wanted, todo, len(entries), duplicates, skipped)
    if scan.progress is not None:
        new_ids = {entry["id"] for entry in plan.todo}
        playlist_new = {}
        for playlist in scan.playlists:
            playlist_new[playlist.url] = len({
                lib.canonical(item["id"]) for item in playlist.items
                if lib.canonical(item["id"]) in new_ids
            })
        library_before = len(lib.songs)
        scan.progress.update_plan(
            len(plan.todo),
            playlist_new,
            library_before,
            library_before + len(plan.todo),
            [entry["title"] for entry in plan.todo],
        )
        from . import ui

        ui.print_scan_summary(scan.progress.snapshot())
    return plan
