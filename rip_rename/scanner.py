"""Discover video files in a directory, gather metadata, and classify extras.

Classification is now a separate step from scanning so it can incorporate
information the CLI learns after the initial scan (specifically: expected
episode runtime from TMDb).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from . import ffprobe


DEFAULT_EXTENSIONS: frozenset[str] = frozenset({".mkv", ".mp4", ".m4v"})

# Absolute safety floor: anything shorter than this is almost certainly not an
# episode, regardless of what the reference runtime says. Keeps the fallback
# behavior safe when we have no better information.
ABSOLUTE_FLOOR_SEC = 5 * 60

# By default, an episode must be at least this fraction of the reference
# runtime to count as an episode. Chosen to comfortably admit standard-length
# episodes (which sometimes vary by ±10-15%) while excluding featurettes.
DEFAULT_MIN_EPISODE_RATIO = 0.5

# Runtime tolerance for the "possible duplicate rip" heuristic.
DUPLICATE_TOLERANCE_SEC = 2.0


@dataclass
class ScannedFile:
    info: ffprobe.MediaInfo
    likely_extra: bool = False


def _natural_sort_key(path: Path) -> list:
    """Sort key that orders embedded numbers numerically, not lexicographically.

    Plain sorting puts 'ep10' between 'ep1' and 'ep2' because it compares
    character by character. This splits the name into text/number runs so
    'ep2' < 'ep10' as a human would expect. Comparison is case-insensitive on
    the text runs.

    'ep1.mkv'  -> [(0,0,'ep'), (1,1,''), (0,0,'.mkv')]
    'ep10.mkv' -> [(0,0,'ep'), (1,10,''), (0,0,'.mkv')]

    Tuples keep text and numbers from ever being compared directly (which would
    raise TypeError), by tagging each element with a type flag first.
    """
    import re
    parts = re.split(r"(\d+)", path.name)
    key: list = []
    for i, part in enumerate(parts):
        # Odd indices are the captured digit groups.
        if i % 2 == 1:
            key.append((1, int(part), ""))
        else:
            key.append((0, 0, part.lower()))
    return key


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def scan(
    directory: Path,
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
) -> list[ScannedFile]:
    """Return a sorted list of video files in `directory` with metadata attached.

    Sort order is natural (numeric-aware): t00, t01, ..., t10 and ep1, ep2,
    ..., ep10 order the way a human reads them, which matches disc/episode
    order and is a good default proxy for episode order.

    Classification of extras is intentionally not done here — call
    `refine_classification()` separately once you have a reference runtime
    (from TMDb, or fall back to file median).
    """
    if not directory.exists():
        raise FileNotFoundError(f"{directory} does not exist")
    if not directory.is_dir():
        raise NotADirectoryError(f"{directory} is not a directory")

    exts = {e.lower() for e in extensions}
    candidates = sorted(
        (p for p in directory.iterdir()
         if p.is_file() and p.suffix.lower() in exts),
        key=_natural_sort_key,
    )

    results: list[ScannedFile] = []
    for path in candidates:
        try:
            info = ffprobe.probe(path)
        except ffprobe.FFProbeError:
            info = ffprobe.MediaInfo(
                path=path,
                duration_sec=None,
                width=None,
                height=None,
                audio_tracks=0,
                subtitle_tracks=0,
            )
        results.append(ScannedFile(info=info))

    return results


@dataclass
class ClassificationResult:
    """Report of what refine_classification did, for the CLI to display."""
    reference_duration_sec: Optional[float]
    reference_source: str  # "tmdb", "file_median", or "floor_only"
    threshold_sec: float
    episode_count: int
    extra_count: int


def refine_classification(
    files: list[ScannedFile],
    expected_duration_sec: Optional[float] = None,
    min_ratio: float = DEFAULT_MIN_EPISODE_RATIO,
    absolute_floor_sec: float = ABSOLUTE_FLOOR_SEC,
) -> ClassificationResult:
    """Classify each file as episode or extra, mutating `likely_extra` in place.

    Threshold selection:
      1. If `expected_duration_sec` is given (from TMDb), use it.
      2. Otherwise, use the median duration of files with known durations.
      3. Otherwise, use `absolute_floor_sec` alone.

    The final threshold is always `max(absolute_floor_sec, expected * min_ratio)`
    when a reference is available — the absolute floor stays as a safety net
    for cases where the reference somehow lands below it (shouldn't happen with
    normal TV, but defensive coding is cheap).

    Files with unknown duration (ffprobe failed) are classified as episodes
    (benefit of the doubt) — better to include and let the user reject than
    to silently drop data.
    """
    if expected_duration_sec is not None and expected_duration_sec > 0:
        source = "tmdb"
        reference: Optional[float] = expected_duration_sec
    else:
        known_durations = [
            f.info.duration_sec for f in files
            if f.info.duration_sec is not None
        ]
        reference = _median(known_durations)
        source = "file_median" if reference is not None else "floor_only"

    if reference is not None:
        threshold = max(absolute_floor_sec, reference * min_ratio)
    else:
        threshold = absolute_floor_sec

    episode_count = 0
    extra_count = 0
    for f in files:
        d = f.info.duration_sec
        if d is None:
            f.likely_extra = False
            episode_count += 1
        elif d < threshold:
            f.likely_extra = True
            extra_count += 1
        else:
            f.likely_extra = False
            episode_count += 1

    return ClassificationResult(
        reference_duration_sec=reference,
        reference_source=source,
        threshold_sec=threshold,
        episode_count=episode_count,
        extra_count=extra_count,
    )


def find_duplicate_runtimes(
    files: list[ScannedFile],
    tolerance_sec: float = DUPLICATE_TOLERANCE_SEC,
) -> list[tuple[ScannedFile, ScannedFile]]:
    """Return pairs of files with suspiciously close runtimes.

    Catches the MakeMKV case where a disc contains multiple titles that are
    actually the same episode (with/without commentary, different audio configs).
    """
    pairs: list[tuple[ScannedFile, ScannedFile]] = []
    for i, a in enumerate(files):
        if a.info.duration_sec is None:
            continue
        for b in files[i + 1:]:
            if b.info.duration_sec is None:
                continue
            if abs(a.info.duration_sec - b.info.duration_sec) <= tolerance_sec:
                pairs.append((a, b))
    return pairs
