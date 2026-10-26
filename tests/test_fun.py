"""Anime filtering, provider failures and discoverability of reaction commands."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from discord import AllowedMentions

from kuroo.src.commands.fun import Fun
from kuroo.workflows.anime import AnimeGif, REACTIONS, get_anime_gif


class AnimeProviderTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, payload, status=200):
        response = SimpleNamespace(status=status, json=AsyncMock(return_value=payload))
        response_context = MagicMock()
        response_context.__aenter__ = AsyncMock(return_value=response)
        session = MagicMock()
        session.get.return_value = response_context
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        with patch('kuroo.workflows.anime.aiohttp.ClientSession', return_value=context):
            return await get_anime_gif('hug')

    async def test_accepts_catalog_anime_gif(self):
        result = await self.fetch({'results': [{'anime_name': 'Test Anime',
            'url': 'https://nekos.best/api/v2/hug/example.gif'}]})
        self.assertEqual(result, AnimeGif('https://nekos.best/api/v2/hug/example.gif', 'Test Anime'))

    async def test_rejects_unlabelled_or_untrusted_media_and_malformed_results(self):
        for payload in ({}, {'results': []}, {'results': [None]}, {'results': [{'url': 5, 'anime_name': 'Anime'}]},
                        {'results': [{'url': 'https://nekos.best/api/v2/hug/x.gif', 'anime_name': ''}]},
                        {'results': [{'url': 'https://example.com/cartoon.gif', 'anime_name': 'Anime'}]},
                        {'results': [{'url': 'https://nekos.best/api/v2/waifu/x.png', 'anime_name': 'Anime'}]}):
            with self.subTest(payload=payload):
                self.assertIsNone(await self.fetch(payload))
        self.assertIsNone(await self.fetch({}, status=429))

    async def test_timeout_and_unknown_actions_do_not_fall_back_to_search(self):
        with patch('kuroo.workflows.anime.aiohttp.ClientSession', side_effect=TimeoutError):
            self.assertIsNone(await get_anime_gif('hug'))
        with patch('kuroo.workflows.anime.aiohttp.ClientSession') as session:
            with self.assertRaises(ValueError):
                await get_anime_gif('cartoons')
            session.assert_not_called()


class FunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = Fun(SimpleNamespace(emoji=SimpleNamespace(FUN='🎉')))
        self.ctx = SimpleNamespace(author=SimpleNamespace(id=1, display_name='Sender'),
                                   send=AsyncMock(), defer=AsyncMock())

    async def test_reaction_targets_member_and_credits_anime(self):
        image = AnimeGif('https://nekos.best/api/v2/hug/example.gif', 'Anime Title')
        with patch('kuroo.src.commands.fun.get_anime_gif', new=AsyncMock(return_value=image)):
            await self.cog.send_reaction(self.ctx, 'hug', SimpleNamespace(id=2, display_name='Friend'))
        self.ctx.defer.assert_awaited_once()
        kwargs = self.ctx.send.await_args.kwargs
        self.assertEqual(kwargs['embed'].title, 'Sender hugs Friend')
        self.assertEqual(kwargs['embed'].image.url, image.url)
        self.assertIn('Anime Title', kwargs['embed'].footer.text)
        self.assertEqual(kwargs['allowed_mentions'].to_dict(), AllowedMentions.none().to_dict())

    async def test_provider_failure_has_helpful_reply_without_other_media(self):
        with patch('kuroo.src.commands.fun.get_anime_gif', new=AsyncMock(return_value=None)):
            await self.cog.send_reaction(self.ctx, 'pat')
        self.assertIn('unavailable', self.ctx.send.await_args.args[0])
        self.assertNotIn('embed', self.ctx.send.await_args.kwargs)

    async def test_prefix_shortcuts_and_slash_choices_cover_same_anime_catalog(self):
        commands = {command.name: command for command in self.cog.get_commands()}
        self.assertEqual(set(commands), set(REACTIONS) | {'anime'})
        self.assertEqual(set(Fun.anime.app_command.parameters[0].choices[i].value
                             for i in range(len(REACTIONS))), set(REACTIONS))
        for action in REACTIONS:
            with self.subTest(action=action), patch.object(self.cog, 'send_reaction', new=AsyncMock()) as send:
                await commands[action].callback(self.cog, self.ctx)
                send.assert_awaited_once_with(self.ctx, action, None)
