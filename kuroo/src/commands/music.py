"""Akeno-inspired music player, ported into Kuroo's Python command runtime."""
import asyncio
from collections import defaultdict
from types import SimpleNamespace
from typing import Literal

import discord
from discord.ext import commands, tasks
import wavelink

import storage.music
from kuroo.console.logging import logger
from kuroo.src.checks import checks
from kuroo.workflows import music as policy


class MusicControls(discord.ui.LayoutView):
    def __init__(self, cog, guild_id, body='Kuroo Music'):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        container = discord.ui.Container(discord.ui.TextDisplay(body))
        row = discord.ui.ActionRow()
        for name, label in (('pause', 'Pause / Resume'), ('skip', 'Skip'), ('stop', 'Stop'), ('like', 'Favourite')):
            button = discord.ui.Button(label=label, custom_id=f'kuroo:music:{name}')
            async def callback(interaction, action=name):
                await self.action(interaction, action)
            button.callback = callback
            row.add_item(button)
        container.add_item(row)
        self.add_item(container)

    async def interaction_check(self, interaction):
        if not interaction.guild or interaction.guild.id != self.guild_id:
            return False
        ctx = await self.cog.bot.get_context(interaction)
        try:
            if not await self.cog.cog_check(ctx):
                raise policy.MusicError('Music controls are unavailable here.')
            self.cog.player_for(ctx)
        except (policy.MusicError, commands.CheckFailure) as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return False
        return True

    async def action(self, interaction, name):
        await interaction.response.defer(ephemeral=True)
        ctx = await self.cog.bot.get_context(interaction)
        try:
            async with self.cog.locks[self.guild_id]:
                player = self.cog.player_for(ctx)
                if name == 'pause':
                    await player.pause(not player.paused)
                elif name == 'skip':
                    await player.skip(force=True)
                elif name == 'stop':
                    await self.cog.end_session(ctx.guild, player)
                elif name == 'like':
                    if not player.current:
                        raise policy.MusicError('Nothing is playing.')
                    await policy.favourite(interaction.user.id, player.current)
            if name != 'stop':
                await self.cog.send_music_controls(ctx.guild)
            await interaction.followup.send('Favourite saved.' if name == 'like' else 'Player updated.', ephemeral=True)
        except Exception as error:
            await interaction.followup.send(str(error) if isinstance(error, policy.MusicError)
                                            else 'The player could not complete that action.', ephemeral=True)


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.locks = defaultdict(asyncio.Lock)
        self.controller_locks = defaultdict(asyncio.Lock)
        self.channels = {}
        self.messages = {}
        self.views = {}
        self.requests = commands.CooldownMapping.from_cooldown(5, 30, commands.BucketType.user)
        self.restore_lock = asyncio.Lock()
        self.cog_info = SimpleNamespace(name='Music', category='Music', hidden=False,
            description='Play music, manage queues, filters and favourites', emoji='🎵')

    def cog_unload(self):
        self.refresh.cancel()
        for view in self.views.values():
            view.stop()

    async def cog_check(self, ctx):
        return ctx.guild is not None and checks.check_blacklist_predicate(ctx) and checks.check_ignore_predicate(ctx)

    async def cog_command_error(self, ctx, error):
        cause = getattr(error, 'original', error)
        if isinstance(cause, policy.MusicError):
            await ctx.send(str(cause), ephemeral=True)
        elif isinstance(error, (commands.CheckFailure, commands.UserInputError, commands.CommandOnCooldown)):
            raise error
        else:
            logger.warning(f'Music action failed ({type(cause).__name__}).')
            await ctx.send('Music could not complete that action. Check voice permissions and the Lavalink node.', ephemeral=True)

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.refresh.is_running():
            self.refresh.start()

    @tasks.loop(seconds=30)
    async def refresh(self):
        for guild in self.bot.guilds:
            if isinstance(guild.voice_client, wavelink.Player):
                try:
                    await self.send_music_controls(guild)
                except Exception as error:
                    logger.warning(f'Music controller refresh failed ({type(error).__name__}).')

    def player_for(self, ctx):
        channel = getattr(getattr(ctx.author, 'voice', None), 'channel', None)
        if not channel:
            raise policy.MusicError('Join a voice channel first.')
        player = ctx.guild.voice_client
        if not isinstance(player, wavelink.Player):
            raise policy.MusicError('No music session is connected. Use /play first.')
        if player.channel.id != channel.id:
            raise policy.MusicError('Join my voice channel to control this session.')
        return player

    async def connect(self, ctx):
        channel = getattr(getattr(ctx.author, 'voice', None), 'channel', None)
        if not isinstance(channel, discord.VoiceChannel):
            raise policy.MusicError('Join a regular voice channel first.')
        if ctx.guild.voice_client:
            return self.player_for(ctx)
        permissions = channel.permissions_for(ctx.guild.me)
        if not permissions.connect or not permissions.speak:
            raise policy.MusicError('I need Connect and Speak in your voice channel.')
        try:
            player = await channel.connect(cls=wavelink.Player, self_deaf=True)
        except (wavelink.InvalidNodeException, wavelink.NodeException):
            raise policy.MusicError('The music node is unavailable. Ask the host to configure Lavalink v4.') from None
        cfg = self.bot.cache.music.get(str(ctx.guild.id), {})
        player.autoplay = wavelink.AutoPlayMode.enabled if cfg.get('default_autoplay') else wavelink.AutoPlayMode.partial
        player.queue.mode = wavelink.QueueMode.loop if cfg.get('default_repeat') else wavelink.QueueMode.normal
        await player.set_volume(self.default_volume(ctx.guild.id, cfg))
        await self.apply_stay(player)
        return player

    def default_volume(self, guild_id, cfg):
        premium = self.bot.cache.guilds.get(str(guild_id), {}).get('subscription', 'free') != 'free'
        return cfg.get('default_volume', 80) if premium else 80

    def allow_links(self, ctx):
        return (self.bot.cache.users.get(str(ctx.author.id), {}).get('no_prefix_subscription')
                or self.bot.cache.guilds.get(str(ctx.guild.id), {}).get('subscription', 'free') != 'free')

    async def stays(self, guild_id):
        coll = await policy.get_collection('music_sessions')
        return bool((await coll.find_one({'_id': guild_id}) or {}).get('stay'))

    async def apply_stay(self, player):
        enabled = await self.stays(player.guild.id)
        player.inactive_timeout = None if enabled else 180
        player.inactive_channel_tokens = None if enabled else 3

    async def restore_stay(self):
        async with self.restore_lock:
            coll = await policy.get_collection('music_sessions')
            async for row in coll.find({'stay': True}):
                guild = self.bot.get_guild(row['_id'])
                channel = guild.get_channel(row.get('voice_channel', 0)) if guild else None
                if not guild or guild.voice_client or not isinstance(channel, discord.VoiceChannel):
                    continue
                perms = channel.permissions_for(guild.me)
                if not perms.connect or not perms.speak:
                    continue
                try:
                    player = await channel.connect(cls=wavelink.Player, self_deaf=True)
                    cfg = self.bot.cache.music.get(str(guild.id), {})
                    player.autoplay = wavelink.AutoPlayMode.enabled if cfg.get('default_autoplay') else wavelink.AutoPlayMode.partial
                    player.queue.mode = wavelink.QueueMode.loop if cfg.get('default_repeat') else wavelink.QueueMode.normal
                    await player.set_volume(self.default_volume(guild.id, cfg))
                    await self.apply_stay(player)
                    if row.get('text_channel'):
                        self.channels[guild.id] = row['text_channel']
                    await self.send_music_controls(guild)
                except Exception as error:
                    logger.warning(f'Could not restore 24/7 music connection ({type(error).__name__}).')

    async def enqueue(self, ctx, query):
        # Check voice access before the provider search, then recheck inside the connection lock.
        channel = getattr(getattr(ctx.author, 'voice', None), 'channel', None)
        if not channel:
            raise policy.MusicError('Join a voice channel first.')
        if ctx.guild.voice_client:
            self.player_for(ctx)
        query = policy.search_query(query, self.bot.BotConfig.MUSIC_SEARCH_SOURCE)
        if query.startswith('https://') and not self.allow_links(ctx):
            raise policy.MusicError('Music links and playlists require server premium or a user no-prefix subscription. Song-name searches are available to everyone.')
        result = await wavelink.Playable.search(query, source=None)
        tracks = list(result.tracks) if isinstance(result, wavelink.Playlist) else list(result[:1])
        if not tracks:
            raise policy.MusicError('No tracks found. Try another name or supported link.')
        async with self.locks[ctx.guild.id]:
            player = await self.connect(ctx)
            if len(tracks) + len(player.queue) > policy.MAX_QUEUE:
                raise policy.MusicError(f'The queue can hold at most {policy.MAX_QUEUE} tracks. Try a smaller playlist.')
            self.channels[ctx.guild.id] = ctx.channel.id
            for track in tracks:
                track.extras = {'requester_id': ctx.author.id}
            await player.queue.put_wait(tracks)
            if not player.current:
                await player.play(player.queue.get())
        await self.send_music_controls(ctx.guild)
        return len(tracks)

    async def end_session(self, guild, player):
        player.autoplay = wavelink.AutoPlayMode.partial
        player.queue.reset()
        player.auto_queue.reset()
        if await self.stays(guild.id):
            await player.skip(force=True)
        else:
            await player.disconnect()
        await self.send_music_controls(guild, end=True)

    async def send_music_controls(self, guild, update_attachments=False, end=False):
        async with self.controller_locks[guild.id]:
            cfg = self.bot.cache.music.get(str(guild.id), {})
            channel_id = cfg.get('music_setup_channel_id') or self.channels.get(guild.id)
            channel = guild.get_channel(channel_id) if channel_id else None
            if not channel:
                return
            player = guild.voice_client
            track = getattr(player, 'current', None) if not end else None
            if track:
                title = discord.utils.escape_markdown(track.title[:150])
                body = (f'## Kuroo Music\n**{title}** — {discord.utils.escape_markdown(track.author[:100])}\n'
                        f'{"Paused" if player.paused else "Playing"} • {policy.duration(player.position)} / '
                        f'{"LIVE" if track.is_stream else policy.duration(track.length)}\n'
                        f'Volume: {player.volume}% • Queue: {len(player.queue)} • Repeat: {player.queue.mode.name}\n'
                        f'Autoplay: {"on" if player.autoplay == wavelink.AutoPlayMode.enabled else "off"}')
            else:
                body = '## Kuroo Music\nNothing is playing. Use /play or send a song name in the configured music channel.'
            view = MusicControls(self, guild.id, body) if isinstance(player, wavelink.Player) else discord.ui.LayoutView(timeout=180)
            if not isinstance(player, wavelink.Player):
                view.add_item(discord.ui.Container(discord.ui.TextDisplay(body)))
            old_view = self.views.pop(guild.id, None)
            if old_view:
                old_view.stop()
            message = self.messages.get(guild.id)
            saved_id = cfg.get('music_setup_message_id')
            if saved_id and (not message or message.id != saved_id):
                try:
                    message = await channel.fetch_message(saved_id)
                except discord.NotFound:
                    message = None
            try:
                if message and message.channel.id == channel.id:
                    await message.edit(content=None, embeds=[], view=view, attachments=[], allowed_mentions=discord.AllowedMentions.none())
                else:
                    message = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
                    if cfg.get('music_setup_channel_id'):
                        await storage.music.update(id=cfg['id'], music_setup_message_id=message.id)
            except discord.NotFound:
                self.messages.pop(guild.id, None)
                if cfg.get('music_setup_channel_id'):
                    await storage.music.update(id=cfg['id'], music_setup_message_id=0)
                return
            if view:
                self.views[guild.id] = view
            self.messages[guild.id] = message

    async def music_setup_function(self, message):
        if message.author.bot or not message.guild:
            return
        ctx = await self.bot.get_context(message)
        # The main message event calls this hook; commands must not also become searches.
        if ctx.valid or message.content.startswith('/') or not message.content.strip():
            return
        if not await self.cog_check(ctx):
            return
        if self.requests.get_bucket(message).update_rate_limit():
            return
        try:
            await self.enqueue(ctx, message.content)
        except policy.MusicError as error:
            await message.channel.send(str(error), allowed_mentions=discord.AllowedMentions.none(), delete_after=15)
        except Exception as error:
            logger.warning(f'Music channel request failed ({type(error).__name__}).')
            await message.channel.send('Music is unavailable. Check the Lavalink node and voice permissions.', delete_after=15)

    @commands.hybrid_command(name='play', aliases=['p'], help='Play a song or add a supported playlist URL')
    @commands.guild_only()
    @commands.cooldown(5, 30, commands.BucketType.user)
    async def play(self, ctx, *, search: str):
        await ctx.defer()
        count = await self.enqueue(ctx, search)
        await ctx.send(f'Added {count} track(s) to the session.', ephemeral=True)

    @commands.hybrid_command(name='pause', help='Pause playback')
    async def pause(self, ctx):
        await self.player_for(ctx).pause(True)
        await self.send_music_controls(ctx.guild)
        await ctx.send('Paused.', ephemeral=True)

    @commands.hybrid_command(name='resume', help='Resume playback')
    async def resume(self, ctx):
        await self.player_for(ctx).pause(False)
        await self.send_music_controls(ctx.guild)
        await ctx.send('Resumed.', ephemeral=True)

    @commands.command(name='skip', help='Skip the current track; slash equivalent: /music skip')
    async def skip(self, ctx):
        await self.player_for(ctx).skip(force=True)
        await ctx.send('Skipped.', ephemeral=True)

    @commands.hybrid_command(name='stop', help='Clear the session and stop playback')
    async def stop(self, ctx):
        async with self.locks[ctx.guild.id]:
            await self.end_session(ctx.guild, self.player_for(ctx))
        await ctx.send('Playback stopped and queue cleared.', ephemeral=True)

    @commands.hybrid_command(name='current', aliases=['nowplaying'], help='Show the current music session')
    async def current(self, ctx):
        self.player_for(ctx)
        self.channels[ctx.guild.id] = ctx.channel.id
        await self.send_music_controls(ctx.guild)
        await ctx.send('Player refreshed.', ephemeral=True)

    @commands.hybrid_command(name='volume', help='Show or change volume from 0 to 100')
    async def volume(self, ctx, volume: int = None):
        player = self.player_for(ctx)
        if volume is not None:
            if not 0 <= volume <= 100:
                raise policy.MusicError('Volume must be between 0 and 100.')
            await player.set_volume(volume)
        await ctx.send(f'Volume: {player.volume}%.', ephemeral=True)

    @commands.hybrid_command(name='loop', help='Set repeat mode: off, track, or queue')
    async def loop(self, ctx, mode: Literal['off', 'track', 'queue'] = 'track'):
        player = self.player_for(ctx)
        player.queue.mode = {'off': wavelink.QueueMode.normal, 'track': wavelink.QueueMode.loop,
                             'queue': wavelink.QueueMode.loop_all}[mode]
        await ctx.send(f'Repeat: {mode}.', ephemeral=True)

    @commands.hybrid_command(name='autoplay', help='Toggle related-song recommendations when the queue ends')
    async def autoplay(self, ctx):
        player = self.player_for(ctx)
        enabled = player.autoplay != wavelink.AutoPlayMode.enabled
        player.autoplay = wavelink.AutoPlayMode.enabled if enabled else wavelink.AutoPlayMode.partial
        await ctx.send(f'Autoplay {"enabled" if enabled else "disabled"}.', ephemeral=True)

    @commands.hybrid_command(name='queue', aliases=['q'], help='Show ten queued tracks per page')
    async def queue(self, ctx, page: int = 1):
        player = self.player_for(ctx)
        if page < 1 or (page - 1) * 10 >= max(1, len(player.queue)):
            raise policy.MusicError('That queue page does not exist.')
        start = (page - 1) * 10
        body = '\n'.join(f'{i+start+1}. {discord.utils.escape_markdown(t.title[:100])}'
                         for i, t in enumerate(player.queue[start:start+10]))
        await ctx.send(body or 'The queue is empty.', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_group(name='music', invoke_without_command=True, help='Music controls, favourites and server settings')
    @commands.guild_only()
    async def music_group(self, ctx):
        await ctx.send('Use /play and /queue. More controls: /music skip, seek, filter, shuffle, remove, move, '
                       'skipto, clear, history, previous, replay, like, unlike, favourites, playliked, stay, join, leave, setup, reset, settings.', ephemeral=True)

    @music_group.command(name='skip', help='Skip the current track')
    async def music_skip(self, ctx):
        await self.skip(ctx)

    @music_group.command(name='join', help='Connect to your voice channel')
    async def join(self, ctx):
        async with self.locks[ctx.guild.id]:
            await self.connect(ctx)
        await ctx.send('Connected.', ephemeral=True)

    @music_group.command(name='leave', help='Disconnect and disable 24/7 for this server')
    async def leave(self, ctx):
        async with self.locks[ctx.guild.id]:
            player = self.player_for(ctx)
            if await self.stays(ctx.guild.id) and not ctx.author.guild_permissions.manage_guild:
                raise policy.MusicError('Manage Server is required to end a 24/7 connection.')
            coll = await policy.get_collection('music_sessions')
            await coll.update_one({'_id': ctx.guild.id}, {'$set': {'stay': False}})
            await player.disconnect()
        await self.send_music_controls(ctx.guild, end=True)
        await ctx.send('Disconnected.', ephemeral=True)

    @music_group.command(name='seek', help='Seek to an absolute time in seconds')
    async def seek(self, ctx, seconds: int):
        player = self.player_for(ctx)
        if not player.current or player.current.is_stream or not 0 <= seconds * 1000 < player.current.length:
            raise policy.MusicError('Choose a time within the current non-live track.')
        await player.seek(seconds * 1000)
        await ctx.send(f'Seeked to {policy.duration(seconds * 1000)}.', ephemeral=True)

    @music_group.command(name='filter', help='Apply a sound preset or reset all filters')
    async def filter(self, ctx, preset: Literal['reset', 'bassboost', 'nightcore', 'daycore', 'slowed', 'vaporwave', 'chipmunk', 'karaoke', '8d', 'vibrato']):
        await self.player_for(ctx).set_filters(policy.make_filter(preset), seek=True)
        await ctx.send(f'Filter: {preset}.', ephemeral=True)

    @music_group.command(name='shuffle', help='Shuffle upcoming tracks')
    async def shuffle(self, ctx):
        self.player_for(ctx).queue.shuffle()
        await ctx.send('Queue shuffled.', ephemeral=True)

    @music_group.command(name='clear', help='Clear upcoming tracks without stopping the current song')
    async def clear(self, ctx):
        self.player_for(ctx).queue.clear()
        await ctx.send('Upcoming tracks cleared.', ephemeral=True)

    def queue_index(self, player, position):
        if not 1 <= position <= len(player.queue):
            raise policy.MusicError('Choose a queued position shown by /queue.')
        return position - 1

    @music_group.command(name='remove', help='Remove a queued track by position')
    async def remove(self, ctx, position: int):
        player = self.player_for(ctx)
        player.queue.delete(self.queue_index(player, position))
        await ctx.send('Track removed.', ephemeral=True)

    @music_group.command(name='move', help='Move a queued track to another queue position')
    async def move(self, ctx, position: int, destination: int):
        player = self.player_for(ctx)
        start, end = self.queue_index(player, position), self.queue_index(player, destination)
        track = player.queue[start]
        player.queue.delete(start)
        player.queue.put_at(end, track)
        await ctx.send('Track moved.', ephemeral=True)

    @music_group.command(name='skipto', help='Discard earlier queued tracks and play the chosen position next')
    async def skipto(self, ctx, position: int):
        player = self.player_for(ctx)
        index = self.queue_index(player, position)
        for _ in range(index):
            player.queue.delete(0)
        await player.skip(force=True)
        await ctx.send('Skipped to the selected queue position.', ephemeral=True)

    @music_group.command(name='history', help='Show the ten most recently played songs')
    async def history(self, ctx):
        player = self.player_for(ctx)
        tracks = list(player.queue.history or [])[-10:]
        await ctx.send('\n'.join(discord.utils.escape_markdown(t.title[:100]) for t in reversed(tracks))
                       or 'No playback history.', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @music_group.command(name='previous', help='Replay the preceding song and queue the current one next')
    async def previous(self, ctx):
        async with self.locks[ctx.guild.id]:
            player = self.player_for(ctx)
            tracks = list(player.queue.history or [])
            if len(tracks) < 2:
                raise policy.MusicError('There is no previous track in this session.')
            if len(player.queue) >= policy.MAX_QUEUE:
                raise policy.MusicError('The queue is full.')
            if player.current:
                player.queue.put_at(0, player.current)
            await player.play(tracks[-2])
        await ctx.send('Playing the previous song.', ephemeral=True)

    @music_group.command(name='replay', help='Restart the current song')
    async def replay(self, ctx):
        await self.seek(ctx, 0)

    @music_group.command(name='like', help='Save the current track to your personal favourites')
    async def like(self, ctx):
        track = self.player_for(ctx).current
        if not track:
            raise policy.MusicError('Nothing is playing.')
        await policy.favourite(ctx.author.id, track)
        await ctx.send('Favourite saved.', ephemeral=True)

    @music_group.command(name='favourites', help='List your personal favourites, ten per page')
    async def favourites(self, ctx, page: int = 1):
        tracks = await policy.favourites(ctx.author.id)
        start = (page - 1) * 10
        if page < 1 or start >= max(1, len(tracks)):
            raise policy.MusicError('That favourites page does not exist.')
        await ctx.send('\n'.join(f'{i+start+1}. {discord.utils.escape_markdown(t["title"][:100])}'
                       for i, t in enumerate(tracks[start:start+10])) or 'No favourites saved.',
                       ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @music_group.command(name='unlike', help='Remove a favourite using its displayed position')
    async def unlike(self, ctx, position: int):
        tracks = await policy.favourites(ctx.author.id)
        if not 1 <= position <= len(tracks):
            raise policy.MusicError('Choose a position shown by /music favourites.')
        coll = await policy.get_collection('music_favourites')
        await coll.update_one({'_id': ctx.author.id}, {'$pull': {'tracks': {'uri': tracks[position-1]['uri']}}})
        await ctx.send('Favourite removed.', ephemeral=True)

    @music_group.command(name='playliked', help='Play a saved favourite by its displayed position')
    async def playliked(self, ctx, position: int):
        await ctx.defer()
        tracks = await policy.favourites(ctx.author.id)
        if not 1 <= position <= len(tracks):
            raise policy.MusicError('Choose a position shown by /music favourites.')
        await self.enqueue(ctx, tracks[position-1]['uri'])
        await ctx.send('Favourite added to the queue.', ephemeral=True)

    @music_group.command(name='stay', help='Enable or disable a persistent 24/7 voice connection')
    @commands.has_guild_permissions(manage_guild=True)
    async def stay(self, ctx, enabled: bool):
        async with self.locks[ctx.guild.id]:
            player = await self.connect(ctx) if enabled else ctx.guild.voice_client
            values = {'stay': enabled}
            if enabled:
                values.update(voice_channel=player.channel.id, text_channel=ctx.channel.id)
            coll = await policy.get_collection('music_sessions')
            await coll.update_one({'_id': ctx.guild.id}, {'$set': values}, upsert=True)
            if isinstance(player, wavelink.Player):
                await self.apply_stay(player)
        await ctx.send(f'24/7 {"enabled" if enabled else "disabled"}. Queue and playback position are not saved across restarts.', ephemeral=True)

    @music_group.command(name='setup', help='Create a dedicated song request channel')
    @commands.has_guild_permissions(manage_guild=True)
    @commands.max_concurrency(1, commands.BucketType.guild)
    async def music_setup(self, ctx):
        await ctx.defer(ephemeral=True)
        cfg = self.bot.cache.music.get(str(ctx.guild.id)) or await storage.music.insert(guild_id=ctx.guild.id)
        if cfg.get('music_setup_channel_id'):
            raise policy.MusicError('A music request channel already exists. Use /music reset first.')
        channel = await ctx.guild.create_text_channel('🎸-music-channel', reason='Music request channel setup')
        await storage.music.update(id=cfg['id'], music_setup_channel_id=channel.id)
        await self.send_music_controls(ctx.guild, end=True)
        await ctx.send(f'Music request channel created: {channel.mention}', ephemeral=True)

    @music_group.command(name='reset', help='Remove the dedicated music request channel')
    @commands.has_guild_permissions(manage_guild=True)
    @commands.max_concurrency(1, commands.BucketType.guild)
    async def music_reset(self, ctx):
        await ctx.defer(ephemeral=True)
        cfg = self.bot.cache.music.get(str(ctx.guild.id), {})
        channel = ctx.guild.get_channel(cfg.get('music_setup_channel_id', 0))
        if channel:
            await channel.delete(reason='Music setup reset by server manager')
        if cfg:
            await storage.music.update(id=cfg['id'], music_setup_channel_id=0, music_setup_message_id=0)
        self.messages.pop(ctx.guild.id, None)
        await ctx.send('Music request channel reset.', ephemeral=True)

    @music_group.command(name='settings', help='Show or change defaults used when a new music session connects')
    @commands.has_guild_permissions(manage_guild=True)
    async def music_settings(self, ctx, volume: int = None, repeat: bool = None, autoplay: bool = None):
        cfg = self.bot.cache.music.get(str(ctx.guild.id)) or await storage.music.insert(guild_id=ctx.guild.id)
        if volume is not None and not 0 <= volume <= 100:
            raise policy.MusicError('Volume must be between 0 and 100.')
        if volume is not None and self.bot.cache.guilds.get(str(ctx.guild.id), {}).get('subscription', 'free') == 'free':
            raise policy.MusicError('Changing the default volume requires server premium.')
        values = {key: value for key, value in {'default_volume': volume, 'default_repeat': repeat,
                                                'default_autoplay': autoplay}.items() if value is not None}
        if values:
            await storage.music.update(id=cfg['id'], **values)
        cfg = cfg | values
        await ctx.send(f'Default volume: {cfg.get("default_volume", 80)}%\nRepeat: {cfg.get("default_repeat", False)}\n'
                       f'Autoplay: {cfg.get("default_autoplay", False)}\nRequest channel: {cfg.get("music_setup_channel_id") or "not set"}', ephemeral=True)
