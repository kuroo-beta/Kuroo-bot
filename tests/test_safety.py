"""Safety permission boundaries, recovery, live registry and Mongo state."""
import asyncio
from copy import deepcopy
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import discord
from discord.ext import commands
from motor.motor_asyncio import AsyncIOMotorClient

from kuroo.src.commands.safety import Safety, PanicControls, OnboardControls, InterestSelect
from kuroo.workflows import safety
from kuroo.workflows.components import message_payload


def guild_fixture():
    return SimpleNamespace(id=1, default_role=SimpleNamespace(id=1),
                           me=SimpleNamespace(guild_permissions=discord.Permissions(manage_channels=True, manage_roles=True)))


def channel_fixture(channel_id, initial=None):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.state = initial or discord.PermissionOverwrite()
    channel.overwrites_for.side_effect = lambda role: deepcopy(channel.state)
    channel.permissions_for.return_value = discord.Permissions(manage_channels=True, send_messages=True)
    async def set_permissions(role, *, overwrite, reason):
        channel.state = deepcopy(overwrite) if overwrite else discord.PermissionOverwrite()
    channel.set_permissions = AsyncMock(side_effect=set_permissions)
    return channel


def role_fixture(role_id, **permissions):
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = f'Interest {role_id}'
    role.is_default.return_value = False
    role.managed = False
    role.is_assignable.return_value = True
    role.permissions = discord.Permissions(**permissions)
    return role


class SafetyUnitTests(unittest.IsolatedAsyncioTestCase):
    def cog(self):
        cog = Safety(SimpleNamespace())
        self.addCleanup(cog.cog_unload)
        return cog

    async def test_safe_roles_reject_management_moderation_and_hierarchy(self):
        self.assertTrue(safety.safe_role(role_fixture(1, send_messages=True)))
        for permission in ('administrator', 'manage_roles', 'manage_channels', 'ban_members',
                           'moderate_members', 'mention_everyone', 'move_members'):
            self.assertFalse(safety.safe_role(role_fixture(1, **{permission: True})), permission)
        role = role_fixture(1)
        role.is_assignable.return_value = False
        self.assertFalse(safety.safe_role(role))
        role.is_assignable.return_value = True
        role.managed = True
        self.assertFalse(safety.safe_role(role))

    async def test_interest_updates_preserve_unrelated_roles_and_revalidate_permissions(self):
        cog = self.cog()
        guild = guild_fixture()
        roles = [role_fixture(11), role_fixture(12), role_fixture(99)]
        member = SimpleNamespace(id=2, roles=[roles[0], roles[2]], add_roles=AsyncMock(), remove_roles=AsyncMock())
        guild.fetch_member = AsyncMock(return_value=member)
        guild.fetch_roles = AsyncMock(return_value=roles)
        with patch.object(safety, 'settings', new=AsyncMock(return_value={'enabled': True, 'roles': [11, 12]})):
            await cog.assign_interests(guild, 2, {12}, {11, 12})
            member.add_roles.assert_awaited_once_with(roles[1], reason='Member selected onboarding interests')
            member.remove_roles.assert_awaited_once_with(roles[0], reason='Member updated onboarding interests')
            roles[1].permissions = discord.Permissions(administrator=True)
            with self.assertRaises(safety.SafetyError):
                await cog.assign_interests(guild, 2, {12}, {11, 12})
        self.assertEqual(member.add_roles.await_count, 1)

    async def test_disabled_or_stale_selector_cannot_assign(self):
        cog = self.cog()
        guild = guild_fixture()
        guild.fetch_member = AsyncMock()
        for cfg in ({'enabled': False, 'roles': [11]}, {'enabled': True, 'roles': [12]}):
            with patch.object(safety, 'settings', new=AsyncMock(return_value=cfg)):
                with self.assertRaises(safety.SafetyError):
                    await cog.assign_interests(guild, 2, {11}, {11})
        guild.fetch_member.assert_not_awaited()

    async def test_raid_alert_threshold_cooldown_and_no_automatic_lock(self):
        cog = self.cog()
        guild = guild_fixture()
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=88)))
        guild.get_channel = lambda _: channel
        member = SimpleNamespace(guild=guild)
        cfg = {'enabled': True, 'window': 30, 'threshold': 3, 'last_alert': 0, 'alert_channel': 7}
        async def update(kind, guild_id, **values):
            cfg.update(values)
        cog.lockdowns.start = AsyncMock()
        with patch.object(safety, 'settings', new=AsyncMock(side_effect=lambda *args: dict(cfg))), \
             patch.object(safety, 'configure', side_effect=update):
            for _ in range(4):
                await cog.raid_alert(member)
        channel.send.assert_awaited_once()
        cog.lockdowns.start.assert_not_awaited()
        self.assertEqual(cfg['alert_id'], 88)

    async def test_buttons_and_commands_require_manager_permissions(self):
        cog = self.cog()
        interaction = SimpleNamespace(guild=guild_fixture(), channel=SimpleNamespace(id=5),
            user=SimpleNamespace(id=2, guild_permissions=discord.Permissions()),
            response=SimpleNamespace(send_message=AsyncMock()))
        with patch('kuroo.src.commands.safety.eligible', return_value=True):
            self.assertFalse(await cog.panic_view.interaction_check(interaction))
        for command in (Safety.panic_setup, Safety.panic_unlock, Safety.panic_off,
                        Safety.onboard_setup, Safety.onboard_addrole, Safety.onboard_panel):
            predicate = next(c for c in command.checks if 'has_guild_permissions' in c.__qualname__)
            ctx = SimpleNamespace(guild=interaction.guild, author=interaction.user)
            with self.assertRaises(commands.MissingPermissions):
                await discord.utils.maybe_coroutine(predicate, ctx)

    async def test_persistent_controls_serialize_through_v2(self):
        cog = self.cog()
        for view in (cog.panic_view, cog.onboard_view):
            self.assertTrue(view.is_persistent())
            payload = message_payload({'content': 'Test panel', 'components': view.to_components()})
            self.assertTrue(payload['flags'] & (1 << 15))
            self.assertNotIn('content', payload)
            self.assertNotIn('accent_color', str(payload))
        cog.bot.add_view = MagicMock()
        await cog.cog_load()
        self.assertEqual(cog.bot.add_view.call_count, 2)


@unittest.skipUnless(os.getenv('KUROO_TEST_MONGO_URI'), 'Set KUROO_TEST_MONGO_URI for disposable Mongo tests')
class SafetyMongoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncIOMotorClient(os.environ['KUROO_TEST_MONGO_URI'], serverSelectionTimeoutMS=3000)
        await self.client.admin.command('ping')
        self.db = self.client['kuroo_test_' + uuid4().hex]
        async def collection(name):
            return self.db[name]
        self.patch = patch.object(safety, 'get_collection', side_effect=collection)
        self.patch.start()
        self.guild = guild_fixture()
        self.channels = [channel_fixture(10, discord.PermissionOverwrite(send_messages=None, view_channel=False)),
                         channel_fixture(20, discord.PermissionOverwrite(send_messages=True, send_messages_in_threads=False))]
        self.guild.fetch_channels = AsyncMock(return_value=self.channels)
        self.guild.fetch_channel = AsyncMock(side_effect=lambda channel_id: next(c for c in self.channels if c.id == channel_id))
        await safety.configure('panic', 1, enabled=True, alert_id=88, last_alert=time.time())

    async def asyncTearDown(self):
        self.patch.stop()
        await self.client.drop_database(self.db.name)
        self.client.close()

    async def test_restart_restores_exact_fields_and_preserves_other_permissions(self):
        service = safety.Lockdowns()
        await service.start(self.guild, 88)
        self.assertTrue(all(c.state.send_messages is False for c in self.channels))
        self.assertEqual(len((await safety.settings('panic', 1))['lockdown']['channels']), 2)
        # Simulate a different permission changed by a manager during the lockdown.
        self.channels[0].state.attach_files = False
        await safety.Lockdowns().restore(self.guild)
        self.assertIsNone(self.channels[0].state.send_messages)
        self.assertIsNone(self.channels[0].state.send_messages_in_threads)
        self.assertFalse(self.channels[0].state.view_channel)
        self.assertFalse(self.channels[0].state.attach_files)
        self.assertTrue(self.channels[1].state.send_messages)
        self.assertFalse(self.channels[1].state.send_messages_in_threads)
        self.assertIsNone((await safety.settings('panic', 1))['lockdown'])

    async def test_concurrent_starts_claim_once(self):
        results = await asyncio.gather(safety.Lockdowns().start(self.guild, 88),
                                       safety.Lockdowns().start(self.guild, 88), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, safety.SafetyError) for r in results), 1)
        self.assertEqual(self.channels[0].set_permissions.await_count, 1)

    async def test_failed_restoration_is_persisted_and_retried_after_restart(self):
        await safety.Lockdowns().start(self.guild, 88)
        setter = self.channels[0].set_permissions.side_effect
        self.channels[0].set_permissions.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'denied')
        await safety.Lockdowns().restore(self.guild)
        cfg = await safety.settings('panic', 1)
        self.assertEqual(set(cfg['lockdown']['channels']), {'10'})
        self.assertIn(1, await safety.Lockdowns().due_guilds())
        self.channels[0].set_permissions.side_effect = setter
        await safety.Lockdowns().restore(self.guild)
        self.assertIsNone((await safety.settings('panic', 1))['lockdown'])

    async def test_snapshot_exists_before_discord_write_and_ambiguous_failures_are_recoverable(self):
        async def uncertain_write(role, **kwargs):
            cfg = await safety.settings('panic', 1)
            self.assertIn('10', cfg['lockdown']['channels'])
            self.channels[0].state = kwargs['overwrite']
            raise discord.HTTPException(SimpleNamespace(status=500, reason='Error'), 'uncertain')
        self.channels[0].set_permissions.side_effect = uncertain_write
        await safety.Lockdowns().start(self.guild, 88)
        self.assertIn('10', (await safety.settings('panic', 1))['lockdown']['channels'])

    async def test_expiry_and_disabled_alerts_do_not_prevent_restoration(self):
        await safety.Lockdowns().start(self.guild, 88)
        await safety.configure('panic', 1, enabled=False)
        self.assertEqual(await safety.Lockdowns().due_guilds(), [])
        await self.db.safety_panic.update_one({'_id': 1}, {'$set': {'lockdown.expires_at': 0}})
        self.assertEqual(await safety.Lockdowns().due_guilds(), [1])
        await safety.Lockdowns().restore(self.guild)
        self.assertIsNone((await safety.settings('panic', 1))['lockdown'])

    async def test_stale_alert_is_rejected_and_settings_are_isolated(self):
        await safety.configure('panic', 1, last_alert=0)
        with self.assertRaises(safety.SafetyError):
            await safety.Lockdowns().start(self.guild, 88)
        await safety.configure('onboard', 1, enabled=True, roles=[12])
        self.assertEqual((await safety.settings('onboard', 2))['roles'], [])
        self.assertTrue((await safety.settings('panic', 1))['enabled'])

    async def test_database_failure_before_snapshot_prevents_discord_permission_change(self):
        coll = self.db.safety_panic
        original = coll.update_one
        async def fail_snapshot(query, update, **kwargs):
            if any(key.startswith('lockdown.channels.') for key in update.get('$set', {})):
                raise RuntimeError('synthetic persistence failure')
            return await original(query, update, **kwargs)
        proxy = SimpleNamespace(find_one=coll.find_one, update_one=fail_snapshot)
        with patch.object(safety, 'get_collection', new=AsyncMock(return_value=proxy)):
            with self.assertRaises(RuntimeError):
                await safety.Lockdowns().start(self.guild, 88)
        self.channels[0].set_permissions.assert_not_awaited()
        self.assertTrue((await safety.settings('panic', 1))['lockdown']['restore_pending'])

    async def test_expiry_worker_recovers_saved_lockdown_in_new_cog(self):
        await safety.Lockdowns().start(self.guild, 88)
        await self.db.safety_panic.update_one({'_id': 1}, {'$set': {'lockdown.expires_at': 0}})
        cog = Safety(SimpleNamespace(get_guild=lambda _: self.guild))
        try:
            await cog.expire_lockdowns()
        finally:
            cog.cog_unload()
        self.assertIsNone((await safety.settings('panic', 1))['lockdown'])
