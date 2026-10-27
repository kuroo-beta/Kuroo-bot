"""Administrator controls for Kuroo's ten native Discord AutoMod protections."""
import asyncio
from typing import Literal
from types import SimpleNamespace

import discord
from discord.ext import commands, tasks
from pymongo.errors import PyMongoError

from kuroo.console.logging import logger
from kuroo.src.checks import checks
from kuroo.workflows import automod

RuleName = Literal['links', 'invites', 'words', 'scams', 'emoji', 'caps', 'spam', 'language', 'mentions', 'profiles']


class Automod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.service = automod.AutoModService(bot)
        self.startup_lock = asyncio.Lock()
        self.cog_info = SimpleNamespace(name='Automod', category='Main', description='Ten native AutoMod protections',
                                        hidden=False, emoji=bot.emoji.AUTOMOD)

    async def cog_load(self):
        self.tempban_worker.start()

    def cog_unload(self):
        self.tempban_worker.cancel()

    @tasks.loop(seconds=30)
    async def tempban_worker(self):
        try:
            await self.service.expire_temporary_bans()
        except PyMongoError:
            logger.warning('AutoMod temporary ban recovery delayed; database unavailable')

    @tempban_worker.before_loop
    async def before_tempban_worker(self):
        await self.bot.wait_until_ready()

    async def cog_check(self, ctx):
        if not ctx.guild:
            raise commands.NoPrivateMessage()
        if not checks.check_ignore_predicate(ctx) or not checks.check_blacklist_predicate(ctx):
            return False
        return await checks.check_is_moderator_permissions(ctx, 'administrator')

    async def run(self, ctx, change=None, *, observe=False):
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer(ephemeral=True)
        try:
            result = (await self.service.configure(ctx.guild, change) if change else
                      await self.service.sync(ctx.guild, apply=not observe))
            lines = [f'**{key}** — {result[key]}' for key in automod.RULES]
            enabled = sum(value == 'enabled' for value in result.values())
            await ctx.send(f'**AutoMod: {enabled}/10 native rules confirmed enabled**\n' + '\n'.join(lines)
                           + '\nSaved settings survive restarts. Resolve any blocked/conflict rows and run `/automod sync`.',
                           allowed_mentions=discord.AllowedMentions.none())
        except (automod.ConfigError, PyMongoError) as error:
            detail = str(error) if isinstance(error, automod.ConfigError) else 'Database unavailable; retry after it recovers.'
            await ctx.send('AutoMod: ' + detail, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_ready(self):
        # Discord reconnects can emit ready more than once; reconciliation is idempotent.
        async with self.startup_lock:
            for guild in self.bot.guilds:
                await self.provision(guild)

    async def provision(self, guild):
        try:
            result = await self.service.provision(guild)
            if any(value not in ('enabled', 'disabled') for value in result.values()):
                logger.warning(f'AutoMod setup needs administrator attention in guild {guild.id}; use /automod status')
        except (automod.ConfigError, PyMongoError, discord.HTTPException) as error:
            logger.warning(f'AutoMod setup failed in guild {guild.id} ({type(error).__name__})')

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        await self.provision(guild)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        self.service.locks.pop(guild.id, None)
        for key in list(self.service.local):
            if key[0] == guild.id:
                self.service.local.pop(key, None)

    @commands.Cog.listener()
    async def on_automod_rule_update(self, rule):
        try:
            await self.service.observe(rule)
        except (automod.ConfigError, PyMongoError, discord.HTTPException):
            logger.warning(f'AutoMod native toggle could not be saved in guild {rule.guild.id}')

    @commands.Cog.listener()
    async def on_automod_rule_delete(self, rule):
        try:
            await self.service.observe(rule, deleted=True)
        except (automod.ConfigError, PyMongoError, discord.HTTPException):
            logger.warning(f'AutoMod native deletion could not be saved in guild {rule.guild.id}')

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if before.content != after.content:
            await self.service.process_message(after, edited=True)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        if payload.cached_message or 'content' not in payload.data or not payload.guild_id:
            return
        channel = self.bot.get_channel(payload.channel_id)
        if channel:
            try:
                message = await channel.fetch_message(payload.message_id)
                await self.service.process_message(message, edited=True)
            except discord.HTTPException:
                logger.warning(f'AutoMod could not check uncached edit in guild {payload.guild_id}')

    @commands.hybrid_group(name='automod', invoke_without_command=True, help='Configure the ten native AutoMod rules')
    @commands.guild_only()
    async def automod_command(self, ctx):
        await self.run(ctx, observe=True)

    @automod_command.command(name='status', help='Show all ten rules and native configuration problems')
    async def status(self, ctx):
        await self.run(ctx, observe=True)

    @automod_command.command(name='sync', help='Install missing native rules and apply saved settings')
    @commands.cooldown(1, 15, commands.BucketType.guild)
    async def sync(self, ctx):
        await self.run(ctx)

    @automod_command.command(name='enable', help='Enable the ten AutoMod protections and sync Discord')
    async def automod_command_enable(self, ctx):
        def change(config):
            for rule in config['rules'].values():
                rule['enabled'] = True
        await self.run(ctx, change)

    @automod_command.command(name='disable', help='Disable the ten AutoMod protections and save the opt-out')
    async def automod_command_disable(self, ctx):
        def change(config):
            for rule in config['rules'].values():
                rule['enabled'] = False
        await self.run(ctx, change)

    @automod_command.command(name='rule', help='Enable or disable one native protection')
    async def rule(self, ctx, rule: RuleName, enabled: bool):
        await self.run(ctx, lambda config: config['rules'][rule].update(enabled=enabled))

    @automod_command.command(name='limit', help='Change a threshold; timeout is in minutes, interval in seconds')
    async def limit(self, ctx, setting: Literal['messages', 'interval', 'mentions', 'emoji', 'caps', 'timeout'], value: int):
        await self.run(ctx, lambda config: config['limits'].update({setting: value}))

    @automod_command.command(name='words', help='Replace words for a filter with comma-separated entries; use clear to empty')
    async def words(self, ctx, rule: Literal['words', 'scams', 'profiles'], *, entries: str):
        values = [] if entries.strip().lower() == 'clear' else list(dict.fromkeys(word.strip() for word in entries.split(',') if word.strip()))
        await self.run(ctx, lambda config: config['rules'][rule].update(words=values))

    @automod_command.command(name='allow', help='Replace allowed words for a rule; use clear to empty')
    async def allow(self, ctx, rule: Literal['links', 'invites', 'words', 'scams', 'emoji', 'caps', 'language', 'profiles'], *, entries: str):
        values = [] if entries.strip().lower() == 'clear' else list(dict.fromkeys(word.strip() for word in entries.split(',') if word.strip()))
        await self.run(ctx, lambda config: config['rules'][rule].update(allow=values))

    @automod_command.command(name='exemptrole', help='Add or remove a rule exemption for a role')
    async def exemptrole(self, ctx, rule: RuleName, role: discord.Role, exempt: bool = True):
        if role.is_default():
            return await ctx.send('Exempting @everyone would bypass protection for everyone. Select another role.')
        await self.exemption(ctx, rule, 'roles', role.id, exempt)

    @automod_command.command(name='exemptchannel', help='Add or remove a rule exemption for a channel or thread')
    async def exemptchannel(self, ctx, rule: RuleName, channel: discord.TextChannel, exempt: bool = True):
        await self.exemption(ctx, rule, 'channels', channel.id, exempt)

    async def exemption(self, ctx, rule, field, sid, exempt):
        def change(config):
            values = set(config['rules'][rule][field])
            values.add(sid) if exempt else values.discard(sid)
            config['rules'][rule][field] = sorted(values)
        await self.run(ctx, change)

    @automod_command.command(name='action', help='Set local spam punishment; native keyword and mention rules support mute')
    async def action(self, ctx, punishment: Literal['mute', 'warn', 'kick', 'ban', 'tempban']):
        await self.run(ctx, lambda config: config.update(punishment=punishment))

    @automod_command.command(name='alerts', help='Choose a native and local AutoMod alert channel, or clear it')
    async def alerts(self, ctx, channel: discord.TextChannel = None):
        await self.run(ctx, lambda config: config.update(alert_channel=channel.id if channel else None))

    @automod_command.command(name='presets', help='Toggle profanity, sexual content and slur language filters')
    async def presets(self, ctx, profanity: bool = True, sexual_content: bool = True, slurs: bool = True):
        await self.run(ctx, lambda config: config.update(presets=[i for i, enabled in enumerate((profanity, sexual_content, slurs), 1) if enabled]))

    @automod_command.command(name='settings', help='Show saved limits, lists and exemptions without changing them')
    async def settings(self, ctx, rule: RuleName = 'spam'):
        try:
            _, config = await self.service.load(ctx.guild.id)
            data = config['rules'][rule]
            text = (f'**{rule}** — enabled: {data["enabled"]}\n'
                    f'Limits: {config["limits"]}\nLocal action: {config["punishment"]}\n'
                    f'Words: {", ".join(data["words"]) or "none"}\n'
                    f'Allowed: {", ".join(data["allow"]) or "none"}\n'
                    f'Exempt role IDs: {data["roles"]}\nExempt channel IDs: {data["channels"]}')
            # Lists can exceed a Discord message; return a private in-memory attachment.
            if len(text) > 1800:
                import io
                await ctx.send(file=discord.File(io.BytesIO(text.encode()), filename='automod-settings.txt'))
            else:
                await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())
        except (automod.ConfigError, PyMongoError):
            await ctx.send('AutoMod settings are unavailable; retry after the database recovers.')

    @automod_command.command(name='badge', help='Check the Discord-controlled AutoMod application badge flag')
    async def badge(self, ctx):
        if ctx.interaction:
            await ctx.defer(ephemeral=True)
        try:
            app = await self.bot.application_info()
            await ctx.send(f'AutoMod badge flag: **{"present" if app.flags.auto_mod_badge else "not present"}**. '
                           'Discord controls eligibility and display. The discord.py documentation describes '
                           'at least 100 application-owned AutoMod rules across guilds; ten in one server does not guarantee a badge. '
                           'Use rules to protect real communities, and keep native permissions working.')
        except discord.HTTPException:
            await ctx.send('Could not fetch the application badge flag from Discord; retry later.')

    async def legacy(self, ctx, group, enabled=None):
        if enabled is None:
            return await self.run(ctx, observe=True)
        def change(config):
            for key in automod.LEGACY_GROUPS[group]:
                config['rules'][key]['enabled'] = enabled
        await self.run(ctx, change)

    @automod_command.command(name='antilink', help='Show link protection; configure with /automod rule links')
    async def automod_command_antilink(self, ctx):
        await self.legacy(ctx, 'antilink')

    @automod_command.command(name='antibadwords', help='Show word protection; configure with /automod words')
    async def automod_command_antibadwords(self, ctx):
        await self.legacy(ctx, 'antibadwords')

    @automod_command.command(name='antispam', help='Show spam protection; configure with /automod limit')
    async def automod_command_antispam(self, ctx):
        await self.legacy(ctx, 'antispam')

    @commands.hybrid_group(name='antispam', invoke_without_command=True, help='Manage spam, mentions, caps and emoji protection')
    @commands.guild_only()
    async def antispam(self, ctx):
        await self.legacy(ctx, 'antispam')

    @antispam.command(name='enable')
    async def antispam_enable(self, ctx):
        await self.legacy(ctx, 'antispam', True)

    @antispam.command(name='disable')
    async def antispam_disable(self, ctx):
        await self.legacy(ctx, 'antispam', False)

    @antispam.command(name='settings')
    async def antispam_settings(self, ctx):
        await self.settings.callback(self, ctx, 'spam')

    @commands.hybrid_group(name='antilink', invoke_without_command=True, help='Manage link and invite protection')
    @commands.guild_only()
    async def antilink(self, ctx):
        await self.legacy(ctx, 'antilink')

    @antilink.command(name='enable')
    async def antilink_enable(self, ctx):
        await self.legacy(ctx, 'antilink', True)

    @antilink.command(name='disable')
    async def antilink_disable(self, ctx):
        await self.legacy(ctx, 'antilink', False)

    @antilink.command(name='settings')
    async def antilink_settings(self, ctx):
        await self.settings.callback(self, ctx, 'links')

    @commands.hybrid_group(name='antibadwords', invoke_without_command=True, help='Manage the custom word protection')
    @commands.guild_only()
    async def antibadwords(self, ctx):
        await self.legacy(ctx, 'antibadwords')

    @antibadwords.command(name='enable')
    async def antibadwords_enable(self, ctx):
        await self.legacy(ctx, 'antibadwords', True)

    @antibadwords.command(name='disable')
    async def antibadwords_disable(self, ctx):
        await self.legacy(ctx, 'antibadwords', False)

    @antibadwords.command(name='settings')
    async def antibadwords_settings(self, ctx):
        await self.settings.callback(self, ctx, 'words')
