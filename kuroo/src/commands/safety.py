"""Panic Shield and opt-in interest-role onboarding for Kuroo."""
import asyncio
from collections import defaultdict, deque
import time
from types import SimpleNamespace

import discord
from discord.ext import commands, tasks
from pymongo.errors import PyMongoError

from kuroo.console.logging import logger
from kuroo.src.checks import checks
from kuroo.workflows import safety


def eligible(ctx):
    return (ctx.guild is not None and checks.check_blacklist_predicate(ctx)
            and checks.check_ignore_predicate(ctx))


class SafetyView(discord.ui.View):
    async def interaction_check(self, interaction):
        ctx = SimpleNamespace(guild=interaction.guild, author=interaction.user,
                              channel=interaction.channel)
        if not eligible(ctx):
            await interaction.response.send_message('This action is unavailable here.', ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error, item):
        text = str(error) if isinstance(error, safety.SafetyError) else 'The action could not finish. Check bot permissions and retry.'
        if not isinstance(error, safety.SafetyError):
            logger.warning(f'Safety interaction failed ({type(error).__name__}).')
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


class PanicControls(SafetyView):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, interaction):
        if not await super().interaction_check(interaction):
            return False
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message('Manage Server is required.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Start 10-minute lockdown', style=discord.ButtonStyle.danger,
                       custom_id='kuroo:panic:start')
    async def start(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        result = await self.cog.lockdowns.start(interaction.guild, interaction.message.id)
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label='Undo lockdown', style=discord.ButtonStyle.success,
                       custom_id='kuroo:panic:undo')
    async def undo(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await self.cog.lockdowns.restore(interaction.guild), ephemeral=True)

    @discord.ui.button(label='Dismiss alert', custom_id='kuroo:panic:dismiss')
    async def dismiss(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        coll = await safety.get_collection('safety_panic')
        await coll.update_one({'_id': interaction.guild.id, 'alert_id': interaction.message.id},
                              {'$unset': {'alert_id': ''}})
        await interaction.followup.send('Alert dismissed. An active lockdown still expires or can be undone.', ephemeral=True)


class InterestSelect(discord.ui.Select):
    def __init__(self, cog, member, roles, configured_ids):
        self.cog = cog
        self.member_id = member.id
        self.configured_ids = set(configured_ids)
        super().__init__(placeholder='Choose your interests (clear all to remove them)',
                         min_values=0, max_values=len(roles), options=[
                             discord.SelectOption(label=role.name[:100], value=str(role.id),
                                                  default=role in member.roles) for role in roles])

    async def callback(self, interaction):
        if interaction.user.id != self.member_id:
            return await interaction.response.send_message('Open your own interest selector.', ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self.cog.assign_interests(interaction.guild, interaction.user.id,
                                       {int(value) for value in self.values}, self.configured_ids)
        await interaction.followup.send('Your interest roles were updated.', ephemeral=True)


class OnboardControls(SafetyView):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label='Choose my interests', style=discord.ButtonStyle.primary,
                       custom_id='kuroo:onboard:choose')
    async def choose(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        cfg = await safety.settings('onboard', interaction.guild.id)
        if not cfg['enabled']:
            raise safety.SafetyError('Onboarding is disabled in this server.')
        member = await interaction.guild.fetch_member(interaction.user.id)
        roles = [r for r in await interaction.guild.fetch_roles() if r.id in cfg['roles'] and safety.safe_role(r)]
        if not roles:
            raise safety.SafetyError('No safe interest roles are available. Ask a server manager to configure them.')
        view = SafetyView(timeout=180)
        view.add_item(InterestSelect(self.cog, member, roles, cfg['roles']))
        await interaction.followup.send('Select the interests you want. Only configured interest roles are changed.',
                                        view=view, ephemeral=True)


class Safety(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lockdowns = safety.Lockdowns()
        self.join_times = defaultdict(lambda: deque(maxlen=20))
        self.event_locks = defaultdict(asyncio.Lock)
        self.role_locks = defaultdict(asyncio.Lock)
        self.panic_view = PanicControls(self)
        self.onboard_view = OnboardControls(self)
        self.cog_info = SimpleNamespace(name='Safety', category='Security',
                                       description='Raid alerts, temporary lockdowns and interest roles',
                                       hidden=False, emoji='🛡️')

    async def cog_load(self):
        self.bot.add_view(self.panic_view)
        self.bot.add_view(self.onboard_view)

    def cog_unload(self):
        self.expire_lockdowns.cancel()
        self.panic_view.stop()
        self.onboard_view.stop()

    async def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return eligible(ctx)

    async def cog_command_error(self, ctx, error):
        cause = getattr(error, 'original', error)
        if isinstance(cause, safety.SafetyError):
            await ctx.send(str(cause), ephemeral=True)
        elif isinstance(cause, (PyMongoError, discord.HTTPException)):
            logger.warning(f'Safety command failed ({type(cause).__name__}).')
            await ctx.send('The action could not finish. Check storage and bot permissions, then retry.', ephemeral=True)
        else:
            raise error

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.expire_lockdowns.is_running():
            self.expire_lockdowns.start()

    @tasks.loop(seconds=15)
    async def expire_lockdowns(self):
        try:
            for guild_id in await self.lockdowns.due_guilds():
                guild = self.bot.get_guild(guild_id)
                if guild:
                    try:
                        await self.lockdowns.restore(guild)
                    except Exception as error:
                        logger.warning(f'Lockdown restoration pending for {guild_id} ({type(error).__name__}).')
        except Exception as error:
            logger.warning(f'Lockdown scan failed ({type(error).__name__}); retrying next cycle.')

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if member.bot or not checks.check_blacklist_predicate(SimpleNamespace(guild=member.guild, author=member)):
            return
        for action in (self.raid_alert, self.welcome_interests):
            try:
                await action(member)
            except Exception as error:
                logger.warning(f'{action.__name__} failed ({type(error).__name__}).')

    async def raid_alert(self, member):
        guild = member.guild
        async with self.event_locks[guild.id]:
            cfg = await safety.settings('panic', guild.id)
            if not cfg['enabled']:
                return
            now = time.time()
            joins = self.join_times[guild.id]
            joins.append(now)
            while joins and joins[0] < now - cfg['window']:
                joins.popleft()
            if len(joins) < cfg['threshold'] or now - cfg['last_alert'] < 300:
                return
            channel = guild.get_channel(cfg['alert_channel'])
            if not channel:
                return
            message = await channel.send(
                f'**Panic Shield:** {len(joins)} members joined within {cfg["window"]} seconds. '
                'Review the joins. A server manager can start a 10-minute lockdown below. '
                'No channels have been locked automatically.', view=self.panic_view,
                allowed_mentions=discord.AllowedMentions.none())
            await safety.configure('panic', guild.id, last_alert=now, alert_id=message.id)

    async def welcome_interests(self, member):
        cfg = await safety.settings('onboard', member.guild.id)
        channel = member.guild.get_channel(cfg['welcome_channel'])
        rules = member.guild.get_channel(cfg['rules_channel'])
        if cfg['enabled'] and cfg['roles'] and channel and rules:
            await channel.send(f'Welcome, {discord.utils.escape_markdown(member.display_name)}! '
                               f'Read the rules in {rules.mention}, then choose your interests.',
                               view=self.onboard_view, allowed_mentions=discord.AllowedMentions.none())

    async def assign_interests(self, guild, member_id, chosen, opened_ids):
        async with self.role_locks[(guild.id, member_id)]:
            cfg = await safety.settings('onboard', guild.id)
            if not cfg['enabled'] or set(cfg['roles']) != opened_ids:
                raise safety.SafetyError('The interest settings changed. Open the selector again.')
            member = await guild.fetch_member(member_id)
            roles = {r.id: r for r in await guild.fetch_roles() if r.id in cfg['roles'] and safety.safe_role(r)}
            if not chosen <= roles.keys():
                raise safety.SafetyError('A selected role is no longer safe or assignable. Open the selector again.')
            if not guild.me or not guild.me.guild_permissions.manage_roles:
                raise safety.SafetyError('I need Manage Roles to assign interests.')
            existing = {r.id for r in member.roles}
            add = [roles[rid] for rid in chosen - existing]
            remove = [roles[rid] for rid in (existing & roles.keys()) - chosen]
            if add:
                await member.add_roles(*add, reason='Member selected onboarding interests')
            if remove:
                await member.remove_roles(*remove, reason='Member updated onboarding interests')

    @commands.hybrid_group(name='panic', invoke_without_command=True, help='Manage raid alerts and temporary lockdowns')
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def panic(self, ctx):
        await self.panic_status(ctx)

    @panic.command(name='setup', help='Enable join-spike alerts; lockdown always requires a manager click')
    @commands.has_guild_permissions(manage_guild=True)
    async def panic_setup(self, ctx, channel: discord.TextChannel,
                          threshold: commands.Range[int, 3, 20] = 5,
                          window: commands.Range[int, 10, 120] = 30):
        if not channel.permissions_for(ctx.guild.me).send_messages or not ctx.guild.me.guild_permissions.manage_channels:
            raise safety.SafetyError('I need Manage Channels and permission to send in the alert channel.')
        await safety.configure('panic', ctx.guild.id, enabled=True, alert_channel=channel.id,
                               threshold=threshold, window=window)
        await ctx.send(f'Panic Shield enabled: {threshold} joins in {window} seconds. Alerts go to {channel.mention}. '
                       'A manager must click to start lockdown.', ephemeral=True)

    @panic.command(name='status', help='Show raid alert and lockdown settings')
    @commands.has_guild_permissions(manage_guild=True)
    async def panic_status(self, ctx):
        cfg = await safety.settings('panic', ctx.guild.id)
        state = cfg['lockdown']
        text = (('Restoration pending' if state.get('restore_pending') else f'Active until <t:{int(state["expires_at"])}:R>')
                + f' • {len(state["channels"])} snapshots remaining') if state else 'Inactive'
        await ctx.send(f'Alerts: {"on" if cfg["enabled"] else "off"} • {cfg["threshold"]} joins / {cfg["window"]}s\n'
                       f'Lockdown: {text}', ephemeral=True)

    @panic.command(name='unlock', help='Restore recorded permissions; failed channels will be retried')
    @commands.has_guild_permissions(manage_guild=True)
    async def panic_unlock(self, ctx):
        await ctx.defer(ephemeral=True)
        await ctx.send(await self.lockdowns.restore(ctx.guild), ephemeral=True)

    @panic.command(name='off', help='Disable join alerts and restore any active lockdown')
    @commands.has_guild_permissions(manage_guild=True)
    async def panic_off(self, ctx):
        await ctx.defer(ephemeral=True)
        await safety.configure('panic', ctx.guild.id, enabled=False)
        self.join_times.pop(ctx.guild.id, None)
        await ctx.send('Alerts disabled. ' + await self.lockdowns.restore(ctx.guild), ephemeral=True)

    @commands.hybrid_group(name='onboard', invoke_without_command=True, help='Configure rules and self-assignable interest roles')
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def onboard(self, ctx):
        await self.onboard_status(ctx)

    @onboard.command(name='setup', help='Set rules and welcome channels for interest-role onboarding')
    @commands.has_guild_permissions(manage_guild=True)
    async def onboard_setup(self, ctx, rules: discord.TextChannel, welcome: discord.TextChannel):
        if not rules.permissions_for(ctx.guild.default_role).view_channel:
            raise safety.SafetyError('Choose a rules channel visible to @everyone.')
        if not welcome.permissions_for(ctx.guild.me).send_messages:
            raise safety.SafetyError('I need permission to send in the welcome channel.')
        await safety.configure('onboard', ctx.guild.id, enabled=True, rules_channel=rules.id, welcome_channel=welcome.id)
        await ctx.send('Onboarding enabled. Add interest roles with /onboard addrole, then use /onboard panel.', ephemeral=True)

    @onboard.command(name='addrole', help='Add a safe interest role (maximum 10)')
    @commands.has_guild_permissions(manage_guild=True, manage_roles=True)
    async def onboard_addrole(self, ctx, role: discord.Role):
        if not safety.safe_role(role) or (ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role):
            raise safety.SafetyError('Choose a regular role below both our highest roles with no moderation or management permissions.')
        async with self.event_locks[ctx.guild.id]:
            cfg = await safety.settings('onboard', ctx.guild.id)
            ids = list(dict.fromkeys(cfg['roles'] + [role.id]))
            if len(ids) > 10:
                raise safety.SafetyError('At most 10 interest roles can be configured.')
            await safety.configure('onboard', ctx.guild.id, roles=ids)
        await ctx.send('Interest role added.', ephemeral=True)

    @onboard.command(name='removerole', help='Stop offering a role; existing member roles are retained')
    @commands.has_guild_permissions(manage_guild=True, manage_roles=True)
    async def onboard_removerole(self, ctx, role: discord.Role):
        async with self.event_locks[ctx.guild.id]:
            cfg = await safety.settings('onboard', ctx.guild.id)
            await safety.configure('onboard', ctx.guild.id, roles=[rid for rid in cfg['roles'] if rid != role.id])
        await ctx.send('Role removed from the interest choices. Existing assignments are retained.', ephemeral=True)

    @onboard.command(name='status', help='Show onboarding channels and interest roles')
    @commands.has_guild_permissions(manage_guild=True)
    async def onboard_status(self, ctx):
        cfg = await safety.settings('onboard', ctx.guild.id)
        await ctx.send(f'Onboarding: {"on" if cfg["enabled"] else "off"}\n'
                       f'Rules: <#{cfg["rules_channel"]}> • Welcome: <#{cfg["welcome_channel"]}>\n'
                       f'Interest roles: {len(cfg["roles"])}/10', ephemeral=True)

    @onboard.command(name='panel', help='Post a persistent interest-role panel')
    @commands.has_guild_permissions(manage_guild=True)
    async def onboard_panel(self, ctx):
        cfg = await safety.settings('onboard', ctx.guild.id)
        rules = ctx.guild.get_channel(cfg['rules_channel'])
        if not cfg['enabled'] or not cfg['roles'] or not rules:
            raise safety.SafetyError('Enable onboarding and configure rules and at least one safe interest role first.')
        await ctx.send(f'Read the rules in {rules.mention}, then choose your interests.', view=self.onboard_view,
                       allowed_mentions=discord.AllowedMentions.none())

    @onboard.command(name='off', help='Disable onboarding panels and new-member prompts')
    @commands.has_guild_permissions(manage_guild=True)
    async def onboard_off(self, ctx):
        await safety.configure('onboard', ctx.guild.id, enabled=False)
        await ctx.send('Onboarding disabled. Existing member roles are retained.', ephemeral=True)
