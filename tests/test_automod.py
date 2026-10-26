"""Default protection and persistent administrator opt-out regression tests."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from storage import automod
from storage.engine import CollectionStore
from kuroo.memory.cache import cache
from kuroo.src.commands.automod import Automod
from kuroo.src.events.message import message as MessageCog


class AutomodTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_settings_enable_spam_and_explicit_false_is_preserved(self):
        for override in (None, False):
            collection = SimpleNamespace(insert_one=AsyncMock())
            with patch.object(CollectionStore, '_find_existing_unique', new=AsyncMock(return_value=None)), \
                 patch.object(CollectionStore, '_next_sequence', new=AsyncMock(return_value=1)), \
                 patch.object(CollectionStore, '_collection', new=AsyncMock(return_value=collection)), \
                 patch.object(CollectionStore, 'get', new=AsyncMock()), \
                 patch.object(CollectionStore, '_trigger_update_hook', new=AsyncMock()):
                await automod.insert(guild_id=42, antispam_enabled=override)
            document = collection.insert_one.call_args.args[0]
            self.assertEqual(document['antispam_enabled'], override is None)
            self.assertFalse(document['antilink_enabled'])
            self.assertFalse(document['antibadwords_enabled'])

    async def test_existing_disabled_settings_survive_initialization(self):
        saved = {'id': 1, 'guild_id': 42, 'antispam_enabled': False}
        with patch.object(CollectionStore, '_find_existing_unique', new=AsyncMock(return_value=saved)), \
             patch.object(CollectionStore, '_trigger_update_hook', new=AsyncMock()), \
             patch.object(CollectionStore, '_collection', new=AsyncMock()) as collection:
            self.assertEqual(await automod.insert(guild_id=42), saved)
            collection.assert_not_awaited()

    async def test_first_message_initializes_protection_and_respects_saved_opt_out(self):
        for enabled in (True, False):
            settings = {'id': 1, 'guild_id': 42, 'antispam_enabled': enabled}
            author = SimpleNamespace(bot=False, roles=[], guild_permissions=SimpleNamespace(
                administrator=False, manage_messages=False, manage_guild=False,
                manage_roles=False, manage_channels=False))
            msg = SimpleNamespace(author=author, guild=SimpleNamespace(id=42), channel=SimpleNamespace(id=2))
            cog = SimpleNamespace(bot=SimpleNamespace(user=object()))
            for name in ('antiduplicate_module', 'antispam_module', 'anti_mass_mentions', 'anti_mass_emojis', 'anti_mass_caps'):
                setattr(cog, name, AsyncMock(return_value=False))
            with patch.object(cache, 'automod', {}), \
                 patch('storage.automod.insert', new=AsyncMock(return_value=settings)) as insert, \
                 patch('kuroo.src.events.message.checks.check_is_owner_raw', new=AsyncMock(return_value=False)):
                await MessageCog.check_automod(cog, msg)
            insert.assert_awaited_once_with(guild_id=42)
            self.assertEqual(cog.antispam_module.await_count, int(enabled))

    async def test_disable_works_with_only_default_spam_filter_enabled(self):
        settings = {'id': 1, 'guild_id': 42, 'antispam_enabled': True}
        ctx = SimpleNamespace(guild=SimpleNamespace(id=42), send=AsyncMock())
        cog = SimpleNamespace(bot=SimpleNamespace(emoji=SimpleNamespace(LOADING='', AUTOMOD='')))
        with patch.object(cache, 'automod', {'42': settings}), \
             patch('kuroo.src.commands.automod.checks.check_is_moderator_permissions', new=AsyncMock(return_value=True)), \
             patch('storage.automod.update', new=AsyncMock()) as update:
            await Automod.automod_command_disable.callback(cog, ctx)
        update.assert_awaited_once_with(id=1, guild_id=42, antilink_enabled=False,
                                       antispam_enabled=False, antibadwords_enabled=False)

    async def test_non_admin_cannot_disable(self):
        with patch('kuroo.src.commands.automod.checks.check_is_moderator_permissions', new=AsyncMock(return_value=False)), \
             patch('storage.automod.update', new=AsyncMock()) as update:
            await Automod.automod_command_disable.callback(SimpleNamespace(), SimpleNamespace())
        update.assert_not_awaited()
