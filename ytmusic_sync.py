#!/usr/bin/env python3
"""ytmusic-sync: YouTube playlists -> clean, tagged music library + .m3u8 playlists."""
import argparse, contextlib, hashlib, json, logging, os, queue, re, shutil, subprocess, sys, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import unicodedata
from urllib.parse import urlparse

try:
    import questionary, yt_dlp
    from platformdirs import user_config_dir, user_data_dir
    from yt_dlp.postprocessor.common import PostProcessor
    from yt_dlp.version import __version__ as YTDLP_VERSION
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress_bar import ProgressBar
    from rich.table import Table
    from rich.text import Text
except ImportError:
    sys.exit("Missing packages. Run: pip install -r requirements.txt")

for _s in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _s.reconfigure(encoding="utf-8")

VERSION = "0.1"
DATA_BASE = Path(user_data_dir("ytsync", appauthor=False)) / "libraries"
POINTER = Path(user_config_dir("ytsync", appauthor=False)) / "last_library.json"
console = Console()

FORMATS = ["m4a", "mp3", "opus", "flac", "wav", "aac"]
QUALITIES = ["best", "320", "256", "192", "128"]
STYLES = {"title": "Title", "artist-title": "Artist - Title", "title-artist": "Title - Artist"}
EXT = {".m4a", ".mp3", ".opus", ".flac", ".wav", ".aac", ".ogg", ".mp4"}
DEFAULTS = dict(format="m4a", quality="best", name_style="title", cover=True, square=True, clean=True,
                playlist_files=True, prune=False, skip_duplicate_songs=True, jobs=3, retries=5,
                rate_limit="", cookies="", urls=[], files=[])
LABELS = [("format", "Format"), ("quality", "Quality"), ("name_style", "Filename style"),
          ("cover", "Cover art"), ("square", "Square cover"), ("clean", "Clean titles"),
          ("playlist_files", "Playlist .m3u8 files"), ("prune", "Prune removed songs"),
          ("skip_duplicate_songs", "Skip duplicate songs"), ("jobs", "Parallel downloads"),
          ("rate_limit", "Speed limit"), ("retries", "Retries"), ("cookies", "Browser cookies")]
CHOICES = {"format": FORMATS, "quality": QUALITIES, "name_style": list(STYLES), "jobs": list(range(1, 7))}
STYLE = questionary.Style([("pointer", "fg:#ff8c00 bold"), ("highlighted", "fg:#ff8c00 bold"),
                           ("selected", "fg:#ff8c00"), ("question", "bold")])

JUNK = re.compile(r"[\(\[\{][^\)\]\}]*\b(official|lyrics?|lyrical|audio|video|visuali[sz]er|hd|hq|4k|"
                  r"full song|music video|mv)\b[^\)\]\}]*[\)\]\}]", re.I)
EMOJI = re.compile("[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f]")
BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
YT_ID = re.compile(r"(?:v=|youtu\.be/)([\w-]{11})")
BLOCKED = re.compile(r"403|sign in|bot|nsig|unable to extract|HTTP Error", re.I)


# ---------- small helpers ----------
class Quiet:
    def debug(self, m): logging.debug(m)
    def info(self, m): logging.debug(m)
    def warning(self, m): logging.warning(m)
    def error(self, m): logging.error(m)


def aw(path, text):
    """Atomic write: temp file, then rename."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def jload(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def size(b):
    b = float(b or 0)
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"


def dur(s):
    m, s = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def ago(s):
    try:
        d = (datetime.now() - datetime.strptime(s, "%Y-%m-%d %H:%M")).total_seconds()
    except Exception:
        return "never"
    return "just now" if d < 3600 else f"{int(d // 3600)}h ago" if d < 86400 else f"{int(d // 86400)}d ago"


def short(e):
    return re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "").strip().split("\n")[-1][:80]


def fsafe(s, n=120):
    return BAD.sub("", s).strip(" .")[:n].strip() or "track"


def read_list(p):
    try:
        return [l.strip() for l in Path(p).read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.strip().startswith("#")]
    except Exception:
        return []


# ---------- library location, config ----------
def profile_dir(out):
    resolved = Path(out).expanduser().resolve()
    digest = hashlib.sha1(str(resolved).lower().encode("utf-8")).hexdigest()[:8]
    profile = DATA_BASE / f"{fsafe(resolved.name)}-{digest}"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def migration_target(profile, name):
    target = profile / name
    if not target.exists():
        return target
    i = 1
    while True:
        candidate = profile / f"{name}.legacy-{i}"
        if not candidate.exists():
            return candidate
        i += 1


def ensure_lib(out):
    """Create a music folder profile, migrate legacy app files, and remember the library."""
    out = Path(out).expanduser().resolve()
    if not out.exists():
        if not out.parent.is_dir():
            raise FileNotFoundError(f"Music folder is unavailable: {out}")
        out.mkdir()
    if not out.is_dir():
        raise NotADirectoryError(f"Music folder is not a directory: {out}")
    profile = profile_dir(out)
    legacy = out / ".ytmusic-sync"
    if legacy.is_dir():
        for item in legacy.iterdir():
            shutil.move(str(item), str(migration_target(profile, item.name)))
        legacy.rmdir()
    for old, new in (("library.json", "library.json"), ("failed.txt", "failed.txt"),
                     ("ytmusic-sync.log", "sync.log")):
        source = out / old
        if source.is_file():
            shutil.move(str(source), str(migration_target(profile, new)))
    POINTER.parent.mkdir(parents=True, exist_ok=True)
    aw(POINTER, json.dumps({"path": str(out)}))
    return out


def find_lib(explicit=None):
    """--out > last used library > no selection."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    p = jload(POINTER, {}).get("path")
    if p:
        folder = Path(p).expanduser()
        if folder.is_dir():
            return folder.resolve()
        console.print(f"[yellow]Last used music folder is unavailable: {folder}. "
                      "Reconnect the drive or choose a folder.[/]")
    return None


def load_cfg(out):
    c = {**DEFAULTS, **jload(profile_dir(out) / "config.json", {})}
    c["urls"], c["files"] = list(c["urls"]), list(c["files"])
    c["jobs"] = int(c["jobs"])
    if not 1 <= c["jobs"] <= 6:
        raise ValueError("Parallel downloads must be between 1 and 6.")
    c["out"] = str(out)
    return c


def save_cfg(c):
    aw(profile_dir(c["out"]) / "config.json", json.dumps({k: v for k, v in c.items() if k != "out"}, indent=1))


def all_urls(c):
    urls = c["urls"] + [u for f in c["files"] for u in read_list(f)] + read_list(profile_dir(c["out"]) / "playlists.txt")
    return list(dict.fromkeys(urls))


# ---------- ffmpeg + yt-dlp updates ----------
FFMPEG = None  # path to a pip-provided ffmpeg, used when none is installed system-wide


def need_ffmpeg():
    global FFMPEG
    if shutil.which("ffmpeg"):
        return True
    try:
        import imageio_ffmpeg  # ships an ffmpeg binary inside the pip package
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            FFMPEG = exe
            return True
    except Exception as e:
        logging.debug("imageio-ffmpeg failed: %s", e)
    console.print(Panel("ffmpeg not found and the bundled copy is unavailable.\n  Windows: winget install ffmpeg\n"
                        "  macOS:   brew install ffmpeg\n  Linux:   sudo apt install ffmpeg",
                        title="[red]Missing ffmpeg", border_style="red"))
    return False


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def ytdlp_latest(out):
    cp = profile_dir(out) / "cache.json"
    c = jload(cp, {})
    if time.time() - c.get("t", 0) < 86400 and c.get("latest"):
        return c["latest"]
    try:
        with urllib.request.urlopen("https://pypi.org/pypi/yt-dlp/json", timeout=4) as r:
            latest = json.load(r)["info"]["version"]
        aw(cp, json.dumps({"t": time.time(), "latest": latest}))
        return latest
    except Exception:  # offline: retry in about an hour
        with contextlib.suppress(OSError):
            aw(cp, json.dumps({"t": time.time() - 86400 + 3600, "latest": c.get("latest")}))
        return c.get("latest")


def outdated(out):
    new = ytdlp_latest(out)
    return (YTDLP_VERSION, new) if new and vtuple(new) > vtuple(YTDLP_VERSION) else None


def upgrade_ytdlp():
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"])
    console.print("[green]yt-dlp updated. Restart ytsync to use it.[/]" if r.returncode == 0
                  else "[red]Update failed. Try: pip install -U yt-dlp[/]")
    return r.returncode == 0


# ---------- tag + filename cleaning ----------
def tidy(s):
    s = JUNK.sub("", EMOJI.sub("", s or ""))
    s = re.sub(r"\s*[|｜].*$", "", s)
    return re.sub(r"\s+", " ", s).strip(" -–—_")


def clean_tags(i, clean):
    title = i.get("track") or i.get("title") or i["id"]
    artist = i.get("artist") or i.get("creator") or ""
    if clean:
        title = tidy(title) or title
    if not artist:
        m = re.match(r"^(.+?)\s+[-–—]\s+(.+)$", title)
        if m:
            artist, title = tidy(m[1]), tidy(m[2])
        else:
            artist = re.sub(r"\s*(-\s*Topic|VEVO)$", "", i.get("channel") or i.get("uploader") or "Unknown", flags=re.I)
    return (tidy(artist) if clean else artist).strip() or "Unknown", title


def pick_name(lib, vid, artist, title, style):
    base = {"title": title, "artist-title": f"{artist} - {title}", "title-artist": f"{title} - {artist}"}[style]
    with lib.name_lock:
        if vid in lib.reserved_by_id:
            return lib.reserved_by_id[vid]
        with lib.lock:
            own = Path(lib.d["songs"].get(vid, {}).get("file", "")).stem.lower()
        taken = {p.stem.lower() for p in lib.out.iterdir() if p.suffix.lower() in EXT} - {own}
        taken.update(lib.reserved_names)
        for c in (base, f"{title} - {artist}", f"{artist} - {title}"):
            candidate = fsafe(c)
            if candidate.lower() not in taken:
                lib.reserved_names.add(candidate.lower())
                lib.reserved_by_id[vid] = candidate
                return candidate
        k = 2
        while f"{fsafe(base)} ({k})".lower() in taken:
            k += 1
        candidate = f"{fsafe(base)} ({k})"
        lib.reserved_names.add(candidate.lower())
        lib.reserved_by_id[vid] = candidate
        return candidate


class Clean(PostProcessor):
    """Runs before filename + tagging: sets clean artist/title and the final filename."""
    def __init__(self, lib, cfg, st):
        super().__init__()
        self.lib, self.cfg, self.st = lib, cfg, st

    def run(self, info):
        a, t = clean_tags(info, self.cfg["clean"])
        info.update(artist=a, track=t, title=t, album_artist=a.split(",")[0].strip())
        info["fname"] = pick_name(self.lib, info["id"], a, t, self.cfg["name_style"])
        self.st.set_track(t, a)
        return [], info


class SquareCover(PostProcessor):
    """Center-crop the downloaded cover to a square, in place (runs after the JPEG conversion)."""
    def run(self, info):
        exe = FFMPEG or shutil.which("ffmpeg")
        for t in info.get("thumbnails") or []:
            src = t.get("filepath")
            if not exe or not src or not os.path.exists(src):
                continue
            tmp = src + ".sq.jpg"
            r = subprocess.run([exe, "-y", "-loglevel", "error", "-i", src, "-vf", "crop='min(iw,ih)':'min(iw,ih)'",
                                "-c:v", "mjpeg", "-q:v", "2", tmp], capture_output=True)
            if r.returncode == 0 and os.path.exists(tmp):
                os.replace(tmp, src)
            else:
                logging.warning("cover crop failed: %s", r.stderr.decode(errors="ignore")[-200:])
        return [], info


# ---------- library database ----------
def tag_has_url(k, v):
    """True for tags that hold the source URL (comment/purl), or a description that is only a URL."""
    k, v = str(k).lower(), str(v)
    if any(x in k for x in ("cmt", "comm", "purl")):
        return True
    return "description" in k and bool(re.fullmatch(r"\W*https?://[^\s'\"]+\W*", v))


class Lib:
    def __init__(self, out):
        self.out, self.f = Path(out), profile_dir(out) / "library.json"
        self.lock, self.name_lock, self.reserved_names = threading.RLock(), threading.Lock(), set()
        self.reserved_by_id = {}
        self.d = {"songs": {}, "playlists": {}, "stats": {}, "aliases": {}}
        self.d.update(jload(self.f, {}))
        self.d.setdefault("aliases", {})
        self.d["stats"] = {"runs": 0, "downloads": 0, "bytes": 0, "last_run": "never", **self.d["stats"]}
        if not self.d["songs"]:
            self.rebuild()
        self.d["songs"] = {k: v for k, v in self.d["songs"].items() if (self.out / v["file"]).exists()}
        self.d["aliases"] = {k: v for k, v in self.d["aliases"].items() if v in self.d["songs"]}

    def rebuild(self):
        """Best effort: recover video IDs from the source URL stored in each file's tags."""
        try:
            import mutagen
        except ImportError:
            return
        for p in self.out.iterdir():
            if p.suffix.lower() not in EXT or not p.is_file():
                continue
            try:
                f = mutagen.File(p)
                if not f or not f.tags:
                    continue
                vid = next((m[1] for k, v in f.tags.items() if tag_has_url(k, v)
                            for m in [YT_ID.search(str(v))] if m), None)
                if not vid:
                    continue
                e = mutagen.File(p, easy=True)
                get = lambda k: ((e.tags.get(k) or [""])[0] if e and e.tags else "")
                self.d["songs"][vid] = {"file": p.name, "artist": get("artist"),
                                        "title": get("title") or p.stem, "dur": int(f.info.length)}
            except Exception:
                continue

    def save(self):
        with self.lock:
            aw(self.f, json.dumps(self.d, indent=1, ensure_ascii=False))

    def total_size(self):
        with self.lock:
            return sum((self.out / v["file"]).stat().st_size for v in self.d["songs"].values()
                       if (self.out / v["file"]).exists())


# ---------- scan ----------
def resolve(urls, cfg, errs, details=None):
    o = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True, "logger": Quiet()}
    if cfg["cookies"]:
        o["cookiesfrombrowser"] = (cfg["cookies"],)
    pls, singles, bad = [], [], 0
    mk = lambda e: {"id": e["id"], "title": e.get("title") or e["id"], "dur": int(e.get("duration") or 0)}
    with console.status("[cyan]Scanning links..."), yt_dlp.YoutubeDL(o) as y:
        for u in urls:
            try:
                info = y.extract_info(u, download=False)
            except Exception as e:
                errs.append((u[:50], short(e)))
                if details is not None:
                    details[u] = (u, 0, short(e))
                continue
            if not info:
                if details is not None:
                    details[u] = (u, 0, "No playlist or song information found")
                continue
            if info.get("entries") is None:
                song = mk(info)
                singles.append(song)
                if details is not None:
                    details[u] = (song["title"], 1, None)
                continue
            good = []
            for e in info["entries"]:
                if not e or not e.get("id"):
                    continue
                if e.get("title") in ("[Private video]", "[Deleted video]"):
                    bad += 1
                    continue
                good.append(mk(e))
            name = fsafe(info.get("title") or "playlist")
            pls.append({"name": name, "items": good})
            if details is not None:
                details[u] = (name, len(good), None)
    return pls, singles, bad


def song_key(title):
    normalized = "".join(c for c in tidy(title).lower() if not unicodedata.category(c).startswith("P"))
    return re.sub(r"\s+", " ", normalized).strip()


def canonical_id(vid, aliases):
    seen = set()
    while vid in aliases and vid not in seen:
        seen.add(vid)
        vid = aliases[vid]
    return vid


def scan(cfg, urls):
    out = Path(cfg["out"])
    logging.basicConfig(filename=profile_dir(out) / "sync.log", level=logging.INFO, format="%(asctime)s %(message)s")
    lib, errs = Lib(out), []
    pls, singles, bad = resolve(urls, cfg, errs)
    entries = [x for p in pls for x in p["items"]] + singles
    want = {}
    for e in entries:
        want.setdefault(e["id"], e)

    with lib.lock:
        songs = dict(lib.d["songs"])
        aliases = dict(lib.d["aliases"])
    titles = {}
    for vid, song in songs.items():
        key, seconds = song_key(song.get("title", "")), int(song.get("dur") or 0)
        if key and seconds > 0:
            titles.setdefault(key, []).append((seconds, vid))

    unique = {}
    skip_dupes = cfg.get("skip_duplicate_songs", True)
    for vid, e in want.items():
        if vid in aliases:
            continue
        key, seconds = song_key(e.get("title", "")), int(e.get("dur") or 0)
        match = next((kept for old_seconds, kept in titles.get(key, [])
                      if skip_dupes and key and seconds > 0 and kept != vid and abs(seconds - old_seconds) <= 3), None)
        if match:
            aliases[vid] = match
            continue
        unique[vid] = e
        if key and seconds > 0:
            titles.setdefault(key, []).append((seconds, vid))

    canonical = {}
    for vid, e in want.items():
        kept = canonical_id(vid, aliases)
        if kept in unique:
            canonical.setdefault(kept, unique[kept])
        elif kept in songs:
            canonical.setdefault(kept, {"id": kept, "title": songs[kept].get("title", e["title"]),
                                        "dur": int(songs[kept].get("dur") or e.get("dur") or 0)})
        else:
            canonical.setdefault(vid, e)
    todo = [e for vid, e in canonical.items() if vid not in songs]
    with lib.lock:
        lib.d["aliases"] = aliases
    return dict(out=out, lib=lib, pls=pls, singles=singles, bad=bad, errs=errs, want=want, unique=canonical,
                todo=todo, aliases=aliases, entries=len(entries), repeats=len(entries) - len(want),
                duplicate_count=sum(1 for vid in want if vid in aliases))


# ---------- live dashboard ----------
class StopRequested(Exception):
    pass


class WorkerStatus:
    def __init__(self, state, worker_id):
        self.state, self.worker_id = state, worker_id

    def set_track(self, title, artist):
        self.state.set_track(self.worker_id, title, artist)


class St:
    def __init__(self, total, todo, jobs):
        self.total, self.n, self.done, self.fail, self.bytes = total, 0, 0, 0, 0
        self.jobs, self.todo = jobs, todo
        self.t0, self.feed, self.song_times = time.time(), [], []
        self.lock, self.print_lock = threading.RLock(), threading.Lock()
        self.pending = set(range(total))
        self.workers = {i: {"active": False} for i in range(jobs)}

    def start(self, worker_id, index, e, playlist):
        with self.lock:
            self.pending.discard(index)
            self.workers[worker_id] = dict(active=True, index=index, title=e["title"], artist="",
                                           playlist=playlist, dl=0, total=0, speed=0, stage=0, retry="")

    def set_track(self, worker_id, title, artist):
        with self.lock:
            self.workers[worker_id].update(title=title, artist=artist)

    def progress(self, worker_id, d):
        with self.lock:
            state = self.workers[worker_id]
            if d["status"] == "downloading":
                state.update(stage=1, dl=d.get("downloaded_bytes") or 0,
                             total=d.get("total_bytes") or d.get("total_bytes_estimate") or 0,
                             speed=d.get("speed") or 0)
            elif d["status"] == "finished":
                state["stage"] = 2

    def postprocess(self, worker_id, d):
        if d["status"] != "started":
            return
        with self.lock:
            if d["postprocessor"] == "ExtractAudio":
                self.workers[worker_id]["stage"] = 2
            elif d["postprocessor"] in ("Metadata", "EmbedThumbnail"):
                self.workers[worker_id]["stage"] = 3

    def set_retry(self, worker_id, delay=None):
        with self.lock:
            self.workers[worker_id]["retry"] = f"Retrying in {delay}s" if delay else ""

    def finish_worker(self, worker_id):
        with self.lock:
            self.workers[worker_id] = {"active": False}

    def record(self, worker_id, ok, fallback_title, song_size, elapsed, reason):
        with self.lock:
            state = self.workers[worker_id]
            title = state.get("title") or fallback_title
            artist = state.get("artist", "")
            if ok:
                self.done += 1
                self.bytes += song_size
            else:
                self.fail += 1
            self.n += 1
            self.feed.append((ok, title, artist, size(song_size) if ok else "-",
                              f"{elapsed:.1f}s", "" if ok else reason))
            self.song_times.append(elapsed)
            self.song_times = self.song_times[-10:]
            return self.n, title

    def render(self):
        narrow = console.size.width < 80
        with self.lock:
            el = time.time() - self.t0
            n, done, fail, byte_count = self.n, self.done, self.fail, self.bytes
            average = sum(self.song_times[-10:]) / len(self.song_times[-10:]) if self.song_times else 0
            feed = list(self.feed)
            workers = [(i, dict(state)) for i, state in self.workers.items() if state["active"]]
            next_up = [self.todo[i]["title"] for i in sorted(self.pending)[:3]]
            pending_count = len(self.pending)
        remaining = max(self.total - n, 0)
        active_eta = [max((s["total"] - s["dl"]) / s["speed"], 0)
                      if s["total"] and s["speed"] else average for _, s in workers]
        worker_count = max(1, min(self.jobs, len(workers) or self.jobs))
        eta = max(active_eta, default=0) + average * ((pending_count + worker_count - 1) // worker_count)
        avg_speed = byte_count / el if el else 0
        stats = [
            ("Done", str(done), "#22c55e"),
            ("Failed", str(fail), "#ef4444"),
            ("Remaining", str(remaining), "bold"),
            ("Size", size(byte_count), "bold"),
            ("Avg speed", f"{size(avg_speed)}/s", "bold"),
            ("Elapsed", dur(el), "bold"),
            ("ETA", dur(eta), "bold"),
        ]
        strip = Table.grid(expand=True, padding=(0, 1))
        if narrow:
            for row in (stats[:4], stats[4:]):
                strip.add_row(*(Text.assemble((f"{label} ", "#8b8b8b"), (value, style)) for label, value, style in row))
        else:
            strip.add_row(*(Text.assemble((f"{label} ", "#8b8b8b"), (value, style)) for label, value, style in stats))

        now_panels = []
        bar_width = max(console.size.width - (60 if narrow else 54), 8)
        for worker_id, state in workers[:3]:
            title = Text(f"Now · {state['index'] + 1}/{self.total} · Worker {worker_id + 1}",
                         style="bold #ff8c00")
            current_title = Text(state["title"], style="bold", no_wrap=True, overflow="ellipsis")
            pct = state["dl"] / state["total"] if state["total"] else 0
            current_eta = max((state["total"] - state["dl"]) / state["speed"], 0) \
                if state["total"] and state["speed"] else 0
            bar_row = Table.grid(expand=True, padding=(0, 1))
            bar_row.add_column(width=6)
            bar_row.add_column(width=bar_width)
            bar_row.add_column(justify="right")
            detail = (f"{size(state['dl'])} / {size(state['total'])} · {size(state['speed'])}/s · ETA {dur(current_eta)}"
                      if state["total"] else f"{size(state['dl'])} · {size(state['speed'])}/s · ETA …")
            if state["retry"]:
                detail += f" · {state['retry']}"
            bar_row.add_row(Text(f"{pct:.1%}", style="bold #ff8c00"),
                            ProgressBar(total=state["total"] or None, completed=state["dl"], width=bar_width,
                                        pulse=not state["total"], style="#3a3a3a", complete_style="#ff8c00",
                                        finished_style="#22c55e", animation_time=time.time()),
                            Text(detail, style="bold"))
            steps = Text()
            for i, name in enumerate(("Fetch", "Download", "Convert", "Tag")):
                if i:
                    steps.append("  ")
                marker, color = ("✔", "#22c55e") if i < state["stage"] else \
                                ("●", "#ff8c00") if i == state["stage"] else ("○", "#8b8b8b")
                steps.append(f"{marker} {name}", style=color)
            row_items = [title, current_title]
            if not narrow:
                row_items += [Text(state["artist"], style="#8b8b8b", no_wrap=True, overflow="ellipsis"),
                              Text(state["playlist"], style="#8b8b8b", no_wrap=True, overflow="ellipsis")]
            row_items += [bar_row, steps]
            now_panels.append(Panel(Group(*row_items), border_style="#ff8c00", padding=(0, 1), expand=True))

        overall_done = min(n + len(workers), self.total)
        done_pct = overall_done / self.total if self.total else 0
        overall_width = max(console.size.width - 22, 8)
        overall = Table.grid(expand=True, padding=(0, 1))
        overall.add_column(width=overall_width)
        overall.add_column(justify="right")
        overall.add_row(ProgressBar(total=self.total or None, completed=overall_done, width=overall_width,
                                    style="#3a3a3a", complete_style="#ff8c00",
                                    finished_style="#22c55e", animation_time=time.time()),
                        Text(f"{done_pct:.1%} · {overall_done}/{self.total}", style="bold"))

        recent = Table(title="Recent", expand=True, border_style="#3a3a3a", header_style="bold")
        recent.add_column("Status", width=7)
        recent.add_column("Title", ratio=3, no_wrap=True, overflow="ellipsis")
        if not narrow:
            recent.add_column("Artist", ratio=2, no_wrap=True, overflow="ellipsis")
            recent.add_column("Size", justify="right", no_wrap=True)
        recent.add_column("Time", justify="right", no_wrap=True)
        for ok, song, song_artist, song_size, elapsed, reason in feed[-6:]:
            color = "#22c55e" if ok else "#ef4444"
            name = song if ok or not reason else f"{song} · {reason}"
            cells = [Text("✔" if ok else "✖", style=color), Text(name, style=color)]
            if not narrow:
                cells.extend([Text(song_artist, style="#8b8b8b"), Text(song_size, justify="right")])
            cells.append(Text(elapsed, justify="right"))
            recent.add_row(*cells)

        items = [strip, *now_panels, overall, recent]
        if not narrow:
            items.append(Text("Up next", style="bold"))
            items.extend(Text(f"  {song}", style="#8b8b8b", no_wrap=True, overflow="ellipsis") for song in next_up)
        items.append(Text("Ctrl+C to stop safely", style="#8b8b8b"))
        return Group(*items)


def dl_opts(cfg, out, st, worker_id, stop_event):
    f = cfg["format"]

    def hook(d):
        if stop_event.is_set():
            raise StopRequested()
        st.progress(worker_id, d)

    def pphook(d):
        if stop_event.is_set():
            raise StopRequested()
        st.postprocess(worker_id, d)

    o = {"outtmpl": str(out / "%(fname)s.%(ext)s"), "quiet": True, "no_warnings": True, "noprogress": True,
         "logger": Quiet(), "progress_hooks": [hook], "postprocessor_hooks": [pphook], "windowsfilenames": True,
         "concurrent_fragment_downloads": 4, "http_chunk_size": 10485760,
         "retries": cfg["retries"], "fragment_retries": cfg["retries"],
         "format": "bestaudio[ext=m4a]/bestaudio/best" if f in ("m4a", "aac") else "bestaudio/best"}
    pp = [{"key": "FFmpegExtractAudio", "preferredcodec": f,
           "preferredquality": "0" if cfg["quality"] == "best" else cfg["quality"]}]
    cover = cfg["cover"] and f != "wav"
    if cover:
        o["writethumbnail"] = True
        pp.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
    pp.append({"key": "FFmpegMetadata", "add_metadata": True})
    if cover:
        pp.append({"key": "EmbedThumbnail"})
    o["postprocessors"] = pp
    if cfg["rate_limit"]:
        o["ratelimit"] = yt_dlp.utils.parse_bytes(cfg["rate_limit"])
    if cfg["cookies"]:
        o["cookiesfrombrowser"] = (cfg["cookies"],)
    if FFMPEG:
        o["ffmpeg_location"] = FFMPEG
    return o


def m3u(out, name, ids, songs, aliases=None):
    aliases = aliases or {}
    L = ["#EXTM3U"]
    for i in ids:
        s = songs.get(canonical_id(i, aliases))
        if s:
            L += [f"#EXTINF:{s['dur']},{s['artist']} - {s['title']}", s["file"]]
    aw(out / f"{name}.m3u8", "\n".join(L) + "\n")


# ---------- run ----------
def watch_url(vid):
    return f"https://www.youtube.com/watch?v={vid}"


class WorkerGate:
    def __init__(self, limit):
        self.limit, self.active = limit, 0
        self.condition = threading.Condition()

    def acquire(self, stop_event):
        with self.condition:
            while self.active >= self.limit and not stop_event.is_set():
                self.condition.wait(0.2)
            if stop_event.is_set():
                return False
            self.active += 1
            return True

    def release(self):
        with self.condition:
            self.active -= 1
            self.condition.notify_all()

    def reduce(self):
        with self.condition:
            self.limit = max(1, self.limit - 1)
            self.condition.notify_all()

    def wake(self):
        with self.condition:
            self.condition.notify_all()


def download_worker(worker_id, work, cfg, out, lib, st, gate, stop_event, failed, failed_lock,
                    playlist_by_id, compact):
    status = WorkerStatus(st, worker_id)
    with yt_dlp.YoutubeDL(dl_opts(cfg, out, st, worker_id, stop_event)) as y:
        y.add_post_processor(Clean(lib, cfg, status), when="pre_process")
        if cfg["cover"] and cfg["square"] and cfg["format"] != "wav":
            y.add_post_processor(SquareCover(), when="before_dl")
        while not stop_event.is_set():
            try:
                index, e = work.get_nowait()
            except queue.Empty:
                return
            if not gate.acquire(stop_event):
                return
            st.start(worker_id, index, e, playlist_by_id.get(e["id"], "Single track"))
            t1, ok, song_size, note, cancelled, retries = time.time(), False, 0, "", False, 0
            held = True
            try:
                while True:
                    retry_delay = None
                    try:
                        if stop_event.is_set():
                            raise StopRequested()
                        info = y.extract_info(watch_url(e["id"]), download=True)
                        rd = (info.get("requested_downloads") or [{}])[0]
                        fp = Path(rd.get("filepath") or out / f"{info['fname']}.{cfg['format']}")
                        record = {"file": fp.name, "artist": info.get("artist") or "",
                                  "title": info.get("title") or e["title"],
                                  "dur": int(info.get("duration") or e["dur"])}
                        song_size = fp.stat().st_size
                        with lib.lock:
                            lib.d["songs"][e["id"]] = record
                        ok, note = True, f"{size(song_size)}  {time.time() - t1:.1f}s"
                    except StopRequested:
                        cancelled = True
                    except Exception as ex:
                        if stop_event.is_set():
                            cancelled = True
                        elif re.search(r"429|sign\s+in", str(ex), re.I) and retries < max(1, int(cfg["retries"])):
                            retries += 1
                            gate.reduce()
                            retry_delay = min(2 ** (retries - 1), 30)
                            st.set_retry(worker_id, retry_delay)
                            logging.warning("%s: transient download failure, retry %s in %ss: %s",
                                            e["id"], retries, retry_delay, short(ex))
                        else:
                            note = short(ex)
                            with failed_lock:
                                failed.append((e["title"], note))
                            logging.error("%s: %s", e["id"], ex)
                    gate.release()
                    held = False
                    if retry_delay is not None:
                        if stop_event.wait(retry_delay) or not gate.acquire(stop_event):
                            cancelled = True
                            break
                        held = True
                        st.set_retry(worker_id)
                        continue
                    break
            finally:
                if held:
                    gate.release()
            elapsed = time.time() - t1
            if cancelled:
                st.finish_worker(worker_id)
                return
            count, title = st.record(worker_id, ok, e["title"], song_size, elapsed, note)
            st.finish_worker(worker_id)
            lib.save()
            if compact:
                with st.print_lock:
                    console.print(f"{'✔' if ok else '✖'} {count}/{st.total} {title[:50]}  {note}")


def execute(cfg, plan, compact=False, interactive=False, prune_ok=False):
    out, lib, todo = plan["out"], plan["lib"], plan["todo"]
    jobs = max(1, min(6, int(cfg.get("jobs", 3))))
    worker_count = min(jobs, max(1, len(todo)))
    st = St(len(todo), todo, worker_count)
    failed, stopped = list(plan["errs"]), False
    failed_lock = threading.Lock()
    plain = compact or not console.is_terminal
    if todo:
        if not plain:
            console.clear()
        live = contextlib.nullcontext() if plain else \
            Live(get_renderable=st.render, console=console, refresh_per_second=10, transient=True, screen=True)
        playlist_by_id = {}
        for playlist in plan["pls"]:
            for item in playlist["items"]:
                playlist_by_id.setdefault(item["id"], playlist["name"])
        work, gate, stop_event = queue.Queue(), WorkerGate(worker_count), threading.Event()
        for index, e in enumerate(todo):
            work.put((index, e))
        with live:
            executor = ThreadPoolExecutor(max_workers=worker_count)
            futures = []
            try:
                futures = [executor.submit(download_worker, worker_id, work, cfg, out, lib, st, gate, stop_event,
                                           failed, failed_lock, playlist_by_id, plain)
                           for worker_id in range(worker_count)]
                for future in as_completed(futures):
                    future.result()
            except KeyboardInterrupt:
                stopped = True
                stop_event.set()
                gate.wake()
                for future in futures:
                    future.cancel()
            except BaseException:
                stop_event.set()
                gate.wake()
                for future in futures:
                    future.cancel()
                raise
            finally:
                executor.shutdown(wait=True, cancel_futures=stop_event.is_set())
        for p in list(out.glob("*.part")) + list(out.glob("*.ytdl")):
            p.unlink(missing_ok=True)

    with lib.lock:
        songs, aliases = dict(lib.d["songs"]), dict(plan["aliases"])
        lib.d["aliases"] = aliases
        old_playlists = dict(lib.d["playlists"])
    rows = []
    for p in plan["pls"]:
        ids = [e["id"] for e in p["items"]]
        current = {canonical_id(i, aliases) for i in ids}
        old = {canonical_id(i, aliases) for i in old_playlists.get(p["name"], [])}
        if cfg["playlist_files"]:
            m3u(out, p["name"], ids, songs, aliases)
        with lib.lock:
            lib.d["playlists"][p["name"]] = ids
        rows.append((p["name"], sum(1 for i in current if i in songs), len(current - old), len(old - current)))

    if cfg["prune"] and prune_ok and not stopped and not plan["errs"] and console.is_terminal:
        stale = [k for k in songs if k not in plan["unique"]]
        if stale and questionary.confirm(f"Delete {len(stale)} song(s) no longer in any playlist?", default=False).ask():
            with lib.lock:
                for k in stale:
                    (out / lib.d["songs"].pop(k)["file"]).unlink(missing_ok=True)
                lib.d["aliases"] = {k: v for k, v in lib.d["aliases"].items() if v not in stale}
                songs = dict(lib.d["songs"])

    el = time.time() - st.t0
    with st.lock:
        downloads, downloaded_bytes, failures = st.done, st.bytes, st.fail
    with lib.lock:
        s = lib.d["stats"]
        s["runs"] += 1
        s["downloads"] += downloads
        s["bytes"] += downloaded_bytes
        s["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        playlist_count = len(lib.d["playlists"])
    lib.save()
    ff = profile_dir(out) / "failed.txt"
    if failed:
        ff.write_text("\n".join(f"{a}\t{b}" for a, b in failed), encoding="utf-8")
    else:
        ff.unlink(missing_ok=True)

    head = "■ Stopped, progress saved. Run Update to continue." if stopped else "✔ Sync complete"
    body = [Text(head, style="bold #ff8c00" if stopped else "bold #22c55e"),
            Text(f"{downloads} new · {failures} failed · {size(downloaded_bytes)} · {dur(el)} · "
                 f"avg {size(downloaded_bytes / el if el else 0)}/s")]
    if not todo and not stopped:
        body.append(Text("Already up to date.", style="dim"))
    console.print(Panel(Group(*body), border_style="#ff8c00" if stopped else "#22c55e"))
    if rows:
        t = Table(border_style="#3a3a3a")
        for c in ("Playlist", "Songs", "Added", "Removed"):
            t.add_column(c, justify="left" if c == "Playlist" else "right")
        for n, c, a, r in rows:
            t.add_row(n, str(c), Text(f"+{a}", style="#22c55e"), Text(f"-{r}", style="#ef4444") if r else "-")
        console.print(t)
    if plan["bad"]:
        console.print(f"[dim]{plan['bad']} private/deleted videos skipped[/]")
    if failed:
        t = Table(title=f"Needs attention ({len(failed)}), saved to {ff}",
                  border_style="#ef4444", header_style="bold #ef4444")
        t.add_column("Item", no_wrap=True)
        t.add_column("Reason")
        for a, b in failed[:8]:
            t.add_row(Text(a[:45], style="#ef4444"), Text(b, style="#ef4444"))
        console.print(t)
        if sum(1 for _, r in failed if BLOCKED.search(r)) >= 2:
            console.print("[yellow]These errors usually mean yt-dlp is outdated.[/]")
            if interactive and console.is_terminal:
                if questionary.confirm("Update yt-dlp now?", default=True).ask():
                    upgrade_ytdlp()
            else:
                console.print("[dim]Run: ytsync --update-ytdlp[/]")
    console.print(Panel(f"{len(songs)} songs · {size(lib.total_size())} · {playlist_count} playlists\n"
                        f"All-time: {s['downloads']} downloads · {s['runs']} runs", title="Library",
                        border_style="#3a3a3a"))
    console.print("[dim]Run again anytime. Only new songs download.[/]")


def show_stats(cfg):
    lib = Lib(cfg["out"])
    s = lib.d["stats"]
    t = Table(title="Library stats", show_header=False, border_style="cyan")
    t.add_row("Folder", cfg["out"])
    t.add_row("Data folder", str(profile_dir(cfg["out"])))
    t.add_row("Songs", str(len(lib.d["songs"])))
    t.add_row("Size", size(lib.total_size()))
    t.add_row("All-time downloads", str(s["downloads"]))
    t.add_row("Runs", str(s["runs"]))
    t.add_row("Last sync", ago(s["last_run"]))
    aliases = lib.d["aliases"]
    for n, ids in lib.d["playlists"].items():
        kept = {canonical_id(i, aliases) for i in ids}
        t.add_row(f"  {n}", f"{sum(1 for i in kept if i in lib.d['songs'])} songs")
    console.print(t)


# ---------- interactive menus (every menu has "‹ Back"; Ctrl+C = back/exit) ----------
def ask(msg, choices):
    return questionary.select(msg, choices=[questionary.Choice(t, v) for t, v in choices],
                              style=STYLE, instruction=" ").ask()


def pause():
    questionary.press_any_key_to_continue("Press any key to continue...").ask()


def val(c, k):
    v = c[k]
    if isinstance(v, bool):
        return "on" if v else "off"
    return STYLES[v] if k == "name_style" else (str(v) or "-")


def save_named_links(path, links):
    path = Path(path).expanduser()
    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
    existing = set(read_list(path))
    new_links = []
    for url, name in links:
        if url not in existing:
            new_links.append((url, name))
            existing.add(url)
    if not new_links:
        console.print("[dim]No new links to save.[/]")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        if existing_text and not existing_text.endswith(("\n", "\r")):
            f.write("\n")
        for url, name in new_links:
            label = str(name).replace("\r", " ").replace("\n", " ").strip()
            f.write(f"# {label}\n{url}\n")
    console.print(f"[green]Saved {len(new_links)} link(s) to {path}.[/]")
    return len(new_links)


def save_my_links(cfg):
    urls = all_urls(cfg)
    if not urls:
        console.print("[dim]No saved links to write.[/]")
        return
    path = questionary.path("Save links to:", default=str(profile_dir(cfg["out"]) / "playlists.txt")).ask()
    if not path:
        return
    existing = set(read_list(path))
    urls = [url for url in urls if url not in existing]
    if not urls:
        console.print("[dim]All saved links are already in that file.[/]")
        return
    details, errs = {}, []
    resolve(urls, cfg, errs, details)
    for url, reason in errs:
        console.print(f"[red]Could not read {url}: {reason}[/]")
    save_named_links(path, [(url, details.get(url, (url, 0, None))[0]) for url in urls])


def add_links(cfg):
    c = ask("Add Music", [("Paste links", "paste"), ("Load from a file", "file"),
                          ("Save my links to a file", "save"), ("‹ Back", "back")])
    found = False
    if c == "paste":
        known, added, number = set(all_urls(cfg)), [], 1
        while True:
            raw = questionary.text(f"Link #{number} (Enter on empty line = Done)").ask()
            if raw is None:
                save_cfg(cfg)
                return False
            link = raw.strip()
            if not link:
                break
            number += 1
            parsed = urlparse(link)
            if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
                console.print("[red]Enter a valid http(s) URL.[/]")
                continue
            if link in known:
                console.print("[dim]Already saved; skipping duplicate link.[/]")
                continue
            details, errs = {}, []
            resolve([link], cfg, errs, details)
            if errs or details.get(link, (None, 0, "No playlist or song information found"))[2]:
                reason = errs[0][1] if errs else details[link][2]
                console.print(f"[red]Could not add link: {reason}[/]")
                continue
            name, count, _ = details[link]
            cfg["urls"].append(link)
            known.add(link)
            added.append((link, name))
            found = True
            console.print(f"[green]✔ {name} · {count} songs[/]")
        save_cfg(cfg)
        save_to_file = questionary.confirm("Save these links to a file too? [y/N]", default=False).ask() \
            if added else False
        if added and save_to_file is None:
            return False
        if save_to_file:
            path = questionary.path("Save links to:", default=str(profile_dir(cfg["out"]) / "playlists.txt")).ask()
            if not path:
                return False
            save_named_links(path, added)
    elif c == "file":
        p = questionary.path("File:", default="playlists.txt").ask()
        if p and Path(p).is_file():
            f = str(Path(p).resolve())
            if f not in cfg["files"]:
                cfg["files"].append(f)
            found = True
        elif p:
            console.print("[red]File not found.[/]")
            pause()
    elif c == "save":
        save_my_links(cfg)
    save_cfg(cfg)
    return found


def manage_links(cfg):
    items = cfg["urls"] + cfg["files"]
    if not items:
        console.print("[dim]No saved links.[/]")
        pause()
        return
    r = questionary.checkbox("Space = select · Enter = remove selected", choices=items, style=STYLE).ask()
    if r:
        cfg["urls"] = [u for u in cfg["urls"] if u not in r]
        cfg["files"] = [f for f in cfg["files"] if f not in r]


def open_playlists(cfg):
    p = profile_dir(cfg["out"]) / "playlists.txt"
    if not p.exists():
        p.write_text("# One YouTube playlist or song URL per line\n", encoding="utf-8")
    console.print(f"[dim]{p}[/]")
    try:
        if os.name == "nt":
            os.startfile(str(p))
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(p)])
    except Exception:
        console.print("[yellow]Could not open an editor. Edit the file above by hand.[/]")
    pause()


def open_data_folder(cfg):
    p = profile_dir(cfg["out"])
    try:
        if os.name == "nt":
            os.startfile(str(p))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except OSError as e:
        console.print(f"[yellow]Could not open the data folder: {e}\nData folder: {p}[/]")


def settings(cfg):
    while True:
        console.clear()
        console.print(f"[bold orange1]Settings[/]\n[dim]Data folder: {profile_dir(cfg['out'])}[/]\n")
        n = len(cfg["urls"]) + len(cfg["files"])
        k = ask("", [(f"{l:<22}{val(cfg, key)}", key) for key, l in LABELS] +
                [("Library folder", "library"), ("Open data folder", "open_data"),
                 ("Edit playlists.txt", "edit"), (f"Saved links ({n})", "links"),
                 ("Reset to defaults", "reset"), ("‹ Back", "back")])
        if k in (None, "back"):
            return cfg
        if k == "library":
            p = questionary.path("Music folder:", default=cfg["out"], only_directories=True).ask()
            if p:
                cfg = load_cfg(ensure_lib(p))
                return cfg
        elif k == "open_data":
            open_data_folder(cfg)
        elif k == "reset":
            cfg.update({**DEFAULTS, "urls": cfg["urls"], "files": cfg["files"]})
        elif k == "edit":
            open_playlists(cfg)
        elif k == "links":
            manage_links(cfg)
        elif isinstance(cfg[k], bool):
            cfg[k] = not cfg[k]
        elif k in CHOICES:
            v = ask(dict(LABELS)[k], [(str(STYLES.get(c, c)), c) for c in CHOICES[k]] + [("‹ Back", "back")])
            if v and v != "back":
                cfg[k] = v
        else:
            v = questionary.text(dict(LABELS)[k] + ":", default=str(cfg[k])).ask()
            if v is not None:
                cfg[k] = (int(v) if v.isdigit() else cfg[k]) if k == "retries" else v.strip()
        save_cfg(cfg)


def review(cfg, plan):
    console.clear()
    t = Table.grid(padding=(0, 2))
    bitrate = {"m4a": 128_000, "opus": 128_000, "aac": 128_000, "mp3": 245_000,
               "flac": 900_000, "wav": 1_411_000}
    if cfg["format"] == "mp3" and cfg["quality"] != "best":
        bitrate["mp3"] = int(cfg["quality"]) * 1000
    estimated_bytes = sum(int(e.get("dur") or 0) for e in plan["todo"]) * bitrate[cfg["format"]] / 8
    total_duration = sum(int(e.get("dur") or 0) for e in plan["unique"].values())
    in_library = len(plan["unique"]) - len(plan["todo"])
    t.add_row("Format", f"{cfg['format']} · {cfg['quality']} · {STYLES[cfg['name_style']]}")
    t.add_row("Library", str(plan["out"]))
    t.add_row("Playlist entries",
              f"{plan['entries']} → {len(plan['unique'])} unique songs "
              f"({plan['repeats']} repeats, {plan['duplicate_count']} same-song duplicates skipped)")
    t.add_row("", Text.assemble((f"✚ {len(plan['todo'])} new", "#22c55e"),
                                (f"   ✔ {in_library} in library", "#22c55e"),
                                (f"   ✖ {plan['bad']} private", "#ef4444")))
    if plan["errs"]:
        t.add_row("Problems", Text(f"{len(plan['errs'])} link(s) failed to load", style="#ef4444"))
    t.add_row("Duration", dur(total_duration))
    t.add_row("Estimate", f"~{size(estimated_bytes)}")
    console.print(Panel(t, title="Ready", border_style="#22c55e", expand=False))
    per_playlist = Table(title="Playlist counts", border_style="#3a3a3a")
    for column in ("Playlist", "Entries", "Unique songs", "Repeats", "Same-song skipped"):
        per_playlist.add_column(column, justify="left" if column == "Playlist" else "right")
    for playlist in plan["pls"]:
        ids = [e["id"] for e in playlist["items"]]
        unique_ids = set(ids)
        canonical = {canonical_id(i, plan["aliases"]) for i in unique_ids}
        per_playlist.add_row(playlist["name"], str(len(ids)), str(len(canonical)),
                             str(len(ids) - len(unique_ids)),
                             str(sum(1 for i in unique_ids if i in plan["aliases"])))
    if plan["pls"]:
        console.print(per_playlist)
    return ask("", [("Start", "start"), ("Edit settings", "edit"), ("‹ Back", "back"), ("✕ Exit", "exit")])


def run_flow(cfg, with_review):
    while True:
        urls = all_urls(cfg)
        if not urls:
            console.print("[yellow]No links yet. Use 'Add Music'.[/]")
            pause()
            return "home"
        plan = scan(cfg, urls)
        a = review(cfg, plan) if with_review else "start"
        if not with_review:
            console.print(f"[bold]{len(plan['todo'])} new[/] · "
                          f"{len(plan['unique']) - len(plan['todo'])} in library\n")
        if a == "start":
            execute(cfg, plan, interactive=True, prune_ok=True)
            pause()
            return "home"
        if a == "edit":
            settings(cfg)
            continue
        return "exit" if a == "exit" else "home"


def header(cfg, upd):
    lib = Lib(cfg["out"])
    console.print(Panel(f"[bold orange1]♪  ytmusic-sync[/]  v{VERSION}\n[dim]YouTube music + Local, always in sync[/]",
                        border_style="orange1", expand=False))
    console.print(f" {cfg['out']} · {len(lib.d['songs'])} songs · {size(lib.total_size())}\n"
                  f" Last sync: {ago(lib.d['stats']['last_run'])}")
    if upd:
        console.print(f" [yellow]⚠ yt-dlp update available ({upd[0]} → {upd[1]})[/]")
    console.print()


def pick_library():
    console.print(Panel("[bold orange1]Welcome![/]\nWhere should your music library live?\n"
                        "[dim]Songs and .m3u8 playlists go here; app data is stored in your user profile.[/]",
                        border_style="orange1", expand=False))
    p = questionary.path("Music folder:", default=str(Path.home() / "music"), only_directories=True).ask()
    return Path(p).expanduser() if p else None


def interactive(out):
    if out is None:
        out = pick_library()
        if out is None:
            return
    cfg = load_cfg(ensure_lib(out))
    upd = outdated(cfg["out"])
    while True:
        console.clear()
        header(cfg, upd)
        first = not all_urls(cfg)
        menu = [("Use defaults & start", "go"), ("Customize", "set"), ("Quit", "q")] if first else \
               [("Update library", "upd"), ("Add Music", "add"), ("Settings", "set"), ("Stats", "stats")]
        if upd:
            menu.append((f"Update yt-dlp ({upd[0]} → {upd[1]})", "ytdlp"))
        if not first:
            menu.append(("Quit", "q"))
        k = ask("", menu)
        if k in (None, "q"):
            return
        if k == "set":
            cfg = settings(cfg)
            upd = outdated(cfg["out"])
        elif k == "stats":
            show_stats(cfg)
            pause()
        elif k == "ytdlp":
            upgrade_ytdlp()
            pause()
            return
        elif k in ("go", "add"):
            if add_links(cfg) and run_flow(cfg, True) == "exit":
                return
        elif k == "upd" and run_flow(cfg, False) == "exit":
            return


# ---------- command line ----------
def main():
    ap = argparse.ArgumentParser(prog="ytsync", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Download YouTube songs/playlists with clean tags and keep .m3u8 playlists in sync.\n"
                    "Run with no arguments for interactive mode.",
        epilog="Examples:\n  ytsync                     interactive menu\n  ytsync -u                  update library (saved links + settings)\n"
               "  ytsync <url> --save        download a link and remember it\n  ytsync <url> -f flac       one-off format\n"
               "  ytsync -p playlists.txt    use a links file\n  ytsync -s                  library stats\n")
    ap.add_argument("urls", nargs="*", help="playlist or song URLs (this run only)")
    g = ap.add_argument_group("run")
    g.add_argument("-u", "--update", action="store_true", help="sync saved links with saved settings, no menus")
    g.add_argument("-p", "--playlists", metavar="FILE", help="text file, one URL per line")
    g.add_argument("--save", action="store_true", help="remember the given links for future --update runs")
    g.add_argument("-d", "--dry-run", action="store_true", help="show what would download")
    g.add_argument("-s", "--stats", action="store_true", help="show library stats only")
    g.add_argument("-o", "--out", "--library", metavar="DIR", help="music library folder")
    g.add_argument("--where", action="store_true", help="print the profile and music folder paths, then exit")
    g = ap.add_argument_group("output (one-off, not saved)")
    g.add_argument("-f", "--format", choices=FORMATS)
    g.add_argument("-q", "--quality", choices=QUALITIES)
    g.add_argument("-n", "--name-style", choices=list(STYLES), dest="name_style")
    g.add_argument("--no-cover", action="store_true")
    g.add_argument("--no-playlist-files", action="store_true", help="skip .m3u8 files")
    g.add_argument("--prune", action="store_true", help="with --update: delete songs no longer in any playlist (asks first)")
    g = ap.add_argument_group("network")
    g.add_argument("--cookies-from-browser", dest="cookies", metavar="NAME", help="chrome, firefox, edge... (private playlists)")
    g.add_argument("--rate-limit", dest="rate_limit", metavar="RATE", help="e.g. 2M")
    g.add_argument("--retries", type=int)
    g.add_argument("--jobs", type=int, choices=range(1, 7), help="parallel downloads (1-6, default 3)")
    g = ap.add_argument_group("other")
    g.add_argument("--compact", action="store_true", help="one line per song instead of the dashboard")
    g.add_argument("--update-ytdlp", action="store_true", help="upgrade yt-dlp and exit")
    g.add_argument("-V", "--version", action="version", version=f"%(prog)s {VERSION} (yt-dlp {YTDLP_VERSION})")
    a = ap.parse_args()

    try:
        if a.update_ytdlp:
            return upgrade_ytdlp()
        out = find_lib(a.out)
        direct = bool(a.urls or a.playlists or a.update or a.stats or a.dry_run or a.where)
        if not direct:
            if not sys.stdin.isatty():
                return ap.print_help()
            if need_ffmpeg():
                interactive(out)
            return
        if out is None:
            if sys.stdin.isatty():
                out = pick_library()
            if out is None:
                console.print("[yellow]No library folder selected. Run [bold]ytsync[/bold] to pick one, or pass -o FOLDER.[/]")
                return 1
        out = ensure_lib(out)
        if a.where:
            console.print(f"Profile folder: {profile_dir(out)}\nLibrary folder: {out}")
            return 0
        cfg = load_cfg(out)
        for k in ("format", "quality", "name_style", "cookies", "rate_limit", "retries", "jobs"):
            if getattr(a, k) is not None:
                cfg[k] = getattr(a, k)
        if a.no_cover:
            cfg["cover"] = False
        if a.no_playlist_files:
            cfg["playlist_files"] = False
        if a.prune:
            cfg["prune"] = True
        if a.stats:
            return show_stats(cfg)
        upd = outdated(cfg["out"])
        if upd:
            console.print(f"[yellow]⚠ yt-dlp {upd[0]} → {upd[1]} available. Run: ytsync --update-ytdlp[/]")
        urls = a.urls + (read_list(a.playlists) if a.playlists else [])
        if a.save and a.urls:
            cfg["urls"] += [u for u in a.urls if u not in cfg["urls"]]
            save_cfg(cfg)
        if a.update:
            urls += all_urls(cfg)
        urls = list(dict.fromkeys(urls))
        if not urls:
            return console.print("[yellow]No links saved yet. Run ytsync, or: ytsync <url> --save[/]")
        if not a.dry_run and not need_ffmpeg():
            return
        plan = scan(cfg, urls)
        if a.dry_run:
            for e in plan["todo"]:
                console.print(f"  [green]+[/] {e['title']}")
            return console.print(f"\n[yellow]Dry run: {len(plan['todo'])} new, nothing downloaded.[/]")
        execute(cfg, plan, compact=a.compact, prune_ok=a.update)
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/]")
    except OSError as e:
        console.print(f"[red]Could not access the music or data folder: {e}[/]")
        console.print("[dim]Check that the music folder is connected and writable.[/]")
        return 1


if __name__ == "__main__":
    main()
