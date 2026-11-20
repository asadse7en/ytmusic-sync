#!/usr/bin/env python3
"""ytmusic-sync: YouTube playlists -> clean, tagged music library + .m3u8 playlists."""
import argparse, contextlib, hashlib, json, logging, os, queue, re, shutil, subprocess, sys, threading, time, urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime
from pathlib import Path
import unicodedata
import signal
from urllib.parse import parse_qs, urlparse

for _s in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _s.reconfigure(encoding="utf-8")

VERSION = "0.1"
DATA = Path.home() / ".ytmusic-sync"
console = questionary = STYLE = None
Group = Live = Panel = Table = Text = None
LIB_CACHE, LIB_CACHE_LOCK = {}, threading.RLock()
TIMING_ENABLED = False
TIMINGS = {}
UPDATE_CHECKER = None
_YT_DLP = None
_POSTPROCESSOR_TYPES = None
_ANSI_ENABLED = False

FORMATS = ["mp3", "m4a", "opus", "flac", "wav", "aac"]
QUALITIES = ["best", "320", "256", "192", "128"]
STYLES = {"title": "Title", "artist-title": "Artist - Title", "title-artist": "Title - Artist"}
EXT = {".m4a", ".mp3", ".opus", ".flac", ".wav", ".aac", ".ogg", ".mp4"}
DEFAULTS = dict(format="mp3", quality="best", name_style="title", cover=True, square=True, clean=True,
                playlist_files=True, prune=False, skip_duplicate_songs=True, jobs=3, retries=5,
                rate_limit="", cookies="")
LABELS = [("format", "Format"), ("quality", "Quality"), ("name_style", "Filename style"),
          ("cover", "Cover art"), ("square", "Square cover"), ("clean", "Clean titles"),
          ("playlist_files", "Playlist .m3u8 files"), ("prune", "Prune removed songs"),
          ("skip_duplicate_songs", "Skip duplicate songs"), ("jobs", "Parallel downloads"),
          ("rate_limit", "Speed limit"), ("retries", "Retries"), ("cookies", "Browser cookies")]
CHOICES = {"format": FORMATS, "quality": QUALITIES, "name_style": list(STYLES), "jobs": list(range(1, 7))}

JUNK = re.compile(r"[\(\[\{][^\)\]\}]*\b(official|lyrics?|lyrical|audio|video|visuali[sz]er|hd|hq|4k|"
                  r"full song|music video|mv)\b[^\)\]\}]*[\)\]\}]", re.I)
EMOJI = re.compile("[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f]")
BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
YT_ID = re.compile(r"(?:v=|youtu\.be/)([\w-]{11})")
BLOCKED = re.compile(r"403|sign in|bot|nsig|unable to extract|HTTP Error", re.I)


def ensure_ui():
    global console, questionary, STYLE, Group, Live, Panel, Table, Text
    enable_terminal_ansi()
    if console is not None:
        return
    try:
        import questionary as _questionary
        from rich.console import Console, Group as _Group
        from rich.live import Live as _Live
        from rich.panel import Panel as _Panel
        from rich.table import Table as _Table
        from rich.text import Text as _Text
    except ImportError as e:
        raise RuntimeError("Missing UI packages. Run: pip install -r requirements.txt") from e
    questionary = _questionary
    Group, Live, Panel, Table, Text = _Group, _Live, _Panel, _Table, _Text
    console = Console()
    STYLE = questionary.Style([("pointer", "fg:#ff8c00 bold"), ("highlighted", "fg:#ff8c00 bold"),
                               ("selected", "fg:#ff8c00"), ("question", "bold")])


def enable_terminal_ansi():
    global _ANSI_ENABLED
    if not _ANSI_ENABLED:
        _ANSI_ENABLED = True
        if os.name == "nt":
            with contextlib.suppress(Exception):
                os.system("")


def get_ytdlp():
    global _YT_DLP
    if _YT_DLP is None:
        try:
            import yt_dlp
        except ImportError as e:
            raise RuntimeError("Missing yt-dlp. Run: pip install -r requirements.txt") from e
        _YT_DLP = yt_dlp
        install_ytdlp_process_tracking()
    return _YT_DLP


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


def clean_reason(reason):
    value = str(reason or "").strip()
    prefix = re.compile(r"^(?:\[youtube\]\s*[\w-]+:\s*|ERROR:\s*|Postprocessing:\s*)", re.I)
    while prefix.match(value):
        value = prefix.sub("", value, count=1).strip()
    return value[:80]


def status_glyph(ok):
    stream = getattr(console, "file", sys.stdout) if console is not None else sys.stdout
    encoding = (getattr(stream, "encoding", None) or getattr(sys.stdout, "encoding", "") or "")
    encoding = encoding.lower().replace("_", "-")
    if encoding in ("utf-8", "utf8"):
        return "✓" if ok else "✗"
    return "OK" if ok else "ERR"


def fsafe(s, n=120):
    return BAD.sub("", s).strip(" .")[:n].strip() or "track"


def read_list(p):
    try:
        return [l.strip() for l in Path(p).read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.strip().startswith("#")]
    except Exception:
        return []


# ---------- data location, config ----------
def ensure_data_dir():
    DATA.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        try:
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(DATA))
            if attrs != 0xFFFFFFFF and not attrs & 0x2:
                if not ctypes.windll.kernel32.SetFileAttributesW(str(DATA), attrs | 0x2):
                    raise ctypes.WinError()
        except (AttributeError, OSError) as e:
            logging.warning("Could not hide data folder %s: %s", DATA, e)
    return DATA


def music_hash(out):
    resolved = Path(out).expanduser().resolve()
    return hashlib.sha1(os.path.normcase(str(resolved)).encode("utf-8")).hexdigest()


def legacy_profile_name(out):
    resolved = Path(out).expanduser().resolve()
    digest = hashlib.sha1(str(resolved).lower().encode("utf-8")).hexdigest()[:8]
    return f"{fsafe(resolved.name)}-{digest}"


def library_path(out):
    ensure_data_dir()
    folder = DATA / "libs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{music_hash(out)}.json"


def copy_legacy_file(source, destination):
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copy2(source, destination)
        return destination
    backup = DATA / "migrations" / hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:12] / source.name
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup)
        return backup
    return None


def append_urls(urls):
    path = DATA / "playlists.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = set(read_list(path))
    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
    added = []
    for value in urls:
        url = str(value).strip()
        if url and url not in existing:
            added.append(url)
            existing.add(url)
    if added:
        with path.open("a", encoding="utf-8") as f:
            if existing_text and not existing_text.endswith(("\n", "\r")):
                f.write("\n")
            f.writelines(f"{url}\n" for url in added)


def append_legacy_playlist(source):
    source = Path(source)
    if not source.is_file():
        return
    target = DATA / "playlists.txt"
    if not target.exists():
        copy_legacy_file(source, target)
        return
    existing_text = target.read_text(encoding="utf-8")
    existing = set(read_list(target))
    pairs, label = [], ""
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#"):
            label = line.strip()[1:].strip()
            if label.lower() == "one link per line":
                label = ""
        elif line.strip():
            pairs.append((line.strip(), label))
            label = ""
    added = [(url, name) for url, name in pairs if url not in existing]
    if added:
        with target.open("a", encoding="utf-8") as f:
            if existing_text and not existing_text.endswith(("\n", "\r")):
                f.write("\n")
            for url, name in added:
                if name:
                    f.write(f"# {name}\n")
                f.write(f"{url}\n")


def legacy_config_links(config):
    urls = config.get("urls") or []
    files = config.get("files") or []
    links = [urls] if isinstance(urls, str) else list(urls)
    for path in ([files] if isinstance(files, str) else files):
        links.extend(read_list(path))
    return links


def migrate_legacy_profiles():
    ensure_data_dir()
    marker = DATA / "migrations" / "platformdirs-v1.done"
    if marker.exists():
        return
    try:
        from platformdirs import user_config_dir, user_data_dir
        old_base = Path(user_data_dir("ytsync", appauthor=False)) / "libraries"
        pointer = Path(user_config_dir("ytsync", appauthor=False)) / "last_library.json"
    except ImportError:
        return

    folders = sorted((p for p in old_base.iterdir() if p.is_dir()), key=lambda p: p.name.lower()) \
        if old_base.is_dir() else []
    pointed = jload(pointer, {}).get("path")
    if pointed:
        active = legacy_profile_name(pointed)
        folders.sort(key=lambda p: (p.name != active, p.name.lower()))
    links = []
    config_path = DATA / "config.json"
    for folder in folders:
        source_cfg = jload(folder / "config.json", {})
        if source_cfg:
            links.extend(legacy_config_links(source_cfg))
            copy_legacy_file(folder / "config.json",
                             DATA / "migrations" / "legacy-profiles" / folder.name / "config.json")
            if not config_path.exists():
                migrated_cfg = {k: v for k, v in source_cfg.items() if k in DEFAULTS}
                aw(config_path, json.dumps({**DEFAULTS, **migrated_cfg}, indent=1))
        append_legacy_playlist(folder / "playlists.txt")
        copy_legacy_file(folder / "library.json", DATA / "libs" / f"{folder.name}.json")
        if pointed and folder.name == active:
            copy_legacy_file(folder / "library.json", library_path(pointed))
        for name in ("failed.txt", "sync.log", "cache.json"):
            copy_legacy_file(folder / name, DATA / name)
    if pointed:
        config = jload(config_path, {})
        if not config.get("out"):
            config["out"] = str(pointed)
            aw(config_path, json.dumps({**DEFAULTS, **config}, indent=1))
    append_urls(links)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("done\n", encoding="utf-8")


def migrate_music_folder(out):
    marker = DATA / "migrations" / f"music-{music_hash(out)}.done"
    if marker.exists():
        return
    legacy = Path(out) / ".ytmusic-sync"
    if legacy.is_dir():
        source_cfg = jload(legacy / "config.json", {})
        if source_cfg:
            links = legacy_config_links(source_cfg)
            config_path = DATA / "config.json"
            copy_legacy_file(legacy / "config.json",
                             DATA / "migrations" / "music-folders" / music_hash(out) / "config.json")
            if not config_path.exists():
                migrated_cfg = {k: v for k, v in source_cfg.items() if k in DEFAULTS}
                aw(config_path, json.dumps({**DEFAULTS, **migrated_cfg}, indent=1))
            append_urls(links)
        append_legacy_playlist(legacy / "playlists.txt")
        copy_legacy_file(legacy / "library.json", library_path(out))
        for name in ("failed.txt", "sync.log", "cache.json"):
            copy_legacy_file(legacy / name, DATA / name)
        for item in legacy.iterdir():
            if item.is_file() and item.name not in {
                    "config.json", "playlists.txt", "library.json", "failed.txt", "sync.log", "cache.json"}:
                copy_legacy_file(item, DATA / item.name)
    for old, new in (("library.json", library_path(out)), ("failed.txt", DATA / "failed.txt"),
                     ("ytmusic-sync.log", DATA / "sync.log")):
        copy_legacy_file(Path(out) / old, new)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("done\n", encoding="utf-8")


def ensure_lib(out):
    """Validate a music folder and import its legacy data without modifying the source."""
    out = Path(out).expanduser().resolve()
    if not out.exists():
        if not out.parent.is_dir():
            raise FileNotFoundError(f"Music folder is unavailable: {out}")
        out.mkdir()
    if not out.is_dir():
        raise NotADirectoryError(f"Music folder is not a directory: {out}")
    ensure_data_dir()
    migrate_legacy_profiles()
    migrate_music_folder(out)
    return out


def find_lib(explicit=None):
    if explicit:
        return Path(explicit).expanduser().resolve()
    saved = jload(DATA / "config.json", {}).get("out")
    if saved:
        folder = Path(saved).expanduser()
        if folder.is_dir():
            return folder.resolve()
        if console is not None:
            console.print(f"[yellow]Last used music folder is unavailable: {folder}. "
                          "Reconnect the drive or choose a folder.[/]")
    return None


def load_cfg(out=None):
    ensure_data_dir()
    c = {**DEFAULTS, **jload(DATA / "config.json", {})}
    c.pop("urls", None)
    c.pop("files", None)
    c["jobs"] = int(c["jobs"])
    if not 1 <= c["jobs"] <= 6:
        raise ValueError("Parallel downloads must be between 1 and 6.")
    saved = out if out is not None else c.get("out")
    c["out"] = str(Path(saved).expanduser().resolve()) if saved else ""
    return c


def save_cfg(c):
    ensure_data_dir()
    values = {k: v for k, v in c.items() if k in DEFAULTS or k == "out"}
    aw(DATA / "config.json", json.dumps(values, indent=1))


def all_urls(_cfg=None):
    return list(dict.fromkeys(read_list(DATA / "playlists.txt")))


# ---------- ffmpeg + yt-dlp updates ----------
FFMPEG = None  # path to a pip-provided ffmpeg, used when none is installed system-wide
CHILD_PROCESSES = []
CHILD_PROCESS_LOCK = threading.Lock()
YTDLP_PROCESS_TRACKED = False
YTDLP_PROCESS_LOCK = threading.Lock()


def register_child_process(process):
    with CHILD_PROCESS_LOCK:
        CHILD_PROCESSES.append(process)


def unregister_child_process(process):
    with CHILD_PROCESS_LOCK:
        with contextlib.suppress(ValueError):
            CHILD_PROCESSES.remove(process)


def install_ytdlp_process_tracking():
    global YTDLP_PROCESS_TRACKED
    with YTDLP_PROCESS_LOCK:
        if YTDLP_PROCESS_TRACKED:
            return
        import yt_dlp.postprocessor.ffmpeg as ffmpeg_postprocessor

        base_process = ffmpeg_postprocessor.Popen

        class TrackedPopen(base_process):
            _ytmusic_sync_tracked = True

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                register_child_process(self)

            def wait(self, *args, **kwargs):
                try:
                    return super().wait(*args, **kwargs)
                finally:
                    if self.returncode is not None:
                        unregister_child_process(self)

        ffmpeg_postprocessor.Popen = TrackedPopen
        YTDLP_PROCESS_TRACKED = True


def terminate_child_processes(timeout=2, force=False, processes=None):
    if processes is None:
        with CHILD_PROCESS_LOCK:
            active = [process for process in CHILD_PROCESSES if process.poll() is None]
            CHILD_PROCESSES[:] = active
    else:
        active = [process for process in processes if process.poll() is None]

    for process in active:
        with contextlib.suppress(Exception):
            process.terminate()

    if not force:
        deadline = time.monotonic() + max(timeout, 0)
        while active and time.monotonic() < deadline:
            active = [process for process in active if process.poll() is None]
            if active:
                time.sleep(min(0.05, max(deadline - time.monotonic(), 0)))

    for process in active:
        if process.poll() is None and os.name == "nt":
            with contextlib.suppress(Exception):
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.3,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with contextlib.suppress(Exception):
            if process.poll() is None:
                process.kill()


def tracked_run(args, stop_event=None, capture_output=False, **kwargs):
    if capture_output:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    process = subprocess.Popen(args, **kwargs)
    register_child_process(process)
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                terminate_child_processes(timeout=0.2, processes=[process])
                raise StopRequested()
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    finally:
        unregister_child_process(process)


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


def installed_ytdlp_version():
    try:
        from importlib.metadata import version
        return version("yt-dlp")
    except Exception:
        return "0"


def ytdlp_latest():
    cp = DATA / "cache.json"
    cached = jload(cp, {})
    if "latest" in cached and time.time() - cached.get("t", 0) < 86400:
        return cached["latest"] or None
    latest = None
    try:
        with urllib.request.urlopen("https://pypi.org/pypi/yt-dlp/json", timeout=2) as r:
            latest = json.load(r)["info"]["version"]
    except Exception:
        pass
    with contextlib.suppress(OSError):
        aw(cp, json.dumps({"t": time.time(), "latest": latest or ""}))
    return latest


def outdated(_out=None):
    current = installed_ytdlp_version()
    latest = ytdlp_latest()
    return (current, latest) if latest and vtuple(latest) > vtuple(current) else None


class UpdateChecker:
    def __init__(self):
        self.started = time.monotonic()
        self.finished = None
        self.result = None
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, name="yt-dlp-update-check", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            self.result = outdated()
        except Exception:
            logging.exception("yt-dlp update check failed")
        finally:
            self.finished = time.monotonic()
            self.done.set()

    @property
    def elapsed(self):
        end = self.finished if self.finished else time.monotonic()
        return end - self.started


def start_update_check():
    global UPDATE_CHECKER
    UPDATE_CHECKER = UpdateChecker()
    return UPDATE_CHECKER


def upgrade_ytdlp():
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"])
    console.print("[green]yt-dlp updated. Restart ytsync to use it.[/]" if r.returncode == 0
                  else "[red]Update failed. Try: pip install -U yt-dlp[/]")
    return r.returncode == 0


# ---------- tag + filename cleaning ----------
def tidy(s):
    s = JUNK.sub("", EMOJI.sub("", str(s or "")))
    s = re.sub(r"\s*[|｜].*$", "", s)
    return re.sub(r"\s+", " ", s).strip(" -–—_")


def first_text(value, default=""):
    if isinstance(value, str):
        return value.strip() or default
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
        return default
    return str(value).strip() if value is not None else default


def clean_tags(i, clean):
    if not isinstance(i, dict):
        return "Unknown", "Unknown"
    fallback = first_text(i.get("id"), "Unknown")
    title = first_text(i.get("track"), first_text(i.get("title"), fallback))
    artist = first_text(i.get("artist"), first_text(i.get("creator")))
    if clean:
        title = tidy(title) or title
    if not artist:
        m = re.match(r"^(.+?)\s+[-–—]\s+(.+)$", title)
        if m:
            artist_part, title_part = m.groups()
            artist, title = tidy(artist_part), tidy(title_part)
        else:
            channel = first_text(i.get("channel"), first_text(i.get("uploader"), "Unknown"))
            artist = re.sub(r"\s*(-\s*Topic|VEVO)$", "", channel, flags=re.I)
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


def postprocessor_types():
    global _POSTPROCESSOR_TYPES
    if _POSTPROCESSOR_TYPES is not None:
        return _POSTPROCESSOR_TYPES
    get_ytdlp()
    from yt_dlp.postprocessor.common import PostProcessor

    class Clean(PostProcessor):
        def __init__(self, lib, cfg, st, stop_event):
            super().__init__()
            self.lib, self.cfg, self.st, self.stop_event = lib, cfg, st, stop_event

        def run(self, info):
            if self.stop_event.is_set():
                raise StopRequested()
            a, t = clean_tags(info, self.cfg["clean"])
            info.update(artist=a, track=t, title=t, album_artist=a.partition(",")[0].strip())
            info["fname"] = pick_name(self.lib, info["id"], a, t, self.cfg["name_style"])
            self.st.set_track(t, a)
            return [], info

    class SquareCover(PostProcessor):
        def __init__(self, stop_event):
            super().__init__()
            self.stop_event = stop_event

        def run(self, info):
            if self.stop_event.is_set():
                raise StopRequested()
            exe = FFMPEG or shutil.which("ffmpeg")
            for thumbnail in info.get("thumbnails") or []:
                if self.stop_event.is_set():
                    raise StopRequested()
                src = thumbnail.get("filepath")
                if not exe or not src or not os.path.exists(src):
                    continue
                tmp = src + ".sq.jpg"
                r = tracked_run([exe, "-y", "-loglevel", "error", "-i", src,
                                 "-vf", "crop='min(iw,ih)':'min(iw,ih)'", "-c:v", "mjpeg", "-q:v", "2", tmp],
                                stop_event=self.stop_event, capture_output=True)
                if r.returncode == 0 and os.path.exists(tmp):
                    os.replace(tmp, src)
                else:
                    logging.warning("cover crop failed: %s", r.stderr.decode(errors="ignore")[-200:])
            return [], info

    _POSTPROCESSOR_TYPES = Clean, SquareCover
    return _POSTPROCESSOR_TYPES


# ---------- library database ----------
def tag_has_url(k, v):
    """True for tags that hold the source URL (comment/purl), or a description that is only a URL."""
    k, v = str(k).lower(), str(v)
    if any(x in k for x in ("cmt", "comm", "purl")):
        return True
    return "description" in k and bool(re.fullmatch(r"\W*https?://[^\s'\"]+\W*", v))


class Lib:
    def __init__(self, out):
        ensure_ui()
        self.out, self.f = Path(out).expanduser().resolve(), library_path(out)
        self.lock, self.name_lock, self.reserved_names = threading.RLock(), threading.Lock(), set()
        self.reserved_by_id = {}
        self.d = {"songs": {}, "playlists": {}, "stats": {}, "aliases": {}}
        old_db = DATA / "libs" / f"{legacy_profile_name(out)}.json"
        if not self.f.exists() and old_db.is_file():
            shutil.copy2(old_db, self.f)
        database_exists = self.f.is_file()
        loaded = jload(self.f, {}) if database_exists else {}
        if isinstance(loaded, dict):
            self.d.update(loaded)
        self.d["songs"] = self.d.get("songs") if isinstance(self.d.get("songs"), dict) else {}
        self.d["playlists"] = self.d.get("playlists") if isinstance(self.d.get("playlists"), dict) else {}
        self.d["stats"] = self.d.get("stats") if isinstance(self.d.get("stats"), dict) else {}
        self.d["aliases"] = self.d.get("aliases") if isinstance(self.d.get("aliases"), dict) else {}
        self.d.setdefault("aliases", {})
        self.d["stats"] = {"runs": 0, "downloads": 0, "bytes": 0, "last_run": "never", **self.d["stats"]}
        if not database_exists:
            with console.status("[cyan]Reading existing files..."):
                self.rebuild()
        self.d["songs"] = {k: v for k, v in self.d["songs"].items()
                           if isinstance(v, dict) and v.get("file") and (self.out / v["file"]).exists()}
        self.d["aliases"] = {k: v for k, v in self.d["aliases"].items() if v in self.d["songs"]}
        if not database_exists:
            self.save()

    def rebuild(self):
        """Best effort: recover video IDs from the source URL stored in each file's tags."""
        import mutagen
        known_files = {Path(v["file"]).name for v in self.d["songs"].values() if v.get("file")}
        for p in self.out.iterdir():
            if p.name in known_files or p.suffix.lower() not in EXT or not p.is_file():
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
                known_files.add(p.name)
            except Exception:
                logging.exception("Could not read tags from %s", p)

    def save(self):
        with self.lock:
            self.f.parent.mkdir(parents=True, exist_ok=True)
            aw(self.f, json.dumps(self.d, indent=1, ensure_ascii=False))

    def total_size(self):
        with self.lock:
            return sum((self.out / v["file"]).stat().st_size for v in self.d["songs"].values()
                       if (self.out / v["file"]).exists())


def get_lib(out):
    key = os.path.normcase(str(Path(out).expanduser().resolve()))
    with LIB_CACHE_LOCK:
        if key not in LIB_CACHE:
            LIB_CACHE[key] = Lib(out)
        return LIB_CACHE[key]


# ---------- scan ----------
def resolve(urls, cfg, errs, details=None):
    yt_dlp = get_ytdlp()
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
    lib, errs = get_lib(out), []
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


class FirstInterrupt(Exception):
    pass


class ForceInterrupt(Exception):
    pass


class WorkerStatus:
    def __init__(self, state, worker_id):
        self.state, self.worker_id = state, worker_id

    def set_track(self, title, artist):
        self.state.set_track(self.worker_id, title, artist)


def build_overall_panel(state, width):
    from rich import box
    from rich.cells import cell_len

    with state.lock:
        elapsed = max(time.time() - state.t0, 0)
        speed_data_seconds = (time.monotonic() - state.speed_started
                              if state.speed_started is not None else 0)
        done, failed, total = state.done, state.fail, state.total
        completed_bytes = state.bytes
        workers = [dict(worker) for worker in state.workers.values()]

    active_workers = [worker for worker in workers if worker.get("active")]
    active = len(active_workers)
    left = max(total - done - failed - active, 0)
    finished = done + failed
    percent = min(finished / total, 1) if total else 0
    panel_width = max(width - int(getattr(console, "legacy_windows", False)), 1)
    inner_width = max(panel_width - 4, 0)
    percent_text = Text(f"{percent:.1%}", style="bold #ff9f1a")
    count_text = Text(f"{finished} / {total}", style="#8b8b8b")
    bar_width = max(inner_width - cell_len(percent_text.plain) - cell_len(count_text.plain) - 2, 0)

    counts = [done, failed, active, left]
    denominator = sum(counts)
    if denominator:
        widths, remainders = [], []
        for count in counts:
            quotient, remainder = divmod(max(count, 0) * bar_width, denominator)
            widths.append(quotient)
            remainders.append(remainder)
        for index in sorted(range(len(widths)), key=lambda i: remainders[i], reverse=True)[
                :bar_width - sum(widths)]:
            widths[index] += 1
    else:
        widths = [0, 0, 0, bar_width]
    for index, count in enumerate(counts):
        if count > 0 and widths[index] == 0:
            donor = max((i for i, segment_width in enumerate(widths) if segment_width > 1),
                        key=lambda i: widths[i], default=None)
            if donor is not None:
                widths[donor] -= 1
                widths[index] = 1

    bar = Text()
    for segment_width, color in zip(widths, ("#3fb950", "#f85149", "#ff9f1a", "#333333")):
        bar.append("━" * segment_width, style=color)
    row = Text()
    row.append(percent_text)
    row.append(" ")
    row.append(bar)
    row.append(" ")
    row.append(count_text)

    bytes_so_far = completed_bytes + sum(max(worker.get("dl", 0), 0) for worker in active_workers)
    speed = sum(max(worker.get("speed", 0), 0) for worker in active_workers)
    total_size = (f"{bytes_so_far / (1024 ** 3):.1f} GB"
                  if bytes_so_far >= 1024 ** 3 else size(bytes_so_far))
    completed_song_size = completed_bytes / done if done else 0
    if not completed_song_size:
        known_sizes = [worker.get("total", 0) for worker in active_workers if worker.get("total", 0) > 0]
        completed_song_size = sum(known_sizes) / len(known_sizes) if known_sizes else 0
    remaining_bytes = completed_song_size * left
    for worker in active_workers:
        if worker.get("total", 0) > 0:
            remaining_bytes += max(worker["total"] - worker.get("dl", 0), 0)
        else:
            remaining_bytes += completed_song_size

    eta = "—"
    if speed_data_seconds >= 5:
        if finished >= total:
            eta = dur(0)
        elif speed > 0 and remaining_bytes > 0:
            eta = dur(remaining_bytes / speed)
    if speed >= 1024 * 1024:
        speed_text = f"{speed / (1024 * 1024):.1f} MB/s"
    else:
        speed_text = f"{speed / 1024:.0f} KB/s"

    items = {
        "done": ("dot", done, "done", "#3fb950"),
        "failed": ("dot", failed, "failed", "#f85149"),
        "active": ("dot", active, "active", "#ff9f1a"),
        "size": ("value", total_size, None, None),
        "speed": ("value", speed_text, None, None),
        "eta": ("label", "ETA", eta, None),
        "elapsed": ("label", "Elapsed", dur(elapsed), None),
    }
    included = list(items)

    def make_stats(spacing):
        line = Text()
        for name in included:
            if line.plain:
                line.append(" " * spacing)
            kind, value, label, color = items[name]
            if kind == "dot":
                line.append("● ", style=color)
                line.append(str(value), style="#e6e6e6")
                line.append(f" {label}", style="#8b8b8b")
            elif kind == "label":
                line.append(f"{value} ", style="#8b8b8b")
                line.append(str(label), style="#e6e6e6")
            else:
                line.append(str(value), style="#e6e6e6")
        return line

    stats = make_stats(3)
    for name in ("elapsed", "eta", "speed", "size"):
        if cell_len(stats.plain) <= inner_width:
            break
        included.remove(name)
        stats = make_stats(3)
    for spacing in (2, 1):
        if cell_len(stats.plain) <= inner_width:
            break
        stats = make_stats(spacing)
    if cell_len(stats.plain) > inner_width:
        stats.truncate(inner_width, overflow="crop")

    return Panel(Group(Text(""), row, stats), expand=True, padding=(0, 1),
                 border_style="#5a5a5a", box=box.ROUNDED)


def short_failure_reason(reason):
    value = str(reason or "Unknown error").strip()
    prefix = re.compile(r"^(?:ERROR:\s*)?(?:\[youtube\]\s*[\w-]+:\s*|Postprocessing:\s*)", re.I)
    while prefix.match(value):
        value = prefix.sub("", value, count=1).strip()
    value = re.sub(r"^ERROR:\s*", "", value, flags=re.I).strip()
    message = re.sub(r"\s+", " ", value).strip()
    lowered = message.lower()
    for pattern, label in (
            (r"\bprivate video\b", "Private video"),
            (r"\bage restricted\b|\bage-restricted\b|\bconfirm your age\b", "Age restricted"),
            (r"\bnot available in your country\b|\bgeo(?:graphical)? restriction\b|"
             r"\bgeo-restricted\b", "Region blocked"),
            (r"\bcopyright\b", "Copyright claim"),
            (r"\bvideo (?:is )?unavailable\b", "Video unavailable")):
        if re.search(pattern, lowered):
            return label
    return message[:60]


def build_failures_view(state, width, height):
    from rich import box

    with state.lock:
        failures = list(state.failures)
    display_rows = []
    for failure in failures:
        title, artist, reason = failure[:3]
        video_id = failure[3] if len(failure) > 3 else ""
        if not video_id:
            match = re.search(r"\[youtube\]\s*([A-Za-z0-9_-]{11})\s*:", str(reason), re.I)
            video_id = match.group(1) if match else "—"
        if not artist or str(artist).strip().casefold() == "unknown":
            artist = "—"
        display_rows.append((str(title), str(artist), short_failure_reason(reason), str(video_id)))

    def make_view(visible_count):
        table = Table(title=f"Failures ({len(failures)})", expand=True,
                      border_style="#5a5a5a", box=box.SQUARE)
        table.add_column("Title", style="bold #ffffff", ratio=4, no_wrap=False)
        table.add_column("Artist", style="#8b8b8b", ratio=2)
        table.add_column("Reason", style="#f85149", ratio=3, no_wrap=True, overflow="ellipsis")
        if width >= 90:
            table.add_column("ID", style="#8b8b8b", width=11, no_wrap=True, overflow="ellipsis")
        if not display_rows:
            table.add_row("No failures", "", "", *([""] if width >= 90 else []))
        else:
            for row in display_rows[-visible_count:]:
                table.add_row(*row[:3], *([row[3]] if width >= 90 else []))
        omitted = max(len(display_rows) - visible_count, 0)
        parts = [table]
        if omitted:
            parts.append(Text(f"+{omitted} more", style="#8b8b8b"))
        parts.append(Text("Press any key to return", style="#8b8b8b"))
        return Group(*parts)

    visible_count = max(1, min(len(display_rows), max(height - 8, 1)))
    while True:
        view = make_view(visible_count)
        lines = console.render_lines(view, options=console.options.update(height=None), pad=False)
        if len(lines) < max(height, 1) or visible_count <= 1:
            return view
        visible_count -= 1


class St:
    def __init__(self, total, todo, jobs, initial_failures=0):
        self.total, self.n, self.done = total, 0, 0
        self.fail, self.worker_fail, self.bytes = initial_failures, 0, 0
        self.interrupted = 0
        self.jobs, self.todo = jobs, todo
        self.t0 = time.time()
        self.speed_started = None
        self.lock, self.print_lock = threading.RLock(), threading.Lock()
        self.workers = {i: {"active": False} for i in range(jobs)}
        self.failures = []

    def start(self, worker_id, index, e, playlist):
        with self.lock:
            self.workers[worker_id] = dict(active=True, index=index, title=e["title"], artist="",
                                           playlist=playlist, dl=0, total=0, speed=0, stage=0, retry="")

    def set_track(self, worker_id, title, artist):
        with self.lock:
            self.workers[worker_id].update(title=title, artist=artist)

    def progress(self, worker_id, d):
        with self.lock:
            state = self.workers[worker_id]
            if d["status"] == "downloading":
                if self.speed_started is None:
                    self.speed_started = time.monotonic()
                speed = d.get("speed") or 0
                old_speed = state.get("speed") or 0
                state.update(stage=1, dl=d.get("downloaded_bytes") or 0,
                             total=d.get("total_bytes") or d.get("total_bytes_estimate") or 0,
                             speed=0.3 * speed + 0.7 * old_speed if old_speed else speed)
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

    def record(self, worker_id, ok, fallback_title, song_size, elapsed, reason, interrupted=False):
        with self.lock:
            state = self.workers[worker_id]
            title = state.get("title") or fallback_title
            artist = state.get("artist", "")
            if ok:
                self.done += 1
                self.bytes += song_size
            elif interrupted:
                self.interrupted += 1
            else:
                self.fail += 1
                self.worker_fail += 1
            self.n += 1
            state["active"] = False
            return self.n, title

    def failure_record(self, worker_id, fallback_title, reason, video_id=None):
        with self.lock:
            state = self.workers[worker_id]
            title = first_text(state.get("title"), fallback_title)
            artist = first_text(state.get("artist"), "Unknown")
            raw_reason = reason or "Unknown error"
            if not video_id:
                match = re.search(r"\[youtube\]\s*([A-Za-z0-9_-]{11})\s*:", str(raw_reason), re.I)
                video_id = match.group(1) if match else ""
            self.failures.append((title, artist, raw_reason, video_id))
            return title, artist, raw_reason

    def render(self):
        width = max(console.size.width, 1)
        narrow = width < 100
        with self.lock:
            failed = self.fail
            workers = []
            for worker_id in range(self.jobs):
                state = dict(self.workers.get(worker_id, {}))
                state.setdefault("active", False)
                state.setdefault("title", "")
                state.setdefault("artist", "")
                state.setdefault("dl", 0)
                state.setdefault("total", 0)
                state.setdefault("speed", 0)
                state.setdefault("stage", 0)
                workers.append((worker_id, state))

        blocks = [build_overall_panel(self, width)]

        orange, green, red, grey = "#ff9f1a", "#3fb950", "#f85149", "#8b8b8b"
        stage_names = ("Fetch", "Download", "Convert", "Tag")
        for worker_id, state in workers:
            title_line = Text()
            title_line.append(f"W{worker_id + 1}  ", style=orange)
            title_line.append(state["title"] or "Waiting for data…", style="bold #ffffff")
            if not narrow and state["artist"]:
                title_line.append(f" · {state['artist']}", style=grey)
            if state["dl"] > 0:
                right_text = f"{size(state['dl'])} / {size(state['total'])} · {size(state['speed'])}/s"
            else:
                right_text = "waiting for data…"
            title_line.truncate(max(width - len(right_text) - 8, 1), overflow="ellipsis")
            line1 = Table.grid(expand=True, padding=0)
            line1.add_column(ratio=1)
            line1.add_column(no_wrap=True, justify="right")
            line1.add_row(title_line, Text(right_text, style=grey))

            stage = min(max(state["stage"], 0), len(stage_names) - 1)
            progress = min(state["dl"] / state["total"], 1) if state["total"] else 0
            percent_style = green if progress >= 1 else orange if progress > 0 else grey
            percent_text = Text(f"{progress:.0%}", style=percent_style)
            chips = Text()
            for index, name in enumerate(stage_names):
                if index:
                    chips.append("  ")
                if index < stage:
                    chips.append(f"✓ {name}", style=green)
                elif index == stage:
                    chips.append(f"● {name}", style=orange)
                else:
                    chips.append(f"○ {name}", style=grey)
            inner_width = max(width - 4, 1)
            bar_cells = max(inner_width - 6 - len(chips.plain), 1)
            filled = min(int(bar_cells * progress), bar_cells)
            progress_bar = Text("━" * filled, style=green if progress >= 1 else orange)
            progress_bar.append("━" * (bar_cells - filled), style="#3a3a3a")
            line2 = Table.grid(expand=True, padding=0)
            line2.add_column(width=5, no_wrap=True)
            line2.add_column(ratio=1, no_wrap=True)
            line2.add_column(no_wrap=True, justify="right")
            line2.add_row(percent_text, progress_bar, chips)
            blocks.append(Panel(Group(line1, line2), expand=True, padding=(0, 1),
                                border_style="#6b4a12"))

        blocks.append(Text(f"Failed: {failed} · press f to view failures, q to quit", style=grey))
        return Group(*blocks)


class DashboardKeys:
    def __init__(self, state, stop_event, gate, live=None, on_quit=None):
        self.state, self.stop_event, self.gate = state, stop_event, gate
        self.live = live
        self.on_quit = on_quit
        self.quit_requested = threading.Event()
        self.quit_started_at = None
        self.quit_done = threading.Event()
        self.quit_screen_open = threading.Event()
        self.quit_screen_closed = threading.Event()
        self.quit_screen_closed.set()
        self.quit_status_lock = threading.Lock()
        self.quit_status = {"downloads": "running", "converters": "pending",
                            "saving": "pending", "cleaning": "pending"}
        self.done = threading.Event()
        self.view_open = threading.Event()
        self.view_closed = threading.Event()
        self.view_closed.set()
        self.thread = None

    def start(self):
        if not sys.stdin.isatty():
            return False
        self.thread = threading.Thread(target=self._run, name="dashboard-keys", daemon=True)
        self.thread.start()
        return True

    def stop(self):
        if self.view_open.is_set() and not self.stop_event.is_set():
            self.view_closed.wait()
        if self.quit_screen_open.is_set():
            self.quit_screen_closed.wait()
        self.done.set()
        if self.thread:
            self.thread.join(timeout=1)

    def _read_key(self):
        if os.name == "nt":
            import msvcrt
            if not msvcrt.kbhit():
                time.sleep(0.05)
                return None
            key = msvcrt.getwch()
            if key in ("\x00", "\xe0"):
                if msvcrt.kbhit():
                    msvcrt.getwch()
                return None
            return key
        import select
        fd = sys.stdin.fileno()
        ready, _, _ = select.select([fd], [], [], 0.1)
        return os.read(fd, 1).decode("utf-8", "ignore") if ready else None

    def _flush_input(self):
        if os.name == "nt":
            with contextlib.suppress(Exception):
                import msvcrt
                while msvcrt.kbhit():
                    key = msvcrt.getwch()
                    if key in ("\x00", "\xe0") and msvcrt.kbhit():
                        msvcrt.getwch()
            return
        with contextlib.suppress(Exception):
            import termios
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)

    def request_quit(self):
        if self.quit_started_at is None:
            self.quit_started_at = time.monotonic()
        self.stop_event.set()
        self.gate.wake()
        self.quit_requested.set()

    def set_quit_status(self, step, value):
        with self.quit_status_lock:
            self.quit_status[step] = value

    def render_quit(self):
        from rich.spinner import Spinner

        with self.quit_status_lock:
            status = dict(self.quit_status)
        steps = (
            ("downloads", "Stopped downloads"),
            ("converters", "Stopped converters"),
            ("saving", "Saving state"),
            ("cleaning", "Cleaning temp files"),
        )
        lines = []
        for key, label in steps:
            value = status[key]
            glyph, color = ("✓", "#22c55e") if value == "done" else \
                ("✗", "#ef4444") if value == "failed" else \
                ("●", "#ff9f1a") if value == "running" else ("○", "#8b8b8b")
            lines.append(Text(f"{glyph} {label}", style=color))
        return Group(
            Spinner("dots", text=Text("Quitting…", style="bold #ff9f1a"), style="#ff9f1a"),
            Text(f"Stopping {self.state.jobs} workers · saving progress", style="#8b8b8b"),
            *lines,
            Text("Press q again to force quit now", style="#8b8b8b"),
        )

    def _force_quit(self):
        terminate_child_processes(force=True)
        with contextlib.suppress(Exception):
            console.file.flush()
        os._exit(0)

    def _show_quit_screen(self):
        self.quit_screen_closed.clear()
        try:
            if self.live is not None and self.live.is_started:
                self.live.stop()
            with console.screen():
                console.clear()
                quit_live = Live(get_renderable=self.render_quit, console=console,
                                 refresh_per_second=8, transient=True, screen=False,
                                 vertical_overflow="crop")
                with quit_live:
                    self.quit_screen_open.set()
                    if self.on_quit:
                        try:
                            self.on_quit()
                        except Exception:
                            logging.exception("Could not start quit cleanup")
                            self.quit_done.set()
                    while not self.quit_done.is_set():
                        key = self._read_key()
                        if key is not None and key.lower() == "q":
                            self._force_quit()
        finally:
            try:
                self._flush_input()
                console.clear()
            finally:
                self.quit_screen_open.clear()
                self.quit_screen_closed.set()

    def _show_failures(self):
        if self.live is None or not self.live.is_started:
            return
        self._flush_input()
        self.view_closed.clear()
        self.view_open.set()
        try:
            self.live.stop()
            with console.screen():
                console.clear()
                console.print(build_failures_view(
                    self.state, console.size.width, console.size.height))
                while not self.done.is_set() and not self.stop_event.is_set():
                    if self._read_key() is not None:
                        break
        finally:
            try:
                self._flush_input()
                console.clear()
                if not self.done.is_set() and not self.stop_event.is_set():
                    self.live.start(refresh=True)
            finally:
                self.view_open.clear()
                self.view_closed.set()

    def _run(self):
        old_terminal = None
        try:
            if os.name != "nt":
                import termios
                import tty
                fd = sys.stdin.fileno()
                old_terminal = termios.tcgetattr(fd)
                tty.setcbreak(fd)
            while not self.done.is_set():
                if self.quit_requested.is_set():
                    self._show_quit_screen()
                    return
                key = self._read_key()
                if key is None:
                    continue
                if key.lower() == "f":
                    self._show_failures()
                elif key.lower() == "q":
                    self.request_quit()
                    self._show_quit_screen()
                    return
        except Exception:
            logging.exception("Dashboard key handling failed")
        finally:
            if old_terminal is not None:
                with contextlib.suppress(Exception):
                    import termios
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_terminal)


def run_demo():
    ensure_ui()
    try:
        import random
    except ImportError:
        random = __import__("random")
    todo = [{"id": f"demo{i:07}", "title": f"Demo Track {i + 1}", "dur": 180}
            for i in range(64)]
    state = St(64, todo, 3)
    stop_event, gate = threading.Event(), WorkerGate(3)
    work = queue.Queue()
    for index, item in enumerate(todo):
        work.put((index, item))

    def demo_worker(worker_id):
        while not stop_event.is_set():
            try:
                index, item = work.get_nowait()
            except queue.Empty:
                return
            state.start(worker_id, index, item, "Demo")
            state.set_track(worker_id, item["title"], "Demo Artist")
            total = random.randint(2_000_000, 5_000_000)
            downloaded = 0
            for step in range(random.randint(8, 16)):
                if stop_event.is_set():
                    return
                downloaded = min(total, downloaded + random.randint(total // 12, total // 6))
                state.progress(worker_id, {"status": "downloading",
                                           "downloaded_bytes": downloaded,
                                           "total_bytes": total,
                                           "speed": random.randint(300_000, 900_000)})
                time.sleep(random.uniform(0.08, 0.16))
            failed = random.random() < 0.08
            if failed:
                reason = "Video unavailable"
                state.failure_record(worker_id, item["title"], reason, video_id=item["id"])
                state.record(worker_id, False, item["title"], 0, 1.2, reason)
            else:
                state.record(worker_id, True, item["title"], total, 1.2, "")
            state.finish_worker(worker_id)

    console.clear()
    live = Live(get_renderable=state.render, console=console, refresh_per_second=8,
                transient=False, screen=True, vertical_overflow="crop")
    executor, futures, demo_cleanup_thread = None, [], None
    demo_cleanup_lock = threading.Lock()

    def start_demo_cleanup():
        nonlocal demo_cleanup_thread
        with demo_cleanup_lock:
            if demo_cleanup_thread is not None:
                return

            def cleanup_demo():
                if executor:
                    executor.shutdown(wait=False, cancel_futures=True)
                pending = {future for future in futures if not future.done()}
                if pending:
                    wait(pending, timeout=3)
                for step in ("downloads", "converters", "saving", "cleaning"):
                    listeners.set_quit_status(step, "done")
                listeners.quit_done.set()

            demo_cleanup_thread = threading.Thread(
                target=cleanup_demo, name="demo-quit-cleanup", daemon=True)
            demo_cleanup_thread.start()

    listeners = DashboardKeys(state, stop_event, gate, live, on_quit=start_demo_cleanup)
    with live:
        try:
            executor = ThreadPoolExecutor(max_workers=3)
            futures.extend(executor.submit(demo_worker, worker_id) for worker_id in range(3))
            listeners.start()
            pending = set(futures)
            while pending:
                if listeners.quit_requested.is_set():
                    break
                completed, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in completed:
                    if not future.cancelled():
                        future.result()
            if listeners.quit_requested.is_set():
                start_demo_cleanup()
                listeners.quit_done.wait(8)
            else:
                executor.shutdown(wait=True)
        finally:
            listeners.stop()
    if listeners.quit_requested.is_set():
        console.show_cursor(True)
        console.print(f"Stopped · {state.done} done · {state.fail} failed · "
                      f"{max(state.total - state.done - state.fail, 0)} left · demo")
    else:
        console.print(f"[bold #3fb950]Done[/] · {state.done} done · {state.fail} failed · "
                      f"{size(state.bytes)} · {dur(time.time() - state.t0)}")


def dl_opts(cfg, out, st, worker_id, force_event=None, stop_event=None):
    yt_dlp = get_ytdlp()
    f = cfg["format"]

    def hook(d):
        if (stop_event and stop_event.is_set()) or (force_event and force_event.is_set()):
            raise StopRequested()
        st.progress(worker_id, d)

    def pphook(d):
        if (stop_event and stop_event.is_set()) or (force_event and force_event.is_set()):
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
                    playlist_by_id, compact, force_event):
    yt_dlp = get_ytdlp()
    Clean, SquareCover = postprocessor_types()
    status = WorkerStatus(st, worker_id)
    with yt_dlp.YoutubeDL(dl_opts(cfg, out, st, worker_id, force_event, stop_event)) as y:
        y.add_post_processor(Clean(lib, cfg, status, stop_event), when="pre_process")
        if cfg["cover"] and cfg["square"] and cfg["format"] != "wav":
            y.add_post_processor(SquareCover(stop_event), when="before_dl")
        while not stop_event.is_set():
            try:
                index, e = work.get_nowait()
            except queue.Empty:
                return
            if not gate.acquire(stop_event):
                return
            st.start(worker_id, index, e, playlist_by_id.get(e["id"], "Single track"))
            t1, ok, song_size, note, cancelled, retries = time.time(), False, 0, "", False, 0
            index_retry = False
            held = True
            try:
                while True:
                    retry_delay = None
                    try:
                        if stop_event.is_set():
                            raise StopRequested()
                        info = y.extract_info(watch_url(e["id"]), download=True)
                        if stop_event.is_set():
                            raise StopRequested()
                        downloads = info.get("requested_downloads") if isinstance(info, dict) else None
                        rd = next((item for item in downloads or [] if isinstance(item, dict)), {})
                        with lib.name_lock:
                            reserved_name = lib.reserved_by_id.get(e["id"])
                        filename = first_text(info.get("fname") if isinstance(info, dict) else None,
                                              reserved_name or fsafe(e.get("title")))
                        fp = Path(first_text(rd.get("filepath"), str(out / f"{filename}.{cfg['format']}")))
                        artist = first_text(info.get("artist") if isinstance(info, dict) else None, "Unknown")
                        title = first_text(info.get("title") if isinstance(info, dict) else None,
                                           first_text(e.get("title"), "Unknown"))
                        record = {"file": fp.name, "artist": artist, "title": title,
                                  "dur": int((info.get("duration") if isinstance(info, dict) else None)
                                             or e.get("dur") or 0)}
                        song_size = fp.stat().st_size
                        with lib.lock:
                            lib.d["songs"][e["id"]] = record
                        ok, note = True, f"{size(song_size)}  {time.time() - t1:.1f}s"
                    except (StopRequested, KeyboardInterrupt) as ex:
                        if stop_event.is_set() or force_event.is_set():
                            cancelled = True
                        else:
                            note = clean_reason(short(ex) or "Interrupted")
                            with failed_lock:
                                failed.append(st.failure_record(
                                    worker_id, first_text(e.get("title"), e["id"]), str(ex) or note,
                                    video_id=e["id"]))
                            logging.exception("Download failed for video %s", e["id"])
                    except Exception as ex:
                        if stop_event.is_set() or force_event.is_set():
                            cancelled = True
                        elif isinstance(ex, IndexError) and not index_retry:
                            index_retry = True
                            retry_delay = 1
                            st.set_retry(worker_id, retry_delay)
                            logging.exception("Retrying index error for video %s once", e["id"])
                        elif re.search(r"429|sign\s+in", str(ex), re.I) and retries < max(1, int(cfg["retries"])):
                            retries += 1
                            gate.reduce()
                            retry_delay = min(2 ** (retries - 1), 30)
                            st.set_retry(worker_id, retry_delay)
                            logging.exception("Transient download failure for video %s; retry %s in %ss",
                                              e["id"], retries, retry_delay)
                        else:
                            note = clean_reason(short(ex))
                            with failed_lock:
                                failed.append(st.failure_record(
                                    worker_id, first_text(e.get("title"), e["id"]), str(ex) or note,
                                    video_id=e["id"]))
                            logging.exception("Download failed for video %s", e["id"])
                    gate.release()
                    held = False
                    if retry_delay is not None:
                        if stop_event.wait(retry_delay) or force_event.is_set() or not gate.acquire(stop_event):
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
                st.record(worker_id, False, first_text(e.get("title"), e["id"]), 0, elapsed,
                          "Interrupted", interrupted=True)
                st.finish_worker(worker_id)
                if not stop_event.is_set() and not force_event.is_set():
                    lib.save()
                return
            count, title = st.record(worker_id, ok, e["title"], song_size, elapsed, note)
            st.finish_worker(worker_id)
            if not stop_event.is_set() and not force_event.is_set():
                lib.save()
            if compact:
                with st.print_lock:
                    console.print(f"{status_glyph(ok)} {count}/{st.total} {title[:50]}  {note}")


def cleanup_partial_files(out, lib):
    with lib.lock:
        known = {Path(song["file"]).name for song in lib.d["songs"].values()
                 if isinstance(song, dict) and song.get("file")}
    with lib.name_lock:
        reserved = {name.casefold() for name in lib.reserved_by_id.values()}
    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    audio_exts = EXT | {".webm"}
    for path in Path(out).iterdir():
        if not path.is_file() or path.name in known:
            continue
        name = path.name
        suffix = path.suffix.lower()
        base_name = name[:-len(suffix)] if suffix in (".part", ".ytdl", ".temp") else name
        stem = Path(base_name).stem
        if stem.lower().endswith(".sq"):
            stem = stem[:-3]
        if stem.casefold() in reserved and (suffix in (".part", ".ytdl", ".temp")
                                            or suffix in audio_exts | image_exts):
            path.unlink(missing_ok=True)


def execute(cfg, plan, compact=False, interactive=False, prune_ok=False):
    out, lib, todo = plan["out"], plan["lib"], plan["todo"]
    jobs = max(1, min(6, int(cfg.get("jobs", 3))))
    worker_count = min(jobs, max(1, len(todo)))
    st = St(len(todo), todo, worker_count, initial_failures=len(plan["errs"]))
    failed, stopped = list(plan["errs"]), False
    with st.lock:
        st.failures.extend((title, "Unknown", reason) for title, reason in plan["errs"])
    failed_lock = threading.Lock()
    plain = compact or not console.is_terminal
    stop_event, force_event = threading.Event(), threading.Event()
    gate = WorkerGate(worker_count)
    executor = None
    futures = []
    key_listener = None
    quit_cleanup_thread = None
    quit_cleanup_done = threading.Event()
    quit_cleanup_lock = threading.Lock()
    quit_timed_out = False
    quit_handled = False
    quit_save_failed = [False]
    live = contextlib.nullcontext() if plain else \
        Live(get_renderable=st.render, console=console, refresh_per_second=8, transient=False, screen=True,
             vertical_overflow="crop")
    live_obj = live if isinstance(live, Live) else None
    old_sigint, signal_installed = None, False
    finalizing = [False]
    signal_stopped = False

    def handle_sigint(_signum, _frame):
        if stop_event.is_set() or finalizing[0]:
            force_event.set()
            gate.wake()
            terminate_child_processes(force=True)
            with contextlib.suppress(Exception):
                console.file.flush()
            os._exit(0)
        stop_event.set()
        gate.wake()
        if key_listener:
            key_listener.request_quit()
        raise FirstInterrupt()

    if todo and threading.current_thread() is threading.main_thread():
        try:
            old_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, handle_sigint)
            signal_installed = True
        except (ValueError, OSError):
            signal_installed = False

    def write_failed_file():
        ff = DATA / "failed.txt"
        with failed_lock:
            failed_rows = []
            for row in failed:
                if len(row) >= 3:
                    title, artist, reason = row[0], row[1], row[2]
                else:
                    title, reason = row
                    artist = "Unknown"
                failed_rows.append((str(title), str(artist), str(reason)))
        if failed_rows:
            aw(ff, "\n".join(
                "\t".join(value.replace("\r", " ").replace("\n", "\\n").replace("\t", " ")
                          for value in (title, artist, reason))
                for title, artist, reason in failed_rows))
        else:
            ff.unlink(missing_ok=True)
        return failed_rows

    def quit_cleanup():
        try:
            if key_listener:
                key_listener.set_quit_status("downloads", "running")
                key_listener.set_quit_status("converters", "running")
            gate.wake()
            if executor:
                executor.shutdown(wait=False, cancel_futures=True)
            worker_deadline = time.monotonic() + 3
            terminate_child_processes(timeout=min(2, max(worker_deadline - time.monotonic(), 0)))
            pending = {future for future in futures if not future.done()}
            if pending:
                wait(pending, timeout=max(worker_deadline - time.monotonic(), 0))
                pending = {future for future in pending if not future.done()}
            if pending:
                force_event.set()
                terminate_child_processes(force=True)
                wait(pending, timeout=min(0.1, max(worker_deadline - time.monotonic(), 0)))

            if key_listener:
                key_listener.set_quit_status("downloads", "done")
                key_listener.set_quit_status("converters", "done")
                key_listener.set_quit_status("saving", "running")
            with st.lock:
                downloads = st.done
                downloaded_bytes = st.bytes
                interrupted = max(st.total - st.done - st.worker_fail, 0)
                st.interrupted = interrupted

            aliases = dict(plan["aliases"])
            with lib.lock:
                lib.d["aliases"] = aliases
                songs = dict(lib.d["songs"])
                for playlist in plan["pls"]:
                    lib.d["playlists"][playlist["name"]] = [item["id"] for item in playlist["items"]]
                stats = lib.d["stats"]
                stats["runs"] += 1
                stats["downloads"] += downloads
                stats["bytes"] += downloaded_bytes
                stats["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            if cfg["playlist_files"]:
                for playlist in plan["pls"]:
                    m3u(out, playlist["name"], [item["id"] for item in playlist["items"]], songs, aliases)
            lib.save()
            write_failed_file()
            if key_listener:
                key_listener.set_quit_status("saving", "done")
                key_listener.set_quit_status("cleaning", "running")
            cleanup_partial_files(out, lib)
            if key_listener:
                key_listener.set_quit_status("cleaning", "done")
        except Exception:
            logging.exception("Quit cleanup failed")
            quit_save_failed[0] = True
            if key_listener:
                key_listener.set_quit_status("saving", "failed")
        finally:
            quit_cleanup_done.set()
            if key_listener:
                key_listener.quit_done.set()

    def start_quit_cleanup():
        nonlocal quit_cleanup_thread
        with quit_cleanup_lock:
            if quit_cleanup_thread is not None:
                return
            quit_cleanup_thread = threading.Thread(
                target=quit_cleanup, name="quit-cleanup", daemon=True)
            quit_cleanup_thread.start()

    def finish_quit():
        nonlocal quit_timed_out
        if key_listener and key_listener.thread is None:
            key_listener.start()
        if key_listener is None or key_listener.thread is None:
            start_quit_cleanup()
        started = (key_listener.quit_started_at
                   if key_listener and key_listener.quit_started_at is not None else time.monotonic())
        deadline = started + 8
        while not quit_cleanup_done.wait(0.05):
            if not quit_cleanup_thread and time.monotonic() - started > 0.2:
                start_quit_cleanup()
            if time.monotonic() >= deadline:
                quit_timed_out = True
                force_event.set()
                terminate_child_processes(force=True)
                if key_listener:
                    key_listener.quit_done.set()
                break

    if todo:
        if not plain:
            console.clear()
        playlist_by_id = {}
        for playlist in plan["pls"]:
            for item in playlist["items"]:
                playlist_by_id.setdefault(item["id"], playlist["name"])
        work = queue.Queue()
        for index, e in enumerate(todo):
            work.put((index, e))
        with live:
            executor = ThreadPoolExecutor(max_workers=worker_count)
            key_listener = DashboardKeys(
                st, stop_event, gate, live_obj, on_quit=start_quit_cleanup) if not plain else None
            try:
                for worker_id in range(worker_count):
                    futures.append(executor.submit(
                        download_worker, worker_id, work, cfg, out, lib, st, gate, stop_event,
                        failed, failed_lock, playlist_by_id, plain, force_event))
                if key_listener:
                    key_listener.start()
                pending = set(futures)
                while pending:
                    if key_listener and key_listener.quit_requested.is_set():
                        stopped = quit_handled = signal_stopped = True
                        finish_quit()
                        break
                    completed, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                    for future in completed:
                        if not future.cancelled():
                            future.result()
                if key_listener and key_listener.quit_requested.is_set() and not quit_handled:
                    stopped = quit_handled = signal_stopped = True
                    finish_quit()
            except FirstInterrupt:
                stopped = True
                signal_stopped = True
                quit_handled = True
                if key_listener:
                    key_listener.request_quit()
                finish_quit()
            except KeyboardInterrupt:
                stopped = True
                signal_stopped = True
                stop_event.set()
                quit_handled = True
                if key_listener:
                    key_listener.request_quit()
                finish_quit()
            except ForceInterrupt:
                stopped = quit_handled = signal_stopped = True
                force_event.set()
                terminate_child_processes(force=True)
                finish_quit()
            except BaseException:
                stop_event.set()
                force_event.set()
                gate.wake()
                if executor:
                    executor.shutdown(wait=False, cancel_futures=True)
                terminate_child_processes(force=True)
                if signal_installed:
                    signal.signal(signal.SIGINT, old_sigint)
                    signal_installed = False
                raise
            finally:
                if key_listener:
                    key_listener.stop()
            if key_listener and key_listener.quit_requested.is_set():
                stopped = True
            if not signal_stopped:
                executor.shutdown(wait=True)

    if quit_handled:
        finalizing[0] = True
        if signal_installed:
            signal.signal(signal.SIGINT, old_sigint)
            signal_installed = False
        if quit_timed_out:
            console.show_cursor(True)
            console.print("Forced quit")
            with contextlib.suppress(Exception):
                console.file.flush()
            os._exit(0)
        with st.lock:
            downloads, failures = st.done, st.fail
            left = max(st.total - st.done - st.worker_fail, 0)
        console.show_cursor(True)
        console.print(
            f"Stopped · {downloads} done · {failures} failed · {left} left · "
            f"{'progress save failed' if quit_save_failed[0] else 'progress saved'}")
        return stopped

    finalizing[0] = True
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
                    song = lib.d["songs"].pop(k, {})
                    filename = song.get("file") if isinstance(song, dict) else None
                    if filename:
                        (out / filename).unlink(missing_ok=True)
                lib.d["aliases"] = {k: v for k, v in lib.d["aliases"].items() if v not in stale}
                songs = dict(lib.d["songs"])

    cleanup_partial_files(out, lib)
    el = time.time() - st.t0
    with st.lock:
        downloads, downloaded_bytes, failures = st.done, st.bytes, st.fail
        interrupted = max(st.total - st.done - st.worker_fail, 0) if stopped else 0
        st.interrupted = interrupted
    with lib.lock:
        s = lib.d["stats"]
        s["runs"] += 1
        s["downloads"] += downloads
        s["bytes"] += downloaded_bytes
        s["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        playlist_count = len(lib.d["playlists"])
    lib.save()
    ff = DATA / "failed.txt"
    with failed_lock:
        failed_rows = []
        for row in failed:
            if len(row) >= 3:
                title, artist, reason = row[0], row[1], row[2]
            else:
                title, reason = row
                artist = "Unknown"
            failed_rows.append((str(title), str(artist), str(reason)))
    if failed_rows:
        ff.write_text("\n".join(
            "\t".join(value.replace("\r", " ").replace("\n", "\\n").replace("\t", " ")
                      for value in (title, artist, reason))
            for title, artist, reason in failed_rows), encoding="utf-8")
    else:
        ff.unlink(missing_ok=True)

    summary_label = "Stopped · " if stopped else "Already up to date · " if not todo else "Done · "
    summary = Text(summary_label,
                   style="bold #ff9f1a" if stopped else "bold #3fb950")
    summary.append(f"{downloads} done · {failures} failed · {size(downloaded_bytes)} · {dur(el)}",
                   style="#ffffff")
    if stopped:
        summary.append(f" · Interrupted {interrupted} (will retry on next Update)", style="#8b8b8b")
    console.print(summary)
    if rows:
        t = Table(border_style="#3a3a3a")
        for c in ("Playlist", "Songs", "Added", "Removed"):
            t.add_column(c, justify="left" if c == "Playlist" else "right")
        for n, c, a, r in rows:
            t.add_row(n, str(c), Text(f"+{a}", style="#22c55e"), Text(f"-{r}", style="#ef4444") if r else "-")
        console.print(t)
    if plan["bad"]:
        console.print(f"[dim]{plan['bad']} private/deleted videos skipped[/]")
    if failed_rows:
        console.print(f"[dim]Failure details saved to {ff}[/]")
        if sum(1 for _, _, reason in failed_rows if BLOCKED.search(reason)) >= 2:
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
    if signal_installed:
        signal.signal(signal.SIGINT, old_sigint)
        signal_installed = False
    return stopped


def show_stats(cfg):
    lib = get_lib(cfg["out"])
    s = lib.d["stats"]
    t = Table(title="Library stats", show_header=False, border_style="cyan")
    t.add_row("Folder", cfg["out"])
    t.add_row("Data folder", short_data_dir())
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
    try:
        return questionary.select(msg, choices=[questionary.Choice(t, v) for t, v in choices],
                                  style=STYLE, instruction=" ").ask()
    except KeyboardInterrupt:
        return None


def pause():
    try:
        console.input("[dim]Press Enter to continue...[/]")
    except KeyboardInterrupt:
        return


def short_data_dir():
    try:
        return str(Path("~") / DATA.relative_to(Path.home()))
    except ValueError:
        return str(DATA)


def setup_logging():
    ensure_data_dir()
    logging.basicConfig(filename=DATA / "sync.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", force=True)


def menu_error(error):
    logging.exception("Menu action failed")
    console.print(f"[red]Error: {short(error)}[/]")


def print_timings():
    if not TIMING_ENABLED:
        return
    for name in ("imports", "config", "library load"):
        if name in TIMINGS:
            console.print(f"[dim]Timing · {name}: {TIMINGS[name]:.3f}s[/]")
    if UPDATE_CHECKER and "update check reported" not in TIMINGS:
        state = "complete" if UPDATE_CHECKER.done.is_set() else "still running"
        console.print(f"[dim]Timing · update check: {UPDATE_CHECKER.elapsed:.3f}s ({state})[/]")


def report_update_timing():
    if TIMING_ENABLED and UPDATE_CHECKER and UPDATE_CHECKER.done.is_set() \
            and "update check reported" not in TIMINGS:
        console.print(f"[dim]Timing · update check: {UPDATE_CHECKER.elapsed:.3f}s[/]")
        TIMINGS["update check reported"] = 1


def val(c, k):
    v = c[k]
    if isinstance(v, bool):
        return "on" if v else "off"
    return STYLES[v] if k == "name_style" else (str(v) or "-")


def save_named_links(links):
    path = DATA / "playlists.txt"
    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
    existing = {normalize_music_url(url) for url in read_list(path)}
    new_links = []
    for url, name in links:
        url = normalize_music_url(url)
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


def open_path(path):
    path = Path(path)
    try:
        if os.name == "nt":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        if os.name == "nt" and path.suffix.lower() in (".json", ".log"):
            try:
                subprocess.Popen(["notepad", str(path)])
                return
            except Exception as fallback_error:
                e = fallback_error
        console.print(f"[yellow]Could not open: {e}\nPath: {path}[/]")


def open_playlists():
    ensure_data_dir()
    path = DATA / "playlists.txt"
    if not path.exists():
        path.write_text("# One link per line\n", encoding="utf-8")
    console.print(f"[dim]{path}[/]")
    open_path(path)


def edit_playlists():
    open_playlists()


def offer_open_playlists():
    try:
        if questionary.confirm("Open Playlists.txt?", default=False).ask():
            open_playlists()
    except KeyboardInterrupt:
        return


def normalize_music_url(url):
    parsed = urlparse(url)
    try:
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        return url
    if hostname and hostname.lower() == "music.youtube.com":
        userinfo = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
        port_suffix = f":{port}" if port is not None else ""
        return parsed._replace(netloc=f"{userinfo}www.youtube.com{port_suffix}").geturl()
    return url


def is_playlist_url(url):
    parsed = urlparse(url)
    return "list" in parse_qs(parsed.query, keep_blank_values=True) or "/playlist" in parsed.path.lower()


def terminal_columns():
    return max(1, shutil.get_terminal_size((80, 24)).columns)


def data_file_size(byte_count):
    return f"{byte_count / 1024:.1f} KB" if byte_count < 1024 * 1024 else size(byte_count)


def truncate_display(value, width):
    from rich.cells import cell_len, set_cell_size

    text = str(value)
    width = max(width, 0)
    if cell_len(text) <= width:
        return Text(text)
    return Text(set_cell_size(text, max(width - 1, 0)) + "…")


def print_single_line(value, style=None):
    width = max(terminal_columns() - 1, 0)
    line = value if isinstance(value, Text) else Text(str(value), style=style)
    from rich.cells import cell_len

    if cell_len(line.plain) > width:
        line.truncate(max(width - 1, 0), overflow="crop")
        line.append("…", style=style or line.style)
    console.print(line, no_wrap=True, soft_wrap=True)


def redraw_prompt_line(display_prompt, entered, shown, value_style):
    from rich.cells import cell_len

    if sys.stdout.isatty():
        columns = terminal_columns()
        occupied = cell_len(display_prompt) + cell_len(entered)
        rows = max(1, (occupied + columns - 1) // columns)
        sys.stdout.write(f"\x1b[{rows}A")
        for row in range(rows):
            sys.stdout.write("\r\x1b[2K")
            if row < rows - 1:
                sys.stdout.write("\x1b[1B")
        if rows > 1:
            sys.stdout.write(f"\x1b[{rows - 1}A")
        sys.stdout.write("\r")
        sys.stdout.flush()

    columns = terminal_columns()
    prompt_width = cell_len(display_prompt)
    visible_value = truncate_display(shown, columns - prompt_width - 1)
    line = Text(display_prompt)
    line.append(visible_value.plain, style=value_style)
    print_single_line(line)


def redraw_link_prompt(prompt, value):
    redraw_prompt_line(f"? {prompt} ", value, value, "#ff8c00")


def confirm_playlist_save(count):
    prompt = f"Add these {count} playlist link(s) to Playlists.txt? [Y/n]"
    display_prompt = f"? {prompt} "
    while True:
        try:
            answer = questionary.text(prompt).ask()
        except KeyboardInterrupt:
            return None
        if answer is None:
            return None
        normalized = answer.strip().casefold()
        if normalized in ("", "y", "yes"):
            result, label = True, "Yes"
        elif normalized in ("n", "no"):
            result, label = False, "No"
        else:
            redraw_prompt_line(display_prompt, answer, answer, "#ffffff")
            print_single_line("Please answer y/yes or n/no.", style="#ef4444")
            continue
        redraw_prompt_line(display_prompt, answer, label, "#ffffff")
        return result


def collect_links(cfg):
    known, added = {normalize_music_url(url) for url in all_urls()}, []
    while True:
        prompt = f"Link #{len(added) + 1} (Enter on empty line = Done)"
        try:
            raw = questionary.text(prompt).ask()
        except KeyboardInterrupt:
            return None
        if raw is None:
            return None
        redraw_link_prompt(prompt, raw)
        link = raw.strip()
        if not link:
            return added
        parsed = urlparse(link)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            print_single_line("Enter a valid http(s) URL.", style="#ef4444")
            continue
        link = normalize_music_url(link)
        if link in known:
            print_single_line("Already in Playlists.txt; skipping duplicate link.", style="#8b8b8b")
            continue
        details, errs = {}, []
        resolve([link], cfg, errs, details)
        detail = details.get(link)
        if errs or not detail or detail[2]:
            reason = errs[0][1] if errs else (detail[2] if detail else "No playlist or song information found")
            print_single_line(f"Could not add link: {reason}", style="#ef4444")
            continue
        name, count, _ = detail
        added.append((link, name))
        known.add(link)
        print_single_line(f"{status_glyph(True)} {name} · {count} songs", style="#22c55e")


def add_links(cfg):
    options = [("Paste links", "paste"), ("Load from Playlists.txt", "load"),
               ("Edit Playlists.txt", "edit"), ("‹ Back", "back")]
    while True:
        console.clear()
        try:
            choice = ask("Add Music", options)
        except Exception as e:
            menu_error(e)
            continue
        if choice in (None, "back"):
            return None
        try:
            if choice == "paste":
                added = collect_links(cfg)
                if added is None:
                    return None
                if not added:
                    continue
                playlist_links = [(url, name) for url, name in added if is_playlist_url(url)]
                if playlist_links:
                    try:
                        save = confirm_playlist_save(len(playlist_links))
                    except KeyboardInterrupt:
                        return None
                    if save is None:
                        return None
                    if save:
                        save_named_links(playlist_links)
                return [url for url, _ in added]
            if choice == "load":
                ensure_data_dir()
                path = DATA / "playlists.txt"
                links = read_list(path)
                if not links:
                    if not path.exists():
                        path.write_text("# One link per line\n", encoding="utf-8")
                    elif "# One link per line" not in path.read_text(encoding="utf-8"):
                        with path.open("a", encoding="utf-8") as f:
                            f.write("\n# One link per line\n")
                    console.print(f"[yellow]No links found. Add one link per line in {path}.[/]")
                    offer_open_playlists()
                    continue
                return links
            if choice == "edit":
                edit_playlists()
                continue
        except Exception as e:
            menu_error(e)
            continue


def data_folder_menu():
    ensure_data_dir()
    while True:
        console.clear()
        console.print(f"[bold]Data folder[/]\n[dim]{DATA}[/]\n")
        try:
            files = []
            for entry in DATA.iterdir():
                if entry.is_file():
                    if not entry.name.lower().endswith(".tmp"):
                        files.append((entry.name, entry))
                elif entry.is_dir():
                    for child in entry.iterdir():
                        if child.is_file() and not child.name.lower().endswith(".tmp"):
                            files.append((f"{entry.name}/{child.name}", child))
            files.sort(key=lambda item: item[0].casefold())
            name_width = max(1, terminal_columns() - 20)
            choices = [("Open in file explorer", "open")]
            choices.extend((f"{truncate_display(name, name_width).plain:<{name_width}} "
                            f"{data_file_size(path.stat().st_size):>10}", str(path))
                           for name, path in files)
            choices.append(("‹ Back", "back"))
            choice = ask("", choices)
        except KeyboardInterrupt:
            return
        except Exception as e:
            menu_error(e)
            continue
        if choice in (None, "back"):
            return
        try:
            if choice == "open":
                open_path(DATA)
            else:
                open_path(Path(choice))
        except Exception as e:
            menu_error(e)


def library_folder_menu(cfg):
    while True:
        console.clear()
        console.print(f"[bold]Library folder[/]\n[dim]{cfg['out']}[/]\n")
        try:
            choice = ask("", [("Change folder", "change"), ("Open in file explorer", "open"),
                              ("‹ Back", "back")])
        except Exception as e:
            menu_error(e)
            continue
        if choice in (None, "back"):
            return cfg
        try:
            if choice == "open":
                open_path(cfg["out"])
                continue
            try:
                value = questionary.text("Music folder:", default=cfg["out"]).ask()
            except KeyboardInterrupt:
                return cfg
            if value is None or not value.strip():
                return cfg
            new_path = Path(value.strip()).expanduser().resolve()
            if new_path == Path(cfg["out"]).expanduser().resolve():
                return cfg
            with console.status("[cyan]Loading library..."):
                new_path = ensure_lib(new_path)
                updated = load_cfg(new_path)
                get_lib(new_path)
                save_cfg(updated)
            return updated
        except Exception as e:
            menu_error(e)


def settings(cfg):
    while True:
        console.clear()
        console.print(f"[bold orange1]Settings[/]\n[dim]Data: {short_data_dir()}[/]\n")
        items = [(f"{label:<22}{val(cfg, key)}", key) for key, label in LABELS]
        items.extend([("Library folder", "library"), ("Data folder", "data"),
                      ("Edit Playlists.txt", "edit"), ("Reset to defaults", "reset"), ("‹ Back", "back")])
        try:
            choice = ask("", items)
        except Exception as e:
            menu_error(e)
            continue
        if choice in (None, "back"):
            return cfg
        try:
            save = True
            if choice == "library":
                cfg = library_folder_menu(cfg)
                save = False
            elif choice == "data":
                data_folder_menu()
                save = False
            elif choice == "edit":
                open_playlists()
                save = False
            elif choice == "reset":
                out = cfg["out"]
                cfg.update(DEFAULTS)
                cfg["out"] = out
            elif isinstance(cfg[choice], bool):
                cfg[choice] = not cfg[choice]
            elif choice in CHOICES:
                value = ask(dict(LABELS)[choice],
                            [(str(STYLES.get(item, item)), item) for item in CHOICES[choice]] + [("‹ Back", "back")])
                if value and value != "back":
                    cfg[choice] = value
            else:
                value = questionary.text(dict(LABELS)[choice] + ":", default=str(cfg[choice])).ask()
                if value is not None:
                    cfg[choice] = (int(value) if value.isdigit() else cfg[choice]) \
                        if choice == "retries" else value.strip()
            if save:
                save_cfg(cfg)
        except Exception as e:
            menu_error(e)


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
    t.add_row("", Text.assemble((f"+ {len(plan['todo'])} new", "#22c55e"),
                                (f"   {status_glyph(True)} {in_library} in library", "#22c55e"),
                                (f"   {status_glyph(False)} {plan['bad']} private", "#ef4444")))
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


def run_flow(cfg, with_review, extra_urls=None):
    extra_urls = list(extra_urls or [])
    while True:
        urls = list(dict.fromkeys(all_urls(cfg) + extra_urls))
        if not urls:
            console.print(f"[yellow]No links yet. Add them to {DATA / 'playlists.txt'} with Add Music.[/]")
            pause()
            return "home"
        if not need_ffmpeg():
            return "home"
        try:
            plan = scan(cfg, urls)
            choice = review(cfg, plan) if with_review else "start"
            if not with_review:
                console.print(f"[bold]{len(plan['todo'])} new[/] · "
                              f"{len(plan['unique']) - len(plan['todo'])} in library\n")
            if choice == "start":
                stopped = execute(cfg, plan, interactive=True, prune_ok=True)
                if not stopped:
                    pause()
                return "home"
            if choice == "edit":
                cfg = settings(cfg)
                continue
            return "exit" if choice == "exit" else "home"
        except Exception as e:
            menu_error(e)
            return "home"


def header(cfg, upd):
    lib = get_lib(cfg["out"])
    console.print(Panel(f"[bold orange1]♪  ytmusic-sync[/]  v{VERSION}\n[dim]YouTube music + Local, always in sync[/]",
                        border_style="orange1", expand=False))
    console.print(f" {cfg['out']} · {len(lib.d['songs'])} songs · {size(lib.total_size())}\n"
                  f" Last sync: {ago(lib.d['stats']['last_run'])} · Data: {short_data_dir()}")
    if upd:
        console.print(f" [yellow]⚠ yt-dlp update available ({upd[0]} → {upd[1]})[/]")
    console.print()


def pick_library():
    console.print(Panel("[bold orange1]Welcome![/]\nWhere should your music library live?\n"
                        "[dim]Songs and .m3u8 playlists go here; app data is stored in ~/.ytmusic-sync.[/]",
                        border_style="orange1", expand=False))
    p = questionary.path("Music folder:", default=str(Path.home() / "music"), only_directories=True).ask()
    return Path(p).expanduser() if p else None


def interactive(cfg):
    while True:
        console.clear()
        upd = UPDATE_CHECKER.result if UPDATE_CHECKER and UPDATE_CHECKER.done.is_set() else None
        header(cfg, upd)
        report_update_timing()
        if TIMING_ENABLED and "startup timing printed" not in TIMINGS:
            print_timings()
            TIMINGS["startup timing printed"] = 1
        first = not all_urls(cfg)
        menu = [("Use defaults & start", "go"), ("Customize", "set"), ("Quit", "q")] if first else \
               [("Update library", "upd"), ("Add Music", "add"), ("Settings", "set"), ("Stats", "stats")]
        if upd:
            menu.append((f"Update yt-dlp ({upd[0]} → {upd[1]})", "ytdlp"))
        if not first:
            menu.append(("Quit", "q"))
        try:
            k = ask("", menu)
        except Exception as e:
            menu_error(e)
            continue
        if k in (None, "q"):
            return
        try:
            if k == "set":
                cfg = settings(cfg)
            elif k == "stats":
                show_stats(cfg)
                pause()
            elif k == "ytdlp":
                upgrade_ytdlp()
                pause()
            elif k == "go":
                links = all_urls(cfg)
                if not links:
                    links = add_links(cfg) or []
                if links and run_flow(cfg, True, links) == "exit":
                    return
            elif k == "add":
                links = add_links(cfg)
                if links and run_flow(cfg, True, links) == "exit":
                    return
            elif k == "upd" and run_flow(cfg, False) == "exit":
                return
        except Exception as e:
            menu_error(e)


# ---------- command line ----------
class VersionAction(argparse.Action):
    def __init__(self, option_strings, dest, nargs=0, **kwargs):
        super().__init__(option_strings, dest, nargs=nargs, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        parser.exit(message=f"{parser.prog} {VERSION} (yt-dlp {installed_ytdlp_version()})\n")


def save_cli_links(cfg, urls):
    if not urls:
        return
    details, errors = {}, []
    resolve(urls, cfg, errors, details)
    for url, reason in errors:
        console.print(f"[yellow]Could not get a name for {url}: {reason}[/]")
    save_named_links([(url, details.get(url, (url, 0, None))[0]) for url in urls])


def main():
    global TIMING_ENABLED, UPDATE_CHECKER
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
    g.add_argument("--demo", action="store_true", help="show a simulated 64-track progress dashboard")
    g.add_argument("--where", action="store_true", help="print the data folder and music library folder, then exit")
    g.add_argument("--timing", action="store_true", help="print startup step timings")
    g.add_argument("--update-ytdlp", action="store_true", help="upgrade yt-dlp and exit")
    g.add_argument("-V", "--version", action=VersionAction, help="show program and yt-dlp versions, then exit")
    a = ap.parse_args()

    direct = bool(a.urls or a.playlists or a.update or a.stats or a.dry_run or a.where or
                  a.update_ytdlp or a.demo)
    if not direct and not sys.stdin.isatty():
        return ap.print_help()
    TIMING_ENABLED = a.timing
    import_started = time.perf_counter()
    try:
        ensure_ui()
        TIMINGS["imports"] = time.perf_counter() - import_started
        config_started = time.perf_counter()
        ensure_data_dir()
        migrate_legacy_profiles()
        cfg = load_cfg()
        TIMINGS["config"] = time.perf_counter() - config_started

        if a.update_ytdlp:
            if a.timing:
                print_timings()
            return upgrade_ytdlp()

        if a.where:
            selected = a.out or cfg.get("out")
            library = str(Path(selected).expanduser().resolve()) if selected else "(not selected)"
            console.print(f"Data folder: {DATA}\nLibrary folder: {library}")
            if a.timing:
                print_timings()
            return 0

        if a.demo:
            run_demo()
            if a.timing:
                print_timings()
            return 0

        out = Path(a.out).expanduser().resolve() if a.out else \
            (Path(cfg["out"]).expanduser().resolve() if cfg["out"] else None)
        if out is not None and not a.out and not out.is_dir():
            console.print(f"[yellow]Last used music folder is unavailable: {out}. "
                          "Reconnect the drive or choose a folder.[/]")
            out = None
        if out is None:
            if sys.stdin.isatty():
                out = pick_library()
            if out is None:
                console.print("[yellow]No library folder selected. Run [bold]ytsync[/bold] to pick one, or pass -o FOLDER.[/]")
                return 1

        config_reload_started = time.perf_counter()
        out = ensure_lib(out)
        setup_logging()
        cfg = load_cfg(out)
        save_cfg(cfg)
        TIMINGS["config"] += time.perf_counter() - config_reload_started
        UPDATE_CHECKER = start_update_check()
        library_started = time.perf_counter()
        if not (a.urls or a.playlists or a.update or a.stats or a.dry_run):
            with console.status("[cyan]Loading library..."):
                get_lib(out)
        else:
            get_lib(out)
        TIMINGS["library load"] = time.perf_counter() - library_started

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
            show_stats(cfg)
            if a.timing:
                print_timings()
            return 0

        if not direct:
            interactive(cfg)
            report_update_timing()
            if a.timing and "startup timing printed" not in TIMINGS:
                print_timings()
            return 0

        urls = a.urls + (read_list(a.playlists) if a.playlists else [])
        if a.save and a.urls:
            save_cli_links(cfg, list(dict.fromkeys(a.urls)))
        if a.update:
            urls += all_urls(cfg)
        urls = list(dict.fromkeys(urls))
        if not urls:
            console.print(f"[yellow]No links saved yet. Add links to {DATA / 'playlists.txt'} or pass a URL.[/]")
            if a.timing:
                print_timings()
            return 0
        if not a.dry_run and not need_ffmpeg():
            return 1
        plan = scan(cfg, urls)
        if a.dry_run:
            for e in plan["todo"]:
                console.print(f"  [green]+[/] {e['title']}")
            console.print(f"\n[yellow]Dry run: {len(plan['todo'])} new, nothing downloaded.[/]")
        else:
            execute(cfg, plan, compact=a.compact, prune_ok=a.update)
        report_update_timing()
        if a.timing:
            print_timings()
        return 0
    except KeyboardInterrupt:
        if console is not None:
            console.print("\n[yellow]Cancelled.[/]")
            report_update_timing()
            if a.timing:
                print_timings()
        else:
            print("Cancelled.", file=sys.stderr)
        return 130
    except OSError as e:
        console.print(f"[red]Could not access the music or data folder: {e}[/]")
        console.print("[dim]Check that the music folder is connected and writable.[/]")
        return 1
    except Exception as e:
        logging.exception("Startup or command failed")
        if console is not None:
            console.print(f"[red]Error: {short(e)}[/]")
        else:
            print(f"Error: {short(e)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    main()
