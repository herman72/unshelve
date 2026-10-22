"""The job that publishes the public feed of an owner. The logic is in ``publish/feed.py``."""

from datetime import timedelta

import structlog

from unshelve.contracts.enums import JobKind
from unshelve.contracts.errors import PublishError
from unshelve.contracts.jobs import JobContext, Permanent, PublishFeedPayload, Retry
from unshelve.publish import feed
from unshelve.queue.registry import registry

log = structlog.get_logger(__name__)

# A full disk or a network can pass; a folder that cannot be written does not.
RETRY_AFTER = timedelta(minutes=5)


@registry.handler(JobKind.PUBLISH_FEED)
async def publish_feed(ctx: JobContext, payload: PublishFeedPayload) -> None:
    target = ctx.services.feed
    if target is None:
        # UNSHELVE_PUBLISH_TARGET is "none": there is nowhere to publish, and that is no fault.
        log.info("publish.no_target", owner_id=payload.owner_id)
        return
    try:
        report = await feed.publish(ctx.services.sessions, payload.owner_id, target)
    except PublishError as error:
        if error.retryable:
            raise Retry(str(error), after=RETRY_AFTER) from error
        raise Permanent(str(error)) from error
    log.info(
        "publish.done",
        owner_id=payload.owner_id,
        items=report.items,
        size=report.size,
        sha256=report.sha256[:12],
        changed=report.published.changed,
        where=report.published.where,
    )
