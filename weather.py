import discord
from discord import app_commands
from discord.ext import tasks
from shared import (
    http_session, wx_report_channel, last_atis_codes,
    fetch_atis, format_atis, extract_codes,
)
from config import WATCHED_AIRPORTS, ATIS_POLL_MINUTES


def register(bot, guild_obj):
    '''Register all weather commands and the ATIS watcher on the bot instance.'''

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
        output = "\n\n".join(f"**{d['type'].upper()}**\n```\n{d['datis']}\n```" for d in data)
        await interaction.followup.send(output)

    @tasks.loop(minutes=ATIS_POLL_MINUTES)
    async def atis_watcher():
        if wx_report_channel is None:
            return
        for icao in WATCHED_AIRPORTS:
            data = await fetch_atis(icao)
            if data is None:
                continue
            current_codes = extract_codes(data)
            changed = any(last_atis_codes.get(f"{icao}:{t}") != code for t, code in current_codes.items())
            if changed:
                for t, code in current_codes.items():
                    last_atis_codes[f"{icao}:{t}"] = code
                output = format_atis(icao, data)
                if output:
                    await wx_report_channel.send(output)

    @atis_watcher.before_loop
    async def before_atis_watcher():
        await bot.wait_until_ready()

    return [atis_watcher]