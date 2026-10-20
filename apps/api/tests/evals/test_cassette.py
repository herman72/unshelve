"""Recorded vectors: a replay of an embedder without the network. Each test name is one rule."""

from pathlib import Path

import pytest

from unshelve.evals.cassette import Cassette, MissingVectorError, Recorder, Replay
from unshelve.testing.fakes import FakeEmbedder


async def _record(tmp_path: Path) -> tuple[Recorder, Path]:
    recorder = Recorder(FakeEmbedder())
    await recorder.embed_documents(["Postgres as a job queue", "Sourdough at home"])
    await recorder.embed_query("postgres queue")
    path = tmp_path / "vectors.json"
    recorder.cassette.save(path)
    return recorder, path


async def test_a_replay_gives_back_what_the_recorder_gave(tmp_path: Path) -> None:
    recorder, path = await _record(tmp_path)
    replay = Replay(Cassette.load(path))

    assert replay.spec == recorder.spec == FakeEmbedder().spec
    assert await replay.embed_documents(["Sourdough at home"]) == await recorder.embed_documents(
        ["Sourdough at home"]
    )
    assert await replay.embed_query("postgres queue") == await recorder.embed_query(
        "postgres queue"
    )


async def test_the_recorder_rounds_its_vectors_like_the_replay(tmp_path: Path) -> None:
    # Half-precision floats keep about three digits: the live run must rank with the same
    # numbers as its replay, not with the more exact ones.
    exact = await FakeEmbedder().embed_query("postgres queue")
    recorder, _ = await _record(tmp_path)

    rounded = await recorder.embed_query("postgres queue")

    assert rounded != exact
    assert all(abs(a - b) < 1e-3 for a, b in zip(rounded, exact, strict=True))


async def test_a_text_that_was_not_recorded_is_refused(tmp_path: Path) -> None:
    _, path = await _record(tmp_path)
    replay = Replay(Cassette.load(path))

    with pytest.raises(MissingVectorError, match="make eval-live"):
        await replay.embed_query("a new query")


async def test_a_document_and_a_query_are_two_recordings(tmp_path: Path) -> None:
    # Many embedders give a query and a document of the same words different vectors.
    _, path = await _record(tmp_path)
    replay = Replay(Cassette.load(path))

    with pytest.raises(MissingVectorError, match="query"):
        await replay.embed_query("Sourdough at home")


async def test_a_vector_takes_two_bytes_for_each_number(tmp_path: Path) -> None:
    recorder = Recorder(FakeEmbedder(dim=1024))
    await recorder.embed_query("one query")

    (packed,) = recorder.cassette.vectors.values()
    # 2,048 bytes in base64: four characters for each three bytes.
    assert len(packed) == 2732
