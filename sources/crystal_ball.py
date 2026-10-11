"""The pick'em "crystal ball" questions, answered from current data.

The questions change every year, so they live here as data rather than as
code: each is a dict with an id, the question text, and a resolver that reads
an already-fetched snapshot. Next October this list gets rewritten and the
engine below does not.

Every resolver returns a status as well as an answer, because "Yunara leads
presence" and "T1 won" are different kinds of fact:

    settled       decided; cannot change
    leading       current frontrunner, still movable
    open          has not happened yet, but still could
    undetermined  not knowable until a later stage

Without that distinction a day-two leader reads as a final answer.
"""

from . import cache
from . import leaguepedia as lp

# Career thresholds the pick'em poses, with the baseline it states entering
# the tournament. Deliberately their figure, not Leaguepedia's: the two
# disagree (616 vs 609 for Faker) and the bet settles against theirs.
CAREER_MILESTONES = {
    "Faker": {"stat": "kills", "baseline": 616, "target": 700},
}

MINUTE_SPLIT = 32
KILLS_IN_A_GAME = 15
CS_IN_A_GAME = 450
HEAD_TO_HEAD = ("Peyz", "Gumayusi")
WATCHED_CHAMPION = "Teemo"
WATCHED_REGION = "North America"


def snapshot():
    """Fetch everything the questions need, once, through the shared cache.

    Resolvers read this rather than querying, so eighteen questions cost the
    same handful of queries a few individual ones would - Leaguepedia starts
    refusing at about five in quick succession.
    """
    champions, games = cache.get("bp", 300, lp.champion_table)
    players, _ = cache.get("players", 300, lp.player_table)
    matches = cache.get("sch", 300, lp.schedule)
    return {
        "champions": champions,
        "games_played": games,
        "champion_play": cache.get("champ_play", 300, lp.champion_player_stats),
        "players": players,
        "totals": cache.get("totals", 300, lp.tournament_totals),
        "matches": matches,
        "placements": cache.get("placements", 300, lambda: lp.team_placements(matches)),
        "mvp": cache.get("mvp", 300, lambda: lp.finals_mvp(matches)),
        "extremes": cache.get("len", 300, lp.length_extremes),
    }


# --- helpers ---------------------------------------------------------------

def _champ_top(ctx, metric, min_games=5):
    rows = lp.rank(ctx["champions"], metric, "desc", min_games,
                   games_played=ctx["games_played"], play_stats=ctx["champion_play"])
    return rows[0] if rows else None


def _player_top(ctx, metric, min_games=5):
    rows = lp.rank_players(ctx["players"], metric, "desc", min_games)
    return rows[0] if rows else None


def _started(ctx):
    return ctx["games_played"] > 0


def _finished(ctx):
    return ctx["placements"].get("complete") and ctx["games_played"] > 0


def _leading(ctx, text):
    """A frontrunner is settled only once nothing more can be played."""
    return {"answer": text, "status": "settled" if _finished(ctx) else "leading"}


def _still_possible(ctx, text):
    """A "hasn't happened" answer is only open while games remain."""
    if _finished(ctx):
        return {"answer": "no — %s" % text, "status": "settled"}
    return {"answer": "not yet — %s" % text, "status": "open"}


def _not_started():
    return {"answer": None, "status": "undetermined",
            "note": "no games played yet"}


# --- resolvers -------------------------------------------------------------

def _presence(ctx):
    if not _started(ctx):
        return _not_started()
    top = _champ_top(ctx, "presence", 1)
    return _leading(ctx, "%s — %s%% of games" % (
        top["champion"], round((top["presence_rate"] or 0) * 100)))


def _winrate(ctx):
    if not _started(ctx):
        return _not_started()
    top = _champ_top(ctx, "winrate")
    if not top:
        return {"answer": None, "status": "undetermined",
                "note": "no champion has 5 games yet"}
    return _leading(ctx, "%s — %s%% over %s games" % (
        top["champion"], round(top["winrate"] * 100), top["games"]))


def _champ_deaths(ctx):
    if not _started(ctx):
        return _not_started()
    top = _champ_top(ctx, "deaths_per_game")
    if not top:
        return {"answer": None, "status": "undetermined",
                "note": "no champion has 5 games yet"}
    return _leading(ctx, "%s — %s deaths per game" % (
        top["champion"], top["deaths_per_game"]))


def _player_metric(metric, label, fmt=str):
    def resolve(ctx):
        if not _started(ctx):
            return _not_started()
        top = _player_top(ctx, metric)
        if not top:
            return {"answer": None, "status": "undetermined",
                    "note": "no player has 5 games yet"}
        return _leading(ctx, "%s (%s) — %s %s" % (
            top["player"], top["team"], fmt(top[metric]), label))
    return resolve


def _head_to_head(ctx):
    left, right = HEAD_TO_HEAD
    rows = {p.lower(): v for p, v in ctx["players"].items()}
    a, b = rows.get(left.lower()), rows.get(right.lower())
    if not a or not b:
        missing = left if not a else right
        return {"answer": None, "status": "undetermined",
                "note": "%s has not played in this tournament" % missing}
    if a["kills"] == b["kills"]:
        return _leading(ctx, "level — both on %s kills" % a["kills"])
    leader, other = ((left, a), (right, b)) if a["kills"] > b["kills"] else ((right, b), (left, a))
    return _leading(ctx, "%s — %s kills to %s" % (
        leader[0], leader[1]["kills"], other[1]["kills"]))


def _career_milestone(ctx):
    name, rule = next(iter(CAREER_MILESTONES.items()))
    target, baseline = rule["target"], rule["baseline"]
    needed = target - baseline
    stat = rule["stat"]
    row = next((v for k, v in ctx["players"].items() if k.lower() == name.lower()), None)
    so_far = row[stat] if row else 0
    if so_far >= needed:
        return {"answer": "yes — %s of %s needed, now past %s career"
                          % (so_far, needed, target),
                "status": "settled"}
    return _still_possible(ctx, "%s of %s %s needed" % (so_far, needed, stat))


def _watched_champion(ctx):
    rows = lp.rank(ctx["champions"], champion=WATCHED_CHAMPION,
                   games_played=ctx["games_played"])
    picks = rows[0]["picks"] if rows else 0
    if picks:
        return {"answer": "yes — picked %s time%s" % (picks, "" if picks == 1 else "s"),
                "status": "settled"}
    return _still_possible(ctx, "0 picks")


def _unique_champions(ctx):
    if not _started(ctx):
        return _not_started()
    return _leading(ctx, "%s so far" % ctx["totals"]["champions_picked"])


def _minute_split(ctx):
    if not _started(ctx):
        return _not_started()
    under = ctx["totals"]["games_under_32_minutes"]
    over = ctx["totals"]["games_32_minutes_or_over"]
    if under == over:
        text = "level — %s each side of %s minutes" % (under, MINUTE_SPLIT)
    else:
        side = "before" if under > over else "after"
        text = "%s — %s before, %s after" % (side, under, over)
    return _leading(ctx, text)


def _single_game_record(metric, threshold, label):
    def resolve(ctx):
        if not _started(ctx):
            return _not_started()
        top = _player_top(ctx, metric, 1)
        best = top[metric] if top else 0
        if best >= threshold:
            return {"answer": "yes — %s with %s" % (top["player"], best),
                    "status": "settled"}
        return _still_possible(ctx, "best is %s with %s" % (
            top["player"] if top else "nobody", best))
    return resolve


def _mvp_role(ctx):
    mvp = ctx["mvp"]
    if not mvp:
        return {"answer": None, "status": "undetermined",
                "note": "decided after the final"}
    return {"answer": "%s — %s" % (mvp["role"] or "unknown role", mvp["player"]),
            "status": "settled"}


def _knockout_regions(ctx):
    qualified = [t for t in ctx["placements"]["teams"] if t["made_knockouts"]]
    if not qualified:
        return {"answer": None, "status": "undetermined",
                "note": "no team has reached the knockout stage yet"}
    regions = sorted({t["region"] for t in qualified if t["region"]})
    return {"answer": "%s — %s" % (len(regions), ", ".join(regions)),
            "status": "settled" if _finished(ctx) else "leading"}


def _region_in_knockouts(ctx):
    qualified = [t for t in ctx["placements"]["teams"] if t["made_knockouts"]]
    if not qualified:
        return {"answer": None, "status": "undetermined",
                "note": "no team has reached the knockout stage yet"}
    theirs = [t["team"] for t in qualified if t["region"] == WATCHED_REGION]
    if theirs:
        return {"answer": "yes — %s" % ", ".join(theirs), "status": "settled"}
    if _finished(ctx):
        return {"answer": "no", "status": "settled"}
    return {"answer": "not so far", "status": "open"}


def _team_vs_team(ctx):
    left, right = "Hanwha Life Esports", "T1"
    rows = {t["team"]: t for t in ctx["placements"]["teams"]}
    a, b = rows.get(left), rows.get(right)
    if not a or not b:
        return {"answer": None, "status": "undetermined",
                "note": "both teams have not played yet"}
    if not _finished(ctx):
        return {"answer": "%s at %s, %s at %s" % (
            left, a["furthest_stage"], right, b["furthest_stage"]),
            "status": "leading"}
    order = {s: i for i, s in enumerate(lp.stages(ctx["matches"]))}
    if order.get(a["furthest_stage"], -1) == order.get(b["furthest_stage"], -1):
        return {"answer": "level — both out in %s" % a["furthest_stage"],
                "status": "settled"}
    higher = left if order[a["furthest_stage"]] > order[b["furthest_stage"]] else right
    return {"answer": "%s — %s" % (higher, rows[higher]["finish"]), "status": "settled"}


def _shortest_game_winner(ctx):
    _, shortest = ctx["extremes"]
    if not shortest:
        return _not_started()
    return _leading(ctx, "%s — %s against %s" % (
        shortest["winner"], shortest["length"],
        shortest["team2"] if shortest["winner"] == shortest["team1"] else shortest["team1"]))


# --- the question set ------------------------------------------------------

QUESTIONS = [
    ("champion_presence", "Which champion will have the highest presence?", _presence),
    ("champion_winrate", "Which champion will have the highest win rate (min 5 games)?", _winrate),
    ("champion_deaths", "Which champion will have the highest average deaths per game (min 5 games)?", _champ_deaths),
    ("player_kda", "Which pro will finish with the highest KDA?",
     _player_metric("kda", "KDA")),
    ("player_damage", "Which pro will finish with the highest average damage per game?",
     _player_metric("damage_per_game", "per game", lambda v: format(int(v), ","))),
    ("player_kp", "Which pro will finish with the highest kill participation?",
     _player_metric("kill_participation", "participation", lambda v: "%s%%" % round(v * 100))),
    ("head_to_head", "%s vs %s: who will finish with more kills?" % HEAD_TO_HEAD, _head_to_head),
    ("career_milestone", "Will Faker reach 700 career Worlds kills?", _career_milestone),
    ("teemo", "Will %s be picked at least once?" % WATCHED_CHAMPION, _watched_champion),
    ("unique_champions", "How many unique champions will be picked?", _unique_champions),
    ("minute_split", "Will more games finish before or after %s minutes?" % MINUTE_SPLIT, _minute_split),
    ("fifteen_kills", "Will any pro get at least %s kills in a single game?" % KILLS_IN_A_GAME,
     _single_game_record("best_kills_in_a_game", KILLS_IN_A_GAME, "kills")),
    ("big_cs", "Will any pro reach %s CS in a single game?" % CS_IN_A_GAME,
     _single_game_record("best_cs_in_a_game", CS_IN_A_GAME, "CS")),
    ("mvp_role", "What role will the Finals MVP play?", _mvp_role),
    ("knockout_regions", "How many regions will be represented in knockouts?", _knockout_regions),
    ("na_knockouts", "Will a %s team reach knockouts?" % WATCHED_REGION, _region_in_knockouts),
    ("hle_vs_t1", "Which team will place higher: HLE or T1?", _team_vs_team),
    ("shortest_game", "Which team will win the shortest game?", _shortest_game_winner),
]


def answer_all():
    """Every question with its current answer and status.

    A resolver that raises is reported as unavailable rather than taking the
    whole summary down with it - a rate limit on one lookup should not cost
    the other seventeen answers.
    """
    try:
        ctx = snapshot()
    except Exception as e:
        return {"error": type(e).__name__, "questions": []}

    out = []
    for key, text, resolve in QUESTIONS:
        try:
            result = resolve(ctx)
        except Exception as e:
            result = {"answer": None, "status": "unavailable",
                      "note": type(e).__name__}
        out.append({"id": key, "question": text, **result})

    counts = {}
    for row in out:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "games_played": ctx["games_played"],
        "tournament_complete": bool(ctx["placements"].get("complete")) and ctx["games_played"] > 0,
        "status_counts": counts,
        "questions": out,
    }
