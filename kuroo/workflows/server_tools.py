"""Invite evidence, persistent staff dashboard state, and creator feeds."""
import asyncio
from collections import defaultdict
import re
import time
import xml.etree.ElementTree as ET

import aiohttp

from kuroo.bridge.storage import get_collection


class ToolsError(Exception):
    pass


class InviteTracker:
    def __init__(self):
        self.snapshots = {}
        self.locks = defaultdict(asyncio.Lock)

    async def snapshot(self, guild):
        try:
            invites = await guild.invites()
            return {invite.code: (invite.uses or 0, invite.inviter.id if invite.inviter else None)
                    for invite in invites}
        except Exception:
            return None

    async def prime(self, guild):
        async with self.locks[guild.id]:
            self.snapshots[guild.id] = await self.snapshot(guild)

    async def joined(self, member):
        async with self.locks[member.guild.id]:
            old = self.snapshots.get(member.guild.id)
            new = await self.snapshot(member.guild)
            self.snapshots[member.guild.id] = new
            source = {'inviter_id': None, 'code': None, 'reason': 'Invite access or baseline unavailable'}
            if old is not None and new is not None:
                changed = [(code, uses - old[code][0], inviter) for code, (uses, inviter) in new.items()
                           if code in old and uses > old[code][0]]
                if (set(new) == set(old) and len(changed) == 1 and changed[0][1] == 1
                        and changed[0][2] is not None
                        and all(new[code][0] >= old[code][0] and new[code][1] == old[code][1] for code in old)):
                    source = {'inviter_id': changed[0][2], 'code': changed[0][0], 'reason': 'Single invite-use increase'}
                else:
                    source['reason'] = 'Ambiguous, deleted, vanity, or unobserved invite'
            coll = await get_collection('invite_events')
            joined_at = member.joined_at.isoformat() if member.joined_at else str(time.time())
            await coll.update_one({'_id': f'{member.guild.id}:{member.id}:{joined_at}'}, {'$setOnInsert': {
                'guild_id': member.guild.id, 'user_id': member.id, 'joined_at': joined_at,
                'left': False, **source}}, upsert=True)
            return source

    async def left(self, member):
        coll = await get_collection('invite_events')
        row = await coll.find_one({'guild_id': member.guild.id, 'user_id': member.id}, sort=[('joined_at', -1)])
        if row:
            await coll.update_one({'_id': row['_id']}, {'$set': {'left': True}})


async def tool_settings(guild_id):
    coll = await get_collection('server_tools')
    return {'tracking': False, 'dashboard_channel': 0, 'dashboard_message': 0} | (await coll.find_one({'_id': guild_id}) or {})


async def configure(guild_id, **values):
    coll = await get_collection('server_tools')
    await coll.update_one({'_id': guild_id}, {'$set': values}, upsert=True)


class CreatorFeeds:
    """Fixed provider hosts; no arbitrary URL fetching from command input."""
    def __init__(self, client_id='', client_secret=''):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.token_until = 0

    async def latest(self, provider, identity):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            if provider == 'youtube':
                if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', identity):
                    raise ToolsError('Use a YouTube channel ID beginning with UC (24 characters).')
                async with session.get('https://www.youtube.com/feeds/videos.xml', params={'channel_id': identity}, allow_redirects=False) as response:
                    response.raise_for_status()
                    raw = await response.content.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ToolsError('The creator feed was too large.')
                root = ET.fromstring(raw)
                ns = {'a': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}
                entry = root.find('a:entry', ns)
                if entry is None:
                    return None
                video_id = entry.findtext('yt:videoId', '', ns)
                if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
                    raise ToolsError('The creator feed returned an invalid video ID.')
                return {'id': video_id, 'title': entry.findtext('a:title', 'New video', ns)[:200],
                        'url': f'https://www.youtube.com/watch?v={video_id}'}
            if provider != 'twitch' or not re.fullmatch(r'[a-zA-Z0-9_]{1,25}', identity):
                raise ToolsError('Use a Twitch channel login containing letters, numbers, or underscores.')
            if not self.client_id or not self.client_secret:
                raise ToolsError('The host must configure TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET.')
            if time.time() >= self.token_until:
                async with session.post('https://id.twitch.tv/oauth2/token', data={
                        'client_id': self.client_id, 'client_secret': self.client_secret,
                        'grant_type': 'client_credentials'}, allow_redirects=False) as response:
                    response.raise_for_status()
                    data = await response.json()
                self.token = data['access_token']
                self.token_until = time.time() + max(0, data['expires_in'] - 60)
            async with session.get('https://api.twitch.tv/helix/streams', params={'user_login': identity},
                    headers={'Client-ID': self.client_id, 'Authorization': f'Bearer {self.token}'}, allow_redirects=False) as response:
                if response.status == 401:
                    self.token_until = 0
                response.raise_for_status()
                data = await response.json()
            if not data.get('data'):
                return None
            stream = data['data'][0]
            return {'id': str(stream['id']), 'title': stream.get('title', 'Live on Twitch')[:200],
                    'url': f'https://www.twitch.tv/{identity.lower()}'}
