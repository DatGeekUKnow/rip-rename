# rip-rename (V2.2)

Renames MakeMKV/HandBrake TV rips (`t00.mkv`, `title01.mkv`, ...) to Plex-compatible
filenames, with optional TMDb episode titles and runtime-based verification.

## Requirements

- Python 3.10+
- `ffprobe` on PATH (`apt install ffmpeg`)
- (Optional) Free TMDb API key for episode titles: https://www.themoviedb.org/settings/api

No PyPI dependencies.

## Install

Drop the `rip_rename/` directory anywhere and run it as a module, or add a shim:

```bash
cat > ~/.local/bin/rip-rename <<'EOF'
#!/usr/bin/env bash
exec python3 -m rip_rename "$@"
EOF
chmod +x ~/.local/bin/rip-rename
```

Make sure the parent directory of `rip_rename/` is on `PYTHONPATH`.

## Usage

```bash
rip-rename                                          # interactive, current dir
rip-rename /media/handbrake/The\ Magicians          # interactive, given dir
rip-rename --series "The Magicians" --season 2 --start 5
rip-rename --dry-run                                # preview only
rip-rename --yes                                    # skip confirmation
rip-rename --undo                                   # reverse last rename
rip-rename --include-extras                         # don't exclude likely extras
rip-rename --no-titles                              # skip TMDb lookup
rip-rename --tmdb-key YOUR_KEY                       # set/save TMDb key
```

TMDb key precedence: `--tmdb-key` flag > `TMDB_API_KEY` env var > saved config
(`~/.config/rip-rename/config.json`).

## Example session (V2.2, with a combined episode)

```
$ rip-rename /media/handbrake/Avatar
Scanning /media/handbrake/Avatar...

Found 3 video file(s):
    t18.mkv  (22:05)
    t19.mkv  (44:12)
    t20.mkv  (22:08)

Series [Avatar: The Last Airbender]:
Season [2]:
Starting episode [1]: 19

Looking up episode data on TMDb...

Classifier:
  Reference episode length: ~22:00 (from TMDb)
  Minimum episode duration: 5:00

Analysis:
  t18.mkv  22:05  ->  S02E19        [MATCH]
  t19.mkv  44:12  ->  S02E20-E21    [COMBINED]
  t20.mkv  22:08  ->  S02E22        [MATCH]

Summary: 3 confident rename(s), 0 excluded, 0 missing episode(s).

Preview:
  t18.mkv
    -> Avatar_ The Last Airbender - S02E19 - The Guru.mkv
  t19.mkv
    -> Avatar_ The Last Airbender - S02E20-E21 - The Crossroads of Destiny.mkv
  t20.mkv
    -> Avatar_ The Last Airbender - S02E22 - The Headband.mkv

Proceed? [Y/n]

Renamed 3 file(s).
```

## What it does for you

**Core (V1):**
- Discovers `.mkv`/`.mp4`/`.m4v` files, sorted lexicographically (disc order).
- Runs `ffprobe` for duration, resolution, track counts.
- Refuses to overwrite existing files; detects internal plan collisions.
- Remembers last-used series/season. Records every executed plan for undo
  (`~/.local/share/rip-rename/history.json`, last 10 kept).

**TMDb titles (V2):**
- Looks up show + season on TMDb, caches results
  (`~/.local/share/rip-rename/cache.json`), prompts to disambiguate multiple
  show matches.
- Filenames become `Series - S02E05 - Episode Title.mkv`.

**Smart extras classification (V2.1):**
- Extras threshold is `max(5min, expected_episode_length × 0.5)` instead of a
  flat 5-minute floor, so short episodes (e.g. 22-min sitcoms) don't
  misclassify an 11-min extra as a real episode.
- Expected episode length comes from TMDb when available, else the median of
  scanned file durations.

**Runtime verification & multi-episode handling (V2.2):**
- Filename order is still primary — TMDb runtime is used to *verify*, not to
  reorder.
- **Match:** file duration ≈ TMDb runtime (tolerance `max(60s, 3%)`) → rename.
- **Combined episode** (1 file covers 2 TMDb episodes, e.g. Avatar
  S02E19-E20 Blu-ray releases): auto-detected by summed runtime, renamed as
  `SxxEyy-Ezz - Title` using the first episode's title.
- **Split episode** (2 files sum to 1 TMDb episode, e.g. Office two-parters
  ripped as separate titles): detected but **blocks the whole batch** —
  cascade risk if guessed wrong. Rename manually, then rerun.
- **Past-last-episode:** files beyond the season's episode count with
  episode-like runtime **blocks the batch** (usually wrong season, or TMDb
  out of date).
- **Runtime mismatch:** a file that doesn't match its expected episode (or
  any episode) is skipped, not renamed; everything else keeps processing.
- **Missing episodes** (on TMDb, no matching disc file): reported, doesn't
  block.
- **No ffprobe data / no TMDb runtime / no TMDb at all:** falls back to
  filename-order assignment, always warns before you confirm.
- Per-file analysis table shown before every rename, e.g.
  `t19.mkv  44:12  ->  S02E20-E21  [COMBINED]`.

## What it does NOT do yet

- No auto-handling of split episodes (Case B) — no established naming
  convention yet, intentionally left manual.
- No auto-detection of the newest HandBrake output folder (`rename-last`).
- No ARM (Automatic Ripping Machine) hook.
- No movie support.

## Design invariants (don't break these)

1. **`RenamePlan` is the single source of truth.** Preview, dry-run, execute,
   and undo all consume the same object.
2. **History is written BEFORE renames execute.**
3. **Execution stops on the first failure.** No mystery half-renamed batches.
4. **Warnings block execution.** No `--force` — resolve conflicts manually.
5. **Filename order is primary; runtime is verification only.** Ambiguous or
   risky cases (splits, past-last-episode) block rather than guess.

## Module layout

```
rip_rename/
├── __init__.py
├── __main__.py     # python -m rip_rename
├── cli.py          # argparse, prompts, TMDb orchestration, preview/execute
├── ffprobe.py      # subprocess wrapper around ffprobe
├── scanner.py      # directory scan, duplicate detection, extras classification
├── matcher.py      # runtime-verified file <-> episode matching (V2.2)
├── rename.py       # RenamePlan: build, execute, reverse
├── tmdb.py         # stdlib TMDb API client
└── state.py        # JSON persistence (history, config, cache, defaults)
```

## Where the remaining extension points are

- **Split-episode auto-handling:** once a naming convention is settled,
  extend `matcher.py`'s Case B branch to emit a `MatchAssignment` pair
  instead of an `Exclusion` pair, and add a template in `rename.py`.
- **`rename-last`:** small function in `cli.py` that finds the newest `mtime`
  subdir of a configured `default_media_path`, excluding dirs modified in
  the last N minutes (still-encoding).
- **ARM hook:** ARM invokes `rip-rename --yes <path>` post-encode. All
  safety guarantees still apply.

## Testing

No formal test suite yet. V2.2's matcher was smoke-tested against 7
scenarios (happy path, combined episode, split episode, past-last-episode,
mid-batch mismatch, missing episodes, no-TMDb fallback) — see git history /
dev notes. Recommend `pytest` with a temp-dir fixture next; priority targets:

- `matcher.match()` against the 7 scenarios above, as real test cases
- `sanitize_for_filename` edge cases
- `build_plan` collision detection
- `execute_plan` refuses on warnings
- `reverse_plan` produces a valid inverse
- `state.record_execution` / `pop_last_plan` round-trip
