import logging

import discord
from discord import app_commands
from discord.ext import tasks

import shared
from shared import last_atis_codes, fetch_atis, format_atis, extract_codes
from config import WATCHED_AIRPORTS, ATIS_POLL_MINUTES

logger = logging.getLogger(__name__)


def register(bot, guild_obj):

    @bot.tree.command(name="metar", description="Get the latest METAR for an airport")
    @app_commands.describe(icao="ICAO code, e.g. KPDX")
    async def metar(interaction: discord.Interaction, icao: str):
        await interaction.response.defer()
        icao = icao.upper().strip()

        if shared.http_session is None:
            await interaction.followup.send("HTTP session not ready yet — try again in a moment.")
            return

        try:
            async with shared.http_session.get(
                f"https://aviationweather.gov/api/data/metar?ids={icao}&format=json",
                timeout=10,
            ) as r:
                if r.status != 200:
                    await interaction.followup.send(f"Error fetching METAR for `{icao}`.")
                    return
                data = await r.json(content_type=None)

            if not isinstance(data, list) or not data:
                await interaction.followup.send(f"No METAR found for `{icao}`.")
                return

            raw = data[0].get("rawOb") or data[0].get("raw_text") or "No raw text available"
            await interaction.followup.send(f"**{icao} METAR**\n```\n{raw}\n```")
        except Exception as e:
            logger.error(f"METAR error for {icao}: {e}")
            await interaction.followup.send(f"Failed to fetch METAR for `{icao}`.")

    @bot.tree.command(name="taf", description="Get the latest TAF for an airport")
    @app_commands.describe(icao="ICAO code, e.g. KEUG")
    async def taf(interaction: discord.Interaction, icao: str):
        await interaction.response.defer()
        icao = icao.upper().strip()

        if shared.http_session is None:
            await interaction.followup.send("HTTP session not ready yet — try again in a moment.")
            return

        try:
            async with shared.http_session.get(
                f"https://aviationweather.gov/api/data/taf?ids={icao}&format=json",
                timeout=10,
            ) as r:
                if r.status != 200:
                    await interaction.followup.send(f"Error fetching TAF for `{icao}`.")
                    return
                data = await r.json(content_type=None)

            if not isinstance(data, list) or not data:
                await interaction.followup.send(f"No TAF found for `{icao}`.")
                return

            raw = data[0].get("rawTAF") or data[0].get("raw_text") or "No raw text available"
            await interaction.followup.send(f"**{icao} TAF**\n```\n{raw}\n```")
        except Exception as e:
            logger.error(f"TAF error for {icao}: {e}")
            await interaction.followup.send(f"Failed to fetch TAF for `{icao}`.")

    @bot.tree.command(name="atis", description="Get the latest Digital ATIS")
    @app_commands.describe(icao="ICAO code, e.g. KPDX")
    async def atis(interaction: discord.Interaction, icao: str):
        await interaction.response.defer()
        icao = icao.upper().strip()
        data = await fetch_atis(icao)
        if not data or (isinstance(data, dict) and "error" in data):
            await interaction.followup.send(f"No D-ATIS available for `{icao}`.")
            return
        output = format_atis(icao, data) or f"No D-ATIS available for `{icao}`."
        await interaction.followup.send(output)

    @tasks.loop(minutes=ATIS_POLL_MINUTES)
    async def atis_watcher():
        if shared.wx_report_channel is None:
            return

        for icao in WATCHED_AIRPORTS:
            data = await fetch_atis(icao)
            if not data:
                continue

            current_codes = extract_codes(data)
            if not current_codes:
                continue

            changed = any(
                last_atis_codes.get(f"{icao}:{t}") != code
                for t, code in current_codes.items()
            )

            if changed:
                for t, code in current_codes.items():
                    last_atis_codes[f"{icao}:{t}"] = code

                output = format_atis(icao, data)
                if output:
                    try:
                        await shared.wx_report_channel.send(output)
                    except Exception as e:
                        logger.error(f"Failed to send ATIS to channel: {e}")

    @atis_watcher.before_loop
    async def before_atis_watcher():
        await bot.wait_until_ready()

    return [atis_watcher]