#!/usr/bin/env python3
"""ytmusic-sync: YouTube playlists -> clean, tagged music library + .m3u8 playlists."""
import argparse, contextlib, hashlib, json, logging, os, queue, re, shutil, subprocess, sys, threading, time, urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
import unicodedata
import signal
from urllib.parse import parse_qs, urlencode, urlparse

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
LAST_SYNC_RESULT = None
_YT_DLP = None
_POSTPROCESSOR_TYPES = None
_ANSI_ENABLED = False
SCAN_WORKERS = 5
SCAN_CACHE = "playlist-scan-cache.json"

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
    console.print(panel_with_title(
        "Missing ffmpeg", border_color="red",
        body=Text("ffmpeg not found and the bundled copy is unavailable.\n  Windows: winget install ffmpeg\n"
                  "  macOS:   brew install ffmpeg\n  Linux:   sudo apt install ffmpeg"),
        padding=(0, 1), expand=None))
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


def playlist_id(url):
    query = parse_qs(urlparse(url).query)
    return first_text(query.get("list"), "")


def canonical_playlist_url(url):
    url = normalize_music_url(url)
    pid = playlist_id(url)
    return f"https://www.youtube.com/playlist?{urlencode({'list': pid})}" if pid else url


def dedupe_scan_urls(urls):
    result, seen = [], set()
    for url in urls:
        normalized = canonical_playlist_url(url)
        pid = playlist_id(normalized)
        key = ("playlist", pid) if pid else ("url", normalized)
        if key in seen:
            continue
        seen.add(key)
        result.append((normalized, pid))
    return result


class ScanCancelled(Exception):
    pass


class ScanState:
    def __init__(self, rows, local_ids, cache, library_size=0):
        self.lock = threading.Lock()
        self.rows = rows
        self.local_ids = local_ids
        self.cache = cache
        self.library_size = library_size
        self.latest_new = {}
        self.final_new_ids = None
        self.errors = []
        self.bad = 0
        self.started = time.monotonic()
        self.final_counts = None

    def set_status(self, index, status, **values):
        with self.lock:
            row = self.rows[index]
            row["status"] = status
            row.update(values)
            if status in ("done", "failed"):
                row["finished_at"] = time.monotonic()

    def start(self, index):
        self.set_status(index, "scanning", started=time.monotonic())

    def progress(self, index, items, total):
        with self.lock:
            row = self.rows[index]
            row["items"] = list(items)
            row["read"] = len(items)
            row["total"] = total
            for item in items:
                vid = item["id"]
                if vid not in self.local_ids and vid not in self.latest_new:
                    self.latest_new[vid] = (time.monotonic(), dict(item), index)

    def fail(self, index, url, reason, elapsed):
        with self.lock:
            row = self.rows[index]
            row.update(status="failed", error=reason, elapsed=elapsed,
                       finished_at=time.monotonic())
            self.errors.append((url[:50], reason))

    def add_bad(self, count):
        with self.lock:
            self.bad += count

    def counts(self):
        with self.lock:
            rows = [dict(row) for row in self.rows]
            final_counts = self.final_counts
            local_ids = self.local_ids
        found_ids = {item["id"] for row in rows for item in row.get("items", [])}
        found = len(found_ids)
        local = len(found_ids & local_ids)
        new = found - local
        if final_counts is not None:
            found, new, local = final_counts
        return rows, found, new, local, sum(row["status"] == "failed" for row in rows)

    def latest_new_items(self):
        with self.lock:
            items = list(self.latest_new.values())
            final_ids = self.final_new_ids
        if final_ids is not None:
            items = [item for item in items if item[1]["id"] in final_ids]
        return sorted(items, key=lambda item: item[0], reverse=True)

    def render(self):
        return render_scan(self)


def _playlist_error(reason):
    value = str(reason or "").lower()
    if "private" in value or "members-only" in value:
        return "private"
    if "timeout" in value or "timed out" in value:
        return "timeout"
    if any(token in value for token in ("unavailable", "deleted", "not found", "http error 403")):
        return "unavailable"
    return short_failure_reason(reason).lower()[:24] or "unavailable"


def _proportional_widths(counts, width):
    if width <= 0:
        return [0] * len(counts)
    total = sum(max(count, 0) for count in counts)
    if not total:
        return [0] * len(counts)
    raw = [max(count, 0) * width / total for count in counts]
    widths = [int(value) for value in raw]
    remainder = width - sum(widths)
    for index in sorted(range(len(widths)), key=lambda i: raw[i] - widths[i], reverse=True)[:remainder]:
        widths[index] += 1
    for index, count in enumerate(counts):
        if count > 0 and widths[index] == 0:
            donor = max((i for i, size_ in enumerate(widths) if size_ > 1),
                        key=lambda i: widths[i], default=None)
            if donor is not None:
                widths[donor] -= 1
                widths[index] = 1
    return widths


def _scan_bar(counts, width, colors):
    bar = Text()
    for size_, color in zip(_proportional_widths(counts, width), colors):
        bar.append("━" * size_, style=color)
    return bar


def _scan_visible_rows(rows, capacity):
    active = [i for i, row in enumerate(rows) if row["status"] == "scanning"]
    queued = [i for i, row in enumerate(rows) if row["status"] == "queued"]
    finished = [i for i, row in enumerate(rows) if row["status"] in ("done", "failed")]
    selected = list(active[:capacity])
    remaining = max(capacity - len(selected), 0)
    hidden_queued = 0
    if len(queued) > remaining and remaining:
        take = max(remaining - 1, 0)
        selected.extend(queued[:take])
        hidden_queued = len(queued) - take
    else:
        selected.extend(queued[:remaining])
        remaining -= min(len(queued), remaining)
        recent = sorted(finished, key=lambda i: rows[i].get("finished_at", 0), reverse=True)
        selected.extend(recent[:remaining])
    return selected, hidden_queued


def render_scan(state):
    from rich.console import Group
    from rich.spinner import Spinner

    width, height = console.size.width, console.size.height
    inner_width = max(width - 6, 1)
    rows, _, new, _, failed = state.counts()
    now = time.monotonic()
    elapsed = max(now - state.started, 0)
    successful = sum(row["status"] == "done" for row in rows)
    completed = successful + failed
    active = sum(row["status"] == "scanning" for row in rows)
    queued = sum(row["status"] == "queued" for row in rows)

    header = Table.grid(expand=True, padding=(0, 0))
    header.add_column(ratio=1)
    header.add_column(no_wrap=True, justify="right")
    spinner = Spinner("dots", text=Text("Scanning playlists…", style="#3b9ae1"), style="#3fb950")
    eta = "—"
    remaining = max(len(rows) - completed, 0)
    if completed >= 3 or elapsed >= 5:
        completed_times = [max(row.get("elapsed", 0), 0.01)
                           for row in rows if row["status"] in ("done", "failed")]
        average = sum(completed_times) / len(completed_times) if completed_times else elapsed
        eta = dur(average * remaining)
    right_header = Text.assemble(
        (str(completed), "#e6e6e6"), (" / ", "#8b8b8b"),
        (str(len(rows)), "#e6e6e6"), (" · Elapsed ", "#8b8b8b"),
        (dur(elapsed), "#e6e6e6"), (" · ETA ", "#8b8b8b"), (eta, "#ff9f1a"))
    header.add_row(spinner, right_header)
    failed_rows = failed
    bar = _scan_bar((successful, failed_rows, active, queued), inner_width,
                    ("#3fb950", "#f85149", "#ff9f1a", "#333333"))

    narrow = width < 100
    row_capacity = max(1, (height - 7) // (2 if narrow else 1))
    visible, hidden_queued = _scan_visible_rows(rows, row_capacity)
    show_song_columns = width >= 70
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(width=2, no_wrap=True)
    table.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    if show_song_columns:
        table.add_column(no_wrap=True, justify="right")
        table.add_column(no_wrap=True, justify="right")
    for index in visible:
        row = rows[index]
        status = row["status"]
        icon = {"done": ("✓", "#3fb950"), "scanning": ("●", "#ff9f1a"),
                "queued": ("○", "#8b8b8b"), "failed": ("✗", "#f85149")}[status]
        icon_text = Text(icon[0], style=icon[1])
        name = row.get("name") or row.get("playlist_id") or row.get("url", "")
        name_text = Text(name, style="#8b8b8b" if not row.get("title_known")
                         or status == "queued" else "#e6e6e6",
                         no_wrap=True, overflow="ellipsis")
        count_text = Text()
        status_text = Text()
        if status == "done":
            count_text.append(f"{len(row['items'])} songs", style="#8b8b8b")
            if row.get("new_count", 0):
                status_text.append(f"+{row['new_count']} new", style="#3fb950")
        elif status == "scanning":
            status_text.append("fetching…", style="#ff9f1a")
        elif status == "queued":
            status_text.append("queued", style="#8b8b8b")
        else:
            status_text.append(row.get("error", "unavailable"), style="#f85149")
        status_text.no_wrap = True
        status_text.overflow = "ellipsis"
        count_text.no_wrap = True
        cells = [icon_text, name_text]
        if show_song_columns:
            cells.extend((count_text, status_text))
        table.add_row(*cells)
    if hidden_queued:
        table.add_row("", Text(f"+ {hidden_queued} more queued", style="#8b8b8b"),
                      *([Text("")] * (len(table.columns) - 2)))

    with state.lock:
        library_size = state.library_size
        final_new_ids = state.final_new_ids
    new_ids = final_new_ids if final_new_ids is not None else {
        item["id"] for _, item, _ in state.latest_new_items()}
    if final_new_ids is not None:
        playlists_with_new = sum(row.get("new_count", 0) > 0 for row in rows)
    else:
        playlists_with_new = sum(any(item["id"] in new_ids for item in row.get("items", []))
                                 for row in rows)
    latest = state.latest_new_items()
    playlist_row_count = len(table.rows)
    fixed_sidebar_rows = 8
    area_height = max(playlist_row_count, fixed_sidebar_rows)
    title_slots = (area_height - fixed_sidebar_rows
                   if playlist_row_count >= fixed_sidebar_rows else 0)
    if len(latest) > title_slots and title_slots:
        shown_latest = latest[:title_slots - 1]
        more_latest = len(latest) - len(shown_latest)
    else:
        shown_latest = latest[:title_slots]
        more_latest = 0
    if title_slots == 0 and latest:
        more_latest = len(latest)
    area_height = max(area_height, fixed_sidebar_rows + (1 if more_latest else 0))
    while len(table.rows) < area_height:
        table.add_row(*([Text("")] * len(table.columns)))

    sidebar_width = inner_width if narrow else 28
    sidebar = Table.grid(expand=True, padding=(0, 0))
    sidebar.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    sidebar.add_row(Text("New songs", style="#8b8b8b"))
    new_line = Text()
    new_line.append(str(new), style="bold #3fb950")
    new_line.append(f" in {playlists_with_new} playlists", style="#8b8b8b")
    sidebar.add_row(new_line)
    sidebar.add_row(Text("─" * sidebar_width, style="#333333", no_wrap=True, overflow="crop"))
    sidebar.add_row(Text("Library after sync", style="#8b8b8b"))
    projected = library_size + new
    library_line = Text()
    library_line.append(str(library_size), style="#8b8b8b")
    library_line.append(" → ", style="#8b8b8b")
    library_line.append(str(projected), style="bold #e6e6e6")
    library_line.append(f" +{new}", style="#3fb950")
    library_line.append(" songs", style="#8b8b8b")
    sidebar.add_row(library_line)
    failed_line = Text()
    failed_line.append(str(failed), style="bold #f85149" if failed else "#8b8b8b")
    failed_line.append(" failed", style="#f85149" if failed else "#8b8b8b")
    sidebar.add_row(failed_line)
    sidebar.add_row(Text("─" * sidebar_width, style="#333333", no_wrap=True, overflow="crop"))
    sidebar.add_row(Text("Latest new", style="#8b8b8b"))
    for _, item, _ in shown_latest:
        title = Text(item.get("title") or item["id"], no_wrap=True, overflow="ellipsis")
        title.truncate(max(sidebar_width - 1, 1), overflow="ellipsis")
        sidebar.add_row(title)
    if more_latest:
        sidebar.add_row(Text(f"+{more_latest} more", style="#8b8b8b"))
    while len(sidebar.rows) < area_height:
        sidebar.add_row(Text(""))

    if narrow:
        content = Group(header, bar, table, sidebar)
    else:
        columns = Table.grid(expand=True, padding=(0, 0))
        columns.add_column(ratio=1)
        columns.add_column(width=22, no_wrap=True)
        columns.add_column(width=28, no_wrap=True)
        columns.add_row(table, Text("│" + " " * 21, style="#333333", no_wrap=True), sidebar)
        content = Group(header, bar, columns)
    return Group(panel_with_title("Update library", border_color="#5a5a5a", body=content),
                 footer_hint("q cancel scan"))


class ScanKeyListener:
    def __init__(self, stop_event):
        self.stop_event = stop_event
        self.done = threading.Event()
        self.thread = None
        self.old_terminal = None

    def start(self):
        if sys.stdin.isatty():
            self.thread = threading.Thread(target=self._run, name="playlist-scan-keys", daemon=True)
            self.thread.start()

    def stop(self):
        self.done.set()
        if self.thread:
            self.thread.join(timeout=0.25)

    def _run(self):
        try:
            if os.name == "nt":
                import msvcrt
                while not self.done.is_set() and not self.stop_event.is_set():
                    if not msvcrt.kbhit():
                        time.sleep(0.05)
                        continue
                    key = msvcrt.getwch()
                    if key.lower() == "q" or key == "\x03":
                        self.stop_event.set()
                        return
            else:
                import select
                import termios
                import tty
                fd = sys.stdin.fileno()
                self.old_terminal = termios.tcgetattr(fd)
                tty.setcbreak(fd)
                while not self.done.is_set() and not self.stop_event.is_set():
                    ready, _, _ = select.select([fd], [], [], 0.1)
                    if ready:
                        key = os.read(fd, 1)
                        if key.lower() == b"q" or key == b"\x03":
                            self.stop_event.set()
                            return
        finally:
            if self.old_terminal is not None:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_terminal)
                self.old_terminal = None


def _flat_item(entry):
    if not entry or not entry.get("id"):
        return None
    title = first_text(entry.get("title"), entry["id"])
    if title in ("[Private video]", "[Deleted video]"):
        return None
    return {"id": entry["id"], "title": title, "dur": int(entry.get("duration") or 0)}


def _extract_entries(ytdlp, url, row_index, state, stop_event, playlist_limit=None):
    if playlist_limit is not None:
        ytdlp.params["playlist_items"] = str(playlist_limit)
    else:
        ytdlp.params.pop("playlist_items", None)
    try:
        info = ytdlp.extract_info(url, download=False)
    finally:
        ytdlp.params.pop("playlist_items", None)
    if not info:
        raise ValueError("No playlist or song information found")
    raw_entries = info.get("entries")
    if raw_entries is None:
        item = _flat_item(info)
        return info, ([item] if item else []), 0

    total = info.get("playlist_count") or info.get("n_entries")
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    title = first_text(info.get("title"), "")
    if title:
        state.set_status(row_index, "scanning", name=fsafe(title), title_known=True, total=total)
    else:
        state.set_status(row_index, "scanning", total=total)
    items, skipped = [], 0
    for entry in raw_entries:
        if stop_event.is_set():
            break
        item = _flat_item(entry)
        if item:
            items.append(item)
        else:
            skipped += 1
        state.progress(row_index, items, total)
    return info, items, skipped


def scan(cfg, urls):
    out = Path(cfg["out"])
    lib = get_lib(out)
    playlist_urls = dedupe_scan_urls(urls)
    with lib.lock:
        songs = dict(lib.d["songs"])
        aliases = dict(lib.d["aliases"])
    cache_path = DATA / SCAN_CACHE
    cache = jload(cache_path, {})
    if not isinstance(cache, dict):
        cache = {}
    rows = [{"url": url, "playlist_id": pid, "status": "queued",
             "name": (pid[:13] + ("…" if len(pid) > 13 else "")) if pid else url[:13],
             "title_known": False,
             "items": [], "read": 0, "total": None, "cached": False, "error": "",
             "elapsed": 0, "new_count": 0}
            for url, pid in playlist_urls]
    local_ids = set(songs) | set(aliases)
    state = ScanState(rows, local_ids, cache, library_size=len(songs))
    stop_event = threading.Event()
    listener = ScanKeyListener(stop_event)
    thread_local = threading.local()
    ytdlp_instances, ytdlp_lock = [], threading.Lock()
    results = [None] * len(rows)

    def worker_ytdlp():
        if not hasattr(thread_local, "ytdlp"):
            options = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
                       "skip_download": True, "socket_timeout": 10, "retries": 2,
                       "lazy_playlist": True, "logger": Quiet()}
            if cfg.get("cookies"):
                options["cookiesfrombrowser"] = (cfg["cookies"],)
            thread_local.ytdlp = get_ytdlp().YoutubeDL(options)
            with ytdlp_lock:
                ytdlp_instances.append(thread_local.ytdlp)
        return thread_local.ytdlp

    def scan_one(index):
        if stop_event.is_set():
            return None
        row = rows[index]
        state.start(index)
        started = time.monotonic()
        try:
            ytdlp = worker_ytdlp()
            pid = row["playlist_id"]
            if pid:
                probe, first_items = _extract_entries(
                    ytdlp, row["url"], index, state, stop_event, playlist_limit=1)[:2]
                if stop_event.is_set():
                    return None
                probe_count = probe.get("playlist_count") or probe.get("n_entries")
                cached = state.cache.get(pid)
                first_id = first_items[0]["id"] if first_items else None
                cache_time = cached.get("scanned_at", 0) if isinstance(cached, dict) else 0
                cache_count = cached.get("count") if isinstance(cached, dict) else None
                cache_ids = cached.get("ids", []) if isinstance(cached, dict) else []
                fresh = isinstance(cache_time, (int, float)) and time.time() - cache_time < 86400
                try:
                    same_count = int(probe_count) == int(cache_count)
                except (TypeError, ValueError):
                    same_count = False
                if fresh and same_count and first_id and cache_ids and first_id == cache_ids[0]:
                    cached_items = cached.get("items") or [
                        {"id": vid, "title": vid, "dur": 0} for vid in cache_ids]
                    items = [{"id": item["id"], "title": item.get("title") or item["id"],
                              "dur": int(item.get("dur") or 0)} for item in cached_items]
                    name = fsafe(cached.get("name") or probe.get("title") or "playlist")
                    state.progress(index, items, len(items))
                    new_count = sum(item["id"] not in state.local_ids for item in items)
                    state.set_status(index, "done", name=name, title_known=True, items=items, read=len(items),
                                     total=len(items), cached=True, elapsed=time.monotonic() - started,
                                     new_count=new_count)
                    state.add_bad(int(cached.get("bad", 0)))
                    results[index] = {"playlist": {"name": name, "items": items},
                                      "single": None, "bad": int(cached.get("bad", 0)),
                                      "url": row["url"], "cached": True}
                    return results[index]

            info, items, skipped = _extract_entries(
                ytdlp, row["url"], index, state, stop_event)
            if stop_event.is_set():
                return None
            is_single = info.get("entries") is None
            name = fsafe(info.get("title") or row["name"])
            if is_single:
                name = items[0]["title"] if items else name
            elapsed = time.monotonic() - started
            new_count = sum(item["id"] not in state.local_ids for item in items)
            state.set_status(index, "done", name=name,
                             title_known=bool(info.get("title") or is_single), items=items, read=len(items),
                             total=len(items), cached=False, elapsed=elapsed, new_count=new_count)
            if row["playlist_id"]:
                record = {"ids": [item["id"] for item in items],
                          "count": int(info.get("playlist_count") or info.get("n_entries") or len(items) + skipped),
                          "scanned_at": time.time(), "name": name, "items": items, "bad": skipped}
                with state.lock:
                    state.cache[row["playlist_id"]] = record
                    try:
                        ensure_data_dir()
                        aw(cache_path, json.dumps(state.cache, ensure_ascii=False))
                    except OSError as exc:
                        logging.warning("Could not save playlist scan cache: %s", exc)
            state.add_bad(skipped)
            result = {"playlist": None if is_single else {"name": name, "items": items},
                      "single": items[0] if is_single and items else None, "bad": skipped,
                      "url": row["url"], "cached": False}
            results[index] = result
            return result
        except Exception as exc:
            if stop_event.is_set():
                return None
            reason = _playlist_error(exc)
            state.fail(index, row["url"], reason, time.monotonic() - started)
            return None

    executor = ThreadPoolExecutor(max_workers=SCAN_WORKERS, thread_name_prefix="playlist-scan")
    futures = []
    cancelled = False
    try:
        with Live(get_renderable=state.render, console=console, refresh_per_second=8,
                  transient=False, vertical_overflow="crop"):
            listener.start()
            futures = [executor.submit(scan_one, index) for index in range(len(rows))]
            pending = set(futures)
            while pending:
                if stop_event.is_set():
                    cancelled = True
                    for future in pending:
                        future.cancel()
                    break
                completed, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in completed:
                    if not future.cancelled():
                        future.result()
    except KeyboardInterrupt:
        cancelled = True
        stop_event.set()
        for future in futures:
            future.cancel()
    finally:
        listener.stop()
        if cancelled:
            for future in futures:
                future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        for ytdlp in ytdlp_instances:
            close = getattr(ytdlp, "close", None)
            if close:
                try:
                    close()
                except Exception:
                    logging.exception("Could not close a playlist scan worker")
    if cancelled:
        raise ScanCancelled()

    pls, singles = [], []
    for result in results:
        if not result:
            continue
        if result["playlist"]:
            pls.append(result["playlist"])
        if result["single"]:
            singles.append(result["single"])
    entries = [item for playlist in pls for item in playlist["items"]] + singles
    want = {}
    for entry in entries:
        want.setdefault(entry["id"], entry)

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
    for vid, entry in want.items():
        if vid in aliases:
            continue
        key, seconds = song_key(entry.get("title", "")), int(entry.get("dur") or 0)
        match = next((kept for old_seconds, kept in titles.get(key, [])
                      if skip_dupes and key and seconds > 0 and kept != vid and abs(seconds - old_seconds) <= 3), None)
        if match:
            aliases[vid] = match
            continue
        unique[vid] = entry
        if key and seconds > 0:
            titles.setdefault(key, []).append((seconds, vid))

    canonical = {}
    for vid, entry in want.items():
        kept = canonical_id(vid, aliases)
        if kept in unique:
            canonical.setdefault(kept, unique[kept])
        elif kept in songs:
            canonical.setdefault(kept, {"id": kept, "title": songs[kept].get("title", entry["title"]),
                                        "dur": int(songs[kept].get("dur") or entry.get("dur") or 0)})
        else:
            canonical.setdefault(vid, entry)
    todo = [entry for vid, entry in canonical.items() if vid not in songs]
    todo_ids = {entry["id"] for entry in todo}
    with lib.lock:
        lib.d["aliases"] = aliases
    found = len(canonical)
    new = len(todo)
    with state.lock:
        state.final_counts = (found, new, found - new)
        state.final_new_ids = todo_ids
        for row in state.rows:
            if row["status"] == "done":
                row["new_count"] = len({canonical_id(item["id"], aliases) for item in row["items"]
                                        if canonical_id(item["id"], aliases) in todo_ids})
        bad_count = state.bad
        errors = list(state.errors)
    plan = dict(out=out, lib=lib, pls=pls, singles=singles, bad=bad_count, errs=errors, want=want, unique=canonical,
                todo=todo, aliases=aliases, entries=len(entries), repeats=len(entries) - len(want),
                duplicate_count=sum(1 for vid in want if vid in aliases),
                cache_hits=sum(bool(result and result.get("cached")) for result in results),
                scan_elapsed=time.monotonic() - state.started, scan_state=state)
    print_scan_summary(plan)
    return plan


def print_scan_summary(plan):
    from rich.cells import cell_len

    _, _, new, _, failed = plan["scan_state"].counts()
    details = (f"Scanned {len(plan['scan_state'].rows)} playlists", f"{new} new",
               f"{failed} failed", f"{plan['scan_elapsed']:.1f}s")
    separator = " · "
    if cell_len("✓ " + separator.join(details)) > console.size.width:
        separator = "·"
    text = Text()
    text.append("✓", style="#3fb950")
    text.append(" " + separator.join(details))
    console.print(text, no_wrap=True)


# ---------- live dashboard ----------
class StopRequested(Exception):
    pass


class WorkerStatus:
    def __init__(self, state, worker_id):
        self.state, self.worker_id = state, worker_id

    def set_track(self, title, artist):
        self.state.set_track(self.worker_id, title, artist)


def build_overall_panel(state, width):
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
    inner_width = max(width - 4, 1)
    percent_text = Text(f"{percent * 100:.1f}%", style="bold #ff9f1a")
    count_text = Text(f"{finished} / {total}", style="#8b8b8b")
    bar_width = max(inner_width - cell_len(percent_text.plain) - cell_len(count_text.plain) - 2, 0)

    counts = [done, failed, active, left]
    widths = _proportional_widths(counts, bar_width)
    if not any(counts):
        widths = [0, 0, 0, bar_width]

    bar = Text()
    for segment_width, color in zip(widths, ("#3fb950", "#f85149", "#ff9f1a", "#333333")):
        bar.append("━" * segment_width, style=color)
    row = Text()
    row.append(percent_text)
    row.append(" ")
    row.append(bar)
    row.append(" ")
    row.append(count_text)
    row.no_wrap = True
    row.overflow = "crop"

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
    stats.no_wrap = True
    stats.overflow = "crop"

    return panel_with_title(
        "Downloading", "Update library", "#5a5a5a",
        Group(row, stats), padding=(1, 1), subtitle_color="#6b6b6b")


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
        table = Table(expand=True, border_style="#5a5a5a")
        table.add_column("Title", style="bold #ffffff", ratio=4,
                         no_wrap=True, overflow="ellipsis")
        table.add_column("Artist", style="#8b8b8b", ratio=2,
                         no_wrap=True, overflow="ellipsis")
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
        return Group(
            panel_with_title("Failures", f"{len(failures)} total", "#5a5a5a",
                             Group(*parts)),
            footer_hint("press any key to return"))

    visible_count = max(1, min(len(display_rows), max(height - 10, 1)))
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
        from rich.cells import cell_len

        orange, green, red, grey = "#ff9f1a", "#3fb950", "#f85149", "#8b8b8b"
        stage_names = ("Fetch", "Download", "Convert", "Tag")
        for worker_id, state in workers:
            if not state["active"]:
                continue
            inner_width = max(width - 4, 1)
            title_line = Text()
            title_line.append(f"W{worker_id + 1}  ", style=orange)
            title_line.append(state["title"] or "Waiting for data…", style="bold #e6e6e6")
            if state["artist"]:
                title_line.append(f" · {state['artist']}", style=grey)
            if state["dl"] > 0:
                if max(state["dl"], state["total"]) >= 1024 * 1024:
                    amount = (f"{state['dl'] / (1024 * 1024):.1f} / "
                              f"{state['total'] / (1024 * 1024):.1f} MB")
                elif max(state["dl"], state["total"]) >= 1024:
                    amount = f"{state['dl'] / 1024:.0f} / {state['total'] / 1024:.0f} KB"
                else:
                    amount = f"{state['dl']:.0f} / {state['total']:.0f} B"
                if state["speed"] >= 1024 * 1024:
                    speed = f"{state['speed'] / (1024 * 1024):.1f} MB/s"
                elif state["speed"] >= 1024:
                    speed = f"{state['speed'] / 1024:.0f} KB/s"
                else:
                    speed = f"{state['speed']:.0f} B/s"
                right_text = f"{amount} · {speed}"
            else:
                right_text = "waiting for data…"
            title_line.truncate(max(inner_width - cell_len(right_text) - 2, 1),
                                overflow="ellipsis")
            title_line.no_wrap = True
            title_line.overflow = "crop"
            line1 = Table.grid(expand=True, padding=0)
            line1.add_column(ratio=1, no_wrap=True)
            line1.add_column(no_wrap=True, justify="right")
            line1.add_row(title_line, Text(right_text, style=grey))

            stage = min(max(state["stage"], 0), len(stage_names) - 1)
            progress = min(state["dl"] / state["total"], 1) if state["total"] else 0
            percent_style = green if progress >= 1 else orange if progress > 0 else "#5a5a5a"
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
                    chips.append(f"○ {name}", style="#5a5a5a")
            chips.no_wrap = True
            bar_cells = max(inner_width - cell_len(percent_text.plain) - cell_len(chips.plain) - 2, 0)
            filled = min(int(bar_cells * progress), bar_cells)
            progress_bar = Text("━" * filled, style=green if progress >= 1 else orange)
            progress_bar.append("━" * (bar_cells - filled), style="#333333")
            progress_bar.no_wrap = True
            line2 = Table.grid(expand=True, padding=0)
            line2.add_column(width=cell_len(percent_text.plain) + 1, no_wrap=True)
            line2.add_column(ratio=1, no_wrap=True)
            line2.add_column(no_wrap=True, justify="right")
            line2.add_row(percent_text, progress_bar, chips)
            blocks.append(panel_with_title(None, border_color="#6b4a12",
                                           body=Group(line1, line2), padding=(0, 1)))

        footer = f"f failures ({failed})" if failed else "f failures"
        footer += " · q stop safely · Ctrl+C force quit"
        blocks.append(footer_hint(footer))
        return Group(*blocks)


class DashboardKeys:
    def __init__(self, state, stop_event, gate, live, quit_steps):
        self.state, self.stop_event, self.gate = state, stop_event, gate
        self.live = live
        self.quit_steps = quit_steps
        self.quit_requested = threading.Event()
        self.quit_started_at = None
        self.quit_done = threading.Event()
        self.quit_screen_open = threading.Event()
        self.quit_screen_closed = threading.Event()
        self.quit_screen_closed.set()
        self.quit_status_lock = threading.Lock()
        self.quit_status = {key: "pending" for key, _, _, _ in self.quit_steps}
        self.quit_active = None
        self.quit_elapsed = 0.0
        self.quit_error = None
        self.last_q_at = 0.0
        self.done = threading.Event()
        self.view_open = threading.Event()
        self.view_closed = threading.Event()
        self.view_closed.set()
        self.thread = None
        self.old_terminal = None

    def start(self):
        if not sys.stdin.isatty():
            return False
        self._flush_input()
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
            keys = []
            while msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in ("\x00", "\xe0"):
                    if msvcrt.kbhit():
                        msvcrt.getwch()
                    continue
                keys.append(key)
            if not keys:
                time.sleep(0.05)
                return None
            keys = [key for key in keys if key != "\x03"]
            if not keys:
                return None
            for key in reversed(keys):
                if key.lower() == "q":
                    return "q"
            return keys[-1]
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
        if self.quit_requested.is_set():
            return False
        self.quit_started_at = time.monotonic()
        self.stop_event.set()
        self.gate.wake()
        self.quit_requested.set()
        return True

    def render_quit(self):
        from rich.spinner import Spinner

        with self.quit_status_lock:
            status = dict(self.quit_status)
        lines = []
        for key, label, _, _ in self.quit_steps:
            value = status[key]
            if value == "done":
                lines.append(Text(f"✓ {label}", style="#3fb950"))
            elif value == "active":
                lines.append(Spinner("dots", text=Text(f"{label}", style="#ff9f1a"),
                                     style="#ff9f1a"))
            elif value == "failed":
                lines.append(Text(f"✗ {label}", style="#f85149"))
            else:
                lines.append(Text(f"○ {label}", style="#5a5a5a"))
        with self.quit_status_lock:
            finished = sum(value in ("done", "failed") for value in self.quit_status.values())
        total = len(self.quit_steps)
        bar_width = max(console.size.width - 6, 1)
        widths = _proportional_widths((finished, total - finished), bar_width)
        bar = Text("━" * widths[0], style="#ff9f1a")
        bar.append("━" * widths[1], style="#333333")
        bar.no_wrap = True
        elapsed = Table.grid(expand=True, padding=0)
        elapsed.add_column(ratio=1, no_wrap=True)
        elapsed.add_column(no_wrap=True, justify="right")
        elapsed.add_row(Text("Your finished songs are saved.", style="#8b8b8b"),
                        Text.assemble(("Elapsed ", "#8b8b8b"),
                                     (f"{self.quit_elapsed:.1f}s", "#e6e6e6"),
                                     no_wrap=True))
        body = Group(*lines, bar, elapsed)
        return Group(panel_with_title("Stopping safely", body=body,
                                      border_color="#5a5a5a"),
                     footer_hint("Ctrl+C exit now"))

    def restore_terminal_mode(self):
        if self.old_terminal is not None and os.name != "nt":
            with contextlib.suppress(Exception):
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_terminal)
            self.old_terminal = None

    def _show_quit_screen(self):
        self.quit_screen_closed.clear()
        try:
            if self.live is not None and self.live.is_started:
                self.live.stop()
            with console.screen():
                console.clear()
                quit_live = Live(get_renderable=self.render_quit, console=console,
                                 refresh_per_second=10, transient=True, screen=False,
                                 vertical_overflow="crop")
                with quit_live:
                    self.quit_screen_open.set()
                    for key, _, action, timeout in self.quit_steps:
                        started = time.monotonic()
                        completed = threading.Event()

                        def run_action(action=action, completed=completed, key=key):
                            try:
                                if action:
                                    action()
                            except Exception:
                                logging.exception("Safe-stop step failed")
                                with self.quit_status_lock:
                                    self.quit_status[key] = "failed"
                                    self.quit_error = key
                            finally:
                                completed.set()

                        with self.quit_status_lock:
                            self.quit_status[key] = "active"
                            self.quit_active = key
                        threading.Thread(target=run_action, name=f"safe-stop-{key}",
                                         daemon=True).start()
                        timed_out = False
                        while True:
                            elapsed = time.monotonic() - started
                            self.quit_elapsed = max(time.monotonic() - self.quit_started_at, 0)
                            if completed.is_set() and elapsed >= 1.0:
                                break
                            if elapsed >= timeout:
                                timed_out = not completed.is_set()
                                break
                            time.sleep(0.1)
                        with self.quit_status_lock:
                            if timed_out and self.quit_status[key] != "failed":
                                self.quit_status[key] = "failed"
                                self.quit_error = key
                            elif self.quit_status[key] != "failed":
                                self.quit_status[key] = "done"
                            self.quit_active = None
                    self.quit_elapsed = max(time.monotonic() - self.quit_started_at, 0)
                    time.sleep(0.5)
                    self.quit_done.set()
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
                    key = self._read_key()
                    if key is not None:
                        if key.lower() == "q":
                            self.request_quit()
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
        try:
            if os.name != "nt":
                import termios
                import tty
                fd = sys.stdin.fileno()
                self.old_terminal = termios.tcgetattr(fd)
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
                    now = time.monotonic()
                    if (now - self.last_q_at >= 0.5 and self.request_quit()):
                        self.last_q_at = now
                        self._show_quit_screen()
                        return
        except Exception:
            logging.exception("Dashboard key handling failed")
        finally:
            self.restore_terminal_mode()


def run_demo():
    ensure_ui()
    import random

    playlist_count = 14
    local_ids = {f"demo-{i}-track-{j}" for i in range(playlist_count) for j in range(12)}
    rows = [{"url": f"demo:{i}", "playlist_id": f"demo-{i}", "status": "queued",
             "name": f"Demo Playlist {i + 1}", "items": [], "read": 0, "total": None,
             "cached": False, "error": "", "elapsed": 0, "new_count": 0}
            for i in range(playlist_count)]
    state = ScanState(rows, local_ids, {})
    stop_event = threading.Event()
    listener = ScanKeyListener(stop_event)
    console.clear()

    def demo_playlist(index):
        if stop_event.is_set():
            return
        started = time.monotonic()
        state.start(index)
        if index == 0:
            time.sleep(0.25)
            items = [{"id": f"demo-0-track-{i}", "title": f"Demo Song {i + 1}", "dur": 180}
                     for i in range(15)]
            state.progress(index, items, len(items))
            state.set_status(index, "done", items=items, read=len(items), total=len(items),
                             cached=True, elapsed=time.monotonic() - started, new_count=3)
            return
        if index == 6:
            time.sleep(random.uniform(0.25, 0.65))
            state.set_status(index, "failed", error="unavailable", elapsed=time.monotonic() - started)
            return
        total = random.randint(18, 38)
        items = []
        for track in range(total):
            if stop_event.is_set():
                return
            items.append({"id": f"demo-{index}-track-{track}",
                          "title": f"Demo Song {track + 1}", "dur": 180})
            state.progress(index, items, total)
            time.sleep(random.uniform(0.015, 0.04))
        new_count = sum(item["id"] not in local_ids for item in items)
        state.set_status(index, "done", items=items, read=len(items), total=total,
                         cached=False, elapsed=time.monotonic() - started, new_count=new_count)

    executor = ThreadPoolExecutor(max_workers=SCAN_WORKERS, thread_name_prefix="playlist-scan-demo")
    futures = []
    started = time.monotonic()
    cancelled = False
    try:
        with Live(get_renderable=state.render, console=console, refresh_per_second=8,
                  transient=False, vertical_overflow="crop"):
            listener.start()
            futures = [executor.submit(demo_playlist, i) for i in range(playlist_count)]
            pending = set(futures)
            while pending:
                if stop_event.is_set():
                    cancelled = True
                    for future in pending:
                        future.cancel()
                    break
                completed, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in completed:
                    future.result()
    except KeyboardInterrupt:
        cancelled = True
        stop_event.set()
        for future in futures:
            future.cancel()
    finally:
        listener.stop()
        if cancelled:
            for future in futures:
                future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
    if cancelled:
        console.print(Text("Scan cancelled.", style="#8b8b8b"))
        return
    _, found, new, local, _ = state.counts()
    with state.lock:
        state.final_counts = (found, new, local)
    print_scan_summary({"scan_state": state, "scan_elapsed": time.monotonic() - started})


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
                        if stop_event.is_set() or force_event.is_set():
                            raise StopRequested()
                        with lib.lock:
                            if stop_event.is_set() or force_event.is_set():
                                raise StopRequested()
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
    global LAST_SYNC_RESULT
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
    quit_handled = False
    quit_save_failed = [False]
    quit_saved = [False]
    quit_save_lock = threading.Lock()
    forced_quit = threading.Event()
    forced_quit_lock = threading.Lock()
    live = contextlib.nullcontext() if plain else \
        Live(get_renderable=st.render, console=console, refresh_per_second=8, transient=False, screen=True,
             vertical_overflow="crop")
    live_obj = live if isinstance(live, Live) else None
    old_sigint, signal_installed = None, False
    signal_stopped = False

    def force_quit_now():
        with forced_quit_lock:
            if forced_quit.is_set():
                os._exit(0)
            forced_quit.set()
        stop_event.set()
        force_event.set()
        gate.wake()
        if executor:
            executor.shutdown(wait=False, cancel_futures=True)
        terminate_child_processes(force=True)
        if key_listener:
            key_listener.restore_terminal_mode()
        try:
            save_quit_state()
        except Exception:
            logging.exception("Could not save library state during force quit")
        try:
            write_inflight_file()
        except Exception:
            logging.exception("Could not write inflight state during force quit")
        with contextlib.suppress(Exception):
            console.show_cursor(True)
            console.set_alt_screen(False)
            console.clear()
            console.print("Force quit")
            console.file.flush()
        os._exit(0)

    def handle_sigint(_signum, _frame):
        force_quit_now()

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

    def write_inflight_file():
        with lib.lock:
            saved_ids = set(lib.d["songs"])
        interrupted = [item["id"] for item in todo if item["id"] not in saved_ids]
        aw(DATA / "inflight.json", json.dumps({
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "video_ids": interrupted,
        }, ensure_ascii=False))

    def save_quit_state():
        with quit_save_lock:
            if quit_saved[0]:
                return
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
            quit_saved[0] = True

    def hard_quit_watchdog(pending, started):
        deadline = started + 8
        while time.monotonic() < deadline and any(not future.done() for future in pending):
            time.sleep(0.05)
        if not any(not future.done() for future in pending):
            return
        force_quit_now()

    def cancel_downloads():
        stop_event.set()
        gate.wake()
        if executor:
            executor.shutdown(wait=False, cancel_futures=True)
        terminate_child_processes(timeout=2)

    def finish_conversions():
        pending = {future for future in futures if not future.done()}
        if pending:
            wait(pending, timeout=3)
            pending = {future for future in pending if not future.done()}
        if pending:
            force_event.set()
            terminate_child_processes(force=True)
            wait(pending, timeout=0.1)
            threading.Thread(target=hard_quit_watchdog,
                             args=(set(pending), time.monotonic()),
                             name="quit-timeout", daemon=True).start()

    def save_library_and_playlists():
        try:
            save_quit_state()
        except Exception:
            quit_save_failed[0] = True
            raise

    def clean_stop_files():
        cleanup_partial_files(out, lib)
        (DATA / "inflight.json").unlink(missing_ok=True)

    def finish_quit():
        if key_listener and key_listener.thread is None:
            key_listener.start()
        if key_listener:
            key_listener.quit_done.wait()

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
            try:
                executor = ThreadPoolExecutor(max_workers=worker_count)
                key_listener = DashboardKeys(
                    st, stop_event, gate, live_obj,
                    quit_steps=[
                        ("downloads", "Cancelling downloads", cancel_downloads, 10),
                        ("converters", "Finishing conversions", finish_conversions, 3),
                        ("saving", "Saving library and playlists", save_library_and_playlists, 10),
                        ("cleaning", "Cleaning up temp files", clean_stop_files, 10),
                    ]) if not plain else None
                if key_listener:
                    key_listener.start()
                if threading.current_thread() is threading.main_thread():
                    try:
                        old_sigint = signal.getsignal(signal.SIGINT)
                        signal.signal(signal.SIGINT, handle_sigint)
                        signal_installed = True
                    except (ValueError, OSError):
                        signal_installed = False
                for worker_id in range(worker_count):
                    futures.append(executor.submit(
                        download_worker, worker_id, work, cfg, out, lib, st, gate, stop_event,
                        failed, failed_lock, playlist_by_id, plain, force_event))
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
            except KeyboardInterrupt:
                stopped = True
                signal_stopped = True
                force_quit_now()
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
        if signal_installed:
            signal.signal(signal.SIGINT, old_sigint)
            signal_installed = False
        with st.lock:
            downloads, failures = st.done, st.fail
            left = max(st.total - st.done - st.worker_fail, 0)
        LAST_SYNC_RESULT = {"out": str(Path(out).expanduser().resolve()),
                            "new": downloads, "failed": failures,
                            "elapsed": time.time() - st.t0}
        stop_warning = (f" · {key_listener.quit_error} step failed or timed out"
                        if key_listener.quit_error else "")
        console.show_cursor(True)
        console.print(
            f"Stopped in {max(time.monotonic() - (key_listener.quit_started_at or time.monotonic()), 0):.1f} s · "
            f"{downloads} done · {failures} failed · {left} left · "
            f"{'progress save failed' if quit_save_failed[0] else 'progress saved'}{stop_warning}")
        raise SystemExit(0)

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
    (DATA / "inflight.json").unlink(missing_ok=True)
    el = time.time() - st.t0
    with st.lock:
        downloads, downloaded_bytes, failures = st.done, st.bytes, st.fail
        interrupted = max(st.total - st.done - st.worker_fail, 0) if stopped else 0
        st.interrupted = interrupted
    LAST_SYNC_RESULT = {"out": str(Path(out).expanduser().resolve()),
                        "new": downloads, "failed": failures, "elapsed": el}
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
    console.print(panel_with_title(
        "Library", border_color="#3a3a3a",
        body=Text(f"{len(songs)} songs · {size(lib.total_size())} · {playlist_count} playlists\n"
                  f"All-time: {s['downloads']} downloads · {s['runs']} runs"),
        padding=(0, 1), expand=None))
    console.print("[dim]Run again anytime. Only new songs download.[/]")
    if signal_installed:
        signal.signal(signal.SIGINT, old_sigint)
        signal_installed = False
    return stopped


def show_stats(cfg, wait_for_key=False):
    from rich import box
    from rich.cells import cell_len

    lib = get_lib(cfg["out"])
    with lib.lock:
        songs = dict(lib.d["songs"])
        playlists = dict(lib.d["playlists"])
        stats = dict(lib.d["stats"])
        aliases = dict(lib.d["aliases"])

    playlist_counts = []
    for name, ids in playlists.items():
        kept = {canonical_id(i, aliases) for i in ids}
        playlist_counts.append((name, sum(1 for i in kept if i in songs)))
    playlist_counts.sort(key=lambda item: (-item[1], item[0].casefold()))
    unique_count = len(songs)
    entry_count = sum(count for _, count in playlist_counts)
    show_note = entry_count > unique_count
    width = max(console.size.width, 1)
    inner_width = max(width - 6, 1)

    tile_values = [
        ("Songs", str(unique_count)),
        ("Size", size(lib.total_size())),
        ("Downloads", str(stats["downloads"])),
        ("Runs", str(stats["runs"])),
    ]
    tile_columns = 4 if width >= 80 else 2
    tiles = Table.grid(expand=True, padding=(0, 1))
    for _ in range(tile_columns):
        tiles.add_column(ratio=1)

    def tile(label, value):
        content = Group(Text(label, style="#8b8b8b", no_wrap=True),
                        Text(value, style="bold #ff9f1a", no_wrap=True))
        return Panel(content, border_style="#4a3a1a", box=box.ROUNDED,
                     padding=(0, 1), expand=True)

    tile_items = [tile(label, value) for label, value in tile_values]
    for start in range(0, len(tile_items), tile_columns):
        tiles.add_row(*tile_items[start:start + tile_columns])

    playlist_header = Table.grid(expand=True, padding=0)
    playlist_header.add_column(ratio=1, no_wrap=True)
    playlist_header.add_column(no_wrap=True, justify="right")
    playlist_header.add_row(
        Text("Playlists", style="bold #f5a623"),
        Text(f"{len(playlist_counts)} total · songs each", style="#8b8b8b"))

    max_count = max((count for _, count in playlist_counts), default=0)
    rank_width, count_width = 3, max(3, max((len(str(count)) for _, count in playlist_counts), default=1))
    name_width = min(28, max(8, int(inner_width * 0.35)))
    while rank_width + count_width + name_width + 3 >= inner_width and name_width > 1:
        name_width -= 1
    bar_width = max(inner_width - rank_width - count_width - name_width - 3, 1)

    def playlist_line(rank, name, count):
        top = rank <= 3
        line = Text(f"{rank:>{rank_width}} ", style="#8b8b8b" if top else "#5a5a5a")
        name_text = truncate_display(name, name_width)
        line.append(name_text.plain, style="bold #ffffff" if top else "#e6e6e6")
        line.append(" " * max(name_width - cell_len(name_text.plain), 0))
        line.append(" ")
        filled = (min(bar_width, max(1, round(bar_width * count / max_count)))
                  if count > 0 and max_count else 0)
        line.append("━" * filled, style="#ff9f1a" if top else "#8a5a12")
        line.append("━" * (bar_width - filled), style="#2e2e2e")
        line.append(" ")
        line.append(f"{count:>{count_width}}", style="#ff9f1a" if top else "#8b8b8b")
        line.no_wrap = True
        line.overflow = "crop"
        return line

    tile_height = 4 if tile_columns == 4 else 8
    available_rows = max(0, console.size.height - tile_height - 8 - int(show_note))
    if len(playlist_counts) > available_rows:
        visible_count = max(available_rows - 1, 0)
    else:
        visible_count = len(playlist_counts)
    playlist_lines = [
        playlist_line(index, name, count)
        for index, (name, count) in enumerate(playlist_counts[:visible_count], 1)
    ]
    if len(playlist_counts) > visible_count and available_rows:
        playlist_lines.append(Text(f"+ {len(playlist_counts) - visible_count} more",
                                   style="#8b8b8b", no_wrap=True))

    body = [tiles, playlist_header, *playlist_lines]
    if show_note:
        note = Text(f"{entry_count} playlist entries · {unique_count} unique songs "
                    "(a song in several playlists counts once)",
                    style="#5a5a5a", no_wrap=True, overflow="ellipsis")
        body.append(note)
    body.append(Text("─" * inner_width, style="#4a3a1a", no_wrap=True, overflow="crop"))

    last_sync = ago(stats.get("last_run", "never"))
    library_path = str(cfg["out"])
    data_path = short_data_dir()
    fixed_width = cell_len(f"Last sync {last_sync} ·  · Data ")
    library_budget = max(inner_width - fixed_width - cell_len(data_path), 0)
    if cell_len(library_path) > library_budget:
        library_path = _middle_truncate(library_path, library_budget)
    remaining = inner_width - cell_len(
        f"Last sync {last_sync} · {library_path} · Data {data_path}")
    if remaining < 0:
        data_path = _middle_truncate(data_path, max(cell_len(data_path) + remaining, 0))
    last_line = Text.assemble(
        ("Last sync ", "#8b8b8b"), (last_sync, "#e6e6e6"),
        (" · ", "#8b8b8b"), (library_path, "#6b6b6b"),
        (" · Data ", "#8b8b8b"), (data_path, "#6b6b6b"),
        no_wrap=True, overflow="crop")
    if cell_len(last_line.plain) > inner_width:
        last_line.truncate(inner_width, overflow="crop")
    body.append(last_line)
    panel = panel_with_title("♪ Stats", "Library overview", "#f5a623",
                             Group(*body), name_color="#f5a623")
    if wait_for_key:
        console.clear()
    console.print(Group(panel, footer_hint("enter or esc back")))
    if wait_for_key:
        try:
            with TerminalKeyReader() as reader:
                while reader.read_key() not in ("enter", "escape", "ctrl_c"):
                    pass
        except KeyboardInterrupt:
            return


# ---------- interactive menus (every menu has "‹ Back"; Ctrl+C = back/exit) ----------
def ask(msg, choices):
    try:
        return questionary.select(msg, choices=[questionary.Choice(t, v) for t, v in choices],
                                  style=STYLE, instruction=" ").ask()
    except KeyboardInterrupt:
        return None


def panel_with_title(title, subtitle=None, border_color="#5a5a5a", body=None,
                     name_color="#ff9f1a", subtitle_color="#8b8b8b",
                     padding=(1, 2), expand=True):
    from rich import box
    from rich.cells import cell_len
    from rich.style import Style

    max_title_width = max(console.size.width - 6, 1)
    name = str(title or "")
    dim = str(subtitle or "")
    if dim:
        dim_width = max(max_title_width - cell_len(name) - 2, 1)
        if cell_len(dim) > dim_width:
            dim = truncate_display(dim, dim_width).plain
    name_width = max(max_title_width - (cell_len(dim) + 2 if dim else 0), 0)
    if cell_len(name) > name_width:
        name = truncate_display(name, name_width).plain if name_width else ""

    spans = [(name, Style(color=name_color, bold=True))]
    if dim:
        spans.extend((
            ("  ", Style(color=subtitle_color, bold=False)),
            (dim, Style(color=subtitle_color, bold=False)),
        ))
    title_text = Text.assemble(*spans)
    title_text.no_wrap = True
    title_text.overflow = "crop"
    panel_title = title_text if name or dim else None
    return Panel(body if body is not None else Text(""), title=panel_title,
                 title_align="left", border_style=border_color, box=box.ROUNDED,
                 padding=padding, expand=expand)


def footer_hint(text):
    return Text(text, style="#5a5a5a", no_wrap=True, overflow="ellipsis")


class TerminalKeyReader:
    def __init__(self):
        self.old_terminal = None

    def __enter__(self):
        if os.name != "nt":
            import termios
            import tty
            fd = sys.stdin.fileno()
            self.old_terminal = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        return self

    def __exit__(self, *_):
        if self.old_terminal is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_terminal)
            self.old_terminal = None

    def read_key(self):
        if os.name == "nt":
            import msvcrt
            while not msvcrt.kbhit():
                time.sleep(0.03)
            key = msvcrt.getwch()
            if key in ("\x00", "\xe0"):
                return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(msvcrt.getwch())
            return {"\r": "enter", "\n": "enter", "\x1b": "escape",
                    "\x03": "ctrl_c", "\x08": "backspace"}.get(key, key)
        import select
        fd = sys.stdin.fileno()
        ready, _, _ = select.select([fd], [], [], None)
        if not ready:
            return None
        first = os.read(fd, 1)
        if first == b"\x1b":
            sequence = bytearray(first)
            deadline = time.monotonic() + 0.04
            while time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], 0.005)
                if not ready:
                    continue
                sequence.extend(os.read(fd, 1))
                if sequence[-1:] in (b"A", b"B", b"C", b"D"):
                    return {b"A": "up", b"B": "down", b"C": "right", b"D": "left"}[sequence[-1:]]
            return "escape"
        return {b"\r": "enter", b"\n": "enter", b"\x03": "ctrl_c",
                b"\x7f": "backspace", b"\x08": "backspace"}.get(
                    first, first.decode("utf-8", "ignore"))

    def read_chunk(self):
        if os.name == "nt":
            import msvcrt
            while not msvcrt.kbhit():
                time.sleep(0.03)
            chars = []
            while msvcrt.kbhit():
                char = msvcrt.getwch()
                if char in ("\x00", "\xe0"):
                    if msvcrt.kbhit():
                        msvcrt.getwch()
                    continue
                chars.append(char)
            return "".join(chars)
        import select
        fd = sys.stdin.fileno()
        ready, _, _ = select.select([fd], [], [], None)
        return os.read(fd, 4096).decode("utf-8", "replace") if ready else ""


def _selector_item(item):
    if len(item) == 2:
        return item[0], item[1], "", "", True
    if len(item) == 3:
        return item[0], item[1], item[2], "", True
    if len(item) == 4:
        return item[0], item[1], item[2], item[3], True
    return item[:5]


def _selector_row(label, badge, badge_style, selected, width, label_width=None):
    from rich.cells import cell_len

    width = max(width, 1)
    prefix = "▌ " if selected else "  "
    badge = str(badge or "")
    badge_width = cell_len(badge)
    label_room = max(width - cell_len(prefix) - badge_width - (1 if badge else 0), 0)
    if label_width is not None:
        label_room = min(label_room, label_width)
    shown_label = truncate_display(label, label_room).plain
    gap = max(width - cell_len(prefix + shown_label) - badge_width, 0)
    line = Text()
    bg = " on #2a2417" if selected else ""
    line.append(prefix, style=f"#ff9f1a{bg}" if selected else None)
    line.append(shown_label, style=f"bold #ff9f1a{bg}" if selected else "#e6e6e6")
    line.append(" " * gap, style=bg.strip() if bg else None)
    if badge:
        style = badge_style or ("#8b8b8b" if selected else "#5a5a5a")
        line.append(badge, style=f"{style}{bg}")
    tail = width - cell_len(line.plain)
    if tail > 0:
        line.append(" " * tail, style=bg.strip() if bg else None)
    line.no_wrap = True
    line.overflow = "crop"
    return line


def select_menu(items, title=None, subtitle=None, border_color="#5a5a5a",
                footer="↑↓ move · enter select · esc back", on_adjust=None,
                extra_content=None, label_width=None, blank_before=False,
                on_enter=None, selected_badge_brackets=False,
                title_name_color="#ff9f1a", title_subtitle_color="#8b8b8b"):
    def get_items():
        return list(items() if callable(items) else items)

    current_items = get_items()
    selectable = [i for i, item in enumerate(current_items) if _selector_item(item)[4]]
    if not selectable:
        return None
    selected_index = selectable[0]
    width = max(console.size.width - (6 if title else 0), 1)

    def render():
        rows = [Text("")] if blank_before else []
        current_items[:] = get_items()
        for index, raw_item in enumerate(current_items):
            label, value, badge, badge_style, enabled = _selector_item(raw_item)
            if not enabled:
                rows.append(Text(f"  {label}", style="#5a5a5a", no_wrap=True, overflow="ellipsis"))
                continue
            if selected_badge_brackets and index == selected_index and badge:
                badge = f"‹ {badge} ›"
            rows.append(_selector_row(label, badge, badge_style, index == selected_index,
                                      width, label_width))
        if extra_content:
            extra = extra_content(_selector_item(current_items[selected_index])[1])
            if extra is not None:
                rows.append(extra)
        body = Group(*rows)
        if title:
            body = panel_with_title(title, subtitle, border_color, body,
                                    name_color=title_name_color,
                                    subtitle_color=title_subtitle_color)
        return Group(body, footer_hint(footer))

    try:
        with Live(get_renderable=render, console=console, refresh_per_second=10,
                  transient=True, vertical_overflow="crop"):
            with TerminalKeyReader() as reader:
                while True:
                    key = reader.read_key()
                    if key == "ctrl_c":
                        return None
                    if key in ("escape", "q", "Q"):
                        return None
                    selectable = [i for i, item in enumerate(current_items) if _selector_item(item)[4]]
                    if not selectable:
                        return None
                    if key in ("up", "down"):
                        position = selectable.index(selected_index)
                        step = -1 if key == "up" else 1
                        selected_index = selectable[(position + step) % len(selectable)]
                    elif key in ("left", "right") and on_adjust:
                        _, value, _, _, _ = _selector_item(current_items[selected_index])
                        on_adjust(value, key)
                    elif key == "enter":
                        value = _selector_item(current_items[selected_index])[1]
                        if on_enter and on_enter(value):
                            continue
                        return value
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


def save_named_links(links, preserve_original=False):
    path = DATA / "playlists.txt"
    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
    if preserve_original:
        existing = {_link_identity(url) or ("url", normalize_music_url(url))
                    for url in read_list(path)}
    else:
        existing = {normalize_music_url(url) for url in read_list(path)}
    new_links = []
    for url, name in links:
        if preserve_original:
            identity = _link_identity(url) or ("url", normalize_music_url(url))
            stored_url = url
        else:
            stored_url = normalize_music_url(url)
            identity = stored_url
        if identity not in existing:
            new_links.append((stored_url, name))
            existing.add(identity)
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
    suffix = "" if preserve_original else "."
    console.print(Text(f"Saved {len(new_links)} link(s) to {path}{suffix}", style="#3fb950"))
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


def _link_identity(url):
    parsed = urlparse(str(url).strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
        return ("video", video_id) if video_id else None
    if host == "youtube.com" or host.endswith(".youtube.com"):
        query = parse_qs(parsed.query)
        playlist_id = (query.get("list") or [None])[0]
        if playlist_id:
            return "playlist", playlist_id
        video_id = (query.get("v") or [None])[0]
        if video_id:
            return "video", video_id
    return None


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


def confirm_playlist_save(count):
    prompt = f"Add these {count} playlist link(s) to Playlists.txt? [Y/n]"
    console.print(Text(prompt, style="#e6e6e6"), end="", no_wrap=True)
    try:
        answer = input().strip().casefold()
    except KeyboardInterrupt:
        return None
    return answer in ("", "y", "yes")


def collect_links(cfg):
    state = {"rows": [], "buffer": "", "lock": threading.Lock()}
    known = {_link_identity(url) or ("url", normalize_music_url(url)) for url in all_urls()}
    pending = {}
    executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="paste-resolve")
    finished_input = False
    cancelled = False

    def resolve_link(url):
        ytdlp = get_ytdlp()
        options = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
                   "skip_download": True, "socket_timeout": 10, "retries": 2,
                   "logger": Quiet()}
        if cfg.get("cookies"):
            options["cookiesfrombrowser"] = (cfg["cookies"],)
        with ytdlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise ValueError("No link information found")
        entries = info.get("entries")
        if entries is not None:
            count = info.get("playlist_count") or info.get("n_entries")
            if count is None:
                count = sum(1 for entry in entries if entry)
            return str(info.get("title") or "Untitled playlist"), int(count or 0)
        return str(info.get("title") or "Untitled song"), 1

    def queue_line(raw):
        url = raw.strip()
        identity = _link_identity(url)
        row = {"url": url, "identity": identity, "status": "checking", "name": "", "count": 0}
        with state["lock"]:
            state["rows"].append(row)
        if identity is None:
            with state["lock"]:
                row["status"] = "invalid"
            return
        if identity in known:
            with state["lock"]:
                row["status"] = "skipped"
            return
        known.add(identity)
        future = executor.submit(resolve_link, url)
        pending[future] = row
        future.add_done_callback(lambda completed, item=row: publish_result(completed, item))

    def publish_result(future, row):
        try:
            title, count = future.result()
        except Exception:
            status, title, count = "invalid", "", 0
        else:
            status = "ready"
        with state["lock"]:
            row["name"] = title
            row["count"] = count
            row["status"] = status

    def update_finished():
        for future in list(pending):
            if not future.done():
                continue
            pending.pop(future)

    def render():
        from rich.cells import cell_len

        with state["lock"]:
            rows = [dict(row) for row in state["rows"]]
            buffer = state["buffer"]
        width = max(console.size.width - 8, 1)
        body = [Text("Paste one playlist or song link per line.", style="#8b8b8b", no_wrap=True),
                Text.assemble(("Press ", "#8b8b8b"), ("Enter", "#e6e6e6"),
                              (" on an empty line when you're done.", "#8b8b8b"),
                              no_wrap=True),
                Text("─" * width, style="#333333", no_wrap=True)]
        grid = Table.grid(padding=(0, 1), expand=True)
        grid.add_column(width=3, justify="right", no_wrap=True)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(no_wrap=True, justify="right")
        max_link = max(1, width - 34)
        for index, row in enumerate(rows, 1):
            link = re.sub(r"^https?://", "", row["url"], flags=re.I)
            shown_link = truncate_display(link, max_link)
            if row["status"] == "ready":
                status = Text(f"✓ {row['name']} · {row['count']} songs", style="#3fb950",
                              no_wrap=True, overflow="ellipsis")
            elif row["status"] == "checking":
                status = Text("checking…", style="#8b8b8b", no_wrap=True)
            elif row["status"] == "skipped":
                status = Text("↷ already in list, skipped", style="#8b8b8b", no_wrap=True)
            else:
                status = Text("✗ not a valid link", style="#f85149", no_wrap=True)
            grid.add_row(Text(str(index), style="#5a5a5a"), Text(shown_link.plain, style="#ff9f1a",
                                                                no_wrap=True), status)
        typing = Text(f"{len(rows) + 1} › ", style="#ff9f1a", no_wrap=True)
        typing.append(truncate_display(buffer, max(1, width - cell_len(typing.plain) - 1)).plain,
                      style="#e6e6e6")
        typing.append("▏", style="#ff9f1a")
        body.append(grid)
        body.append(typing)
        body.append(Text("─" * width, style="#333333", no_wrap=True))
        ready = sum(row["status"] == "ready" for row in rows)
        skipped = sum(row["status"] == "skipped" for row in rows)
        invalid = sum(row["status"] == "invalid" for row in rows)
        summary = Text(f"{ready} ready", style="#3fb950")
        summary.append(f" · {skipped} skipped", style="#8b8b8b")
        if invalid:
            summary.append(f" · {invalid} invalid", style="#f85149")
        body.append(summary)
        return Group(panel_with_title("Add Music", "Paste links", body=Group(*body)),
                     footer_hint("enter on empty line = done · esc cancel"))

    def process_chunk(chunk):
        nonlocal finished_input, cancelled
        if chunk in ("\x1b",):
            cancelled = True
            return
        if "\x03" in chunk:
            cancelled = True
            return
        chunk = re.sub(r"\x1b\[[A-D]", "", chunk)
        normalized = chunk.replace("\r\n", "\n").replace("\r", "\n")
        if normalized == "\n":
            with state["lock"]:
                current = state["buffer"]
                state["buffer"] = ""
            if current.strip():
                queue_line(current)
            else:
                finished_input = True
            return
        if "\n" in normalized:
            parts = normalized.split("\n")
            with state["lock"]:
                current = state["buffer"]
                state["buffer"] = ""
            for index, part in enumerate(parts[:-1]):
                current += part
                if not current.strip():
                    finished_input = True
                    break
                queue_line(current)
                current = ""
            with state["lock"]:
                state["buffer"] = parts[-1] if not finished_input else ""
            return
        with state["lock"]:
            for char in chunk:
                if char in ("\x08", "\x7f"):
                    state["buffer"] = state["buffer"][:-1]
                elif char >= " ":
                    state["buffer"] += char

    try:
        with Live(get_renderable=render, console=console, refresh_per_second=8,
                  transient=False, vertical_overflow="crop"):
            with TerminalKeyReader() as reader:
                while not finished_input and not cancelled:
                    update_finished()
                    try:
                        chunk = reader.read_chunk()
                    except KeyboardInterrupt:
                        cancelled = True
                        break
                    process_chunk(chunk)
                if cancelled:
                    for future in pending:
                        future.cancel()
                while pending:
                    update_finished()
                    time.sleep(0.04)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    if cancelled:
        return None
    update_finished()
    with state["lock"]:
        rows = [dict(row) for row in state["rows"]]
    return [(row["url"], row["name"]) for row in rows if row["status"] == "ready"]


def add_links(cfg):
    while True:
        console.clear()
        links_count = len(read_list(DATA / "playlists.txt"))
        options = [("Paste links", "paste", "playlists or songs"),
                   ("Load from Playlists.txt", "load", f"{links_count} links"),
                   ("Edit Playlists.txt", "edit", "open in editor"),
                   ("‹ Back", "back", "", "#5a5a5a")]
        try:
            choice = select_menu(options, title="Add Music",
                                 footer="↑↓ move · enter select · esc back")
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
                        save_named_links(playlist_links, preserve_original=True)
                        continue
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
    descriptions = {
        "format": "output file type. mp3 plays everywhere.",
        "quality": "audio quality. Higher bitrates use more storage.",
        "name_style": "filename pattern used for saved songs.",
        "cover": "embed cover art in each audio file.",
        "square": "crop embedded cover art to a square.",
        "clean": "remove promotional phrases from song titles.",
        "playlist_files": "create .m3u8 files for each playlist.",
        "prune": "remove songs no longer present in a playlist.",
        "skip_duplicate_songs": "avoid downloading songs already in the library.",
        "jobs": "maximum number of simultaneous downloads.",
        "rate_limit": "maximum download speed.",
        "retries": "retry count for failed downloads.",
        "cookies": "browser profile used for YouTube cookies.",
        "library": "music folder used for downloads and playlists.",
        "data": "folder containing settings, history, and saved links.",
        "edit": "open the saved playlist links in your editor.",
        "reset": "restore all settings except the library folder.",
        "back": "return to the main menu.",
    }

    def value_for(key):
        if key in ("library", "data"):
            return str(cfg["out"] if key == "library" else DATA), "#8b8b8b"
        if key == "rate_limit" and not cfg[key]:
            return "unlimited", "#8b8b8b"
        if key == "cookies" and not cfg[key]:
            return "none", "#8b8b8b"
        value = cfg.get(key, "")
        if isinstance(value, bool):
            return ("on", "#3fb950") if value else ("off", "#8b8b8b")
        if key == "name_style":
            value = STYLES.get(value, value)
        return str(value), "#4dd0e1"

    def get_rows():
        rows = []
        section_keys = {
            "AUDIO": ("format", "quality", "cover", "square"),
            "LIBRARY": ("name_style", "clean", "playlist_files", "prune",
                        "skip_duplicate_songs"),
            "DOWNLOADS": ("jobs", "rate_limit", "retries", "cookies"),
        }
        labels = dict(LABELS)
        for heading, keys in section_keys.items():
            rows.append((heading, None, "", "", False))
            for key in keys:
                value, style = value_for(key)
                rows.append((labels[key], key, value, style, True))
        rows.append(("FOLDERS", None, "", "", False))
        rows.extend([
            ("Library folder", "library",
             truncate_display(value_for("library")[0], max(console.size.width - 50, 12)).plain,
             "#8b8b8b", True),
            ("Data folder", "data",
             truncate_display(value_for("data")[0], max(console.size.width - 50, 12)).plain,
             "#8b8b8b", True),
            ("Edit Playlists.txt", "edit", "open in editor", "#8b8b8b", True),
            ("Reset to defaults", "reset", "", "", True),
            ("‹ Back", "back", "", "", True),
        ])
        return rows

    def adjust(key, direction):
        if key not in cfg:
            return
        step = -1 if direction == "left" else 1
        value = cfg[key]
        if isinstance(value, bool):
            cfg[key] = not value
        elif key in CHOICES:
            choices = CHOICES[key]
            current = choices.index(value) if value in choices else 0
            cfg[key] = choices[(current + step) % len(choices)]
        elif key == "retries":
            cfg[key] = max(1, int(value) + step)
        else:
            return
        save_cfg(cfg)

    def enter_setting(key):
        if key in cfg and (isinstance(cfg[key], bool) or key in CHOICES or key == "retries"):
            if isinstance(cfg[key], bool):
                cfg[key] = not cfg[key]
                save_cfg(cfg)
            return True
        return False

    while True:
        console.clear()

        def extra_content(selected):
            if selected not in descriptions:
                return None
            description_labels = dict(LABELS)
            description_labels.update({
                "library": "Library folder", "data": "Data folder",
                "edit": "Edit Playlists.txt", "reset": "Reset to defaults",
                "back": "‹ Back",
            })
            label = description_labels.get(selected, selected.title())
            text = Text(label, style="#e6e6e6")
            text.append(f" · {descriptions[selected]}", style="#8b8b8b")
            if selected in CHOICES:
                text.append(" Options: ", style="#8b8b8b")
                options = ([STYLES[item] for item in CHOICES[selected]]
                           if selected == "name_style"
                           else [str(item) for item in CHOICES[selected]])
                text.append(" · ".join(options), style="#4dd0e1")
            text.no_wrap = True
            text.overflow = "ellipsis"
            return Group(Text("─" * max(console.size.width - 8, 1), style="#333333"), text)

        try:
            choice = select_menu(
                get_rows, title="Settings", subtitle=f"Data: {DATA}",
                footer="↑↓ move · ←→ change value · enter edit · esc back",
                on_adjust=adjust, extra_content=extra_content, label_width=27,
                on_enter=enter_setting, selected_badge_brackets=True,
                title_subtitle_color="#6b6b6b")
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
            elif choice in ("rate_limit", "cookies"):
                value = questionary.text(dict(LABELS)[choice] + ":", default=str(cfg[choice])).ask()
                if value is not None:
                    cfg[choice] = value.strip()
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
    console.print(panel_with_title("Ready", border_color="#22c55e", body=t,
                                   padding=(0, 1), expand=False))
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
        except ScanCancelled:
            return "home"
        except Exception as e:
            menu_error(e)
            return "home"


def header(cfg, upd):
    from rich.cells import cell_len

    lib = get_lib(cfg["out"])
    width = max(console.size.width - 8, 1)
    last_run = lib.d["stats"]["last_run"]
    current_library = str(Path(cfg["out"]).expanduser().resolve())
    recent = LAST_SYNC_RESULT if last_run and LAST_SYNC_RESULT and \
        os.path.normcase(LAST_SYNC_RESULT["out"]) == os.path.normcase(current_library) else None
    details = [
        ("path", str(cfg["out"]), "#6b6b6b"),
        ("last_label", "Last sync", "#8b8b8b"),
        ("last_value", ago(last_run), "#e6e6e6"),
    ]
    if recent:
        details.append(("result", f"✓ {recent['new']} new", "#3fb950"))
        if recent["failed"]:
            details.append(("result", f"✗ {recent['failed']} failed", "#f85149"))
        details.append(("duration", f"{recent['elapsed']:.1f}s", "#8b8b8b"))

    def line_width(chunks):
        total = 0
        has_text = False
        for kind, text, _ in chunks:
            if not text:
                continue
            if has_text:
                total += cell_len(" " if kind == "last_value" else " · ")
            total += cell_len(text)
            has_text = True
        return total

    if line_width(details) > width:
        other_width = line_width(details[1:])
        details[0] = ("path", _middle_truncate(details[0][1], max(width - other_width - 3, 0)),
                      details[0][2])
    if line_width(details) > width and any(kind == "duration" for kind, _, _ in details):
        details = [chunk for chunk in details if chunk[0] != "duration"]
    if line_width(details) > width:
        details = [chunk for chunk in details if chunk[0] != "result"]
    line_one = Text()
    has_text = False
    for kind, value, style in details:
        if not value:
            continue
        if has_text:
            line_one.append(" " if kind == "last_value" else " · ", style="#8b8b8b")
        line_one.append(value, style=style)
        has_text = True
    if cell_len(line_one.plain) > width:
        line_one.truncate(width, overflow="crop")
    line_one.no_wrap = True
    line_one.overflow = "crop"

    line_two = Text()
    line_two.append(str(len(lib.d["songs"])), style="bold #4dd0e1")
    line_two.append(" songs · ", style="#e6e6e6")
    line_two.append(size(lib.total_size()), style="bold #4dd0e1")
    line_two.append(" · ", style="#8b8b8b")
    line_two.append(str(len(read_list(DATA / "playlists.txt"))), style="bold #4dd0e1")
    line_two.append(" playlists", style="#e6e6e6")
    line_two.no_wrap = True
    line_two.overflow = "ellipsis"
    body = [
        Text("YouTube music + Local, always in sync", style="#8b8b8b", no_wrap=True),
        Text("─" * width, style="#333333", no_wrap=True),
        line_one,
        line_two,
    ]
    panel = panel_with_title("♪ ytmusic-sync", f"v{VERSION}", "#f5a623",
                             Group(*body), name_color="#f5a623")
    console.print(panel)
    return lib


def _middle_truncate(value, width):
    from rich.cells import cell_len, get_character_cell_size

    value = str(value)
    if cell_len(value) <= width:
        return value
    if width <= 0:
        return ""
    if width == 1:
        return "…"
    left_limit = (width - 1) // 2
    right_limit = width - 1 - left_limit
    left, used = [], 0
    for char in value:
        size = get_character_cell_size(char)
        if used + size > left_limit:
            break
        left.append(char)
        used += size
    right, used = [], 0
    for char in reversed(value[len(left):]):
        size = get_character_cell_size(char)
        if used + size > right_limit:
            break
        right.append(char)
        used += size
    return "".join(left) + "…" + "".join(reversed(right))


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
        lib = header(cfg, upd)
        report_update_timing()
        if TIMING_ENABLED and "startup timing printed" not in TIMINGS:
            print_timings()
            TIMINGS["startup timing printed"] = 1
        link_count = len(read_list(DATA / "playlists.txt"))
        menu = [("Update library", "upd", f"scan {link_count} playlists"),
                ("Add Music", "add", f"{link_count} links · {len(lib.d['songs'])} songs"),
                ("Settings", "set",
                 f"{cfg['format']} · {cfg['quality']} · {cfg['jobs']} parallel"),
                ("Stats", "stats", "history · artists"),
                ("Quit", "q", "", "#5a5a5a")]
        if upd:
            menu.insert(4, (f"Update yt-dlp ({upd[0]} → {upd[1]})", "ytdlp",
                            "available", "#ff9f1a"))
        try:
            k = select_menu(menu, footer="↑↓ move · enter select · q quit",
                            blank_before=True)
        except Exception as e:
            menu_error(e)
            continue
        if k in (None, "q"):
            return
        try:
            if k == "set":
                cfg = settings(cfg)
            elif k == "stats":
                show_stats(cfg, wait_for_key=True)
            elif k == "ytdlp":
                upgrade_ytdlp()
                pause()
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
    g.add_argument("--demo", action="store_true", help="preview the live playlist scan dashboard")
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
    except ScanCancelled:
        if console is not None:
            console.print("\n[yellow]Scan cancelled.[/]")
        return 130
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
