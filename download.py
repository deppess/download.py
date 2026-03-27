#!/usr/bin/env python3
"""
download.py — Download, clean, tag, and sort music files.

Pipeline (all steps run by default):
  1. Download    yt-dlp → landing dir (requires URL)
  2. Crop        Center-crop embedded cover art to 1:1
  3. Clean       Strip 4-digit index and '- Topic' from filenames
  4. Tag         Last.fm genre lookup → genre buckets
  5. Lyrics      Fetch synced lyrics from LRCLIB
  6. Sort        Move files into genre/collection folders

Usage:
    download <url>                       Full pipeline
    download --process <dir>             Process existing files (no download)
    download --process <dir> --dry-run   Preview changes

    --skip-crop    Skip cover art cropping
    --skip-tag     Skip genre tagging
    --skip-lyrics  Skip lyrics fetching
    --skip-sort    Skip sorting into folders
    --dry-run      Preview all changes without modifying files
    --verbose      Show Last.fm tag details per file
    --force        Re-tag files that already have a valid genre
"""

import sys
import json
import time
import re
import signal
import subprocess
import argparse
import configparser
import urllib.request
import urllib.parse
from io import BytesIO
from pathlib import Path
from datetime import datetime
from collections import defaultdict

try:
    import pylast
except ImportError:
    print("Error: python-pylast not installed. Run: sudo pacman -S python-pylast")
    sys.exit(1)

try:
    import mutagen.mp4
    from mutagen.mp4 import MP4Cover
except ImportError:
    print("Error: python-mutagen not installed. Run: sudo pacman -S python-mutagen")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    print("Error: python-pillow not installed. Run: sudo pacman -S python-pillow")
    sys.exit(1)

# ── Config ───────────────────────────────────────────────────────────────

MUSIC_DIR     = Path("/path/to/your/music")
LANDING_DIR   = MUSIC_DIR / "playlists" / "000-Landing"
PLAYLISTS_DIR = MUSIC_DIR / "playlists"
LYRICS_DIR    = MUSIC_DIR / "lyrics"
ARCHIVE_FILE  = MUSIC_DIR / ".downloaded.txt"
CONFIG_FILE   = Path.home() / ".config" / "genre-tagger.cfg"
LOG_DIR       = MUSIC_DIR

_BROWSER         = "firefox"           # browser to pull cookies from
_BROWSER_PROFILE = "/path/to/profile"  # browser profile dir (or "" to use default)

# ── Collections (keyword → sort folder override) ──────────────────────────

COLLECTIONS = {
    "initial d":           {"folder": "Soundtracks/Initial D",           "genre": "Electronic"},
    "initiald":            {"folder": "Soundtracks/Initial D",           "genre": "Electronic"},
    "jet set radio":       {"folder": "Soundtracks/Jet Set Radio",       "genre": "Electronic"},
    "bomb rush cyberfunk": {"folder": "Soundtracks/Bomb Rush Cyberfunk", "genre": "Electronic"},
}

# ── Keyword genre fallback (last resort when all API lookups fail) ─────────

KEYWORD_GENRES = {
    "Electronic": ["eurobeat", "super eurobeat", "para para", "parapara",
                   "d & b", "d&b", "dnb", "drum and bass", "drum & bass"],
    "Hip Hop":    ["type beat"],
}

# ── Genre buckets ─────────────────────────────────────────────────────────

GENRE_TAGS = {
    "Hip Hop": {
        "hip-hop", "hip hop", "rap", "hiphop", "hip hop/rap",
        "gangsta rap", "underground hip-hop", "underground hip hop",
        "east coast hip-hop", "east coast hip hop",
        "west coast hip-hop", "west coast hip hop",
        "southern hip hop", "southern hip-hop",
        "midwest hip hop", "midwest hip-hop",
        "old school hip-hop", "old school hip hop",
        "golden age hip-hop", "golden age hip hop",
        "boom bap", "boombap",
        "conscious hip-hop", "conscious hip hop", "conscious rap",
        "alternative hip-hop", "alternative hip hop",
        "jazz rap", "jazz hip-hop", "jazz hip hop",
        "abstract hip-hop", "abstract hip hop",
        "turntablism", "turntablist",
        "hardcore hip-hop", "hardcore hip hop", "hardcore rap",
        "dirty south", "crunk",
        "trap", "trap music", "trap rap",
        "cloud rap", "soundcloud rap",
        "phonk", "drift phonk", "memphis phonk", "brazilian phonk",
        "memphis rap",
        "drill", "uk drill", "chicago drill",
        "grime", "grime music",
        "g-funk",
        "horrorcore",
        "nerdcore", "nerdcore hip-hop",
        "chopped and screwed", "chopped & screwed",
        "instrumental hip-hop", "instrumental hip hop",
        "lofi hip hop", "lo-fi hip hop",
        "plugg", "pluggnb",
        "uk hip-hop", "uk hip hop", "british hip-hop", "british hip hop",
        "french hip-hop", "french hip hop", "french rap", "rap francais",
        "german hip-hop", "german hip hop", "german rap", "deutsch rap", "deutschrap",
        "russian hip-hop", "russian hip hop", "russian rap",
        "latin hip hop", "latin rap", "reggaeton",
        "australian hip-hop", "australian hip hop",
        "japanese hip hop", "japanese hip-hop",
    },

    "Rock & Metal": {
        "rock", "hard rock", "classic rock", "rock and roll", "rock & roll",
        "alternative rock", "alt-rock", "alt rock", "indie rock",
        "punk rock", "punk", "pop punk", "hardcore punk", "post-punk",
        "garage rock", "garage", "garage punk",
        "psychedelic rock", "psychedelic", "psych rock", "acid rock",
        "progressive rock", "prog rock", "art rock",
        "southern rock", "country rock", "heartland rock",
        "blues rock", "blues-rock",
        "folk rock", "folk-rock",
        "surf rock", "surf",
        "grunge", "seattle",
        "post-rock", "post rock",
        "noise rock", "noise",
        "stoner rock", "stoner metal", "doom", "sludge",
        "shoegaze",
        "emo", "emo rock", "screamo", "skramz",
        "math rock",
        "new wave", "no wave",
        "power pop",
        "britpop", "brit pop",
        "glam rock", "glam",
        "rockabilly", "psychobilly",
        "metal", "heavy metal",
        "thrash metal", "thrash",
        "death metal", "melodic death metal", "technical death metal",
        "black metal", "atmospheric black metal", "symphonic black metal",
        "doom metal", "funeral doom", "death-doom",
        "power metal", "symphonic metal",
        "progressive metal", "prog metal",
        "nu metal", "nu-metal", "rap metal", "rap rock",
        "metalcore", "deathcore", "mathcore",
        "groove metal",
        "speed metal",
        "industrial metal", "industrial rock", "industrial",
        "gothic metal", "gothic rock", "goth",
        "folk metal", "viking metal", "pagan metal",
        "sludge metal",
        "grindcore", "goregrind", "crust punk",
        "nwobhm",
        "djent",
        "post-metal", "post metal",
        "crossover thrash",
        "hair metal", "glam metal",
        "hardcore", "beatdown hardcore", "melodic hardcore",
        "ska", "ska punk", "reggae rock",
        "pop rock",
    },

    "Soul": {
        "trip-hop", "trip hop", "triphop",
        "downtempo", "chill-out", "chillout", "chill out",
        "lounge", "lounge music",
        "nu jazz", "nu-jazz", "acid jazz", "jazz-funk", "jazz funk",
        "jazz", "smooth jazz", "cool jazz", "jazz fusion",
        "bossa nova", "latin jazz", "afro-cuban jazz",
        "neo-soul", "neo soul", "new soul",
        "soul", "motown", "northern soul", "southern soul",
        "r&b", "rnb", "rhythm and blues", "contemporary r&b",
        "funk", "deep funk", "p-funk",
        "reggae", "dub", "roots reggae", "dancehall", "lovers rock",
        "afrobeat", "afrobeats", "afro",
        "world music", "world", "worldbeat",
        "latin", "salsa", "cumbia", "bachata",
        "bossa nova", "mpb", "brazilian",
        "flamenco", "fado",
        "indian classical", "bollywood", "bhangra",
        "middle eastern", "arabic",
        "african", "highlife", "afropop",
        "classical", "orchestral", "opera", "baroque", "romantic",
        "ambient", "dark ambient", "drone",
        "new age",
        "gospel", "spiritual",
        "blues", "delta blues", "chicago blues", "electric blues",
        "country", "bluegrass", "americana",
    },

    "Electronic": {
        "electronic", "electronica",
        "edm", "dance", "dance music",
        "house", "deep house", "tech house", "progressive house",
        "acid house", "funky house", "electro house", "french house",
        "minimal house", "minimal techno", "minimal",
        "future house", "future bass", "future garage",
        "garage", "uk garage", "speed garage", "2-step",
        "techno", "detroit techno", "acid techno", "hard techno",
        "trance", "psytrance", "goa trance", "uplifting trance",
        "progressive trance", "vocal trance",
        "hardstyle", "hardcore", "happy hardcore", "gabber",
        "dubstep", "brostep", "riddim", "melodic dubstep",
        "electro", "electro-funk", "electro funk",
        "synthwave", "retrowave", "outrun", "darksynth",
        "synthpop", "synth-pop", "synth pop", "electropop",
        "new wave", "darkwave", "coldwave",
        "italo disco", "italo-disco", "euro disco", "eurobeat",
        "hi-nrg", "hi nrg", "high energy",
        "disco", "nu-disco", "nu disco", "disco house",
        "eurodance", "euro dance", "hands up",
        "big beat",
        "breakbeat", "breaks", "nu skool breaks",
        "uk bass", "bass music", "bassline",
        "jungle", "ragga jungle", "darkside jungle",
        "drum and bass", "drum & bass", "dnb", "d&b", "d & b",
        "liquid dnb", "liquid drum and bass", "liquid funk",
        "neurofunk", "neuro", "techstep",
        "jump up", "jump-up",
        "darkstep", "crossbreed",
        "drumfunk", "drill and bass", "drill n bass",
        "intelligent dnb", "atmospheric dnb",
        "breakcore", "digital hardcore",
        "idm", "intelligent dance music",
        "glitch", "glitch hop", "wonky",
        "experimental electronic", "experimental",
        "noise", "power electronics", "harsh noise",
        "vaporwave", "future funk", "mallsoft",
        "chiptune", "8-bit", "chip music", "bitpop",
        "lo-fi", "lo fi", "lofi",
        "ambient", "dark ambient", "ambient techno",
        "downtempo", "chill-out", "chillout", "chill",
        "trip-hop", "trip hop",
        "dub techno", "dub",
        "industrial", "ebm", "aggrotech",
        "witch house", "drag",
        "footwork", "juke", "chicago juke",
        "jersey club",
        "baltimore club",
        "miami bass", "booty bass",
        "baile funk", "funk carioca",
        "moombahton", "moombahcore",
        "tropical house", "tropical",
        "afro house", "tribal house",
        "amapiano",
        "j-pop", "jpop", "j pop",
        "k-pop", "kpop", "k pop",
        "j-rock", "jrock", "j rock",
        "anime", "anime soundtrack", "anime ost",
        "video game music", "game music", "vgm",
        "pop", "pop music", "art pop", "dance pop",
        "indie", "indie pop", "indie electronic",
        "dream pop", "ethereal",
        "bedroom pop",
        "hyperpop", "hyper pop",
        "pc music",
        "nightcore",
        "denpa",
        "eurobeat", "super eurobeat", "para para",
    },

    "Oldies": {
        "50s", "1950s", "fifties",
        "60s", "1960s", "sixties",
        "70s", "1970s", "seventies",
        "80s", "1980s", "eighties",
        "90s", "1990s", "nineties",
        "oldies", "golden oldies",
        "classic", "timeless",
        "doo-wop", "doo wop", "doowop",
        "early rock and roll", "early rock & roll",
        "girl group", "girl groups",
        "motown",
        "bubblegum pop", "bubblegum",
        "soft rock",
        "yacht rock",
        "am pop",
        "easy listening",
        "adult contemporary",
        "standards", "great american songbook",
        "vocal jazz",
        "swing", "big band", "swing revival",
        "crooner",
        "country", "classic country", "outlaw country",
        "bluegrass",
        "folk", "traditional folk", "singer-songwriter",
        "blues", "delta blues", "chicago blues",
        "rockabilly",
        "surf", "surf rock", "surf pop",
        "british invasion",
        "mod", "northern soul",
        "teen pop", "teen idol",
        "novelty",
        "brill building",
        "wall of sound",
        "exotica",
    },
}

# Priority order when genre scores tie
PRIORITY = ["Hip Hop", "Rock & Metal", "Oldies", "Soul", "Electronic"]

# O(1) priority lookup used in sort key
_PRIORITY_IDX = {g: i for i, g in enumerate(PRIORITY)}

# Valid genre tags — anything outside this set triggers a re-tag
VALID_GENRES = {*PRIORITY, "Misc"}

# Consistent display order for summaries
_DISPLAY_ORDER = PRIORITY + ["Misc"]

# Confidence thresholds for genre assignment
HIGH_SCORE = 60
HIGH_RATIO = 2.0
LOW_SCORE  = 15
LOW_RATIO  = 1.0

# Seconds between API calls
RATE_LIMIT   = 0.25   # Last.fm
LRCLIB_RATE  = 0.10   # LRCLIB

# ── Graceful shutdown ──────────────────────────────────────────────────────

_stopping = False

def _handle_signal(sig, frame):
    global _stopping
    if not _stopping:
        _stopping = True
        print("\n  Stopping — finishing current file...", flush=True)

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ── Artist name cleanup ────────────────────────────────────────────────────

# Pre-compiled — strips YouTube channel suffixes before Last.fm lookup
_STRIP_RES = [re.compile(p, re.IGNORECASE) for p in (
    r'\s*-\s*topic$',
    r'\s*official\s*channel$',
    r'\s*official$',
    r'^official\s+',
    r'\s*music$',
    r'\s*vevo$',
    r'vevo$',
    r'\s*tv$',
    r'\s*hq$',
    r'\s*records?$',
    r'\s*entertainment$',
    r'\s*channel$',
)]

def clean_artist(name):
    """Take primary artist (first before comma) and strip YouTube channel suffixes."""
    s = name.strip()
    if ',' in s:
        s = s.split(',')[0].strip()
    if ' / ' in s:
        s = s.split(' / ')[-1].strip()
    for pat in _STRIP_RES:
        s = pat.sub('', s).strip()
    return s

# ── Junk tag filter ────────────────────────────────────────────────────────

_JUNK_TAG_RES = [
    re.compile(r'^\d+\s*stars?$',       re.IGNORECASE),
    re.compile(r'^seen live$',           re.IGNORECASE),
    re.compile(r'^my playlist$',         re.IGNORECASE),
    re.compile(r'^favou?rites?$',        re.IGNORECASE),
    re.compile(r'^check out$',           re.IGNORECASE),
    re.compile(r'^albums i own$',        re.IGNORECASE),
    re.compile(r'^under \d+ listeners$', re.IGNORECASE),
]

def filter_junk_tags(tags):
    """Drop personal/non-genre tags from Last.fm results."""
    return [(n, w) for n, w in tags if not any(p.match(n.strip()) for p in _JUNK_TAG_RES)]

# ── Genre scoring ──────────────────────────────────────────────────────────

def score_tags(tags):
    """Map (tag, weight) pairs to per-bucket scores."""
    scores = defaultdict(float)
    for tag_name, weight in tags:
        t = tag_name.lower().strip()
        for genre, tag_set in GENRE_TAGS.items():
            if t in tag_set:
                scores[genre] += weight
    return dict(scores)


def assign_genre(scores):
    """Apply confidence thresholds and return (genre, level)."""
    if not scores:
        return "Misc", "misc"

    sorted_scores = sorted(
        scores.items(),
        key=lambda x: (-x[1], _PRIORITY_IDX.get(x[0], 99))
    )

    winner_genre, winner_score = sorted_scores[0]
    runner_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0.0
    ratio = winner_score / runner_score if runner_score > 0 else float("inf")

    if winner_score >= HIGH_SCORE and ratio >= HIGH_RATIO:
        return winner_genre, "high"
    if winner_score >= LOW_SCORE and ratio >= LOW_RATIO:
        return winner_genre, "low"
    # Only one bucket matched — take it at low confidence
    if len(sorted_scores) == 1 and winner_score > 0:
        return winner_genre, "low"
    return "Misc", "misc"

# ── Last.fm helpers ────────────────────────────────────────────────────────

def get_track_tags(network, artist, title):
    """Fetch track-level tags from Last.fm."""
    try:
        top = network.get_track(artist, title).get_top_tags(limit=10)
        if top:
            return [(t.item.get_name(), int(t.weight)) for t in top]
    except Exception:
        pass
    return []


def get_artist_tags(network, artist, cache):
    """Fetch artist-level tags from Last.fm, cached by name."""
    if artist in cache:
        return cache[artist]
    try:
        top = network.get_artist(artist).get_top_tags(limit=10)
        if top:
            tags = [(t.item.get_name(), int(t.weight)) for t in top]
            cache[artist] = tags
            time.sleep(RATE_LIMIT)
            return tags
    except Exception:
        pass
    cache[artist] = []
    return []


def search_artist_tags(network, artist, cache):
    """Search Last.fm by name, use top result's tags."""
    key = f"__search__{artist}"
    if key in cache:
        return cache[key]
    try:
        results = network.search_for_artist(artist).get_next_page()
        if results:
            top = results[0].get_top_tags(limit=10)
            if top:
                tags = [(t.item.get_name(), int(t.weight)) for t in top]
                cache[key] = tags
                time.sleep(RATE_LIMIT)
                return tags
    except Exception:
        pass
    cache[key] = []
    return []

# ── Progressive channel name cleanup ──────────────────────────────────────

_CHANNEL_SUFFIX_RES = [
    re.compile(r'official\s*$',  re.IGNORECASE),
    re.compile(r'music\s*$',     re.IGNORECASE),
    re.compile(r'band\s*$',      re.IGNORECASE),
    re.compile(r'archive\s*$',   re.IGNORECASE),
    re.compile(r'tv\s*$',        re.IGNORECASE),
    re.compile(r'hq\s*$',        re.IGNORECASE),
    re.compile(r'channel\s*$',   re.IGNORECASE),
    re.compile(r'records?\s*$',  re.IGNORECASE),
    re.compile(r'\d+\s*$'),
]

def try_channel_name_cleanup(network, artist_name, cache):
    """
    Progressively strip channel suffixes and re-query Last.fm.
    Only accepts if the result scores a non-Misc genre.
    """
    name = artist_name.strip()
    for pat in _CHANNEL_SUFFIX_RES:
        new_name = pat.sub('', name).strip()
        if new_name and new_name != name and len(new_name) >= 2:
            tags = get_artist_tags(network, new_name, cache)
            if tags:
                scores = score_tags(filter_junk_tags(tags))
                if assign_genre(scores)[0] != "Misc":
                    return tags, new_name
            name = new_name
    return [], None

# ── Title cleaning ─────────────────────────────────────────────────────────

_TITLE_JUNK_RES = [
    re.compile(r'\(official\s*(video|audio|music\s*video|track|lyric\s*video)\)', re.IGNORECASE),
    re.compile(r'\(feat\.?\s*[^)]+\)',  re.IGNORECASE),
    re.compile(r'\(ft\.?\s*[^)]+\)',    re.IGNORECASE),
    re.compile(r'\(with\s+[^)]+\)',     re.IGNORECASE),
    re.compile(r'\(full\s*song\)',       re.IGNORECASE),
    re.compile(r'\(hq\)',               re.IGNORECASE),
    re.compile(r'\(\d{4}\)',            re.IGNORECASE),
    re.compile(r'\(remaster(?:ed)?\)',  re.IGNORECASE),
    re.compile(r'shot\s*by\s*.*$',      re.IGNORECASE),
    re.compile(r'\[.*?\]'),
]

def clean_title(title):
    """Strip parenthetical junk from title for cleaner Last.fm searches."""
    s = title.strip()
    for pat in _TITLE_JUNK_RES:
        s = pat.sub('', s).strip()
    return s

# ── Title parsing fallback ─────────────────────────────────────────────────

def try_artist_from_title(network, title, cache):
    """
    When metadata fails, extract artist from 'Artist - Song' or 'Artist / Song' title formats.
    """
    normalized = title.replace('⧸', '/').replace('／', '/')

    for sep in (' - ', ' / '):
        if sep not in normalized:
            continue
        left, right = normalized.split(sep, 1)
        left  = left.strip()
        right = right.strip()

        if not left or len(left) < 2:
            continue

        tags = get_artist_tags(network, left, cache)
        if tags:
            return tags, "title_parse"

        if len(right) >= 2:
            clean_right = clean_title(right)
            if clean_right:
                track_tags = get_track_tags(network, left, clean_right)
                if track_tags:
                    return track_tags, "title_parse_track"

        if len(right) >= 2:
            tags = get_artist_tags(network, right, cache)
            if tags:
                return tags, "title_parse_reverse"

    return [], None


def keyword_genre_fallback(fname, title):
    """Last resort: scan filename and title for genre-defining keywords."""
    text = f"{fname} {title}".lower()
    for genre, keywords in KEYWORD_GENRES.items():
        for kw in keywords:
            if kw in text:
                return genre, f"keyword:{kw}"
    return None, None


def artist_name_genre_match(artist_clean, artist_raw):
    """Check if the artist name itself is a known genre tag."""
    for name in (artist_clean, artist_raw):
        t = name.lower().strip()
        if len(t) >= 3:
            for genre, tag_set in GENRE_TAGS.items():
                if t in tag_set:
                    return genre, "artist_name_match"
    return None, None


def get_collection(fname):
    """Return (folder, genre) if filename matches a known collection, else (None, None)."""
    fn_lower = fname.lower()
    for keyword, info in COLLECTIONS.items():
        if keyword in fn_lower:
            return info["folder"], info["genre"]
    return None, None

# ── Download ───────────────────────────────────────────────────────────────

def download(url, dry_run=False):
    """Run yt-dlp to download a playlist or video to the landing dir."""
    LANDING_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_FILE.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "yt-dlp",
        "--extract-audio",
        "--audio-format",         "m4a",
        "--audio-quality",        "0",
        "--embed-thumbnail",
        "--convert-thumbnails",   "jpg",
        "--embed-metadata",
        "--embed-subs",
        "--sub-langs",            "en.*",
        "--download-archive",     str(ARCHIVE_FILE),
        "--output",               str(LANDING_DIR / "%(uploader)s - %(title)s.%(ext)s"),
        "--ignore-errors",
        "--continue",
        "--no-overwrites",
        "--sleep-interval",       "2",
        "--max-sleep-interval",   "5",
        "--sleep-requests",       "1",
        "--rate-limit",           "1.5M",
        "--concurrent-fragments", "3",
        "--retries",              "infinite",
        "--fragment-retries",     "infinite",
        "--parse-metadata",       "%(uploader)s:%(meta_artist)s",
        "--parse-metadata",       "%(title)s:%(meta_title)s",
        "--replace-in-metadata",  "uploader", " - Topic$", "",
        "--match-filter",         "!is_live & !live",
        "--check-formats",
    ]

    if _BROWSER_PROFILE:
        cmd += ["--cookies-from-browser", f"{_BROWSER}:{_BROWSER_PROFILE}"]
    else:
        cmd += ["--cookies-from-browser", _BROWSER]

    cmd.append(url)

    if dry_run:
        print(f"  [dry run] yt-dlp → {LANDING_DIR}")
        return

    print(f"  Downloading to {LANDING_DIR}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"  yt-dlp exited with code {result.returncode} (some items may have been skipped)")

# ── Cover crop ─────────────────────────────────────────────────────────────

def crop_covers(files, dry_run=False):
    """Center-crop embedded cover art to a 1:1 square."""
    fixed = skipped = errors = 0

    for filepath in files:
        if _stopping:
            break
        try:
            audio = mutagen.mp4.MP4(filepath)
        except Exception:
            errors += 1
            continue

        covers = audio.tags.get("covr", []) if audio.tags else []
        if not covers:
            skipped += 1
            continue

        cover = covers[0]
        try:
            img = Image.open(BytesIO(bytes(cover)))
        except Exception:
            errors += 1
            continue

        w, h = img.size
        if w == h:
            skipped += 1
            continue

        if dry_run:
            print(f"  [crop] {filepath.name[:70]} ({w}x{h} → {min(w,h)}x{min(w,h)})")
            fixed += 1
            continue

        side = min(w, h)
        img  = img.crop(((w - side) // 2, (h - side) // 2,
                          (w + side) // 2, (h + side) // 2))

        pil_fmt = "JPEG" if cover.imageformat == MP4Cover.FORMAT_JPEG else "PNG"
        buf = BytesIO()
        img.save(buf, format=pil_fmt)
        audio.tags["covr"] = [MP4Cover(buf.getvalue(), imageformat=cover.imageformat)]
        audio.save()
        fixed += 1

    return fixed, skipped, errors

# ── Clean filenames ────────────────────────────────────────────────────────

_RE_INDEX = re.compile(r'^\d{4} - ')
_RE_TOPIC = re.compile(r' - Topic(?= - )')

def clean_filenames(files, dry_run=False):
    """Strip leading 4-digit index and '- Topic' from filenames."""
    cleaned = skipped = 0
    errors   = []
    path_map = {}

    for filepath in files:
        if _stopping:
            break
        fname    = filepath.name
        new_name = _RE_INDEX.sub('', fname)
        new_name = _RE_TOPIC.sub('', new_name)

        if new_name == fname:
            skipped += 1
            continue

        new_path = filepath.parent / new_name

        if dry_run:
            print(f"  [clean] {fname[:80]}")
            print(f"        → {new_name[:80]}")
            cleaned += 1
            continue

        if new_path.exists() and new_path != filepath:
            errors.append(f"SKIP (exists): {new_name}")
            continue

        try:
            filepath.rename(new_path)
            path_map[filepath] = new_path
            cleaned += 1
        except Exception as e:
            errors.append(f"ERROR: {fname}: {e}")

    return cleaned, skipped, errors, path_map

# ── Lyrics ─────────────────────────────────────────────────────────────────

LRCLIB_API   = "https://lrclib.net/api/get"
_RE_LRC_SAFE = re.compile(r'[\\/<>|]')


def _lrc_path(artist, title):
    """Build the .lrc file path for a given artist and title."""
    safe = _RE_LRC_SAFE.sub('', f"{artist} - {title}")
    return LYRICS_DIR / f"{safe}.lrc"


def _fetch_lrclib(artist, title):
    """Query LRCLIB for synced lyrics. Returns the LRC string or None."""
    params = urllib.parse.urlencode({"artist_name": artist, "track_name": title})
    req    = urllib.request.Request(
        f"{LRCLIB_API}?{params}", headers={"User-Agent": "download.py/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("syncedLyrics")
    except Exception:
        return None


def fetch_lyrics(files, dry_run=False):
    """Fetch synced lyrics from LRCLIB and save as .lrc files."""
    LYRICS_DIR.mkdir(parents=True, exist_ok=True)
    fetched = skipped = no_match = errors = 0
    total   = len(files)

    for idx, filepath in enumerate(files, 1):
        if _stopping:
            break
        prefix = f"  [{idx:>4}/{total}]"

        try:
            audio = mutagen.mp4.MP4(filepath)
        except Exception:
            errors += 1
            continue

        if not audio.tags:
            skipped += 1
            continue

        artist = audio.tags.get("\xa9ART", [""])[0].strip()
        title  = audio.tags.get("\xa9nam", [""])[0].strip()

        if not artist or not title:
            skipped += 1
            continue

        # Raw artist in the header so rmpc matches exactly to MPD metadata.
        # Cleaned artist used only for the LRCLIB search query.
        lrc_file = _lrc_path(artist, title)

        if lrc_file.exists():
            print(f"{prefix} {filepath.name}")
            print(f"               → (skip — already exists)\n")
            skipped += 1
            continue

        if dry_run:
            print(f"{prefix} {filepath.name}")
            print(f"               → {lrc_file.name}\n")
            fetched += 1
            continue

        print(f"{prefix} {filepath.name}")

        synced = _fetch_lrclib(clean_artist(artist), title)
        if not synced:
            print(f"               → (no synced lyrics found)\n")
            no_match += 1
            continue

        try:
            lrc_file.write_text(f"[ar:{artist}]\n[ti:{title}]\n\n{synced}\n", encoding="utf-8")
            print(f"               → {lrc_file.name}\n")
            fetched += 1
        except Exception:
            print(f"               → (error writing file)\n")
            errors += 1

        time.sleep(LRCLIB_RATE)

    return fetched, skipped, no_match, errors

# ── Genre tagging ──────────────────────────────────────────────────────────

_RE_FNAME    = re.compile(r"^(.+?) - (.+)\.m4a$")
_CONF_LABEL  = {"high": "", "low": " [low]", "misc": " [misc]"}

def tag_files(files, network, dry_run=False, verbose=False, force=False):
    """Tag files with a genre via Last.fm. Returns (results, counts, file_genres)."""
    artist_cache = {}
    results      = []
    counts       = defaultdict(int)
    file_genres  = []
    total        = len(files)

    for idx, filepath in enumerate(files, 1):
        if _stopping:
            break
        fname = filepath.name
        print(f"  [{idx:>4}/{total}] {fname[:55].ljust(55)}", end=" ", flush=True)

        try:
            audio = mutagen.mp4.MP4(filepath)
        except Exception as e:
            print(f"ERROR: {e}")
            continue

        # Skip if already tagged with a valid genre (unless --force or existing genre is invalid)
        if not force and audio.tags:
            existing = audio.tags.get("\xa9gen", [None])[0]
            if existing in VALID_GENRES:
                print(f"skip ({existing})")
                counts[existing] += 1
                file_genres.append((filepath, existing))
                continue
            if existing:
                print(f"re-tag (invalid: {existing})", end=" ", flush=True)

        # Read metadata, fall back to parsing the filename
        artist_raw = title = ""
        if audio.tags:
            artist_raw = audio.tags.get("\xa9ART", [""])[0]
            title      = audio.tags.get("\xa9nam", [""])[0]
        if not artist_raw or not title:
            m = _RE_FNAME.match(fname)
            if m:
                artist_raw, title = m.group(1), m.group(2)

        artist_clean = clean_artist(artist_raw)
        source       = "none"

        # Fallback chain: track → artist → raw artist → search → channel cleanup → title parse
        tags = get_track_tags(network, artist_clean, title)
        if tags:
            source = "lastfm_track"
        time.sleep(RATE_LIMIT)

        if not tags:
            tags = get_artist_tags(network, artist_clean, artist_cache)
            if tags:
                source = "lastfm_artist"

        if not tags and artist_clean != artist_raw:
            tags = get_artist_tags(network, artist_raw, artist_cache)
            if tags:
                source = "lastfm_artist_raw"

        if not tags:
            tags = search_artist_tags(network, artist_clean, artist_cache)
            if tags:
                source = "lastfm_search"

        if not tags:
            tags, _ = try_channel_name_cleanup(network, artist_raw, artist_cache)
            if tags:
                source = "channel_cleanup"

        if not tags:
            tags, tp_src = try_artist_from_title(network, title, artist_cache)
            if tags:
                source = tp_src

        kw_genre = kw_source = None
        if not tags:
            kw_genre, kw_source = keyword_genre_fallback(fname, title)

        tags              = filter_junk_tags(tags)
        scores            = score_tags(tags)
        genre, confidence = assign_genre(scores)

        if genre == "Misc" and kw_genre:
            genre, confidence, source = kw_genre, "low", kw_source

        if genre == "Misc":
            an_genre, an_source = artist_name_genre_match(artist_clean, artist_raw)
            if an_genre:
                genre, confidence, source = an_genre, "low", an_source

        # Collection keyword overrides everything
        _, col_genre = get_collection(fname)
        if col_genre:
            genre, confidence, source = col_genre, "high", "collection"

        if not dry_run:
            if not audio.tags:
                audio.add_tags()
            audio.tags["\xa9gen"] = [genre]
            audio.save()

        print(f"→ {genre}{_CONF_LABEL.get(confidence, '')}")

        if verbose:
            if tags:
                print(f"           tags:   {', '.join(f'{n}:{w}' for n, w in tags[:8])}")
                print(f"           scores: {', '.join(f'{g}={s:.0f}' for g, s in sorted(scores.items(), key=lambda x: -x[1]))}")
            else:
                raw_note = f" (raw: '{artist_raw}')" if artist_raw != artist_clean else ""
                print(f"           tags:   (none) — artist='{artist_clean}'{raw_note}")

        counts[genre] += 1
        file_genres.append((filepath, genre))
        results.append({
            "num": idx, "file": fname,
            "artist": artist_clean, "artist_raw": artist_raw,
            "title": title, "genre": genre,
            "confidence": confidence, "source": source,
            "scores": scores, "tags": tags,
        })

    return results, counts, file_genres

# ── Sort ───────────────────────────────────────────────────────────────────

def sort_files(file_genres, dry_run=False):
    """Move files into PLAYLISTS_DIR/{genre}/ or collection subfolders."""
    moved   = defaultdict(int)
    skipped = 0
    errors  = []

    for filepath, genre in file_genres:
        if _stopping:
            break
        col_folder   = get_collection(filepath.name)[0]
        folder_label = col_folder or genre
        if col_folder:
            dest_dir = PLAYLISTS_DIR / col_folder
        else:
            dest_dir = PLAYLISTS_DIR / (genre if genre in VALID_GENRES else "Misc")
        dest = dest_dir / filepath.name

        if filepath.parent == dest_dir:
            skipped += 1
            continue

        if dry_run:
            print(f"  [sort] {filepath.name[:55]:55} → {folder_label}/")
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            errors.append(f"SKIP (exists): {filepath.name} → {dest_dir.name}/")
            continue
        try:
            filepath.rename(dest)
            moved[folder_label] += 1
        except Exception as e:
            errors.append(f"ERROR: {filepath.name}: {e}")

    return moved, skipped, errors

# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Download, clean, tag, and sort music files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("url",           nargs="?",          help="YouTube playlist or video URL")
    parser.add_argument("--process",     metavar="DIR",       help="Process existing files (skip download)")
    parser.add_argument("--dry-run",     action="store_true", help="Preview changes without modifying files")
    parser.add_argument("--skip-crop",   action="store_true", help="Skip cover art cropping")
    parser.add_argument("--skip-tag",    action="store_true", help="Skip genre tagging")
    parser.add_argument("--skip-lyrics", action="store_true", help="Skip lyrics fetching")
    parser.add_argument("--skip-sort",   action="store_true", help="Skip sorting into folders")
    parser.add_argument("--verbose",     action="store_true", help="Show Last.fm tag details per file")
    parser.add_argument("--force",       action="store_true", help="Re-tag files that already have a genre")
    args = parser.parse_args()

    if not args.url and not args.process:
        parser.print_help()
        sys.exit(1)

    if args.dry_run:
        print("DRY RUN — no files will be modified\n")

    # Step 1: Download
    if args.url:
        print("── Download " + "─" * 39)
        download(args.url, dry_run=args.dry_run)
        print()
        scan_dir = LANDING_DIR
    else:
        scan_dir = Path(args.process)
        if not scan_dir.is_dir():
            print(f"Error: directory not found: {scan_dir}")
            sys.exit(1)

    files = sorted(scan_dir.rglob("*.m4a"))
    if not files:
        print("No .m4a files found.")
        return
    print(f"Found {len(files)} files in {scan_dir}\n")

    # Step 2: Crop covers
    if not args.skip_crop:
        print("── Crop Covers " + "─" * 35)
        fixed, skipped, errors = crop_covers(files, dry_run=args.dry_run)
        label = "Would crop" if args.dry_run else "Cropped"
        print(f"  {label}: {fixed}, already square: {skipped}, errors: {errors}\n")
        if _stopping:
            sys.exit(130)

    # Step 3: Clean filenames
    print("── Clean Filenames " + "─" * 31)
    cleaned, skipped, clean_errors, path_map = clean_filenames(files, dry_run=args.dry_run)
    label = "Would clean" if args.dry_run else "Cleaned"
    print(f"  {label}: {cleaned}, already clean: {skipped}")
    for err in clean_errors:
        print(f"  {err}")
    if path_map:
        files = [path_map.get(f, f) for f in files]
    print()
    if _stopping:
        sys.exit(130)

    # Step 4: Tag genres
    file_genres = []
    if not args.skip_tag:
        if not CONFIG_FILE.exists():
            print(f"Error: config not found at {CONFIG_FILE}")
            print("Expected format: [lastfm] / api_key = YOUR_KEY")
            sys.exit(1)

        config = configparser.ConfigParser()
        config.read(CONFIG_FILE)
        network = pylast.LastFMNetwork(api_key=config["lastfm"]["api_key"])

        print("── Tag Genres " + "─" * 36)
        results, counts, file_genres = tag_files(
            files, network, dry_run=args.dry_run,
            verbose=args.verbose, force=args.force,
        )

        if results and not args.dry_run:
            log_json = LOG_DIR / "download-log.json"
            log_txt  = LOG_DIR / "download-log.txt"
            with open(log_json, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            with open(log_txt, "w", encoding="utf-8") as f:
                f.write(f"Download Log — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Files processed: {len(results)}\n\n")
                for r in results:
                    tag_str   = ", ".join(f"{n}:{w}" for n, w in r["tags"][:8]) if r["tags"] else "(none)"
                    score_str = ", ".join(f"{g}={s:.0f}" for g, s in sorted(r["scores"].items(), key=lambda x: -x[1])) if r["scores"] else "(none)"
                    f.write(f"[{r['num']:04d}] {r['file']}\n")
                    f.write(f"       → {r['genre']} ({r['confidence']}) via {r['source']}\n")
                    f.write(f"       tags:   {tag_str}\n")
                    f.write(f"       scores: {score_str}\n\n")

        print()
        print("=" * 50)
        print("  TAGGING COMPLETE")
        print("=" * 50)
        for genre in _DISPLAY_ORDER:
            count = counts.get(genre, 0)
            bar   = "█" * (count // 20) if count else ""
            print(f"  {genre:<15} {count:>5}  {bar}")

        low_conf = [r for r in results if r["confidence"] == "low"]
        misc     = [r for r in results if r["genre"] == "Misc"]
        print(f"\n  Low confidence: {len(low_conf)}")
        print(f"  Misc (review):  {len(misc)}")
        if results and not args.dry_run:
            print(f"  Logs:           {log_json}")
            print(f"                  {log_txt}")
        print()
        if _stopping:
            sys.exit(130)

    # Step 5: Fetch lyrics
    if not args.skip_lyrics:
        print("── Fetch Lyrics " + "─" * 34)
        fetched, skipped, no_match, errors = fetch_lyrics(files, dry_run=args.dry_run)
        if args.dry_run:
            print(f"  Would fetch: {fetched}, already have: {skipped}, no match: {no_match}")
        else:
            print(f"  Fetched: {fetched}, already have: {skipped}, no match: {no_match}, errors: {errors}")
        print()
        if _stopping:
            sys.exit(130)

    # Step 6: Sort into genre folders (requires tagging)
    if not args.skip_sort:
        print("── Sort Files " + "─" * 36)
        if not file_genres:
            print("  Sort skipped (no tagging results)\n")
        else:
            moved, skipped, sort_errors = sort_files(file_genres, dry_run=args.dry_run)
            if args.dry_run:
                print("  (dry run — no files moved)")
            else:
                for genre in _DISPLAY_ORDER:
                    c = moved.get(genre, 0)
                    if c:
                        print(f"  {genre:<15} {c:>5} moved")
                for key in sorted(moved):
                    if key not in PRIORITY and key != "Misc":
                        print(f"  {key:<30} {moved[key]:>5} moved")
                if skipped:
                    print(f"  Already in correct folder: {skipped}")
                for err in sort_errors:
                    print(f"  {err}")


if __name__ == "__main__":
    main()
