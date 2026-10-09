
import discord
import pprint
from helpers.fetch_data_utils import get_bp, get_schedule_for_league
from helpers.process_data_utils import get_future_matches, get_current_stage, mp_bp, wr_bp
from datetime import datetime, timezone
from discord.ext import commands
import requests
import json
import os
from dotenv import load_dotenv

load_dotenv()

TEST_GUILD_ID = int(os.getenv('TEST_GUILD_ID'))
guild = discord.Object(id=TEST_GUILD_ID)

page_to_query = os.getenv('page_to_query') 
worlds_id = os.getenv('worlds_id') 
client_run_key = os.getenv('client_run_key')


def create_embed(pick_list,string): #creates an embed from a list of picks that needs to be specified whether its banned/picked, and outputs a discord embed structure for it
    if string == 'Picked':
        color = 0x60f542
        spec = 'picks'
    else:
        color = 0xe63030
        spec = 'bans'
    embedpicked = discord.Embed(
        title=f"Statistics of the Most {string} Champions",
        description="",
        color=color
        )
    picked_str = "\n".join([f"**{i + 1}. {name}** — {value} {spec}" for i, (name, value) in enumerate(pick_list)])
    name = str(pick_list[0][0]).split()
    name = "".join(name)
    embedpicked.set_thumbnail(url=f"https://ddragon.leagueoflegends.com/cdn/15.18.1/img/champion/{name}.png")

    embedpicked.add_field(name=f"The Most {string} Champion Currently is: {pick_list[0][0]}", value=picked_str,
                          inline=True)
    return embedpicked


def create_embed_wr(wr_list,string):
    if string == 'Highest':
        color = 0x60f542
    else:
        color = 0xe63030
        wr_list = wr_list[::-1]

    wr_embed = discord.Embed(
        title=f"Statistics of the {string} Winrate Champions",
        description="",
        color=color
        )
    wr_str = "\n".join([f"**{i + 1}. {name}** — {value*100:.2f}% winrate" for i, (name, value) in enumerate(wr_list)])
    name = str(wr_list[0][0]).split()
    name = "".join(name).replace("'", "").replace("&", "")
    wr_embed.set_thumbnail(url=f"https://ddragon.leagueoflegends.com/cdn/15.18.1/img/champion/{name}.png")
    wr_embed.add_field(name=f"The {string} Winrate Champion Currently is: {wr_list[0][0]}", value=wr_str,
                              inline=True)
    return wr_embed

def discord_relative_time(utc_string): #takes a UTC ISO time string (ie 2025-10-22T08:00:00Z) and converts it into a Discord-formatted relative timestamp
    dt = datetime.fromisoformat(utc_string.replace("Z", "+00:00"))

    timestamp = int(dt.timestamp())

    return f"<t:{timestamp}:R>"


def schedule_embed(upcoming_matches): #creates an embed from the data structure returned by the get_future_matches() or get_schedule_for_league() functions

    embed = discord.Embed(
        title=f"Upcoming Matches",
        description="",
        color=0x6A5423
        )

    for match in upcoming_matches:
        teams = match["match"]["teams"]
        team1 = teams[0]
        team2 = teams[1]

        team1_code = team1["code"]
        team2_code = team2["code"]
        team1_score = team1["record"]

        team1_img = team1["image"]
        team2_img = team2["image"]


        #start_time = match["startTime"].replace("T", " ").replace("Z", " UTC")


        field_value = f"{team1_code} vs {team2_code}\nStart: {discord_relative_time(match['startTime'])}"
        embed.set_thumbnail(url=team1_img)

        embed.add_field(name=field_value, value=team1_score, inline=True)
    return embed

def standings_embed():
    embed = discord.Embed(
    title=f"Standings ({get_current_stage()})",
    description="",
    color=0x6A5423
    )
    return embed



if __name__ == '__main__':
    intents = discord.Intents.all()
    client = commands.Bot(command_prefix='/', intents=intents)
    bp_data = get_bp()
    @client.hybrid_command()
    async def bp(ctx: commands.Context):
        data = mp_bp(bp_data)
        most_picked, most_banned = data
        embedpicked = create_embed(most_picked,"Picked")
        embedbanned = create_embed(most_banned,"Banned")
        embedpicked.timestamp = ctx.message.created_at
        embedbanned.timestamp = ctx.message.created_at

        await ctx.send(embed=embedpicked)
        await ctx.send(embed=embedbanned)
    @client.hybrid_command()
    async def wr(ctx: commands.Context):
        data = wr_bp(bp_data)
        highest, lowest = data
        highest = create_embed_wr(highest,"Highest")
        lowest = create_embed_wr(lowest,"Lowest")
        highest.timestamp = ctx.message.created_at
        lowest.timestamp = ctx.message.created_at

        await ctx.send(embed=highest)
        await ctx.send(embed=lowest)

    @client.hybrid_command()
    async def schedule(ctx: commands.Context):
        game_schedule = get_future_matches(get_schedule_for_league(worlds_id))
        await ctx.send(embed=schedule_embed(game_schedule))

    @client.tree.command(name="standings", description="Shows the current standings of the tournament", guild=guild)
    async def standings(interaction: discord.Interaction):
        await interaction.response.send_message(embed=standings_embed())

    @client.event
    async def on_ready():
        guild = discord.Object(id=TEST_GUILD_ID)
        print(f'{client.user} is ready')

        #Sync commands for test server(instant)
        await client.tree.sync(guild=guild)
        print("Guild commands synced!")

        #Sync commands for across all servers (up to 1 hour)
        await client.tree.sync()
        print("Global commands synced!")


    client.run(client_run_key)
