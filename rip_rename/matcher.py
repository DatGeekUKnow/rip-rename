"""Match ripped files to TMDb episodes using runtime data.

The approach is filename order-primary: we assume `file[i]` corresponds to
`start_episode + i`, and use TMDb's per-episode runtime to verify. Files that
verify get renamed; files that don't are left alone and reported to the user.

Special cases we detect during the walk:
  - COMBINED (1 file -> 2 episodes): auto-handled, rename as SxxEyy-Ezz.
    Common on Blu-ray rips where the disc has one file for what TMDb lists
    as two consecutive short episodes (e.g., Avatar S02E19-E20).
  - SPLIT (2 files -> 1 episode): detected, but blocks the batch. Renaming
    would misalign every file after it and Plex has no single canonical
    naming convention for split parts, so we require manual resolution.
  - PAST-LAST-EPISODE: files beyond the season's episode count with
    episode-like runtimes. Suggests something structurally wrong
    (wrong season? TMDb out of date?). Block the batch.
  - EXTRA: file's runtime matches no episode AND is clearly short
    (well under half an episode's length). Silently excluded, not blocking.
  - AMBIGUOUS_RUNTIME: file's runtime matches no episode, but is close
    enough to episode length to plausibly BE one — TMDb runtime data may
    be missing/wrong, or it's an unusually long extra. We can't tell which,
    so this blocks the batch rather than guessing "extra" and silently
    misaligning every file after it.
  - RUNTIME_MISMATCH: file matches SOME episode in the season but not the
    one at its position. Skip that file only; keep processing the rest.
  - ASSUMED: ffprobe couldn't read the file OR TMDb has no runtime for the
    episode. Assign by filename order, warn the user before confirming.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .scanner import ScannedFile
from .tmdb import EpisodeInfo


# Tolerance for a single-episode runtime match: max(60s, 3% of expected).
# 3% of a 43-min episode = ~77s; 3% of a 22-min episode = ~40s (floored at 60s).
# Broadcast episodes often vary by 30-60s from TMDb's rounded-minute value, so
# this tolerance absorbs that noise without being loose enough to confuse
# adjacent episodes (which are usually within seconds of each other anyway —
# runtime distinguishes episode-vs-extra, not episode-vs-episode).
TOLERANCE_MIN_SEC = 60.0
TOLERANCE_PCT = 0.03

# When a file matches NO episode's runtime, we still need to decide whether
# it's a confident, silent "extra" or a case ambiguous enough to block on.
# If its duration is at or above this fraction of the season's median known
# runtime, it's plausibly episode-length -- and "doesn't match any known
# runtime" then means either TMDb's data is wrong/missing for that episode,
# or it's a genuinely long extra. We can't tell which, so we block instead
# of silently excluding it (silent exclusion here does NOT advance the
# episode pointer, which -- if this was actually a real episode -- shifts
# every subsequent file's assignment down by one for the rest of the batch).
# Below this ratio, it's confidently just a short extra: safe to exclude
# without blocking. Matches the ratio scanner.py uses for the same reason.
AMBIGUOUS_LENGTH_RATIO = 0.5


def runtime_matches(
    actual_sec: Optional[float],
    expected_sec: Optional[float],
    is_sum: bool = False,
) -> bool:
    """Return True if `actual_sec` is within tolerance of `expected_sec`.

    When `is_sum=True` (comparing a sum of two runtimes against a single value,
    or vice versa), tolerances are doubled since errors accumulate.
    """
    if actual_sec is None or expected_sec is None:
        return False
    if actual_sec <= 0 or expected_sec <= 0:
        return False
    tol_min = TOLERANCE_MIN_SEC * (2 if is_sum else 1)
    tol_pct = TOLERANCE_PCT * (2 if is_sum else 1)
    tolerance = max(tol_min, expected_sec * tol_pct)
    return abs(actual_sec - expected_sec) <= tolerance


@dataclass
class MatchAssignment:
    """A file with a confirmed episode assignment; will be included in the rename plan."""
    file: ScannedFile
    episode_numbers: list[int]     # [n] for single, [n, n+1] for combined
    kind: str                      # "match", "combined", "assumed_no_ffprobe",
                                   # "assumed_no_tmdb_runtime", "assumed_no_tmdb"


@dataclass
class Exclusion:
    """A file that will NOT be renamed, with a reason."""
    file: ScannedFile
    kind: str                      # "extra", "runtime_mismatch",
                                   # "split_episode_pt1", "split_episode_pt2",
                                   # "past_last_episode"
    reason: str


@dataclass
class MatchReport:
    matches: list[MatchAssignment] = field(default_factory=list)
    exclusions: list[Exclusion] = field(default_factory=list)
    missing_episodes: list[EpisodeInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)   # informational
    block_reasons: list[str] = field(default_factory=list)  # if any, don't rename

    @property
    def blocked(self) -> bool:
        return bool(self.block_reasons)

    @property
    def files_to_rename(self) -> int:
        return len(self.matches)

    @property
    def episodes_covered(self) -> int:
        return sum(len(m.episode_numbers) for m in self.matches)


def _match_core(
    files: list[ScannedFile],
    season_episodes: dict[int, EpisodeInfo],
    start_episode: int,
) -> MatchReport:
    """Walk files against expected episodes, producing a MatchReport.

    `files` should already have obvious extras filtered out (via
    scanner.refine_classification with likely_extra=True files removed) and
    any manually-pinned files removed (see `match()`, the public entry point).
    The matcher does additional runtime-based filtering on top.
    """
    report = MatchReport()

    # Build the ordered list of episodes to walk against, starting at start_episode.
    ep_list = sorted(
        [e for e in season_episodes.values() if e.number >= start_episode],
        key=lambda e: e.number,
    )
    if not ep_list:
        # start_episode is past all known episodes — nothing we can do.
        report.warnings.append(
            f"No episodes at or after E{start_episode:02d} in TMDb data for this season."
        )
        return report

    # Cache: does any episode in the whole season match this duration?
    def matches_any_episode(duration_sec: Optional[float]) -> bool:
        if duration_sec is None:
            return False
        return any(
            e.runtime_min is not None
            and runtime_matches(duration_sec, e.runtime_min * 60)
            for e in season_episodes.values()
        )

    max_ep_num = max(e.number for e in season_episodes.values())

    # Reference for the "is this plausibly episode-length?" check below.
    known_runtimes_sec = sorted(
        e.runtime_min * 60 for e in season_episodes.values()
        if e.runtime_min is not None and e.runtime_min > 0
    )
    season_reference_sec: Optional[float] = None
    if known_runtimes_sec:
        n = len(known_runtimes_sec)
        season_reference_sec = (
            known_runtimes_sec[n // 2] if n % 2 == 1
            else (known_runtimes_sec[n // 2 - 1] + known_runtimes_sec[n // 2]) / 2
        )

    file_idx = 0
    ep_idx = 0

    while file_idx < len(files):
        f = files[file_idx]

        # Past the last episode? Categorize and continue.
        if ep_idx >= len(ep_list):
            if matches_any_episode(f.info.duration_sec):
                report.exclusions.append(Exclusion(
                    file=f,
                    kind="past_last_episode",
                    reason=f"runtime matches an episode but season only has {max_ep_num} episodes",
                ))
                report.block_reasons.append(
                    f"{f.info.path.name} ({_fmt(f.info.duration_sec)}) has episode-like "
                    f"runtime but the season only has {max_ep_num} episodes on TMDb"
                )
            else:
                report.exclusions.append(Exclusion(
                    file=f,
                    kind="extra",
                    reason="past last episode; runtime doesn't match any episode",
                ))
            file_idx += 1
            continue

        ep = ep_list[ep_idx]
        next_ep = ep_list[ep_idx + 1] if ep_idx + 1 < len(ep_list) else None

        # Missing data → assign by order, warn before confirm.
        if f.info.duration_sec is None:
            report.matches.append(MatchAssignment(
                file=f, episode_numbers=[ep.number], kind="assumed_no_ffprobe",
            ))
            report.warnings.append(
                f"{f.info.path.name}: ffprobe couldn't read this file — "
                f"assigning to E{ep.number:02d} by filename order only "
                f"(no runtime verification)"
            )
            file_idx += 1
            ep_idx += 1
            continue

        if ep.runtime_min is None:
            report.matches.append(MatchAssignment(
                file=f, episode_numbers=[ep.number], kind="assumed_no_tmdb_runtime",
            ))
            report.warnings.append(
                f"{f.info.path.name}: TMDb has no runtime for E{ep.number:02d} — "
                f"assigning by filename order only (no runtime verification)"
            )
            file_idx += 1
            ep_idx += 1
            continue

        ep_runtime_sec = ep.runtime_min * 60

        # Case A: straight 1:1 match. Preferred.
        if runtime_matches(f.info.duration_sec, ep_runtime_sec):
            report.matches.append(MatchAssignment(
                file=f, episode_numbers=[ep.number], kind="match",
            ))
            file_idx += 1
            ep_idx += 1
            continue

        # Case C: this one file covers two episodes.
        if next_ep and next_ep.runtime_min is not None:
            combined_expected = (ep.runtime_min + next_ep.runtime_min) * 60
            if runtime_matches(f.info.duration_sec, combined_expected, is_sum=True):
                report.matches.append(MatchAssignment(
                    file=f,
                    episode_numbers=[ep.number, next_ep.number],
                    kind="combined",
                ))
                file_idx += 1
                ep_idx += 2
                continue

        # Case B: this file plus the next together cover one episode.
        # Detected but blocks the batch — cascade risk if we rename around it.
        if file_idx + 1 < len(files):
            next_f = files[file_idx + 1]
            if next_f.info.duration_sec is not None:
                sum_actual = f.info.duration_sec + next_f.info.duration_sec
                if runtime_matches(sum_actual, ep_runtime_sec, is_sum=True):
                    report.exclusions.append(Exclusion(
                        file=f, kind="split_episode_pt1",
                        reason=f"possibly part 1 of E{ep.number:02d}",
                    ))
                    report.exclusions.append(Exclusion(
                        file=next_f, kind="split_episode_pt2",
                        reason=f"possibly part 2 of E{ep.number:02d}",
                    ))
                    report.block_reasons.append(
                        f"Possible split episode: {f.info.path.name} "
                        f"({_fmt(f.info.duration_sec)}) + {next_f.info.path.name} "
                        f"({_fmt(next_f.info.duration_sec)}) = {_fmt(sum_actual)}, "
                        f"which matches E{ep.number:02d} (~{_fmt(ep_runtime_sec)}). "
                        f"Rename these two files manually before rerunning."
                    )
                    file_idx += 2
                    ep_idx += 1
                    continue

        # No structural match found. Two possibilities:
        #   (a) Extra with episode-like duration that slipped past the classifier
        #       — matches no episode in the season at all.
        #   (b) A real episode whose runtime is unusual — matches SOME episode
        #       in the season but not the one at this position.
        # Under filename-order-primary, we skip this file (don't rename) and
        # advance the episode pointer so downstream files stay aligned to the
        # expected sequence.
        if not matches_any_episode(f.info.duration_sec):
            if (
                season_reference_sec is not None
                and f.info.duration_sec >= season_reference_sec * AMBIGUOUS_LENGTH_RATIO
            ):
                # Plausibly episode-length but matches no known runtime.
                # Could be a real episode with missing/wrong TMDb runtime
                # data, or an unusually long extra -- can't tell which, so
                # block rather than silently guessing "extra" (which would
                # leave the episode pointer unadvanced and shift every
                # subsequent file's assignment down by one).
                report.exclusions.append(Exclusion(
                    file=f, kind="ambiguous_runtime",
                    reason=(
                        f"runtime {_fmt(f.info.duration_sec)} matches no known "
                        f"episode, but is close to episode length "
                        f"(~{_fmt(season_reference_sec)} season median)"
                    ),
                ))
                report.block_reasons.append(
                    f"{f.info.path.name} ({_fmt(f.info.duration_sec)}) doesn't match "
                    f"any known episode runtime, but is long enough to plausibly BE "
                    f"one -- currently expected around E{ep.number:02d}. This could "
                    f"mean TMDb's runtime data is missing/wrong for that episode, or "
                    f"this file is genuinely bonus content of unusual length. Verify "
                    f"manually, then either move/rename the file out of the way or "
                    f"confirm it belongs, and rerun."
                )
                file_idx += 1
                # Don't advance ep_idx -- if this really is bonus content,
                # the current episode still needs a real match from a later file.
                continue
            report.exclusions.append(Exclusion(
                file=f, kind="extra",
                reason=f"runtime {_fmt(f.info.duration_sec)} doesn't match "
                       f"any episode in this season",
            ))
            file_idx += 1
            # Don't advance episode — this file is noise; give the current
            # episode to the next file to try.
            continue

        # Case (b): runtime mismatch. Skip this file, keep sequence aligned.
        report.exclusions.append(Exclusion(
            file=f, kind="runtime_mismatch",
            reason=f"runtime {_fmt(f.info.duration_sec)} doesn't match "
                   f"E{ep.number:02d} (expected ~{_fmt(ep_runtime_sec)})",
        ))
        report.warnings.append(
            f"{f.info.path.name}: runtime doesn't match expected E{ep.number:02d}. "
            f"Skipping — investigate manually."
        )
        file_idx += 1
        ep_idx += 1

    # Any episodes left over after we've consumed all files → missing from disc.
    report.missing_episodes = ep_list[ep_idx:]
    return report


def match(
    files: list[ScannedFile],
    season_episodes: dict[int, EpisodeInfo],
    start_episode: int,
    manual_overrides: Optional[dict[str, int]] = None,
) -> MatchReport:
    """Public entry point. Wraps `_match_core`, handling manual pins first.

    `manual_overrides` maps a file's basename (ScannedFile.info.path.name) to
    an episode number the user has independently verified. This exists for
    releases where disc authoring breaks the sequential-order assumption
    (e.g. a season where one episode's only file is an "extended cut" whose
    real runtime doesn't match TMDb's listed runtime, physically placed out
    of sequence on a different disc) — a situation runtime tolerance cannot
    resolve on its own, no matter how it's tuned, because the ambiguity is
    about disc authoring, not duration.

    Approach: pull pinned files and their target episode numbers OUT of the
    walk entirely (both directions — a pin can point at a higher or lower
    episode number than its file's position would suggest), run the normal
    algorithm on whatever's left, then merge the pins back in as `kind="manual"`
    matches. This sidesteps needing the walk itself to support jumping
    backward/forward across episode numbers out of order.
    """
    manual_overrides = manual_overrides or {}
    if not manual_overrides:
        return _match_core(files, season_episodes, start_episode)

    manual_matches: list[MatchAssignment] = []
    remaining_files: list[ScannedFile] = []
    remaining_episodes = dict(season_episodes)

    for f in files:
        target = manual_overrides.get(f.info.path.name)
        if target is None:
            remaining_files.append(f)
            continue
        manual_matches.append(MatchAssignment(
            file=f, episode_numbers=[target], kind="manual",
        ))
        remaining_episodes.pop(target, None)

    report = _match_core(remaining_files, remaining_episodes, start_episode)

    # Merge manual matches back in, restoring original scan order for display.
    order = {id(f): i for i, f in enumerate(files)}
    combined = report.matches + manual_matches
    combined.sort(key=lambda m: order[id(m.file)])
    report.matches = combined
    return report


def build_naive_report(files: list[ScannedFile], start_episode: int) -> MatchReport:
    """When TMDb data isn't available, fall back to V1: pure filename order.

    Every file gets an assignment; no verification, no extras detection beyond
    what refine_classification already did. Users get a warning before they
    confirm.
    """
    report = MatchReport()
    for i, f in enumerate(files):
        report.matches.append(MatchAssignment(
            file=f,
            episode_numbers=[start_episode + i],
            kind="assumed_no_tmdb",
        ))
    if files:
        report.warnings.append(
            "TMDb data unavailable — using filename order only. "
            "Episode assignments are not verified against runtime."
        )
    return report


def _fmt(sec: Optional[float]) -> str:
    """Format seconds as M:SS or H:MM:SS. Only used for warning/error messages."""
    if sec is None:
        return "?:??"
    total = int(sec)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
