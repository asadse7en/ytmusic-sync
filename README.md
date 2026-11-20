# ytmusic-sync

Download your YouTube playlists as clean, tagged music files, and keep matching `.m3u8` playlists in sync.
Run it again any time: only new songs download, and playlists update (additions, removals, order).

- Interactive menus, a live progress dashboard, and a stats summary
- Clean filenames (`Title.m4a`) with artist, title and cover art in the tags
- One flat folder with `.m3u8` playlists next to the songs, so VLC, Poweramp, Musicolet, foobar2000 and Jellyfin can use them
- Settings, links and history live in a hidden `.ytmusic-sync` folder inside your music folder

## Install

Needs Python 3.9+.

```
pipx install git+https://github.com/<you>/ytmusic-sync
```

Or from a clone:

```
git clone https://github.com/<you>/ytmusic-sync
cd ytmusic-sync
pipx install .        # or: pip install .
```

ffmpeg is needed for conversion and tags. If it is not installed, ytsync falls back to the copy bundled in the
`imageio-ffmpeg` package, so you normally do nothing. A system ffmpeg is used first when present
(`winget install ffmpeg`, `brew install ffmpeg`, `sudo apt install ffmpeg`).

No install: `pip install -r requirements.txt`, then `python ytmusic_sync.py`.

## Quick start

```
ytsync
```

First run: choose a music folder, then **Use defaults & start**, then paste playlist links (or load a text file).
Every run after that:

```
ytsync -u          # or open ytsync and choose "Update library"
```

## Menus

Every menu has **‹ Back**. Ctrl+C goes back, or exits from the home screen.

| Screen | What it does |
|---|---|
| Home (returning) | Update library, Add Music, Settings, Stats, Quit |
| Home (first run) | Use defaults & start, Customize, Quit |
| Add Music | Paste links one at a time, load a file, or save your links to a named links file |
| Ready | Review unique songs and playlist counts (Start, Edit settings, Back, Exit) |
| Settings | All options below, saved automatically |
| Stats | Songs, size, all-time downloads, runs, last sync, songs per playlist |

**Update library** uses your saved settings and links and asks nothing.

## Commands

| Command | Does |
|---|---|
| `ytsync` | Interactive menu |
| `ytsync -u` | Update library with saved links and settings |
| `ytsync <url> [<url>...]` | Download links (this run only) |
| `ytsync <url> --save` | Download and remember the links for `-u` |
| `ytsync -p links.txt` | Use a text file of URLs (one per line, `#` comments allowed) |
| `ytsync -d` | Dry run: show what would download |
| `ytsync -s` | Library stats |
| `ytsync --update-ytdlp` | Upgrade yt-dlp |

Options (flags apply to that run only; change defaults in Settings):

| Flag | Meaning |
|---|---|
| `-o, --out DIR` | Music library folder |
| `-f, --format` | `m4a` (default), `mp3`, `opus`, `flac`, `wav`, `aac` |
| `-q, --quality` | `best` (default), `320`, `256`, `192`, `128` (kbps, lossy formats) |
| `-n, --name-style` | `title` (default), `artist-title`, `title-artist` |
| `--no-cover` | Skip cover art |
| `--no-playlist-files` | Do not write `.m3u8` files |
| `--prune` | With `-u`: delete songs no longer in any playlist (asks first) |
| `--cookies-from-browser NAME` | `chrome`, `firefox`, `edge`... for private playlists |
| `--rate-limit RATE` | For example `2M` |
| `--retries N` | Retries per download (default 5) |
| `--jobs N` | Parallel downloads, from 1 to 6 (default 3; saved in Settings) |
| `--compact` | One line per song instead of the dashboard (good for logs) |

## Settings

Format, Quality, Filename style, Cover art, Square cover, Clean titles, Playlist .m3u8 files, Prune removed songs,
Skip duplicate songs, Parallel downloads, Speed limit, Retries, Browser cookies, Edit playlists.txt, Saved links,
Reset to defaults. Duplicate video IDs are downloaded once; matching titles with durations within three seconds
are recorded as aliases and their playlist entries use the kept song file. Links saved to a file include a name
comment above each URL.

## Where files live

```
music/
├── Song.m4a
├── Gym.m3u8
└── .ytmusic-sync/
    ├── config.json      settings and saved links
    ├── playlists.txt    optional links file (edit it, then run Update)
    ├── library.json     video ID -> file map, aliases, playlists, stats
    ├── failed.txt       songs that failed last run
    ├── sync.log
    └── cache.json       yt-dlp update check
```

- Run `ytsync` inside a library (or any subfolder) to use it. Anywhere else it opens the last library you used.
  Use `-o FOLDER` to pick another one. Each library has its own settings.
- The only file outside the music folder is a small pointer to your last library, in your OS config folder.
- If you copy the music folder to another PC or drive, sync keeps working.
- If you sync the folder to a phone (Syncthing and similar), ignore `.ytmusic-sync`.
- If `library.json` is lost, it is rebuilt from the source URL stored in each file's tags.

## Filenames and tags

- Filenames default to `Title.m4a`. If two songs share a title, the second becomes `Title - Artist.m4a`.
- Titles are cleaned: "(Official Video)", "[HD]", "(Lyrics)", emoji, and "- Topic" on channel names are removed.
- Artist, title and album come from YouTube Music metadata when available; otherwise `Artist - Title` is parsed from the video title, then the channel name.
- Cover art is cropped square and embedded. Tags are a best guess, so a few odd titles may need manual fixing.

## Players

- **Laptop/Android:** VLC, foobar2000, MusicBee, Poweramp, Musicolet and similar read `.m3u8` files from the folder.
- **iPhone:** Apple Music and Files cannot import `.m3u8`. Use VLC, foobar2000 mobile or Doppler, or stream through the Jellyfin app.
- **Jellyfin:** add the folder as a Music library and enable "Prefer embedded metadata". Native `.m3u8` pickup is limited; if playlists do not appear, use a playlist-import plugin. If your scanner lists the hidden `.ytmusic-sync` folder, add it to its ignore list.

## Keeping yt-dlp current

YouTube breaks old yt-dlp versions often. ytsync checks PyPI at most once a day and shows a warning plus an
**Update yt-dlp** menu item when a newer version exists. If several downloads fail with errors like HTTP 403 or
"Sign in", it offers to update. Manual: `ytsync --update-ytdlp` or `pip install -U yt-dlp`.

## Troubleshooting

- **Many downloads fail:** update yt-dlp (above).
- **Private playlist or age-restricted songs:** `--cookies-from-browser chrome` (or your browser).
- **Playlist missing songs:** private/deleted videos are skipped and counted in the summary.
- **Ctrl+C:** progress is saved, playlists are written, and the next Update continues.
- **ffmpeg errors:** install a system ffmpeg (see Install).
- **Windows symbols look broken:** use Windows Terminal.

## Notes

- Download only content you have the right to download. Check YouTube's terms and your local laws.
- Switching format later does not re-download songs, since they are matched by video ID. Use a new library folder for a second copy in another format.
- `--prune` only runs with `-u` (or from the menu), because a one-off link would otherwise make every other song look "removed".

## License

MIT
