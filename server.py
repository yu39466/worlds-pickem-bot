"""MCP server exposing Worlds pick'ems data."""

import logging
import sys
from typing import Annotated, Literal

import anyio
from fastmcp import FastMCP
from pydantic import Field

from sources import cache
from sources import leaguepedia as lp
from sources.errors import (
    ConfigError,
    SourceError,
    Upstream429,
    UpstreamAuth,
)

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("lina.server")

mcp = FastMCP("lina-worlds")

# Minimum games for a champion to appear in a winrate ranking.
MIN_GAMES = 5

# Minimum games for a player to appear in a KDA ranking.
MIN_GAMES_PLAYER = 5


def _err(e):
    """Turn an exception into something Claude can explain to a user.

    Tools return this instead of raising. A raised exception reaches Claude as
    a stringified traceback - long, leaky, and differently worded every time -
    whereas a small tagged dict is a fact it can report in one sentence.

    Order matters: every Upstream* class inherits from SourceError, so the
    specific cases have to be tested before the catch-all.
    """
    if isinstance(e, Upstream429):
        return {"error": "rate_limited", "source": e.source, "retry_after_s": e.retry_after}
    if isinstance(e, UpstreamAuth):
        return {"error": "auth_failed", "source": e.source}
    if isinstance(e, ConfigError):
        return {"error": "config", "source": e.source, "detail": str(e)[:120]}
    if isinstance(e, SourceError):
        return {"error": "upstream_unavailable", "source": e.source, "detail": str(e)[:120]}

    # Anything else is a bug in our code. The traceback goes to stderr, where
    # the log file keeps it; Claude only needs to know it failed.
    log.exception("unhandled error in tool")
    return {"error": "internal", "detail": type(e).__name__}


@mcp.tool
async def get_game_length_extremes() -> dict:
    """Longest and shortest completed games of the tournament.

    Use for questions about game length records — longest game, shortest game,
    fastest win, quickest game so far.

    Returns `longest` and `shortest`, each with: length as "MM:SS",
    the two teams, and winner (a team name).
    """
    try:
        longest, shortest = await anyio.to_thread.run_sync(
            cache.get, "len", 300, lp.length_extremes
        )
        return {"longest": longest, "shortest": shortest}
    except Exception as e:
        return _err(e)

@mcp.tool
async def get_champion_stats(
    metric: Annotated[
        Literal["picks", "bans", "presence", "winrate"],
        Field(description="Which statistic to rank by. Presence is picks+bans."),
    ] = "picks",
    order: Annotated[
        Literal["desc", "asc"],
        Field(description="desc = most/highest, asc = least/lowest"),
    ] = "desc",
    limit: Annotated[
        int,
        Field(ge=1, le=5, description="How many champions to return. At most 5."),
    ] = 5,
    champion: Annotated[
        str | None,
        Field(description="Provides metrics for only this given champion instead of a ranked list."),
    ] = None,
) -> dict:
    """Returns statistics of champions. Either a list of rankings based on a certain metric, or metrics of one specified champion.

    Use for questions about a metric: most picked, most banned, least picked, least banned, highest presence, lowest presence, highest winrate, lowest winrate.
    Also use when asked for statistics about a specific champion: how many times was [X] picked?

    Picks: how many times the champion has been picked
    Bans: how many times the champion has been banned
    Presence: picks + bans
    Winrate: number of times that champion has won / (wins + losses)

    Single champion look up should return picks, bans, presence and winrate.
    Every champion also carries pick_rate, ban_rate and presence_rate: the
    share of the tournament's games, as a 0-1 fraction (0.7 means 70%).
    games_played is the number of games those rates are out of.
    Returns at most 5 champions.
    """
    try:
        table, games = await anyio.to_thread.run_sync(
            cache.get, "bp", 300, lp.champion_table
        )

        rows = lp.rank(table, metric, order, MIN_GAMES, champion, games_played=games)

        if champion is None:
            rows = rows[:limit]

        return {
            "metric": metric,
            "order": order,
            "games_played": games,
            "champions": rows,
        }
    except Exception as e:
        return _err(e)


@mcp.tool
async def get_matches(
    day: Annotated[
        str | None,
        Field(description="A single date as YYYY-MM-DD, in UTC. Omit for today's games."),
    ] = None,
    team: Annotated[
        str | None,
        Field(description="Team name, code or any part of one - 'T1', 't1', '100T', 'gen' all match. Returns that team's next unplayed match instead of a day's schedule."),
    ] = None,
    stage: Annotated[
        str | None,
        Field(description="A stage or group of stages: 'swiss', 'knockouts', 'quarterfinals', 'semis', 'finals', 'round 3'. Returns every match in it instead of one day's."),
    ] = None,
    limit: Annotated[
        int,
        Field(ge=1, le=20, description="Cap on matches returned. `total` reports how many there really are."),
    ] = 10,
) -> dict:
    """Match schedule and results for the tournament.

    Use for: what games are on today, who plays tomorrow, what happened on a
    given date, when does T1 play next, who did Gen.G lose to, who is in the
    semifinals, how did the Swiss stage go.

    The three filters are alternatives, checked in this order:
        team  - that team's next unplayed match (ignores day and stage)
        stage - every match in that stage
        day   - every match on one date (the default)

    Stage accepts a single stage or a group: "swiss" covers the round-robin
    rounds, "knockouts" / "playoffs" cover quarterfinals through finals.
    Broad stages are truncated to `limit` - compare `count` with `total` and
    tell the user when you are showing only part of it.

    `basis` says which day you are looking at and why:
        requested - the date that was asked for
        today     - there are games today
        next      - nothing today, so the next day that has games
        last      - no unplayed matches remain, so this is the most recent one

    A `last` basis for a team does NOT mean they were eliminated. It only
    means no future match is on the schedule, which also happens when the
    team is still alive and the next round has not been drawn yet. Say their
    next game is not scheduled; never say they are out.

    Each match has: team1, team2, their records as "wins-losses", best_of,
    stage, played, winner (the winning team's name, or null if not yet
    played), start_utc, and start_epoch.

    Render start_epoch as <t:EPOCH:R> so Discord shows it in each reader's
    own timezone - write <t:1762671600:R>, not the bare number.
    """
    try:
        matches = await anyio.to_thread.run_sync(cache.get, "sch", 300, lp.schedule)

        if team:
            # Matches a full name, a short code, or part of either, because
            # Claude passes whatever the user typed: "100T", "100 Thieves"
            # and "thieves" all have to find the same team.
            theirs = [m for m in matches if lp.team_matches(m, team)]
            if not theirs:
                return {"team": team, "count": 0, "matches": [],
                        "note": "no team matched that name"}

            # Their next unplayed match, else the most recent one. "No next
            # match" is reported as basis=last, never as elimination - the
            # schedule cannot tell those apart.
            upcoming = [m for m in theirs if not m["played"]]
            pick = upcoming[0] if upcoming else theirs[-1]
            return {
                "team": team,
                "basis": "next" if upcoming else "last",
                "count": 1,
                "total": 1,
                "matches": [pick],
            }

        if stage:
            names = lp.resolve_stage(matches, stage)
            if not names:
                return {"stage": stage, "count": 0, "total": 0, "matches": [],
                        "note": "no stage matched; known stages are %s"
                                % ", ".join(lp.stages(matches))}
            found = [m for m in matches if m["stage"] in names]
            return {
                "stage": stage,
                "stages": names,
                "basis": "stage",
                "count": len(found[:limit]),
                "total": len(found),
                "matches": found[:limit],
            }

        # resolve_day picks today / next day with games / last day, and says
        # which it chose. That decision is ours to make, not Claude's - it
        # knows nothing about which days have games.
        chosen, basis = lp.resolve_day(matches, day)
        if chosen is None:
            return {"day": None, "basis": "none", "count": 0, "total": 0, "matches": []}

        on_day = lp.matches_on(matches, chosen)
        return {
            "day": chosen,
            "basis": basis,
            "count": len(on_day[:limit]),
            "total": len(on_day),
            "matches": on_day[:limit],
        }
    except Exception as e:
        return _err(e)


@mcp.tool
async def get_standings(
    stage: Annotated[
        str | None,
        Field(description="Which stage to build the table from: 'swiss' (the default), 'all' for the whole tournament, or a specific stage like 'round 3'."),
    ] = "swiss",
) -> dict:
    """Team standings: every team's win-loss record, ordered best first.

    Use for: who is undefeated, who is 2-1, how does the Swiss table look,
    who is at the top or bottom, what is everyone's record.

    Defaults to the Swiss stage, where a record genuinely is the competition.
    Records are near-meaningless in the knockout rounds - every team there is
    0-1 or 1-1 until they are out, which restates the bracket rather than
    ranking anyone. For knockout questions use get_matches instead.

    Each row is: team, code (the short form like T1 or 100T), wins, losses.
    Sorted best record first. wins and losses count MATCHES, not individual
    games - a team that won a Bo5 3-2 has one win, not three.

    current_stage is the earliest stage still being played, and complete says
    whether every match in scope has finished. When complete is true,
    current_stage is the last stage played, not an upcoming one.

    These are played results only. Never say a team has qualified, advanced,
    or been eliminated: this tool has no bracket and no format rules, so a
    3-1 record means three wins and one loss and nothing more. Report the
    records and let the reader draw conclusions.
    """
    try:
        matches = await anyio.to_thread.run_sync(cache.get, "sch", 300, lp.schedule)

        # Scope first, then compute. standings() counts whatever list it is
        # given, so filtering here is what makes "who is 3-0 in Swiss"
        # different from "who is 3-0 overall".
        scope = (stage or "all").strip().lower()
        if scope not in ("all", "overall", "tournament"):
            names = lp.resolve_stage(matches, scope)
            if not names:
                return {"stage": stage, "count": 0, "standings": [],
                        "note": "no stage matched; known stages are %s"
                                % ", ".join(lp.stages(matches))}
            matches = [m for m in matches if m["stage"] in names]

        rows = lp.standings(matches)

        # Codes come from the match rows, which already carry them, so this
        # costs no extra query and keeps "T1 (6-2)" consistent with
        # get_matches.
        codes = {}
        for m in matches:
            codes.setdefault(m["team1"], m.get("team1_code"))
            codes.setdefault(m["team2"], m.get("team2_code"))
        for row in rows:
            row["code"] = codes.get(row["team"])

        return {
            "stage": stage,
            "current_stage": lp.current_stage(matches),
            "complete": all(m["played"] for m in matches) if matches else True,
            "count": len(rows),
            "standings": rows,
        }
    except Exception as e:
        return _err(e)


@mcp.tool
async def get_player_stats(
    metric: Annotated[
        Literal["kills", "deaths", "assists", "kda", "damage", "cs", "vision",
                "champions_played", "pentakills"],
        Field(description="Which statistic to rank by. All are tournament totals except kda, which is a per-player rate."),
    ] = "kills",
    order: Annotated[
        Literal["desc", "asc"],
        Field(description="desc = most/highest, asc = least/lowest"),
    ] = "desc",
    limit: Annotated[
        int,
        Field(ge=1, le=5, description="How many players to return. At most 5."),
    ] = 5,
    player: Annotated[
        str | None,
        Field(description="Return only this player's row instead of a ranked list. Partial names work: 'faker', 'guma'."),
    ] = None,
) -> dict:
    """Player statistics for the tournament: kills, deaths, assists, KDA, damage, CS, vision score, pentakills and champion pool.

    Use for questions about individual players: who has the most kills, who has
    the best KDA, who has played the most champions, how has Faker done, who
    has the most vision score, how many pentakills have there been.

    Totals, summed over every game the player appeared in:
      kills, deaths, assists - their scoreline
      damage                 - damage dealt to champions
      cs                     - creep score
      vision                 - vision score
      pentakills             - may legitimately be 0 for everyone; that is a
                               real answer, not missing data
      champions_played       - how many DIFFERENT champions they played, not
                               how many games

    kda is (kills + assists) / deaths, and is the only rate here. Only players
    with at least 5 games are ranked by it, so one strong game cannot top the
    table. The other metrics have no such cutoff.

    Each row also carries team, role and games played. Returns at most 5 players.
    """
    try:
        table, player_games = await anyio.to_thread.run_sync(
            cache.get, "players", 300, lp.player_table
        )
        rows = lp.rank_players(
            table, metric, order, MIN_GAMES_PLAYER, player, games_played=player_games
        )
        if player is None:
            rows = rows[:limit]

        out = {
            "metric": metric,
            "order": order,
            "players_counted": len(table),
            "players": rows,
        }
        # A ranking where every value is zero is not a ranking - it is five
        # arbitrary players. Say so, or it reads as a leaderboard.
        if rows and order == "desc" and not any(r[metric] for r in rows):
            out["note"] = f"no player has any {metric} in this tournament"
        return out
    except Exception as e:
        return _err(e)


@mcp.tool
async def get_tournament_totals() -> dict:
    """Single-number facts about the tournament as a whole: counts, not rankings.

    Use for "how many" questions: how many games have been played, how many
    different champions have been picked, how many pentakills, how many
    reverse sweeps, how many barons, how long is the average game.

    For "who" or "which" questions - most banned champion, best player,
    team records - use the ranking tools instead.

    Returns:
      games_played, matches_played   a Bo5 is one match and up to five games
      champions_picked               how many DIFFERENT champions were picked
      champions_banned               how many different ones were banned
      champions_used_at_all          picked or banned
      champions_never_picked         banned every time, never played
      total_kills                    across every game
      average_game_length_minutes    a decimal, e.g. 32.8
      five_game_series               how many matches went the distance
      reverse_sweeps                 lost the first two games, won the series;
                                     reverse_sweep_details names them
      objectives                     totals for barons, dragons, towers,
                                     inhibitors, elders, riftheralds,
                                     voidgrubs, atakhans
      pentakills                     may legitimately be 0; that is an answer,
                                     not missing data

    These are objectives SECURED, not stolen - Leaguepedia does not record
    steals, so never report a steal count.
    """
    try:
        totals = await anyio.to_thread.run_sync(
            cache.get, "totals", 300, lp.tournament_totals
        )
        pentakills = await anyio.to_thread.run_sync(
            cache.get, "pentakills", 300, lp.pentakill_count
        )
        return {**totals, "pentakills": pentakills}
    except Exception as e:
        return _err(e)


if __name__ == "__main__":
    mcp.run()