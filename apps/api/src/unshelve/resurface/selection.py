"""The choice of a digest: a pure function, with no database, no clock and no network.

The same candidates, the same budget and the same seed give the same digest. So a digest can
be tried again and show the same items, and a test can state rules that hold for each input
(tests/resurface/test_selection.py uses Hypothesis for that).

A digest is filled in this order, each item at most one time and each pick only while it fits
the time budget:

1. **One random slot**, chosen first so that it always has its place: an item of the inbox
   that no other rule would choose, and that is short enough. It brings back an item that you
   would not see again otherwise, and it lets you judge scores that the other rules never
   show (docs/learn/34).
2. **The focus goal**: items that help it (an effective score of 2 or 3), the highest first.
3. **Other goals**: items that help another active goal.
4. **Saved again**: an item that you saved two times or more.
5. **Recent**: items of the last days, the newest first. So a digest is useful before the
   items have scores.

Only items of the inbox count, and none that a digest showed in the last weeks: the caller
gives those as ``shown_recently``.
"""

import random
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from unshelve.contracts.enums import GoalLabel, ItemStatus

# An item without an estimate of its minutes (a short note, a link that was not read) counts
# as a short read.
DEFAULT_MINUTES = 5
# A digest is a short message: at most this many items, also when the budget allows more.
MAX_PICKS = 8
RECENT_DAYS = 7
# A score from this value up says that the item helps the goal (relevance.prompt).
HELPS_FROM = 2
# An item that you saved this many times or more comes back by the rule "saved again".
SAVED_AGAIN_FROM = 2
# The random slot takes at most this share of the budget, so that it cannot crowd out the
# items of the focus goal.
RANDOM_SHARE = 4


class Reason(StrEnum):
    FOCUS_GOAL = "focus_goal"
    OTHER_GOAL = "other_goal"
    SAVED_AGAIN = "saved_again"
    RECENT = "recent"
    RANDOM = "random"


@dataclass(frozen=True, slots=True)
class Candidate:
    """An item that a digest can show, with what the choice needs to know about it."""

    item_id: int
    status: ItemStatus
    saved_at: datetime  # the last time that it was saved
    saves: int = 1  # how many times it was saved
    minutes: int | None = None
    # The score of each active goal, and your verdict on it.
    scores: Mapping[int, tuple[int, GoalLabel | None]] = field(
        default_factory=dict[int, tuple[int, GoalLabel | None]]
    )


@dataclass(frozen=True, slots=True)
class Pick:
    item_id: int
    reason: Reason
    minutes: int
    goal_id: int | None = None  # the goal of a pick for a goal


@dataclass(frozen=True, slots=True)
class Selection:
    picks: tuple[Pick, ...]

    @property
    def total_minutes(self) -> int:
        return sum(pick.minutes for pick in self.picks)


def effective_score(score: int, label: GoalLabel | None) -> int:
    """The score after your verdict. A confirmed score, or one without a verdict, stands. A
    rejected score turns: a rejected 2 or 3 says that the item does not help (0), and a rejected
    0 or 1 says that it helps (2). The golden set uses the same rule (relevance.golden)."""
    if label is not GoalLabel.REJECTED:
        return score
    return 0 if score >= HELPS_FROM else HELPS_FROM


def minutes_of(candidate: Candidate) -> int:
    return candidate.minutes if candidate.minutes and candidate.minutes > 0 else DEFAULT_MINUTES


def _freshness(candidate: Candidate) -> tuple[int, float, int]:
    """Saved more often first, then saved later, then the lower id: an order without ties."""
    return (-candidate.saves, -candidate.saved_at.timestamp(), candidate.item_id)


def _has_a_rule(candidate: Candidate, since: datetime) -> bool:
    """True when a rule other than the random slot would choose the candidate."""
    return (
        any(effective_score(*value) >= HELPS_FROM for value in candidate.scores.values())
        or candidate.saves >= SAVED_AGAIN_FROM
        or candidate.saved_at >= since
    )


def _random_slot(
    eligible: Sequence[Candidate], budget: int, seed: str, since: datetime
) -> Candidate | None:
    limit = min(budget, max(DEFAULT_MINUTES, budget // RANDOM_SHARE))
    short = [
        candidate
        for candidate in eligible
        if minutes_of(candidate) <= limit and not _has_a_rule(candidate, since)
    ]
    if not short:
        return None
    # A choice, not a secret: the seed makes it the same for each try of one digest. Python
    # keeps random() the same for a seed in each new version, with the seeding of version 2;
    # it does not promise that for choice() ("Notes on Reproducibility" of the random module).
    numbers = random.Random()  # noqa: S311
    numbers.seed(seed, version=2)
    return short[int(numbers.random() * len(short))]


def _ranked(
    eligible: Sequence[Candidate], focus_goal_id: int | None, since: datetime
) -> Iterable[tuple[Reason, list[tuple[Candidate, int | None]]]]:
    """The candidates of each rule, in the order of the rules and in the order of each rule."""
    focus: list[tuple[int, Candidate]] = []
    others: list[tuple[int, int, Candidate]] = []
    for candidate in eligible:
        for goal_id, (score, label) in candidate.scores.items():
            if goal_id == focus_goal_id and effective_score(score, label) >= HELPS_FROM:
                focus.append((effective_score(score, label), candidate))
        best = max(
            (
                (effective_score(score, label), -goal_id)
                for goal_id, (score, label) in candidate.scores.items()
                if goal_id != focus_goal_id
            ),
            default=None,
        )
        if best is not None and best[0] >= HELPS_FROM:
            others.append((best[0], -best[1], candidate))
    focus.sort(key=lambda pair: (-pair[0], *_freshness(pair[1])))
    others.sort(key=lambda triple: (-triple[0], *_freshness(triple[2])))
    yield Reason.FOCUS_GOAL, [(candidate, focus_goal_id) for _, candidate in focus]
    yield Reason.OTHER_GOAL, [(candidate, goal_id) for _, goal_id, candidate in others]
    again = sorted((c for c in eligible if c.saves >= SAVED_AGAIN_FROM), key=_freshness)
    yield Reason.SAVED_AGAIN, [(candidate, None) for candidate in again]
    recent = sorted(
        (c for c in eligible if c.saved_at >= since),
        key=lambda c: (-c.saved_at.timestamp(), c.item_id),
    )
    yield Reason.RECENT, [(candidate, None) for candidate in recent]


def select(
    candidates: Sequence[Candidate],
    *,
    focus_goal_id: int | None,
    budget: int,
    seed: str,
    now: datetime,
    shown_recently: Collection[int] = (),
) -> Selection:
    """The items of a digest for a reading time of ``budget`` minutes."""
    eligible = sorted(
        (
            candidate
            for candidate in candidates
            if candidate.status is ItemStatus.INBOX and candidate.item_id not in shown_recently
        ),
        key=lambda candidate: candidate.item_id,
    )
    since = now - timedelta(days=RECENT_DAYS)  # the start of "recent"
    chosen = _random_slot(eligible, budget, seed, since)
    left = budget - (minutes_of(chosen) if chosen is not None else 0)
    room = MAX_PICKS - (chosen is not None)
    taken = {chosen.item_id} if chosen is not None else set[int]()
    picks: list[Pick] = []
    for reason, ranked in _ranked(eligible, focus_goal_id, since):
        for candidate, goal_id in ranked:
            minutes = minutes_of(candidate)
            if len(picks) >= room or candidate.item_id in taken or minutes > left:
                continue
            picks.append(Pick(candidate.item_id, reason, minutes, goal_id))
            taken.add(candidate.item_id)
            left -= minutes
    if chosen is not None:
        picks.append(Pick(chosen.item_id, Reason.RANDOM, minutes_of(chosen)))
    return Selection(tuple(picks))
