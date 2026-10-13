"""Which function runs which kind of job.

A handler is registered with a decorator:

    @registry.handler(JobKind.FETCH_ITEM, timeout_s=120)
    async def fetch_item(ctx: JobContext, payload: FetchItemPayload) -> None:
        ...

The payload type comes from ``PAYLOAD_TYPES``, so a kind has exactly one payload shape.
"""

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from unshelve.contracts.enums import JobKind
from unshelve.contracts.jobs import PAYLOAD_TYPES, JobHandler, JobPayload


@dataclass(frozen=True, slots=True)
class HandlerSpec:
    kind: JobKind
    payload_type: type[JobPayload]
    run: JobHandler[Any]
    timeout: timedelta  # the handler is stopped after this time, and the job is tried again


class Registry:
    def __init__(self) -> None:
        self._specs: dict[JobKind, HandlerSpec] = {}

    def handler[P: JobPayload](
        self, kind: JobKind, *, timeout_s: float = 120
    ) -> Callable[[JobHandler[P]], JobHandler[P]]:
        """Register the decorated function as the handler of ``kind``."""

        def register(run: JobHandler[P]) -> JobHandler[P]:
            if kind in self._specs:
                msg = f"{kind.value} already has a handler"
                raise ValueError(msg)
            self._specs[kind] = HandlerSpec(
                kind, PAYLOAD_TYPES[kind], run, timedelta(seconds=timeout_s)
            )
            return run

        return register

    def get(self, kind: JobKind) -> HandlerSpec | None:
        return self._specs.get(kind)

    def kinds(self) -> list[JobKind]:
        """The kinds that have a handler. A worker claims only these."""
        return sorted(self._specs)


# The registry that the handlers in ``unshelve.pipeline`` use.
registry = Registry()


def discover(package: str = "unshelve.pipeline") -> int:
    """Import every module of ``package`` so that its handlers register. Returns how many."""
    root = importlib.import_module(package)
    names = [info.name for info in pkgutil.walk_packages(root.__path__, prefix=f"{package}.")]
    for name in names:
        importlib.import_module(name)
    return len(names)
