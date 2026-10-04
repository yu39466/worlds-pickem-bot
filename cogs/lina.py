"""The /lina command: a natural-language front end over the MCP server.

This process is the MCP *host*. It spawns server.py as a child over stdio,
hands Claude the tool list, and lets Claude decide which tools to call. The
Claude API never talks to the MCP server directly - it cannot reach
localhost - so every tool call round-trips through here.

Lives in a cog so discord_bot.py needs only one line to pick it up, and so
run_lina.py can load it alone while iterating (starting the full bot fires
a Leaguepedia query at import, which burns rate limit you need for testing).
"""

import asyncio
import io
import json
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import discord
from anthropic import (
    APIConnectionError,
    APIStatusError,
    AsyncAnthropic,
    RateLimitError,
)
from anthropic.lib.tools.mcp import async_mcp_tool
from discord import app_commands
from discord.ext import commands
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from sources import cache
from sources import leaguepedia as lp

from . import matchup

log = logging.getLogger("lina.cog")

ROOT = Path(__file__).resolve().parent.parent
MODEL = os.getenv("LINA_MODEL", "claude-haiku-4-5")

# Discord hard-caps a message at 2000 characters. Asking for less leaves room
# for the model to overshoot slightly without being cut mid-sentence.
DISCORD_LIMIT = 2000

SYSTEM = """You are Lina, a League of Legends Worlds assistant in a Discord server.

Answer only from tool results. Never invent a score, date, champion statistic
or record - if the tools cannot answer, say so plainly.

You know nothing about this tournament except what the tools return. So never
say that a team, champion or match does not exist - look it up first. Team
names match loosely, so a partial name or a code ("Flying Oyster", "CFO",
"oyster") is enough; pass whatever the user wrote. If a lookup comes back
empty, say you could not find it and suggest they check the spelling. Never
describe your tools or list what you cannot do.

Format for Discord, not for a document:
- Under 1200 characters. Be direct; no preamble, no "great question".
- Plain text and short bullet lists. No markdown headers, no tables.
- Render any start_epoch as a Discord timestamp: <t:EPOCH:R> for matches not
  yet played ("in 2 hours"), <t:EPOCH:f> for ones already played.
- Write records next to team names like "T1 (6-2)".
- Percentages: presence_rate 0.887 is "89%".

If a tool result has an `error` field, explain it in one friendly sentence and
stop. Do not call that tool again.

Never claim a team has qualified, advanced or been eliminated. The tools
report played results only, with no bracket or format rules behind them."""


EMBED_COLOR = 0x6A5423
MATCHUP_FILENAME = "matchup.png"


@dataclass
class Answer:
    text: str
    images: list[str] = field(default_factory=list)


def _render(answer: "Answer") -> tuple[list[discord.Embed], discord.File | None]:
    """Build the embed, plus an attachment when there is more than one logo.

    Discord's two picture slots are different sizes, so two logos placed one
    in each come out mismatched. Several logos are instead drawn into a single
    PNG and attached; the embed points at it with the attachment:// scheme.
    """
    embed = discord.Embed(description=answer.text[:4096], color=EMBED_COLOR)

    if len(answer.images) == 1:
        embed.set_thumbnail(url=answer.images[0])
        return [embed], None

    if len(answer.images) > 1:
        png = matchup.compose(answer.images)
        if png:
            embed.set_image(url=f"attachment://{MATCHUP_FILENAME}")
            return [embed], discord.File(io.BytesIO(png), filename=MATCHUP_FILENAME)
        # Compositing failed (a logo would not download). Fall back to one.
        embed.set_thumbnail(url=answer.images[0])

    return [embed], None


class LinaCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.claude = AsyncAnthropic()
        self._stack: AsyncExitStack | None = None
        self.mcp: ClientSession | None = None
        self.tools: list = []

    # ---- MCP lifecycle ----------------------------------------------------

    async def cog_load(self):
        await self._connect()

    async def cog_unload(self):
        if self._stack:
            await self._stack.aclose()
            self._stack = None

    async def _connect(self):
        """Spawn server.py and build the tool list Claude will see.

        One session for the bot's lifetime: a per-question subprocess would
        add an interpreter start and a Leaguepedia login to every query, and
        would throw away the server's cache each time.
        """
        if self._stack:
            await self._stack.aclose()
        self._stack = AsyncExitStack()

        params = StdioServerParameters(
            command="uv",
            args=["run", "--directory", str(ROOT), "server.py"],
            env=dict(os.environ),
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.mcp = await self._stack.enter_async_context(ClientSession(read, write))
        await self.mcp.initialize()

        listed = sorted((await self.mcp.list_tools()).tools, key=lambda t: t.name)
        # Sorted so the rendered tool list is byte-identical between calls -
        # the prompt cache is a prefix match, and an unstable order would
        # silently invalidate it on every request.
        self.tools = [async_mcp_tool(t, self.mcp) for t in listed]
        log.info("MCP connected: %s", [t.name for t in listed])

    # ---- the agent call ---------------------------------------------------

    async def _images_for(self, payloads: list[dict]) -> list[str]:
        """Pick the picture(s) that go with an answer, from what the tools returned.

        Champion rankings get the top champion's portrait; anything about
        matches gets both teams' logos; a standings table gets the leader's.
        The names are already in the payloads, so none of this costs tokens -
        the tools return no image URLs and never need to.
        """
        for data in payloads:
            champs = data.get("champions")
            if champs:
                url = await anyio.to_thread.run_sync(lp.icon_url, champs[0]["champion"])
                return [url] if url else []

        wanted: list[str] = []
        for data in payloads:
            if data.get("matches"):
                first = data["matches"][0]
                wanted = [first.get("team1"), first.get("team2")]
                break
            if data.get("standings"):
                wanted = [data["standings"][0].get("team")]
                break

        wanted = [t for t in wanted if t]
        if not wanted:
            return []
        logos = await anyio.to_thread.run_sync(
            cache.get, "logos", 3600, lambda: lp.team_logos(wanted)
        )
        return [logos[t] for t in wanted if t in logos]

    @staticmethod
    def _payload(block) -> dict | None:
        """Unwrap a tool_result block into the dict the tool actually returned.

        The content arrives as [{"type": "text", "text": "<json>"}] - the
        payload is JSON inside a string, not a nested object.
        """
        content = block.get("content")
        if isinstance(content, str):
            raw = content
        elif isinstance(content, list):
            raw = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    async def ask(self, question: str, *, _retry: bool = True) -> "Answer":
        kwargs = dict(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM,
            tools=self.tools,
            max_iterations=6,
            # Caches the tools + system prefix. Everything above this point is
            # identical on every request, so after the first call it is billed
            # at a fraction. Watch cache_read_input_tokens to confirm.
            cache_control={"type": "ephemeral"},
            messages=[{"role": "user", "content": question}],
        )
        if MODEL.startswith("claude-sonnet-5"):
            kwargs["output_config"] = {"effort": "low"}   # 400s on Haiku 4.5

        # Driven by hand rather than until_done(), because until_done()
        # swallows the tool results and those are where the champion and team
        # names live - the ones the pictures are chosen from.
        payloads: list[dict] = []
        final = None
        try:
            runner = self.claude.beta.messages.tool_runner(**kwargs)
            async for message in runner:
                final = message
                response = await runner.generate_tool_call_response()
                if response is None:
                    continue
                for block in response["content"]:
                    if block.get("type") != "tool_result":
                        continue
                    data = self._payload(block)
                    if data:
                        payloads.append(data)
        except (BrokenPipeError, ConnectionError, OSError) as e:
            # The MCP child died. Respawn once, then give up rather than loop.
            if not _retry:
                raise
            log.warning("MCP session lost (%s); reconnecting", e)
            await self._connect()
            return await self.ask(question, _retry=False)

        if final is None:
            return Answer("I couldn't put together an answer for that.", [])
        if final.stop_reason == "refusal":
            return Answer("I can't help with that one.", [])

        usage = final.usage
        log.info(
            "model=%s in=%s cached=%s out=%s tools=%s",
            MODEL, usage.input_tokens,
            getattr(usage, "cache_read_input_tokens", None), usage.output_tokens,
            len(payloads),
        )

        text = "".join(b.text for b in final.content if b.type == "text").strip()
        if not text:
            return Answer("I couldn't put together an answer for that.", [])

        try:
            images = await self._images_for(payloads)
        except Exception:
            log.exception("image lookup failed")
            images = []               # decoration only - never lose the answer
        return Answer(text, images)

    # ---- the command ------------------------------------------------------

    @app_commands.command(name="lina", description="Ask anything about Worlds")
    @app_commands.describe(question="e.g. who plays today? most banned champ? who's 3-0?")
    @app_commands.checks.cooldown(1, 15.0, key=lambda i: i.user.id)
    async def lina(self, interaction: discord.Interaction, question: str):
        # Discord drops the interaction if nothing responds within 3 seconds,
        # and a Leaguepedia query alone can take longer than that.
        await interaction.response.defer(thinking=True)

        problem = None
        try:
            answer = await asyncio.wait_for(self.ask(question), timeout=60)
        except asyncio.TimeoutError:
            problem = "That took too long — Leaguepedia may be slow. Try again in a moment."
        except RateLimitError:
            problem = "I'm being rate-limited right now. Give me about 30 seconds."
        except (APIStatusError, APIConnectionError) as e:
            log.warning("claude unavailable: %s", e)
            problem = "I can't reach my brain right now. Try again shortly."
        except Exception:
            log.exception("/lina failed")
            problem = "Something broke on my side. It's been logged."

        try:
            if problem:
                # Failures go out as plain text: an error dressed up in a
                # styled card reads as though it were content.
                await interaction.followup.send(problem)
            else:
                embeds, attachment = await anyio.to_thread.run_sync(_render, answer)
                if attachment:
                    await interaction.followup.send(embeds=embeds, file=attachment)
                else:
                    await interaction.followup.send(embeds=embeds)
        except discord.NotFound:
            log.warning("interaction expired before the answer was ready")

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"Slow down a sec — try again in {error.retry_after:.0f}s."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        log.exception("unhandled app command error", exc_info=error)


async def setup(bot: commands.Bot):
    await bot.add_cog(LinaCog(bot))
