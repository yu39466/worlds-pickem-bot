"""Leaguepedia (lol.fandom.com) via the Cargo API.

Source of record for picks, bans, winrates, game length, pentakills, rosters
and the match schedule. It is editor/bot maintained, so it lags live play -
for in-progress games see lolesports.py.

Two tournament identifiers are needed, because the tables disagree:

    ScoreboardGames / ScoreboardPlayers -> Tournament
        e.g. "Worlds 2025 Main Event"
    MatchSchedule                       -> OverviewPage   (no Tournament column)
        e.g. "2025 Season World Championship/Main Event"

Nothing here touches the network at import time: the wiki login happens on the
first query. That matters because an MCP server that logs in (or worse, prompts
for credentials) while starting up would hang its host with no error.
"""

import os
import threading
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .errors import ConfigError, Upstream429, UpstreamAuth, UpstreamBadData, UpstreamDown

load_dotenv(Path(__file__).parent.parent / ".env")

SOURCE = "leaguepedia"

_site = None
_site_lock = threading.Lock()


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------

def site():
    """The authenticated wiki client, created on first use.

    Credentials come from WIKI_USERNAME_ME / WIKI_PASSWORD_ME, or from
    ~/.config/mwcleric/wiki_account_me.json if those are unset.
    """
    global _site
    if _site is not None:
        return _site
    with _site_lock:
        if _site is None:
            from mwrogue.auth_credentials import AuthCredentials
            from mwrogue.esports_client import EsportsClient
            try:
                _site = EsportsClient("lol", credentials=AuthCredentials(user_file="me"))
            except Exception as e:
                raise UpstreamAuth(SOURCE, f"wiki login failed: {type(e).__name__}") from e
    return _site


def reset():
    """Drop the cached client so the next call logs in again (used after auth errors)."""
    global _site
    with _site_lock:
        _site = None


def tournament():
    v = os.getenv("page_to_query")
    if not v:
        raise ConfigError(SOURCE, "page_to_query is not set")
    return v


_overview_page = None


def overview_page():
    """MatchSchedule keys on OverviewPage, which differs from the Tournament name.

    Prefer the env var. The lookup fallback is memoised because it costs a
    query, and the wiki rate-limits after about five in quick succession -
    resolving this on every schedule() call would spend the budget on
    bookkeeping.
    """
    global _overview_page
    v = os.getenv("overview_page")
    if v:
        return v
    if _overview_page:
        return _overview_page
    rows = _query(
        tables="ScoreboardGames=SG",
        fields="SG.OverviewPage,COUNT(*)=n",
        where="SG.Tournament='%s'" % _esc(tournament()),
        group_by="SG.OverviewPage",
        limit=5,
    )
    if not rows:
        raise ConfigError(SOURCE, "cannot resolve overview_page; set it in .env")
    _overview_page = max(rows, key=lambda r: int(r.get("n", 0)))["OverviewPage"]
    return _overview_page


# --------------------------------------------------------------------------
# query plumbing
# --------------------------------------------------------------------------

def _esc(value):
    """Cargo's where clause is raw SQL-ish; a stray quote breaks the query."""
    return str(value).replace("'", "\\'")


def _norm(row):
    """Cargo returns 'DateTime UTC' for a field queried as DateTime_UTC.

    It also appends bookkeeping keys like 'DateTime UTC__precision'. Normalise
    the spaces back to underscores and drop the bookkeeping, so callers can use
    the same spelling they queried with.
    """
    out = {}
    for key, value in row.items():
        if key.endswith("__precision"):
            continue
        out[key.replace(" ", "_")] = value
    return out


def _query(**kwargs):
    """Run a Cargo query, translating library failures into typed errors."""
    try:
        rows = site().cargo_client.query(**kwargs)
    except (Upstream429, UpstreamAuth, UpstreamDown, ConfigError):
        raise
    except Exception as e:
        text = str(e).lower()
        if "rate limit" in text or "ratelimited" in text:
            raise Upstream429(SOURCE, retry_after=30, message=str(e)[:200]) from e
        if "login" in text or "denied" in text or "permission" in text:
            reset()
            raise UpstreamAuth(SOURCE, str(e)[:200]) from e
        if "db_error" in text:
            # Almost always a column that does not exist on that table.
            raise UpstreamBadData(SOURCE, f"bad cargo query: {str(e)[:200]}") from e
        raise UpstreamDown(SOURCE, f"{type(e).__name__}: {str(e)[:200]}") from e
    return [_norm(r) for r in rows]


# --------------------------------------------------------------------------
# champion picks / bans / winrate
# --------------------------------------------------------------------------

def _split(value):
    return [c.strip() for c in (value or "").split(",") if c.strip()]


def champion_table():
    """One pass over every game, returning (table, games_played).

    table maps champion name -> {"picks", "bans", "wins", "losses"}.
    Replaces the separate mp_bp() and wr_bp() passes in helpers/.
    """
    rows = _query(
        tables="Tournaments=T, ScoreboardGames=SG",
        fields="SG.Team1Picks,SG.Team2Picks,SG.Team1Bans,SG.Team2Bans,SG.Winner",
        where="T.Name='%s'" % _esc(tournament()),
        join_on="SG.OverviewPage=T.OverviewPage",
    )

    table = {}

    def slot(champ):
        return table.setdefault(champ, {"picks": 0, "bans": 0, "wins": 0, "losses": 0})

    for row in rows:
        team1 = _split(row.get("Team1Picks"))
        team2 = _split(row.get("Team2Picks"))
        for champ in team1 + team2:
            slot(champ)["picks"] += 1
        for champ in _split(row.get("Team1Bans")) + _split(row.get("Team2Bans")):
            slot(champ)["bans"] += 1

        winner = str(row.get("Winner") or "")
        if winner == "1":
            won, lost = team1, team2
        elif winner == "2":
            won, lost = team2, team1
        else:
            continue                      # unplayed or nullified game
        for champ in won:
            slot(champ)["wins"] += 1
        for champ in lost:
            slot(champ)["losses"] += 1

    return table, len(rows)


def rank(table, metric="picks", order="desc", min_games=5, champion=None):
    """Rank champion_table() output. Returns a list of flat dicts.

    metric "winrate" only includes champions with at least min_games games,
    matching the existing /wr behaviour.
    """
    rows = []
    for name, stat in table.items():
        played = stat["wins"] + stat["losses"]
        rows.append({
            "champion": name,
            "picks": stat["picks"],
            "bans": stat["bans"],
            "wins": stat["wins"],
            "losses": stat["losses"],
            "games": played,
            "winrate": round(stat["wins"] / played, 4) if played else None,
        })

    if champion:
        wanted = champion.strip().lower()
        return [r for r in rows if r["champion"].lower() == wanted]

    if metric == "winrate":
        rows = [r for r in rows if r["games"] >= min_games and r["winrate"] is not None]

    rows.sort(key=lambda r: (r[metric] is None, r[metric]), reverse=(order == "desc"))
    return rows


# --------------------------------------------------------------------------
# schedule / standings
# --------------------------------------------------------------------------

def schedule():
    """Every match of the tournament, earliest first.

    MatchSchedule has no Tournament column - it keys on OverviewPage.
    """
    rows = _query(
        tables="MatchSchedule=MS",
        fields="MS.Team1,MS.Team2,MS.DateTime_UTC,MS.BestOf,MS.Winner,MS.Tab,MS.Stream",
        where="MS.OverviewPage='%s'" % _esc(overview_page()),
        order_by="MS.DateTime_UTC ASC",
    )
    out = []
    for row in rows:
        out.append({
            "team1": row.get("Team1"),
            "team2": row.get("Team2"),
            "start_utc": row.get("DateTime_UTC"),
            "best_of": _int(row.get("BestOf")),
            "winner": _int(row.get("Winner")),          # 1, 2, or None if unplayed
            "stage": row.get("Tab"),
            "played": bool(row.get("Winner")),
        })
    return out


def standings(matches=None):
    """Win/loss record per team, derived from the schedule. Best record first."""
    matches = schedule() if matches is None else matches
    record = {}

    def slot(team):
        return record.setdefault(team, {"team": team, "wins": 0, "losses": 0})

    for match in matches:
        if not match["played"] or not match["team1"] or not match["team2"]:
            continue
        won = match["team1"] if match["winner"] == 1 else match["team2"]
        lost = match["team2"] if match["winner"] == 1 else match["team1"]
        slot(won)["wins"] += 1
        slot(lost)["losses"] += 1

    rows = list(record.values())
    rows.sort(key=lambda r: (-r["wins"], r["losses"], r["team"]))
    return rows


def current_stage(matches=None):
    """Name of the earliest stage that still has unplayed matches."""
    matches = schedule() if matches is None else matches
    for match in matches:
        if not match["played"]:
            return match["stage"]
    return matches[-1]["stage"] if matches else None


# --------------------------------------------------------------------------
# game length / pentakills / rosters
# --------------------------------------------------------------------------

def length_extremes():
    """Longest and shortest completed games, as (longest, shortest).

    One sorted query rather than a DESC and an ASC: the wiki rate-limits after
    roughly five requests in quick succession, so query count is the scarce
    resource here, not rows transferred.

    Gamelength_Number is minutes as a float and sorts correctly; the
    Gamelength string ("32:41") is what we display.
    """
    rows = _query(
        tables="ScoreboardGames=SG",
        fields="SG.Gamelength,SG.Gamelength_Number,SG.Team1,SG.Team2,SG.WinTeam",
        where="SG.Tournament='%s' AND SG.Gamelength_Number IS NOT NULL" % _esc(tournament()),
        order_by="SG.Gamelength_Number ASC",
    )
    if not rows:
        return None, None

    def shape(row):
        return {
            "length": row.get("Gamelength"),
            "minutes": _float(row.get("Gamelength_Number")),
            "team1": row.get("Team1"),
            "team2": row.get("Team2"),
            "winner": row.get("WinTeam"),
        }

    return shape(rows[-1]), shape(rows[0])


def pentakills(limit=10):
    """Players who have scored a pentakill, most recent first."""
    rows = _query(
        tables="ScoreboardPlayers=SP",
        fields="SP.Name,SP.Champion,SP.Team,SP.TeamVs,SP.Pentakills,SP.DateTime_UTC",
        where="SP.Tournament='%s' AND SP.Pentakills > 0" % _esc(tournament()),
        order_by="SP.DateTime_UTC DESC",
        limit=limit,
    )
    return [{
        "player": r.get("Name"),
        "champion": r.get("Champion"),
        "team": r.get("Team"),
        "against": r.get("TeamVs"),
        "count": _int(r.get("Pentakills")) or 1,
        "when_utc": r.get("DateTime_UTC"),
    } for r in rows]


def players(team=None, role=None):
    """Tournament roster: player, team, country, role."""
    rows = _query(
        tables="Tournaments=T, TournamentPlayers=TP, PlayerRedirects=PR, Players=P",
        fields="P.Player,P.Name,P.Country,P.Role,TP.Team",
        where="T.Name='%s'" % _esc(tournament()),
        join_on="T.OverviewPage=TP.OverviewPage, TP.Player=PR.AllName, PR.OverviewPage=P.OverviewPage",
    )
    out = [{
        "player": r.get("Player"),
        "name": r.get("Name"),
        "country": r.get("Country"),
        "role": r.get("Role"),
        "team": r.get("Team"),
    } for r in rows]

    if team:
        wanted = team.strip().lower()
        out = [p for p in out if (p["team"] or "").lower() == wanted]
    if role:
        wanted = role.strip().lower()
        out = [p for p in out if (p["role"] or "").lower() == wanted]
    return out


# --------------------------------------------------------------------------
# champion icons
# --------------------------------------------------------------------------

_ddragon = {"version": None, "names": {}}
_ddragon_lock = threading.Lock()


def _load_ddragon():
    """Fetch the current patch and its champion list.

    Needed because display names do not map to icon filenames by any simple
    rule - "Kai'Sa" is Kaisa, "Nunu & Willump" is Nunu, "Dr. Mundo" is DrMundo.
    Guessing with string replacement (as the old embed code did) gets these
    wrong, so read the real mapping instead.
    """
    with _ddragon_lock:
        if _ddragon["names"]:
            return _ddragon
        with httpx.Client(timeout=httpx.Timeout(5.0, read=10.0)) as client:
            versions = client.get("https://ddragon.leagueoflegends.com/api/versions.json").json()
            version = versions[0]
            data = client.get(
                f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
            ).json()["data"]
        _ddragon["version"] = version
        _ddragon["names"] = {v["name"].lower(): k for k, v in data.items()}
        return _ddragon


def icon_url(champion):
    """Square icon URL for a champion display name, or None if unrecognised."""
    try:
        dd = _load_ddragon()
    except Exception:
        return None                       # cosmetic only; never fail a query over it
    key = dd["names"].get((champion or "").strip().lower())
    if not key:
        return None
    return f"https://ddragon.leagueoflegends.com/cdn/{dd['version']}/img/champion/{key}.png"


# --------------------------------------------------------------------------

def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
