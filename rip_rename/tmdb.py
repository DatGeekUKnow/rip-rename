"""Thin TMDb API client — stdlib only, no external deps.

Uses TMDb API v3 with the api_key query parameter for auth. Rate limits are
generous (~50 rps) so no throttling logic needed for a CLI tool.

TMDb attribution: this product uses the TMDB API but is not endorsed or
certified by TMDB. See https://www.themoviedb.org/
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional


TMDB_BASE = "https://api.themoviedb.org/3"
DEFAULT_TIMEOUT_SEC = 15


class TMDbError(RuntimeError):
    """Any TMDb API failure."""


class TMDbAuthError(TMDbError):
    """API key missing, invalid, or expired."""


@dataclass
class ShowMatch:
    id: int
    name: str
    first_air_date: str  # "2015-12-16" or "" if unknown
    overview: str

    @property
    def year(self) -> str:
        return self.first_air_date[:4] if self.first_air_date else "----"


@dataclass
class EpisodeInfo:
    """Per-episode data from TMDb.

    `runtime_min` is in minutes and may be None if TMDb doesn't have a value
    for a specific episode. Individual episodes can lack runtime even when
    the show as a whole has data.
    """
    number: int
    title: str
    runtime_min: Optional[int]


def _request(path: str, api_key: str, params: Optional[dict[str, Any]] = None) -> dict:
    """Make a GET request against the TMDb API and return parsed JSON."""
    query: dict[str, Any] = {"api_key": api_key}
    if params:
        query.update(params)
    url = f"{TMDB_BASE}{path}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise TMDbAuthError(
                "TMDb rejected the API key (401 Unauthorized). "
                "Check that your key is correct."
            ) from e
        if e.code == 404:
            raise TMDbError(f"TMDb returned 404 for {path} (not found).") from e
        raise TMDbError(f"TMDb HTTP {e.code} for {path}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise TMDbError(f"Network error calling TMDb: {e.reason}") from e

    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise TMDbError(f"TMDb returned invalid JSON: {e}") from e


def search_tv(query: str, api_key: str, limit: int = 5) -> list[ShowMatch]:
    """Return up to `limit` show matches for the query, ordered by TMDb popularity."""
    if not query.strip():
        return []
    data = _request("/search/tv", api_key, {"query": query})
    results = (data.get("results") or [])[:limit]
    matches: list[ShowMatch] = []
    for r in results:
        if "id" not in r:
            continue
        matches.append(ShowMatch(
            id=int(r["id"]),
            name=r.get("name") or "?",
            first_air_date=r.get("first_air_date") or "",
            overview=r.get("overview") or "",
        ))
    return matches


def get_tv_season_episodes(show_id: int, season: int, api_key: str) -> dict[int, EpisodeInfo]:
    """Return {episode_number: EpisodeInfo} for the given season.

    A missing/empty title results in an empty string. A missing runtime
    results in None. The caller decides how to render or use these.
    """
    data = _request(f"/tv/{show_id}/season/{season}", api_key)
    episodes = data.get("episodes") or []
    result: dict[int, EpisodeInfo] = {}
    for ep in episodes:
        if "episode_number" not in ep:
            continue
        try:
            num = int(ep["episode_number"])
        except (TypeError, ValueError):
            continue

        rt = ep.get("runtime")
        runtime_min: Optional[int]
        try:
            runtime_min = int(rt) if rt is not None else None
        except (TypeError, ValueError):
            runtime_min = None
        if runtime_min is not None and runtime_min <= 0:
            runtime_min = None  # TMDb sometimes returns 0 for unknown

        result[num] = EpisodeInfo(
            number=num,
            title=ep.get("name") or "",
            runtime_min=runtime_min,
        )
    return result
