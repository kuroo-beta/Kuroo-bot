"""Akeno-inspired music policies implemented for Kuroo's Wavelink runtime."""
from urllib.parse import urlsplit

import wavelink

from kuroo.bridge.storage import get_collection

MAX_QUEUE = 500
MAX_FAVOURITES = 100
SOURCES = {'ytsearch', 'ytmsearch', 'scsearch'}
PROVIDERS = {'youtube.com', 'www.youtube.com', 'music.youtube.com', 'youtu.be',
             'open.spotify.com', 'soundcloud.com', 'www.soundcloud.com',
             'on.soundcloud.com', 'deezer.com', 'www.deezer.com'}
FILTERS = ('reset', 'bassboost', 'nightcore', 'daycore', 'slowed', 'vaporwave',
           'chipmunk', 'karaoke', '8d', 'vibrato')


class MusicError(Exception):
    pass


def search_query(query, source):
    query = query.strip()
    if not query or len(query) > 500:
        raise MusicError('Enter a song name or supported music link (up to 500 characters).')
    try:
        parsed = urlsplit(query)
        port = parsed.port
    except ValueError:
        raise MusicError('That music link is invalid.') from None
    if parsed.scheme or query.startswith('//'):
        if (parsed.scheme != 'https' or parsed.hostname not in PROVIDERS
                or parsed.username or parsed.password or port not in (None, 443)):
            raise MusicError('Use an HTTPS YouTube, Spotify, SoundCloud, or Deezer link.')
        return query
    if source not in SOURCES:
        raise MusicError('MUSIC_SEARCH_SOURCE must be ytsearch, ytmsearch, or scsearch.')
    return f'{source}:{query}'


def make_filter(name):
    filters = wavelink.Filters()
    if name == 'bassboost':
        filters.equalizer.set(bands=[{'band': i, 'gain': gain} for i, gain in enumerate([.25, .2, .15, .1])])
    elif name in ('nightcore', 'daycore', 'slowed', 'vaporwave', 'chipmunk'):
        speed, pitch = {'nightcore': (1.2, 1.2), 'daycore': (.85, .85), 'slowed': (.8, 1),
                        'vaporwave': (.8, .8), 'chipmunk': (1, 1.4)}[name]
        filters.timescale.set(speed=speed, pitch=pitch, rate=1)
    elif name == 'karaoke':
        filters.karaoke.set(level=1, mono_level=1, filter_band=220, filter_width=100)
    elif name == '8d':
        filters.rotation.set(rotation_hz=.2)
    elif name == 'vibrato':
        filters.vibrato.set(frequency=4, depth=.5)
    elif name != 'reset':
        raise MusicError('Choose: ' + ', '.join(FILTERS))
    return filters


def duration(ms):
    seconds = max(0, int(ms)) // 1000
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes}:{seconds:02}'


async def favourite(user_id, track):
    if not track.uri:
        raise MusicError('This track has no link to save.')
    search_query(track.uri, 'ytmsearch')
    coll = await get_collection('music_favourites')
    # One bounded document per user; atomic set semantics prevent duplicate races.
    row = {'uri': track.uri, 'title': track.title[:200]}
    result = await coll.update_one({'_id': user_id, 'tracks.uri': {'$ne': track.uri},
                                   f'tracks.{MAX_FAVOURITES - 1}': {'$exists': False}},
                                  {'$push': {'tracks': row}})
    if result.modified_count:
        return
    existing = await coll.find_one({'_id': user_id})
    if existing is None:
        from pymongo.errors import DuplicateKeyError
        try:
            await coll.insert_one({'_id': user_id, 'tracks': [row]})
            return
        except DuplicateKeyError:
            return await favourite(user_id, track)
    if any(t['uri'] == track.uri for t in existing.get('tracks', [])):
        return
    raise MusicError(f'You can save at most {MAX_FAVOURITES} favourites. Remove one first.')


async def favourites(user_id):
    coll = await get_collection('music_favourites')
    return (await coll.find_one({'_id': user_id}) or {}).get('tracks', [])
