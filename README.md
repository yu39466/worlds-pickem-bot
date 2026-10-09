# Worlds Pick'ems Bot

A Discord bot that answers questions about the League of Legends World
Championship — pick and ban rates, schedules, standings, game-length records —
the categories people bet each other on during Worlds.

It has two interfaces. The original one is a set of slash commands, each
hard-wired to a single query. The newer one is `/lina`, which takes a plain
question and works out which data to fetch:

```
/bp                                  →  top 5 picked and banned champions
/lina who's been banned the most?    →  same data, plus "why?", "how about Ambessa?",
                                        "what about in Swiss?", "who's 3-0?"
```

`/lina` is a Claude agent driving a local **Model Context Protocol** server.
The bot process hosts the server, Claude chooses which tools to call, and the
bot relays the results back.

---

## Why the rewrite

The slash commands worked, but every new question meant a new command, a new
embed builder, and a new deploy. `/mostbanned` could not answer "how about in
the Swiss stage", and `/schedule` could not answer "when does T1 play next".

Moving the data behind an MCP server changed the shape of the problem. Adding a
capability now means writing one typed function with a good docstring; the
agent works out when to use it. Four tools cover what five commands did, and a
great deal they could not.

The architecture has two properties worth calling out:

**The server is host-agnostic.** It speaks stdio MCP, so Claude Desktop can
drive it just as well as the bot can. Every tool was built and tested through
Claude Desktop before any bot code existed.

**The model is swappable.** Claude never touches Leaguepedia; it only sees tool
definitions. Changing model — or provider — means rewriting `cogs/lina.py` and
nothing else.

---

## Architecture

```
Discord
   │  /lina "when does T1 play next?"
   ▼
cogs/lina.py ──────────────► Claude API  (Haiku 4.5)
  MCP host                      │
   │                            │  "call get_matches(team='T1')"
   │ ◄──────────────────────────┘
   │  tools/call over stdio
   ▼
server.py  (FastMCP)
   │
   ▼
sources/ ──► Leaguepedia Cargo API   (picks, bans, schedule, standings)
         └─► LoL Esports API         (live match state — not yet used)
```

The Claude API cannot reach `localhost`, so it never speaks to the MCP server
directly. Every tool call round-trips through the bot, which is what makes the
bot the *host* rather than just a client.

### Layout

| Path | What it is |
|---|---|
| `sources/` | Data layer. Upstream clients, TTL cache, typed errors. No MCP, no Discord. |
| `server.py` | MCP server. Six tools, thin wrappers over `sources/`. |
| `cogs/lina.py` | The `/lina` command. Spawns the server, drives Claude, renders embeds. |
| `cogs/matchup.py` | Composites team logos into one image for match answers. |
| `fixtures/` | A recorded LoL Esports schedule, so tests need no network. |
| `discord_bot.py` | The original bot and its five slash commands. |
| `helpers/` | Data access for the original commands. Nothing new imports it. |
| `check.py` | Dev tool: call any MCP tool from the terminal. |
| `run_lina.py` | Dev entry point: runs `/lina` alone, without the legacy commands. |

---

## The tools

| Tool | Answers |
|---|---|
| `get_champion_stats` | Most/least picked, banned, highest presence, best/worst winrate, one champion's numbers |
| `get_player_stats` | Most kills, best KDA, most damage or vision, biggest champion pool, one player's line |
| `get_matches` | What's on today, a specific date, a stage, or when a team plays next |
| `get_standings` | Win-loss table, defaulting to the Swiss stage |
| `get_tournament_totals` | How many games, unique champions, pentakills, reverse sweeps, barons |
| `get_game_length_extremes` | Longest and shortest games of the tournament |

Schemas are generated from Python type hints and docstrings by FastMCP, so the
docstring *is* the interface — it is what the model reads to decide which tool
to call and with what arguments.

A few deliberate choices in there:

- `Literal` types become JSON-Schema enums, so the model cannot invent a metric
  name or sort order.
- `limit` is capped in the schema rather than the body, so an over-large request
  fails validation instead of returning a 4 KB response.
- Tool failures return `{"error": "rate_limited", "retry_after_s": 30}` rather
  than raising. A typed dict costs ~25 tokens and reads cleanly; a traceback
  costs ~150 and leaks internals.
- Image URLs are deliberately **not** in tool output. The bot resolves them
  locally from names the payload already contains, so the model never pays for
  a string it would only echo back.
- **"Which" and "how many" are separate tools.** `get_champion_stats` ranks
  champions and caps at five rows; "how many different champions were picked"
  is a single number, so it lives in `get_tournament_totals` instead. Raising
  the row cap would not have answered it — the model would have received five
  champions and no total.
- A ranking where every value is zero is not a ranking. When nobody has a
  pentakill, the response says so rather than returning five arbitrary players
  on nil, which reads as a leaderboard.

---

## Data sources

**Leaguepedia** (`lol.fandom.com`), via the Cargo API — everything the bot
currently serves. Complete and historical, but written by editors and bots, so
it lags live play by minutes to hours.

Four quirks worth knowing before touching the queries:

- **The tables disagree on how to name a tournament**, and a wrong column name
  in a `WHERE` clause returns an empty result rather than an error — so the
  symptom is "no data", not a traceback:

  | Table | Keys on |
  |---|---|
  | `ScoreboardGames`, `Pentakills` | `Tournament` — `Worlds 2025 Main Event` |
  | `MatchSchedule`, `ScoreboardPlayers` | `OverviewPage` — `2025 Season World Championship/Main Event` |

  Both names are environment variables.

- **A player's display name is not their wiki page name.** `ScoreboardPlayers`
  has `Name` (`Bin`) and `Link` (`Bin (Chen Ze-Bin)`); 14 of 83 players at
  Worlds 2025 differ. Image lookups key on `Link`.

- **It rate-limits after roughly five queries in quick succession.** The TTL
  cache in `sources/cache.py` is a correctness requirement, not an
  optimisation. Each tool is built to need one query, two at most.

- **The MediaWiki API caps `titles` at 50** and rejects the whole request past
  that, so image lookups are chunked through `_resolve_files()`.

Images come from the same wiki: champion portraits from Riot's Data Dragon,
team logos and player photos from Leaguepedia. `PlayerImages` has no usable
date — `SortDate` is empty on all but a handful of rows — so the year is parsed
out of the tournament name, and a photo from the tournament's own year is
preferred over a newer one. Otherwise a 2026 headshot shows the player in
whichever jersey they wear now, not the one they wore at this event.

**Not available:** objective *steals*. None of Leaguepedia's 91 Cargo tables
record them — `Barons` is how many a team secured. Steals would need Riot's
match timeline, a different upstream entirely.

**LoL Esports** (`esports-api.lolesports.com`) — implemented in
`sources/lolesports.py` but not yet exposed as a tool. It is the only source
for live match state, and it always serves the current season, so it cannot
answer about a past tournament.

---

## Setup

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Create `.env` in the repo root:

```bash
# Leaguepedia — lol.fandom.com/wiki/Special:BotPasswords
# The bot name goes in the PASSWORD, not the username.
WIKI_USERNAME_ME=YourFandomName
WIKI_PASSWORD_ME=yourbotname@thirtytwocharactertoken

# Which tournament. The two names are different on purpose - see Data sources.
page_to_query=Worlds 2025 Main Event
overview_page=2025 Season World Championship/Main Event

# Discord
client_run_key=<bot token>
TEST_GUILD_ID=<server id for instant command sync>

# /lina only
ANTHROPIC_API_KEY=sk-ant-...
LINA_MODEL=claude-haiku-4-5

# Legacy /schedule and /standings only
API_KEY=<x-api-key from lolesports.com devtools>
worlds_id=98767975604431411
```

Check it works:

```bash
uv run check.py                              # list the tools
uv run check.py get_game_length_extremes     # call one
```

---

## Running it

```bash
uv run run_lina.py       # just /lina, syncs to TEST_GUILD_ID
uv run discord_bot.py    # the original slash commands
```

`run_lina.py` exists because `discord_bot.py` fires a Leaguepedia query at
import time — enough to spend the rate limit before the bot is even up, which
makes iterating on `/lina` painful.

### Developing tools

`check.py` is the fast loop. It talks to the server in-process, so there is no
subprocess and no Claude involved, and it prints the tool description exactly
as the model sees it alongside the result and its size in bytes.

Before trusting a tool, run it through the real protocol too:

```bash
uv run fastmcp dev inspector server.py
```

That catches the one class of bug `check.py` cannot: anything written to stdout
corrupts the JSON-RPC stream, so the server logs to stderr only.

Two things to look at on every new tool, because neither will error:

- **Does the description match the data?** Nothing validates a docstring. An
  early version of `get_game_length_extremes` promised `hours:minutes:seconds`
  for a field holding `58:51`, which would have had the bot reporting a
  58-hour game.
- **How big is the response?** `check.py` prints it. Most tools land under
  1 KB; a full Swiss day of matches is the outlier at ~2.3 KB.

### Testing routing in Claude Desktop

The server runs under Claude Desktop as well, which is the cheapest way to find
out whether the model picks the right tool:

```bash
uv run fastmcp install claude-desktop server.py --name lina-worlds
```

The generated config needs two corrections — an absolute path to `uv`, since
Desktop does not load your shell `PATH`, and `--directory` pointing at this repo
so the project's dependencies are on the path:

```json
{
  "mcpServers": {
    "lina-worlds": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "--directory", "/path/to/worlds-pickem-bot", "server.py"]
    }
  }
}
```

Quit Claude Desktop completely and reopen it. When routing goes wrong, the fix
is almost always the docstring, not the code.

---

## Notes

**Cost.** Roughly $0.005 per `/lina` question on Haiku 4.5 — two API calls, one
to pick a tool and one to read the result. Prompt caching does not engage: the
tools and system prompt come to ~2,400 tokens, below Haiku's minimum cacheable
prefix, and padding it to qualify would cost more than it saves.

**Changing `server.py` or `sources/` requires a restart** of anything hosting
the server — the bot and Claude Desktop both spawn it as a subprocess at
startup. `check.py` imports fresh each run and is the exception.

**The legacy commands still work** and still read through `helpers/`. They will
be retired once `/lina` has covered their use for a while; nothing in the new
code path depends on them.
