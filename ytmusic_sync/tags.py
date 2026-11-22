"""Title/artist cleaning and safe filenames. Pure functions, no I/O."""
from __future__ import annotations

import re
import unicodedata

JUNK = re.compile(
    r"[\(\[\{][^\)\]\}]*\b(official|lyrics?|lyrical|audio|video|visuali[sz]er|hd|hq|4k|"
    r"full song|music video|mv)\b[^\)\]\}]*[\)\]\}]", re.I)
EMOJI = re.compile("[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f]")
UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
ARTIST_TITLE = re.compile(r"^(.+?)\s+[-–—]\s+(.+)$")
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def tidy(text) -> str:
    text = JUNK.sub("", EMOJI.sub("", str(text or "")))
    text = re.sub(r"\s*[|｜].*$", "", text)
    return re.sub(r"\s+", " ", text).strip(" -–—_")


def first_text(value, default: str = "") -> str:
    if isinstance(value, str):
        return value.strip() or default
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return default


def clean_tags(info: dict, clean: bool = True) -> tuple[str, str]:
    """Return (artist, title). Prefers YouTube Music metadata, then 'Artist - Title', then channel."""
    fallback = first_text(info.get("id"), "Unknown")
    title = first_text(info.get("track"), first_text(info.get("title"), fallback))
    artist = first_text(info.get("artist"), first_text(info.get("creator")))
    scrub = tidy if clean else str.strip
    if clean:
        title = tidy(title) or title
    if not artist:
        match = ARTIST_TITLE.match(title)
        if match:
            artist, title = scrub(match[1]), scrub(match[2]) or title
        else:
            channel = first_text(info.get("channel"), first_text(info.get("uploader"), "Unknown"))
            artist = re.sub(r"\s*(-\s*Topic|VEVO)$", "", channel, flags=re.I)
    return scrub(artist).strip() or "Unknown", title


def safe_name(text: str, limit: int = 120) -> str:
    name = UNSAFE.sub("", str(text)).strip(" .")[:limit].strip(" .") or "track"
    return f"{name}_" if name.split(".")[0].upper() in RESERVED else name


def filename_candidates(artist: str, title: str, style: str) -> list[str]:
    """Preferred name first, then fallbacks used when another song already has that name."""
    base = {"title": title, "artist-title": f"{artist} - {title}",
            "title-artist": f"{title} - {artist}"}[style]
    result = []
    for text in (base, f"{title} - {artist}", f"{artist} - {title}"):
        name = safe_name(text)
        if name not in result:
            result.append(name)
    return result


def song_key(title: str) -> str:
    """Punctuation- and case-insensitive key used to spot the same song under two video IDs."""
    kept = "".join(c for c in tidy(title).lower() if not unicodedata.category(c).startswith("P"))
    return re.sub(r"\s+", " ", kept).strip()
