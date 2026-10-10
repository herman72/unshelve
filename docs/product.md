# Product brief

## Problem

People who learn in public spaces save a lot: posts, videos, articles, repositories, screenshots. The saving is easy and the pile grows. Very little of it is read. The pile has no status, no priority and no link to what the person is trying to achieve, so going back to it feels like work.

The usual answer is a better place to store things. That does not help. A better database of unread items is a better graveyard.

## Who it is for

**First user: the author.** One engineer with a private Telegram channel full of saved items and a written 12-month growth plan.

**Next users:** developers who self-host their tools and already use Telegram as an inbox.

A hosted, multi-user service is a possible later step. It is not a goal of version 1.

## What Unshelve does

```
Capture → Enrich → Triage → Resurface → Track → Retrieve → Publish
```

1. **Capture.** A bot is an admin of the user's existing channel and receives every post. The channel stays write-only: the user's habit does not change. A one-time import of a Telegram Desktop export loads the history.
2. **Enrich.** Each item is fetched according to its type and gets a summary, tags, a kind, a language and a reading time. A dead link still produces an item from the text that was saved with it.
3. **Triage.** The user keeps a short list of goals. Each item is scored 0 to 3 against each goal, with a one-sentence reason. The user confirms or rejects scores, and those labels become the evaluation set.
4. **Resurface.** Once a week the bot sends a digest in a private chat: a few picks for the focus goal, within a time budget, plus one random item. The reading queue has a size limit. Old inbox items get an "archive?" prompt.
5. **Track.** `inbox → queued → reading → done`, or `archived`. Nothing is ever deleted. Every change is recorded in an append-only log.
6. **Retrieve.** Private hybrid search with filters, and an overview answer with citations that shows what the user already has on a topic and what is still unread.
7. **Publish.** A per-item visibility flag. The user sees the exact public record before confirming. A static feed of public items is pushed to the user's site, which searches it in the browser.

## What makes it different

| Existing tools | Unshelve |
|---|---|
| Store and tag | Score against the user's own goals, with reasons |
| Wait to be opened | Push a small, time-boxed digest |
| One audience | Private library with a curated public subset on the user's own site |
| New capture habit (extension, app) | The Telegram channel the user already has |

## Principles

- **Return over storage.** Every feature is judged by whether it makes reading more likely.
- **Nothing is lost.** Captures are immutable. Items are archived, never deleted.
- **Private unless published.** Public data is a strict subset by construction, not by filtering in the UI.
- **Works with outbound connections only.** Capture and publishing need no inbound port.
- **Measured, not assumed.** Relevance and search quality are evaluated on labelled data.

## Success measures

- Items read each week.
- Median age of unread items.
- Share of read items that match an active goal.
- Share of digest picks that are opened.

## Out of scope for version 1

Hosted multi-user service, billing, browser extension, Telegram Mini App, full-page archiving, highlights and spaced repetition, a public Telegram channel as a publishing target, an MCP server.

Each table carries an `owner_id`, so a later multi-user version does not need a schema rewrite.
