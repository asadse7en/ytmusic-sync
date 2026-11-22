# ytmusic-sync

Download your YouTube playlists as clean, tagged music files and keep matching `.m3u8` playlists in sync.
Run it again any time: only new songs download, and playlists update (additions, removals, order).

- Interactive arrow-key menus, a live progress dashboard and a summary
- Clean filenames (`Title.mp3` by default) with artist, title and cover art in the tags
- One flat folder with `.m3u8` playlists beside the songs (VLC, Poweramp, Musicolet, foobar2000, Jellyfin)
- Each song is downloaded once, even if it appears in several playlists or under several video IDs
- Settings, links and history live in the hidden `~/.ytmusic-sync` folder

## Install

Needs Python 3.9+.

```
pipx install .            # from a clone; or: pip install .
```

No install: `pip install -r requirements.txt`, then `python -m ytmusic_sync`.

ffmpeg is used for conversion and tags. A system ffmpeg is used when present; otherwise the copy bundled in
`imageio-ffmpeg` is used, so you normally do nothing.

## Quick start

```
ytmusic-sync
```

First run: pick a music folder, choose **Use defaults & start**, paste playlist links. Every run after that:

```
ytmusic-sync -u        # or open ytmusic-sync and choose "Update library"
```

## Menus

Every menu has **‹ Back**. Ctrl+C goes back, or quits from the home screen.

| Screen | What it does |
| --- | --- |
| Welcome (first run) | Use defaults & start, Customize, Quit |
| Home | Update library, Add Music, Settings, Stats, (Update yt-dlp), Quit |
| Add Music | Paste links, Load from Playlists.txt, Edit Playlists.txt |
| Ready | Shows new/known song counts, then Start (or Save playlist links & start) |
| Settings | All options below, saved automatically |

**Update library** uses your saved links and settings and asks nothing.
In **Paste links**, only valid YouTube links are counted. Playlist links can be saved to `playlists.txt`
(with the playlist name as a comment above each link); single-song links are downloaded for that run only.

## Commands

| Command | Does |
| --- | --- |
| `ytmusic-sync` | Interactive menu |
| `ytmusic-sync -u` | Update library with saved links and settings |
| `ytmusic-sync <url> [<url>...]` | Download links (this run only) |
| `ytmusic-sync <url> --save` | Download and remember the links for `-u` |
| `ytmusic-sync -p links.txt` | Use a text file of links (one per line, `#` comments allowed) |
| `ytmusic-sync -d` | Dry run: show what would download |
| `ytmusic-sync -s` | Library stats |
| `ytmusic-sync --where` | Print the data folder and library folder |
| `ytmusic-sync --update-ytdlp` | Upgrade yt-dlp |

| Flag | Meaning |
| --- | --- |
| `-o, --out DIR` | Music library folder (remembered) |
| `-f, --format` | `mp3` (default), `m4a`, `opus`, `flac`, `wav`, `aac` |
| `-q, --quality` | `best` (default), `320`, `256`, `192`, `128` (kbps, lossy formats) |
| `-n, --name-style` | `title` (default), `artist-title`, `title-artist` |
| `--no-cover` | Skip cover art |
| `--no-playlist-files` | Do not write `.m3u8` files |
| `--prune` | With `-u`: delete songs no longer in any playlist (asks first) |
| `--cookies-from-browser NAME` | `chrome`, `firefox`, `edge`... for private playlists |
| `--rate-limit RATE` | For example `2M` |
| `--retries N` | Retries per download (default 5) |
| `--jobs N` | Parallel downloads, 1 to 6 (default 3) |
| `--compact` | One line per song instead of the dashboard (good for logs) |

Output flags apply to that run only; change defaults in Settings.

## Where files live

```
music/
├── Song.mp3
└── Gym.m3u8
```

All app data is in one hidden folder in your home directory (`C:\Users\<you>\.ytmusic-sync` on Windows):
`config.json`, `playlists.txt`, `failed.txt`, `sync.log`, `cache.json` and `libs/<hash>.json` (one song database
per music folder). Set `YTMUSIC_SYNC_HOME` to use a different folder.

Moving to a new PC: copy the music folder, run ytmusic-sync, pick that folder. The song database is rebuilt from
the file tags. Copy `playlists.txt` too to keep your links.

## Filenames and tags

- Filenames default to `Title.mp3`. If a name is taken, the song becomes `Title - Artist.mp3`, then `Title (2).mp3`.
- Titles are cleaned: "(Official Video)", "[HD]", "(Lyrics)", emoji and "- Topic" on channel names are removed.
- Artist, title and album come from YouTube Music metadata when available, otherwise `Artist - Title` is parsed
  from the video title, then the channel name. Tags are a best guess; a few odd titles may need manual fixing.
- Cover art is cropped square and embedded.
- Two video IDs with the same cleaned title and a length within 3 seconds count as the same song.

## Players

- **Android / laptop:** VLC, foobar2000, MusicBee, Poweramp, Musicolet read `.m3u8` files from the folder.
- **iPhone:** Apple Music and Files cannot import `.m3u8`. Use VLC, foobar2000 mobile or Doppler.
- **Jellyfin:** add the folder as a Music library and enable "Prefer embedded metadata". Native `.m3u8` pickup
  is limited; if playlists do not appear, use a playlist-import plugin.

## Troubleshooting

- **Many downloads fail** (403, "Sign in"): update yt-dlp (`ytmusic-sync --update-ytdlp`). The version check
  runs at most once a day in the background and never slows startup.
- **Private or age-restricted songs:** `--cookies-from-browser chrome`.
- **Ctrl+C:** running conversions finish, progress is saved, playlists are written. Press Ctrl+C twice to quit at once.
- **ffmpeg errors:** install a system ffmpeg (`winget install ffmpeg`, `brew install ffmpeg`, `sudo apt install ffmpeg`).
- **Windows symbols look broken:** use Windows Terminal.
- Logs: `~/.ytmusic-sync/sync.log`. Failed songs: `~/.ytmusic-sync/failed.txt`.

## Development

```
python -m unittest discover -s tests
```

The tests replace yt-dlp with a fake, so they run offline.

## Notes

Download only content you have the right to download. Check YouTube's terms and your local laws.
Changing the format later does not re-download songs (they are matched by video ID).

## License

MIT
