"""The walking skeleton: one path through every layer of the system.

Sign in with a login link, save a link, let the worker run the pipeline, read the list. If
this test passes, the API, the database, the queue, the worker and the providers fit together.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import ClientFactory
from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig
from unshelve.services import build_services

URL = "https://example.com/how-pgvector-works"
TEXT = "A short guide to vector search in Postgres"


async def test_a_saved_link_becomes_a_finished_item_in_the_list(
    client_for: ClientFactory,
    private_database_url: str,
    committed_db: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(env="test", database_url=private_database_url)
    async with committed_db() as db:
        token = await auth.issue_login_token(db, await auth.ensure_owner(db, 4242))
        await db.commit()

    async with client_for(settings) as client:
        # 1. Sign in with the link.
        signed_in = await client.post("/api/v1/auth/magic/consume", json={"token": token})
        assert signed_in.status_code == 204

        # 2. Save a link. The answer comes at once, before any processing.
        saved = await client.post("/api/v1/items", json={"url": URL, "text": TEXT})
        assert saved.status_code == 201
        assert saved.json()["enrich_state"] == "pending"
        item_id = saved.json()["id"]

        # 3. The worker runs the four steps: fetch, enrich, embed, and score (no goal yet).
        discover()
        worker = Worker(
            build_services(settings, committed_db), registry, WorkerConfig(worker_id="w1")
        )
        steps = 0
        while await worker.run_one():
            steps += 1
        assert steps == 4

        # 4. The list shows the finished item.
        inbox = await client.get("/api/v1/items", params={"status": "inbox"})
        detail = await client.get(f"/api/v1/items/{item_id}")

    (item,) = inbox.json()["items"]
    assert item["id"] == item_id
    assert item["enrich_state"] == "ready"
    assert item["url"] == URL
    # The fake model builds its answer from the first words of the text.
    assert item["title"] == "a short guide to vector search in postgres"
    assert item["summary"] == "a short guide to vector search in postgres"
    assert item["est_minutes"] == 1
    assert item["partial"] is True
    assert detail.json()["key_points"] == ["short", "guide", "vector"]
