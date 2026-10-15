"""A pause between two requests to the same host.

You can save ten links of one site in one evening. Without a pause, the worker asks that site
ten times in one second, and the site can block it. So each request to a host gets a turn,
at least ``interval`` seconds after the turn before it.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable


class HostPacer:
    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        keep: int = 10_000,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._keep = keep
        # The moment from which each host can have its next turn.
        self._next: dict[str, float] = {}

    def __len__(self) -> int:
        return len(self._next)

    async def wait(self, host: str) -> float:
        """Wait for the turn of ``host``. Returns the seconds that it waited."""
        host = host.lower()
        now = self._clock()
        # No await between the read and the write: two requests cannot take the same turn.
        turn = max(now, self._next.get(host, now))
        self._next[host] = turn + self._interval
        if len(self._next) > self._keep:
            self._forget(now)
        if turn > now:
            await self._sleep(turn - now)
        return turn - now

    def _forget(self, now: float) -> None:
        """Forget the hosts whose next turn is free already: they wait for nothing."""
        self._next = {host: turn for host, turn in self._next.items() if turn > now}
