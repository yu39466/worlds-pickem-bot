"""LoL Esports API (esports-api.lolesports.com).

This is the unofficial API behind lolesports.com. There is no developer
portal: the x-api-key is a public constant the website itself sends, shared
by everyone, and it can change without notice.

Use it for freshness - it reflects live and just-finished matches, where
Leaguepedia lags behind its editors. It is pinned to the *current* season by
league id, so it cannot serve a past tournament; historical questions go to
leaguepedia.py.
"""

import os
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .errors import ConfigError, Upstream429, UpstreamAuth, UpstreamBadData, UpstreamDown

load_dotenv(Path(__file__).parent.parent / ".env")

SOURCE = "lolesports"
BASE = "https://esports-api.lolesports.com/persisted/gw"

# Connect fast, allow a slower body; the old code had no timeout at all, so a
# hung request would block a Discord handler indefinitely.
TIMEOUT = httpx.Timeout(3.0, read=8.0)


def _api_key():
    key = os.getenv("API_KEY")
    if not key:
        raise ConfigError(SOURCE, "API_KEY is not set")
    return key


def _league_id():
    league = os.getenv("worlds_id")
    if not league:
        raise ConfigError(SOURCE, "worlds_id is not set")
    return league


def _get(path, params):
    """One GET, with library exceptions translated into typed errors."""
    headers = {"x-api-key": _api_key(), "Accept": "application/json"}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.get(f"{BASE}/{path}", headers=headers, params=params)
    except httpx.TimeoutException as e:
        raise UpstreamDown(SOURCE, "request timed out") from e
    except httpx.HTTPError as e:
        raise UpstreamDown(SOURCE, f"{type(e).__name__}: {str(e)[:150]}") from e

    if response.status_code == 429:
        retry = response.headers.get("Retry-After")
        raise Upstream429(SOURCE, retry_after=_int(retry) or 30)
    if response.status_code in (401, 403):
        raise UpstreamAuth(SOURCE, "API_KEY rejected - it may have rotated")
    if response.status_code >= 500:
        raise UpstreamDown(SOURCE, f"HTTP {response.status_code}")
    if response.status_code != 200:
        raise UpstreamDown(SOURCE, f"HTTP {response.status_code}")

    try:
        return response.json()
    except ValueError as e:
        raise UpstreamBadData(SOURCE, "response was not JSON") from e


def fetch_schedule(league_id=None, locale="en-US", page_token=None):
    """Raw getSchedule payload for a league (defaults to worlds_id)."""
    params = {"hl": locale, "leagueId": league_id or _league_id()}
    if page_token:
        params["pageToken"] = page_token
    return _get("getSchedule", params)


def events(payload=None):
    """Flatten a getSchedule payload into a list of normalised matches."""
    payload = fetch_schedule() if payload is None else payload
    try:
        raw = payload["data"]["schedule"]["events"]
    except (KeyError, TypeError) as e:
        raise UpstreamBadData(SOURCE, "unexpected getSchedule shape") from e

    out = []
    for event in raw:
        if not isinstance(event, dict):
            continue
        match = event.get("match")
        start = event.get("startTime")
        if not match or not start:
            continue                      # shows and other non-match events

        teams = []
        for team in match.get("teams", []):
            record = team.get("record") or {}
            result = team.get("result") or {}
            teams.append({
                "code": team.get("code"),
                "name": team.get("name"),
                "wins": record.get("wins"),
                "losses": record.get("losses"),
                "game_wins": result.get("gameWins"),
                "outcome": result.get("outcome"),
            })

        out.append({
            "start_utc": start,
            "start_epoch": _epoch(start),
            "state": event.get("state"),          # unstarted / inProgress / completed
            "stage": event.get("blockName"),
            "best_of": (match.get("strategy") or {}).get("count"),
            "teams": teams,
            "tbd": any(t.get("name") == "TBD" for t in match.get("teams", [])),
        })
    return out


def filter_events(all_events, day=None, state="upcoming", drop_tbd=True, limit=None):
    """Narrow the event list by date and state.

    day  - ISO date "YYYY-MM-DD" in UTC; None means no date filter.
    state - "upcoming" (future and not completed), "completed", "live", or "all".
    """
    now = datetime.now(timezone.utc)
    rows = []

    for event in all_events:
        if drop_tbd and event["tbd"]:
            continue

        if day:
            if not event["start_utc"].startswith(day):
                continue

        if state == "upcoming":
            started = _parse(event["start_utc"])
            if event["state"] == "completed" or (started and started <= now):
                continue
        elif state == "completed":
            if event["state"] != "completed":
                continue
        elif state == "live":
            if event["state"] != "inProgress":
                continue

        rows.append(event)

    return rows[:limit] if limit else rows


def next_match_day(all_events, after=None):
    """Earliest future date (YYYY-MM-DD) that has matches, or None."""
    after = after or date.today().isoformat()
    days = sorted({e["start_utc"][:10] for e in all_events if e["start_utc"][:10] > after})
    return days[0] if days else None


def current_stage(all_events):
    """blockName of the earliest event that has not finished."""
    for event in all_events:
        if event["state"] != "completed":
            return event["stage"]
    return all_events[-1]["stage"] if all_events else None


def _parse(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _epoch(iso):
    parsed = _parse(iso)
    return int(parsed.timestamp()) if parsed else None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
