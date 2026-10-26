"""Per-server bot name styling using the fields described by the reference guide.

These fields are not part of Discord's published API contract. A successful HTTP
request alone does not prove Discord accepted them, so verify the returned values.
"""
from enum import Enum

from discord.http import Route


class NameStyleResult(Enum):
    APPLIED = 'applied'
    UNCONFIRMED = 'unconfirmed'


def name_style_payload(config):
    font = int(config.NAME_STYLE_FONT_ID)
    effect = int(config.NAME_STYLE_EFFECT_ID)
    if font not in {1, 3, 4, 6, 7, 8, 10, 11} or effect not in {1, 2, 3, 4, 5, 6}:
        raise ValueError('Unsupported name style font or effect ID')
    colors = []
    for value in config.NAME_STYLE_COLORS.split(','):
        value = value.strip().removeprefix('#')
        if len(value) != 6 or any(char not in '0123456789abcdefABCDEF' for char in value):
            raise ValueError('Name style colors must be six-digit hexadecimal values')
        colors.append(int(value, 16))
    if len(colors) != (2 if effect == 2 else 1):
        raise ValueError('Gradient requires two colors; other effects require one')
    return {
        'display_name_font_id': font,
        'display_name_effect_id': effect,
        'display_name_colors': colors,
    }


async def apply_name_style(bot, guild):
    payload = name_style_payload(bot.BotConfig)
    route = Route('PATCH', '/guilds/{guild_id}/members/@me', guild_id=guild.id)
    result = await bot.http.request(route, json=payload)
    if isinstance(result, dict) and all(result.get(key) == value for key, value in payload.items()):
        return NameStyleResult.APPLIED
    return NameStyleResult.UNCONFIRMED
