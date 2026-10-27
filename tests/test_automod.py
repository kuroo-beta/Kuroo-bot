"""Discord REST serialization, original opt-out contracts, local enforcement and Mongo recovery."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
import subprocess
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import discord
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import AutoReconnect

from storage import automod as store
from storage.engine import CollectionStore
from kuroo.memory.cache import cache
from kuroo.src.commands.automod import Automod
from kuroo.src.events.message import message as MessageCog
from kuroo.workflows import automod as mod
from kuroo.workflows.components import message_payload


def http_error(status=403):
    return discord.HTTPException(SimpleNamespace(status=status, reason='Synthetic'), {'code': 50013, 'message': 'Synthetic denied'})


class DiscordHTTP:
    """Synthetic wire boundary; Guild and AutoModRule do their real serialization."""
    def __init__(self):
        self.records = {}
        self.created = []
        self.edited = []
        self.failure = None
        self.next_id = 100

    async def get_auto_moderation_rules(self, guild_id):
        return deepcopy(list(self.records.values()))

    async def create_auto_moderation_rule(self, guild_id, **payload):
        if self.failure:
            raise self.failure
        self.created.append(deepcopy(payload))
        trigger = payload['trigger_type']
        self._validate(payload, trigger)
        self.next_id += 1
        data = {**deepcopy(payload), 'id': str(self.next_id), 'guild_id': str(guild_id), 'creator_id': '42'}
        data.pop('reason', None)
        data['exempt_roles'] = data.get('exempt_roles') or []
        data['exempt_channels'] = data.get('exempt_channels') or []
        self.records[self.next_id] = data
        return deepcopy(data)

    async def edit_auto_moderation_rule(self, guild_id, rule_id, **payload):
        if self.failure:
            raise self.failure
        self.edited.append(deepcopy(payload))
        data = {**self.records[rule_id], **deepcopy(payload)}
        data.pop('reason', None)
        self._validate(data, data['trigger_type'])
        self.records[rule_id] = data
        return deepcopy(data)

    def _validate(self, payload, trigger):
        # Platform constraints independently checked at the serialized boundary.
        if 'event_type' in payload:
            assert payload['event_type'] == (2 if trigger == 6 else 1)
        for action in payload.get('actions', []):
            if action['type'] == 3:
                assert trigger in (1, 5)
                assert 0 < action['metadata']['duration_seconds'] <= 2419200
            if action['type'] == 4:
                assert trigger == 6
            if trigger == 6:
                assert action['type'] in (2, 4)
        metadata = payload.get('trigger_metadata') or {}
        assert len(metadata.get('regex_patterns', [])) <= 10
        assert all(len(regex) <= 260 for regex in metadata.get('regex_patterns', []))
        assert len(payload.get('exempt_roles') or []) <= 20
        assert len(payload.get('exempt_channels') or []) <= 50


class GuildFixture:
    def __init__(self, guild_id=1):
        self.id = guild_id
        self.owner_id = 99
        self.http = DiscordHTTP()
        self._state = SimpleNamespace(http=self.http)
        self.me = SimpleNamespace(guild_permissions=discord.Permissions(manage_guild=True, moderate_members=True))
        self.create_automod_rule = MethodType(discord.Guild.create_automod_rule, self)
        self.fetch_automod_rules = MethodType(discord.Guild.fetch_automod_rules, self)
        self.get_channel = lambda sid: SimpleNamespace(
            id=sid, permissions_for=lambda _: discord.Permissions(send_messages=True, view_channel=True))


class MemoryStore:
    def __init__(self, record=None):
        self.record = deepcopy(record)
        self.fail_after = None
        self.updates = 0

    async def insert(self, **values):
        if self.record is None:
            self.record = {**deepcopy(store._store.defaults), **deepcopy(values), 'id': 1}
        return deepcopy(self.record)

    async def update(self, **values):
        self.updates += 1
        if self.fail_after and self.updates >= self.fail_after:
            raise AutoReconnect('Synthetic database failure')
        self.record.update(deepcopy(values))
        return deepcopy(self.record)


class AutoModNativeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.guild = GuildFixture()
        self.bot = SimpleNamespace(user=SimpleNamespace(id=42))
        self.service = mod.AutoModService(self.bot)
        self.db = MemoryStore()
        self.insert_patch = patch.object(store, 'insert', side_effect=self.db.insert)
        self.update_patch = patch.object(store, 'update', side_effect=self.db.update)
        self.insert_patch.start()
        self.update_patch.start()
        self.addCleanup(self.insert_patch.stop)
        self.addCleanup(self.update_patch.stop)

    async def test_exact_ten_real_serialized_rules_and_idempotent_reconnect(self):
        result = await self.service.provision(self.guild)
        self.assertEqual(list(result.values()), ['enabled'] * 10)
        self.assertEqual(len(self.guild.http.created), 10)
        counts = {trigger: sum(p['trigger_type'] == trigger for p in self.guild.http.created) for trigger in (1, 3, 4, 5, 6)}
        self.assertEqual(counts, {1: 6, 3: 1, 4: 1, 5: 1, 6: 1})
        profile = next(p for p in self.guild.http.created if p['trigger_type'] == 6)
        self.assertEqual(profile['actions'], [{'type': 4, 'metadata': {}}])
        for _ in range(2):
            await self.service.provision(self.guild)
            await self.service.sync(self.guild)
        self.assertEqual(len(self.guild.http.created), 10)
        self.assertEqual(self.guild.http.edited, [])

    async def test_native_disable_survives_status_restart_and_offline_deletion(self):
        await self.service.provision(self.guild)
        sid = self.db.record['native_rule_ids']['spam']
        self.guild.http.records[sid]['enabled'] = False
        await mod.AutoModService(self.bot).provision(self.guild)
        self.assertFalse(self.db.record['native_config']['rules']['spam']['enabled'])
        sid = self.db.record['native_rule_ids']['caps']
        del self.guild.http.records[sid]
        await mod.AutoModService(self.bot).provision(self.guild)
        self.assertFalse(self.db.record['native_config']['rules']['caps']['enabled'])
        self.assertEqual(len(self.guild.http.created), 10)
        self.assertEqual(self.guild.http.edited, [])

    async def test_saved_disable_retried_after_permission_failure_and_restart(self):
        await self.service.provision(self.guild)
        self.guild.http.failure = http_error()
        result = await self.service.configure(self.guild, lambda cfg: [r.update(enabled=False) for r in cfg['rules'].values()])
        self.assertTrue(all('blocked' in value for value in result.values()))
        self.assertTrue(all(not rule['enabled'] for rule in self.db.record['native_config']['rules'].values()))
        self.assertEqual(len(self.db.record['native_pending']), 10)
        self.guild.http.failure = None
        self.guild.me.guild_permissions.moderate_members = False
        result = await mod.AutoModService(self.bot).provision(self.guild)
        self.assertEqual(list(result.values()), ['disabled'] * 10)
        self.assertFalse(any(rule['enabled'] for rule in self.guild.http.records.values()))
        self.assertEqual(self.db.record['native_pending'], [])

    async def test_no_remote_changes_before_configuration_is_durable(self):
        self.db.fail_after = 1
        with self.assertRaises(AutoReconnect):
            await self.service.configure(self.guild, lambda cfg: cfg['limits'].update(mentions=7))
        self.assertEqual(self.guild.http.created, [])

    async def test_database_failure_after_creation_recovers_without_duplicate_rules(self):
        self.db.fail_after = 2
        with self.assertRaises(AutoReconnect):
            await self.service.sync(self.guild)
        self.assertEqual(len(self.guild.http.created), 10)
        self.db.fail_after = None
        await mod.AutoModService(self.bot).provision(self.guild)
        self.assertEqual(len(self.guild.http.created), 10)
        self.assertEqual(len(self.db.record['native_rule_ids']), 10)

    async def test_foreign_rules_and_trigger_limits_are_preserved(self):
        await self.service.provision(self.guild)
        for rule in self.guild.http.records.values():
            rule['creator_id'] = '999'
        self.guild.http.created.clear()
        result = await self.service.sync(self.guild)
        self.assertTrue(all('conflict' in value for value in result.values()))
        self.assertEqual(len(self.guild.http.records), 10)
        self.assertEqual(self.guild.http.created, [])
        self.assertEqual(self.guild.http.edited, [])

    async def test_legacy_two_native_rules_are_adopted_without_extra_slots(self):
        await self.service.provision(self.guild)
        ids = self.db.record['native_rule_ids']
        for key, old_name in (('links', 'AntiLink by Kuroo'), ('words', 'AntiBadWords by Kuroo')):
            self.guild.http.records[ids[key]]['name'] = old_name
            self.db.record['antilink_rule_id' if key == 'links' else 'antibadwords_rule_id'] = ids[key]
        self.db.record['native_rule_ids'] = {}
        await self.service.sync(self.guild)
        self.assertEqual(len(self.guild.http.created), 10)
        self.assertEqual(len(self.guild.http.edited), 2)

    async def test_missing_permissions_and_alert_channel_fail_explicitly(self):
        self.guild.me.guild_permissions.manage_guild = False
        result = await self.service.provision(self.guild)
        self.assertTrue(all('Manage Server' in value for value in result.values()))
        self.assertEqual(self.db.record['native_status'], result)
        self.guild.me.guild_permissions.manage_guild = True
        self.guild.me.guild_permissions.moderate_members = False
        result = await self.service.sync(self.guild)
        self.assertIn('Moderate Members', result['links'])
        self.assertEqual(result['profiles'], 'enabled')
        self.guild.get_channel = lambda sid: None
        result = await self.service.configure(self.guild, lambda cfg: cfg.update(alert_channel=999))
        self.assertTrue(all('alert channel' in value or 'Moderate Members' in value for value in result.values()))

    async def test_empty_custom_words_disable_existing_filter_without_fake_keywords(self):
        await self.service.provision(self.guild)
        result = await self.service.configure(self.guild, lambda cfg: cfg['rules']['words'].update(words=[]))
        self.assertIn('needs configuration', result['words'])
        self.assertFalse(self.guild.http.records[self.db.record['native_rule_ids']['words']]['enabled'])
        self.assertEqual(len(self.guild.http.created), 10)

    async def test_exemptions_allowlist_mentions_and_native_timeout_minutes(self):
        def change(cfg):
            cfg['rules']['links'].update(roles=[11], channels=[12], allow=['*example.com*'])
            cfg['limits'].update(mentions=50, timeout=40320)
            cfg['alert_channel'] = 77
        await self.service.configure(self.guild, change)
        payload = next(p for p in self.guild.http.created if p['name'].endswith('links'))
        self.assertEqual(payload['exempt_roles'], ['11'])
        self.assertEqual(payload['exempt_channels'], ['12'])
        self.assertEqual(payload['trigger_metadata']['allow_list'], ['*example.com*'])
        self.assertEqual(payload['actions'][2]['metadata']['duration_seconds'], 2419200)
        for key in ('messages', 'interval', 'mentions', 'emoji', 'caps', 'timeout'):
            with self.assertRaises(mod.ConfigError):
                await self.service.configure(self.guild, lambda cfg, key=key: cfg['limits'].update({key: 0}))

    async def test_out_of_order_toggle_events_read_current_discord_state(self):
        await self.service.provision(self.guild)
        rules = await self.guild.fetch_automod_rules()
        stale = rules[0]
        self.guild.http.records[stale.id]['enabled'] = False
        await self.service.observe(stale)
        self.assertFalse(self.db.record['native_config']['rules']['links']['enabled'])
        self.assertTrue(stale.enabled)

    async def test_concurrent_commands_do_not_create_duplicates_or_lose_updates(self):
        await asyncio.gather(
            self.service.configure(self.guild, lambda cfg: cfg['limits'].update(mentions=12)),
            self.service.configure(self.guild, lambda cfg: cfg['limits'].update(emoji=15)))
        self.assertEqual(len(self.guild.http.created), 10)
        self.assertEqual(self.db.record['native_config']['limits']['mentions'], 12)
        self.assertEqual(self.db.record['native_config']['limits']['emoji'], 15)

    async def test_reset_restores_defaults_and_reuses_existing_native_rules(self):
        await self.service.provision(self.guild)
        await self.service.configure(self.guild, lambda cfg: [rule.update(enabled=False) for rule in cfg['rules'].values()])
        original_ids = deepcopy(self.db.record['native_rule_ids'])
        result = await self.service.reset(self.guild)
        self.assertEqual(list(result.values()), ['enabled'] * 10)
        self.assertEqual(self.db.record['native_config'], mod.default_config())
        self.assertEqual(self.db.record['native_rule_ids'], original_ids)
        self.assertEqual(len(self.guild.http.created), 10)

    async def test_status_reports_pending_disable_without_retry_or_overwriting_it(self):
        await self.service.provision(self.guild)
        self.guild.http.failure = http_error()
        await self.service.configure(self.guild, lambda cfg: cfg['rules']['spam'].update(enabled=False))
        self.guild.http.failure = None
        result = await self.service.sync(self.guild, apply=False)
        self.assertIn('pending', result['spam'])
        self.assertFalse(self.db.record['native_config']['rules']['spam']['enabled'])
        self.assertTrue(self.guild.http.records[self.db.record['native_rule_ids']['spam']]['enabled'])
        self.assertEqual(self.guild.http.edited, [])


class AutoModContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_defaults_and_explicit_false_and_existing_opt_out(self):
        for override in (None, False):
            collection = SimpleNamespace(insert_one=AsyncMock())
            with patch.object(CollectionStore, '_find_existing_unique', new=AsyncMock(return_value=None)), \
                 patch.object(CollectionStore, '_next_sequence', new=AsyncMock(return_value=1)), \
                 patch.object(CollectionStore, '_collection', new=AsyncMock(return_value=collection)), \
                 patch.object(CollectionStore, 'get', new=AsyncMock()), \
                 patch.object(CollectionStore, '_trigger_update_hook', new=AsyncMock()):
                await store.insert(guild_id=42, antispam_enabled=override)
            document = collection.insert_one.call_args.args[0]
            self.assertEqual(document['antispam_enabled'], override is None)
            self.assertEqual(document['native_config']['rules']['spam']['enabled'], override is None)
        saved = {'id': 1, 'guild_id': 42, 'antispam_enabled': False}
        with patch.object(CollectionStore, '_find_existing_unique', new=AsyncMock(return_value=saved)), \
             patch.object(CollectionStore, '_trigger_update_hook', new=AsyncMock()), \
             patch.object(CollectionStore, '_collection', new=AsyncMock()) as collection:
            self.assertEqual(await store.insert(guild_id=42), saved)
            collection.assert_not_awaited()
        cfg = mod.config_from_record(saved)
        self.assertFalse(any(rule['enabled'] for rule in cfg['rules'].values()))

    async def test_old_word_substring_semantics_exemptions_and_duration_survive_migration(self):
        cfg = mod.config_from_record({'antispam_enabled': True, 'antibadwords_enabled': True,
                                      'antibadwords_words': ['foobar'], 'antibadwords_whitelist_roles': [9],
                                      'antispam_punishment_duration': 12, 'antispam_punishment': 'tempban'})
        self.assertEqual(cfg['rules']['words']['words'], ['*foobar*'])
        self.assertEqual(cfg['rules']['words']['roles'], [9])
        self.assertEqual(cfg['limits']['timeout'], 12)
        self.assertEqual(cfg['punishment'], 'tempban')

    async def test_message_pipeline_stops_after_successful_automod_enforcement(self):
        service = SimpleNamespace(process_message=AsyncMock(return_value=True))
        cog = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda name: SimpleNamespace(service=service)))
        self.assertTrue(await MessageCog.check_automod(cog, 'synthetic message'))
        service.process_message.assert_awaited_once_with('synthetic message')

    async def test_administrator_permissions_dm_ignore_and_blacklist_are_enforced(self):
        cog = Automod(SimpleNamespace(emoji=SimpleNamespace(AUTOMOD='')))
        ctx = SimpleNamespace(guild=SimpleNamespace(id=1))
        with patch('kuroo.src.commands.automod.checks.check_is_moderator_permissions', new=AsyncMock(return_value=False)) as allowed, \
             patch('kuroo.src.commands.automod.checks.check_ignore_predicate', return_value=True), \
             patch('kuroo.src.commands.automod.checks.check_blacklist_predicate', return_value=True):
            self.assertFalse(await cog.cog_check(ctx))
            allowed.assert_awaited_once_with(ctx, 'administrator')
        from discord.ext import commands
        with self.assertRaises(commands.NoPrivateMessage):
            await cog.cog_check(SimpleNamespace(guild=None))

    async def test_native_regexes_compile_with_rust_and_match_representative_content(self):
        samples = {'links': 'visit https://example.com/path', 'invites': 'discord.gg/test',
                   'caps': 'THIS IS A CAPITAL MESSAGE', 'emoji': '😀' * 10}
        for key, sample in samples.items():
            regex = mod.rule_payload(key, mod.default_config())['trigger'].regex_patterns[0]
            result = subprocess.run(['rg', '--quiet', '--', regex], input=sample, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, (key, result.stderr))
        self.assertEqual(mod.emoji_count('👨‍👩‍👧‍👦 🇯🇵 👍🏽 1️⃣ <:happy:123456789>'), 5)

    async def test_status_text_serializes_with_shared_components_transport(self):
        payload = message_payload({'content': '\n'.join(f'{key}: enabled' for key in mod.RULES)})
        self.assertTrue(payload['flags'] & (1 << 15))
        self.assertIn('profiles', str(payload))
        self.assertNotIn('content', payload)


class AutoModLocalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = mod.default_config()
        self.config['limits'].update(messages=4, emoji=4, mentions=3)
        self.service = mod.AutoModService(SimpleNamespace(user=SimpleNamespace(id=42)))
        self.owner_patch = patch.object(mod.checks, 'check_is_owner_raw', new=AsyncMock(return_value=False))
        self.owner_patch.start()
        self.addCleanup(self.owner_patch.stop)
        self.cache_patch = patch.object(cache, 'automod', {})
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.sequence = 0

    def message(self, text='hello', guild_id=1, member_id=2, **values):
        self.sequence += 1
        member = MagicMock(spec=discord.Member)
        member.id, member.bot, member.roles = member_id, False, []
        member.guild_permissions = discord.Permissions.none()
        member.top_role = 1
        member.mention = f'<@{member_id}>'
        member.timeout, member.kick, member.ban = AsyncMock(), AsyncMock(), AsyncMock()
        channel = SimpleNamespace(id=3, parent_id=None, send=AsyncMock())
        guild = SimpleNamespace(id=guild_id, owner_id=99, me=SimpleNamespace(top_role=10, guild_permissions=discord.Permissions(moderate_members=True, kick_members=True, ban_members=True)), get_channel=lambda sid: None)
        message = SimpleNamespace(id=self.sequence, content=text, author=member, guild=guild, channel=channel,
                                  delete=AsyncMock(), **values)
        cache.automod[str(guild_id)] = {'id': guild_id, 'guild_id': guild_id, 'native_config': deepcopy(self.config)}
        return message

    async def test_guild_member_scoped_sliding_window_and_duplicate_threshold(self):
        for guild_id in (1, 2):
            for index in range(2):
                self.assertFalse(await self.service.process_message(self.message('duplicate', guild_id)))
        current = self.message('duplicate', 1)
        self.assertTrue(await self.service.process_message(current))
        current.delete.assert_awaited_once()
        current.author.timeout.assert_awaited_once_with(timedelta(minutes=10), reason='AutoMod: duplicate messages')
        self.assertFalse(await self.service.process_message(self.message('another', 2)))
        self.assertFalse(await self.service.process_message(self.message('duplicate', 1, member_id=3)))

    async def test_flood_window_expires_and_does_not_purge_channel_history(self):
        with patch.object(mod.time, 'monotonic', side_effect=[100, 101, 102, 140, 141, 142, 143]):
            for text in ('one', 'two', 'three', 'four', 'five', 'six'):
                self.assertFalse(await self.service.process_message(self.message(text)))
            current = self.message('seven')
            self.assertTrue(await self.service.process_message(current))
        current.delete.assert_awaited_once_with()
        self.assertFalse(hasattr(current.channel, 'purge'))

    async def test_native_off_optout_and_parent_channel_or_role_exemptions(self):
        for field, value in (('enabled', False), ('roles', [7]), ('channels', [8]), ('allow', ['*HELLO*'])):
            self.service.local.clear()
            self.config['rules']['caps'].update({field: value})
            current = self.message('HELLO WORLD')
            current.author.roles = [SimpleNamespace(id=7)]
            current.channel.parent_id = 8
            self.assertFalse(await self.service.process_message(current))
            self.config = mod.default_config()

    async def test_edits_check_content_without_counting_flood_and_emoji_clusters(self):
        for _ in range(5):
            self.assertFalse(await self.service.process_message(self.message('ordinary edit'), edited=True))
        family = self.message('👨‍👩‍👧‍👦')
        self.assertFalse(await self.service.process_message(family, edited=True))
        edited = self.message('😀😀😀😀')
        self.assertTrue(await self.service.process_message(edited, edited=True))
        edited.delete.assert_awaited_once()

    async def test_caps_use_letters_mentions_are_unique_and_counters_are_bounded(self):
        self.assertTrue(await self.service.process_message(self.message('HELLO WORLD' + ' ' * 100), edited=True))
        self.assertFalse(await self.service.process_message(self.message('<@2> <@!2> <@2> <@2>'), edited=True))
        self.assertTrue(await self.service.process_message(self.message('<@2> <@3> <@4> <@&5>'), edited=True))
        self.service.local_limit = 3
        for member_id in range(10, 20):
            await self.service.process_message(self.message('normal', member_id=member_id))
        self.assertEqual(len(self.service.local), 3)
        for state in self.service.local.values():
            self.assertEqual(state['messages'].maxlen, 30)
            self.assertEqual(state['seen'].maxlen, 60)

    async def test_cooldown_delete_failure_and_hierarchy_never_claim_punishment_success(self):
        with patch.object(mod.time, 'monotonic', return_value=100):
            first = self.message('HELLO WORLD')
            self.assertTrue(await self.service.process_message(first, edited=True))
            second = self.message('CAPITAL LETTERS')
            self.assertTrue(await self.service.process_message(second, edited=True))
            first.author.timeout.assert_awaited_once()
            second.author.timeout.assert_not_awaited()
        denied = self.message('HELLO WORLD', member_id=3)
        denied.delete.side_effect = http_error()
        self.assertFalse(await self.service.process_message(denied, edited=True))
        denied.author.timeout.assert_not_awaited()
        high = self.message('HELLO WORLD', member_id=4)
        high.author.top_role = 20
        self.assertTrue(await self.service.process_message(high, edited=True))
        high.author.timeout.assert_not_awaited()


@unittest.skipUnless(os.getenv('KUROO_TEST_MONGO_URI'), 'Set KUROO_TEST_MONGO_URI for disposable Mongo tests')
class AutoModMongoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncIOMotorClient(os.environ['KUROO_TEST_MONGO_URI'], serverSelectionTimeoutMS=3000)
        await self.client.admin.command('ping')
        self.db = self.client['kuroo_test_' + uuid4().hex]
        async def collection(name):
            return self.db[name]
        self.storage_patch = patch('storage.engine.get_collection', side_effect=collection)
        self.storage_patch.start()
        self.jobs_patch = patch.object(mod, 'get_collection', side_effect=collection)
        self.jobs_patch.start()
        self.cache_patch = patch.object(cache, 'automod', {})
        self.cache_patch.start()
        self.guild, self.bot = GuildFixture(), SimpleNamespace(user=SimpleNamespace(id=42))
        await store.create_table()

    async def asyncTearDown(self):
        self.storage_patch.stop()
        self.jobs_patch.stop()
        self.cache_patch.stop()
        await self.client.drop_database(self.db.name)
        self.client.close()

    async def test_fresh_ten_rules_and_disable_survive_real_database_restart(self):
        await mod.AutoModService(self.bot).provision(self.guild)
        record = await store.get(guild_id=1)
        self.assertEqual(len(record['native_rule_ids']), 10)
        self.assertEqual(len(record['native_config']['rules']), 10)
        await mod.AutoModService(self.bot).configure(self.guild, lambda cfg: [rule.update(enabled=False) for rule in cfg['rules'].values()])
        cache.automod.clear()
        result = await mod.AutoModService(self.bot).provision(self.guild)
        self.assertEqual(list(result.values()), ['disabled'] * 10)
        self.assertEqual(len(self.guild.http.created), 10)
        self.assertFalse((await store.get(guild_id=1))['antispam_enabled'])

    async def test_old_explicit_off_record_is_migrated_without_enabling_any_rule(self):
        await self.db.automod.insert_one({'id': 1, 'guild_id': 1, 'antispam_enabled': False,
                                        'antilink_enabled': False, 'antibadwords_enabled': False})
        result = await mod.AutoModService(self.bot).provision(self.guild)
        self.assertTrue(all(value == 'disabled (not installed)' for value in result.values()))
        self.assertEqual(self.guild.http.created, [])
        record = await store.get(guild_id=1)
        self.assertFalse(any(rule['enabled'] for rule in record['native_config']['rules'].values()))

    async def test_failed_native_toggle_intent_survives_restart_in_real_database(self):
        service = mod.AutoModService(self.bot)
        await service.provision(self.guild)
        self.guild.http.failure = http_error()
        await service.configure(self.guild, lambda cfg: cfg['rules']['spam'].update(enabled=False))
        self.assertIn('spam', (await store.get(guild_id=1))['native_pending'])
        self.guild.http.failure = None
        cache.automod.clear()
        await mod.AutoModService(self.bot).provision(self.guild)
        self.assertFalse((await store.get(guild_id=1))['native_config']['rules']['spam']['enabled'])
        self.assertEqual(len(self.guild.http.created), 10)

    async def test_temporary_ban_expiry_survives_service_restart(self):
        self.guild.me.guild_permissions.ban_members = True
        self.bot.get_guild = lambda sid: self.guild if sid == self.guild.id else None
        bans = {}
        member = SimpleNamespace(id=7)
        async def ban(**kwargs):
            self.assertIsNotNone(await self.db.automod_tempbans.find_one({'member_id': 7}))
            bans[7] = kwargs['reason']
        member.ban = AsyncMock(side_effect=ban)
        self.guild.fetch_ban = AsyncMock(side_effect=lambda user: SimpleNamespace(reason=bans[user.id]))
        self.guild.unban = AsyncMock()
        service = mod.AutoModService(self.bot)
        before = datetime.now(timezone.utc)
        await service.temporary_ban(self.guild, member, 12)
        job = await self.db.automod_tempbans.find_one({'member_id': 7})
        self.assertGreaterEqual(job['expires_at'].replace(tzinfo=timezone.utc), before + timedelta(minutes=12, milliseconds=-1))
        self.assertLessEqual(job['expires_at'].replace(tzinfo=timezone.utc), datetime.now(timezone.utc) + timedelta(minutes=12))
        member.ban.assert_awaited_once_with(reason=job['reason'], delete_message_seconds=0)
        await service.expire_temporary_bans()
        self.guild.unban.assert_not_awaited()
        await self.db.automod_tempbans.update_one({'_id': job['_id']}, {'$set': {'expires_at': before - timedelta(seconds=1)}})
        await mod.AutoModService(self.bot).expire_temporary_bans()
        self.guild.unban.assert_awaited_once()
        self.assertEqual(await self.db.automod_tempbans.count_documents({}), 0)

    async def test_temporary_unban_retries_failure_and_preserves_later_manual_ban(self):
        self.guild.me.guild_permissions.ban_members = True
        self.bot.get_guild = lambda sid: self.guild
        member = SimpleNamespace(id=8, ban=AsyncMock())
        service = mod.AutoModService(self.bot)
        await service.temporary_ban(self.guild, member, 1)
        job = await self.db.automod_tempbans.find_one({'member_id': 8})
        await self.db.automod_tempbans.update_one({'_id': job['_id']}, {'$set': {'expires_at': datetime.now(timezone.utc) - timedelta(seconds=1)}})
        self.guild.fetch_ban = AsyncMock(return_value=SimpleNamespace(reason=job['reason']))
        self.guild.unban = AsyncMock(side_effect=http_error())
        await service.expire_temporary_bans()
        self.assertEqual(await self.db.automod_tempbans.count_documents({}), 1)
        self.guild.fetch_ban.return_value = SimpleNamespace(reason='Later manual permanent ban')
        self.guild.unban.reset_mock()
        await mod.AutoModService(self.bot).expire_temporary_bans()
        self.guild.unban.assert_not_awaited()
        self.assertEqual(await self.db.automod_tempbans.count_documents({}), 0)

    async def test_database_failure_prevents_ban_and_http_failure_keeps_expiry(self):
        member = SimpleNamespace(id=9, ban=AsyncMock(side_effect=http_error(500)))
        service = mod.AutoModService(self.bot)
        with patch.object(mod, 'get_collection', new=AsyncMock(side_effect=AutoReconnect('synthetic'))):
            with self.assertRaises(AutoReconnect):
                await service.temporary_ban(self.guild, member, 1)
        member.ban.assert_not_awaited()
        with self.assertRaises(discord.HTTPException):
            await service.temporary_ban(self.guild, member, 1)
        self.assertIsNotNone(await self.db.automod_tempbans.find_one({'member_id': 9}))


class AutoModDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from fastapi import FastAPI
        from kuroo.surface.routes import dashboard
        self.dashboard = dashboard
        self.app = FastAPI()
        self.app.include_router(dashboard.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://synthetic.test')
        self.guild = GuildFixture()
        self.service = mod.AutoModService(SimpleNamespace(user=SimpleNamespace(id=42)))
        self.db = MemoryStore()
        self.insert_patch = patch.object(store, 'insert', side_effect=self.db.insert)
        self.update_patch = patch.object(store, 'update', side_effect=self.db.update)
        self.insert_patch.start()
        self.update_patch.start()
        self.addCleanup(self.insert_patch.stop)
        self.addCleanup(self.update_patch.stop)
        self.bot = SimpleNamespace(get_guild=lambda sid: self.guild if sid == 1 else None,
                                   get_cog=lambda name: SimpleNamespace(service=self.service))

    async def asyncTearDown(self):
        await self.client.aclose()

    def form(self):
        data = {'native_form': '1', 'punishment': 'mute', 'alert_channel': ''}
        data.update({'rule_' + key: 'on' for key in mod.RULES})
        data.update({'limit_' + key: str(spec[2]) for key, spec in mod.LIMITS.items()})
        cfg = mod.default_config()
        data.update({'words_' + key: ', '.join(cfg['rules'][key]['words']) for key in mod.WORD_RULES})
        return data

    async def test_unauthenticated_post_redirects_without_saving(self):
        response = await self.client.post('/dashboard/guild/1/moderation', data=self.form())
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/dashboard')
        self.assertIsNone(self.db.record)

    async def test_validated_dashboard_save_uses_native_service_and_reports_partial_failure(self):
        context = ({'user': {'username': 'Synthetic'}}, [], {'id': '1'}, {})
        with patch.object(self.dashboard, '_require_dashboard_context', new=AsyncMock(return_value=context)), \
             patch.object(self.dashboard, 'get_bot', return_value=self.bot):
            data = self.form()
            data['limit_mentions'] = 'not-an-integer'
            response = await self.client.post('/dashboard/guild/1/moderation', data=data)
            self.assertIn('whole%20number', response.headers['location'])
            self.assertEqual(self.guild.http.created, [])
            data = self.form()
            response = await self.client.post('/dashboard/guild/1/moderation', data=data)
            self.assertIn('saved%20and%20synced', response.headers['location'])
            self.assertEqual(len(self.guild.http.created), 10)
            self.guild.http.failure = http_error()
            data.pop('rule_spam')
            response = await self.client.post('/dashboard/guild/1/moderation', data=data)
            self.assertIn('need%20attention', response.headers['location'])
            self.assertFalse(self.db.record['native_config']['rules']['spam']['enabled'])

    async def test_dashboard_renders_ten_toggles_escapes_word_lists_and_uses_ten_rule_coverage(self):
        from html.parser import HTMLParser
        class Inputs(HTMLParser):
            names = []
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'input' and attrs.get('type') == 'checkbox':
                    self.names.append(attrs['name'])
        cfg = mod.default_config()
        cfg['rules']['words']['words'] = ['</textarea><script>alert(1)</script>']
        record = {'native_config': cfg, 'native_status': {'spam': 'enabled', 'caps': 'blocked'}}
        session = {'user': {'username': 'Synthetic'}}
        guild = {'id': '1', 'name': 'Synthetic server'}
        text = self.dashboard._render_moderation(session, [guild], guild, {'automod': record})
        parser = Inputs()
        parser.feed(text)
        self.assertEqual(set(parser.names), {'rule_' + key for key in mod.RULES})
        self.assertNotIn('</textarea><script>alert(1)</script>', text)
        self.assertIn('&lt;script&gt;', text)
        state = {'automod': record, 'antinuke': {}, 'ticket_modules': [], 'welcomer': {}, 'command_access': {}}
        self.assertEqual(self.dashboard._overview_metrics(guild, state, self.guild)['moderation'], 1)
