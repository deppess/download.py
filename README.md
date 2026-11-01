# download.py

YouTube music sync pipeline for Linux and macOS. Downloads with `yt-dlp`, crops cover art, cleans filenames, tags genres with Last.fm, fetches LRCLIB lyrics, and finishes files for the local music library. Linux sorts files into genre folders; macOS imports finished files into Apple Music.

Source: [Codeberg](https://codeberg.org/deppes/download.py) (primary) · [GitHub](https://github.com/deppess/download.py) (mirror)

## Dependencies

Arch Linux:

```bash
sudo pacman -S yt-dlp ffmpeg python-pylast python-mutagen python-pillow libnotify
```

macOS:

```bash
# Homebrew or MacPorts is fine for yt-dlp/ffmpeg.
brew install yt-dlp ffmpeg
# or: sudo port install yt-dlp ffmpeg

# Python packages used by this script.
sudo port install py313-pylast py313-mutagen py313-pillow
```

macOS notifications use `osascript`. Linux notifications use `notify-send` when available.

## Install

```bash
install -m 755 download.py ~/.local/bin/download
```

Make sure `~/.local/bin` is in your `PATH`.

## Config

Main paths and playlist settings live at the top of `download.py`.

Linux defaults to:

```python
MUSIC_DIR = Path("/home/deppes/Media/music")
COOKIE_BROWSER = "chromium:Profile 1"
```

macOS defaults to:

```python
MUSIC_DIR = Path("/Users/deppes/Music")
LANDING_DIR = MUSIC_DIR / "download-staging"
APPLE_MUSIC_AUTO_ADD_DIR = MUSIC_DIR / "Music/Media.localized/Automatically Add to Music.localized"
COOKIE_BROWSER = "chromium:/Users/deppes/Library/Application Support/net.imput.helium/Default"
```

Last.fm genre tagging requires `~/.config/genre-tagger.cfg`:

```ini
[lastfm]
api_key = YOUR_LASTFM_API_KEY

[youtube]
# Optional; only needed if YouTube bot detection starts triggering.
po_token = VISITOR_DATA+TOKEN
```

For Helium cookies on macOS, run once:

```bash
download --setup-helium-cookies
```

When migrating an existing library, keep `.downloaded.txt` and `.failed.txt` in `MUSIC_DIR` so yt-dlp skips already synced videos.

## Commands

```bash
download playlist                  sync the hardcoded playlist: The Mix
download <url>                     download another YouTube URL
download --process <dir>           process existing `.m4a` files only
download --process <dir> --dry-run preview changes without writing files
```

Useful flags:

```bash
--skip-crop      skip embedded cover crop
--skip-tag       skip Last.fm genre tagging
--skip-lyrics    skip LRCLIB lyric fetch
--skip-sort      Linux: skip folder sorting; macOS: skip Apple Music import
--force          re-tag files that already have a valid genre
--verbose        show Last.fm tags and scoring
--no-cookies     run yt-dlp without browser cookies
--po-token       YouTube PO token; overrides config
```

Bare `download` intentionally fails instead of syncing the playlist by accident.

## Pipeline

```text
Linux:
yt-dlp -> playlists/000-Landing/
  crop embedded cover art to 1:1
  strip playlist index prefix + "- Topic" from filenames
  Last.fm lookup -> write genre metadata
  LRCLIB lookup -> save synced `.lrc` files
  move into playlists/{Genre}/

macOS:
yt-dlp -> download-staging/
  crop embedded cover art to 1:1
  strip playlist index prefix + "- Topic" from filenames
  Last.fm lookup -> write genre metadata
  LRCLIB lookup -> embed plain lyrics in `.m4a`
  move into Apple Music auto-import folder
```

Genre buckets: `Hip Hop`, `Rock & Metal`, `Soul`, `Electronic`, `Oldies`, `Misc`.

## macOS Apple Music

Finished macOS downloads are moved to Apple Music's auto-import folder:

```text
~/Music/Music/Media.localized/Automatically Add to Music.localized
```

Apple Music then imports and organizes the files under its managed media folder. Keep Music's Files setting “Copy files to Media folder when adding to library” enabled.

For a clean rebuild, use Apple's supported library chooser: quit Music, hold Option while reopening Music, then create or choose the library. Do not reset a Music library by manually moving its internal folders while Music still points at it.

## State Files

```text
.downloaded.txt  yt-dlp success archive
.failed.txt      permanent unavailable-video log
.download-log.*  latest Last.fm tagging log
```

`.failed.txt` IDs are also written into `.downloaded.txt` so yt-dlp skips permanently unavailable videos on future runs.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).

I ask that this code not be used to train AI models. This is a request, not a license term.
