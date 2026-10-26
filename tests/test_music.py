"""Music queue, voice boundaries, filters, and persistent favourites."""
import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import discord
from discord.ext import commands
from motor.motor_asyncio import AsyncIOMotorClient
import wavelink

from kuroo.src.commands.music import Music, MusicControls
from kuroo.src.events.wavelink import Wavelink
from kuroo.workflows import music
from kuroo.workflows.components import message_payload


def track(title='Song', identifier='abcdefghijk'):
    return wavelink.Playable({'encoded': identifier, 'pluginInfo': {}, 'info': {'identifier': identifier, 'isSeekable': True,
        'author': 'Artist', 'length': 180000, 'isStream': False, 'position': 0, 'title': title,
        'uri': f'https://www.youtube.com/watch?v={identifier}', 'sourceName': 'youtube'}})


def fixture():
    player = MagicMock(spec=wavelink.Player)
    player.channel = SimpleNamespace(id=20)
    player.queue = wavelink.Queue()
    player.auto_queue = wavelink.Queue()
    player.current = None
    player.autoplay = wavelink.AutoPlayMode.partial
    for name in ('play', 'pause', 'skip', 'seek', 'set_filters', 'disconnect', 'set_volume'):
        setattr(player, name, AsyncMock())
    async def play(item):
        player.current = item
        player.queue.history.put(item)
    player.play.side_effect = play
    guild = SimpleNamespace(id=1, voice_client=player)
    player.guild = guild
    ctx = SimpleNamespace(guild=guild, channel=SimpleNamespace(id=10),
        author=SimpleNamespace(id=2, voice=SimpleNamespace(channel=SimpleNamespace(id=20)),
                               guild_permissions=discord.Permissions()),
        send=AsyncMock(), defer=AsyncMock())
    bot = SimpleNamespace(cache=SimpleNamespace(music={}, users={}, guilds={}),
                          BotConfig=SimpleNamespace(MUSIC_SEARCH_SOURCE='ytmsearch'))
    cog = Music(bot)
    cog.send_music_controls = AsyncMock()
    return cog, ctx, player


class MusicTests(unittest.IsolatedAsyncioTestCase):
    def test_search_uses_fixed_sources_and_rejects_arbitrary_urls(self):
        self.assertEqual(music.search_query('anime opening', 'ytmsearch'), 'ytmsearch:anime opening')
        url = 'https://www.youtube.com/watch?v=abcdefghijk'
        self.assertEqual(music.search_query(url, 'ytmsearch'), url)
        for query in ('http://127.0.0.1/x', 'https://youtube.com.evil/x', 'file:///etc/passwd',
                      '//localhost/x', 'https://user:pass@youtube.com/x', 'https://youtube.com:bad/x',
                      'ytsearch:malformed', 'https://localhost/x', ' ', 'a'*501):
            with self.assertRaises(music.MusicError, msg=query):
                music.search_query(query, 'ytmsearch')
        with self.assertRaises(music.MusicError):
            music.search_query('song', 'arbitrary')

    def test_filters_are_real_wavelink_payloads_and_reset_is_clean(self):
        for preset in music.FILTERS:
            result = music.make_filter(preset)
            self.assertIsInstance(result, wavelink.Filters)
            self.assertIsInstance(result(), dict)
        self.assertNotIn('timescale', music.make_filter('reset')())
        self.assertEqual(music.make_filter('nightcore')()['timescale']['speed'], 1.2)
        self.assertEqual(music.make_filter('8d')()['rotation']['rotationHz'], .2)
        with self.assertRaises(music.MusicError):
            music.make_filter('invalid')

    async def test_same_voice_required_and_missing_voice_rejected(self):
        cog, ctx, player = fixture()
        self.assertIs(cog.player_for(ctx), player)
        ctx.author.voice.channel.id = 99
        with self.assertRaises(music.MusicError):
            cog.player_for(ctx)
        ctx.author.voice = None
        with self.assertRaises(music.MusicError):
            cog.player_for(ctx)
        with patch('wavelink.Playable.search', new=AsyncMock()) as search:
            with self.assertRaises(music.MusicError):
                await cog.enqueue(ctx, 'song')
            search.assert_not_awaited()

    async def test_enqueue_search_starts_once_and_queues_concurrent_requests(self):
        cog, ctx, player = fixture()
        with patch('wavelink.Playable.search', new=AsyncMock(return_value=[track()])), \
             patch.object(cog, 'connect', new=AsyncMock(return_value=player)):
            await asyncio.gather(cog.enqueue(ctx, 'song'), cog.enqueue(ctx, 'song'))
        self.assertEqual(player.play.await_count, 1)
        self.assertEqual(len(player.queue), 1)
        self.assertEqual(player.current.extras.requester_id, 2)
        self.assertEqual(cog.channels[1], 10)

    async def test_playlists_are_atomic_and_queue_limit_does_not_partially_add(self):
        cog, ctx, player = fixture()
        cog.bot.cache.guilds = {'1': {'subscription': 'silver'}}
        playlist = MagicMock(spec=wavelink.Playlist)
        playlist.tracks = [track('First'), track('Second')]
        with patch('wavelink.Playable.search', new=AsyncMock(return_value=playlist)), \
             patch.object(cog, 'connect', new=AsyncMock(return_value=player)):
            self.assertEqual(await cog.enqueue(ctx, 'https://www.youtube.com/playlist?list=example'), 2)
            self.assertEqual(player.current.title, 'First')
            self.assertEqual(player.queue[0].title, 'Second')
            with patch.object(music, 'MAX_QUEUE', 1):
                with self.assertRaises(music.MusicError):
                    await cog.enqueue(ctx, 'song')
            self.assertEqual(len(player.queue), 1)

    async def test_link_and_default_volume_subscription_contracts(self):
        cog, ctx, player = fixture()
        with patch('wavelink.Playable.search', new=AsyncMock()) as search:
            with self.assertRaises(music.MusicError):
                await cog.enqueue(ctx, 'https://youtu.be/abcdefghijk')
            search.assert_not_awaited()
        self.assertEqual(cog.default_volume(1, {'default_volume': 30}), 80)
        cog.bot.cache.users = {'2': {'no_prefix_subscription': True}}
        self.assertTrue(cog.allow_links(ctx))
        cog.bot.cache.guilds = {'1': {'subscription': 'golden'}}
        self.assertEqual(cog.default_volume(1, {'default_volume': 30}), 30)

    async def test_queue_edits_preserve_positions_and_validate_before_mutation(self):
        cog, ctx, player = fixture()
        player.queue.put([track('A'), track('B'), track('C')])
        await Music.move.callback(cog, ctx, 1, 3)
        self.assertEqual([t.title for t in player.queue], ['B', 'C', 'A'])
        with self.assertRaises(music.MusicError):
            await Music.move.callback(cog, ctx, 1, 10)
        self.assertEqual([t.title for t in player.queue], ['B', 'C', 'A'])
        await Music.remove.callback(cog, ctx, 2)
        await Music.skipto.callback(cog, ctx, 2)
        self.assertEqual([t.title for t in player.queue], ['A'])
        player.skip.assert_awaited_once_with(force=True)

    async def test_seek_rejects_stream_and_out_of_range(self):
        cog, ctx, player = fixture()
        player.current = track()
        for value in (-1, 180):
            with self.assertRaises(music.MusicError):
                await Music.seek.callback(cog, ctx, value)
        await Music.seek.callback(cog, ctx, 30)
        player.seek.assert_awaited_once_with(30000)
        player.current = SimpleNamespace(is_stream=True)
        with self.assertRaises(music.MusicError):
            await Music.seek.callback(cog, ctx, 0)

    async def test_stop_resets_both_queues_and_respects_stay(self):
        for stay in (False, True):
            cog, ctx, player = fixture()
            player.queue.put(track())
            player.auto_queue.put(track())
            player.autoplay = wavelink.AutoPlayMode.enabled
            with patch.object(cog, 'stays', new=AsyncMock(return_value=stay)):
                await cog.end_session(ctx.guild, player)
            self.assertEqual(len(player.queue), 0)
            self.assertEqual(len(player.auto_queue), 0)
            self.assertEqual(player.autoplay, wavelink.AutoPlayMode.partial)
            self.assertEqual(player.disconnect.await_count, int(not stay))
            self.assertEqual(player.skip.await_count, int(stay))

    async def test_idle_disconnect_is_disabled_only_by_saved_stay(self):
        cog, ctx, player = fixture()
        events = Wavelink(SimpleNamespace(get_cog=lambda _: cog))
        for stay in (True, False):
            with patch.object(cog, 'stays', new=AsyncMock(return_value=stay)):
                await cog.apply_stay(player)
                self.assertEqual(player.inactive_timeout, None if stay else 180)
                await events.on_wavelink_inactive_player(player)
        player.disconnect.assert_awaited_once()

    async def test_internal_wavelink_advances_queue_without_manual_track_end_handler(self):
        cog, ctx, player = fixture()
        player.channel.members = [SimpleNamespace(bot=False)]
        player._inactive_channel_count = 3
        player._inactive_channel_limit = 3
        player._error_count = 0
        player._autoplay = wavelink.AutoPlayMode.partial
        player.node = SimpleNamespace(status=wavelink.NodeStatus.CONNECTED)
        player._do_partial = AsyncMock()
        await wavelink.Player._auto_play_event(player, SimpleNamespace(reason='finished'))
        player._do_partial.assert_awaited_once()
        self.assertFalse(hasattr(Wavelink, 'on_wavelink_track_end'))

    async def test_controls_check_blacklist_and_voice_and_serialize_v2(self):
        cog, ctx, player = fixture()
        cog.bot.get_context = AsyncMock(return_value=ctx)
        view = MusicControls(cog, 1)
        self.addCleanup(view.stop)
        interaction = SimpleNamespace(guild=ctx.guild, response=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(cog, 'cog_check', new=AsyncMock(return_value=False)):
            self.assertFalse(await view.interaction_check(interaction))
        with patch.object(cog, 'cog_check', new=AsyncMock(return_value=True)):
            ctx.author.voice = None
            self.assertFalse(await view.interaction_check(interaction))
        payload = message_payload({'content': 'Now playing', 'components': view.to_components()})
        self.assertTrue(payload['flags'] & 32768)
        self.assertNotIn('accent_color', str(payload))

    async def test_track_start_keeps_full_bounded_history_for_large_queue_repeat(self):
        cog, ctx, player = fixture()
        player.queue.history.put([track(str(i)) for i in range(510)])
        player.auto_queue.history.put([track(str(i)) for i in range(510)])
        events = Wavelink(SimpleNamespace(get_cog=lambda _: cog))
        await events.on_wavelink_track_start(SimpleNamespace(player=player))
        self.assertEqual(len(player.queue.history), music.MAX_QUEUE)
        self.assertEqual(player.queue.history[0].title, '10')
        self.assertEqual(len(player.auto_queue.history), music.MAX_QUEUE)

    async def test_controller_edit_replaces_old_native_layout_and_rebinds_buttons(self):
        cog, ctx, player = fixture()
        player.current = track('New song')
        player.paused = False
        player.position = 1000
        player.volume = 80
        old_message = SimpleNamespace(id=88, channel=ctx.channel, edit=AsyncMock())
        ctx.channel.fetch_message = AsyncMock(return_value=old_message)
        ctx.guild.get_channel = lambda _: ctx.channel
        cog.bot.cache.music = {'1': {'id': 1, 'music_setup_channel_id': 10, 'music_setup_message_id': 88}}
        previous = MusicControls(cog, 1, 'Old song')
        cog.views[1] = previous
        await Music.send_music_controls(cog, ctx.guild)
        new_view = old_message.edit.await_args.kwargs['view']
        self.addCleanup(new_view.stop)
        self.assertTrue(previous.is_finished())
        self.assertIn('New song', str(new_view.to_components()))
        payload = message_payload({'components': new_view.to_components(), 'content': None, 'embeds': []},
                                  previous={'components': previous.to_components()})
        self.assertNotIn('Old song', str(payload))
        self.assertEqual(payload['components'][0]['type'], 17)

    async def test_admin_music_operations_reject_non_managers(self):
        cog, ctx, player = fixture()
        for command in (Music.stay, Music.music_setup, Music.music_reset, Music.music_settings):
            check = next(p for p in command.checks if 'has_guild_permissions' in p.__qualname__)
            with self.assertRaises(commands.MissingPermissions):
                await check(ctx)


@unittest.skipUnless(os.getenv('KUROO_TEST_MONGO_URI'), 'Disposable Mongo required')
class MusicMongoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncIOMotorClient(os.environ['KUROO_TEST_MONGO_URI'], serverSelectionTimeoutMS=3000)
        await self.client.admin.command('ping')
        self.db = self.client['kuroo_test_' + uuid4().hex]
        async def collection(name):
            return self.db[name]
        self.patch = patch.object(music, 'get_collection', side_effect=collection)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        await self.client.drop_database(self.db.name)
        self.client.close()

    async def test_concurrent_favourites_are_unique_bounded_and_personal(self):
        await asyncio.gather(*(music.favourite(1, track()) for _ in range(8)))
        self.assertEqual(len(await music.favourites(1)), 1)
        self.assertEqual(await music.favourites(2), [])
        with patch.object(music, 'MAX_FAVOURITES', 2):
            await music.favourite(1, track('Other', 'bcdefghijkl'))
            with self.assertRaises(music.MusicError):
                await music.favourite(1, track('Third', 'cdefghijklm'))
        self.assertEqual(len(await music.favourites(1)), 2)

    async def test_stay_saved_for_restart_and_leave_disables_it(self):
        cog, ctx, player = fixture()
        ctx.author.guild_permissions = discord.Permissions(manage_guild=True)
        with patch.object(cog, 'connect', new=AsyncMock(return_value=player)):
            await Music.stay.callback(cog, ctx, True)
        fresh, _, _ = fixture()
        self.assertTrue(await fresh.stays(1))
        self.assertFalse(await fresh.stays(2))
        row = await self.db.music_sessions.find_one({'_id': 1})
        self.assertEqual(row['voice_channel'], 20)
        await Music.leave.callback(cog, ctx)
        self.assertFalse(await fresh.stays(1))
        player.disconnect.assert_awaited_once()

    async def test_offline_manager_can_disable_saved_stay_without_voice(self):
        cog, ctx, player = fixture()
        await self.db.music_sessions.insert_one({'_id': 1, 'stay': True, 'voice_channel': 20})
        ctx.guild.voice_client = None
        ctx.author.voice = None
        await Music.stay.callback(cog, ctx, False)
        self.assertFalse(await cog.stays(1))

    async def test_restore_uses_saved_voice_and_text_channels_and_is_idempotent(self):
        cog, ctx, player = fixture()
        await self.db.music_sessions.insert_one({'_id': 1, 'stay': True, 'voice_channel': 20, 'text_channel': 10})
        ctx.guild.voice_client = None
        ctx.guild.me = object()
        voice = MagicMock(spec=discord.VoiceChannel)
        voice.id = 20
        voice.permissions_for.return_value = discord.Permissions(connect=True, speak=True)
        async def connect(**kwargs):
            ctx.guild.voice_client = player
            return player
        voice.connect = AsyncMock(side_effect=connect)
        ctx.guild.get_channel = lambda channel_id: voice if channel_id == 20 else None
        cog.bot.get_guild = lambda guild_id: ctx.guild if guild_id == 1 else None
        await cog.restore_stay()
        await cog.restore_stay()
        voice.connect.assert_awaited_once()
        self.assertEqual(cog.channels[1], 10)
        self.assertIsNone(player.inactive_timeout)
        self.assertEqual(player.autoplay, wavelink.AutoPlayMode.partial)
        cog.send_music_controls.assert_awaited_once_with(ctx.guild)

    async def test_leave_cannot_disable_managed_stay_as_regular_member(self):
        cog, ctx, player = fixture()
        await self.db.music_sessions.insert_one({'_id': 1, 'stay': True})
        with self.assertRaises(music.MusicError):
            await Music.leave.callback(cog, ctx)
        player.disconnect.assert_not_awaited()
        self.assertTrue(await cog.stays(1))
