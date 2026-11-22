"""YouTube link parsing and the playlists.txt link list."""
from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlparse

from . import config


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_youtube_link(url: str) -> bool:
    parsed = urlparse(url.strip())
    host = _host(url)
    return parsed.scheme in ("http", "https") and (
        host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"))


def normalize_url(url: str) -> str:
    """music.youtube.com -> www.youtube.com (yt-dlp handles both, this keeps links uniform)."""
    url = url.strip()
    if _host(url) == "music.youtube.com":
        return urlparse(url)._replace(netloc="www.youtube.com").geturl()
    return url


def playlist_id(url: str) -> str:
    values = parse_qs(urlparse(url).query).get("list")
    return values[0] if values else ""


def is_playlist_url(url: str) -> bool:
    parsed = urlparse(url)
    return "list" in parse_qs(parsed.query, keep_blank_values=True) or "/playlist" in parsed.path.lower()


def canonical_url(url: str) -> str:
    """Playlist links collapse to https://www.youtube.com/playlist?list=ID so duplicates match."""
    url = normalize_url(url)
    pid = playlist_id(url)
    return f"https://www.youtube.com/playlist?{urlencode({'list': pid})}" if pid else url


def link_key(url: str) -> tuple[str, str]:
    """Identity used to drop duplicate links."""
    canonical = canonical_url(url)
    pid = playlist_id(canonical)
    if pid:
        return "playlist", pid
    parsed = urlparse(canonical)
    if _host(canonical) == "youtu.be":
        return "video", parsed.path.strip("/")
    video = parse_qs(parsed.query).get("v")
    return ("video", video[0]) if video else ("url", canonical)


def dedupe(urls) -> list[str]:
    seen, result = set(), []
    for url in urls:
        key = link_key(url)
        if key not in seen:
            seen.add(key)
            result.append(canonical_url(url))
    return result


def parse_pasted(text: str) -> tuple[list[str], list[str]]:
    """Split pasted text into (valid unique links, rejected pieces)."""
    valid, rejected, seen = [], [], set()
    for piece in text.replace(",", " ").split():
        if piece.startswith("#"):
            continue
        if not is_youtube_link(piece):
            rejected.append(piece)
            continue
        key = link_key(piece)
        if key not in seen:
            seen.add(key)
            valid.append(piece)
    return valid, rejected


# ---------- playlists.txt ----------
def read_links(path=None) -> list[str]:
    """Links from a text file: one per line, '#' lines are comments."""
    try:
        lines = (path or config.LINKS_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return dedupe(line.strip() for line in lines if line.strip() and not line.strip().startswith("#"))


def save_links(entries: list[tuple[str, str]]) -> int:
    """Append (url, name) pairs not already saved; the name goes in a comment above the link."""
    config.ensure_data_dir()
    path = config.LINKS_FILE
    existing = {link_key(u) for u in read_links(path)}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    block = []
    for url, name in entries:
        key = link_key(url)
        if key in existing:
            continue
        existing.add(key)
        label = " ".join(str(name).split())
        block.append((f"# {label}\n" if label else "") + f"{url}\n")
    if block:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            if text and not text.endswith("\n"):
                handle.write("\n")
            handle.writelines(block)
    return len(block)
