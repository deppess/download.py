# download.py

YouTube music pipeline for Linux and macOS. Downloads land in a safe staging
directory, can be cleaned and enriched, and then enter a separate
review-first, checksum-verified library-placement workflow. It never moves
downloads directly into Apple Music's managed-media folder.

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

The pipeline now also supports an optional TOML configuration at:

```text
~/.config/download.py/config.toml
```

Start from [`config.example.toml`](config.example.toml). `DOWNLOAD_MUSIC_ROOT`
overrides the configured root, which is useful for testing. Before any fetch or
library operation, run the read-only environment check:

```bash
download doctor
```

Linux defaults to:

```python
MUSIC_DIR = Path("/home/deppes/Media/music")
COOKIE_BROWSER = "chromium:Profile 1"
```

macOS defaults to:

```python
MUSIC_DIR = Path.home() / "Music"
LANDING_DIR = MUSIC_DIR / "download-staging"
LIBRARY_DIR = MUSIC_DIR / "Library"
COOKIE_BROWSER = f"chromium:{Path.home()}/Library/Application Support/net.imput.helium/Default"
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
download fetch playlist            fetch the hardcoded playlist into incoming staging
download fetch <url>               fetch another YouTube URL into incoming staging
download intake                    verify and preserve new raw downloads
download --process <dir> --allow-legacy-mutation
                                   deprecated in-place processor
```

`download playlist` and `download <url>` remain temporary compatibility aliases
for the `fetch` command. Fetching intentionally stops after download; run
`download intake` before any enrichment or library placement.

### Evidence providers

The new provider layer caches Last.fm and LRCLIB responses in the pipeline
database before any classification. It never writes audio tags. Set Last.fm's
key as `LASTFM_API_KEY` in the environment (rather than committing it to a
configuration file). Successful responses have explicit TTLs; transient API,
network, and rate-limit failures receive retry windows; LRCLIB misses are
cached as bounded negative evidence. Missing or contradictory evidence remains
in review rather than selecting a genre.

### Future-download workflow

The normal pipeline deliberately requires review and explicit confirmation:

```bash
download fetch <url>                         # writes only incoming staging
download intake                              # checksum-verified raw preservation
export LASTFM_API_KEY='…'
download enrich                              # evidence and conservative classification
download review                              # writes .download-state/reports/raw-review.csv
# after reviewing a CSV row, explicitly choose its destination genre:
download review approve --id <id> --genre Pop --confirm-review
download plan create                         # writes an immutable pending JSON plan
# inspect the plan; review items are excluded from it
download plan approve --plan <plan.json> --confirm-approve
download plan publish --plan <plan.json> --confirm-publish
download playlists                           # atomically refreshes five genre M3U8 files
download import-bundle --plan <plan.json>    # verified bundle for manual Music import
```

Plans reject changed raw sources, duplicate checksums, stale approvals, unsafe
paths, and existing destination files. Publication copies from preserved raw
media only, verifies checksums and readability, and never removes or
overwrites a file. The import bundle does not launch Apple Music or edit its
database; import it manually with Music configured not to copy media files.

Useful flags:

```bash
--skip-crop      skip embedded cover crop
--skip-tag       skip Last.fm genre tagging
--skip-lyrics    skip LRCLIB lyric fetch
--skip-sort      Linux: skip folder sorting; macOS: skip Apple Music import
--force          re-tag files that already have a genre
--write-low-confidence-tags  legacy override; normally uncertain genres are review-only
--verbose        show Last.fm tags and scoring
--no-cookies     run yt-dlp without browser cookies
--po-token       YouTube PO token; overrides config
```

## Safe library rebuild preparation

The existing Apple Music media folder is never a target for direct sorting or
metadata rewrites. The new `library` commands are opt-in safety checks for a
future rebuild and never delete, move, overwrite, or change source music.

```bash
# Read-only audit of the current managed-media files.
download library audit

# Create ~/Music/Library-Backup only after quitting Music. Every media file and
# the Music library database are copied and SHA-256 verified. The command
# refuses to overwrite an existing backup.
download library backup

# Independently verify an existing backup manifest.
download library verify \
  --root ~/Music/Library-Backup \
  --manifest ~/Music/Library-Backup/library-manifest.jsonl

# Verify and register the finished library in the pipeline's SQLite state.
# This is read-only for media and validates every file before recording it.
download library adopt
```

Do not delete or move the old Apple Music library. It remains the rollback
source until the future rebuilt library has passed its complete reconciliation.

### Rebuild evidence and dry-run plan

These commands still do **not** copy, tag, move, or delete a media file. They
cache raw Last.fm responses in a private SQLite database, then write a
review-first proposed `Genre/Artist/Album` layout. Albums are included only
when that artist/album group has five or more tracks. Weak identities, weak or
ambiguous genre evidence, and every filename collision remain in `Review`.

```bash
# Requires the existing [lastfm] api_key. This is network-only and resumable.
download library rebuild evidence

# Writes timestamped JSONL plan + CSV review queue under ~/.download-state.
download library rebuild plan
```

After reviewing a complete plan, copy the entire planned set (including
unresolved files in `Review`) from the verified backup with an explicit
acknowledgement:

```bash
download library rebuild stage \
  --plan ~/.download-state/rebuild-reports/TIMESTAMP/copy-plan.jsonl \
  --confirm-stage-copy
```

This requires an empty destination, never overwrites a file, atomically copies
every item with SHA-256 verification, and then reconciles a staged-library
manifest. It never edits or removes backup media.

If an interrupted or older staging run has already copied every file, verify
and reconcile it without copying anything again:

```bash
download library rebuild reconcile \
  --plan ~/.download-state/rebuild-reports/TIMESTAMP/copy-plan.jsonl
```

After reconciliation, create portable genre playlists for the tracks with
high-confidence classifications. Import these M3U8 files into the new Apple
Music library after importing the corresponding staged tracks:

```bash
download library rebuild playlists \
  --plan ~/.download-state/rebuild-reports/TIMESTAMP/copy-plan.jsonl
```

To rename the verified staged folder before importing, use the guarded
relocation command. It refuses to overwrite a destination and updates the
generated playlist paths without changing any media file.

```bash
download library rebuild relocate \
  --source ~/Music/Sorted-Library --destination ~/Music/Library
```

Bare `download` intentionally fails instead of syncing the playlist by accident.

## Pipeline

```text
All platforms:
yt-dlp -> download-staging/
  crop embedded cover art to 1:1
  strip playlist index prefix + "- Topic" from filenames
  Last.fm lookup -> write only high-confidence genre metadata
  LRCLIB lookup -> save synced `.lrc` files
  keep files in staging for a reviewed and verified library plan
```

Genre buckets: `Hip Hop`, `Rock & Metal`, `Soul`, `Electronic`, `Oldies`, `Misc`.

## macOS Apple Music

Import only verified files from `~/Music/Library` using Music's **File → Add
to Library**. Turn off Music's “Copy files to Media folder when adding to
library” setting to preserve the verified physical layout. Apple Music import
is intentionally never an automatic side effect of downloading.

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
