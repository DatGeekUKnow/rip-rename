"""JSON-file persistence for rename history, defaults, config, and TMDb cache.

Layout under XDG_DATA_HOME (default ~/.local/share/rip-rename/):
  - history.json  : stack of recent executed plans (for undo)
  - defaults.json : last-used series/season for one-keypress reruns
  - cache.json    : TMDb responses (show IDs by name, episodes by season)

Config lives separately under XDG_CONFIG_HOME (default ~/.config/rip-rename/):
  - config.json   : user-editable settings (API key, etc.)
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


def data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    d = base / "rip-rename"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    d = base / "rip-rename"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _history_path() -> Path:
    return data_dir() / "history.json"


def _defaults_path() -> Path:
    return data_dir() / "defaults.json"


def _cache_path() -> Path:
    return data_dir() / "cache.json"


def _config_path() -> Path:
    return config_dir() / "config.json"


# ---------- history (unchanged from V1) ----------

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
    entries = _read_history()
    entries.append({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "plan": _plan_to_dict(plan),
    })
    entries = entries[-HISTORY_LIMIT:]
    _write_history(entries)
    return _history_path()


def peek_last_plan() -> Optional[RenamePlan]:
    entries = _read_history()
    if not entries:
        return None
    return _plan_from_dict(entries[-1]["plan"])


def pop_last_plan() -> Optional[RenamePlan]:
    entries = _read_history()
    if not entries:
        return None
    last = entries.pop()
    _write_history(entries)
    return _plan_from_dict(last["plan"])


# ---------- defaults (unchanged from V1) ----------

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


# ---------- config (new in V2) ----------

def load_config() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(**kwargs: Any) -> None:
    current = load_config()
    current.update(kwargs)
    _config_path().write_text(json.dumps(current, indent=2))


def get_tmdb_api_key(explicit: Optional[str] = None) -> Optional[str]:
    """Resolve the TMDb API key from (in precedence order):
    1. an explicit argument (from --tmdb-key)
    2. the TMDB_API_KEY environment variable
    3. the config file
    """
    if explicit:
        return explicit.strip()
    env = os.environ.get("TMDB_API_KEY")
    if env:
        return env.strip()
    cfg_key = load_config().get("tmdb_api_key")
    return cfg_key.strip() if isinstance(cfg_key, str) and cfg_key.strip() else None


# ---------- TMDb cache (new in V2) ----------

def _read_cache() -> dict[str, Any]:
    path = _cache_path()
    if not path.exists():
        return {"shows": {}, "seasons": {}}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return {"shows": {}, "seasons": {}}
        data.setdefault("shows", {})
        data.setdefault("seasons", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"shows": {}, "seasons": {}}


def _write_cache(cache: dict[str, Any]) -> None:
    _cache_path().write_text(json.dumps(cache, indent=2))


def _normalize_show_key(name: str) -> str:
    return name.strip().lower()


def get_cached_show_id(series: str) -> Optional[int]:
    cache = _read_cache()
    val = cache.get("shows", {}).get(_normalize_show_key(series))
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def cache_show_id(series: str, show_id: int) -> None:
    cache = _read_cache()
    cache["shows"][_normalize_show_key(series)] = int(show_id)
    _write_cache(cache)


def get_cached_season(show_id: int, season: int) -> Optional[dict[int, str]]:
    cache = _read_cache()
    key = f"{show_id}:{season}"
    raw = cache.get("seasons", {}).get(key)
    if not isinstance(raw, dict):
        return None
    try:
        return {int(k): str(v) for k, v in raw.items()}
    except (TypeError, ValueError):
        return None


def cache_season(show_id: int, season: int, episodes: dict[int, str]) -> None:
    cache = _read_cache()
    key = f"{show_id}:{season}"
    cache["seasons"][key] = {str(k): v for k, v in episodes.items()}
    _write_cache(cache)


def clear_cache() -> None:
    """Erase the TMDb cache. Not exposed in CLI yet; delete cache.json manually."""
    _cache_path().unlink(missing_ok=True)
