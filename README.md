# download.py

YouTube music pipeline for Linux and macOS. On macOS, one command downloads
new tracks, sorts them into `~/Music/Library`, and adds them to Music without
moving files into Music's managed-media folder.

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

## Install / update

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

The yt-dlp archive and unavailable-ID list are retained at
`~/Music/Library/.pipeline/.downloaded.txt` and `.failed.txt`.

## Commands

```bash
download add <url>                 download, sort, and add new tracks to Music
download add playlist              do the same for the configured playlist
download fetch playlist            fetch the hardcoded playlist into incoming staging
download fetch <url>               fetch another YouTube URL into incoming staging
download intake                    verify and preserve new raw downloads
download --process <dir> --allow-legacy-mutation
                                   deprecated in-place processor
```

For normal use, `download add` is the only command needed. It keeps strong
matches in the normal genre folders and puts uncertain tracks in
`~/Music/Library/Review`, then adds all newly published files to Music.
The remaining commands below are advanced troubleshooting/review controls.

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

### Advanced workflow

These lower-level commands are optional troubleshooting controls. For normal
use, run `download add <url>` or `download add playlist` instead.

```bash
download fetch <url>                         # writes only incoming staging
download intake                              # checksum-verified raw preservation
export LASTFM_API_KEY='…'
download enrich                              # evidence and conservative classification
download review                              # writes Library/.pipeline/reports/raw-review.csv
# after reviewing a CSV row, explicitly choose its destination genre:
download review approve --id <id> --genre Pop --confirm-review
download plan create                         # writes an immutable pending JSON plan
# inspect the plan; review items are excluded from it
download plan approve --plan <plan.json> --confirm-approve
download plan publish --plan <plan.json> --confirm-publish
download playlists                           # atomically refreshes five genre M3U8 files
download import-bundle --plan <plan.json>    # verified bundle for manual Music import
download music import --plan <plan.json> --confirm-music-import
                                             # add published tracks directly to Music
```

Plans reject changed raw sources, duplicate checksums, stale approvals, unsafe
paths, and existing destination files. Publication copies from preserved raw
media only, verifies checksums and readability, and never removes or
overwrites a file. The import bundle does not launch Apple Music or edit its
database; import it manually with Music configured not to copy media files.

`download music import` uses macOS Music's AppleScript `add` command rather
than modifying Music's database. It imports only checksum-verified files from
a plan already published to `~/Music/Library`, records Music's returned
persistent ID, and stops if Music reports a location different from the source
file. It never deletes or retries an uncertain import automatically.

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

# Writes timestamped JSONL plan + CSV review queue under Library/.pipeline.
download library rebuild plan
```

After reviewing a complete plan, copy the entire planned set (including
unresolved files in `Review`) from the verified backup with an explicit
acknowledgement:

```bash
download library rebuild stage \
  --plan ~/Music/Library/.pipeline/rebuild-reports/TIMESTAMP/copy-plan.jsonl \
  --confirm-stage-copy
```

This requires an empty destination, never overwrites a file, atomically copies
every item with SHA-256 verification, and then reconciles a staged-library
manifest. It never edits or removes backup media.

If an interrupted or older staging run has already copied every file, verify
and reconcile it without copying anything again:

```bash
download library rebuild reconcile \
  --plan ~/Music/Library/.pipeline/rebuild-reports/TIMESTAMP/copy-plan.jsonl
```

After reconciliation, create portable genre playlists for the tracks with
high-confidence classifications. Import these M3U8 files into the new Apple
Music library after importing the corresponding staged tracks:

```bash
download library rebuild playlists \
  --plan ~/Music/Library/.pipeline/rebuild-reports/TIMESTAMP/copy-plan.jsonl
```

To rename the verified staged folder before importing, use the guarded
relocation command. It refuses to overwrite a destination and updates the
generated playlist paths without changing any media file.

```bash
download library rebuild relocate \
  --source ~/Music/Sorted-Library --destination ~/Music/Library
```

Bare `download` intentionally fails instead of syncing the playlist by accident.

## Legacy compatibility

```text
The original direct-processing flags remain available behind
`--allow-legacy-mutation`. They are not part of the normal workflow.
```

New tracks use `Electronic`, `Hip Hop`, `Pop`, `Rock & Metal`, `Soul`, or
`Review` when evidence is uncertain.

## macOS Apple Music

`download add` imports newly published verified files from `~/Music/Library`
using Music's AppleScript interface. Turn off Music's “Copy files to Media
folder when adding to library” setting to preserve the verified physical layout.

## State Files

```text
~/Music/Library/.pipeline/.downloaded.txt   yt-dlp success archive
~/Music/Library/.pipeline/.failed.txt       permanent unavailable-video log
~/Music/Library/.pipeline/.download-log.*   legacy tagging log
~/Music/Library/.pipeline/pipeline.sqlite3  active pipeline state
~/Music/Library/.sorted-library-manifest.jsonl  verified library manifest
```

`.failed.txt` IDs are also written into `.downloaded.txt` so yt-dlp skips permanently unavailable videos on future runs.

## Build and test

Requires Python 3.11 or later and the packages under Dependencies, plus pytest.

```sh
python3 -m pytest -q
```

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).

I ask that this code not be used to train AI models. This is a request, not a license term.
