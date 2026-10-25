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
make eval    # measure search on the golden set: recall@10, nDCG@10 and MRR of each list
```

CI fails when the recall@10 or the nDCG@10 of search differs from `apps/api/evals/search/baseline.json`. `make eval-live` records the vectors of the golden set with the embedder of `.env`, and `make eval-baseline` writes the new numbers; [the note about measuring search](docs/learn/40-measuring-search.md) explains them.

The stack runs with fake providers and recorded pages by default, so it needs no accounts and no keys. Copy `.env.example` to `.env` to connect a Telegram bot and real providers.

To sign in to the web app, set `UNSHELVE_OWNER_TELEGRAM_ID` in `.env` (for a local trial, any number works), run `make up` again, and ask for a link:

```bash
docker compose exec api unshelve auth link
```

Open the link that it prints. It works once, for 5 minutes. When a bot is connected, you can also write `/login` to the bot in its private chat. It answers you, and nobody else, with the same kind of link. `/digest` shows what a digest would bring back now, and why, with a button "Done" for each item and "Right" or "Wrong" for its score. The bot also sends a digest by itself each week, on the day and at the hour of `UNSHELVE_DIGEST_DAY` and `UNSHELVE_DIGEST_HOUR` in `UNSHELVE_TIMEZONE`, for a reading time of `UNSHELVE_DIGEST_MINUTES`. After it, the bot asks about up to 5 old items of your inbox: archive or keep? [The note about sending the digest](docs/learn/35-sending-the-digest.md) explains both messages. `docker compose exec api unshelve search retrievers "words"` shows what each of the three retrievers of search finds in your items, and their fused list; [the retrievers note](docs/learn/36-three-retrievers.md) explains them. While you are signed in on the web app, `http://localhost:3000/api/v1/search?q=words` shows the answer of the search API, with a snippet that tells why each item was found; [the search API note](docs/learn/38-search-api.md) explains it. The page `http://localhost:3000/search` shows the same search with filters, a mark on each item that you read, and an overview when you ask for one; [the search page note](docs/learn/41-search-page.md) explains it. `docker compose exec api unshelve publish show` prints the feed of the items that you made public, and `unshelve publish run` writes it to the target of `UNSHELVE_PUBLISH_TARGET`; [the public feed note](docs/learn/42-public-feed.md) explains how no private text can reach it. With `UNSHELVE_PUBLISH_TARGET=git`, the worker commits the feed into the repository of your site and pushes it, with a deploy key; [the git target note](docs/learn/43-git-target.md) explains it. With `UNSHELVE_PUBLISH_TARGET=s3`, it puts the feed into a bucket of an object storage with the S3 API, such as Cloudflare R2; [the object storage note](docs/learn/44-object-storage-target.md) explains it. `GET /api/v1/items/{id}/public-preview` shows the exact public record of an item, and `PUT /api/v1/items/{id}/visibility` publishes that record and nothing else; [the note about publishing an item](docs/learn/45-publishing-an-item.md) explains why. `docker compose exec api unshelve search overview "a question"` answers from your items, with a number for each citation; with `UNSHELVE_PROVIDERS=live` it is one call of the model, and the same question again costs nothing. [The overview note](docs/learn/39-overview-with-citations.md) explains it. [How the sign-in works](docs/learn/08-login.md) explains each step.

To save your first item and see the worker finish it, follow "Try it yourself" in [the walking skeleton note](docs/learn/09-walking-skeleton.md#try-it-yourself).

On the page of an item, a button for each allowed move changes its reading status: into the queue, to reading, done, or archived. The reading queue has 5 slots, and the page "Queue" shows them. The section "History" shows each move. [The reading status note](docs/learn/30-reading-status.md) explains the rules.

The page "Goals" holds what you work on now: add, change, archive and focus a goal there. [The goals note](docs/learn/31-goals.md) explains slugs, versions and the focus. Each new item gets a score from 0 to 3 for each goal, with a reason; an item far from each goal gets 0 without a question to the model. After you add or change a goal, `docker compose exec api unshelve goals rescore` scores the old items: it shows the price first. [The scores note](docs/learn/32-scores-against-goals.md) explains it. Tell Unshelve when a score is right or wrong: with `/label` in the private chat of the bot, or under each score on the page of an item. `docker compose exec -T api unshelve labels export > exports/golden-set.jsonl` writes your verdicts as an evaluation set. [The verdicts note](docs/learn/33-labels-and-golden-set.md) explains it. The page "Digest" shows what a digest would bring back now, for a reading time that you choose, and why each item is in it: your focus goal first, then your other goals, items that you saved again, recent items and one random pick. [The digest note](docs/learn/34-choosing-a-digest.md) explains the rules, and [the digest simulator](docs/digest-simulator.html) shows them step by step.

To capture from a Telegram channel, put the token of your bot into `.env`, make the bot an administrator of the channel, and add the id of the channel to `UNSHELVE_CAPTURE_CHAT_IDS`. [The bot note](docs/learn/10-bot-capture.md) explains each step and how to find the id.

The bot gets only new posts. To bring in the old posts of a channel, export the chat in Telegram ("Export chat history"), move the folder of the export into `exports/`, and import it. Give the time zone of the computer that made the export:

```bash
docker compose exec api unshelve import /imports/ChatExport_2026-10-13 --timezone Europe/Helsinki --dry-run
```

The dry run shows what the import would store. Run the command again without `--dry-run` to store the posts. It is safe to run two times. [The importer note](docs/learn/11-export-importer.md) explains each step.

For a large export, batch mode makes the cards cost half: [the batch note](docs/learn/29-cards-in-a-batch.md) explains it. The import also copies the files of the export (photos, PDFs and other files, 50 MB at most each) into the blob store. After the import, you can delete the folder. To give the posts of an earlier import their files, run the import again: [the export files note](docs/learn/27-export-files.md) explains it.

The worker reads the page of each saved link when `.env` has `UNSHELVE_FETCHER=live`. With the default (`fixtures`) it has no web, and each link keeps its saved text. [The fetch step note](docs/learn/16-fetch-step.md) explains what happens to a link that cannot be read. With `UNSHELVE_PROVIDERS=live` and `ANTHROPIC_API_KEY`, Claude writes the card of each item: summary, key points and tags. Each answer is kept, so the same question is paid one time, and a daily budget (`UNSHELVE_LLM_DAILY_BUDGET_USD`) stops new calls. `unshelve llm check` makes one test call, and `unshelve llm costs` prints the costs. [The model note](docs/learn/17-model-card.md) explains it. To see what the worker does with one link, open it with the same fetcher. Nothing is stored, and an address inside your network is refused:

```bash
docker compose exec worker unshelve fetch https://example.com/
```

[The fetcher note](docs/learn/14-fetcher-and-guard.md) explains the guard. To see the text that Unshelve reads from a link, use `unshelve extract` in the same way: it chooses the reader as the worker does. With `--file`, it reads an image or a PDF of this computer as a file that you sent. The model of `UNSHELVE_PROVIDERS` reads an image: with `live`, that is a paid call. [The article note](docs/learn/15-article-extractor.md) explains how a page becomes clean text, [the YouTube note](docs/learn/19-youtube-video.md) how a video becomes a title, a length and a description, [the X note](docs/learn/20-post-on-x.md) how a post on X is read, [the GitHub note](docs/learn/21-github-repository.md) how a repository is read, [the arXiv note](docs/learn/22-arxiv-paper.md) how a paper is read, [the Telegram note](docs/learn/23-telegram-post.md) how a forwarded post or a link to a post is read, [the PDF note](docs/learn/24-pdf-file.md) how a PDF file is read, [the files note](docs/learn/25-files-you-send.md) how a file that you send is downloaded once and kept, [the item page note](docs/learn/26-item-page.md) how the web app shows one item and sends it back to the start, [the export files note](docs/learn/27-export-files.md) how the files of an export are copied, and [the image note](docs/learn/28-image.md) how the model reads a photo or a screenshot.

With `UNSHELVE_EMBEDDER=voyage` and `VOYAGE_API_KEY`, Voyage AI makes the vectors for search: one for the card of each item, and one for each piece of a long text. The first 200 million tokens of a Voyage account are free. `unshelve embeddings check` makes one test call. When you change the model, `unshelve embeddings status`, `backfill` and `activate` give the old items their new vectors before search uses them. [The vectors note](docs/learn/18-vectors-for-search.md) explains it.

A thing that you save again does not make a second item. The item counts one more save, also when the second address looks different (`http`, `www.`, a tracking parameter, a `youtu.be` link). [The resolver note](docs/learn/12-resolver.md) explains the rules. After an update that changes these rules, run `docker compose exec api unshelve rekey` one time.

`make help` lists every target.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
