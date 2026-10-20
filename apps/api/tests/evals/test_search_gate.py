"""The CI gate of search: the golden set, replayed with its recorded vectors, must give the
scores of the committed baseline (docs/learn/40).

    make eval           the gate, with the report of each list (the same test as in CI)
    make eval-live      record the vectors again with the embedder of the settings
    make eval-baseline  write the scores of the recorded vectors as the new baseline
"""

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.evals import golden, search
from unshelve.evals.cassette import Cassette, Recorder, Replay
from unshelve.services import build_embedder, close_embedder

EVALS = Path(__file__).resolve().parents[2] / "evals" / "search"
GOLDEN, VECTORS, BASELINE = EVALS / "golden.json", EVALS / "vectors.json", EVALS / "baseline.json"
# The owner of the golden items, in the database of the test.
OWNER_TELEGRAM_ID = 9000


def _show(report: search.Report, capsys: pytest.CaptureFixture[str]) -> None:
    """Write the report to the terminal, also without -s: it is the output of make eval, and
    the log of CI shows it for each run."""
    with capsys.disabled():
        sys.stdout.write(f"\n{report.table()}\n")


async def test_search_holds_its_baseline(
    db: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    golden_set = golden.load(GOLDEN)
    replay = Replay(Cassette.load(VECTORS))
    owner_id = await auth.ensure_owner(db, OWNER_TELEGRAM_ID)
    ids = await search.load_items(db, owner_id, golden_set, replay)

    report = await search.measure(db, replay, owner_id, golden_set, ids)

    _show(report, capsys)
    if os.environ.get("UNSHELVE_EVAL_BASELINE") == "write":
        search.save_baseline(report, BASELINE)
        return
    problems = search.compare(report, BASELINE)
    assert not problems, "\n".join(problems)


@pytest.mark.live
async def test_record_the_vectors_of_the_golden_set(
    db: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """Record with the embedder of the settings: Voyage with UNSHELVE_EMBEDDER=voyage and
    VOYAGE_API_KEY, else the fake one. It replaces vectors.json; the baseline stays."""
    embedder = build_embedder(Settings(env="test"))
    recorder = Recorder(embedder)
    golden_set = golden.load(GOLDEN)
    owner_id = await auth.ensure_owner(db, OWNER_TELEGRAM_ID)
    try:
        ids = await search.load_items(db, owner_id, golden_set, recorder)
        report = await search.measure(db, recorder, owner_id, golden_set, ids)
    finally:
        await close_embedder(embedder)

    # Only the texts of this run: a text that left the golden set leaves the cassette too.
    recorder.cassette.save(VECTORS)
    _show(report, capsys)
