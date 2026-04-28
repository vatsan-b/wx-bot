"""
Shared state and helpers across the bot.

IMPORTANT: any name in this module that gets reassigned at runtime
(http_session, wx_report_channel, *_initialized flags, vatsim_cache*)
MUST be accessed as `shared.NAME`, not `from shared import NAME`.
A `from` import binds the value at import time; later reassignments
in this module won't be visible to the importer.

Mutable containers (dicts, sets) can be imported directly because
they are mutated in place, not rebound.
"""

import math
import time
import logging
import aiohttp

from config import VATSIM_FEED_URL, VATSIM_STATS_URL, VATSIM_CACHE_SECONDS

logger = logging.getLogger(__name__)

# --- Resources reassigned at startup (access as shared.X) ---
http_session: aiohttp.ClientSession | None = None
wx_report_channel = None

# --- Caches & state ---
last_atis_codes: dict[str, str] = {}      # mutated in place — safe to import directly

vatsim_cache: dict | None = None           # reassigned — access as shared.vatsim_cache
vatsim_cache_time: float = 0.0

# Watchers: sets are mutated in place, but the *_initialized flags are reassigned.
# Use `shared.foo_initialized` when writing.
known_prefiles: set[str] = set()
prefile_initialized: bool = False

known_controllers: set[str] = set()
controller_initialized: bool = False

known_inbound: set[str] = set()
inbound_initialized: bool = False


# ---------------------------------------------------------------------------
# ATIS
# ---------------------------------------------------------------------------
async def fetch_atis(icao: str):
    """Fetch parsed ATIS from atis.info. Returns parsed JSON or None."""
    if http_session is None:
        logger.error("fetch_atis called before http_session was initialized")
        return None
    try:
        async with http_session.get(f"https://atis.info/api/{icao}", timeout=10) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None)
    except Exception as e:
        logger.warning(f"Failed to fetch ATIS for {icao}: {e}")
        return None


def format_atis(icao: str, data) -> str | None:
    if not data or (isinstance(data, dict) and "error" in data):
        return None
    body = "\n\n".join(
        f"**{d.get('type', '?').upper()}**\n```\n{d.get('datis', '')}\n```"
        for d in data
    )
    return f"**{icao} ATIS Update**\n{body}"


def extract_codes(data) -> dict[str, str]:
    if not data or (isinstance(data, dict) and "error" in data):
        return {}
    return {d["type"]: d.get("code", "") for d in data if "type" in d}


# ---------------------------------------------------------------------------
# VATSIM
# ---------------------------------------------------------------------------
async def fetch_vatsim() -> dict | None:
    """Cached VATSIM data feed. Returns latest payload or stale cache on failure."""
    global vatsim_cache, vatsim_cache_time

    if http_session is None:
        logger.error("fetch_vatsim called before http_session was initialized")
        return None

    now = time.monotonic()
    if vatsim_cache is not None and (now - vatsim_cache_time) < VATSIM_CACHE_SECONDS:
        return vatsim_cache

    try:
        async with http_session.get(VATSIM_FEED_URL, timeout=15) as r:
            if r.status != 200:
                logger.error(f"VATSIM feed returned {r.status}")
                return vatsim_cache
            vatsim_cache = await r.json(content_type=None)
            vatsim_cache_time = now
            return vatsim_cache
    except Exception as e:
        logger.error(f"Failed to fetch VATSIM data: {e}")
        return vatsim_cache


async def fetch_pilot_stats(cid: int | None) -> dict | None:
    if not cid or http_session is None:
        return None
    try:
        async with http_session.get(VATSIM_STATS_URL.format(cid=cid), timeout=8) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def estimate_minutes_out(dist_nm: float, groundspeed: int) -> float | None:
    if groundspeed is None or groundspeed < 50:
        return None
    return (dist_nm / groundspeed) * 60


# ---------------------------------------------------------------------------
# Flight plan formatter
# ---------------------------------------------------------------------------
async def format_flightplan(pilot: dict, is_prefile: bool = False, stats: dict | None = None) -> str:
    fp = pilot.get("flight_plan") or {}
    if stats is None:
        stats = await fetch_pilot_stats(pilot.get("cid"))

    pilot_hrs = f"{int(stats.get('pilot', 0))}h" if stats else "N/A"
    atc_hrs = f"{int(stats.get('atc', 0))}h" if stats else "N/A"

    tag = "  [PREFILE]" if is_prefile else ""
    raw_remarks = fp.get("remarks", "") or ""
    remarks = raw_remarks[:200] + ("…" if len(raw_remarks) > 200 else "")

    lines = [
        f"`{pilot.get('callsign', '?')}`{tag} | {fp.get('aircraft_faa', '?')} "
        f"({fp.get('flight_rules', '?')}) | "
        f"{fp.get('departure', '?')}→{fp.get('arrival', '?')} | {fp.get('altitude', '?')}",
        f"ETD: {fp.get('deptime', '?')}z  ETE: {fp.get('enroute_time', '?')}  "
        f"Fuel: {fp.get('fuel_time', '?')}  Alt: {fp.get('alternate', 'N/A')}",
        f"Route: {fp.get('route') or 'No route filed'}",
    ]
    if remarks:
        lines.append(f"Remarks: {remarks}")
    lines.append(f"Pilot {pilot_hrs}  |  ATC {atc_hrs}")

    return "\n".join(lines) + "\n"