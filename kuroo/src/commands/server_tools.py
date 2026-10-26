"""Invite tracking, staff health messages, and creator notifications."""
from collections import defaultdict
from types import SimpleNamespace
from typing import Literal
import asyncio

import discord
from discord.ext import commands, tasks

from kuroo.console.logging import logger
from kuroo.src.checks import checks
from kuroo.workflows import server_tools as data, safety


class ServerTools(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.tracker = data.InviteTracker()
        self.feeds = data.CreatorFeeds(bot.BotConfig.TWITCH_CLIENT_ID, bot.BotConfig.TWITCH_CLIENT_SECRET)
        self.scan_lock = asyncio.Lock()
        self.config_locks = defaultdict(asyncio.Lock)
        self.cog_info = SimpleNamespace(name='ServerTools', category='Security', hidden=False,
            description='Invite tracking, staff dashboard and creator alerts', emoji='🛡️')

    def cog_unload(self):
        self.refresh.cancel()

    async def cog_check(self, ctx):
        return (ctx.guild is not None and checks.check_blacklist_predicate(ctx) and checks.check_ignore_predicate(ctx))

    async def cog_command_error(self, ctx, error):
        cause = getattr(error, 'original', error)
        if isinstance(cause, data.ToolsError):
            await ctx.send(str(cause), ephemeral=True)
        elif isinstance(error, commands.CheckFailure):
            raise error
        else:
            logger.warning(f'Server tools action failed ({type(cause).__name__}).')
            await ctx.send('The action could not finish. Check provider availability, storage, and bot permissions.', ephemeral=True)

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.refresh.is_running():
            self.refresh.start()
        for guild in self.bot.guilds:
            try:
                if (await data.tool_settings(guild.id))['tracking']:
                    await self.tracker.prime(guild)
            except Exception:
                logger.warning('Could not prime invite tracking; joins will be marked Unknown until access recovers.')

    @commands.Cog.listener()
    async def on_invite_create(self, invite):
        # Add only the new invite; fetching all invites here could consume join evidence.
        async with self.tracker.locks[invite.guild.id]:
            snapshot = self.tracker.snapshots.get(invite.guild.id)
            if snapshot is not None:
                if invite.uses:
                    self.tracker.snapshots[invite.guild.id] = None
                else:
                    snapshot[invite.code] = (0, invite.inviter.id if invite.inviter else None)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if member.bot:
            return
        try:
            if (await data.tool_settings(member.guild.id))['tracking']:
                await self.tracker.joined(member)
        except Exception:
            logger.warning('Invite event could not be recorded.')

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        try:
            if (await data.tool_settings(member.guild.id))['tracking']:
                await self.tracker.left(member)
        except Exception:
            logger.warning('Invite departure could not be recorded.')

    @tasks.loop(seconds=60)
    async def refresh(self):
        async with self.scan_lock:
            for guild in self.bot.guilds:
                for refresh in (self.refresh_dashboard, self.refresh_alerts):
                    try:
                        await refresh(guild)
                    except Exception as error:
                        logger.warning(f'Server tools refresh failed for {guild.id} ({type(error).__name__}).')

    async def refresh_dashboard(self, guild):
        cfg = await data.tool_settings(guild.id)
        if not cfg['dashboard_message']:
            return
        channel = guild.get_channel(cfg['dashboard_channel'])
        if not channel:
            return
        if channel.permissions_for(guild.default_role).view_channel:
            await data.configure(guild.id, dashboard_message=0)
            return
        panic = await safety.settings('panic', guild.id)
        lockdown = panic['lockdown']
        automod = self.bot.cache.automod.get(str(guild.id), {})
        state = ('restoration pending' if lockdown.get('restore_pending') else 'active') if lockdown else 'inactive'
        remaining = len(lockdown['channels']) if lockdown else 0
        body = (f'## Kuroo staff dashboard\nLatency: {round(self.bot.latency * 1000)}ms • '
                f'Shards: {len(getattr(self.bot, "shards", {}))}/{self.bot.shard_count or 1}\n'
                f'Server members: {guild.member_count or 0}\n'
                f'Panic alerts: {"on" if panic["enabled"] else "off"} • Lockdown: {state}\n'
                f'Permission snapshots pending: {remaining}\n'
                f'Spam protection: {"on" if automod.get("antispam_enabled", True) else "off"}\n'
                f'Invite tracking: {"on" if cfg["tracking"] else "off"}\n'
                f'Updated: <t:{int(discord.utils.utcnow().timestamp())}:R>')
        try:
            message = await channel.fetch_message(cfg['dashboard_message'])
            await message.edit(content=body, allowed_mentions=discord.AllowedMentions.none())
        except discord.NotFound:
            await data.configure(guild.id, dashboard_message=0)

    async def refresh_alerts(self, guild):
        coll = await data.get_collection('creator_alerts')
        async for cfg in coll.find({'guild_id': guild.id}):
            try:
                latest = await self.feeds.latest(cfg['provider'], cfg['identity'])
                if latest is None or latest['id'] == cfg.get('last_id'):
                    continue
                channel = guild.get_channel(cfg['channel_id'])
                if channel:
                    await channel.send(f'**{discord.utils.escape_markdown(cfg["identity"])}** — '
                        f'{discord.utils.escape_markdown(latest["title"])}\n{latest["url"]}',
                        allowed_mentions=discord.AllowedMentions.none())
                    await coll.update_one({'_id': cfg['_id']}, {'$set': {'last_id': latest['id']}})
            except Exception as error:
                logger.warning(f'Creator feed refresh failed ({type(error).__name__}).')

    @commands.hybrid_group(name='servertools', invoke_without_command=True,
                           help='Manage invite tracking, staff dashboard and creator alerts')
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def tools(self, ctx):
        await ctx.send('Use /servertools tracking, invites, inviteinfo, dashboard, dashboardoff, alertadd, alertremove or alerts.', ephemeral=True)

    @tools.command(name='tracking', help='Enable or disable invite tracking (requires bot Manage Server)')
    @commands.has_guild_permissions(manage_guild=True)
    async def tracking(self, ctx, enabled: bool):
        await ctx.defer(ephemeral=True)
        if enabled:
            await self.tracker.prime(ctx.guild)
            if self.tracker.snapshots.get(ctx.guild.id) is None:
                raise data.ToolsError('I need Manage Server and access to invites to enable tracking.')
        else:
            self.tracker.snapshots.pop(ctx.guild.id, None)
        await data.configure(ctx.guild.id, tracking=enabled)
        await ctx.send(f'Invite tracking {"enabled" if enabled else "disabled"}. Past records are retained.', ephemeral=True)

    @tools.command(name='invites', help='Show recorded join and departure totals for an inviter')
    @commands.has_guild_permissions(manage_guild=True)
    async def invites(self, ctx, member: discord.Member):
        coll = await data.get_collection('invite_events')
        query = {'guild_id': ctx.guild.id, 'inviter_id': member.id}
        total = await coll.count_documents(query)
        left = await coll.count_documents(query | {'left': True})
        await ctx.send(f'{member.display_name}: {total} attributed joins, {left} departures, {total-left} memberships without a recorded departure. '
                       'Includes rejoins; unknown joins are excluded.', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @tools.command(name='inviteinfo', help='Show the evidence for a member’s most recent recorded join')
    @commands.has_guild_permissions(manage_guild=True)
    async def inviteinfo(self, ctx, member: discord.Member):
        coll = await data.get_collection('invite_events')
        row = await coll.find_one({'guild_id': ctx.guild.id, 'user_id': member.id}, sort=[('joined_at', -1)])
        text = 'No join has been recorded for this member.'
        if row:
            text = f'Inviter: {row["inviter_id"] or "Unknown"}\nEvidence: {row["reason"]}\nJoined: {row["joined_at"]}'
        await ctx.send(text, ephemeral=True)

    @tools.command(name='dashboard', help='Create a staff dashboard refreshed every minute')
    @commands.has_guild_permissions(manage_guild=True)
    async def dashboard(self, ctx, channel: discord.TextChannel):
        async with self.config_locks[ctx.guild.id]:
            await ctx.defer(ephemeral=True)
            if channel.permissions_for(ctx.guild.default_role).view_channel:
                raise data.ToolsError('Choose a private staff channel hidden from @everyone.')
            if not channel.permissions_for(ctx.guild.me).send_messages:
                raise data.ToolsError('I need permission to send messages in that channel.')
            cfg = await data.tool_settings(ctx.guild.id)
            if cfg['dashboard_message']:
                raise data.ToolsError('Disable the existing dashboard with /servertools dashboardoff first.')
            message = await channel.send('Preparing Kuroo staff dashboard…')
            await data.configure(ctx.guild.id, dashboard_channel=channel.id, dashboard_message=message.id)
            await self.refresh_dashboard(ctx.guild)
            await ctx.send('Staff dashboard created.', ephemeral=True)

    @tools.command(name='dashboardoff', help='Stop updating and remove the current staff dashboard')
    @commands.has_guild_permissions(manage_guild=True)
    async def dashboardoff(self, ctx):
        async with self.config_locks[ctx.guild.id]:
            cfg = await data.tool_settings(ctx.guild.id)
            channel = ctx.guild.get_channel(cfg['dashboard_channel'])
            await data.configure(ctx.guild.id, dashboard_message=0)
            if channel and cfg['dashboard_message']:
                try:
                    await (await channel.fetch_message(cfg['dashboard_message'])).delete()
                except discord.HTTPException:
                    pass
            await ctx.send('Staff dashboard disabled. If the old message could not be deleted, remove it manually.', ephemeral=True)

    @tools.command(name='alertadd', help='Subscribe to a YouTube channel ID or Twitch login')
    @commands.has_guild_permissions(manage_guild=True)
    async def alertadd(self, ctx, provider: Literal['youtube', 'twitch'], identity: str, channel: discord.TextChannel):
        async with self.config_locks[ctx.guild.id]:
            await ctx.defer(ephemeral=True)
            identity = identity.strip().lower() if provider == 'twitch' else identity.strip()
            coll = await data.get_collection('creator_alerts')
            if await coll.count_documents({'guild_id': ctx.guild.id}) >= 10:
                raise data.ToolsError('At most 10 creator subscriptions per server are supported.')
            if not channel.permissions_for(ctx.guild.me).send_messages:
                raise data.ToolsError('I need permission to send in the alert channel.')
            latest = await self.feeds.latest(provider, identity)
            await coll.update_one({'_id': f'{ctx.guild.id}:{provider}:{identity}'}, {'$set': {
                'guild_id': ctx.guild.id, 'provider': provider, 'identity': identity, 'channel_id': channel.id,
                'last_id': latest['id'] if latest else None}}, upsert=True)
            await ctx.send('Creator subscription saved. Only new uploads or streams will be announced.', ephemeral=True)

    @tools.command(name='alertremove', help='Remove a creator subscription')
    @commands.has_guild_permissions(manage_guild=True)
    async def alertremove(self, ctx, provider: Literal['youtube', 'twitch'], identity: str):
        async with self.config_locks[ctx.guild.id]:
            coll = await data.get_collection('creator_alerts')
            identity = identity.lower().strip() if provider == 'twitch' else identity.strip()
            await coll.delete_one({'_id': f'{ctx.guild.id}:{provider}:{identity}'})
            await ctx.send('Creator subscription removed.', ephemeral=True)

    @tools.command(name='alerts', help='List this server’s creator subscriptions')
    @commands.has_guild_permissions(manage_guild=True)
    async def alerts(self, ctx):
        coll = await data.get_collection('creator_alerts')
        rows = await coll.find({'guild_id': ctx.guild.id}).limit(10).to_list(10)
        await ctx.send('\n'.join(f'{r["provider"]}: {r["identity"]} → <#{r["channel_id"]}>' for r in rows)
                       or 'No creator subscriptions.', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
