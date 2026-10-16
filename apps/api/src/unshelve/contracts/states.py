"""The item state machines, as data. Pure: no database, no I/O.

``status`` is the reading state and belongs to the user. ``enrich_state`` is the processing
state and belongs to the workers. Keeping them apart means a failed fetch can never move an
item out of the user's queue, and marking something as read can never restart processing.
"""

from collections.abc import Mapping
from types import MappingProxyType

from unshelve.contracts.enums import EnrichState, ItemStatus

S = ItemStatus
E = EnrichState

STATUS_TRANSITIONS: Mapping[ItemStatus, frozenset[ItemStatus]] = MappingProxyType(
    {
        S.INBOX: frozenset({S.QUEUED, S.READING, S.DONE, S.ARCHIVED}),
        S.QUEUED: frozenset({S.READING, S.DONE, S.INBOX, S.ARCHIVED}),
        S.READING: frozenset({S.DONE, S.QUEUED, S.ARCHIVED}),
        S.DONE: frozenset({S.QUEUED, S.ARCHIVED}),
        S.ARCHIVED: frozenset({S.INBOX}),
    }
)

# Statuses that take a slot in the reading queue (the work-in-progress limit).
QUEUE_SLOT_STATUSES: frozenset[ItemStatus] = frozenset({S.QUEUED, S.READING})

ENRICH_TRANSITIONS: Mapping[EnrichState, frozenset[EnrichState]] = MappingProxyType(
    {
        E.PENDING: frozenset({E.FETCHED, E.FAILED}),
        # A new version of a message sends the item back to the start, also in the middle of
        # its pipeline. The step that runs then fails its compare-and-set and writes nothing.
        E.FETCHED: frozenset({E.ENRICHED, E.FAILED, E.PENDING}),
        E.ENRICHED: frozenset({E.READY, E.FAILED, E.PENDING}),
        # A reprocess request sends a finished or failed item back to the start.
        E.READY: frozenset({E.PENDING, E.FAILED}),
        E.FAILED: frozenset({E.PENDING}),
    }
)


class IllegalTransitionError(ValueError):
    """The requested move is not in the transition table. The API maps this to 409."""

    def __init__(self, current: str, target: str) -> None:
        # str() of a state is its plain value. The repr of a state is "<ItemStatus.DONE: 'done'>",
        # and the message must say "cannot move from 'archived' to 'done'".
        current, target = str(current), str(target)
        super().__init__(f"cannot move from {current!r} to {target!r}")
        self.current = current
        self.target = target


def can_change_status(current: ItemStatus, target: ItemStatus) -> bool:
    """Whether the user may move an item from ``current`` to ``target``.

    A move to the same status is not a transition and returns False. Callers that receive
    a repeated request (a double tap on a button) treat it as already done, not as an error.
    """
    return target in STATUS_TRANSITIONS[current]


def check_status_change(current: ItemStatus, target: ItemStatus) -> None:
    if not can_change_status(current, target):
        raise IllegalTransitionError(current, target)


def can_change_enrich_state(current: EnrichState, target: EnrichState) -> bool:
    return target in ENRICH_TRANSITIONS[current]


def check_enrich_change(current: EnrichState, target: EnrichState) -> None:
    if not can_change_enrich_state(current, target):
        raise IllegalTransitionError(current, target)
