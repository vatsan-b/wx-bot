import discord
from discord.ext import commands
import aiohttp
from config import TOKEN, GUILD_ID, WX_REPORT_CHANNEL_ID
import shared
import weather
import traffic
import controllers

intents = discord.Intents.none()
intents.guilds = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
guild_obj = discord.Object(id=GUILD_ID)

# Register commands at import time and collect watcher start callbacks
watcher_starters = []
watcher_starters += weather.register(bot, guild_obj)
watcher_starters += traffic.register(bot, guild_obj)
watcher_starters += controllers.register(bot, guild_obj)


@bot.event
async def on_ready():
    if shared.http_session is None:
        shared.http_session = aiohttp.ClientSession()
    shared.wx_report_channel = bot.get_channel(WX_REPORT_CHANNEL_ID)

    await bot.tree.sync(guild=guild_obj)
    await bot.tree.sync()

    # Start watchers now that the loop and session exist
    for start in watcher_starters:
        if not start.is_running():
            start.start()

    print(f"Logged in as {bot.user}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    msg = f"Error: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


bot.run(TOKEN)