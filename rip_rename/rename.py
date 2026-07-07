"""Build, execute, and reverse rename plans.

Design note: the RenamePlan is the single source of truth. `--dry-run`,
preview, execute, and undo are all just different consumers of the same
data structure. This keeps the safety guarantees simple: if you can preview
it, you can execute it; if you executed it, you can reverse it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .scanner import ScannedFile


# Plex/Jellyfin-compatible default. Series title suffix is optional so the
# episode file works both loose and inside a series folder.
DEFAULT_TEMPLATE = "{series} - S{season:02d}E{episode:02d}"


@dataclass
class RenameItem:
    src: str          # str, not Path, so this trivially JSON-serializes
    dst: str
    duration_sec: Optional[float]
    warnings: list[str] = field(default_factory=list)


@dataclass
class RenamePlan:
    items: list[RenameItem]
    series: str
    season: int
    start_episode: int
    template: str
    source_dir: str
    warnings: list[str] = field(default_factory=list)

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings) or any(item.warnings for item in self.items)


# Characters that filesystems (or Plex's scanner) dislike.
_BAD_CHARS = '<>:"/\\|?*'


def sanitize_for_filename(s: str) -> str:
    """Strip filesystem-hostile characters but keep the name readable."""
    return "".join("_" if c in _BAD_CHARS else c for c in s).strip()


def build_plan(
    files: list[ScannedFile],
    series: str,
    season: int,
    start_episode: int,
    template: str = DEFAULT_TEMPLATE,
    include_extras: bool = False,
) -> RenamePlan:
    """Construct a rename plan from a list of scanned files.

    Extras (files flagged by scanner) are skipped unless `include_extras=True`.
    Warnings are attached per-item (destination collisions) or on the plan
    (internal duplicate destinations). The CLI/caller is responsible for
    presenting them; `execute_plan` will refuse to run if any exist.
    """
    if season < 0:
        raise ValueError(f"season must be >= 0, got {season}")
    if start_episode < 1:
        raise ValueError(f"start_episode must be >= 1, got {start_episode}")

    safe_series = sanitize_for_filename(series)
    if not safe_series:
        raise ValueError("series name is empty after sanitization")

    source_dir = str(files[0].info.path.parent) if files else ""
    plan = RenamePlan(
        items=[],
        series=series,
        season=season,
        start_episode=start_episode,
        template=template,
        source_dir=source_dir,
    )

    episode = start_episode
    for f in files:
        if f.likely_extra and not include_extras:
            continue

        src = f.info.path
        try:
            stem = template.format(
                series=safe_series,
                season=season,
                episode=episode,
            )
        except (KeyError, IndexError, ValueError) as e:
            raise ValueError(f"invalid template {template!r}: {e}") from e

        dst = src.parent / f"{stem}{src.suffix}"

        item_warnings: list[str] = []
        if dst.exists() and dst != src:
            item_warnings.append(f"destination already exists: {dst.name}")

        plan.items.append(RenameItem(
            src=str(src),
            dst=str(dst),
            duration_sec=f.info.duration_sec,
            warnings=item_warnings,
        ))
        episode += 1

    # Check for internal collisions across the plan itself.
    dst_paths = [item.dst for item in plan.items]
    duplicates = sorted({p for p in dst_paths if dst_paths.count(p) > 1})
    if duplicates:
        plan.warnings.append(
            f"plan produces duplicate destinations: {[Path(d).name for d in duplicates]}"
        )

    return plan


def execute_plan(plan: RenamePlan) -> tuple[list[RenameItem], list[tuple[RenameItem, str]]]:
    """Execute all renames in the plan.

    Returns (succeeded, failed). On the first failure, execution stops —
    we never leave a partially-renamed batch in a mystery state. The caller
    can inspect what succeeded vs. what didn't and decide how to recover.

    Raises RuntimeError if the plan still has warnings — resolve them first.
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
                # Already correctly named — no-op success.
                succeeded.append(item)
                continue
            if dst.exists():
                # Race: destination appeared between planning and execution.
                raise FileExistsError(f"destination appeared during execution: {dst}")
            src.rename(dst)
            succeeded.append(item)
        except Exception as e:  # noqa: BLE001 — we intentionally catch everything here
            failed.append((item, str(e)))
            break

    return succeeded, failed


def reverse_plan(plan: RenamePlan) -> RenamePlan:
    """Return a new plan that undoes `plan`.

    Warnings are recomputed against the current filesystem state — the undo
    may itself have collisions if the user has manually shuffled files since.
    """
    reversed_items: list[RenameItem] = []
    for item in plan.items:
        # After execution, the file lives at item.dst; we want to send it back to item.src.
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
