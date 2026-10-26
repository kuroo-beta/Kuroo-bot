"""Raw name-style request, confirmation and startup/admin boundaries."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import commands

from kuroo.src.events.ready import ready
from kuroo.workflows.name_style import NameStyleResult, apply_name_style, name_style_payload


def config(**overrides):
    values = dict(NAME_STYLE_ENABLED=True, NAME_STYLE_FONT_ID='4',
                  NAME_STYLE_EFFECT_ID='4', NAME_STYLE_COLORS='FFFFFF')
    values.update(overrides)
    return SimpleNamespace(**values)


class NameStyleTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_payload_uses_guild_member_route_and_confirms_response(self):
        settings = config()
        expected = {'display_name_font_id': 4, 'display_name_effect_id': 4,
                    'display_name_colors': [16777215]}
        request = AsyncMock(return_value={'user': {'id': '10'}, **expected})
        bot = SimpleNamespace(BotConfig=settings, http=SimpleNamespace(request=request))
        self.assertIs(await apply_name_style(bot, SimpleNamespace(id=123)), NameStyleResult.APPLIED)
        route = request.await_args.args[0]
        self.assertEqual(route.method, 'PATCH')
        self.assertTrue(route.url.endswith('/guilds/123/members/@me'))
        self.assertEqual(request.await_args.kwargs, {'json': expected})

    async def test_ignored_or_mismatched_fields_are_not_reported_as_applied(self):
        for response in ({'user': {'id': '10'}}, None, {'display_name_font_id': 3}):
            bot = SimpleNamespace(BotConfig=config(), http=SimpleNamespace(request=AsyncMock(return_value=response)))
            self.assertIs(await apply_name_style(bot, SimpleNamespace(id=1)), NameStyleResult.UNCONFIRMED)

    async def test_gradient_converts_two_hex_colors_and_rejects_invalid_settings_before_http(self):
        self.assertEqual(name_style_payload(config(NAME_STYLE_EFFECT_ID='2', NAME_STYLE_COLORS='#FF0000, 00FF00')),
                         {'display_name_font_id': 4, 'display_name_effect_id': 2,
                          'display_name_colors': [16711680, 65280]})
        invalid = [config(NAME_STYLE_FONT_ID='100'), config(NAME_STYLE_EFFECT_ID='8'),
                   config(NAME_STYLE_COLORS='invalid'), config(NAME_STYLE_EFFECT_ID='2'),
                   config(NAME_STYLE_COLORS='FFFFFF,000000')]
        for settings in invalid:
            bot = SimpleNamespace(BotConfig=settings, http=SimpleNamespace(request=AsyncMock()))
            with self.assertRaises(ValueError):
                await apply_name_style(bot, SimpleNamespace(id=1))
            bot.http.request.assert_not_awaited()

    async def test_sync_isolates_failure_avoids_reconnect_duplicates_and_handles_rejoin(self):
        guilds = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        bot = SimpleNamespace(BotConfig=config(), guilds=guilds)
        cog = ready(bot)
        error = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'denied')
        with patch('kuroo.src.events.ready.apply_name_style', new=AsyncMock(
                side_effect=[error, NameStyleResult.APPLIED, NameStyleResult.APPLIED])) as apply, \
             patch('kuroo.src.events.ready.logger'):
            await cog.sync_name_styles()
            await cog.sync_name_styles()
            self.assertEqual(apply.await_count, 2)
            await cog.on_guild_remove(guilds[0])
            await cog.on_guild_join(guilds[0])
            self.assertEqual(apply.await_count, 3)

    async def test_disabled_auto_styling_makes_no_request(self):
        cog = ready(SimpleNamespace(BotConfig=config(NAME_STYLE_ENABLED=False)))
        with patch('kuroo.src.events.ready.apply_name_style', new=AsyncMock()) as apply:
            await cog.on_guild_join(SimpleNamespace(id=1))
        apply.assert_not_awaited()

    async def test_manual_retry_reports_unconfirmed_style_and_does_not_claim_success(self):
        cog = ready(SimpleNamespace(BotConfig=config()))
        ctx = SimpleNamespace(guild=SimpleNamespace(id=1), defer=AsyncMock(), send=AsyncMock())
        with patch('kuroo.src.events.ready.apply_name_style', new=AsyncMock(return_value=NameStyleResult.UNCONFIRMED)):
            await ready.setbotstyle.callback(cog, ctx)
        self.assertIn('did not confirm', ctx.send.await_args.args[0])

    async def test_manual_retry_requires_guild_administrator(self):
        predicate = next(check for check in ready.setbotstyle.checks if 'has_guild_permissions' in check.__qualname__)
        ctx = SimpleNamespace(guild=SimpleNamespace(id=1),
                              author=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False)))
        with self.assertRaises(commands.MissingPermissions):
            await discord.utils.maybe_coroutine(predicate, ctx)
        ctx.author.guild_permissions.administrator = True
        self.assertTrue(await discord.utils.maybe_coroutine(predicate, ctx))
