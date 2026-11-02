"""Small, add-only macOS Music adapter used after verified publication.

The adapter never writes Music's private files. Its only mutating AppleScript
command is ``add`` for one already-published local file.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from library_safety import SafetyError, sha256
from music_paths import load_paths
from pipeline_state import connect, utc_now, writer_lock
from pipeline_workflow import load_plan

_DELIMITER = "\x1e"
_SCRIPT = r"""
on run argv
    if (count of argv) is not 1 then error "expected one local file path"
    set sourceFile to POSIX file (item 1 of argv) as alias
    tell application "Music"
        set addedTrack to add {sourceFile}
        set trackID to persistent ID of addedTrack
        set trackLocation to POSIX path of (location of addedTrack as alias)
    end tell
    return trackID & character id 30 & trackLocation
end run
"""

_LOOKUP_SCRIPT = r"""
on run argv
    if (count of argv) is not 1 then error "expected one local file path"
    set sourceFile to POSIX file (item 1 of argv) as alias
    tell application "Music"
        set matches to every file track of library playlist 1 whose location is sourceFile
        if (count of matches) is 0 then return ""
        if (count of matches) is not 1 then error "multiple Music tracks match this file"
        set matchedTrack to item 1 of matches
        return (persistent ID of matchedTrack) & character id 30 & (POSIX path of (location of matchedTrack as alias))
    end tell
end run
"""


def _same_local_file(left: Path, right: Path) -> bool:
    """Compare filesystem objects, accepting APFS's canonical path casing."""
    try:
        return os.path.samefile(left, right)
    except OSError:
        return left.resolve() == right.resolve()


def add_to_music(path: Path, runner=subprocess.run) -> tuple[str, Path]:
    """Add one local file through Music and return its ID and reported location."""
    if sys.platform != "darwin":
        raise SafetyError("Apple Music import is available only on macOS")
    if not path.is_file():
        raise SafetyError(f"Published file does not exist: {path}")
    result = runner(
        ["/usr/bin/osascript", "-l", "AppleScript", "-", str(path)],
        input=_SCRIPT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise SafetyError(f"Music import failed: {(result.stderr or result.stdout).strip()}")
    try:
        persistent_id, reported = result.stdout.rstrip("\n").split(_DELIMITER, 1)
    except ValueError as exc:
        raise SafetyError("Music import returned an unexpected result") from exc
    if not persistent_id or not reported:
        raise SafetyError("Music import did not return a persistent ID and location")
    return persistent_id, Path(reported).resolve()


def find_in_music(path: Path, runner=subprocess.run) -> tuple[str, Path] | None:
    """Read-only lookup of an already-added local Music file by exact location."""
    if sys.platform != "darwin":
        raise SafetyError("Apple Music lookup is available only on macOS")
    if not path.is_file():
        raise SafetyError(f"Published file does not exist: {path}")
    result = runner(
        ["/usr/bin/osascript", "-l", "AppleScript", "-", str(path)],
        input=_LOOKUP_SCRIPT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise SafetyError(f"Music lookup failed: {(result.stderr or result.stdout).strip()}")
    output = result.stdout.rstrip("\n")
    if not output:
        return None
    try:
        persistent_id, reported = output.split(_DELIMITER, 1)
    except ValueError as exc:
        raise SafetyError("Music lookup returned an unexpected result") from exc
    return persistent_id, Path(reported).resolve()


def import_plan(
    state_file: Path, library: Path, plan_path: Path, runner=subprocess.run
) -> dict[str, int]:
    """Add every published plan item once, stopping on the first uncertainty."""
    plan = load_plan(plan_path)
    library = library.resolve()
    db = connect(state_file)
    imported = already_present = 0
    try:
        items = db.execute(
            """SELECT p.id, p.raw_sha256, p.destination_relative_path, i.status
               FROM plan_items p LEFT JOIN music_imports i ON i.plan_item_id=p.id
               WHERE p.plan_id=? AND p.status='published' ORDER BY p.id""",
            (plan["plan_id"],),
        ).fetchall()
        if not items:
            raise SafetyError("Plan has no published tracks to import into Music")
        for item in items:
            if item["status"] == "verified":
                already_present += 1
                continue
            if item["status"] is not None:
                raise SafetyError(
                    f"Music import for plan item {item['id']} is {item['status']}; run reconciliation before retrying"
                )
            track = library / item["destination_relative_path"]
            if not track.is_file() or sha256(track) != item["raw_sha256"]:
                raise SafetyError(f"Published track is missing or changed: {track}")
            db.execute(
                "INSERT INTO music_imports(plan_item_id, status, created_at) VALUES (?, 'intent', ?)",
                (item["id"], utc_now()),
            )
            db.commit()  # Intent must survive an Apple-event interruption.
            try:
                persistent_id, location = add_to_music(track, runner)
            except Exception as exc:
                db.execute(
                    "UPDATE music_imports SET status='uncertain', error=? WHERE plan_item_id=?",
                    (str(exc), item["id"]),
                )
                db.commit()
                raise
            if not _same_local_file(location, track):
                db.execute(
                    """UPDATE music_imports SET status='location_mismatch', persistent_id=?, reported_location=?, error=?
                       WHERE plan_item_id=?""",
                    (
                        persistent_id,
                        str(location),
                        "Music reported a different file location",
                        item["id"],
                    ),
                )
                db.commit()
                raise SafetyError(f"Music reported an unexpected location for {track}: {location}")
            db.execute(
                """UPDATE music_imports SET status='verified', persistent_id=?, reported_location=?, verified_at=?
                   WHERE plan_item_id=?""",
                (persistent_id, str(location), utc_now(), item["id"]),
            )
            db.commit()
            imported += 1
        return {"imported": imported, "already_present": already_present}
    finally:
        db.close()


def reconcile_plan(state_file: Path, library: Path, plan_path: Path) -> dict[str, int]:
    """Resolve prior uncertain imports by read-only exact-location lookup."""
    plan = load_plan(plan_path)
    library = library.resolve()
    db = connect(state_file)
    verified = unresolved = 0
    try:
        rows = db.execute(
            """SELECT p.id, p.destination_relative_path, i.status FROM plan_items p
               JOIN music_imports i ON i.plan_item_id=p.id
               WHERE p.plan_id=? AND i.status IN ('uncertain', 'location_mismatch') ORDER BY p.id""",
            (plan["plan_id"],),
        ).fetchall()
        for row in rows:
            track = library / row["destination_relative_path"]
            found = find_in_music(track)
            if found is None or not _same_local_file(found[1], track):
                unresolved += 1
                continue
            persistent_id, location = found
            db.execute(
                """UPDATE music_imports SET status='verified', persistent_id=?, reported_location=?,
                   error=NULL, verified_at=? WHERE plan_item_id=?""",
                (persistent_id, str(location), utc_now(), row["id"]),
            )
            verified += 1
        db.commit()
        return {"verified": verified, "unresolved": unresolved}
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    paths = load_paths()
    parser = argparse.ArgumentParser(
        description="Add verified published tracks to macOS Music using AppleScript."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    import_parser = sub.add_parser(
        "import", help="Add each published plan track once, then verify Music's reported location."
    )
    import_parser.add_argument("--plan", type=Path, required=True)
    import_parser.add_argument("--state", type=Path, default=paths.state / "pipeline.sqlite3")
    import_parser.add_argument("--library", type=Path, default=paths.library)
    import_parser.add_argument(
        "--confirm-music-import",
        action="store_true",
        help="Required acknowledgement before Apple Music is changed.",
    )
    reconcile_parser = sub.add_parser(
        "reconcile", help="Read-only check of a prior uncertain Music import."
    )
    reconcile_parser.add_argument("--plan", type=Path, required=True)
    reconcile_parser.add_argument("--state", type=Path, default=paths.state / "pipeline.sqlite3")
    reconcile_parser.add_argument("--library", type=Path, default=paths.library)
    args = parser.parse_args(argv)
    if args.command == "import" and not args.confirm_music_import:
        print("Safety check failed: Refusing Apple Music import without --confirm-music-import")
        return 2
    try:
        with writer_lock(args.state.parent):
            result = (
                import_plan(args.state, args.library, args.plan)
                if args.command == "import"
                else reconcile_plan(args.state, args.library, args.plan)
            )
        print(
            (
                "Music import complete: "
                if args.command == "import"
                else "Music reconciliation complete: "
            )
            + ", ".join(f"{key}={value}" for key, value in result.items())
        )
        return 0
    except (SafetyError, subprocess.TimeoutExpired) as exc:
        print(f"Music import stopped: {exc}")
        return 2
