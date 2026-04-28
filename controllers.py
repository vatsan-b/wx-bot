import logging

import discord
from discord import app_commands
from discord.ext import tasks

import shared
from shared import (
    fetch_vatsim,
    format_flightplan,
    known_controllers,
    known_prefiles,
)
from config import ZSE_PREFIXES, WATCHED_CONTROLLER_PREFIXES

logger = logging.getLogger(__name__)


def register(bot, guild_obj):

    @bot.tree.command(
        name="controllers",
        description="Show online ZSE controllers (SEA, PDX, EUG, HIO)",
    )
    async def controllers_cmd(interaction: discord.Interaction):
        await interaction.response.defer()
        data = await fetch_vatsim()
        if data is None:
            await interaction.followup.send("Unable to reach VATSIM data feed.")
            return

        zse = [
            c for c in data.get("controllers", [])
            if any(c.get("callsign", "").startswith(pfx) for pfx in ZSE_PREFIXES)
        ]

        if not zse:
            await interaction.followup.send("No ZSE controllers currently online.")
            return

        lines = ["**ZSE Controllers Online**"]
        for c in sorted(zse, key=lambda x: x["callsign"]):
            lines.append(
                f"`{c['callsign']}` — {c.get('frequency', '?')} MHz | {c.get('name', '?')}"
            )
        await interaction.followup.send("\n".join(lines))

    # ---- Prefile watcher (DMs owner on new KPDX prefiles) ----
    @tasks.loop(minutes=5)
    async def prefile_watcher():
        data = await fetch_vatsim()
        if data is None:
            return

        kpdx_prefiles = {
            p["callsign"]: p for p in data.get("prefiles", [])
            if (p.get("flight_plan") or {}).get("departure") == "KPDX"
        }

        if not shared.prefile_initialized:
            known_prefiles.clear()
            known_prefiles.update(kpdx_prefiles.keys())
            shared.prefile_initialized = True
            return

        new_ones = set(kpdx_prefiles.keys()) - known_prefiles
        if new_ones:
            try:
                app_info = await bot.application_info()
                owner = app_info.owner
                for cs in new_ones:
                    msg = "**New Prefile — KPDX**\n" + await format_flightplan(
                        kpdx_prefiles[cs], is_prefile=True
                    )
                    await owner.send(msg)
            except Exception as e:
                logger.error(f"Prefile watcher notification failed: {e}")

        known_prefiles.clear()
        known_prefiles.update(kpdx_prefiles.keys())

    @prefile_watcher.before_loop
    async def before_prefile_watcher():
        await bot.wait_until_ready()

    # ---- Controller watcher (DMs owner when KPDX/KSEA controllers go on/offline) ----
    @tasks.loop(minutes=2)
    async def controller_watcher():
        data = await fetch_vatsim()
        if data is None:
            return

        current = {
            c["callsign"]: c for c in data.get("controllers", [])
            if any(c.get("callsign", "").startswith(pfx) for pfx in WATCHED_CONTROLLER_PREFIXES)
        }

        if not shared.controller_initialized:
            known_controllers.clear()
            known_controllers.update(current.keys())
            shared.controller_initialized = True
            return

        try:
            app_info = await bot.application_info()
            owner = app_info.owner

            for cs in set(current.keys()) - known_controllers:
                c = current[cs]
                await owner.send(
                    f"🟢 `{cs}` online — {c.get('name', '?')} | {c.get('frequency', '?')} MHz"
                )

            for cs in known_controllers - set(current.keys()):
                await owner.send(f"🔴 `{cs}` offline")
        except Exception as e:
            logger.error(f"Controller watcher notification failed: {e}")

        known_controllers.clear()
        known_controllers.update(current.keys())

    @controller_watcher.before_loop
    async def before_controller_watcher():
        await bot.wait_until_ready()

    return [prefile_watcher, controller_watcher]