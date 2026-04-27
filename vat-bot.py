# vat-bot - Discord bot for aviation weather and VATSIM network data

import os
import math
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

# --- VATSIM settings ---
VATSIM_FEED_URL = "https://data.vatsim.net/v3/vatsim-data.json"
VATSIM_CACHE_SECONDS = 60  # re-fetch feed at most once per minute

# ZSE positions to watch for /controllers
# Prefixes cover Seattle ARTCC: Seattle, Portland, Eugene, Hillsboro
ZSE_PREFIXES = ("SEA_", "PDX_", "EUG_", "HIO_")

# KPDX coordinates for distance calculation (arrivals within 30 min)
# Used in haversine to estimate if an aircraft is inbound within range
AIRPORT_COORDS = {
    "KPDX": (45.5887, -122.5975),
    "KSEA": (47.4502, -122.3088),
    "KEUG": (44.1246, -123.2119),
    "KHIO": (45.5388, -122.9538),
}

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

# VATSIM feed cache — avoids fetching on every command
# vatsim_cache holds the parsed JSON; vatsim_cache_time tracks when it was last fetched
vatsim_cache: dict | None = None
vatsim_cache_time: float = 0.0

# Tracks callsigns already seen in prefiles — used to detect new filings
# Populated on first poll, notifies on subsequent new entries
known_prefiles: set[str] = set()
prefile_initialized: bool = False  # True after first poll, prevents false alerts on startup

# --- Helpers: weather ---
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

# --- Helpers: VATSIM ---
async def fetch_vatsim() -> dict | None:
    """
    Fetch and cache the VATSIM data feed.
    Returns the full parsed JSON, or None on failure.
    Cache is valid for VATSIM_CACHE_SECONDS — avoids redundant fetches
    when multiple commands fire in quick succession.
    """
    global vatsim_cache, vatsim_cache_time
    import time
    now = time.monotonic()
    # Return cached data if still fresh
    if vatsim_cache is not None and (now - vatsim_cache_time) < VATSIM_CACHE_SECONDS:
        return vatsim_cache
    try:
        async with http_session.get(VATSIM_FEED_URL) as r:
            vatsim_cache = await r.json(content_type=None)
            vatsim_cache_time = now
            return vatsim_cache
    except Exception:
        return None

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate great-circle distance in nautical miles between two coordinates.
    Used to determine if an arriving aircraft is within range of the airport.
    Formula: haversine — standard spherical distance calculation.
    """
    R = 3440.065  # Earth radius in nautical miles
    # Convert degrees to radians for trig functions
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    # Core haversine formula
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))

def estimate_minutes_out(dist_nm: float, groundspeed: int) -> float | None:
    """
    Estimate minutes until arrival based on distance and current groundspeed.
    Returns None if groundspeed is too low to be meaningful (taxiing/stopped).
    """
    if groundspeed < 50:
        return None
    return (dist_nm / groundspeed) * 60

def check_altitude_parity(altitude_str: str, heading: float) -> str:
    """
    Check if filed altitude follows the hemispherical rule:
    - Magnetic track 0-179° (eastbound): odd thousands (FL190, FL210, etc.)
    - Magnetic track 180-359° (westbound): even thousands (FL180, FL200, etc.)
    Returns a string flag: 'OK', 'MISMATCH', or 'UNABLE TO CHECK'.
    Note: uses true heading as proxy for magnetic track — close enough for VATSIM.
    """
    try:
        # Strip leading 'FL' if present, then parse as integer
        alt = int(altitude_str.replace("FL", "").strip()) * 100 if "FL" in altitude_str.upper() else int(altitude_str)
        thousands = (alt // 1000)
        is_odd = thousands % 2 != 0
        eastbound = heading < 180
        # Eastbound should be odd, westbound should be even
        if (eastbound and is_odd) or (not eastbound and not is_odd):
            return "✅ OK"
        else:
            return "⚠️ MISMATCH"
    except Exception:
        return "❓ Unable to check"

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
    # Start VATSIM prefile watcher loop
    if not prefile_watcher.is_running():
        prefile_watcher.start()
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

# --- /flightplan: filed flight plan for a callsign ---
@bot.tree.command(
    name="flightplan",
    description="Get the filed flight plan for a VATSIM callsign",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(callsign="VATSIM callsign, e.g. UAL123")
async def flightplan(interaction: discord.Interaction, callsign: str):
    await interaction.response.defer()
    callsign = callsign.upper()
    data = await fetch_vatsim()
    if data is None:
        await interaction.followup.send("Unable to reach VATSIM data feed.")
        return
    # Search pilots list for exact callsign match
    pilot = next((p for p in data.get("pilots", []) if p["callsign"] == callsign), None)
    if pilot is None:
        await interaction.followup.send(f"No active pilot found for `{callsign}`.")
        return
    fp = pilot.get("flight_plan")
    if fp is None:
        await interaction.followup.send(f"`{callsign}` is online but has no flight plan filed.")
        return
    # Build altitude parity check using current heading
    parity = check_altitude_parity(fp.get("altitude", ""), pilot.get("heading", 0))
    # Format and send the flight plan summary
    lines = [
        f"**{callsign}** — {fp.get('aircraft_short', 'N/A')} ({fp.get('flight_rules', '?')})",
        f"**Route:** {fp.get('departure', '?')} → {fp.get('arrival', '?')} (Alt: {fp.get('alternate', 'N/A')})",
        f"**Altitude:** {fp.get('altitude', '?')} ft  |  Parity: {parity}",
        f"**CAS:** {fp.get('cruise_tas', '?')} kts",
        f"**ETD:** {fp.get('deptime', '?')}z  |  ETE: {fp.get('enroute_time', '?')}",
        f"**Squawk:** {fp.get('assigned_transponder', 'N/A')}",
        f"```\n{fp.get('route', 'No route filed')}\n```",
    ]
    await interaction.followup.send("\n".join(lines))

# --- /route: display full filed route for a callsign ---
@bot.tree.command(
    name="route",
    description="Show the full filed route for a VATSIM callsign",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(callsign="VATSIM callsign, e.g. UAL123")
async def route(interaction: discord.Interaction, callsign: str):
    await interaction.response.defer()
    callsign = callsign.upper()
    data = await fetch_vatsim()
    if data is None:
        await interaction.followup.send("Unable to reach VATSIM data feed.")
        return
    pilot = next((p for p in data.get("pilots", []) if p["callsign"] == callsign), None)
    if pilot is None:
        await interaction.followup.send(f"No active pilot found for `{callsign}`.")
        return
    fp = pilot.get("flight_plan")
    if not fp or not fp.get("route"):
        await interaction.followup.send(f"`{callsign}` has no route filed.")
        return
    # Display departure, arrival, and full route string
    output = (
        f"**{callsign}** — {fp.get('departure', '?')} → {fp.get('arrival', '?')}\n"
        f"```\n{fp['route']}\n```"
    )
    await interaction.followup.send(output)

# --- /altitude: filed altitude and parity check for a callsign ---
@bot.tree.command(
    name="altitude",
    description="Check filed altitude and hemispherical rule for a VATSIM callsign",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(callsign="VATSIM callsign, e.g. UAL123")
async def altitude(interaction: discord.Interaction, callsign: str):
    await interaction.response.defer()
    callsign = callsign.upper()
    data = await fetch_vatsim()
    if data is None:
        await interaction.followup.send("Unable to reach VATSIM data feed.")
        return
    pilot = next((p for p in data.get("pilots", []) if p["callsign"] == callsign), None)
    if pilot is None:
        await interaction.followup.send(f"No active pilot found for `{callsign}`.")
        return
    fp = pilot.get("flight_plan")
    if not fp:
        await interaction.followup.send(f"`{callsign}` has no flight plan filed.")
        return
    heading = pilot.get("heading", 0)
    filed_alt = fp.get("altitude", "N/A")
    parity = check_altitude_parity(filed_alt, heading)
    output = (
        f"**{callsign}**\n"
        f"Filed Altitude: `{filed_alt}` ft\n"
        f"Current Heading: `{heading}°`\n"
        f"Hemispherical Rule: {parity}"
    )
    await interaction.followup.send(output)

# --- /traffic: inbound and ground traffic for an airport ---
@bot.tree.command(
    name="traffic",
    description="Show inbound (within 30 min) and departing traffic for an airport",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(icao="ICAO code, e.g. KPDX")
async def traffic(interaction: discord.Interaction, icao: str):
    await interaction.response.defer()
    icao = icao.upper()
    data = await fetch_vatsim()
    if data is None:
        await interaction.followup.send("Unable to reach VATSIM data feed.")
        return
    # Look up airport coordinates for distance calculation
    coords = AIRPORT_COORDS.get(icao)
    if coords is None:
        await interaction.followup.send(f"No coordinates on file for `{icao}`. Supported: {', '.join(AIRPORT_COORDS.keys())}")
        return
    apt_lat, apt_lon = coords
    arrivals = []
    departures = []
    for p in data.get("pilots", []):
        fp = p.get("flight_plan")
        if not fp:
            continue
        gs = p.get("groundspeed", 0)
        # --- Arrivals: aircraft flying toward this airport ---
        if fp.get("arrival") == icao and gs >= 50:
            dist = haversine(p["latitude"], p["longitude"], apt_lat, apt_lon)
            mins = estimate_minutes_out(dist, gs)
            # Only include if estimated arrival is within 30 minutes
            if mins is not None and mins <= 30:
                arrivals.append((mins, p["callsign"], fp.get("aircraft_short", "?"), int(dist), gs))
        # --- Departures: aircraft on the ground at this airport ---
        if fp.get("departure") == icao and gs < 50:
            dist = haversine(p["latitude"], p["longitude"], apt_lat, apt_lon)
            # Only include if within 5nm of airport (actually on the ground there)
            if dist <= 5:
                departures.append((p["callsign"], fp.get("aircraft_short", "?"), fp.get("arrival", "?")))
    # Sort arrivals by time remaining
    arrivals.sort(key=lambda x: x[0])
    lines = [f"**Traffic at {icao}**"]
    if arrivals:
        lines.append("\n**Arrivals (within 30 min)**")
        for mins, cs, ac, dist, gs in arrivals:
            lines.append(f"`{cs}` — {ac} | ~{int(mins)} min | {dist} nm | {gs} kts")
    else:
        lines.append("\n*No arrivals within 30 minutes.*")
    if departures:
        lines.append("\n**Departures (on ground)**")
        for cs, ac, arr in departures:
            lines.append(f"`{cs}` — {ac} → {arr}")
    else:
        lines.append("*No departures on ground.*")
    await interaction.followup.send("\n".join(lines))

# --- /controllers: online ZSE controllers ---
@bot.tree.command(
    name="controllers",
    description="Show online ZSE controllers (SEA, PDX, EUG, HIO)",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def controllers(interaction: discord.Interaction):
    await interaction.response.defer()
    data = await fetch_vatsim()
    if data is None:
        await interaction.followup.send("Unable to reach VATSIM data feed.")
        return
    # Filter controllers whose callsign starts with any ZSE prefix
    zse = [
        c for c in data.get("controllers", [])
        if any(c["callsign"].startswith(pfx) for pfx in ZSE_PREFIXES)
    ]
    if not zse:
        await interaction.followup.send("No ZSE controllers currently online.")
        return
    lines = ["**ZSE Controllers Online**"]
    for c in sorted(zse, key=lambda x: x["callsign"]):
        # logon_time is ISO8601 — show callsign, frequency, and name
        lines.append(f"`{c['callsign']}` — {c.get('frequency', '?')} MHz | {c.get('name', '?')}")
    await interaction.followup.send("\n".join(lines))

# --- /prefiles: show all prefiles for an airport ---
@bot.tree.command(
    name="prefiles",
    description="Show pilots who have filed but not yet connected to VATSIM",
)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(icao="ICAO code, e.g. KPDX")
async def prefiles(interaction: discord.Interaction, icao: str):
    await interaction.response.defer()
    icao = icao.upper()
    data = await fetch_vatsim()
    if data is None:
        await interaction.followup.send("Unable to reach VATSIM data feed.")
        return
    # Filter prefiles where departure or arrival matches the requested ICAO
    results = [
        p for p in data.get("prefiles", [])
        if p.get("flight_plan", {}).get("departure") == icao
        or p.get("flight_plan", {}).get("arrival") == icao
    ]
    if not results:
        await interaction.followup.send(f"No prefiles found for `{icao}`.")
        return
    lines = [f"**Prefiles for {icao}**"]
    for p in results:
        fp = p.get("flight_plan", {})
        dep = fp.get("departure", "?")
        arr = fp.get("arrival", "?")
        ac = fp.get("aircraft_short", "?")
        cs = p.get("callsign", "?")
        # Tag whether this is a departure or arrival prefile
        tag = "DEP" if dep == icao else "ARR"
        lines.append(f"`{cs}` [{tag}] — {ac} | {dep} → {arr}")
    await interaction.followup.send("\n".join(lines))

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

# --- Auto-update loop: notify via DM when new prefile appears out of KPDX ---
@tasks.loop(minutes=5)
async def prefile_watcher():
    """
    Polls VATSIM prefiles every 5 minutes.
    On first run, populates known_prefiles silently (no notifications).
    On subsequent runs, DMs the bot owner for any new KPDX departure prefiles.
    """
    global known_prefiles, prefile_initialized
    data = await fetch_vatsim()
    if data is None:
        return
    # Filter prefiles departing KPDX
    kpdx_prefiles = {
        p["callsign"]: p for p in data.get("prefiles", [])
        if p.get("flight_plan", {}).get("departure") == "KPDX"
    }
    if not prefile_initialized:
        # First run — just record what exists, do not notify
        known_prefiles = set(kpdx_prefiles.keys())
        prefile_initialized = True
        return
    # Find callsigns not seen before
    new_ones = set(kpdx_prefiles.keys()) - known_prefiles
    for cs in new_ones:
        p = kpdx_prefiles[cs]
        fp = p.get("flight_plan", {})
        msg = (
            f"**New Prefile — KPDX**\n"
            f"`{cs}` — {fp.get('aircraft_short', '?')} | "
            f"{fp.get('departure', '?')} → {fp.get('arrival', '?')} | "
            f"Alt: {fp.get('altitude', '?')} ft\n"
            f"```\n{fp.get('route', 'No route filed')}\n```"
        )
        # DM the bot application owner directly
        app_info = await bot.application_info()
        owner = app_info.owner
        await owner.send(msg)
    # Update known set — remove disconnected prefiles, add new ones
    known_prefiles = set(kpdx_prefiles.keys())

@prefile_watcher.before_loop
async def before_prefile_watcher():
    await bot.wait_until_ready()

# --- Run the bot ---
bot.run(TOKEN)