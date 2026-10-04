"""Dev entry point: runs ONLY the /lina cog. Not the production bot.

discord_bot.py fires a Leaguepedia query at import time, which spends rate
limit before the bot is even up. This starts just the cog, so iterating on
/lina costs nothing extra.

    uv run run_lina.py

Syncs to TEST_GUILD_ID only, so commands appear instantly instead of taking
up to an hour to propagate globally.
"""

import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

GUILD = discord.Object(id=int(os.environ["TEST_GUILD_ID"]))


class Dev(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self):
        await self.load_extension("cogs.lina")
        self.tree.copy_global_to(guild=GUILD)
        await self.tree.sync(guild=GUILD)
        logging.info("synced /lina to guild %s", GUILD.id)

    async def on_ready(self):
        logging.info("ready as %s", self.user)


Dev().run(os.environ["client_run_key"], log_handler=None)
