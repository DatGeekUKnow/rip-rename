"""Discover video files in a directory and gather metadata for planning."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import ffprobe


DEFAULT_EXTENSIONS: frozenset[str] = frozenset({".mkv", ".mp4", ".m4v"})

# Files shorter than this are almost certainly extras/junk (menus, trailers, etc.).
# 5 minutes is conservative — real TV episodes are essentially never this short.
MIN_EPISODE_DURATION_SEC = 5 * 60

# Files this close in duration are suspected duplicate rips of the same title.
DUPLICATE_TOLERANCE_SEC = 2.0


@dataclass
class ScannedFile:
    info: ffprobe.MediaInfo
    likely_extra: bool


def scan(
    directory: Path,
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
    min_duration_sec: float = MIN_EPISODE_DURATION_SEC,
) -> list[ScannedFile]:
    """Return a sorted list of video files in `directory` with metadata attached.

    Sort order is filename-lexicographic, which matches MakeMKV/HandBrake output
    (t00.mkv, t01.mkv, ...) and is a good default proxy for episode order.
    """
    if not directory.exists():
        raise FileNotFoundError(f"{directory} does not exist")
    if not directory.is_dir():
        raise NotADirectoryError(f"{directory} is not a directory")

    exts = {e.lower() for e in extensions}
    candidates = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in exts
    )

    results: list[ScannedFile] = []
    for path in candidates:
        try:
            info = ffprobe.probe(path)
        except ffprobe.FFProbeError:
            # Include the file with unknown metadata; caller can still rename it.
            info = ffprobe.MediaInfo(
                path=path,
                duration_sec=None,
                width=None,
                height=None,
                audio_tracks=0,
                subtitle_tracks=0,
            )
        likely_extra = (
            info.duration_sec is not None
            and info.duration_sec < min_duration_sec
        )
        results.append(ScannedFile(info=info, likely_extra=likely_extra))

    return results


def find_duplicate_runtimes(
    files: list[ScannedFile],
    tolerance_sec: float = DUPLICATE_TOLERANCE_SEC,
) -> list[tuple[ScannedFile, ScannedFile]]:
    """Return pairs of files with suspiciously close runtimes.

    This catches the common MakeMKV case where a disc contains multiple titles
    that are actually the same episode (with/without commentary, different
    audio configs, etc.).
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
