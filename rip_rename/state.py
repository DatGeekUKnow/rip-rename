"""JSON-file persistence for rename history and user defaults.

Two small files under ~/.local/share/rip-rename/:
  - history.json  : stack of recent executed plans (for undo)
  - defaults.json : last-used series/season, so re-running is one keypress

Combined into one module because for V1 there isn't enough surface area to
justify separate cache.py / history.py / config.py modules. Split later if
it grows.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .rename import RenamePlan, RenameItem


HISTORY_LIMIT = 10


def state_dir() -> Path:
    """Return (and create if needed) the state directory.

    Follows XDG conventions but falls back to ~/.local/share.
    """
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    d = base / "rip-rename"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _history_path() -> Path:
    return state_dir() / "history.json"


def _defaults_path() -> Path:
    return state_dir() / "defaults.json"


def _plan_to_dict(plan: RenamePlan) -> dict[str, Any]:
    return {
        "items": [asdict(item) for item in plan.items],
        "series": plan.series,
        "season": plan.season,
        "start_episode": plan.start_episode,
        "template": plan.template,
        "source_dir": plan.source_dir,
        "warnings": list(plan.warnings),
    }


def _plan_from_dict(d: dict[str, Any]) -> RenamePlan:
    return RenamePlan(
        items=[RenameItem(**item) for item in d["items"]],
        series=d["series"],
        season=d["season"],
        start_episode=d["start_episode"],
        template=d["template"],
        source_dir=d["source_dir"],
        warnings=list(d.get("warnings", [])),
    )


def _read_history() -> list[dict[str, Any]]:
    path = _history_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_history(entries: list[dict[str, Any]]) -> None:
    _history_path().write_text(json.dumps(entries, indent=2))


def record_execution(plan: RenamePlan) -> Path:
    """Append an executed plan to the history stack.

    Called BEFORE the actual renames run, so that a crash mid-batch still
    leaves a recoverable history entry. The old entries are truncated to
    HISTORY_LIMIT.
    """
    entries = _read_history()
    entries.append({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "plan": _plan_to_dict(plan),
    })
    entries = entries[-HISTORY_LIMIT:]
    _write_history(entries)
    return _history_path()


def peek_last_plan() -> Optional[RenamePlan]:
    """Return the most recent plan without removing it."""
    entries = _read_history()
    if not entries:
        return None
    return _plan_from_dict(entries[-1]["plan"])


def pop_last_plan() -> Optional[RenamePlan]:
    """Remove and return the most recent plan (for undo)."""
    entries = _read_history()
    if not entries:
        return None
    last = entries.pop()
    _write_history(entries)
    return _plan_from_dict(last["plan"])


def load_defaults() -> dict[str, Any]:
    path = _defaults_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_defaults(**kwargs: Any) -> None:
    current = load_defaults()
    current.update(kwargs)
    _defaults_path().write_text(json.dumps(current, indent=2))
