"""Build, execute, and reverse rename plans.

The RenamePlan is the single source of truth. Preview, dry-run, execute, and
undo all consume the same object.

V2.2: `build_plan()` now takes a list of MatchAssignment objects (from
matcher.py) instead of raw files + start_episode. This lets the matcher own
the logic of which file maps to which episode(s), including combined-episode
files like Avatar S02E19-E20.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .matcher import MatchAssignment


# Plex/Jellyfin-compatible naming templates. See:
#   https://support.plex.tv/articles/naming-and-organizing-your-tv-show-files/
DEFAULT_TEMPLATE_WITH_TITLE = "{series} - S{season:02d}E{episode:02d} - {title}"
DEFAULT_TEMPLATE_WITHOUT_TITLE = "{series} - S{season:02d}E{episode:02d}"
DEFAULT_TEMPLATE_COMBINED_WITH_TITLE = "{series} - S{season:02d}E{start_ep:02d}-E{end_ep:02d} - {title}"
DEFAULT_TEMPLATE_COMBINED_WITHOUT_TITLE = "{series} - S{season:02d}E{start_ep:02d}-E{end_ep:02d}"


@dataclass
class RenameItem:
    src: str
    dst: str
    duration_sec: Optional[float]
    warnings: list[str] = field(default_factory=list)


@dataclass
class RenamePlan:
    items: list[RenameItem]
    series: str
    season: int
    start_episode: int
    template: str          # informational — the template actually used
    source_dir: str
    warnings: list[str] = field(default_factory=list)

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings) or any(item.warnings for item in self.items)


_BAD_CHARS = '<>:"/\\|?*'


def sanitize_for_filename(s: str) -> str:
    """Strip filesystem-hostile characters but keep the name readable."""
    return "".join("_" if c in _BAD_CHARS else c for c in s).strip()


def build_plan(
    assignments: list[MatchAssignment],
    series: str,
    season: int,
    start_episode: int,
    titles: Optional[dict[int, str]] = None,
    template_with_title: str = DEFAULT_TEMPLATE_WITH_TITLE,
    template_without_title: str = DEFAULT_TEMPLATE_WITHOUT_TITLE,
    template_combined_with_title: str = DEFAULT_TEMPLATE_COMBINED_WITH_TITLE,
    template_combined_without_title: str = DEFAULT_TEMPLATE_COMBINED_WITHOUT_TITLE,
) -> RenamePlan:
    """Construct a rename plan from matcher assignments.

    Args:
        assignments: pre-verified file→episode(s) mappings
        series: show name (raw; will be sanitized for filesystem safety)
        season: season number
        start_episode: informational, stored on the plan
        titles: optional {episode_number: title}; for combined-episode files,
                only the first episode's title is used
        templates: format strings for the four combinations of single/combined
                   and with/without title

    Missing title for a specific episode falls back to the without-title
    template for that item only (so you don't get "S02E05 - .mkv").
    """
    if season < 0:
        raise ValueError(f"season must be >= 0, got {season}")
    if start_episode < 1:
        raise ValueError(f"start_episode must be >= 1, got {start_episode}")

    safe_series = sanitize_for_filename(series)
    if not safe_series:
        raise ValueError("series name is empty after sanitization")

    titles = titles or {}
    source_dir = str(assignments[0].file.info.path.parent) if assignments else ""

    plan = RenamePlan(
        items=[],
        series=series,
        season=season,
        start_episode=start_episode,
        template=template_with_title if titles else template_without_title,
        source_dir=source_dir,
    )

    for a in assignments:
        src = a.file.info.path
        is_combined = len(a.episode_numbers) > 1
        # For combined episodes, use the first episode's title (per user preference).
        raw_title = titles.get(a.episode_numbers[0], "") if titles else ""
        safe_title = sanitize_for_filename(raw_title) if raw_title else ""

        try:
            if is_combined:
                if safe_title:
                    stem = template_combined_with_title.format(
                        series=safe_series,
                        season=season,
                        start_ep=a.episode_numbers[0],
                        end_ep=a.episode_numbers[-1],
                        title=safe_title,
                    )
                else:
                    stem = template_combined_without_title.format(
                        series=safe_series,
                        season=season,
                        start_ep=a.episode_numbers[0],
                        end_ep=a.episode_numbers[-1],
                    )
            else:
                if safe_title:
                    stem = template_with_title.format(
                        series=safe_series,
                        season=season,
                        episode=a.episode_numbers[0],
                        title=safe_title,
                    )
                else:
                    stem = template_without_title.format(
                        series=safe_series,
                        season=season,
                        episode=a.episode_numbers[0],
                    )
        except (KeyError, IndexError, ValueError) as e:
            raise ValueError(f"invalid template: {e}") from e

        dst = src.parent / f"{stem}{src.suffix}"

        item_warnings: list[str] = []
        if dst.exists() and dst != src:
            item_warnings.append(f"destination already exists: {dst.name}")

        plan.items.append(RenameItem(
            src=str(src),
            dst=str(dst),
            duration_sec=a.file.info.duration_sec,
            warnings=item_warnings,
        ))

    # Internal collision check (two sources → same destination)
    dst_paths = [item.dst for item in plan.items]
    duplicates = sorted({p for p in dst_paths if dst_paths.count(p) > 1})
    if duplicates:
        plan.warnings.append(
            f"plan produces duplicate destinations: {[Path(d).name for d in duplicates]}"
        )

    return plan


def execute_plan(plan: RenamePlan) -> tuple[list[RenameItem], list[tuple[RenameItem, str]]]:
    """Execute all renames. Returns (succeeded, failed).

    Stops on first failure — never leaves a partial batch in a mystery state.
    Raises RuntimeError if the plan has unresolved warnings.
    """
    if plan.has_warnings:
        raise RuntimeError(
            "refusing to execute plan with unresolved warnings; "
            "resolve conflicts or rebuild the plan"
        )

    succeeded: list[RenameItem] = []
    failed: list[tuple[RenameItem, str]] = []

    for item in plan.items:
        src = Path(item.src)
        dst = Path(item.dst)
        try:
            if not src.exists():
                raise FileNotFoundError(f"source no longer exists: {src}")
            if dst == src:
                succeeded.append(item)
                continue
            if dst.exists():
                raise FileExistsError(f"destination appeared during execution: {dst}")
            src.rename(dst)
            succeeded.append(item)
        except Exception as e:  # noqa: BLE001
            failed.append((item, str(e)))
            break

    return succeeded, failed


def reverse_plan(plan: RenamePlan) -> RenamePlan:
    """Return a new plan that undoes `plan`."""
    reversed_items: list[RenameItem] = []
    for item in plan.items:
        forward_src = Path(item.dst)
        forward_dst = Path(item.src)

        warnings: list[str] = []
        if forward_dst.exists() and forward_dst != forward_src:
            warnings.append(f"destination already exists: {forward_dst.name}")

        reversed_items.append(RenameItem(
            src=str(forward_src),
            dst=str(forward_dst),
            duration_sec=item.duration_sec,
            warnings=warnings,
        ))

    return RenamePlan(
        items=reversed_items,
        series=plan.series,
        season=plan.season,
        start_episode=plan.start_episode,
        template=plan.template,
        source_dir=plan.source_dir,
    )
