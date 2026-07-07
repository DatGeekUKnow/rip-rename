"""Command-line interface: argparse, interactive prompts, TMDb lookup, preview/execute."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from . import ffprobe, scanner, rename, state, tmdb


# ---------- small UI helpers ----------

def _prompt(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        resp = input(f"{question}{suffix}: ").strip()
    except EOFError:
        return default
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


# ---------- TMDb integration ----------

def _resolve_show_id(series: str, api_key: str) -> Optional[int]:
    """Look up a show ID, using cache when possible. Prompts on ambiguous matches."""
    cached = state.get_cached_show_id(series)
    if cached is not None:
        return cached

    try:
        matches = tmdb.search_tv(series, api_key, limit=5)
    except tmdb.TMDbAuthError as e:
        print(f"TMDb auth error: {e}", file=sys.stderr)
        return None
    except tmdb.TMDbError as e:
        print(f"TMDb lookup failed: {e}", file=sys.stderr)
        return None

    if not matches:
        print(f"No TMDb matches for '{series}'.")
        return None

    if len(matches) == 1:
        m = matches[0]
        print(f"TMDb match: {m.name} ({m.year})")
        state.cache_show_id(series, m.id)
        return m.id

    print(f"\nTMDb matches for '{series}':")
    for i, m in enumerate(matches, start=1):
        print(f"  {i}. {m.name} ({m.year})")
    print(f"  0. None of these (skip title lookup)")

    while True:
        raw = _prompt("Choose", "1")
        try:
            choice = int(raw)
        except ValueError:
            print("Enter a number.")
            continue
        if choice == 0:
            return None
        if 1 <= choice <= len(matches):
            picked = matches[choice - 1]
            state.cache_show_id(series, picked.id)
            return picked.id
        print(f"Enter a number between 0 and {len(matches)}.")


def _fetch_season(show_id: int, season: int, api_key: str) -> Optional[dict[int, tmdb.EpisodeInfo]]:
    """Get episode data (titles + runtimes), from cache or TMDb. None on failure."""
    cached = state.get_cached_season(show_id, season)
    if cached is not None:
        return cached
    try:
        episodes = tmdb.get_tv_season_episodes(show_id, season, api_key)
    except tmdb.TMDbError as e:
        print(f"TMDb season lookup failed: {e}", file=sys.stderr)
        return None
    state.cache_season(show_id, season, episodes)
    return episodes


def _median_runtime_sec(episode_info: dict[int, tmdb.EpisodeInfo]) -> Optional[float]:
    """Extract the median episode runtime (in seconds) from TMDb data, if any."""
    runtimes = [
        info.runtime_min * 60 for info in episode_info.values()
        if info.runtime_min is not None and info.runtime_min > 0
    ]
    if not runtimes:
        return None
    s = sorted(runtimes)
    n = len(s)
    return s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2


# ---------- commands ----------

def cmd_rename(args: argparse.Namespace) -> int:
    try:
        ffprobe.ensure_available()
    except ffprobe.FFProbeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.tmdb_key:
        state.save_config(tmdb_api_key=args.tmdb_key.strip())
        print(f"Saved TMDb API key to {state.config_dir() / 'config.json'}")

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

    print(f"\nFound {len(files)} video file(s):")
    for f in files:
        print(f"    {f.info.path.name}  ({_format_duration(f.info.duration_sec)})")

    dupes = scanner.find_duplicate_runtimes(files)
    if dupes:
        print("\n[!] Files with near-identical runtimes (possible duplicate rips):")
        for a, b in dupes:
            print(f"     {a.info.path.name}  <->  {b.info.path.name}")

    # Series / season / start-episode
    defaults = state.load_defaults()
    series = args.series or _prompt("\nSeries", defaults.get("series", ""))
    if not series:
        print("error: series name required", file=sys.stderr)
        return 1

    if args.season is not None:
        season = args.season
    else:
        try:
            season = int(_prompt("Season", str(defaults.get("season", 1))))
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

    # TMDb title/runtime lookup (with graceful degradation)
    episode_info: Optional[dict[int, tmdb.EpisodeInfo]] = None
    titles: Optional[dict[int, str]] = None

    if args.no_titles:
        print("\nSkipping TMDb lookup (--no-titles).")
    else:
        api_key = state.get_tmdb_api_key(args.tmdb_key)
        if not api_key:
            print(
                "\nNo TMDb API key configured; renaming without episode titles.\n"
                "  To enable titles: get a free key at https://www.themoviedb.org/settings/api\n"
                "  Then run: rip-rename --tmdb-key <YOUR_KEY> (saves it) or set TMDB_API_KEY.\n"
                "  To silence this message, use --no-titles."
            )
        else:
            print("\nLooking up episode data on TMDb...")
            show_id = _resolve_show_id(series, api_key)
            if show_id is not None:
                episode_info = _fetch_season(show_id, season, api_key)
                if episode_info:
                    titles = {num: info.title for num, info in episode_info.items() if info.title}
                    if not titles:
                        titles = None

    # Classify extras using TMDb runtime if available, else file median.
    expected_sec: Optional[float] = None
    if episode_info:
        expected_sec = _median_runtime_sec(episode_info)

    classification = scanner.refine_classification(files, expected_duration_sec=expected_sec)

    print("\nClassification:")
    if classification.reference_source == "tmdb":
        print(
            f"  Reference episode length: ~{_format_duration(classification.reference_duration_sec)} "
            f"(from TMDb)"
        )
    elif classification.reference_source == "file_median":
        print(
            f"  Reference episode length: ~{_format_duration(classification.reference_duration_sec)} "
            f"(median of scanned files — TMDb runtime unavailable)"
        )
    else:
        print("  No reference duration available; using absolute floor only.")
    print(f"  Minimum episode duration: {_format_duration(classification.threshold_sec)}")
    print(
        f"  {classification.episode_count} file(s) classified as episodes, "
        f"{classification.extra_count} as extras."
    )

    extras = [f for f in files if f.likely_extra]
    if extras and not args.include_extras:
        print("\nExtras (excluded — pass --include-extras to include):")
        for f in extras:
            print(f"    {f.info.path.name}  ({_format_duration(f.info.duration_sec)})")

    # Build plan
    try:
        plan = rename.build_plan(
            files,
            series=series,
            season=season,
            start_episode=start,
            titles=titles,
            include_extras=args.include_extras,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not plan.items:
        print("No files to rename.")
        return 1

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
            "Resolve conflicts and try again.",
            file=sys.stderr,
        )
        return 1

    if not args.yes and not _confirm("\nProceed?", default_yes=True):
        print("Aborted.")
        return 0

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
        state.record_execution(last)
        return 1

    if not args.yes and not _confirm("\nProceed with undo?", default_yes=True):
        print("Aborted.")
        state.record_execution(last)
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
    p.add_argument("path", nargs="?", default=".",
                   help="Directory containing video files (default: current directory)")
    p.add_argument("--series", help="Series name (skips prompt)")
    p.add_argument("--season", type=int, help="Season number (skips prompt)")
    p.add_argument("--start", type=int, help="Starting episode number (skips prompt)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would happen; make no changes.")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Skip confirmation prompt (assume yes).")
    p.add_argument("--include-extras", action="store_true",
                   help="Include files classified as extras.")
    p.add_argument("--undo", action="store_true",
                   help="Reverse the most recent rename operation.")
    p.add_argument("--no-titles", action="store_true",
                   help="Skip TMDb episode-title lookup for this run.")
    p.add_argument("--tmdb-key", metavar="KEY",
                   help="Set/override the TMDb API key (also saved to config).")
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
