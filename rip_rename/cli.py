"""Command-line interface: argparse, interactive prompts, preview/execute flow."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from . import ffprobe, scanner, rename, state


# ---------- small UI helpers ----------

def _prompt(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    resp = input(f"{question}{suffix}: ").strip()
    return resp if resp else default


def _confirm(question: str, default_yes: bool = True) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        resp = input(f"{question} {suffix} ").strip().lower()
    except EOFError:
        return False
    if not resp:
        return default_yes
    return resp.startswith("y")


def _format_duration(sec: Optional[float]) -> str:
    if sec is None:
        return "?:??"
    total = int(sec)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ---------- commands ----------

def cmd_rename(args: argparse.Namespace) -> int:
    try:
        ffprobe.ensure_available()
    except ffprobe.FFProbeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    directory = Path(args.path).expanduser().resolve()
    if not directory.exists():
        print(f"error: {directory} does not exist", file=sys.stderr)
        return 1
    if not directory.is_dir():
        print(f"error: {directory} is not a directory", file=sys.stderr)
        return 1

    print(f"Scanning {directory}...")
    try:
        files = scanner.scan(directory)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not files:
        print("No video files found.")
        return 1

    # Show what we found
    print(f"\nFound {len(files)} video file(s):")
    for f in files:
        marker = "! " if f.likely_extra else "  "
        print(f"  {marker}{f.info.path.name}  ({_format_duration(f.info.duration_sec)})")

    dupes = scanner.find_duplicate_runtimes(files)
    if dupes:
        print("\n[!] Files with near-identical runtimes (possible duplicate rips):")
        for a, b in dupes:
            print(f"     {a.info.path.name}  <->  {b.info.path.name}")

    extras = [f for f in files if f.likely_extra]
    if extras and not args.include_extras:
        print(
            f"\n{len(extras)} file(s) flagged as likely extras (short runtime); "
            "excluded from rename. Use --include-extras to include."
        )

    # Gather series / season / start-episode
    defaults = state.load_defaults()
    series = args.series or _prompt("Series", defaults.get("series", ""))
    if not series:
        print("error: series name required", file=sys.stderr)
        return 1

    if args.season is not None:
        season = args.season
    else:
        season_default = str(defaults.get("season", 1))
        try:
            season = int(_prompt("Season", season_default))
        except ValueError:
            print("error: season must be an integer", file=sys.stderr)
            return 1

    if args.start is not None:
        start = args.start
    else:
        try:
            start = int(_prompt("Starting episode", "1"))
        except ValueError:
            print("error: starting episode must be an integer", file=sys.stderr)
            return 1

    # Build the plan
    try:
        plan = rename.build_plan(
            files,
            series=series,
            season=season,
            start_episode=start,
            include_extras=args.include_extras,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not plan.items:
        print("No files to rename (all were excluded as extras?).")
        return 1

    # Preview
    print("\nPreview:")
    for item in plan.items:
        src_name = Path(item.src).name
        dst_name = Path(item.dst).name
        print(f"  {src_name}")
        print(f"    -> {dst_name}")
        for w in item.warnings:
            print(f"    [!] {w}")

    if plan.warnings:
        print("\nPlan warnings:")
        for w in plan.warnings:
            print(f"  [!] {w}")

    if args.dry_run:
        print("\nDry run — no changes made.")
        return 0

    if plan.has_warnings:
        print(
            "\nCannot proceed: plan has unresolved warnings. "
            "Resolve conflicts (rename or remove the blocking files) and try again.",
            file=sys.stderr,
        )
        return 1

    if not args.yes and not _confirm("\nProceed?", default_yes=True):
        print("Aborted.")
        return 0

    # Record history BEFORE executing — a crash mid-batch is still recoverable.
    state.record_execution(plan)
    state.save_defaults(series=series, season=season)

    succeeded, failed = rename.execute_plan(plan)

    print(f"\nRenamed {len(succeeded)} file(s).")
    if failed:
        item, err = failed[0]
        remaining = len(plan.items) - len(succeeded) - 1
        print(f"Stopped at {Path(item.src).name}: {err}", file=sys.stderr)
        if remaining > 0:
            print(f"{remaining} file(s) not processed.", file=sys.stderr)
        print("Run with `--undo` to reverse what did succeed.", file=sys.stderr)
        return 2

    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    last = state.pop_last_plan()
    if last is None:
        print("Nothing to undo.")
        return 0

    reverse = rename.reverse_plan(last)

    print(f"Undoing last rename ({last.series} S{last.season:02d}):")
    for item in reverse.items:
        src_name = Path(item.src).name
        dst_name = Path(item.dst).name
        print(f"  {src_name}")
        print(f"    -> {dst_name}")
        for w in item.warnings:
            print(f"    [!] {w}")

    if reverse.has_warnings:
        print(
            "\nCannot undo: reversing would collide with existing files. "
            "Resolve manually.",
            file=sys.stderr,
        )
        # Put the entry back on the stack so the user can try again after fixing.
        state.record_execution(last)
        return 1

    if not args.yes and not _confirm("\nProceed with undo?", default_yes=True):
        print("Aborted.")
        state.record_execution(last)  # push back onto history
        return 0

    succeeded, failed = rename.execute_plan(reverse)
    print(f"\nReverted {len(succeeded)} file(s).")
    if failed:
        item, err = failed[0]
        print(f"Stopped: {err}", file=sys.stderr)
        return 2
    return 0


# ---------- entrypoint ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rip-rename",
        description="Rename MakeMKV/HandBrake TV rips to Plex-compatible filenames.",
    )
    p.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Directory containing video files (default: current directory)",
    )
    p.add_argument("--series", help="Series name (skips prompt)")
    p.add_argument("--season", type=int, help="Season number (skips prompt)")
    p.add_argument("--start", type=int, help="Starting episode number (skips prompt)")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen; make no changes.",
    )
    p.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompt (assume yes).",
    )
    p.add_argument(
        "--include-extras",
        action="store_true",
        help="Include files flagged as likely extras (short runtime).",
    )
    p.add_argument(
        "--undo",
        action="store_true",
        help="Reverse the most recent rename operation.",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.undo:
            return cmd_undo(args)
        return cmd_rename(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
