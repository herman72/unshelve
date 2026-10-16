import pytest

from unshelve.contracts.enums import EnrichState, ItemStatus
from unshelve.contracts.states import (
    ENRICH_TRANSITIONS,
    QUEUE_SLOT_STATUSES,
    STATUS_TRANSITIONS,
    IllegalTransitionError,
    can_change_enrich_state,
    can_change_status,
    check_enrich_change,
    check_status_change,
)

S = ItemStatus
E = EnrichState


def _reachable[T](start: T, table: dict[T, frozenset[T]]) -> set[T]:
    seen = {start}
    frontier = [start]
    while frontier:
        for target in table[frontier.pop()]:
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    return seen


def test_every_status_has_a_row() -> None:
    assert set(STATUS_TRANSITIONS) == set(ItemStatus)
    assert set(ENRICH_TRANSITIONS) == set(EnrichState)


def test_no_state_moves_to_itself() -> None:
    assert all(state not in targets for state, targets in STATUS_TRANSITIONS.items())
    assert all(state not in targets for state, targets in ENRICH_TRANSITIONS.items())


def test_every_status_is_reachable_from_inbox_and_can_return_to_it() -> None:
    table = dict(STATUS_TRANSITIONS)
    assert _reachable(S.INBOX, table) == set(ItemStatus)
    # Nothing is a dead end: an archived or finished item can always come back.
    assert all(S.INBOX in _reachable(state, table) for state in ItemStatus)


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (S.INBOX, S.DONE, True),  # read it straight from the inbox
        (S.DONE, S.QUEUED, True),  # read it again
        (S.ARCHIVED, S.INBOX, True),
        (S.ARCHIVED, S.DONE, False),  # must be restored first
        (S.DONE, S.READING, False),
        (S.DONE, S.INBOX, False),
        (S.READING, S.INBOX, False),
        (S.QUEUED, S.QUEUED, False),  # not a transition
    ],
)
def test_status_changes(current: ItemStatus, target: ItemStatus, allowed: bool) -> None:
    assert can_change_status(current, target) is allowed


def test_an_illegal_status_change_raises_with_both_states() -> None:
    with pytest.raises(IllegalTransitionError) as caught:
        check_status_change(S.ARCHIVED, S.DONE)
    assert (caught.value.current, caught.value.target) == ("archived", "done")
    # The plain values, not the repr of the enum: this text is the detail of a 409.
    assert str(caught.value) == "cannot move from 'archived' to 'done'"
    check_status_change(S.INBOX, S.QUEUED)  # a legal move raises nothing


def test_only_queued_and_reading_take_a_queue_slot() -> None:
    assert frozenset({S.QUEUED, S.READING}) == QUEUE_SLOT_STATUSES


def test_processing_moves_forward_one_step_at_a_time() -> None:
    assert can_change_enrich_state(E.PENDING, E.FETCHED)
    assert not can_change_enrich_state(E.PENDING, E.ENRICHED)
    assert not can_change_enrich_state(E.PENDING, E.READY)


def test_any_processing_state_can_fail_and_a_failure_can_only_restart() -> None:
    assert all(E.FAILED in ENRICH_TRANSITIONS[s] for s in EnrichState if s is not E.FAILED)
    assert ENRICH_TRANSITIONS[E.FAILED] == frozenset({E.PENDING})


def test_a_restart_sends_an_item_back_to_the_start_from_each_state() -> None:
    # A reprocess, or a new version of a message, also in the middle of the pipeline.
    for state in (E.FETCHED, E.ENRICHED, E.READY, E.FAILED):
        check_enrich_change(state, E.PENDING)
    with pytest.raises(IllegalTransitionError):
        check_enrich_change(E.PENDING, E.PENDING)  # not a move
