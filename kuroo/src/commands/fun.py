"""Anime-only fun commands, with prefix shortcuts and one slash entry point."""
from typing import Literal

import discord
from discord.ext import commands

from kuroo.src.checks import checks
from kuroo.style import color
from kuroo.workflows.anime import REACTIONS, get_anime_gif

Reaction = Literal[
    'slap', 'hug', 'kiss', 'pat', 'cry', 'dance',
    'laugh', 'smile', 'angry', 'confused', 'sleep', 'cuddle',
    'poke', 'highfive', 'wave', 'blush', 'wink', 'smug',
    'facepalm', 'pout', 'happy', 'nod', 'thumbsup', 'cheer',
]


class Fun(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cog_info = type("cog_info", (), {
            "name": "Anime Fun", "category": "Extra", "description": "Anime reaction commands",
            "hidden": False, "emoji": self.bot.emoji.FUN or "🎉",
        })

    async def send_reaction(self, ctx: commands.Context, action: str, user: discord.User = None):
        if action not in REACTIONS:
            await ctx.send("Choose an anime reaction: " + ", ".join(REACTIONS))
            return
        await ctx.defer()
        image = await get_anime_gif(action)
        if image is None:
            await ctx.send("Anime GIFs are unavailable right now. Please try again shortly.",
                           allowed_mentions=discord.AllowedMentions.none())
            return
        verb, targeted = REACTIONS[action]
        target = user or ctx.author
        target_name = "themselves" if target.id == ctx.author.id else target.display_name
        title = f"{ctx.author.display_name} {verb}"
        if targeted:
            title += f" {target_name}"
        embed = discord.Embed(title=title[:256], color=color.random_color())
        embed.set_image(url=image.url)
        embed.set_footer(text=f"Anime: {image.anime} • Requested by {ctx.author.display_name}"[:2048])
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="anime", help="Choose an anime reaction GIF; all reactions are anime only")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def anime(self, ctx: commands.Context, action: Reaction = "hug", user: discord.User = None):
        await self.send_reaction(ctx, action, user)

    @commands.command(name="slap", help="Anime slap reaction (also /anime slap)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def slap(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "slap", user)

    @commands.command(name="hug", help="Anime hug reaction (also /anime hug)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def hug(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "hug", user)

    @commands.command(name="kiss", help="Anime kiss reaction (also /anime kiss)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def kiss(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "kiss", user)

    @commands.command(name="pat", help="Anime pat reaction (also /anime pat)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def pat(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "pat", user)

    @commands.command(name="cry", help="Anime cry reaction (also /anime cry)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def cry(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "cry", user)

    @commands.command(name="dance", help="Anime dance reaction (also /anime dance)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def dance(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "dance", user)

    @commands.command(name="laugh", help="Anime laugh reaction (also /anime laugh)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def laugh(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "laugh", user)

    @commands.command(name="smile", help="Anime smile reaction (also /anime smile)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def smile(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "smile", user)

    @commands.command(name="angry", help="Anime angry reaction (also /anime angry)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def angry(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "angry", user)

    @commands.command(name="confused", help="Anime confused reaction (also /anime confused)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def confused(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "confused", user)

    @commands.command(name="sleep", help="Anime sleep reaction (also /anime sleep)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def sleep(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "sleep", user)

    @commands.command(name="cuddle", help="Anime cuddle reaction (also /anime cuddle)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def cuddle(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "cuddle", user)

    @commands.command(name="poke", help="Anime poke reaction (also /anime poke)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def poke(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "poke", user)

    @commands.command(name="highfive", help="Anime highfive reaction (also /anime highfive)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def highfive(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "highfive", user)

    @commands.command(name="wave", help="Anime wave reaction (also /anime wave)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def wave(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "wave", user)

    @commands.command(name="blush", help="Anime blush reaction (also /anime blush)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def blush(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "blush", user)

    @commands.command(name="wink", help="Anime wink reaction (also /anime wink)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def wink(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "wink", user)

    @commands.command(name="smug", help="Anime smug reaction (also /anime smug)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def smug(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "smug", user)

    @commands.command(name="facepalm", help="Anime facepalm reaction (also /anime facepalm)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def facepalm(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "facepalm", user)

    @commands.command(name="pout", help="Anime pout reaction (also /anime pout)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def pout(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "pout", user)

    @commands.command(name="happy", help="Anime happy reaction (also /anime happy)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def happy(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "happy", user)

    @commands.command(name="nod", help="Anime nod reaction (also /anime nod)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def nod(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "nod", user)

    @commands.command(name="thumbsup", help="Anime thumbsup reaction (also /anime thumbsup)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def thumbsup(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "thumbsup", user)

    @commands.command(name="cheer", help="Anime cheer reaction (also /anime cheer)")
    @checks.ignore_check()
    @checks.blacklist_check()
    @commands.cooldown(rate=3, per=30, type=commands.BucketType.user)
    async def cheer(self, ctx: commands.Context, user: discord.User = None):
        await self.send_reaction(ctx, "cheer", user)
