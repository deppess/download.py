# download.py

Downloads, cleans, tags, and sorts music from YouTube into genre folders. Built around yt-dlp, Last.fm, and LRCLIB.

## Dependencies

```
yt-dlp  python-pylast  python-mutagen  python-pillow
```

## Setup

Edit the constants at the top of `download.py`:

```python
MUSIC_DIR        = Path("/path/to/your/music")
_BROWSER         = "firefox"
_BROWSER_PROFILE = "/path/to/profile"
```

Create `~/.config/genre-tagger.cfg` with your [Last.fm API key](https://www.last.fm/api/account/create):

```ini
[lastfm]
api_key = YOUR_KEY
```

## Usage

```
download <url>                       download + full pipeline
download --process <dir>             skip download, process existing files
download --process <dir> --dry-run   preview only

--skip-crop      skip cover art cropping
--skip-tag       skip Last.fm genre tagging
--skip-lyrics    skip LRCLIB lyrics fetch
--skip-sort      skip moving files into genre folders
--force          re-tag files that already have a genre
--verbose        show raw Last.fm tags and scores per file
```

## Pipeline

```
yt-dlp → 000-Landing/
  crop embedded cover art to 1:1
  strip playlist index prefix + "- Topic" from filenames
  Last.fm lookup → genre tag → write to file metadata
  LRCLIB → save .lrc alongside music
  move into playlists/{Genre}/
```

**Genre buckets:** Hip Hop · Rock & Metal · Soul · Electronic · Oldies · Misc

**Tag fallback chain** (stops at first hit):
track lookup → artist lookup → raw artist → search → channel name cleanup → title parsing → keyword scan → Misc

Files that fail every step get tagged `Misc` for manual review.

**Collections** — filenames matching certain keywords get routed to a fixed subfolder regardless of genre. Edit `COLLECTIONS` at the top of the script to add your own.

Ctrl+C finishes the current file then exits cleanly.
