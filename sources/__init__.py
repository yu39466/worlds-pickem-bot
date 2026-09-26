"""Data sources for the Worlds bot.

Two upstreams, deliberately kept apart:

    leaguepedia - picks, bans, winrates, game length, pentakills, rosters,
                  schedule and standings. Complete and historical, but lags
                  live play because editors and bots write it.

    lolesports  - the same schedule plus live match state, always for the
                  current season. Fresh, but cannot serve a past tournament
                  and depends on an unofficial shared API key.

Importing either module does no network I/O. Clients and logins are created
on first query, so an MCP server can start up instantly and report failures
as data rather than dying during handshake.
"""

from . import cache, errors, leaguepedia, lolesports

__all__ = ["cache", "errors", "leaguepedia", "lolesports"]
