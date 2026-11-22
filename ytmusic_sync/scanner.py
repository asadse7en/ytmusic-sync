"""Resolve links into playlists and songs (metadata only, nothing is downloaded)."""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import links, ytdlp
from .config import Settings
from .tags import first_text, safe_name

SCAN_THREADS = 4
UNAVAILABLE = ("[Private video]", "[Deleted video]")
log = logging.getLogger(__name__)


@dataclass
class ScannedPlaylist:
    name: str
    url: str
    items: list[dict]


@dataclass
class ScanResult:
    playlists: list[ScannedPlaylist] = field(default_factory=list)
    singles: list[dict] = field(default_factory=list)   # single-video links
    names: dict = field(default_factory=dict)           # url -> display name (for playlists.txt)
    errors: list[tuple[str, str]] = field(default_factory=list)
    unavailable: int = 0                                # private/deleted videos skipped


def short_error(exc) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).replace("ERROR: ", "").strip()
    return (text.splitlines() or ["Unknown error"])[-1][:80]


def _item(entry: dict | None) -> dict | None:
    if not entry or not entry.get("id"):
        return None
    title = first_text(entry.get("title"), entry["id"])
    if title in UNAVAILABLE:
        return None
    return {"id": entry["id"], "title": title, "dur": int(entry.get("duration") or 0)}


def scan_links(urls: list[str], settings: Settings, progress=None) -> ScanResult:
    """Scan links in parallel. `progress(done, total)` is called as each link finishes."""
    urls = links.dedupe(urls)
    yt_dlp = ytdlp.module()
    options = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
               "skip_download": True, "logger": ytdlp.QuietLogger()}
    if settings.cookies:
        options["cookiesfrombrowser"] = (settings.cookies,)

    def scan_one(url: str):
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise ValueError("No playlist or song information found")
        if info.get("entries") is None:
            item = _item(info)
            if not item:
                raise ValueError("Video is private or unavailable")
            return url, "single", item, 0
        entries = list(info["entries"] or [])
        items = [i for i in map(_item, entries) if i]
        return url, "playlist", (first_text(info.get("title"), "playlist"), items), len(entries) - len(items)

    result = ScanResult()
    used_names: set[str] = set()
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, min(SCAN_THREADS, len(urls)))) as pool:
        futures = [(url, pool.submit(scan_one, url)) for url in urls]
        for url, future in futures:  # keep the order links were given in
            try:
                _, kind, payload, skipped = future.result()
                if kind == "single":
                    result.singles.append(payload)
                    result.names[url] = payload["title"]
                else:
                    name = _unique_name(safe_name(payload[0]), used_names)
                    result.playlists.append(ScannedPlaylist(name, url, payload[1]))
                    result.names[url] = name
                    result.unavailable += skipped
            except Exception as exc:
                log.warning("Scan failed for %s: %s", url, exc)
                result.errors.append((url[:60], short_error(exc)))
            done += 1
            if progress:
                progress(done, len(urls))
    return result


def _unique_name(name: str, used: set[str]) -> str:
    candidate, n = name, 2
    while candidate.lower() in used:
        candidate, n = f"{name} ({n})", n + 1
    used.add(candidate.lower())
    return candidate
