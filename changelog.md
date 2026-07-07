# Changelog

## V2.2 — Runtime verification & multi-episode handling
- Added `matcher.py`: matches files to TMDb episodes by runtime, filename
  order remains primary.
- Auto-handles combined-episode files (1 file = 2 TMDb episodes, e.g. Avatar
  Blu-ray two-parters) → `SxxEyy-Ezz - Title`.
- Detects split-episode files (2 files = 1 TMDb episode, e.g. Office
  two-parters) → blocks the batch, no auto-rename (no established naming
  convention yet).
- Detects past-last-episode files (episode-like runtime beyond season's
  known episode count) → blocks the batch.
- Runtime mismatches skip just that file; rest of batch still processes.
- Missing episodes (on TMDb, absent from disc) reported, non-blocking.
- Falls back to filename-order-only assignment when ffprobe or TMDb data is
  unavailable, with an explicit warning before confirmation.
- `rename.py`: `build_plan()` now takes `MatchAssignment` list instead of
  raw files + start episode; added combined-episode filename templates.
- `cli.py`: new per-file analysis table (filename, duration, assignment,
  status) shown before every confirmation.

## V2.1 — Smart extras classification
- Extras threshold changed from a flat 5-minute floor to
  `max(5min, expected_episode_length × 0.5)`, fixing false negatives on
  shows with short episodes (e.g. 11-min extra on a 22-min sitcom).
- Expected episode length sourced from TMDb season data when available,
  else median of scanned file durations.
- `scanner.py`: `scan()` no longer auto-classifies; new
  `refine_classification()` does it separately and reports which reference
  source was used.
- `tmdb.py`: `EpisodeInfo` now carries `runtime_min`.
- `state.py`: cache format extended (reads old-format caches gracefully).

## V2 — TMDb episode titles
- Added `tmdb.py`: stdlib-only TMDb API client (show search, season/episode
  lookup).
- Filenames now include episode titles: `Series - S02E05 - Title.mkv`.
- TMDb API key via `--tmdb-key`, `TMDB_API_KEY` env var, or saved config
  (`~/.config/rip-rename/config.json`).
- Added local cache (`~/.local/share/rip-rename/cache.json`) to avoid
  repeat API calls.
- Show-match disambiguation prompt when TMDb search returns multiple hits.

## V1 — Core renamer
- Initial release. Discovers `.mkv`/`.mp4`/`.m4v` files in filename order,
  renames to `Series - S02E05.mkv` using series/season/start-episode input.
- `ffprobe` integration for duration, resolution, track counts.
- Duplicate-runtime detection (commentary tracks ripped as separate titles).
- Dry-run mode, confirmation prompt, `--yes` flag.
- Collision detection (won't overwrite existing files; detects internal
  plan collisions).
- Undo support: last 10 operations recorded to
  `~/.local/share/rename-tv/history.json`.
