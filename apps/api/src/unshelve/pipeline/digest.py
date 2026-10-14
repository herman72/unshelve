"""The job that sends the weekly digest. The logic is in ``resurface/weekly.py``."""

from datetime import UTC, datetime

from unshelve.contracts.enums import JobKind
from unshelve.contracts.jobs import JobContext, SendDigestPayload
from unshelve.queue.registry import registry
from unshelve.resurface import weekly


@registry.handler(JobKind.SEND_DIGEST)
async def send_digest(ctx: JobContext, payload: SendDigestPayload) -> None:
    await weekly.send(ctx.services, payload.owner_id, payload.period_key, datetime.now(UTC))
