"""Provider parsing, manager boundaries, invite evidence and alert persistence."""
import asyncio
from datetime import datetime, timezone, timedelta
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import discord
from discord.ext import commands
from motor.motor_asyncio import AsyncIOMotorClient

from kuroo.src.commands.server_tools import ServerTools
from kuroo.workflows import server_tools as tools


def cog_fixture():
    bot = SimpleNamespace(BotConfig=SimpleNamespace(TWITCH_CLIENT_ID='', TWITCH_CLIENT_SECRET=''),
        latency=.05, shard_count=1, shards={0: object()}, cache=SimpleNamespace(automod={}))
    cog = ServerTools(bot)
    channel = SimpleNamespace(id=10, send=AsyncMock(return_value=SimpleNamespace(id=88)),
                              permissions_for=lambda member: discord.Permissions(send_messages=True))
    guild = SimpleNamespace(id=1, get_channel=lambda _: channel, member_count=20, me=object(), default_role=object())
    ctx = SimpleNamespace(guild=guild, author=SimpleNamespace(guild_permissions=discord.Permissions()),
                          send=AsyncMock(), defer=AsyncMock())
    return cog, ctx, channel


def response(data=None, raw=b'', status=200):
    res = MagicMock()
    res.status = status
    res.json = AsyncMock(return_value=data)
    res.content.read = AsyncMock(return_value=raw)
    res.__aenter__ = AsyncMock(return_value=res)
    res.__aexit__ = AsyncMock(return_value=None)
    return res


class ServerToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_identity_validation_before_network(self):
        feeds = tools.CreatorFeeds()
        for provider, value in [('youtube', 'https://localhost'), ('youtube', 'UCinvalid'),
                                ('twitch', 'http://127.0.0.1'), ('twitch', 'valid_login')]:
            with self.assertRaises(tools.ToolsError):
                await feeds.latest(provider, value)

    async def test_youtube_atom_feed_uses_video_id_and_bounded_title(self):
        raw = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
            <entry><yt:videoId>abcdefghijk</yt:videoId><title>Anime update</title></entry></feed>'''
        session = MagicMock()
        session.get.return_value = response(raw=raw)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        with patch.object(tools.aiohttp, 'ClientSession', return_value=session):
            item = await tools.CreatorFeeds().latest('youtube', 'UC'+'a'*22)
        self.assertEqual(item, {'id': 'abcdefghijk', 'title': 'Anime update',
                                'url': 'https://www.youtube.com/watch?v=abcdefghijk'})
        self.assertEqual(session.get.call_args.args[0], 'https://www.youtube.com/feeds/videos.xml')
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])

    async def test_oversize_or_invalid_video_feed_is_rejected(self):
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        for raw in (b'x'*1_000_001, b'<feed xmlns="http://www.w3.org/2005/Atom"><entry/></feed>'):
            session.get.return_value = response(raw=raw)
            with patch.object(tools.aiohttp, 'ClientSession', return_value=session):
                with self.assertRaises(tools.ToolsError):
                    await tools.CreatorFeeds().latest('youtube', 'UC'+'a'*22)

    async def test_twitch_token_cached_and_offline_not_announced(self):
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        session.post.return_value = response({'access_token': 'synthetic-token', 'expires_in': 3600})
        session.get.side_effect = [response({'data': []}), response({'data': [{'id': '42', 'title': 'Live now'}]})]
        feeds = tools.CreatorFeeds('synthetic-client', 'synthetic-secret')
        with patch.object(tools.aiohttp, 'ClientSession', return_value=session):
            self.assertIsNone(await feeds.latest('twitch', 'anime'))
            item = await feeds.latest('twitch', 'anime')
        session.post.assert_called_once()
        self.assertEqual(item['url'], 'https://www.twitch.tv/anime')
        self.assertEqual(item['id'], '42')

    async def test_all_server_tools_commands_require_manage_server(self):
        cog, ctx, channel = cog_fixture()
        self.addCleanup(cog.cog_unload)
        for command in (ServerTools.tracking, ServerTools.invites, ServerTools.inviteinfo,
                        ServerTools.dashboard, ServerTools.dashboardoff, ServerTools.alertadd,
                        ServerTools.alertremove, ServerTools.alerts):
            check = next(p for p in command.checks if 'has_guild_permissions' in p.__qualname__)
            with self.assertRaises(commands.MissingPermissions):
                await check(ctx)

    async def test_public_channel_rejected_for_staff_dashboard(self):
        cog, ctx, channel = cog_fixture()
        self.addCleanup(cog.cog_unload)
        channel.permissions_for = lambda _: discord.Permissions(view_channel=True, send_messages=True)
        with self.assertRaises(tools.ToolsError):
            await ServerTools.dashboard.callback(cog, ctx, channel)
        channel.send.assert_not_awaited()

    async def test_invite_create_does_not_consume_other_invite_evidence(self):
        cog, ctx, channel = cog_fixture()
        self.addCleanup(cog.cog_unload)
        cog.tracker.snapshots[1] = {'existing': (0, 2)}
        invite = SimpleNamespace(guild=ctx.guild, code='new', uses=0, inviter=SimpleNamespace(id=3))
        await cog.on_invite_create(invite)
        self.assertEqual(cog.tracker.snapshots[1], {'existing': (0, 2), 'new': (0, 3)})
        invite.uses = 1
        await cog.on_invite_create(invite)
        self.assertIsNone(cog.tracker.snapshots[1])

    async def test_dashboard_failure_does_not_block_creator_alerts(self):
        cog, ctx, channel = cog_fixture()
        self.addCleanup(cog.cog_unload)
        cog.bot.guilds = [ctx.guild]
        cog.refresh_dashboard = AsyncMock(side_effect=RuntimeError('synthetic'))
        cog.refresh_alerts = AsyncMock()
        await cog.refresh()
        cog.refresh_alerts.assert_awaited_once_with(ctx.guild)


@unittest.skipUnless(os.getenv('KUROO_TEST_MONGO_URI'), 'Disposable Mongo required')
class ServerToolsMongoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncIOMotorClient(os.environ['KUROO_TEST_MONGO_URI'], serverSelectionTimeoutMS=3000)
        await self.client.admin.command('ping')
        self.db = self.client['kuroo_test_'+uuid4().hex]
        async def collection(name):
            return self.db[name]
        self.patch = patch.object(tools, 'get_collection', side_effect=collection)
        self.patch.start()
        self.cog, self.ctx, self.channel = cog_fixture()
        self.cog.feeds.latest = AsyncMock(return_value={'id': 'first', 'title': 'Old video', 'url': 'https://youtube.com/'})
        self.guild = SimpleNamespace(id=1, invites=AsyncMock())
        self.member = SimpleNamespace(id=2, guild=self.guild, joined_at=datetime.now(timezone.utc))

    async def asyncTearDown(self):
        self.cog.cog_unload()
        self.patch.stop()
        await self.client.drop_database(self.db.name)
        self.client.close()

    async def test_single_invite_delta_attributed_rejoins_and_departures_recorded(self):
        tracker = tools.InviteTracker()
        tracker.snapshots[1] = {'code': (0, 7)}
        tracker.snapshot = AsyncMock(return_value={'code': (1, 7)})
        self.assertEqual((await tracker.joined(self.member))['inviter_id'], 7)
        await tracker.left(self.member)
        self.member.joined_at += timedelta(seconds=1)
        tracker.snapshot.return_value = {'code': (2, 7)}
        await tracker.joined(self.member)
        self.assertEqual(await self.db.invite_events.count_documents({'inviter_id': 7}), 2)
        self.assertEqual(await self.db.invite_events.count_documents({'left': True}), 1)
        await tracker.joined(self.member)  # duplicate gateway event cannot replace the first evidence
        self.assertEqual(await self.db.invite_events.count_documents({}), 2)
        row = await self.db.invite_events.find_one({'left': False})
        self.assertEqual(row['inviter_id'], 7)

    async def test_ambiguous_missing_deleted_new_or_reset_invites_are_unknown(self):
        tracker = tools.InviteTracker()
        cases = [({'a': (0, 7)}, {'a': (2, 7)}),
                 ({'a': (0, 7), 'b': (0, 8)}, {'a': (1, 7), 'b': (1, 8)}),
                 ({'a': (0, 7)}, {}), (None, {'a': (1, 7)}),
                 ({'a': (0, 7)}, {'a': (1, 7), 'new': (1, 8)}),
                 ({'a': (0, 7), 'b': (2, 8)}, {'a': (1, 7), 'b': (0, 8)}),
                 ({'a': (0, 7)}, None)]
        for old, new in cases:
            tracker.snapshots[1] = old
            tracker.snapshot = AsyncMock(return_value=new)
            self.member.joined_at += timedelta(seconds=1)
            self.assertIsNone((await tracker.joined(self.member))['inviter_id'])
        self.assertEqual(await self.db.invite_events.count_documents({'inviter_id': None}), len(cases))

    async def test_tracking_settings_survive_restart_and_are_guild_scoped(self):
        await tools.configure(1, tracking=True)
        self.assertTrue((await tools.tool_settings(1))['tracking'])
        self.assertFalse((await tools.tool_settings(2))['tracking'])
        fresh = tools.InviteTracker()
        fresh.snapshot = AsyncMock(return_value={'a': (1, 7)})
        self.assertIsNone((await fresh.joined(self.member))['inviter_id'])

    async def test_alert_baseline_deduplication_and_restart(self):
        identity = 'UC'+'a'*22
        await ServerTools.alertadd.callback(self.cog, self.ctx, 'youtube', identity, self.channel)
        await self.cog.refresh_alerts(self.ctx.guild)
        self.channel.send.assert_not_awaited()
        self.cog.feeds.latest.return_value = {'id': 'second', 'title': 'New video', 'url': 'https://youtube.com/'}
        await self.cog.refresh_alerts(self.ctx.guild)
        fresh, _, _ = cog_fixture()
        self.addCleanup(fresh.cog_unload)
        fresh.feeds = self.cog.feeds
        await fresh.refresh_alerts(self.ctx.guild)
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.channel.send.await_args.kwargs['allowed_mentions'].to_dict()['parse'], [])

    async def test_failed_alert_send_remains_pending_for_retry(self):
        await self.db.creator_alerts.insert_one({'_id': 'test', 'guild_id': 1, 'provider': 'youtube',
            'identity': 'UC'+'a'*22, 'last_id': 'old', 'channel_id': 10})
        self.channel.send.side_effect = RuntimeError('synthetic delivery failure')
        await self.cog.refresh_alerts(self.ctx.guild)
        self.assertEqual((await self.db.creator_alerts.find_one({'_id': 'test'}))['last_id'], 'old')
        self.channel.send.side_effect = None
        await self.cog.refresh_alerts(self.ctx.guild)
        self.assertEqual((await self.db.creator_alerts.find_one({'_id': 'test'}))['last_id'], 'first')

    async def test_staff_dashboard_reports_pending_restoration_without_member_records(self):
        guild = self.ctx.guild
        await tools.configure(1, dashboard_channel=10, dashboard_message=88)
        message = SimpleNamespace(edit=AsyncMock())
        self.channel.fetch_message = AsyncMock(return_value=message)
        self.channel.permissions_for = lambda _: discord.Permissions(send_messages=True)
        panic = {'enabled': True, 'lockdown': {'restore_pending': True, 'channels': {'10': {}, '20': {}}}}
        with patch('kuroo.src.commands.server_tools.safety.settings', new=AsyncMock(return_value=panic)):
            await self.cog.refresh_dashboard(guild)
        content = message.edit.await_args.kwargs['content']
        self.assertIn('restoration pending', content)
        self.assertIn('snapshots pending: 2', content)
        self.assertIn('Spam protection: on', content)
        self.assertNotIn('inviter', content)

    async def test_dashboardoff_disables_refresh_even_if_discord_delete_is_denied(self):
        await tools.configure(1, dashboard_channel=10, dashboard_message=88)
        error = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'synthetic')
        self.channel.fetch_message = AsyncMock(side_effect=error)
        await ServerTools.dashboardoff.callback(self.cog, self.ctx)
        self.assertEqual((await tools.tool_settings(1))['dashboard_message'], 0)

    async def test_dashboard_stops_when_channel_becomes_public(self):
        await tools.configure(1, dashboard_channel=10, dashboard_message=88)
        self.channel.permissions_for = lambda _: discord.Permissions(view_channel=True)
        await self.cog.refresh_dashboard(self.ctx.guild)
        self.assertEqual((await tools.tool_settings(1))['dashboard_message'], 0)

    async def test_concurrent_subscriptions_cannot_exceed_limit(self):
        identities = ['UC' + str(i).zfill(22) for i in range(12)]
        results = await asyncio.gather(*(ServerTools.alertadd.callback(self.cog, self.ctx, 'youtube', identity, self.channel)
                                       for identity in identities), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, tools.ToolsError) for result in results), 2)
        self.assertEqual(await self.db.creator_alerts.count_documents({'guild_id': 1}), 10)
