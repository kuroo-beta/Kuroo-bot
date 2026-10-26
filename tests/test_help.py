"""Live discovery, startup registration and actual Components V2 help payloads."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord
from discord import app_commands
from discord.ext import commands

from kuroo.engine.Bot import AutoShardedBot
from kuroo.src.commands.help import Help, HomeView, AllCommandsView, CategoryView, CommandView, PageButton, BackButton, CommandSelectRow
from kuroo.workflows.command_catalog import CommandCatalog


class Unlabelled(commands.Cog):
    @commands.hybrid_command(name='sample', aliases=['example'], help='A hybrid command')
    async def sample(self, ctx, amount: int = 1):
        pass

    @commands.group(name='tools', aliases=['tool'], invoke_without_command=True)
    async def tools(self, ctx):
        pass

    @tools.command(name='nested', help='Nested command details')
    async def nested(self, ctx, value: str):
        pass

    @tools.command(name='privatechild', hidden=True)
    async def privatechild(self, ctx):
        pass

    @commands.group(name='private', hidden=True)
    async def private(self, ctx):
        pass

    @private.command(name='secret')
    async def secret(self, ctx):
        pass

    @app_commands.command(name='native', description='A native slash-only command')
    async def native(self, interaction: discord.Interaction, value: int = 1):
        pass

    @app_commands.command(name='nativehidden', description='Hidden native command', extras={'hidden': True})
    async def nativehidden(self, interaction: discord.Interaction):
        pass


class HiddenCog(commands.Cog):
    cog_info = SimpleNamespace(name='Hidden', hidden=True)

    @commands.hybrid_command(name='adminsecret')
    async def adminsecret(self, ctx):
        pass


class HelpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = AutoShardedBot()
        await self.bot._async_setup_hook()
        self.bot._connection.user = discord.ClientUser(state=self.bot._connection, data={
            'id': '42', 'username': 'Kuroo', 'discriminator': '0', 'avatar': None,
        })
        self.bot.cache = SimpleNamespace(guilds={'321': {'prefix': '!'}})
        self.ctx = SimpleNamespace(guild=SimpleNamespace(id=321), author=SimpleNamespace(id=100),
                                   interaction=None, send=AsyncMock())
        await self.bot.add_cog(Unlabelled())
        await self.bot.add_cog(HiddenCog())

    async def asyncTearDown(self):
        for name in list(self.bot.cogs):
            await self.bot.remove_cog(name)
        await self.bot.http.close()

    def serialize(self, view):
        with discord.http.handle_message_parameters(view=view) as params:
            self.assertFalse(params.files)
            result = params.payload
        self.assertTrue(result['flags'] & 32768)
        self.assertNotIn('accent_color', json.dumps(result))
        return result

    def interaction(self, *, user_id=100, values=None):
        return SimpleNamespace(user=SimpleNamespace(id=user_id), message=SimpleNamespace(edit=AsyncMock()),
                               data={'values': values or ['0']},
                               response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))

    async def test_discovers_prefix_native_hybrid_nested_and_unlabelled_commands(self):
        catalog = CommandCatalog(self.bot, self.ctx.guild)
        names = [entry.name for entry in catalog.entries]
        self.assertEqual(names, ['native', 'sample', 'tools', 'tools nested'])
        hybrid = catalog.find('sample')
        self.assertIsNotNone(hybrid.prefix)
        self.assertIsNotNone(hybrid.slash)
        self.assertEqual(catalog.find('example'), hybrid)
        self.assertIn('!sample [amount=1]', hybrid.usages('!'))
        self.assertIn('/sample [amount]', hybrid.usages('!'))
        self.assertIn('Unlabelled', catalog.categories)
        self.assertEqual(catalog.find('/native').category, 'Unlabelled')

    async def test_hidden_ancestry_and_hidden_cogs_never_leak(self):
        text = json.dumps(self.serialize(AllCommandsView(self.bot, self.ctx)))
        for hidden in ['adminsecret', 'nativehidden', 'privatechild', 'private secret']:
            self.assertNotIn(hidden, text)

    async def test_guild_specific_and_ungrouped_commands_are_discovered(self):
        async def local(interaction: discord.Interaction):
            pass
        self.bot.tree.add_command(app_commands.Command(name='guildonly', description='Local command', callback=local), guild=discord.Object(id=321))
        async def loose(ctx):
            pass
        self.bot.add_command(commands.Command(loose, name='loose'))
        catalog = CommandCatalog(self.bot, self.ctx.guild)
        self.assertEqual(catalog.find('loose').category, 'General')
        self.assertIsNotNone(catalog.find('guildonly'))
        self.assertIsNone(CommandCatalog(self.bot).find('guildonly'))

    async def test_help_works_when_remote_slash_fetch_is_unavailable(self):
        cog = Help(self.bot)
        self.bot.tree.fetch_commands = AsyncMock(side_effect=AssertionError('Help must use local registry'))
        with patch.object(self.bot.BotConfig, 'BOT_BANNER_URL', 'https://example.com/banner.png'):
            await Help.help.callback(cog, self.ctx)
        self.bot.tree.fetch_commands.assert_not_awaited()
        view = self.ctx.send.await_args.kwargs['view']
        body = self.serialize(view)
        self.assertEqual(body['components'][0]['type'], 17)
        self.assertEqual(body['components'][0]['components'][0]['type'], 12)
        self.assertIn('Unlabelled', json.dumps(body))
        view.stop()

    async def test_direct_help_supports_nested_group_aliases_and_guild_prefix(self):
        cog = Help(self.bot)
        cog._banner_loaded = True
        for query in ['!tools nested', 'tool nested']:
            await Help.help.callback(cog, self.ctx, command=query)
            view = self.ctx.send.await_args.kwargs['view']
            text = json.dumps(self.serialize(view))
            self.assertIn('!tools nested <value>', text)
            self.assertIn('Nested command details', text)
            view.stop()
        await Help.help.callback(cog, self.ctx, command='private secret')
        self.assertIn('Command not found', self.ctx.send.await_args.args[0])

    async def test_registry_add_remove_is_visible_without_reloading_help(self):
        cog = Help(self.bot)
        cog._banner_loaded = True
        async def later(ctx):
            pass
        self.bot.add_command(commands.Command(later, name='later'))
        await Help.help.callback(cog, self.ctx)
        self.assertIsNotNone(self.ctx.send.await_args.kwargs['view'].catalog.find('later'))
        self.ctx.send.await_args.kwargs['view'].stop()
        self.bot.remove_command('later')
        await Help.help.callback(cog, self.ctx)
        self.assertIsNone(self.ctx.send.await_args.kwargs['view'].catalog.find('later'))
        self.ctx.send.await_args.kwargs['view'].stop()

    async def test_large_categories_and_long_descriptions_remain_within_limits(self):
        async def callback(ctx):
            pass
        for index in range(37):
            self.bot.add_command(commands.Command(callback, name=f'public{index:02}', help='Long description ' * 100))
        self.bot.help_banner_url = 'https://example.com/banner.png'
        seen = []
        for page in range(5):
            view = AllCommandsView(self.bot, self.ctx, page=page)
            seen.extend(entry.name for entry in view.entries)
            body = self.serialize(view)
            self.assertEqual(body['components'][0]['type'], 17)
            self.assertEqual(body['components'][0]['components'][0]['type'], 12)
            self.assertNotIn('kuroo-details.txt', json.dumps(body))
            for item in view.walk_children():
                if isinstance(item, discord.ui.Select):
                    self.assertLessEqual(len(item.options), 25)
                    self.assertTrue(all(len(option.description) <= 100 for option in item.options))
            view.stop()
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), {entry.name for entry in CommandCatalog(self.bot).entries})

    async def test_more_than_25_categories_are_paginated(self):
        async def method(self, ctx):
            pass
        for index in range(26):
            cls = type(f'Generated{index:02}', (commands.Cog,), {'method': commands.Command(method, name=f'generated{index:02}')})
            await self.bot.add_cog(cls())
        categories = []
        for page in range(3):
            view = HomeView(self.bot, self.ctx, page=page)
            self.serialize(view)
            for item in view.walk_children():
                if isinstance(item, discord.ui.Select):
                    self.assertLessEqual(len(item.options), 25)
                    categories.extend(option.label for option in item.options)
            view.stop()
        self.assertEqual(len(categories), 27)
        self.assertEqual(len(categories), len(set(categories)))

    async def test_navigation_callbacks_and_author_gate(self):
        view = AllCommandsView(self.bot, self.ctx)
        row = next(item for item in view.walk_children() if isinstance(item, CommandSelectRow))
        denied = self.interaction(user_id=999)
        await row.select_command(denied)
        denied.response.edit_message.assert_not_awaited()
        denied.response.send_message.assert_awaited_once()
        interaction = self.interaction()
        await row.select_command(interaction)
        selected = interaction.response.edit_message.await_args.kwargs['view']
        self.assertIsInstance(selected, CommandView)
        button = next(item for item in selected.walk_children() if isinstance(item, BackButton))
        back = self.interaction()
        await button.callback(back)
        self.assertIsInstance(back.response.edit_message.await_args.kwargs['view'], CategoryView)
        back.response.edit_message.await_args.kwargs['view'].stop()

    async def test_page_buttons_move_through_live_command_pages(self):
        async def command(ctx):
            pass
        for index in range(20):
            self.bot.add_command(commands.Command(command, name=f'page{index:02}'))
        view = AllCommandsView(self.bot, self.ctx)
        next_button = next(item for item in view.walk_children() if isinstance(item, PageButton) and item.delta == 1)
        interaction = self.interaction()
        await next_button.callback(interaction)
        second = interaction.response.edit_message.await_args.kwargs['view']
        self.assertEqual(second.current_page, 1)
        self.assertFalse({entry.name for entry in view.entries} & {entry.name for entry in second.entries})
        previous_button = next(item for item in second.walk_children() if isinstance(item, PageButton) and item.delta == -1)
        back = self.interaction()
        await previous_button.callback(back)
        first = back.response.edit_message.await_args.kwargs['view']
        self.assertEqual([entry.name for entry in first.entries], [entry.name for entry in view.entries])
        first.stop()

    async def test_unloaded_command_during_navigation_is_handled(self):
        view = AllCommandsView(self.bot, self.ctx)
        row = next(item for item in view.walk_children() if isinstance(item, CommandSelectRow))
        self.bot.remove_command('sample')
        interaction = self.interaction(values=[str(row.names.index('sample'))])
        await row.select_command(interaction)
        interaction.response.send_message.assert_awaited_once()
        interaction.response.edit_message.assert_not_awaited()
        view.stop()

    async def test_timeout_disables_nested_controls(self):
        view = HomeView(self.bot, self.ctx)
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.on_timeout()
        self.assertTrue(all(item.disabled for item in view.walk_children() if isinstance(item, (discord.ui.Button, discord.ui.Select))))
        view.message.edit.assert_awaited_once_with(view=view)
        view.stop()

    async def test_sync_recovery_is_admin_only_and_publishes_live_registry(self):
        self.assertTrue(Help.synccommands.hidden)
        check = Help.synccommands.checks[0]
        with patch('kuroo.src.checks.checks.check_is_admin_predicate', return_value=False):
            self.assertFalse(check(self.ctx))
        with patch('kuroo.src.checks.checks.check_is_admin_predicate', return_value=True):
            self.assertTrue(check(self.ctx))
        self.ctx.defer = AsyncMock()
        with patch.object(self.bot.tree, 'sync', new=AsyncMock(return_value=[1, 2])) as sync:
            await Help.synccommands.callback(Help(self.bot), self.ctx)
        sync.assert_awaited_once_with()
        self.assertTrue(self.bot._slash_synced)
        self.assertIn('Synced 2', self.ctx.send.await_args.args[0])

    async def test_real_startup_registers_new_commands_before_publishing(self):
        # Real startup has no synthetic test cogs competing for slash command slots.
        await self.bot.remove_cog('Unlabelled')
        await self.bot.remove_cog('HiddenCog')
        async def publish():
            self.assertLessEqual(len(self.bot.tree.get_commands()), 100)
            for name in ('rank', 'lb', 'balance', 'daily', 'work', 'leave', 'welcomecards', 'premiumcode', 'help', 'synccommands', 'levelconfig', 'xp', 'economy', 'panic', 'onboard'):
                self.assertIsNotNone(self.bot.get_command(name), name)
                self.assertIsNotNone(self.bot.tree.get_command(name), name)
            return self.bot.tree.get_commands()
        with patch.object(self.bot.BotConfig, 'SYNC_EMOJIS', False), patch.object(self.bot.tree, 'sync', side_effect=publish) as sync:
            await self.bot.setup_hook()
        sync.assert_awaited_once_with()
        self.assertTrue(self.bot._slash_synced)
        catalog = CommandCatalog(self.bot, self.ctx.guild)
        for name in ('rank', 'lb', 'daily', 'work', 'leave', 'welcomecards'):
            self.assertIsNotNone(catalog.find(name))
        self.assertIsNone(catalog.find('synccommands'))
        seen = []
        for page in range((len(catalog.entries) + 9) // 10):
            view = AllCommandsView(self.bot, self.ctx, page=page)
            self.serialize(view)
            seen.extend(entry.name for entry in view.entries)
            view.stop()
        self.assertEqual(set(seen), {entry.name for entry in catalog.entries})

    async def test_community_categories_custom_emojis_and_group_inheritance(self):
        from kuroo.src.commands.community import Community
        await self.bot.add_cog(Community(self.bot))
        self.bot.help_banner_url = 'https://example.com/banner.png'
        catalog = CommandCatalog(self.bot, self.ctx.guild)
        for name, category in [('rank', 'Leveling'), ('levelconfig rate', 'Leveling'), ('xp edit', 'Leveling'),
                               ('economy rewards', 'Economy'), ('daily', 'Economy'), ('leave', 'MemberCards')]:
            self.assertEqual(catalog.find(name).category, category)
        self.assertNotIn('Community', catalog.categories)
        for view in [HomeView(self.bot, self.ctx), AllCommandsView(self.bot, self.ctx),
                     CategoryView(self.bot, self.ctx, category='Leveling'),
                     CommandView(self.bot, self.ctx, entry=catalog.find('xp edit'))]:
            payload = self.serialize(view)
            self.assertEqual(payload['components'][0]['type'], 17)
            self.assertEqual(payload['components'][0]['components'][0]['type'], 12)
            for item in view.walk_children():
                if isinstance(item, discord.ui.Select):
                    self.assertTrue(all(option.emoji and option.emoji.id for option in item.options))
                elif isinstance(item, discord.ui.Button):
                    self.assertIsNotNone(item.emoji)
                    self.assertIsNotNone(item.emoji.id)
            view.stop()

    async def test_failed_sync_can_be_retried_without_losing_commands(self):
        self.ctx.defer = AsyncMock()
        error = discord.HTTPException(SimpleNamespace(status=503, reason='Unavailable'), 'down')
        with patch.object(self.bot.tree, 'sync', new=AsyncMock(side_effect=error)):
            await Help.synccommands.callback(Help(self.bot), self.ctx)
        self.assertIn('Discord rejected', self.ctx.send.await_args.args[0])
        self.assertIsNotNone(self.bot.get_command('sample'))

    async def test_startup_sync_sees_community_commands_and_failure_keeps_prefix_commands(self):
        async def load(name):
            async def rank(ctx):
                pass
            self.bot.add_command(commands.Command(rank, name='rank'))
        async def publish():
            self.assertIsNotNone(self.bot.get_command('rank'))
            return []
        with patch.object(self.bot.BotConfig, 'SYNC_EMOJIS', False), patch.object(self.bot, 'load_extension', side_effect=load), patch.object(self.bot.tree, 'sync', side_effect=publish):
            await self.bot.setup_hook()
        self.assertTrue(self.bot._slash_synced)
        error = discord.HTTPException(SimpleNamespace(status=503, reason='Unavailable'), 'down')
        with patch.object(self.bot.BotConfig, 'SYNC_EMOJIS', False), patch.object(self.bot, 'load_extension', new=AsyncMock()), patch.object(self.bot.tree, 'sync', new=AsyncMock(side_effect=error)):
            await self.bot.setup_hook()
        self.assertFalse(self.bot._slash_synced)
        self.assertIsNotNone(self.bot.get_command('rank'))
