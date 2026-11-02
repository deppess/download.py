"""The normal one-command download, sort, and Music-import workflow."""

from __future__ import annotations

import argparse
import configparser
import json
import os
import uuid
from pathlib import Path

from library_safety import SafetyError
from music_adapter import import_plan, reconcile_plan
from music_paths import load_paths
from pipeline_intake import intake
from pipeline_state import connect, writer_lock
from pipeline_workflow import approve_plan, create_plan, enrich, publish_plan


def _lastfm_key(config_path: Path) -> str | None:
    """Use the environment first, then retain compatibility with legacy config."""
    key = os.environ.get("LASTFM_API_KEY", "").strip()
    if key:
        return key
    config = configparser.ConfigParser()
    if config_path.is_file():
        config.read(config_path)
        return config.get("lastfm", "api_key", fallback="").strip() or None
    return None


def mark_review_for_simple_import(state_file: Path) -> int:
    """Keep uncertain tracks, but make their destination explicitly Review."""
    db = connect(state_file)
    try:
        result = db.execute(
            """UPDATE media_files SET genre='Review', review_status='ready',
               classification_reason=COALESCE(classification_reason, 'automatic Review placement')
               WHERE role='raw' AND COALESCE(review_status, 'review') != 'ready'"""
        )
        db.commit()
        return result.rowcount
    finally:
        db.close()


def resume_pending_music_imports(state_file: Path, library: Path) -> dict[str, int]:
    """Finish a prior published simple-run plan before making a new one."""
    db = connect(state_file)
    try:
        rows = db.execute(
            """SELECT r.plan_id, r.summary_json FROM plan_runs r
               WHERE r.status='published' AND EXISTS (
                   SELECT 1 FROM plan_items p LEFT JOIN music_imports i ON i.plan_item_id=p.id
                   WHERE p.plan_id=r.plan_id AND (i.status IS NULL OR i.status='uncertain')
               ) ORDER BY r.id"""
        ).fetchall()
    finally:
        db.close()
    totals = {"reconciled": 0, "music_imported": 0, "music_already_present": 0}
    for row in rows:
        plan_path = Path(json.loads(row["summary_json"])["path"])
        reconciliation = reconcile_plan(state_file, library, plan_path)
        totals["reconciled"] += reconciliation["verified"]
        result = import_plan(state_file, library, plan_path)
        totals["music_imported"] += result["imported"]
        totals["music_already_present"] += result["already_present"]
    return totals


def run(
    url: str,
    paths,
    use_cookies: bool = True,
    po_token: str | None = None,
    with_lyrics: bool = False,
) -> dict[str, int]:
    """Fetch, preserve, sort, publish, and import without exposing intermediate steps."""
    import download as legacy  # Avoid an import cycle while download.py dispatches us.

    source, force_playlist = (
        (legacy.DEFAULT_PLAYLIST_URL, True) if url.casefold() == "playlist" else (url, False)
    )
    downloaded = legacy.download(
        source, force_playlist=force_playlist, use_cookies=use_cookies, po_token=po_token
    )
    if not downloaded:
        raise SafetyError(
            "Download did not complete successfully; incoming files were left untouched"
        )
    plan_path = paths.state / "plans" / f"simple-{uuid.uuid4().hex}.json"
    state_file = paths.state / "pipeline.sqlite3"
    with writer_lock(paths.state):
        resumed = resume_pending_music_imports(state_file, paths.library)
        intake_result = intake(paths.landing / "incoming", paths.landing / "raw", state_file)
        if intake_result["accepted"] == 0:
            return {
                **resumed,
                "intake": 0,
                "review_placed": 0,
                "published": 0,
                "classified_ready": 0,
            }
        enrich_result = enrich(
            state_file,
            paths.landing,
            with_lyrics=with_lyrics,
            lastfm_api_key=_lastfm_key(legacy.CONFIG_FILE),
        )
        review_count = mark_review_for_simple_import(state_file)
        create_plan(state_file, paths.library, plan_path)
        approve_plan(state_file, plan_path)
        published = publish_plan(state_file, paths.landing, paths.library, plan_path)
        music = import_plan(state_file, paths.library, plan_path)
    return {
        **resumed,
        "intake": intake_result["accepted"],
        "review_placed": review_count,
        "published": published,
        "music_imported": music["imported"],
        "music_already_present": music["already_present"],
        "classified_ready": enrich_result.get("ready", 0),
    }


def main(argv: list[str] | None = None) -> int:
    paths = load_paths()
    parser = argparse.ArgumentParser(description="Download, sort, and import music in one command.")
    parser.add_argument("url", help="A YouTube URL, or 'playlist' for the configured playlist")
    parser.add_argument("--no-cookies", action="store_true")
    parser.add_argument("--po-token")
    parser.add_argument("--with-lyrics", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(args.url, paths, not args.no_cookies, args.po_token, args.with_lyrics)
        print("Complete: " + ", ".join(f"{key}={value}" for key, value in result.items()))
        return 0
    except SafetyError as exc:
        print(f"Stopped: {exc}")
        return 2
