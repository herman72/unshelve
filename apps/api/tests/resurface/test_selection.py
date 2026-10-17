"""The choice of a digest. First examples with real values, then properties that Hypothesis
checks on many made-up inputs (500 by default, the same in each run: tests/conftest.py). Each
test name is one rule."""

import random
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from unshelve.contracts.enums import GoalLabel, ItemStatus
from unshelve.relevance.golden import relevant
from unshelve.resurface.selection import (
    DEFAULT_MINUTES,
    HELPS_FROM,
    MAX_PICKS,
    RANDOM_SHARE,
    RECENT_DAYS,
    SAVED_AGAIN_FROM,
    Candidate,
    Reason,
    Selection,
    effective_score,
    minutes_of,
    select,
)

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=30)
RUST, GPU = 1, 2


def _item(
    item_id: int,
    *,
    # More than a quarter of a budget of 60: such an item is never the random slot.
    minutes: int | None = 20,
    saved: datetime = OLD,
    saves: int = 1,
    status: ItemStatus = ItemStatus.INBOX,
    **scores: tuple[int, GoalLabel | None],
) -> Candidate:
    goals = {"rust": RUST, "gpu": GPU}
    return Candidate(
        item_id=item_id,
        status=status,
        saved_at=saved,
        saves=saves,
        minutes=minutes,
        scores={goals[name]: value for name, value in scores.items()},
    )


def _picks(selection: Selection) -> list[tuple[int, Reason]]:
    return [(pick.item_id, pick.reason) for pick in selection.picks]


def _select(
    candidates: list[Candidate],
    budget: int = 60,
    seed: str = "2026-W42",
    shown_recently: frozenset[int] = frozenset(),
) -> Selection:
    return select(
        candidates,
        focus_goal_id=RUST,
        budget=budget,
        seed=seed,
        now=NOW,
        shown_recently=shown_recently,
    )


# --- Examples ------------------------------------------------------------------------------


def test_the_focus_goal_comes_first_and_the_random_slot_last() -> None:
    # A budget of 70: the random slot takes items up to 17 minutes, so only item 9 can be it.
    candidates = [
        _item(1, rust=(2, None)),
        _item(2, rust=(3, None)),
        _item(3, gpu=(3, None)),
        _item(9, minutes=5),
    ]

    selection = _select(candidates, budget=70)

    assert _picks(selection) == [
        (2, Reason.FOCUS_GOAL),  # 3 before 2
        (1, Reason.FOCUS_GOAL),
        (3, Reason.OTHER_GOAL),
        (9, Reason.RANDOM),
    ]
    assert selection.picks[2].goal_id == GPU
    assert selection.total_minutes == 65


def test_in_one_rule_more_saves_come_first_then_the_newer_save() -> None:
    candidates = [
        _item(1, minutes=5, rust=(3, None), saved=OLD - timedelta(days=1)),
        _item(2, minutes=5, rust=(3, None), saved=OLD),
        _item(3, minutes=5, rust=(3, None), saved=OLD - timedelta(days=9), saves=2),
    ]

    assert [item for item, _ in _picks(_select(candidates))] == [3, 2, 1]


def test_an_item_that_helps_two_other_goals_as_much_comes_for_the_older_goal() -> None:
    candidate = Candidate(
        item_id=1,
        status=ItemStatus.INBOX,
        saved_at=OLD,
        minutes=20,
        scores={3: (3, None), GPU: (3, None)},
    )

    [pick] = _select([candidate]).picks

    assert (pick.reason, pick.goal_id) == (Reason.OTHER_GOAL, GPU)


def test_an_item_that_you_saved_again_comes_after_the_goals() -> None:
    candidates = [
        _item(4, saves=3),
        _item(5),  # saved one time, long ago, without scores: no rule takes it
        _item(9, minutes=5),
    ]

    assert _picks(_select(candidates, budget=70)) == [(4, Reason.SAVED_AGAIN), (9, Reason.RANDOM)]


def test_the_random_pick_counts_against_the_budget() -> None:
    candidates = [
        _item(1, minutes=16, rust=(3, None)),
        _item(2, minutes=15, rust=(2, None)),
        _item(9, minutes=5),  # the only item for the random slot
    ]

    selection = _select(candidates, budget=20)

    # 15 minutes are left after the random pick: 16 does not fit, 15 does.
    assert _picks(selection) == [(2, Reason.FOCUS_GOAL), (9, Reason.RANDOM)]
    assert selection.total_minutes == 20


def test_the_budget_skips_a_long_item_and_takes_a_shorter_one() -> None:
    candidates = [
        _item(1, minutes=40, rust=(3, None)),
        _item(2, minutes=30, rust=(3, None)),
        _item(3, minutes=15, rust=(2, None)),
    ]

    selection = _select(candidates, budget=60)

    # 40 fits, then 30 does not fit into the 20 minutes that are left, then 15 fits.
    assert _picks(selection) == [(1, Reason.FOCUS_GOAL), (3, Reason.FOCUS_GOAL)]
    assert selection.total_minutes == 55


def test_your_verdict_turns_a_score() -> None:
    candidates = [
        _item(1, rust=(3, GoalLabel.REJECTED)),  # you said: this 3 is wrong
        _item(2, rust=(0, GoalLabel.REJECTED)),  # you said: this 0 is wrong
        _item(3, rust=(2, GoalLabel.CONFIRMED)),
        _item(9, minutes=2),
    ]

    picks = _picks(_select(candidates, budget=60))

    assert (1, Reason.FOCUS_GOAL) not in picks
    assert [item for item, reason in picks if reason is Reason.FOCUS_GOAL] == [2, 3]


@pytest.mark.parametrize("score", [0, 1, 2, 3])
@pytest.mark.parametrize("label", [None, GoalLabel.CONFIRMED, GoalLabel.REJECTED])
def test_the_effective_score_agrees_with_the_truth_of_the_golden_set(
    score: int, label: GoalLabel | None
) -> None:
    helps = effective_score(score, label) >= HELPS_FROM
    if label is not None:
        assert helps is relevant(score, label)
    else:
        assert helps is (score >= HELPS_FROM)


def test_an_item_that_a_digest_showed_recently_does_not_come_again() -> None:
    candidates = [_item(1, rust=(3, None)), _item(2, rust=(3, None)), _item(9, minutes=2)]

    picks = _picks(_select(candidates, shown_recently=frozenset({2, 9})))

    assert picks == [(1, Reason.FOCUS_GOAL)]


def test_only_the_inbox_counts() -> None:
    candidates = [
        _item(1, rust=(3, None), status=ItemStatus.QUEUED),
        _item(2, rust=(3, None), status=ItemStatus.DONE),
        _item(3, rust=(3, None)),
    ]

    picks = _picks(_select(candidates))

    assert {item for item, _ in picks} == {3}


def test_without_scores_a_digest_has_the_items_of_the_last_7_days_newest_first() -> None:
    candidates = [
        _item(1, saved=NOW - timedelta(days=2)),
        _item(2, saved=NOW - timedelta(days=1)),
        _item(3, saved=NOW - timedelta(days=7, seconds=1)),
        _item(4, minutes=10, saved=NOW - timedelta(days=7)),  # just recent
        _item(9, minutes=2, saved=OLD),
    ]

    picks = _picks(_select(candidates, budget=60))

    assert picks == [(2, Reason.RECENT), (1, Reason.RECENT), (4, Reason.RECENT), (9, Reason.RANDOM)]


def test_an_item_without_minutes_counts_as_5_minutes() -> None:
    assert minutes_of(_item(1, minutes=None)) == 5
    assert minutes_of(_item(1, minutes=0)) == 5
    assert minutes_of(_item(1, minutes=12)) == 12


@pytest.mark.parametrize(
    ("budget", "longest"),
    [
        (60, 15),  # a quarter of the budget
        (12, 5),  # never less than 5 minutes
        (4, 4),  # and never more than the budget
    ],
)
def test_the_random_slot_takes_at_most_a_quarter_of_the_budget(budget: int, longest: int) -> None:
    # Without a rule, so each one can only be the random pick.
    candidates = [_item(1, minutes=longest + 1), _item(2, minutes=longest)]

    assert _picks(_select(candidates, budget=budget)) == [(2, Reason.RANDOM)]


def test_the_random_slot_never_takes_an_item_that_another_rule_takes() -> None:
    # Each item is short enough for the random slot, but each one has a rule.
    candidates = [
        _item(1, minutes=5, rust=(3, None)),
        _item(2, minutes=5, gpu=(0, GoalLabel.REJECTED)),  # a rejected 0 helps
        _item(3, minutes=5, saves=2),
        _item(4, minutes=5, saved=NOW - timedelta(days=1)),
    ]

    picks = _picks(_select(candidates, budget=60))

    assert picks == [
        (1, Reason.FOCUS_GOAL),
        (2, Reason.OTHER_GOAL),
        (3, Reason.SAVED_AGAIN),
        (4, Reason.RECENT),
    ]


def test_the_same_seed_gives_the_same_random_slot_and_another_seed_can_change_it() -> None:
    candidates = [_item(n, minutes=5) for n in range(1, 30)]

    first = {_select(candidates, seed="2026-W42").picks[-1].item_id for _ in range(3)}
    others = {_select(candidates, seed=f"week {n}").picks[-1].item_id for n in range(10)}

    assert len(first) == 1
    assert len(others) > 1


def test_the_order_of_the_items_does_not_change_the_random_pick() -> None:
    candidates = [_item(n, minutes=5) for n in range(1, 30)]

    forward = _select(candidates).picks[-1]
    backward = _select(candidates[::-1]).picks[-1]

    assert forward == backward


def test_a_seed_always_gives_the_same_random_pick() -> None:
    # Python keeps the numbers of random() the same for a seed in each version, not those of
    # choice() (docs/learn/34). With this seed, choice() would take item 29, and the seeding of
    # version 1 item 20: a change of the way of picking fails here.
    candidates = [_item(n, minutes=5) for n in range(1, 30)]

    assert _select(candidates, seed="2026-W47").picks[-1].item_id == 26


def test_a_digest_has_at_most_8_items() -> None:
    candidates = [_item(n, minutes=1, rust=(3, None)) for n in range(1, 30)]
    with_a_random_pick = [*candidates, _item(99, minutes=1)]

    assert len(_select(candidates, budget=600).picks) == 8
    assert len(_select(with_a_random_pick, budget=600).picks) == 8


# --- Properties ----------------------------------------------------------------------------

LABELS = st.sampled_from([None, GoalLabel.CONFIRMED, GoalLabel.REJECTED])
SCORES = st.dictionaries(st.integers(1, 4), st.tuples(st.integers(0, 3), LABELS), max_size=4)
NO_SCORES: dict[int, tuple[int, GoalLabel | None]] = {}


@st.composite
def _candidates(draw: st.DrawFn) -> list[Candidate]:
    # The common values more often: an item of the inbox, saved one time, often without a
    # helpful score. Such items are the ones of the random slot, and a property about the random
    # slot needs them in most inputs.
    ids = draw(st.lists(st.integers(1, 500), unique=True, max_size=40))
    return [
        Candidate(
            item_id=item_id,
            status=draw(st.just(ItemStatus.INBOX) | st.sampled_from(list(ItemStatus))),
            saved_at=NOW - timedelta(minutes=draw(st.integers(0, 60 * 24 * 60))),
            saves=draw(st.just(1) | st.integers(1, 5)),
            minutes=draw(st.none() | st.integers(0, 30) | st.integers(0, 180)),
            scores=draw(st.just(NO_SCORES) | SCORES),
        )
        for item_id in ids
    ]


@dataclass(frozen=True)
class Inputs:
    candidates: list[Candidate]
    focus_goal_id: int | None
    budget: int
    seed: str
    shown_recently: frozenset[int]

    def run(self) -> Selection:
        return select(
            self.candidates,
            focus_goal_id=self.focus_goal_id,
            budget=self.budget,
            seed=self.seed,
            now=NOW,
            shown_recently=self.shown_recently,
        )

    def candidate(self, item_id: int) -> Candidate:
        return next(c for c in self.candidates if c.item_id == item_id)


@st.composite
def _inputs(draw: st.DrawFn) -> Inputs:
    candidates = draw(_candidates())
    ids = [candidate.item_id for candidate in candidates]
    shown = draw(st.frozensets(st.sampled_from(ids))) if ids else frozenset[int]()
    return Inputs(
        candidates=candidates,
        focus_goal_id=draw(st.none() | st.integers(1, 4)),
        budget=draw(st.integers(0, 60) | st.integers(0, 600)),
        seed=draw(st.text(max_size=12)),
        shown_recently=shown,
    )


@given(_inputs())
def test_a_digest_never_passes_its_budget(inputs: Inputs) -> None:
    assert inputs.run().total_minutes <= inputs.budget


@given(_inputs())
def test_a_digest_repeats_nothing(inputs: Inputs) -> None:
    ids = [pick.item_id for pick in inputs.run().picks]

    assert len(ids) == len(set(ids))  # no item two times in one digest
    assert not set(ids) & inputs.shown_recently  # nothing that a digest showed recently
    assert all(inputs.candidate(item).status is ItemStatus.INBOX for item in ids)


@given(_inputs())
def test_the_same_input_and_seed_give_the_same_digest(inputs: Inputs) -> None:
    assert inputs.run() == inputs.run()


@given(_inputs(), st.randoms(use_true_random=False))
def test_the_order_of_the_candidates_does_not_change_the_digest(
    inputs: Inputs, shuffler: random.Random
) -> None:
    # The database gives its rows in no fixed order.
    shuffled = list(inputs.candidates)
    shuffler.shuffle(shuffled)

    assert replace(inputs, candidates=shuffled).run() == inputs.run()


@given(_inputs())
def test_each_pick_has_its_reason(inputs: Inputs) -> None:
    selection = inputs.run()
    order = list(Reason)

    reasons = [pick.reason for pick in selection.picks]
    assert reasons == sorted(reasons, key=order.index)  # the rules in their order
    assert reasons.count(Reason.RANDOM) <= 1
    assert len(selection.picks) <= MAX_PICKS
    for pick in selection.picks:
        item = inputs.candidate(pick.item_id)
        assert pick.minutes == minutes_of(item)
        if pick.reason in (Reason.FOCUS_GOAL, Reason.OTHER_GOAL):
            assert pick.goal_id is not None
            assert (pick.goal_id == inputs.focus_goal_id) is (pick.reason is Reason.FOCUS_GOAL)
            assert effective_score(*item.scores[pick.goal_id]) >= HELPS_FROM
        if pick.reason is Reason.SAVED_AGAIN:
            assert item.saves >= SAVED_AGAIN_FROM
        if pick.reason is Reason.RECENT:
            assert item.saved_at >= NOW - timedelta(days=RECENT_DAYS)
        if pick.reason is Reason.RANDOM:
            assert _without_a_rule(item)


def _without_a_rule(candidate: Candidate) -> bool:
    """No rule other than the random slot would choose the candidate."""
    return (
        all(effective_score(*value) < HELPS_FROM for value in candidate.scores.values())
        and candidate.saves < SAVED_AGAIN_FROM
        and candidate.saved_at < NOW - timedelta(days=RECENT_DAYS)
    )


@given(_inputs())
def test_the_random_slot_is_there_when_a_short_item_without_a_rule_is_there(
    inputs: Inputs,
) -> None:
    selection = inputs.run()
    limit = min(inputs.budget, max(DEFAULT_MINUTES, inputs.budget // RANDOM_SHARE))
    short = [
        candidate
        for candidate in inputs.candidates
        if candidate.status is ItemStatus.INBOX
        and candidate.item_id not in inputs.shown_recently
        and minutes_of(candidate) <= limit
        and _without_a_rule(candidate)
    ]

    random_picks = [pick for pick in selection.picks if pick.reason is Reason.RANDOM]
    assert len(random_picks) == (1 if short else 0)
    if random_picks:
        assert selection.picks[-1] is random_picks[0]  # the last item of the digest
