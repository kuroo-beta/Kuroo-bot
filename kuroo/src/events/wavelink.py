"""Music lifecycle hooks. Wavelink owns queue advancement and recommendations."""
import asyncio

from kuroo.bridge import lavalink
from discord.ext import commands

from kuroo.console.logging import logger
from kuroo.workflows.music import MAX_QUEUE


class Wavelink(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.connector = None

    async def cog_load(self):
        self.connector = asyncio.create_task(lavalink.on_node(self.bot))

    def cog_unload(self):
        if self.connector:
            self.connector.cancel()

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload):
        logger.info('Lavalink music node ready.')
        cog = self.bot.get_cog('Music')
        if cog:
            await cog.restore_stay()

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload):
        player = payload.player
        if player and player.guild:
            # Keep history bounded during long 24/7 sessions.
            for queue in (player.queue, player.auto_queue):
                if queue.history:
                    while len(queue.history) > MAX_QUEUE:
                        queue.history.delete(0)
            cog = self.bot.get_cog('Music')
            if cog:
                await cog.send_music_controls(player.guild)

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player):
        cog = self.bot.get_cog('Music')
        if not cog or not player.guild:
            return
        if not await cog.stays(player.guild.id):
            await player.disconnect()
            await cog.send_music_controls(player.guild, end=True)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload):
        # Do not log provider URLs, node credentials, or raw upstream errors.
        logger.warning('A music track failed; Wavelink will attempt the next queued track.')

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload):
        if payload.player:
            await payload.player.skip(force=True)
