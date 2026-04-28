# vat-bot - Discord bot for aviation weather and VATSIM network data

import os
import math
import time
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import aiohttp

# --- Load environment variables ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1356450656310923355  # your Discord server ID

# ===========================================================================
# CONFIGURATION
# ===========================================================================

# ATIS auto-update settings
WX_REPORT_CHANNEL_ID = 1498242952252624917  # #wx-report channel
WATCHED_AIRPORTS = ["KPDX"]  # "KSEA" commented out
ATIS_POLL_MINUTES = 10

# VATSIM data feed
VATSIM_FEED_URL = "https://data.vatsim.net/v3/vatsim-data.json"
VATSIM_STATS_URL = "https://api.vatsim.net/v2/members/{cid}/stats"
VATSIM_CACHE_SECONDS = 60

# ZSE positions — all for /controllers command
ZSE_PREFIXES = ("SEA_", "PDX_", "EUG_", "HIO_")

# Positions affecting KPDX and KSEA — for controller watcher DMs only
WATCHED_CONTROLLER_PREFIXES = ("PDX_", "SEA_")

# Airport coordinates for haversine distance calculation
AIRPORT_COORDS = {
    "KPDX": (45.5887, -122.5975),
    "KSEA": (47.4502, -122.3088),
    "KEUG": (44.1246, -123.2119),
    "KHIO": (45.5388, -122.9538),
}

# ===========================================================================
# BOT SETUP
# ===========================================================================

intents = discord.Intents.none()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)
guild_obj = discord.Object(id=GUILD_ID)

# ===========================================================================
# SHARED STATE
# ===========================================================================

# Single aiohttp session reused across all commands and polling loops
http_session: aiohttp.ClientSession | None = None

# Cached channel reference — resolved once on startup
wx_report_channel: discord.TextChannel | None = None

# Last seen ATIS info code per airport+type e.g. {"KPDX:combined": "D"}
last_atis_codes: dict[str, str] = {}

# VATSIM feed cache
vatsim_cache: dict | None = None
vatsim_cache_time: float = 0.0

# Prefile watcher state
known_prefiles: set[str] = set()
prefile_initialized: bool = False

# Controller watcher state — callsigns currently online
known_controllers: set[str] = set()
controller_initialized: bool = False

# ===========================================================================
# HELPERS: WEATHER
# ===========================================================================

async def fetch_atis(icao: str):
    """Return parsed atis.info JSON for an ICAO, or None on failure."""
    try:
        async with http_session.get(f"https://atis.info/api/{icao}") as r:
            return await r.json()
    except Exception:
        return None

def format_atis(icao: str, data) -> str | None:
    """Format atis.info response for display. Returns None if unavailable."""
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

# ===========================================================================
# HELPERS: VATSIM
# ===========================================================================

async def fetch_vatsim() -> dict | None:
    """
    Fetch and cache the VATSIM data feed.
    Returns full parsed JSON or None on failure.
    Cache is valid for VATSIM_CACHE_SECONDS.
    """
    global vatsim_cache, vatsim_cache_time
    now = time.monotonic()
    if vatsim_cache is not None and (now - vatsim_cache_time) < VATSIM_CACHE_SECONDS:
        return vatsim_cache
    try:
        async with http_session.get(VATSIM_FEED_URL) as r:
            vatsim_cache = await r.json(content_type=None)
            vatsim_cache_time = now
            return vatsim_cache
    except Exception:
        return None

async def fetch_pilot_stats(cid: int) -> dict | None:
    """
    Fetch pilot and ATC hours from VATSIM Core API by CID.
    Returns parsed stats JSON or None on failure.
    Public endpoint — no authentication required.
    """
    try:
        async with http_session.get(VATSIM_STATS_URL.format(cid=cid)) as r:
            return await r.json(content_type=None)
    except Exception:
        return None

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles between two lat/lon pairs."""
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))

def estimate_minutes_out(dist_nm: float, groundspeed: int) -> float | None:
    """ETA in minutes based on distance and groundspeed. None if on ground."""
    if groundspeed < 50:
        return None
    return (dist_nm / groundspeed) * 60

async def format_flightplan(pilot: dict, is_prefile: bool) -> str:
    """
    Shared compact flight plan formatter used by /flightplan, /prefiles,
    and the prefile watcher DM.
    Fetches pilot/ATC hours from VATSIM stats API using the pilot's CID.
    """
    fp = pilot.get("flight_plan", {})
    cid = pilot.get("cid")

    # Fetch pilot stats — one extra async call per entry
    stats = await fetch_pilot_stats(cid) if cid else None
    pilot_hrs = f"{int(stats['pilot'])}h" if stats else "N/A"
    atc_hrs = f"{int(stats['atc'])}h" if stats else "N/A"

    # Header line — callsign, prefile tag, FAA equipment string, flight rules
    tag = "  ⚠️ PREFILE" if is_prefile else ""
    line1 = f"`{pilot.get('callsign', '?')}`{tag} | {fp.get('aircraft_faa', '?')} ({fp.get('flight_rules', '?')}) | {fp.get('departure', '?')}→{fp.get('arrival', '?')} | {fp.get('altitude', '?')}"

    # Timing and alternate
    line2 = f"ETD {fp.get('deptime', '?')}z  ETE {fp.get('enroute_time', '?')}  Fuel {fp.get('fuel_time', '?')}  Alt {fp.get('alternate', 'N/A')}"

    # Route
    route_str = fp.get('route', 'No route filed')
    line3 = f"Route: {route_str}"

    # Remarks — truncate at 200 chars to keep message short
    remarks = fp.get('remarks', '')
    if remarks:
        remarks = remarks[:200] + ('…' if len(remarks) > 200 else '')
        line4 = f"Remarks: {remarks}"
    else:
        line4 = None

    # Pilot stats
    line5 = f"Pilot {pilot_hrs}  ATC {atc_hrs}"

    lines = [line1, line2, line3]
    if line4:
        lines.append(line4)
    lines.append(line5)
    lines.append("―" * 30)  # separator between entries

    return "\n".join(lines)

# ===========================================================================
# ON READY
# ===========================================================================

@bot.event
async def on_ready():
    global http_session, wx_report_channel
    if http_session is None:
        http_session = aiohttp.ClientSession()
    wx_report_channel = bot.get_channel(WX_REPORT_CHANNEL_ID)
    bot.tree.clear_commands(guild=guild_obj)
    await bot.tree.sync(guild=guild_obj)
    await bot.tree.sync()
    if not atis_watcher.is_running():
        atis_watcher.start()
    if not prefile_watcher.is_running():
        prefile_watcher.start()
    if not controller_watcher.is_running():
        controller_watcher.start()
    print(f"Logged in as {bot.user}")

# ===========================================================================
# COMMANDS: WEATHER
# ===========================================================================

@bot.tree.command(name="metar", description="Get the latest METAR for an airport")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(icao="ICAO code, e.g. KCVO")
async def metar(interaction: discord.Interaction, icao: str):
    await interaction.response.defer()
    icao = icao.upper()
    async with http_session.get(f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw") as r:
        text = (await r.text()).strip()
    await interaction.followup.send(f"```\n{text}\n```" if text else f"No METAR found for {icao}.")

@bot.tree.command(name="taf", description="Get the latest TAF for an airport")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(icao="ICAO code, e.g. KEUG")
async def taf(interaction: discord.Interaction, icao: str):
    await interaction.response.defer()
    icao = icao.upper()
    async with http_session.get(f"https://aviationweather.gov/api/data/taf?ids={icao}&format=raw") as r:
        text = (await r.text()).strip()
    await interaction.followup.send(f"```\n{text}\n```" if text else f"No TAF found for {icao}.")

@bot.tree.command(name="atis", description="Get the latest Digital ATIS")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(icao="ICAO code, e.g. KCVO")
async def atis(interaction: discord.Interaction, icao: str):
    await interaction.response.defer()
    icao = icao.upper()
    data = await fetch_atis(icao)
    if isinstance(data, dict) and "error" in data:
        await interaction.followup.send(f"No D-ATIS available for {icao}.")
        return
    output = "\n\n".join(
        f"**{d['type'].upper()}**\n```\n{d['datis']}\n```" for d in data
    )
    await interaction.followup.send(output)

# ===========================================================================
# COMMANDS: VATSIM
# ===========================================================================

@bot.tree.command(name="flightplan", description="Get the filed flight plan for a VATSIM callsign")
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
    # Search connected pilots first, then fall back to prefiles
    pilot = next((p for p in data.get("pilots", []) if p["callsign"] == callsign), None)
    is_prefile = False
    if pilot is None:
        pilot = next((p for p in data.get("prefiles", []) if p["callsign"] == callsign), None)
        is_prefile = True
    if pilot is None:
        await interaction.followup.send(f"No pilot or prefile found for `{callsign}`.")
        return
    if pilot.get("flight_plan") is None:
        await interaction.followup.send(f"`{callsign}` has no flight plan filed.")
        return
    output = await format_flightplan(pilot, is_prefile)
    await interaction.followup.send(output)

@bot.tree.command(name="route", description="Show the full filed route for a VATSIM callsign")
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
    # Search connected pilots first, then fall back to prefiles
    pilot = next((p for p in data.get("pilots", []) if p["callsign"] == callsign), None)
    is_prefile = False
    if pilot is None:
        pilot = next((p for p in data.get("prefiles", []) if p["callsign"] == callsign), None)
        is_prefile = True
    if pilot is None:
        await interaction.followup.send(f"No pilot or prefile found for `{callsign}`.")
        return
    fp = pilot.get("flight_plan")
    if not fp or not fp.get("route"):
        await interaction.followup.send(f"`{callsign}` has no route filed.")
        return
    tag = "  ⚠️ PREFILE" if is_prefile else ""
    await interaction.followup.send(
        f"**{callsign}**{tag} — {fp.get('departure', '?')}→{fp.get('arrival', '?')}\n```\n{fp['route']}\n```"
    )

@bot.tree.command(name="traffic", description="Show inbound (within 30 min) and departing traffic for an airport")
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
    coords = AIRPORT_COORDS.get(icao)
    if coords is None:
        await interaction.followup.send(f"No coordinates on file for `{icao}`. Supported: {', '.join(AIRPORT_COORDS.keys())}")
        return
    apt_lat, apt_lon = coords
    arrivals, departures = [], []
    for p in data.get("pilots", []):
        fp = p.get("flight_plan")
        if not fp:
            continue
        gs = p.get("groundspeed", 0)
        if fp.get("arrival") == icao and gs >= 50:
            dist = haversine(p["latitude"], p["longitude"], apt_lat, apt_lon)
            mins = estimate_minutes_out(dist, gs)
            if mins is not None and mins <= 30:
                arrivals.append((mins, p["callsign"], fp.get("aircraft_short", "?"), int(dist), gs))
        if fp.get("departure") == icao and gs < 50:
            dist = haversine(p["latitude"], p["longitude"], apt_lat, apt_lon)
            if dist <= 5:
                departures.append((p["callsign"], fp.get("aircraft_short", "?"), fp.get("arrival", "?")))
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

@bot.tree.command(name="controllers", description="Show online ZSE controllers (SEA, PDX, EUG, HIO)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def controllers(interaction: discord.Interaction):
    await interaction.response.defer()
    data = await fetch_vatsim()
    if data is None:
        await interaction.followup.send("Unable to reach VATSIM data feed.")
        return
    zse = [
        c for c in data.get("controllers", [])
        if any(c["callsign"].startswith(pfx) for pfx in ZSE_PREFIXES)
    ]
    if not zse:
        await interaction.followup.send("No ZSE controllers currently online.")
        return
    lines = ["**ZSE Controllers Online**"]
    for c in sorted(zse, key=lambda x: x["callsign"]):
        lines.append(f"`{c['callsign']}` — {c.get('frequency', '?')} MHz | {c.get('name', '?')}")
    await interaction.followup.send("\n".join(lines))

@bot.tree.command(name="prefiles", description="Show pilots who have filed but not yet connected to VATSIM")
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
    results = [
        p for p in data.get("prefiles", [])
        if p.get("flight_plan", {}).get("departure") == icao
        or p.get("flight_plan", {}).get("arrival") == icao
    ]
    if not results:
        await interaction.followup.send(f"No prefiles found for `{icao}`.")
        return
    # Format all entries — one stats API call per entry (async, shared session)
    formatted = []
    for p in results:
        entry = await format_flightplan(p, is_prefile=True)
        formatted.append(entry)
    # Split into batches of 5 to stay within Discord's 2000 char limit
    header_sent = False
    batch = []
    for entry in formatted:
        batch.append(entry)
        if len(batch) == 5:
            header = f"**Prefiles for {icao}**\n" if not header_sent else ""
            await interaction.followup.send(header + "\n".join(batch))
            header_sent = True
            batch = []
    if batch:
        header = f"**Prefiles for {icao}**\n" if not header_sent else ""
        await interaction.followup.send(header + "\n".join(batch))

# ===========================================================================
# AUTO-UPDATE: ATIS WATCHER
# ===========================================================================

@tasks.loop(minutes=ATIS_POLL_MINUTES)
async def atis_watcher():
    if wx_report_channel is None:
        return
    for icao in WATCHED_AIRPORTS:
        data = await fetch_atis(icao)
        if data is None:
            continue
        current_codes = extract_codes(data)
        changed = False
        for atis_type, code in current_codes.items():
            key = f"{icao}:{atis_type}"
            if last_atis_codes.get(key) != code:
                changed = True
                last_atis_codes[key] = code
        if changed:
            output = format_atis(icao, data)
            if output:
                await wx_report_channel.send(output)

@atis_watcher.before_loop
async def before_atis_watcher():
    await bot.wait_until_ready()

# ===========================================================================
# AUTO-UPDATE: PREFILE WATCHER
# ===========================================================================

@tasks.loop(minutes=5)
async def prefile_watcher():
    """
    Polls VATSIM prefiles every 5 minutes.
    First run populates known_prefiles silently — no notifications.
    Subsequent runs DM the bot owner for any new KPDX departure prefiles.
    Uses format_flightplan for consistent compact output.
    """
    global known_prefiles, prefile_initialized
    data = await fetch_vatsim()
    if data is None:
        return
    kpdx_prefiles = {
        p["callsign"]: p for p in data.get("prefiles", [])
        if p.get("flight_plan", {}).get("departure") == "KPDX"
    }
    if not prefile_initialized:
        known_prefiles = set(kpdx_prefiles.keys())
        prefile_initialized = True
        return
    new_ones = set(kpdx_prefiles.keys()) - known_prefiles
    for cs in new_ones:
        p = kpdx_prefiles[cs]
        msg = "**New Prefile — KPDX**\n" + await format_flightplan(p, is_prefile=True)
        app_info = await bot.application_info()
        owner = app_info.owner
        await owner.send(msg)
    known_prefiles = set(kpdx_prefiles.keys())

@prefile_watcher.before_loop
async def before_prefile_watcher():
    await bot.wait_until_ready()

# ===========================================================================
# AUTO-UPDATE: CONTROLLER WATCHER
# ===========================================================================

@tasks.loop(minutes=2)
async def controller_watcher():
    """
    Polls VATSIM controllers every 2 minutes.
    Watches PDX_ and SEA_ positions only (affects KPDX and KSEA operations).
    First run populates known_controllers silently — no notifications.
    Subsequent runs DM the bot owner when a position comes online or goes offline.
    """
    global known_controllers, controller_initialized
    data = await fetch_vatsim()
    if data is None:
        return
    # Filter controllers relevant to KPDX and KSEA operations
    current = {
        c["callsign"]: c for c in data.get("controllers", [])
        if any(c["callsign"].startswith(pfx) for pfx in WATCHED_CONTROLLER_PREFIXES)
    }
    if not controller_initialized:
        known_controllers = set(current.keys())
        controller_initialized = True
        return
    app_info = await bot.application_info()
    owner = app_info.owner
    # Detect new controllers coming online
    came_online = set(current.keys()) - known_controllers
    for cs in came_online:
        c = current[cs]
        await owner.send(
            f"🟢 `{cs}` online — {c.get('name', '?')} | {c.get('frequency', '?')} MHz"
        )
    # Detect controllers going offline
    went_offline = known_controllers - set(current.keys())
    for cs in went_offline:
        await owner.send(f"🔴 `{cs}` offline")
    # Update known set
    known_controllers = set(current.keys())

@controller_watcher.before_loop
async def before_controller_watcher():
    await bot.wait_until_ready()

# ===========================================================================
# RUN
# ===========================================================================

bot.run(TOKEN)