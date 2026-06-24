# download.py

YouTube music sync pipeline for Linux — downloads with yt-dlp, crops cover art, cleans filenames, tags genres with Last.fm, fetches synced LRCLIB lyrics, and sorts `.m4a` files into genre folders.

## Dependencies

```bash
yt-dlp  ffmpeg  python-pylast  python-mutagen  python-pillow
libnotify   # desktop error notifications via notify-send / mako
```

Arch:

```bash
sudo pacman -S yt-dlp ffmpeg python-pylast python-mutagen python-pillow libnotify
```

## Install

```bash
chmod +x download.py
cp download.py ~/.local/bin/download
```

Make sure `~/.local/bin` is in your `PATH`.

## Config

Main paths and playlist settings live at the top of `download.py`:

```python
MUSIC_DIR     = Path("/home/deppes/Media/music")
LANDING_DIR   = MUSIC_DIR / "playlists" / "000-Landing"
PLAYLISTS_DIR = MUSIC_DIR / "playlists"
LYRICS_DIR    = MUSIC_DIR / "lyrics"
ARCHIVE_FILE  = MUSIC_DIR / ".downloaded.txt"
FAILED_FILE   = MUSIC_DIR / ".failed.txt"

DEFAULT_PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLHxdRPbjKyHsTayiqW6F492cCqI02NbCR"
COOKIE_BROWSER = "chromium:Profile 1"
```

Last.fm genre tagging requires `~/.config/genre-tagger.cfg`:

```ini
[lastfm]
api_key = YOUR_LASTFM_API_KEY
```

## Commands

```bash
download playlist                  sync the hardcoded playlist: The Mix
download <url>                     download another YouTube URL
download --process <dir>           process existing `.m4a` files only
download --process <dir> --dry-run preview changes without writing files
```

```bash
--skip-crop      skip embedded cover crop
--skip-tag       skip Last.fm genre tagging
--skip-lyrics    skip LRCLIB lyric fetch
--skip-sort      skip moving files into folders
--force          re-tag files that already have a valid genre
--verbose        show Last.fm tags and scoring
--no-cookies     run yt-dlp without Chromium cookies
```

Bare `download` intentionally fails instead of syncing the playlist by accident.

## Pipeline

```text
yt-dlp → playlists/000-Landing/
  crop embedded cover art to 1:1
  strip playlist index prefix + "- Topic" from filenames
  Last.fm lookup → write genre metadata
  LRCLIB lookup → save synced `.lrc` files
  move into playlists/{Genre}/
```

Genre buckets:

```text
Hip Hop · Rock & Metal · Soul · Electronic · Oldies · Misc
```

Collection overrides route matching filenames into fixed soundtrack folders, such as Initial D, Jet Set Radio, and Bomb Rush Cyberfunk. Edit `COLLECTIONS` in the script to add more.

## State Files

```text
.downloaded.txt  yt-dlp success archive; managed by yt-dlp
.failed.txt      permanent unavailable-video skip list; managed by download.py
```

`.failed.txt` is updated live when yt-dlp reports a permanent unavailable YouTube ID. Future runs skip those IDs by default while leaving `.downloaded.txt` untouched.

## YouTube Handling

`download playlist` runs a quick preflight check before the full sync. It uses Chromium cookies from `COOKIE_BROWSER`, keeps yt-dlp's normal archive behavior, streams yt-dlp output live, suppresses failed-ID filter noise, and sends a desktop notification only for full-stop failures such as cookie/auth/rate-limit problems.

## License

MIT
