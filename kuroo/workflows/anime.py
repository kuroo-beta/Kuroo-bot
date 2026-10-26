"""Anime reaction GIFs from the fixed nekos.best reaction catalog."""
import asyncio
from dataclasses import dataclass
from urllib.parse import urlsplit

import aiohttp

REACTIONS = {'slap': ('slaps', True),
 'hug': ('hugs', True),
 'kiss': ('kisses', True),
 'pat': ('pats', True),
 'cry': ('is crying', False),
 'dance': ('is dancing', False),
 'laugh': ('is laughing', False),
 'smile': ('is smiling', False),
 'angry': ('is angry at', True),
 'confused': ('looks confused', False),
 'sleep': ('is sleeping', False),
 'cuddle': ('cuddles', True),
 'poke': ('pokes', True),
 'highfive': ('high-fives', True),
 'wave': ('waves to', True),
 'blush': ('is blushing', False),
 'wink': ('winks at', True),
 'smug': ('looks smug', False),
 'facepalm': ('facepalms', False),
 'pout': ('is pouting', False),
 'happy': ('looks happy', False),
 'nod': ('nods', False),
 'thumbsup': ('gives a thumbs up to', True),
 'cheer': ('cheers for', True)}


@dataclass(frozen=True)
class AnimeGif:
    url: str
    anime: str


async def get_anime_gif(action: str) -> AnimeGif | None:
    if action not in REACTIONS:
        raise ValueError("Unknown anime reaction")
    endpoint = "clap" if action == "cheer" else action
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(f"https://nekos.best/api/v2/{endpoint}") as response:
                if response.status != 200:
                    return None
                data = await response.json()
        result = data["results"][0]
        url, anime = result["url"], result["anime_name"]
        if not isinstance(url, str):
            return None
        parsed = urlsplit(url)
        # Never fall back to general GIF search or accept a non-anime result.
        if (parsed.scheme != "https" or parsed.netloc != "nekos.best"
                or not parsed.path.startswith(f"/api/v2/{endpoint}/")
                or not parsed.path.endswith(".gif") or not isinstance(anime, str) or not anime.strip()):
            return None
        return AnimeGif(url=url, anime=anime)
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, IndexError, TypeError, ValueError):
        return None
