# WX Bot - Discord bot for aviation weather (METAR, TAF, D-ATIS)

import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import aiohttp

# --- Load environment variables ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1356450656310923355  # your Discord server ID

# --- ATIS auto-update settings ---
WX_REPORT_CHANNEL_ID = 1498242952252624917  # #wx-report channel
WATCHED_AIRPORTS = [
    "KPDX",
    # "KSEA",
]
ATIS_POLL_MINUTES = 10

# --- Bot setup ---
# Slash commands only need the 'guilds' intent
intents = discord.Intents.none()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)
guild_obj = discord.Object(id=GUILD_ID)

# --- Shared state ---
# Single aiohttp session reused across all commands and the polling loop
http_session: aiohttp.ClientSession | None = None
# Cached channel reference (resolved once)
wx_report_channel: discord.TextChannel | None = None
# Last seen ATIS info code per airport+type, e.g. {"KPDX:combined": "D"}
last_atis_codes: dict[str, str] = {}

# --- Helpers ---
async def fetch_atis(icao: str):
    """Return parsed atis.info JSON for an ICAO, or None on failure."""
    url = f"https://atis.info/api/{icao}"
    try:
        async with http_session.get(url) as r:
            return await r.json()
    except Exception:
        return None

def format_atis(icao: str, data) -> str | None:
    """Format atis.info response for display. Returns None if no ATIS available."""
    if isinstance(data, dict) and "error" in data:
        return None
    body = "\n\n".join(
        f"**{d['type'].upper()}**\n```\n{d['datis']}\n```" for d in data
    )
    return f"**{icao}**\n{body}"

def extract_codes(data) -> dict[str, str]:
    """Extract {type: code} from atis.info response. Empty dict if unavailable."""
    if isinstance(data, dict) and "error" in data:
        return {}
    return {d["type"]: d.get("code", "") for d in data}

# --- On ready: sync slash commands and start polling ---
@bot.event
async def on_ready():
    global http_session, wx_report_channel
    # Initialize shared HTTP session once
    if http_session is None:
        http_session = aiohttp.ClientSession()
    # Cache channel reference once
    wx_report_channel = bot.get_channel(WX_REPORT_CHANNEL_ID)
    # Clear stale guild commands so we don't have duplicates
    bot.tree.clear_commands(guild=guild_obj)
    await bot.tree.sync(guild=guild_obj)
    # Sync globally for DMs and user installs
    await bot.tree.sync()
    # Start ATIS auto-update loop (idempotent - safe if called twice)
    if not atis_watcher.is_running():
        atis_watcher.start()
    print(f"Logged in as {bot.user}")

# --- /metar: current METAR for a given ICAO ---
@bot.tree.command(
    name="metar",
    description="Get the latest METAR for an airport",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(icao="ICAO code, e.g. KCVO")
async def metar(interaction: discord.Interaction, icao: str):
    await interaction.response.defer()
    icao = icao.upper()
    url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw"
    async with http_session.get(url) as r:
        text = (await r.text()).strip()
    if not text:
        await interaction.followup.send(f"No METAR found for {icao}.")
    else:
        await interaction.followup.send(f"```\n{text}\n```")

# --- /taf: current TAF for a given ICAO ---
@bot.tree.command(
    name="taf",
    description="Get the latest TAF for an airport",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(icao="ICAO code, e.g. KEUG")
async def taf(interaction: discord.Interaction, icao: str):
    await interaction.response.defer()
    icao = icao.upper()
    url = f"https://aviationweather.gov/api/data/taf?ids={icao}&format=raw"
    async with http_session.get(url) as r:
        text = (await r.text()).strip()
    if not text:
        await interaction.followup.send(f"No TAF found for {icao}.")
    else:
        await interaction.followup.send(f"```\n{text}\n```")

# --- /atis: current D-ATIS for a given US airport (via atis.info) ---
@bot.tree.command(
    name="atis",
    description="Get the latest Digital ATIS",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(icao="ICAO code, e.g. KCVO")
async def atis(interaction: discord.Interaction, icao: str):
    await interaction.response.defer()
    icao = icao.upper()
    data = await fetch_atis(icao)
    # API returns a dict with 'error' if the airport has no D-ATIS
    if isinstance(data, dict) and "error" in data:
        await interaction.followup.send(f"No D-ATIS available for {icao}.")
        return
    # Otherwise, response is a list — typically arrival and/or departure
    output = "\n\n".join(
        f"**{d['type'].upper()}**\n```\n{d['datis']}\n```" for d in data
    )
    await interaction.followup.send(output)

# --- Auto-update loop: post ATIS to #wx-report on changes ---
@tasks.loop(minutes=ATIS_POLL_MINUTES)
async def atis_watcher():
    if wx_report_channel is None:
        return
    for icao in WATCHED_AIRPORTS:
        data = await fetch_atis(icao)
        if data is None:
            continue
        current_codes = extract_codes(data)
        # Compare against last seen codes for this airport
        changed = False
        for atis_type, code in current_codes.items():
            key = f"{icao}:{atis_type}"
            if last_atis_codes.get(key) != code:
                changed = True
                last_atis_codes[key] = code
        # Post only if something changed (also true on first run)
        if changed:
            output = format_atis(icao, data)
            if output:
                await wx_report_channel.send(output)

@atis_watcher.before_loop
async def before_atis_watcher():
    await bot.wait_until_ready()




# --- Run the bot ---
bot.run(TOKEN)