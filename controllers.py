import discord
from discord import app_commands
from discord.ext import tasks
from shared import fetch_vatsim, format_flightplan, known_controllers, controller_initialized, known_prefiles, prefile_initialized
from config import ZSE_PREFIXES, WATCHED_CONTROLLER_PREFIXES
import shared


def register(bot, guild_obj):
    '''Register controller commands, prefile watcher, and controller watcher.'''

    @bot.tree.command(name="controllers", description="Show online ZSE controllers (SEA, PDX, EUG, HIO)")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    async def controllers(interaction: discord.Interaction):
        await interaction.response.defer()
        data = await fetch_vatsim()
        if data is None:
            await interaction.followup.send("Unable to reach VATSIM data feed.")
            return
        zse = [c for c in data.get("controllers", []) if any(c["callsign"].startswith(pfx) for pfx in ZSE_PREFIXES)]
        if not zse:
            await interaction.followup.send("No ZSE controllers currently online.")
            return
        lines = ["**ZSE Controllers Online**"]
        for c in sorted(zse, key=lambda x: x["callsign"]):
            lines.append(f"`{c['callsign']}` — {c.get('frequency', '?')} MHz | {c.get('name', '?')}")
        await interaction.followup.send("\n".join(lines))

    @tasks.loop(minutes=5)
    async def prefile_watcher():
        '''
        Polls VATSIM prefiles every 5 minutes.
        First run populates known_prefiles silently — no notifications.
        Subsequent runs DM the bot owner for new KPDX departure prefiles.
        '''
        data = await fetch_vatsim()
        if data is None:
            return
        kpdx_prefiles = {
            p["callsign"]: p for p in data.get("prefiles", [])
            if p.get("flight_plan", {}).get("departure") == "KPDX"
        }
        if not shared.prefile_initialized:
            shared.known_prefiles = set(kpdx_prefiles.keys())
            shared.prefile_initialized = True
            return
        new_ones = set(kpdx_prefiles.keys()) - shared.known_prefiles
        if new_ones:
            app_info = await bot.application_info()
            owner = app_info.owner
            for cs in new_ones:
                msg = "**New Prefile — KPDX**\n" + await format_flightplan(kpdx_prefiles[cs], is_prefile=True)
                await owner.send(msg)
        shared.known_prefiles = set(kpdx_prefiles.keys())

    @prefile_watcher.before_loop
    async def before_prefile_watcher():
        await bot.wait_until_ready()

    @tasks.loop(minutes=2)
    async def controller_watcher():
        '''
        Polls VATSIM controllers every 2 minutes for PDX_ and SEA_ positions.
        First run populates known_controllers silently — no notifications.
        Subsequent runs DM the bot owner when a position comes online or goes offline.
        '''
        data = await fetch_vatsim()
        if data is None:
            return
        current = {
            c["callsign"]: c for c in data.get("controllers", [])
            if any(c["callsign"].startswith(pfx) for pfx in WATCHED_CONTROLLER_PREFIXES)
        }
        if not shared.controller_initialized:
            shared.known_controllers = set(current.keys())
            shared.controller_initialized = True
            return
        app_info = await bot.application_info()
        owner = app_info.owner
        for cs in set(current.keys()) - shared.known_controllers:
            c = current[cs]
            await owner.send(f"🟢 `{cs}` online — {c.get('name', '?')} | {c.get('frequency', '?')} MHz")
        for cs in shared.known_controllers - set(current.keys()):
            await owner.send(f"🔴 `{cs}` offline")
        shared.known_controllers = set(current.keys())

    @controller_watcher.before_loop
    async def before_controller_watcher():
        await bot.wait_until_ready()

    return [prefile_watcher, controller_watcher]