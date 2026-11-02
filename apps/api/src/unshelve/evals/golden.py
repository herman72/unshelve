"""The golden set of search: made-up items, queries, and their grades (docs/learn/40).

A grade says how well an item answers a query: 3 it answers it, 2 it is about the topic, 1 it
touches it, 0 it does not help. An item without a grade counts as 0. The metrics read the
grades as they are (``search.metrics``), so the loader refuses a set that they cannot measure:
a grade outside 0 to 3, a grade for an item that does not exist, or a query without any item
that helps.
"""

import json
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from unshelve.contracts.enums import ItemKind

Grade = Annotated[int, Field(ge=0, le=3)]
Key = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=80)]
# From this grade up, an item counts as relevant for recall@k and the reciprocal rank.
RELEVANT_FROM = 1


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GoldenItem(_Strict):
    key: Key
    title: str = Field(min_length=1)
    summary: str | None = None
    key_points: list[str] = []
    text: str | None = None
    kind: ItemKind = ItemKind.ARTICLE
    site: str | None = None
    author: str | None = None


class GoldenQuery(_Strict):
    q: str = Field(min_length=1)
    what: str = Field(min_length=1, description="What the query tests, in a few words.")
    grades: dict[Key, Grade]

    @model_validator(mode="after")
    def _helps(self) -> Self:
        if not any(grade >= RELEVANT_FROM for grade in self.grades.values()):
            msg = f"the query {self.q!r} has no item with a grade of {RELEVANT_FROM} or more"
            raise ValueError(msg)
        return self

    @property
    def relevant(self) -> frozenset[str]:
        return frozenset(key for key, grade in self.grades.items() if grade >= RELEVANT_FROM)


class GoldenSet(_Strict):
    about: str = ""
    items: list[GoldenItem] = Field(min_length=1)
    queries: list[GoldenQuery] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        keys = [item.key for item in self.items]
        if len(set(keys)) != len(keys):
            msg = "two items have the same key"
            raise ValueError(msg)
        questions = [query.q for query in self.queries]
        if len(set(questions)) != len(questions):
            msg = "two queries have the same words"
            raise ValueError(msg)
        unknown = sorted({key for query in self.queries for key in query.grades} - set(keys))
        if unknown:
            msg = f"grades for items that do not exist: {', '.join(unknown)}"
            raise ValueError(msg)
        return self


def load(path: Path) -> GoldenSet:
    """The golden set in ``path``, checked. Raises ``ValueError`` with each problem."""
    return GoldenSet.model_validate(json.loads(path.read_text(encoding="utf-8")))
