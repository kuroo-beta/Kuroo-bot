"""Shared streaming card for initial connection and live updates."""
import discord


def streaming_activity(config, guilds=(), shard_count=1):
    servers = len(guilds)
    users = sum(guild.member_count or 0 for guild in guilds)
    shards = shard_count or 1
    state = (f"{servers} {'server' if servers == 1 else 'servers'} • "
             f"{users} {'user' if users == 1 else 'users'} • "
             f"{shards} {'shard' if shards == 1 else 'shards'}")
    # Streaming drops `state` in discord.py 2.6; Activity preserves both lines.
    return discord.Activity(type=discord.ActivityType.streaming, name=config.STREAMING_NAME,
                            details=config.STREAMING_NAME, state=state, url=config.STREAMING_URL)
