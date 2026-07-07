# rip-rename (V1)

Renames MakeMKV/HandBrake TV rips (`t00.mkv`, `title01.mkv`, ...) to Plex-compatible filenames.

**V1 scope:** rock-solid renaming engine. No network, no TMDb, no runtime heuristics — those come in V2/V3 on top of this foundation.

## Requirements

- Python 3.10+
- `ffprobe` on PATH (`apt install ffmpeg`)

No PyPI dependencies.

## Install

Just drop the `rip_rename/` directory anywhere and run it as a module. For a
convenient global command, add a shim:

```bash
# From wherever you put the code:
cat > ~/.local/bin/rip-rename <<'EOF'
#!/usr/bin/env bash
exec python3 -m rip_rename "$@"
EOF
chmod +x ~/.local/bin/rip-rename
```

Make sure the parent directory of `rip_rename/` is on `PYTHONPATH`, or install
via `pip install -e .` later once you add a `pyproject.toml`.

## Usage

Interactive (from inside a folder of rips):

```bash
rip-rename
```

With arguments:

```bash
rip-rename /media/handbrake/The\ Magicians --series "The Magicians" --season 2 --start 5
```

Preview only:

```bash
rip-rename --dry-run
```

Skip confirmation:

```bash
rip-rename --yes
```

Undo the most recent rename:

```bash
rip-rename --undo
```

Include files flagged as likely extras (short runtime):

```bash
rip-rename --include-extras
```

## Example session

```
$ rip-rename /media/handbrake/The\ Magicians
Scanning /media/handbrake/The Magicians...

Found 3 video file(s):
   t00.mkv  (43:12)
   t01.mkv  (43:01)
   t02.mkv  (42:57)

Series [The Magicians]:
Season [2]:
Starting episode [1]: 5

Preview:
  t00.mkv
    -> The Magicians - S02E05.mkv
  t01.mkv
    -> The Magicians - S02E06.mkv
  t02.mkv
    -> The Magicians - S02E07.mkv

Proceed? [Y/n]

Renamed 3 file(s).
```

## What V1 does for you

- Discovers `.mkv`/`.mp4`/`.m4v` files, sorted lexicographically (= disc
  order = episode order, 90% of the time).
- Runs `ffprobe` on each file for duration, resolution, and track counts.
- Flags files shorter than 5 minutes as likely extras (excluded by default).
- Flags pairs of files with near-identical runtimes as possible duplicate
  rips (MakeMKV frequently rips commentary tracks as separate titles).
- Refuses to overwrite existing files.
- Detects internal collisions in the rename plan (two sources -> same name).
- Remembers the last-used series and season so re-running takes one keypress.
- Records every executed plan to `~/.local/share/rip-rename/history.json` for
  undo (keeps the last 10 operations).

## What V1 does NOT do

- No TMDb integration (episode titles). Filenames are `Series - S02E05.mkv`,
  not `Series - S02E05 - Cheat Day.mkv`.
- No runtime-based episode matching.
- No auto-detection of the newest HandBrake output folder (`rename-last`).
- No ARM hook.

These are the V2-V5 layers. The plan structure in `rename.py` is designed so
they can plug in without touching the safety guarantees below.

## Design invariants (don't break these)

1. **`RenamePlan` is the single source of truth.** Preview, dry-run, execute,
   and undo all consume the same object.
2. **History is written BEFORE renames execute.** A crash mid-batch leaves a
   recoverable log.
3. **Execution stops on the first failure.** No mystery half-renamed batches.
4. **Warnings block execution.** No `--force` in V1 — resolve conflicts
   manually so the safety net stays intact.
5. **Runtime analysis (when it lands in V3) reports confidence; it does not
   silently reorder.** Silent "helpful" automation on filenames is how
   libraries get corrupted.

## Module layout

```
rip_rename/
├── __init__.py
├── __main__.py     # python -m rip_rename
├── cli.py          # argparse, prompts, preview/execute flow
├── ffprobe.py      # subprocess wrapper around ffprobe
├── scanner.py      # directory scan + duplicate detection
├── rename.py       # RenamePlan: build, execute, reverse
└── state.py        # JSON persistence (history stack + defaults)
```

## Where the extension points are

- **TMDb (V2):** new `metadata.py` module that takes `(series, season)` and
  returns `{episode_num: title}`. Modify `build_plan()` to accept an
  optional `titles` dict and change `DEFAULT_TEMPLATE` to include `{title}`.
- **Runtime matching (V3):** compare `ScannedFile.info.duration_sec` against
  TMDb-reported runtimes and attach a `confidence_score` to `RenameItem`.
  Present as a warning if low; never auto-reorder.
- **`rename-last` (V4):** small function in `cli.py` that finds the newest
  `mtime` subdir of a configured `default_media_path`, excluding dirs modified
  within the last N minutes (still-encoding).
- **ARM hook (V5):** ARM invokes `rip-rename --yes <path>` post-encode. All
  the safety guarantees still apply.

## Testing

Not included yet — I'd recommend `pytest` with a temp-directory fixture that
seeds fake `.mkv` files (empty is fine; ffprobe will fail, and the scanner
already handles that path). Test targets:

- `sanitize_for_filename` edge cases
- `build_plan` collision detection
- `execute_plan` refuses on warnings
- `reverse_plan` produces a valid inverse
- `state.record_execution` / `pop_last_plan` round-trip
