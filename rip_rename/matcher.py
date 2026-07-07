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
  - EXTRA: file's runtime matches no episode in the season at all.
    Silently excluded, not blocking.
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


def match(
    files: list[ScannedFile],
    season_episodes: dict[int, EpisodeInfo],
    start_episode: int,
) -> MatchReport:
    """Walk files against expected episodes, producing a MatchReport.

    `files` should already have obvious extras filtered out (via
    scanner.refine_classification with likely_extra=True files removed).
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
