# WX Bot - Discord bot for aviation weather (METAR, TAF, D-ATIS)

import os
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import aiohttp

# --- Load environment variables ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1356450656310923355  # your Discord server ID

# --- Bot setup ---
# Slash commands only need the 'guilds' intent
intents = discord.Intents.none()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)
guild_obj = discord.Object(id=GUILD_ID)

# --- On ready: sync slash commands to the guild ---
@bot.event
async def on_ready():
    # One-time cleanup of stale global commands; safe to leave in
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync()
    # Guild-scoped sync (instant propagation)
    await bot.tree.sync(guild=guild_obj)
    print(f"Logged in as {bot.user}")

# --- /metar: current METAR for a given ICAO ---
@bot.tree.command(
    name="metar",
    description="Get the latest METAR for an airport",
    guild=guild_obj,
)
@app_commands.describe(icao="ICAO code, e.g. KPDX")
async def metar(interaction: discord.Interaction, icao: str):
    await interaction.response.defer()
    icao = icao.upper()
    url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as r:
            text = (await r.text()).strip()
    if not text:
        await interaction.followup.send(f"No METAR found for {icao}.")
    else:
        await interaction.followup.send(f"```\n{text}\n```")

# --- /taf: current TAF for a given ICAO ---
@bot.tree.command(
    name="taf",
    description="Get the latest TAF for an airport",
    guild=guild_obj,
)
@app_commands.describe(icao="ICAO code, e.g. KPDX")
async def taf(interaction: discord.Interaction, icao: str):
    await interaction.response.defer()
    icao = icao.upper()
    url = f"https://aviationweather.gov/api/data/taf?ids={icao}&format=raw"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as r:
            text = (await r.text()).strip()
    if not text:
        await interaction.followup.send(f"No TAF found for {icao}.")
    else:
        await interaction.followup.send(f"```\n{text}\n```")

# --- /atis: current D-ATIS for a given US airport (via atis.info) ---
@bot.tree.command(
    name="atis",
    description="Get the latest D-ATIS for a US airport",
    guild=guild_obj,
)
@app_commands.describe(icao="ICAO code, e.g. KPDX")
async def atis(interaction: discord.Interaction, icao: str):
    await interaction.response.defer()
    icao = icao.upper()
    url = f"https://atis.info/api/{icao}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as r:
            data = await r.json()
    # API returns a dict with 'error' if the airport has no D-ATIS
    if isinstance(data, dict) and "error" in data:
        await interaction.followup.send(f"No D-ATIS available for {icao}.")
        return
    # Otherwise, response is a list — typically arrival and/or departure
    output = "\n\n".join(
        f"**{d['type'].upper()}**\n```\n{d['datis']}\n```" for d in data
    )
    await interaction.followup.send(output)

# --- Run the bot ---
bot.run(TOKEN)