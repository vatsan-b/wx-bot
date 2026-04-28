import asyncio
import discord
from discord import app_commands
from discord.ext import tasks
from shared import (
    fetch_vatsim, fetch_pilot_stats, format_flightplan, haversine, estimate_minutes_out,
    known_inbound, inbound_initialized,
)
from config import AIRPORT_COORDS, TRAFFIC_WATCH_ICAO, TRAFFIC_POLL_MINUTES, TRAFFIC_WINDOW_MINUTES
import shared


def register(bot, guild_obj):
    '''Register all traffic/VATSIM commands and the inbound traffic watcher.'''

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
        await interaction.followup.send(await format_flightplan(pilot, is_prefile))

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
        pilot = next((p for p in data.get("pilots", []) if p["callsign"] == callsign), None)
        is_prefile = pilot is None
        if is_prefile:
            pilot = next((p for p in data.get("prefiles", []) if p["callsign"] == callsign), None)
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
        all_stats = await asyncio.gather(*[fetch_pilot_stats(p.get("cid")) for p in results])
        formatted = [await format_flightplan(p, is_prefile=True, stats=s) for p, s in zip(results, all_stats)]
        # Send in batches of 5 to stay within Discord's 2000 char limit
        header_sent = False
        for i in range(0, len(formatted), 5):
            batch = formatted[i:i + 5]
            header = f"**Prefiles for {icao}**\n" if not header_sent else ""
            await interaction.followup.send(header + "\n".join(batch))
            header_sent = True

    @tasks.loop(minutes=TRAFFIC_POLL_MINUTES)
    async def inbound_watcher():
        '''
        Polls VATSIM pilots every 5 minutes for KPDX inbound traffic.
        First run populates known_inbound silently — no notifications.
        Subsequent runs DM the bot owner when a new callsign enters the window.
        '''
        coords = AIRPORT_COORDS.get(TRAFFIC_WATCH_ICAO)
        if coords is None:
            return
        apt_lat, apt_lon = coords
        data = await fetch_vatsim()
        if data is None:
            return
        current_inbound = {}
        for p in data.get("pilots", []):
            fp = p.get("flight_plan")
            if not fp or fp.get("arrival") != TRAFFIC_WATCH_ICAO:
                continue
            gs = p.get("groundspeed", 0)
            if gs < 50:
                continue
            dist = haversine(p["latitude"], p["longitude"], apt_lat, apt_lon)
            mins = estimate_minutes_out(dist, gs)
            if mins is not None and mins <= TRAFFIC_WINDOW_MINUTES:
                current_inbound[p["callsign"]] = (mins, p, fp, int(dist), gs)

        if not shared.inbound_initialized:
            shared.known_inbound = set(current_inbound.keys())
            shared.inbound_initialized = True
            return

        new_ones = set(current_inbound.keys()) - shared.known_inbound
        if new_ones:
            app_info = await bot.application_info()
            owner = app_info.owner
            for cs in new_ones:
                mins, p, fp, dist, gs = current_inbound[cs]
                await owner.send(
                    f"**Inbound — {TRAFFIC_WATCH_ICAO}**\n"
                    f"`{cs}` — {fp.get('aircraft_short', '?')} | ~{int(mins)} min | {dist} nm | {gs} kts\n"
                    f"From: {fp.get('departure', '?')}"
                )
        shared.known_inbound = set(current_inbound.keys())

    @inbound_watcher.before_loop
    async def before_inbound_watcher():
        await bot.wait_until_ready()

    return [inbound_watcher]