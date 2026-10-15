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

To see the data move, open [docs/architecture-explorer.html](docs/architecture-explorer.html) in a browser: it plays each flow through the system step by step. The same design is in [docs/architecture.drawio](docs/architecture.drawio) as editable diagrams.

New to the code? Start with the [learning notes](docs/learn/README.md): one short and simple note for each part of the system, each with examples and an exercise.

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

## Development

You need Docker, [uv](https://docs.astral.sh/uv/), Node.js 20.9 or newer and pnpm 10.

```bash
make setup   # install Python and Node dependencies
make up      # build and start the stack: web on :3000, API on :8000
make check   # lint, type check, tests and the API contract check
```

The stack runs with fake providers and recorded pages by default, so it needs no accounts and no keys. Copy `.env.example` to `.env` to connect a Telegram bot and real providers.

To sign in to the web app, set `UNSHELVE_OWNER_TELEGRAM_ID` in `.env` (for a local trial, any number works), run `make up` again, and ask for a link:

```bash
docker compose exec api unshelve auth link
```

Open the link that it prints. It works once, for 5 minutes. When a bot is connected, you can also write `/login` to the bot in its private chat. It answers you, and nobody else, with the same kind of link. `/digest` shows the things that you saved in the last 7 days, and one older thing, with a button "Done" for each. The bot also sends this digest by itself each week, on the day and at the hour of `UNSHELVE_DIGEST_DAY` and `UNSHELVE_DIGEST_HOUR` in `UNSHELVE_TIMEZONE`. [How the sign-in works](docs/learn/08-login.md) explains each step.

To save your first item and see the worker finish it, follow "Try it yourself" in [the walking skeleton note](docs/learn/09-walking-skeleton.md#try-it-yourself).

To capture from a Telegram channel, put the token of your bot into `.env`, make the bot an administrator of the channel, and add the id of the channel to `UNSHELVE_CAPTURE_CHAT_IDS`. [The bot note](docs/learn/10-bot-capture.md) explains each step and how to find the id.

The bot gets only new posts. To bring in the old posts of a channel, export the chat in Telegram ("Export chat history"), move the folder of the export into `exports/`, and import it. Give the time zone of the computer that made the export:

```bash
docker compose exec api unshelve import /imports/ChatExport_2026-10-13 --timezone Europe/Helsinki --dry-run
```

The dry run shows what the import would store. Run the command again without `--dry-run` to store the posts. It is safe to run two times. [The importer note](docs/learn/11-export-importer.md) explains each step.

The worker reads the page of each saved link when `.env` has `UNSHELVE_FETCHER=live`. With the default (`fixtures`) it has no web, and each link keeps its saved text. [The fetch step note](docs/learn/16-fetch-step.md) explains what happens to a link that cannot be read. With `UNSHELVE_PROVIDERS=live` and `ANTHROPIC_API_KEY`, Claude writes the card of each item: summary, key points and tags. Each answer is kept, so the same question is paid one time, and a daily budget (`UNSHELVE_LLM_DAILY_BUDGET_USD`) stops new calls. `unshelve llm check` makes one test call, and `unshelve llm costs` prints the costs. [The model note](docs/learn/17-model-card.md) explains it. To see what the worker does with one link, open it with the same fetcher. Nothing is stored, and an address inside your network is refused:

```bash
docker compose exec worker unshelve fetch https://example.com/
```

[The fetcher note](docs/learn/14-fetcher-and-guard.md) explains the guard. To see the text that Unshelve reads from a link, use `unshelve extract` in the same way: it chooses the reader as the worker does. [The article note](docs/learn/15-article-extractor.md) explains how a page becomes clean text, [the YouTube note](docs/learn/19-youtube-video.md) how a video becomes a title, a length and a description, [the X note](docs/learn/20-post-on-x.md) how a post on X is read, [the GitHub note](docs/learn/21-github-repository.md) how a repository is read, [the arXiv note](docs/learn/22-arxiv-paper.md) how a paper is read, and [the Telegram note](docs/learn/23-telegram-post.md) how a forwarded post or a link to a post is read.

With `UNSHELVE_EMBEDDER=voyage` and `VOYAGE_API_KEY`, Voyage AI makes the vectors for search: one for the card of each item, and one for each piece of a long text. The first 200 million tokens of a Voyage account are free. `unshelve embeddings check` makes one test call. When you change the model, `unshelve embeddings status`, `backfill` and `activate` give the old items their new vectors before search uses them. [The vectors note](docs/learn/18-vectors-for-search.md) explains it.

A thing that you save again does not make a second item. The item counts one more save, also when the second address looks different (`http`, `www.`, a tracking parameter, a `youtu.be` link). [The resolver note](docs/learn/12-resolver.md) explains the rules. After an update that changes these rules, run `docker compose exec api unshelve rekey` one time.

`make help` lists every target.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
