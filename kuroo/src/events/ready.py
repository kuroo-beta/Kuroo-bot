from kuroo.workflows.components import message_payload
from discord.ext import commands
import wavelink
import traceback, sys

from kuroo.console.logging import logger
from kuroo.config.config import users as users_config

from kuroo.engine.Bot import AutoShardedBot
import asyncio
import requests
from kuroo.style import color

import discord
from kuroo.workflows.presence import streaming_activity

from kuroo.src.startup import giveaways
from kuroo.src.startup import j2c_controller
class ready(commands.Cog):
    def __init__(self, bot):
        self.bot:AutoShardedBot = bot
        self._activity_task = None
        self._profile_updated = False


        
    @commands.Cog.listener(name="on_ready")
    async def on_ready(self):
        try:
            logger.startup_summary(self.bot)
            if self.bot.BotConfig.DASHBOARD_ENABLED:
                logger.web_startup_summary(self.bot)
            logger.success(f"Connected as {self.bot.user}")
            await self.update_display_name()
            # The logged-in application is authoritative when client ID is omitted.
            self.bot.urls.INVITE = discord.utils.oauth_url(self.bot.user.id)
            if not self.bot.BotConfig.DISCORD_CLIENT_ID:
                self.bot.urls.VOTE = f"https://top.gg/bot/{self.bot.user.id}/vote"
            try:
                if self._activity_task is None or self._activity_task.done():
                    self._activity_task = asyncio.create_task(self.activity())
            except:
                pass
            try:
                asyncio.create_task(self.on_ready_startups())
            except:
                pass
            if not getattr(self.bot, '_slash_synced', False):
                try:
                    await self.bot.tree.sync()
                    self.bot._slash_synced = True
                    logger.system('Application Commands (Tree) Synced')
                except discord.HTTPException as error:
                    logger.error(f'Slash command sync failed (HTTP {error.status}, code {error.code}). '
                                 'Bot admins can retry with the prefix synccommands command.')
            # Load developers
            dev_ids = users_config.developer
            if isinstance(dev_ids, (int, str)):
                dev_ids = [dev_ids]
            
            self.bot.developers = []
            for dev_id in dev_ids:
                try:
                    user = await self.bot.fetch_user(int(dev_id))
                    self.bot.developers.append(user)
                except Exception as e:
                    logger.warning(f"Failed to fetch developer {dev_id}: {e}")
            
            if self.bot.developers:
                self.bot.developer = self.bot.developers[0]
            else:
                self.bot.developer = self.bot.user
                self.bot.developers = [self.bot.user]

            logger.system(f"Found {len(self.bot.developers)} Authorized Developers")

        except Exception as e:
            logger.error(f"Error in file {__file__} at line {traceback.extract_tb(sys.exc_info()[2])[0][1]}: {e}")
    
    async def on_ready_startups(self):
        try:
            asyncio.create_task(giveaways.resume_active_giveaway(self.bot))
        except:
            pass
        logger.cog("Active Giveaways Resumed")
        try:
            asyncio.create_task(j2c_controller.resume_j2c_controllers(self.bot))
        except:
            pass
        logger.cog("J2C Controllers Resumed")

        

    async def update_display_name(self):
        # At most one attempt per process; reconnects must not repeat profile writes.
        if self._profile_updated:
            return
        self._profile_updated = True
        desired = self.bot.BotConfig.DISPLAY_NAME.strip()
        if desired and self.bot.user.name != desired:
            try:
                await self.bot.user.edit(username=desired)
            except discord.HTTPException as error:
                logger.warning(f"Discord rejected the configured bot display name (HTTP {error.status}).")

    async def activity(self):
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            try:
                activity = streaming_activity(self.bot.BotConfig, self.bot.guilds, self.bot.shard_count)
                await self.bot.change_presence(status=discord.Status.online, activity=activity)
                await asyncio.sleep(60)

            except discord.ConnectionClosed:
                logger.warning("Discord connection closed. Waiting to reconnect...")
                await asyncio.sleep(10)

            except Exception as e:
                tb = traceback.extract_tb(sys.exc_info()[2])[-1]
                logger.error(f"Error in {__file__}, line {tb.lineno}: {e}")
                await asyncio.sleep(300) 



    def cog_unload(self):
        if self._activity_task is not None:
            self._activity_task.cancel()

    async def send_shard_log(self, msg, embed_color=color.green):
        try:
            shards_log_webhook = self.bot.channels.shards_log_webhook
            if shards_log_webhook:
                embed = discord.Embed(description=f"{msg}", color=embed_color)
                requests.post(shards_log_webhook, params={"with_components": "true"}, json=message_payload({"embeds": [embed.to_dict()]}),timeout=5)
            else:
                logger.warning(f"Could not send shard log: {msg}")
        except Exception as e:
            logger.error(f"Error in file {__file__}: {traceback.format_exc()}")

    @commands.Cog.listener()
    async def on_disconnect(self):
        await self.send_shard_log("Disconnected from Discord", embed_color=color.red)
        logger.warning("Disconnected from Discord")

    @commands.Cog.listener()
    async def on_resumed(self):
        await self.send_shard_log("Reconnected to Discord", embed_color=color.orange)
        logger.info("Reconnected to Discord")

    @commands.Cog.listener()
    async def on_shard_ready(self, shard_id):
        await self.send_shard_log(f"Shard {shard_id} is ready", embed_color=color.green)
        logger.info(f"Shard {shard_id} is ready")

    @commands.Cog.listener()
    async def on_shard_disconnect(self, shard_id):
        await self.send_shard_log(f"Shard {shard_id} is disconnected", embed_color=color.red)
        logger.warning(f"Shard {shard_id} is disconnected")

    @commands.Cog.listener()
    async def on_shard_resumed(self, shard_id):
        await self.send_shard_log(f"Shard {shard_id} is resumed", embed_color=color.orange)
        logger.info(f"Shard {shard_id} is resumed")
    
    # event when a cog is loaded
    @commands.Cog.listener()
    async def on_cog_load(self, cog):
        logger.info(f"Cog {cog.qualified_name} loaded")