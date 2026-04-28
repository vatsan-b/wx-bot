import math
import time
import aiohttp
from config import VATSIM_FEED_URL, VATSIM_STATS_URL, VATSIM_CACHE_SECONDS

# Single aiohttp session reused across all commands and polling loops
http_session: aiohttp.ClientSession | None = None

# Cached channel reference resolved once on startup
wx_report_channel = None

# Last seen ATIS info code per airport+type e.g. {"KPDX:combined": "D"}
last_atis_codes: dict[str, str] = {}

# VATSIM feed cache
vatsim_cache: dict | None = None
vatsim_cache_time: float = 0.0

# Prefile watcher state
known_prefiles: set[str] = set()
prefile_initialized: bool = False

# Controller watcher state
known_controllers: set[str] = set()
controller_initialized: bool = False

# Inbound traffic watcher state — callsigns currently flagged as inbound
known_inbound: set[str] = set()
inbound_initialized: bool = False


# Weather helpers

async def fetch_atis(icao: str):
    '''Fetch parsed atis.info JSON for an ICAO, or None on failure.'''
    try:
        async with http_session.get(f"https://atis.info/api/{icao}") as r:
            return await r.json()
    except Exception:
        return None

def format_atis(icao: str, data) -> str | None:
    '''Format atis.info response for display. Returns None if unavailable.'''
    if isinstance(data, dict) and "error" in data:
        return None
    body = "\n\n".join(f"**{d['type'].upper()}**\n```\n{d['datis']}\n```" for d in data)
    return f"**{icao}**\n{body}"

def extract_codes(data) -> dict[str, str]:
    '''Extract {type: code} from atis.info response. Empty dict if unavailable.'''
    if isinstance(data, dict) and "error" in data:
        return {}
    return {d["type"]: d.get("code", "") for d in data}


# VATSIM helpers

async def fetch_vatsim() -> dict | None:
    '''
    Fetch and cache the VATSIM data feed.
    Cache is valid for VATSIM_CACHE_SECONDS.
    '''
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
    '''Fetch pilot and ATC hours from VATSIM Core API. No auth required.'''
    try:
        async with http_session.get(VATSIM_STATS_URL.format(cid=cid)) as r:
            return await r.json(content_type=None)
    except Exception:
        return None

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    '''Great-circle distance in nautical miles between two lat/lon pairs.'''
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))

def estimate_minutes_out(dist_nm: float, groundspeed: int) -> float | None:
    '''ETA in minutes based on distance and groundspeed. None if on ground.'''
    return None if groundspeed < 50 else (dist_nm / groundspeed) * 60

async def format_flightplan(pilot: dict, is_prefile: bool, stats: dict | None = None) -> str:
    '''
    Compact flight plan formatter used by /flightplan, /prefiles, and watcher DMs.
    Accepts optional pre-fetched stats to allow concurrent fetching in batch callers.
    '''
    fp = pilot.get("flight_plan", {})
    if stats is None:
        cid = pilot.get("cid")
        stats = await fetch_pilot_stats(cid) if cid else None
    pilot_hrs = f"{int(stats['pilot'])}h" if stats else "N/A"
    atc_hrs = f"{int(stats['atc'])}h" if stats else "N/A"

    tag = "  ⚠️ PREFILE" if is_prefile else ""
    remarks = fp.get("remarks", "")
    if remarks:
        remarks = remarks[:200] + ("…" if len(remarks) > 200 else "")

    lines = [
        f"`{pilot.get('callsign', '?')}`{tag} | {fp.get('aircraft_faa', '?')} ({fp.get('flight_rules', '?')}) | {fp.get('departure', '?')}→{fp.get('arrival', '?')} | {fp.get('altitude', '?')}",
        f"ETD: {fp.get('deptime', '?')}z  ETE: {fp.get('enroute_time', '?')}  Fuel: {fp.get('fuel_time', '?')}  Alternate: {fp.get('alternate', 'N/A')}",
        f"Route: {fp.get('route', 'No route filed')}",
    ]
    if remarks:
        lines.append(f"Remarks: {remarks}")
    lines += [f"Pilot {pilot_hrs}  ATC {atc_hrs}"]
    return "\n".join(lines) + "\n"