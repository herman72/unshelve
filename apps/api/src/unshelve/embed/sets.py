"""The embedding sets: one for each embedding model that made vectors (ADR 0007).

Exactly one set is active, and search reads only its vectors. A new model gets a new set,
which starts inactive.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.dto import EmbeddingSpec
from unshelve.db.models import EmbeddingSet


def _same_spec(spec: EmbeddingSpec) -> sa.ColumnElement[bool]:
    return sa.and_(
        EmbeddingSet.provider == spec.provider,
        EmbeddingSet.model == spec.model,
        EmbeddingSet.dim == spec.dim,
    )


async def ensure_set(session: AsyncSession, spec: EmbeddingSpec) -> int:
    """The id of the embedding set of ``spec``. The row is made the first time.

    The first set of an instance becomes the active one. A later set starts inactive.
    """
    found = await find_set(session, spec)
    if found is not None:
        return found
    no_active_set = ~sa.exists().where(EmbeddingSet.is_active)
    await session.execute(
        postgresql.insert(EmbeddingSet)
        .values(
            provider=spec.provider,
            model=spec.model,
            dim=spec.dim,
            distance=spec.distance,
            doc_prefix=spec.doc_prefix,
            query_prefix=spec.query_prefix,
            is_active=no_active_set,
        )
        # No index is named here, on purpose. Two workers can do this at the same moment on a
        # new database. Then the second one can meet the row of the first one in two indexes:
        # "one set for each model" and "one active set". Each of the two must mean: do nothing.
        .on_conflict_do_nothing()
    )
    found = await session.execute(sa.select(EmbeddingSet.id).where(_same_spec(spec)))
    return found.scalar_one()


async def find_set(session: AsyncSession, spec: EmbeddingSpec) -> int | None:
    """The id of the embedding set of ``spec``, or None if no vector was made with it yet."""
    return await session.scalar(sa.select(EmbeddingSet.id).where(_same_spec(spec)))
