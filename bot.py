import logging
import asyncio

import aiohttp
import discord
from discord.ext import commands

from config import TOKEN, GUILD_ID, WX_REPORT_CHANNEL_ID, WATCHED_AIRPORTS
import shared
import weather
import traffic
import controllers

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

intents = discord.Intents.none()
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
guild_obj = discord.Object(id=GUILD_ID)

watcher_starters: list = []


@bot.event
async def on_ready():
    if shared.http_session is None or shared.http_session.closed:
        shared.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )

    shared.wx_report_channel = bot.get_channel(WX_REPORT_CHANNEL_ID)
    if shared.wx_report_channel is None:
        logger.warning(
            f"Could not resolve WX_REPORT_CHANNEL_ID={WX_REPORT_CHANNEL_ID}; "
            "ATIS watcher will no-op until it is reachable."
        )

    try:
        await bot.tree.sync(guild=guild_obj)
        await bot.tree.sync()  # global commands
    except Exception as e:
        logger.error(f"Slash command sync failed: {e}")

    for task in watcher_starters:
        if not task.is_running():
            task.start()

    logger.info(f"Logged in as {bot.user} | Watching: {WATCHED_AIRPORTS}")


@bot.event
async def on_disconnect():
    logger.warning("Bot disconnected")


async def cleanup():
    if shared.http_session and not shared.http_session.closed:
        await shared.http_session.close()
        logger.info("HTTP session closed")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    logger.error(f"Command error: {error}", exc_info=True)
    msg = "An error occurred while processing the command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


# Register all modules. Each .register() returns a list of task loops to start.
watcher_starters.extend(weather.register(bot, guild_obj))
watcher_starters.extend(traffic.register(bot, guild_obj))
watcher_starters.extend(controllers.register(bot, guild_obj))


if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.critical(f"Failed to start bot: {e}")
    finally:
        try:
            asyncio.run(cleanup())
        except RuntimeError:
            # Event loop may already be closed by discord.py on shutdown.
            pass