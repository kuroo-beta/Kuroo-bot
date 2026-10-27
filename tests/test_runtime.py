import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from kuroo.engine.Bot import AutoShardedBot
from kuroo.src.events.ready import ready


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_cogs_and_hybrid_commands_load_without_gateway(self):
        bot = AutoShardedBot()
        await bot._async_setup_hook()
        try:
            self.assertEqual(bot.status, discord.Status.online)
            self.assertEqual(bot.activity.type, discord.ActivityType.streaming)
            self.assertEqual(bot.activity.url, bot.BotConfig.STREAMING_URL)
            self.assertEqual(bot.activity.name, bot.BotConfig.STREAMING_NAME)
            await bot.load_extension('kuroo.src')
            self.assertEqual(len(bot.cogs), 43)
            self.assertGreater(len(list(bot.walk_commands())), 200)
            self.assertGreater(len(bot.tree.get_commands()), 60)
            self.assertIn('help', bot.all_commands)
            self.assertIsNotNone(bot.tree.get_command('help'))
            self.assertIsNotNone(bot.tree.get_command('anime'))
            self.assertIsNotNone(bot.tree.get_command('setbotstyle'))
            for name in ('cuddle', 'poke', 'highfive', 'blush', 'cheer'):
                self.assertIn(name, bot.all_commands)
            for name in ('gay', 'lesbian', 'horny', 'simp', 'iq', 'cute', 'fakeban', 'fakekick', 'nukeall', 'ship'):
                self.assertNotIn(name, bot.all_commands)

            for name in ('premiumcode', 'premiumgrant', 'premiumrevoke', 'votepremium', 'redeem', 'noprefix', 'rank', 'lb', 'daily', 'work', 'balance', 'deposit', 'withdraw', 'shop', 'buy', 'inventory', 'equip', 'leveling', 'levelchannel', 'welcomecards', 'leave', 'welcomepreview', 'leavepreview', 'levelconfig', 'xp', 'economy', 'levelreward', 'liveleaderboard'):
                self.assertIn(name, bot.all_commands)
                self.assertIsNotNone(bot.tree.get_command(name))
            # Validate the exact definitions sent by tree.sync, including nested options.
            definitions = [command.to_dict(bot.tree) for command in bot.tree.get_commands()]
            self.assertLessEqual(len(definitions), 100)
            def validate(items):
                for item in items:
                    self.assertTrue(1 <= len(item['name']) <= 32, item['name'])
                    if 'description' in item:
                        self.assertTrue(1 <= len(item['description']) <= 100, item['name'])
                    self.assertLessEqual(len(item.get('choices', [])), 25)
                    self.assertLessEqual(len(item.get('options', [])), 25)
                    validate(item.get('options', []))
            validate(definitions)
            for path in [('automod', 'sync'), ('automod', 'rule'), ('automod', 'words'), ('automod', 'badge'),
                         ('music', 'stay'), ('music', 'filter'), ('music', 'favourites'),
                         ('servertools', 'tracking'), ('servertools', 'dashboard'), ('servertools', 'alertadd'),
                         ('panic', 'setup'), ('panic', 'unlock'), ('onboard', 'addrole'), ('onboard', 'panel'), ('levelconfig', 'announcements'), ('levelconfig', 'ignorechannel'),
                         ('levelconfig', 'rate'), ('xp', 'edit'), ('economy', 'rewards'), ('levelreward', 'set'), ('liveleaderboard', 'set')]:
                command = bot.tree.get_command(path[0]).get_command(path[1])
                self.assertIsNotNone(command)
                self.assertIsNotNone(bot.get_command(' '.join(path)))
        finally:
            for name in list(bot.cogs):
                await bot.remove_cog(name)
            await bot.http.close()

    async def test_streaming_card_keeps_title_and_refreshes_live_counts(self):
        sent = []
        async def change_presence(**kwargs):
            sent.append(kwargs)
            bot.guilds.append(SimpleNamespace(member_count=None))
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(), is_closed=lambda: len(sent) >= 2,
            change_presence=change_presence,
            BotConfig=SimpleNamespace(STREAMING_NAME='Kuroo • Security & Moderation', STREAMING_URL='https://www.twitch.tv/discord'),
            guilds=[SimpleNamespace(member_count=10)], shard_count=1,
        )
        with patch('kuroo.src.events.ready.asyncio.sleep', new=AsyncMock()):
            await ready(bot).activity()
        for item in sent:
            payload = item['activity'].to_dict()
            self.assertEqual(item['status'], discord.Status.online)
            self.assertEqual(payload['type'], discord.ActivityType.streaming.value)
            self.assertEqual(payload['url'], 'https://www.twitch.tv/discord')
            self.assertEqual(payload['name'], 'Kuroo • Security & Moderation')
            self.assertEqual(payload['details'], 'Kuroo • Security & Moderation')
        self.assertEqual(sent[0]['activity'].to_dict()['state'], '1 server • 10 users • 1 shard')
        self.assertEqual(sent[1]['activity'].to_dict()['state'], '2 servers • 10 users • 1 shard')

    async def test_display_name_changes_once_and_blank_preserves_current_name(self):
        for desired in ('Kuroo', ''):
            bot = SimpleNamespace(user=SimpleNamespace(name='Old Name', edit=AsyncMock()),
                                  BotConfig=SimpleNamespace(DISPLAY_NAME=desired))
            cog = ready(bot)
            await cog.update_display_name()
            await cog.update_display_name()
            self.assertEqual(bot.user.edit.await_count, int(bool(desired)))
            if desired:
                bot.user.edit.assert_awaited_once_with(username=desired)

    async def test_rejected_profile_name_does_not_retry_or_block_presence(self):
        error = discord.HTTPException(SimpleNamespace(status=400, reason='Bad request'), 'invalid name')
        bot = SimpleNamespace(user=SimpleNamespace(name='Kuroo', edit=AsyncMock(side_effect=error)),
                              BotConfig=SimpleNamespace(DISPLAY_NAME='𝕂𝕌ℝ𝕆𝕆'))
        cog = ready(bot)
        with patch('kuroo.src.events.ready.logger'):
            await cog.update_display_name()
            await cog.update_display_name()
        bot.user.edit.assert_awaited_once()

    async def test_reconnect_does_not_duplicate_presence_task(self):
        bot = SimpleNamespace(
            user=SimpleNamespace(id=42, name="Kuroo"), BotConfig=SimpleNamespace(DASHBOARD_ENABLED=False, DISCORD_CLIENT_ID="", DISPLAY_NAME=""),
            urls=SimpleNamespace(),
            tree=SimpleNamespace(sync=AsyncMock()), fetch_user=AsyncMock(),
        )
        cog = ready(bot)
        waiting = asyncio.Event()
        cog.activity = waiting.wait
        cog.on_ready_startups = AsyncMock()
        with patch('kuroo.src.events.ready.logger'), patch('kuroo.src.events.ready.users_config.developer', ()):
            await cog.on_ready()
            first_task = cog._activity_task
            await cog.on_ready()
            self.assertIs(cog._activity_task, first_task)
            bot.tree.sync.assert_awaited_once_with()
            self.assertTrue(bot._slash_synced)
            cog.cog_unload()
            with self.assertRaises(asyncio.CancelledError):
                await first_task


if __name__ == '__main__':
    unittest.main()
