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
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .errors import (
    ConfigError,
    SourceError,
    Upstream429,
    UpstreamAuth,
    UpstreamBadData,
    UpstreamDown,
)

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


def rank(table, metric="picks", order="desc", min_games=5, champion=None, games_played=None):
    """Rank champion_table() output. Returns a list of flat dicts.

    metric "winrate" only includes champions with at least min_games games,
    matching the existing /wr behaviour.

    "presence" is picks + bans: the number of games a champion was drafted or
    removed. A champion cannot be both picked and banned in the same game, so
    the sum is a game count, not a double count. Pass games_played (from
    champion_table) to also get presence_rate as a fraction of the tournament.
    """
    rows = []
    for name, stat in table.items():
        played = stat["wins"] + stat["losses"]
        presence = stat["picks"] + stat["bans"]
        rows.append({
            "champion": name,
            "picks": stat["picks"],
            "pick_rate": _rate(stat["picks"], games_played),
            "bans": stat["bans"],
            "ban_rate": _rate(stat["bans"], games_played),
            "wins": stat["wins"],
            "losses": stat["losses"],
            "games": played,
            "presence": presence,
            "presence_rate": _rate(presence, games_played),
            "winrate": round(stat["wins"] / played, 3) if played else None,
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
        team1, team2 = row.get("Team1"), row.get("Team2")
        winner = _int(row.get("Winner"))                 # 1, 2, or None if unplayed
        out.append({
            "team1": team1,
            "team2": team2,
            "start_utc": row.get("DateTime_UTC"),
            "start_epoch": _epoch(row.get("DateTime_UTC")),
            "best_of": _int(row.get("BestOf")),
            "winner": {1: team1, 2: team2}.get(winner),  # team name, not an index
            "stage": row.get("Tab"),
            "played": winner is not None,
        })

    # Attach each team's current record. Derived from the same rows, so it
    # costs no extra query, and it saves the caller a second tool call just
    # to put "3-1" next to a team name.
    record = {r["team"]: f'{r["wins"]}-{r["losses"]}' for r in standings(out)}
    codes = short_codes({m["team1"] for m in out} | {m["team2"] for m in out})
    for match in out:
        match["team1_record"] = record.get(match["team1"])
        match["team2_record"] = record.get(match["team2"])
        match["team1_code"] = codes.get(match["team1"])
        match["team2_code"] = codes.get(match["team2"])
    return out


def short_codes(names):
    """Map full team names to their short codes: "100 Thieves" -> "100T".

    Leaguepedia stores only full names on a match row, but people type codes.
    One extra query per schedule refresh, which the 300s cache absorbs.
    """
    names = [n for n in names if n]
    if not names:
        return {}
    try:
        rows = _query(
            tables="Teams=T",
            fields="T.Name,T.Short",
            where=" OR ".join("T.Name='%s'" % _esc(n) for n in sorted(names)),
        )
    except SourceError:
        return {}                     # cosmetic - never fail a schedule over it
    return {r["Name"]: r["Short"] for r in rows if r.get("Short")}


def stages(matches):
    """Stage names present in the schedule, in the order they are played."""
    seen, out = set(), []
    for m in matches:
        if m["stage"] and m["stage"] not in seen:
            seen.add(m["stage"])
            out.append(m["stage"])
    return out


def resolve_stage(matches, needle):
    """Which stage names does a user's phrase mean? Returns a list, possibly empty.

    Leaguepedia names the Swiss rounds individually ("Round 1".."Round 5")
    and has no umbrella label, so "swiss" has to expand to the round names.
    The groups are derived from whatever stages the data actually contains
    rather than hardcoded, because Worlds changes format between years.

    Matching is deliberately ordered: an exact name wins first, so "finals"
    means Finals and not also Quarterfinals and Semifinals.
    """
    needle = (needle or "").strip().lower()
    if not needle:
        return []
    present = stages(matches)

    exact = [s for s in present if s.lower() == needle]
    if exact:
        return exact

    rounds = [s for s in present if s.lower().startswith("round")]
    knockout = [s for s in present if s not in rounds]
    groups = {
        "swiss": rounds, "swiss stage": rounds, "group": rounds, "group stage": rounds,
        "knockout": knockout, "knockouts": knockout, "playoff": knockout,
        "playoffs": knockout, "bracket": knockout, "elimination": knockout,
    }
    if needle in groups:
        return groups[needle]

    abbrev = {"qf": "quarterfinals", "sf": "semifinals", "quarters": "quarterfinals",
              "quarterfinal": "quarterfinals", "semis": "semifinals", "semi": "semifinals",
              "semifinal": "semifinals", "final": "finals", "grand final": "finals",
              "grand finals": "finals"}
    if needle in abbrev:
        return [s for s in present if s.lower() == abbrev[needle]]

    starts = [s for s in present if s.lower().startswith(needle)]
    if starts:
        return starts
    return [s for s in present if needle in s.lower()]


def team_logos(names):
    """Map team names to logo URLs.

    Leaguepedia stores a wiki filename ("T1logo profile.png"), not a URL, so
    the names have to be resolved through the MediaWiki imageinfo API. Both
    calls are batched - one Cargo query, one API query, for every team at
    once - and the result is small enough to cache for an hour.
    """
    names = sorted(n for n in names if n)
    if not names:
        return {}
    try:
        rows = _query(
            tables="Teams=T",
            fields="T.Name,T.Image",
            where=" OR ".join("T.Name='%s'" % _esc(n) for n in names),
        )
        files = {r["Name"]: r["Image"] for r in rows if r.get("Image")}
        if not files:
            return {}
        answer = site().client.api(
            "query",
            titles="|".join("File:" + f for f in files.values()),
            prop="imageinfo",
            iiprop="url",
        )
        by_title = {}
        for page in answer.get("query", {}).get("pages", {}).values():
            info = (page.get("imageinfo") or [{}])[0]
            if info.get("url"):
                by_title[page["title"]] = info["url"]
        return {
            name: _discord_safe(by_title["File:" + filename])
            for name, filename in files.items()
            if "File:" + filename in by_title
        }
    except Exception:
        return {}                     # decoration only - never fail over a logo


def _discord_safe(url, width=256):
    """Rewrite a Fandom image URL into something Discord will actually show.

    Fandom content-negotiates: a URL ending .png is served as image/webp, and
    Discord's media proxy starts loading it and then drops it. ?format=original
    forces the real PNG, and scaling it down turns a 128KB logo into ~5KB.
    """
    base = url.split("/revision/")[0]
    return f"{base}/revision/latest/scale-to-width-down/{width}?format=original"


def team_matches(match, needle):
    """Does this match involve the team the user named?

    Accepts a full name, a short code, or any part of either, case-insensitive:
    "100T", "100 thieves", "thieves" and "100t" all find 100 Thieves.
    """
    needle = (needle or "").strip().lower()
    if not needle:
        return False
    for side in ("team1", "team2"):
        name = (match.get(side) or "").lower()
        code = (match.get(side + "_code") or "").lower()
        if needle == code or needle in name or needle.replace(" ", "") == name.replace(" ", ""):
            return True
    return False


def matches_on(matches, day):
    """Just the matches starting on an ISO date (YYYY-MM-DD, UTC)."""
    return [m for m in matches if (m["start_utc"] or "").startswith(day)]


def resolve_day(matches, day=None, today=None):
    """Decide which day to show, and say why. Returns (day, basis).

    basis is one of:
        requested - the caller named a date
        today     - there are games today
        next      - nothing today, so the next day that has games
        last      - no games left at all, so the most recent day

    The "last" case matters once a tournament is over: asking what is on today
    should say the tournament has finished and show the final day, rather than
    returning an empty list with no explanation.
    """
    today = today or date.today().isoformat()
    days = sorted({(m["start_utc"] or "")[:10] for m in matches if m["start_utc"]})
    if not days:
        return None, "none"
    if day:
        return day, "requested"
    if today in days:
        return today, "today"
    upcoming = [d for d in days if d > today]
    if upcoming:
        return upcoming[0], "next"
    return days[-1], "last"


def standings(matches=None):
    """Win/loss record per team, derived from the schedule. Best record first."""
    matches = schedule() if matches is None else matches
    record = {}

    def slot(team):
        return record.setdefault(team, {"team": team, "wins": 0, "losses": 0})

    for match in matches:
        if not match["played"] or not match["team1"] or not match["team2"]:
            continue
        won = match["winner"]                 # a team name, not an index
        if won not in (match["team1"], match["team2"]):
            continue                          # malformed row; do not guess
        lost = match["team2"] if won == match["team1"] else match["team1"]
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

    # Gamelength_Number is queried and sorted on, but not returned:
    # "58:51" is the readable form, 58.85 is just how the database sorts.
    def shape(row):
        return {
            "length": row.get("Gamelength"),
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

def _epoch(stamp):
    """Cargo returns "2025-10-15 05:00:00" - UTC, but with no marker on it.

    Discord renders <t:EPOCH:R> in each reader's own timezone, so the epoch
    is what the bot actually needs; the string is kept for readability.
    """
    if not stamp:
        return None
    try:
        naive = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return int(naive.replace(tzinfo=timezone.utc).timestamp())


def _rate(count, total):
    """Share of the tournament's games, as a 0-1 fraction rounded to 3 places."""
    return round(count / total, 3) if total else None


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
