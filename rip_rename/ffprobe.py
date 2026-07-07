"""Thin wrapper around ffprobe for extracting media metadata.

Only the fields we actually use in V1 are extracted. If ffprobe is missing or
fails on a specific file, the caller decides how to degrade gracefully.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class FFProbeError(RuntimeError):
    """Raised when ffprobe isn't available or fails on a file."""


@dataclass
class MediaInfo:
    path: Path
    duration_sec: Optional[float]
    width: Optional[int]
    height: Optional[int]
    audio_tracks: int
    subtitle_tracks: int


def ensure_available() -> None:
    """Raise FFProbeError if ffprobe isn't on PATH."""
    if shutil.which("ffprobe") is None:
        raise FFProbeError(
            "ffprobe not found in PATH. Install it (Debian: `apt install ffmpeg`)."
        )


def probe(path: Path) -> MediaInfo:
    """Run ffprobe on a single file and return parsed metadata."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,width,height",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise FFProbeError(
            f"ffprobe failed for {path.name}: {result.stderr.strip() or 'unknown error'}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise FFProbeError(f"ffprobe returned invalid JSON for {path.name}: {e}") from e

    duration: Optional[float] = None
    fmt = data.get("format") or {}
    if "duration" in fmt:
        try:
            duration = float(fmt["duration"])
        except (TypeError, ValueError):
            duration = None

    width: Optional[int] = None
    height: Optional[int] = None
    audio = 0
    subs = 0
    for stream in data.get("streams", []) or []:
        ct = stream.get("codec_type")
        if ct == "video" and width is None:
            width = stream.get("width")
            height = stream.get("height")
        elif ct == "audio":
            audio += 1
        elif ct == "subtitle":
            subs += 1

    return MediaInfo(
        path=path,
        duration_sec=duration,
        width=width,
        height=height,
        audio_tracks=audio,
        subtitle_tracks=subs,
    )
