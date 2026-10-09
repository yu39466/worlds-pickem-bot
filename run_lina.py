"""Dev entry point: runs ONLY the /lina cog. Not the production bot.

discord_bot.py fires a Leaguepedia query at import time, which spends rate
limit before the bot is even up. This starts just the cog, so iterating on
/lina costs nothing extra.

    uv run run_lina.py

Syncs to every server the bot is in. Per-guild syncs appear immediately,
whereas a global sync can take Discord up to an hour to propagate - not much
use when you are showing the thing to someone.
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
log = logging.getLogger("run_lina")

# Servers that should get /lina, as a comma-separated list of ids in .env.
# Leave LINA_GUILDS unset to sync to every server the bot is in.
ALLOWED = {
    int(g) for g in os.getenv("LINA_GUILDS", "").replace(" ", "").split(",") if g
}


class Dev(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self._synced = False

    async def setup_hook(self):
        await self.load_extension("cogs.lina")

    async def on_ready(self):
        # Syncing happens here rather than in setup_hook because the guild
        # list is only populated once the gateway connection is up. on_ready
        # also fires again after a reconnect, hence the guard.
        log.info("ready as %s", self.user)
        if self._synced:
            return
        self._synced = True

        for guild in self.guilds:
            wanted = not ALLOWED or guild.id in ALLOWED
            try:
                if wanted:
                    self.tree.copy_global_to(guild=guild)
                    await self.tree.sync(guild=guild)
                    log.info("synced /lina to %s (%s)", guild.name, guild.id)
                else:
                    # Skipping is not enough: commands already synced to a
                    # guild stay there until they are explicitly cleared.
                    self.tree.clear_commands(guild=guild)
                    await self.tree.sync(guild=guild)
                    log.info("removed /lina from %s (%s)", guild.name, guild.id)
            except discord.HTTPException as e:
                log.warning("sync failed for %s: %s", guild.name, e)

        if not self.guilds:
            log.warning("bot is not in any servers - nothing to sync")


Dev().run(os.environ["client_run_key"], log_handler=None)
