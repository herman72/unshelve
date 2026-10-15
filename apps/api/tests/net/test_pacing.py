"""The pause between two requests to the same host. A fake clock: no test waits for real."""

from unshelve.net.pacing import HostPacer


class Clock:
    """A clock that moves only when somebody sleeps, or when the test moves it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def _pacer(clock: Clock, interval: float = 1.0, keep: int = 10_000) -> HostPacer:
    return HostPacer(interval, clock=clock, sleep=clock.sleep, keep=keep)


async def test_requests_to_one_host_get_turns_one_interval_apart() -> None:
    clock = Clock()
    pacer = _pacer(clock)

    # Three requests at the same moment: the first goes at once, the others wait their turn.
    waits = [await pacer.wait("example.com") for _ in range(3)]

    assert waits == [0.0, 1.0, 2.0]
    assert clock.slept == [1.0, 2.0]


async def test_a_different_host_does_not_wait() -> None:
    clock = Clock()
    pacer = _pacer(clock)
    await pacer.wait("example.com")

    assert await pacer.wait("example.org") == 0.0


async def test_after_the_interval_a_request_goes_at_once() -> None:
    clock = Clock()
    pacer = _pacer(clock)
    await pacer.wait("example.com")
    clock.now += 1.5

    assert await pacer.wait("example.com") == 0.0


async def test_the_host_is_compared_without_capital_letters() -> None:
    clock = Clock()
    pacer = _pacer(clock)
    await pacer.wait("Example.COM")

    assert await pacer.wait("example.com") == 1.0


async def test_an_interval_of_zero_never_waits() -> None:
    clock = Clock()
    pacer = _pacer(clock, interval=0.0)

    assert [await pacer.wait("example.com") for _ in range(3)] == [0.0, 0.0, 0.0]


async def test_old_hosts_are_forgotten_so_that_the_list_stays_small() -> None:
    clock = Clock()
    pacer = _pacer(clock, keep=2)
    for host in ("a.example", "b.example", "c.example"):
        await pacer.wait(host)
    clock.now += 5

    await pacer.wait("d.example")

    assert len(pacer) <= 2
