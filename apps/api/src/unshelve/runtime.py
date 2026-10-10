"""Process lifecycle helpers shared by the long-running commands."""

import asyncio
import signal


async def wait_for_shutdown() -> None:
    """Return when the process receives SIGINT or SIGTERM."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
