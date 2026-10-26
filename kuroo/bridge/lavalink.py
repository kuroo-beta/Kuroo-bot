"""Connect music to the host's Lavalink v4 node and clean up on unload."""
import asyncio

import aiohttp
import wavelink

from kuroo.console.logging import logger


async def on_node(bot):
    await bot.wait_until_ready()
    config = bot.BotConfig
    while not bot.is_closed():
        async with aiohttp.ClientSession() as session:
            node = wavelink.Node(uri=config.LAVALINK_URI, password=config.LAVALINK_PASSWORD,
                                 identifier='kuroo', session=session, retries=2, inactive_player_timeout=180)
            try:
                await wavelink.Pool.connect(nodes=[node], client=bot)
                # The websocket's ready payload arrives after Pool.connect returns.
                for _ in range(100):
                    if node.status is not wavelink.NodeStatus.CONNECTING:
                        break
                    await asyncio.sleep(.2)
                if node.status is wavelink.NodeStatus.CONNECTED:
                    while not bot.is_closed() and node.status is not wavelink.NodeStatus.DISCONNECTED:
                        await asyncio.sleep(5)
            except (wavelink.NodeException, aiohttp.ClientError, OSError, ValueError) as error:
                logger.warning(f'Music connection failed ({type(error).__name__}).')
            finally:
                await node.close(eject=True)
        if not bot.is_closed():
            logger.warning('Music node unavailable; retrying in 30 seconds.')
            await asyncio.sleep(30)
