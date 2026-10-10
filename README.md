# Unshelve

**A goal-aware reading inbox.** Save things where you already save them. Unshelve tells you which ones matter for your plan, and brings them back until you read them.

> Status: early development (milestone M0). Nothing here is ready to use yet.

## Why

Most "read it later" tools solve storage. Storage is not the problem. The problem is that you never go back.

Unshelve is built around that:

- **No new habit.** You keep sending links, posts and screenshots to your private Telegram channel. A bot picks them up.
- **Goal-aware.** You describe what you are working towards. Each saved item gets a relevance score and a reason for each goal.
- **It pushes.** A weekly digest brings back a few items for your current goal, sized to a time budget.
- **It remembers.** Items move from inbox to done. Nothing is deleted.
- **Private by default, public by choice.** You pick which items appear on your own site. Private items cannot leak, because the public feed is built from a separate, restricted view.

## How it works

```
Capture → Enrich → Triage → Resurface → Track → Retrieve → Publish
```

| Stage | What happens |
|---|---|
| Capture | A Telegram bot receives each post in your channel. A one-time import loads the old history. |
| Enrich | The content is fetched and summarised. Tags, type, language and reading time are added. |
| Triage | Each item is scored against your goals, with a reason you can confirm or reject. |
| Resurface | A weekly digest proposes what to read next. |
| Track | `inbox → queued → reading → done`, or `archived`. |
| Retrieve | Hybrid search (keyword + trigram + vector) and an overview answer with citations. |
| Publish | A static feed of the items you made public, ready for client-side search on your site. |

## Architecture in one paragraph

A Python service (FastAPI API, worker, Telegram bot) on Postgres with pgvector, and a Next.js web app. Postgres is also the job queue. The bot uses long polling, so a self-hosted instance needs no inbound port for capture. Docker Compose is the canonical way to run it.

More detail: [docs/architecture.md](docs/architecture.md), [docs/product.md](docs/product.md), and the decision records in [docs/adr](docs/adr/README.md).

## Roadmap

| Milestone | Result |
|---|---|
| M0 Foundations | Repository, schema, contracts, job queue, walking skeleton |
| M1 Capture | Every channel post lands in the database once; first digest |
| M2 Enrich | Summaries, tags and embeddings for each item |
| M3 Track, Triage, Resurface | Statuses, goals, relevance scores, weekly digest |
| M4 Search | Hybrid search, overview answers, evaluation harness |
| M5 Publish | Public feed and site search |
| M6 Ship | Deployment, backups, observability, documentation |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
