"""Durable native rule reconciliation and bounded local spam enforcement."""
import asyncio
from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import re
import time
from uuid import uuid4

import discord
from pymongo.errors import DuplicateKeyError, PyMongoError

from kuroo.bridge.storage import get_collection
from kuroo.memory.cache import cache
from kuroo.console.logging import logger
from kuroo.src.checks import checks
from kuroo.workflows.automod_config import (
    RULES, LEGACY_GROUPS, WORD_RULES, LIMITS, LEGACY_LIMITS,
    ConfigError, default_config, config_from_record, validate, rule_payload,
    fingerprint, native_fingerprint, keyword_matches, emoji_count,
)
import storage.automod


class AutoModService:
    def __init__(self, bot):
        self.bot = bot
        self.locks = {}  # discarded on guild removal; one lock per connected guild
        self.suppressed = set()  # ignore our own gateway changes while a sync is running
        self.local = OrderedDict()  # capped across all guilds and members
        self.local_limit = 10000

    def lock(self, guild_id):
        return self.locks.setdefault(guild_id, asyncio.Lock())

    async def load(self, guild_id):
        try:
            record = await storage.automod.insert(guild_id=guild_id, native_config=default_config(),
                                                  antilink_enabled=True, antibadwords_enabled=True)
        except DuplicateKeyError:
            record = await storage.automod.get(guild_id=guild_id)
        if not record:
            raise ConfigError('AutoMod settings are unavailable; retry after the database recovers.')
        return record, config_from_record(record)

    async def save(self, record, config, **extra):
        validate(config)
        legacy = {group + '_enabled': any(config['rules'][key]['enabled'] for key in keys)
                  for group, keys in LEGACY_GROUPS.items()}
        legacy.update({field: config['limits'][key] for key, field in LEGACY_LIMITS.items()})
        legacy.update(antispam_punishment=config['punishment'],
                      antibadwords_words=config['rules']['words']['words'])
        result = await storage.automod.update(id=record['id'], guild_id=record['guild_id'],
                                             native_config=deepcopy(config), **legacy, **extra)
        if result is None:
            raise ConfigError('AutoMod settings could not be saved.')
        return result

    def owned_key(self, rule, record):
        if rule.creator_id != self.bot.user.id:
            return None
        for key in RULES:
            legacy = {'links': 'antilink_rule_id', 'words': 'antibadwords_rule_id'}.get(key)
            if (rule.name == 'Kuroo AutoMod: ' + key or
                str(rule.id) == str((record.get('native_rule_ids') or {}).get(key)) or
                (legacy and str(rule.id) == str(record.get(legacy)))):
                if rule.trigger.type.value == RULES[key][1]:
                    return key
        return None

    async def reset(self, guild):
        def restore(config):
            config.clear()
            config.update(default_config())
        return await self.configure(guild, restore)

    async def configure(self, guild, change):
        async with self.lock(guild.id):
            record, config = await self.load(guild.id)
            change(config)
            record = await self.save(record, config, native_pending=list(RULES))  # intent must survive partial Discord failure
            return await self._sync(guild, record, config, apply=True)

    async def sync(self, guild, *, apply=True):
        async with self.lock(guild.id):
            record, config = await self.load(guild.id)
            if apply:
                record = await self.save(record, config, native_pending=list(RULES))
            return await self._sync(guild, record, config, apply=apply)

    async def _sync(self, guild, record, config, *, apply, install_missing=False):
        statuses = {}
        pending = set(record.get('native_pending') or [])
        completed = set()
        if not guild.me or not guild.me.guild_permissions.manage_guild:
            statuses = {key: 'blocked: bot needs Manage Server' for key in RULES}
            await self.save(record, config, native_status=statuses)
            return statuses
        try:
            native = await guild.fetch_automod_rules()
        except discord.HTTPException as error:
            statuses = {key: f'blocked: Discord HTTP {error.status}; retry /automod sync' for key in RULES}
            await self.save(record, config, native_status=statuses)
            return statuses
        managed = {}
        for rule in native:
            key = self.owned_key(rule, record)
            if key:
                managed.setdefault(key, []).append(rule)
        ids = dict(record.get('native_rule_ids') or {})
        self.suppressed.add(guild.id)
        try:
            for key in RULES:
                write = apply or (install_missing and key in pending)
                existing = managed.get(key, [])
                if len(existing) > 1:
                    statuses[key] = 'conflict: duplicate owned rules; remove duplicates in Discord first'
                    continue
                rule = existing[0] if existing else None
                if rule:
                    ids[key] = rule.id
                if key in pending and not write:
                    current = ('enabled' if rule.enabled else 'disabled') if rule else 'missing'
                    statuses[key] = f'pending: Discord currently {current}; retry /automod sync'
                    continue
                # Startup/status reads adopt native toggles, never re-enable an admin opt-out.
                if not rule and not write and ids.get(key):
                    config['rules'][key]['enabled'] = False  # deleted while Kuroo was offline
                if rule and not write:
                    config['rules'][key]['enabled'] = rule.enabled
                try:
                    if not config['rules'][key]['enabled']:
                        if rule and write and rule.enabled:
                            rule = await rule.edit(enabled=False, reason='Kuroo AutoMod administrator opt-out')
                        statuses[key] = 'disabled' if rule else 'disabled (not installed)'
                        completed.add(key)
                        continue
                    payload = rule_payload(key, config)
                    if any(a.type == discord.AutoModRuleActionType.timeout for a in payload['actions']):
                        if not guild.me.guild_permissions.moderate_members:
                            raise ConfigError('blocked: bot needs Moderate Members for configured timeouts')
                    alert = config['alert_channel']
                    if alert:
                        channel = guild.get_channel(alert)
                        if not channel or not channel.permissions_for(guild.me).send_messages or not channel.permissions_for(guild.me).view_channel:
                            raise ConfigError('blocked: alert channel must be visible and writable by the bot')
                    if rule and (rule.event_type != payload['event_type']):
                        raise ConfigError('conflict: existing rule has a different event type')
                    if not rule:
                        # Status only observes. Startup calls provision explicitly below.
                        if not write and not install_missing:
                            statuses[key] = 'missing: run /automod sync'
                            continue
                        maximum = 6 if RULES[key][1] == 1 else 1
                        if sum(r.trigger.type.value == RULES[key][1] for r in native) >= maximum:
                            raise ConfigError('conflict: Discord trigger slots are occupied; existing rules were preserved')
                        rule = await guild.create_automod_rule(**payload, reason='Kuroo ten-rule AutoMod setup')
                        native.append(rule)
                        ids[key] = rule.id
                    elif write and fingerprint(payload) != native_fingerprint(rule):
                        rule = await rule.edit(**payload, reason='Kuroo AutoMod configuration')
                    statuses[key] = ('enabled' if rule.enabled else 'disabled')
                    if not write and fingerprint(payload) != native_fingerprint(rule):
                        statuses[key] += ' (settings differ; /automod sync to apply saved settings)'
                    else:
                        completed.add(key)
                except ConfigError as error:
                    statuses[key] = str(error)
                    # An empty word list must never leave the old blocking list enabled.
                    if rule and write and not config['rules'][key]['words'] and key in WORD_RULES:
                        try:
                            await rule.edit(enabled=False, reason='Kuroo empty word filter')
                        except discord.HTTPException:
                            statuses[key] += '; could not disable existing rule'
                except discord.HTTPException as error:
                    statuses[key] = f'blocked: Discord HTTP {error.status} (code {error.code}); retry /automod sync'
            await self.save(record, config, native_rule_ids=ids, native_status=statuses, native_pending=sorted(pending - completed))
        finally:
            self.suppressed.discard(guild.id)
        return statuses

    async def provision(self, guild):
        """Adopt native toggles and install missing rules without overwriting metadata."""
        async with self.lock(guild.id):
            record, config = await self.load(guild.id)
            return await self._sync(guild, record, config, apply=False, install_missing=True)

    async def observe(self, rule, deleted=False):
        if rule.guild.id in self.suppressed:
            return
        async with self.lock(rule.guild.id):
            record, config = await self.load(rule.guild.id)
            key = self.owned_key(rule, record)
            if not key or key in (record.get('native_pending') or []):
                return
            # Gateway events may arrive after our REST response or out of order.
            # Re-read Discord instead of persisting a stale event's toggle.
            current = await rule.guild.fetch_automod_rules()
            found = next((item for item in current if item.id == rule.id), None)
            if found:
                config['rules'][key]['enabled'] = found.enabled
            elif deleted:
                if (record.get('native_rule_ids') or {}).get(key) not in (None, rule.id):
                    return  # old deletion after a replacement rule was installed
                config['rules'][key]['enabled'] = False
            else:
                return
            ids = dict(record.get('native_rule_ids') or {})
            ids[key] = rule.id
            await self.save(record, config, native_rule_ids=ids)

    async def process_message(self, message, *, edited=False):
        if not message.guild or message.author.bot:
            return False
        member = message.author
        if not isinstance(member, discord.Member):
            return False
        if await checks.check_is_owner_raw(member, message.guild):
            return False
        permissions = member.guild_permissions
        if member.id == message.guild.owner_id or any(getattr(permissions, name) for name in
            ('administrator', 'manage_messages', 'manage_guild', 'manage_roles', 'manage_channels')):
            return False
        record = cache.automod.get(str(message.guild.id))
        if not record:
            record, config = await self.load(message.guild.id)
        else:
            config = config_from_record(record)
        roles = {role.id for role in member.roles}
        channel_ids = {message.channel.id, getattr(message.channel, 'parent_id', None)}
        def active(key):
            rule = config['rules'][key]
            return (rule['enabled'] and not roles.intersection(rule['roles'])
                    and not channel_ids.intersection(rule['channels'])
                    and not any(keyword_matches(word, message.content) for word in rule['allow']))
        limits = config['limits']
        now = time.monotonic()
        state_key = (message.guild.id, member.id)
        state = self.local.pop(state_key, None) or {'messages': deque(maxlen=30), 'last': 0, 'seen': deque(maxlen=60)}
        self.local[state_key] = state
        while len(self.local) > self.local_limit:
            self.local.popitem(last=False)
        if (message.id, hashlib.blake2s(message.content.encode(), digest_size=8).digest()) in state['seen']:
            return False
        state['seen'].append((message.id, hashlib.blake2s(message.content.encode(), digest_size=8).digest()))
        while state['messages'] and now - state['messages'][0][0] > limits['interval']:
            state['messages'].popleft()
        reason = None
        digest = hashlib.blake2s(message.content.casefold().strip().encode(), digest_size=8).digest()
        if not edited and active('spam'):
            state['messages'].append((now, digest))
            if len(state['messages']) >= limits['messages']:
                reason = 'message flood'
            elif message.content.strip() and sum(item[1] == digest for item in state['messages']) >= min(3, limits['messages']):
                reason = 'duplicate messages'
        if active('emoji') and emoji_count(message.content) >= limits['emoji']:
            reason = 'excessive emoji'
        letters = [char for char in message.content if char.isalpha()]
        if active('caps') and len(letters) >= 8 and sum(char.isupper() for char in letters) / len(letters) * 100 >= limits['caps']:
            reason = 'excessive capitals'
        if active('mentions') and len({('role' if role else 'user', user or role) for user, role in re.findall(r'<@!?([0-9]+)>|<@&([0-9]+)>', message.content)}) > limits['mentions']:
            reason = 'mention spam'
        if not reason:
            return False
        # Delete only the offending message. A cooldown prevents repeated punishments.
        try:
            await message.delete()
        except discord.NotFound:
            return True
        except discord.HTTPException as error:
            logger.warning(f'AutoMod delete failed in guild {message.guild.id}: HTTP {error.status}')
            return False
        if now - state['last'] >= limits['interval']:
            state['last'] = now
            await self.punish(message, config, reason)
        return True

    async def punish(self, message, config, reason):
        member, me = message.author, message.guild.me
        action = config['punishment']
        permission = {'mute': 'moderate_members', 'kick': 'kick_members', 'ban': 'ban_members', 'tempban': 'ban_members'}.get(action)
        if not me or (permission and not getattr(me.guild_permissions, permission)):
            logger.warning(f'AutoMod {action} blocked by missing bot permission in guild {message.guild.id}')
            return
        if action != 'warn' and (member.id == message.guild.owner_id or member.top_role >= me.top_role):
            logger.warning(f'AutoMod punishment blocked by role hierarchy in guild {message.guild.id}')
            return
        try:
            if action == 'mute':
                await member.timeout(timedelta(minutes=config['limits']['timeout']), reason='AutoMod: ' + reason)
            elif action == 'kick':
                await member.kick(reason='AutoMod: ' + reason)
            elif action == 'ban':
                await member.ban(reason='AutoMod: ' + reason, delete_message_seconds=0)
            elif action == 'tempban':
                await self.temporary_ban(message.guild, member, config['limits']['timeout'])
            elif action == 'warn':
                await message.channel.send(f'{member.mention}: AutoMod removed your message ({reason}).',
                                           allowed_mentions=discord.AllowedMentions.none(), delete_after=10)
        except PyMongoError:
            logger.warning(f'AutoMod {action} could not persist recovery state in guild {message.guild.id}')
            return
        except discord.HTTPException as error:
            logger.warning(f'AutoMod {action} failed in guild {message.guild.id}: HTTP {error.status}')
            return
        channel = message.guild.get_channel(config['alert_channel']) if config['alert_channel'] else None
        if channel:
            try:
                await channel.send(f'AutoMod: {action} applied to member {member.id} for {reason}.',
                                   allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                logger.warning(f'AutoMod local alert failed in guild {message.guild.id}')


    async def temporary_ban(self, guild, member, minutes):
        """Save expiry before banning; an ambiguous REST failure remains recoverable."""
        jobs = await get_collection('automod_tempbans')
        token = uuid4().hex
        reason = 'Kuroo AutoMod temporary ban ' + token
        await jobs.replace_one(
            {'_id': f'{guild.id}:{member.id}'},
            {'_id': f'{guild.id}:{member.id}', 'guild_id': guild.id, 'member_id': member.id,
             'token': token, 'reason': reason,
             'expires_at': datetime.now(timezone.utc) + timedelta(minutes=minutes)}, upsert=True)
        await member.ban(reason=reason, delete_message_seconds=0)

    async def expire_temporary_bans(self):
        jobs = await get_collection('automod_tempbans')
        await jobs.create_index('expires_at')  # ordinary index, never TTL-delete recovery jobs
        due = jobs.find({'expires_at': {'$lte': datetime.now(timezone.utc)}}).sort('expires_at', 1).limit(100)
        async for job in due:
            guild = self.bot.get_guild(job['guild_id'])
            if not guild or not guild.me or not guild.me.guild_permissions.ban_members:
                continue  # preserve retry information until permissions/access recover
            user = discord.Object(id=job['member_id'])
            try:
                entry = await guild.fetch_ban(user)
                # A later manual permanent ban must not be undone by the old timer.
                if entry.reason == job['reason']:
                    await guild.unban(user, reason='Kuroo AutoMod temporary ban expired')
            except discord.NotFound:
                pass
            except discord.HTTPException as error:
                logger.warning(f'AutoMod temporary unban delayed in guild {guild.id}: HTTP {error.status}')
                continue
            await jobs.delete_one({'_id': job['_id'], 'token': job['token']})
