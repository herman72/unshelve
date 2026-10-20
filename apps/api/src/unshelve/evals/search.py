"""The evaluation of search on the golden set (docs/learn/40).

1. The items of the golden set go into the database of one owner, with their cards, their
   pieces and their vectors, as the pipeline makes them (``embed.chunks``).
2. Each query runs through hybrid search (``search.hybrid``).
3. Each list is measured against the grades: the list of each retriever, and the fused list.
   recall@10 and the reciprocal rank count the items with a grade of 1 or more; nDCG@10 reads
   the grades as they are (``search.metrics``).
4. The mean of each metric over the queries is the score of a list. The CI gate compares the
   scores of the fused list with the committed baseline.
"""

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean

from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.dto import EmbeddingSpec
from unshelve.contracts.enums import ChunkKind, EnrichState
from unshelve.contracts.protocols import Embedder
from unshelve.db.models import Chunk, Item, ItemContent
from unshelve.embed.chunks import card_text, pieces_of
from unshelve.embed.sets import ensure_set
from unshelve.evals.golden import GoldenSet
from unshelve.search import hybrid
from unshelve.search.metrics import ndcg_at_k, recall_at_k, reciprocal_rank

K = 10
# The day on which each golden item was saved. Search does not read it.
SAVED = datetime(2026, 1, 1, tzinfo=UTC)
# The lists that are measured, in the order of the report.
LISTS: dict[str, Callable[[hybrid.Ranks], int | None]] = {
    "full text": lambda ranks: ranks.full_text,
    "trigram": lambda ranks: ranks.trigram,
    "vector": lambda ranks: ranks.vector,
}
FUSED = "fused"
# The gate compares numbers of a deterministic replay; this only absorbs the last bits of a
# float. One query that moves one place changes a mean by far more.
TOLERANCE = 1e-6


@dataclass(frozen=True, slots=True)
class Scores:
    recall: float  # recall@10
    ndcg: float  # nDCG@10
    mrr: float  # the mean reciprocal rank


@dataclass(frozen=True, slots=True)
class QueryResult:
    q: str
    what: str
    fused: Scores  # of this query alone: its recall@10, nDCG@10 and reciprocal rank


@dataclass(frozen=True, slots=True)
class Report:
    spec: EmbeddingSpec
    items: int
    queries: tuple[QueryResult, ...]
    lists: dict[str, Scores]  # "full text", "trigram", "vector" and "fused"

    def table(self, weakest: int = 5) -> str:
        """The scores as text: one line for each list, then the weakest queries."""
        spec = self.spec
        lines = [
            f"search on the golden set: {self.items} items, {len(self.queries)} queries; "
            f"vectors of {spec.provider}/{spec.model} ({spec.dim} numbers)",
            f"{'list':<10} {'recall@10':>10} {'nDCG@10':>8} {'MRR':>6}",
        ]
        for name, scores in self.lists.items():
            numbers = f"{scores.recall:>10.3f} {scores.ndcg:>8.3f} {scores.mrr:>6.3f}"
            lines.append(f"{name:<10} {numbers}")
        lines.append(f"the {weakest} weakest queries (nDCG@10 of the fused list):")
        for result in sorted(self.queries, key=lambda result: result.fused.ndcg)[:weakest]:
            lines.append(f"  {result.fused.ndcg:.3f}  {result.q!r}  ({result.what})")
        return "\n".join(lines)


async def load_items(
    session: AsyncSession, owner_id: int, golden: GoldenSet, embedder: Embedder
) -> dict[str, int]:
    """Save the items of the golden set for ``owner_id``, with their vectors in the set of
    ``embedder``. Returns the id of each item by its key. The caller commits, or not."""
    set_id = await ensure_set(session, embedder.spec)
    ids: dict[str, int] = {}
    # (item key, kind, position, text, start, end) of each chunk, as the embed step makes it.
    wanted: list[tuple[str, ChunkKind, int, str, int | None, int | None]] = []
    for golden_item in golden.items:
        item = Item(
            owner_id=owner_id,
            kind=golden_item.kind,
            enrich_state=EnrichState.READY,
            title=golden_item.title,
            summary=golden_item.summary,
            key_points=golden_item.key_points,
            site=golden_item.site,
            author=golden_item.author,
            first_captured_at=SAVED,
            last_captured_at=SAVED,
        )
        session.add(item)
        await session.flush()
        ids[golden_item.key] = item.id
        text = golden_item.text or ""
        if text:
            session.add(
                ItemContent(
                    item_id=item.id,
                    owner_id=owner_id,
                    text=text,
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                    extractor="golden",
                    extractor_version=1,
                )
            )
        card = card_text(golden_item.title, golden_item.summary, golden_item.key_points)
        wanted.append((golden_item.key, ChunkKind.CARD, 0, card, None, None))
        wanted += [
            (golden_item.key, ChunkKind.BODY, number, piece.text, piece.start, piece.end)
            for number, piece in enumerate(pieces_of(text))
        ]
    # One call for all the texts: a live embedder gets a few batches, not one call per item.
    vectors = await embedder.embed_documents([row[3] for row in wanted])
    session.add_all(
        Chunk(
            owner_id=owner_id,
            item_id=ids[key],
            embedding_set_id=set_id,
            kind=kind,
            ord=position,
            text=text,
            char_start=start,
            char_end=end,
            dim=len(vector),
            embedding=vector,
        )
        for (key, kind, position, text, start, end), vector in zip(wanted, vectors, strict=True)
    )
    await session.flush()
    return ids


def _list_of(ranking: hybrid.Ranking, place: Callable[[hybrid.Ranks], int | None]) -> list[int]:
    """The list of one retriever, read back from the places that the hits keep."""
    placed = [(rank, hit.item_id) for hit in ranking.hits if (rank := place(hit.ranks)) is not None]
    return [item_id for _, item_id in sorted(placed)]


def _scores(ranking: Sequence[int], relevant: set[int], relevance: dict[int, int]) -> Scores:
    return Scores(
        recall=recall_at_k(ranking, relevant, K),
        ndcg=ndcg_at_k(ranking, relevance, K),
        mrr=reciprocal_rank(ranking, relevant),
    )


async def measure(
    session: AsyncSession,
    embedder: Embedder,
    owner_id: int,
    golden: GoldenSet,
    ids: dict[str, int],
) -> Report:
    """Run each query of the golden set and measure each list. ``ids``: from ``load_items``."""
    per_list: dict[str, list[Scores]] = {name: [] for name in [*LISTS, FUSED]}
    results: list[QueryResult] = []
    for query in golden.queries:
        ranking = await hybrid.search(session, embedder, owner_id, query.q)
        relevant = {ids[key] for key in query.relevant}
        relevance = {ids[key]: grade for key, grade in query.grades.items()}
        for name, place in LISTS.items():
            per_list[name].append(_scores(_list_of(ranking, place), relevant, relevance))
        fused = _scores([hit.item_id for hit in ranking.hits], relevant, relevance)
        per_list[FUSED].append(fused)
        results.append(QueryResult(q=query.q, what=query.what, fused=fused))
    return Report(
        spec=embedder.spec,
        items=len(golden.items),
        queries=tuple(results),
        lists={
            name: Scores(
                recall=fmean(scores.recall for scores in values),
                ndcg=fmean(scores.ndcg for scores in values),
                mrr=fmean(scores.mrr for scores in values),
            )
            for name, values in per_list.items()
        },
    )


# --- The baseline ---------------------------------------------------------------------------


def _vectors_of(spec: EmbeddingSpec) -> str:
    return f"{spec.provider}/{spec.model}/{spec.dim}"


def save_baseline(report: Report, path: Path) -> None:
    """Write the scores of the fused list as the new baseline."""
    fused = report.lists[FUSED]
    data = {
        "vectors": _vectors_of(report.spec),
        "queries": len(report.queries),
        "recall@10": round(fused.recall, 6),
        "nDCG@10": round(fused.ndcg, 6),
        # Shown, not gated: the issue gates recall and nDCG.
        "MRR": round(fused.mrr, 6),
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def compare(report: Report, path: Path) -> list[str]:
    """What differs from the baseline in ``path``: empty when the gate holds.

    A score below the baseline fails, and so does a score above it. A better score must be
    written into the baseline, or a later change could lose it again without a failure.
    """
    baseline = json.loads(path.read_text(encoding="utf-8"))
    if baseline["vectors"] != _vectors_of(report.spec):
        return [
            f"the baseline was measured with the vectors of {baseline['vectors']}, this run with "
            f"{_vectors_of(report.spec)}: write a new baseline with make eval-baseline"
        ]
    fused = report.lists[FUSED]
    problems: list[str] = []
    for name, now in (("recall@10", fused.recall), ("nDCG@10", fused.ndcg)):
        before = float(baseline[name])
        if now < before - TOLERANCE:
            problems.append(f"{name} dropped from {before:.6f} to {now:.6f}")
        elif now > before + TOLERANCE:
            problems.append(
                f"{name} rose from {before:.6f} to {now:.6f}: write the new baseline with "
                "make eval-baseline, and commit it"
            )
    return problems
