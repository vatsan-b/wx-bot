import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1356450656310923355

# ATIS auto-update settings
WX_REPORT_CHANNEL_ID = 1498242952252624917
WATCHED_AIRPORTS = ["KPDX"]
ATIS_POLL_MINUTES = 10

# VATSIM URLs
VATSIM_FEED_URL = "https://data.vatsim.net/v3/vatsim-data.json"
VATSIM_STATS_URL = "https://api.vatsim.net/v2/members/{cid}/stats"
VATSIM_CACHE_SECONDS = 60

# ZSE positions for /controllers
ZSE_PREFIXES = ("SEA_", "PDX_", "EUG_", "HIO_")

# Positions affecting KPDX and KSEA for watcher DMs
WATCHED_CONTROLLER_PREFIXES = ("PDX_", "SEA_")

# Airport coordinates for haversine
AIRPORT_COORDS = {
    "KPDX": (45.5887, -122.5975),
    "KSEA": (47.4502, -122.3088),
    "KEUG": (44.1246, -123.2119),
    "KHIO": (45.5388, -122.9538),
}

# Inbound traffic watcher settings
TRAFFIC_WATCH_ICAO = "KPDX"
TRAFFIC_POLL_MINUTES = 5
TRAFFIC_WINDOW_MINUTES = 30