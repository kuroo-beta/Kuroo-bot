"""Mongo-backed raid response and onboarding; Discord effects stay in the cog/service."""
import asyncio
from collections import defaultdict
from copy import deepcopy
import time
from uuid import uuid4

import discord

from kuroo.bridge.storage import get_collection

PANIC_DEFAULTS = {'enabled': False, 'alert_channel': 0, 'threshold': 5, 'window': 30,
                  'last_alert': 0, 'lockdown': None}
ONBOARD_DEFAULTS = {'enabled': False, 'rules_channel': 0, 'welcome_channel': 0, 'roles': []}
SEND_FIELDS = ('send_messages', 'send_messages_in_threads')


class SafetyError(Exception):
    pass


async def settings(kind, guild_id):
    defaults = PANIC_DEFAULTS if kind == 'panic' else ONBOARD_DEFAULTS
    coll = await get_collection(f'safety_{kind}')
    return deepcopy(defaults) | (await coll.find_one({'_id': guild_id}) or {})


async def configure(kind, guild_id, **values):
    coll = await get_collection(f'safety_{kind}')
    await coll.update_one({'_id': guild_id}, {'$set': values}, upsert=True)


def safe_role(role):
    if role is None or role.is_default() or role.managed or not role.is_assignable():
        return False
    forbidden = {'administrator', 'kick_members', 'ban_members', 'moderate_members',
                 'mention_everyone', 'view_audit_log', 'mute_members', 'deafen_members',
                 'move_members'}
    return not any(enabled and (name.startswith('manage_') or name in forbidden)
                   for name, enabled in role.permissions)


class Lockdowns:
    """One service per bot; local locks serialize starts/restores across its shards.

    Mongo snapshots are written before Discord changes. A conditional claim also
    prevents another process from starting a second lockdown for the same guild.
    """
    def __init__(self):
        self.locks = defaultdict(asyncio.Lock)

    async def start(self, guild, alert_id):
        async with self.locks[guild.id]:
            cfg = await settings('panic', guild.id)
            if (not cfg['enabled'] or cfg.get('alert_id') != alert_id
                    or time.time() - cfg['last_alert'] > 300):
                raise SafetyError('This raid alert is no longer active. Use the latest alert.')
            if cfg['lockdown']:
                raise SafetyError('A lockdown already exists. Undo it before starting another.')
            if not guild.me or not guild.me.guild_permissions.manage_channels:
                raise SafetyError('I need Manage Channels to start a lockdown.')
            state = {'id': uuid4().hex, 'expires_at': time.time() + 600,
                     'restore_pending': False, 'channels': {}}
            coll = await get_collection('safety_panic')
            claim = await coll.update_one(
                {'_id': guild.id, 'enabled': True, 'alert_id': alert_id, 'lockdown': None},
                {'$set': {'lockdown': state}})
            if not claim.modified_count:
                raise SafetyError('The alert changed or another lockdown is already active.')
            query = {'_id': guild.id, 'lockdown.id': state['id']}
            locked = failed = 0
            # Fetch current permissions, not potentially stale gateway cache entries.
            try:
                channels = await guild.fetch_channels()
                for channel in channels:
                    if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
                        continue
                    if not channel.permissions_for(guild.me).manage_channels:
                        failed += 1
                        continue
                    overwrite = channel.overwrites_for(guild.default_role)
                    snapshot = {key: getattr(overwrite, key) for key in SEND_FIELDS}
                    # Persist even for an ambiguous HTTP failure; expiry will reconcile it.
                    await coll.update_one(query, {'$set': {f'lockdown.channels.{channel.id}': snapshot}})
                    for key in SEND_FIELDS:
                        setattr(overwrite, key, False)
                    try:
                        await channel.set_permissions(guild.default_role, overwrite=overwrite,
                                                      reason='Kuroo Panic Shield: manager-approved lockdown')
                        locked += 1
                    except discord.HTTPException:
                        failed += 1
            except Exception:
                # Keep all recorded snapshots recoverable; don't mask the original error.
                await coll.update_one(query, {'$set': {'lockdown.restore_pending': True}})
                raise
            return (f'Lockdown requested for 10 minutes: {locked} channels locked, {failed} failed. '
                    'Use /panic unlock or Undo lockdown to restore recorded permissions. '
                    'Role/member overrides may still allow messages.')

    async def restore(self, guild):
        async with self.locks[guild.id]:
            cfg = await settings('panic', guild.id)
            state = cfg['lockdown']
            if not state:
                return 'There is no active lockdown.'
            coll = await get_collection('safety_panic')
            query = {'_id': guild.id, 'lockdown.id': state['id']}
            await coll.update_one(query, {'$set': {'lockdown.restore_pending': True}})
            restored = failed = 0
            for raw_id, snapshot in state['channels'].items():
                try:
                    channel = await guild.fetch_channel(int(raw_id))
                    overwrite = channel.overwrites_for(guild.default_role)
                    for key in SEND_FIELDS:
                        # Preserve a manager's later explicit True/None edits.
                        if getattr(overwrite, key) is False:
                            setattr(overwrite, key, snapshot[key])
                    await channel.set_permissions(
                        guild.default_role, overwrite=None if overwrite.is_empty() else overwrite,
                        reason='Kuroo Panic Shield: restore recorded message permissions')
                except discord.NotFound:
                    pass  # Deleted channel requires no restoration.
                except discord.HTTPException:
                    failed += 1
                    continue
                await coll.update_one(query, {'$unset': {f'lockdown.channels.{raw_id}': ''}})
                restored += 1
            if not failed:
                await coll.update_one(query, {'$set': {'lockdown': None}})
            return (f'Restored {restored} channel permission snapshots; {failed} pending. '
                    + ('I will retry pending channels every 15 seconds.' if failed else 'Lockdown ended.'))

    async def due_guilds(self):
        coll = await get_collection('safety_panic')
        cursor = coll.find({'lockdown': {'$ne': None}, '$or': [
            {'lockdown.expires_at': {'$lte': time.time()}}, {'lockdown.restore_pending': True}]})
        return [row['_id'] async for row in cursor]
