# Learning notes

These notes explain how Unshelve works and why it is built this way. Each note belongs to one part of the system. You do not need to know the part before you read its note.

## How to use them

1. Read the note **before** you read the code of that part.
2. Open the interactive page, if the note has one. Play with it for five minutes.
3. Do the exercise at the end. Each one takes 10 to 20 minutes.

## The notes, in reading order

| # | Note | What you learn | Interactive page |
|---|---|---|---|
| 1 | [The project skeleton](01-project-skeleton.md) | How six small programs start together, and how one program asks "are you ready?" | [System map](../architecture-explorer.html) |
| 2 | [Contracts and fakes](02-contracts-and-fakes.md) | How to build and test a part before the parts around it exist | |
| 3 | [The database schema](03-database-schema.md) | Why the most important rules live in the database, and how tests get their own database | |
| 4 | [The job queue](04-job-queue.md) | How slow work is done safely, also when a worker dies | [Job queue simulator](../queue-simulator.html) |
| 5 | [The worker loop](05-worker-loop.md) | What happens around one job: heartbeat, retry, timeout | [Job queue simulator](../queue-simulator.html) |
| 6 | [The API contract and the web shell](06-api-contract-and-web-shell.md) | How to plan every endpoint first, and why everything is locked by default | |
| 7 | [Stacked pull requests with git](07-stacked-pull-requests.md) | The few git commands that keep a chain of pull requests healthy, and the mistakes to avoid | |
| 8 | [Signing in with a one-time link](08-login.md) | How a link becomes a session, and why the database stores no secret | |
| 9 | [The walking skeleton](09-walking-skeleton.md) | How a saved link becomes a finished item, and why each step is safe to run two times | [Job queue simulator](../queue-simulator.html) |
| 10 | [The bot](10-bot-capture.md) | How a post in your channel becomes a stored message, and why no post is lost | |
| 11 | [The importer](11-export-importer.md) | How the old posts of a channel come in from an export, and why a post that the reader does not know is left out and counted | |
| 12 | [The resolver](12-resolver.md) | How a thing that you save two times stays one item, also when two workers see it at the same moment | |
| 13 | [Cards, a button, and the digest](13-cards-and-digest.md) | How the bot brings saved things back, why text from the internet is escaped, and what a press of a button does | |
| 14 | [The fetcher and its guard](14-fetcher-and-guard.md) | How a saved link is opened without opening a door into your own network | |
| 15 | [Reading an article](15-article-extractor.md) | How the bytes of a page become the clean text of its article, also in Persian | |
| 16 | [The fetch step](16-fetch-step.md) | How each saved link gets the best reader, a second try, or the saved text | |
| 17 | [Asking the model](17-model-card.md) | How the model writes the card of an item, why the same question is paid one time, and the daily budget | |
| 18 | [Vectors for search](18-vectors-for-search.md) | How a text becomes pieces and vectors, why a second run costs nothing, and how to change the model without a broken search | |
| 19 | [Reading a YouTube video](19-youtube-video.md) | How a bare link to a video becomes a title, a length and a description, and why the text holds no number of views | |
| 20 | [Reading a post on X](20-post-on-x.md) | How a post on X is read through two ways in, why one of them is a safety net, and what X does not give | |
| 21 | [Reading a GitHub repository](21-github-repository.md) | How the API of GitHub gives the facts and the whole README of a repository, and what happens at its limit | |
| 22 | [Reading a paper on arXiv](22-arxiv-paper.md) | How a link to a paper, also to its PDF, gives the title, the authors and the abstract, and how the code keeps the terms of a service | |
| 23 | [Reading a post on Telegram](23-telegram-post.md) | How a forwarded post gets its channel without a request, how a link gives the post, and why a second source can lie | |
| 24 | [Reading a PDF file](24-pdf-file.md) | How a PDF becomes text in a process of its own, how its limits were measured, and how Persian glyph forms become letters again | |
| 25 | [A file that you send](25-files-you-send.md) | How the worker downloads a file from Telegram once, keeps a copy, and keeps the bot token out of each log line and error | |
| 26 | [The page of an item](26-item-page.md) | How the web app shows one item, sends it back to the start, keeps Persian right to left, and is tested against a pretend API | |
| 27 | [The files of an export](27-export-files.md) | How the import copies the files of an export, why a file is known by its post and not by its path, and how a path is kept inside its folder | |
| 28 | [Reading an image](28-image.md) | How the model turns a photo or a screenshot into text that search can find, what one image costs, and why the type of an image comes from its bytes | |
| 29 | [Cards in a batch](29-cards-in-a-batch.md) | How the cards of a large import cost half, why a waiting card has no job, and how one question keeps one cache key | |
| 30 | [The reading status](30-reading-status.md) | How an item moves from the inbox to done, why each move and its event are one transaction, and how a lock keeps the queue at 5 slots | |
| 31 | [Goals](31-goals.md) | How a goal gets a slug that never changes, why only a new meaning gives a new version, and how one focus and 10 active goals are kept | |
| 32 | [Scores against your goals](32-scores-against-goals.md) | How a free vector prefilter saves questions, why one question scores all goals, and why a score with a label stays | |
| 33 | [Your verdicts, and the golden set](33-labels-and-golden-set.md) | How a verdict holds only for the score that you saw, which score the chat asks about next, and how the golden set stays reproducible | |
| 34 | [Choosing a digest](34-choosing-a-digest.md) | How a digest fills a time budget rule by rule, why its random slot is reserved first, and how a pure function and property tests keep the choice the same for the same seed | [Digest simulator](../digest-simulator.html) |
| 35 | [Sending the digest](35-sending-the-digest.md) | How one job sends the digest and the stale prompt each one time, how Archive and Keep work, and why the random pick asks for a verdict | |
| 36 | [Three retrievers](36-three-retrievers.md) | How full text, trigrams and vectors each find items, why a Persian word with a zero-width non-joiner needs trigrams, and how a measurement chose the trigram threshold | |
| 37 | [Fusion and metrics](37-fusion-and-metrics.md) | How reciprocal rank fusion joins three ranked lists, and how recall@k, MRR and nDCG@k measure a ranking: the formulas with worked examples, and how the code computes them | |
| 38 | [The search API](38-search-api.md) | How one request runs the three retrievers and fuses their lists, why a filter acts inside each query, what happens when the embedder fails, and how a snippet shows why an item was found, without HTML | |
| 39 | [An overview with citations](39-overview-with-citations.md) | How the model answers from the passages of the found items, why native citations cannot invent a quote, how each citation maps back to a chunk, and why the answer keeps no link or image | |
| 40 | [Measuring search](40-measuring-search.md) | How a golden set with grades, recorded vectors and a baseline measure each retriever and the fused list, and why the gate of CI fails on a rise too | |
| 41 | [The search page](41-search-page.md) | How the filters of a search live in the address, how each hit says why it was found and whether you read it, how the overview numbers its sources, and how one key on two siblings kept an old form on the page | |
| 42 | [The public feed](42-public-feed.md) | How the items that you make public become one feed.json, why a view and a role keep each private place out of it, also when the code has a mistake, and how a test with markers proves it | |
| 43 | [Publishing to a git repository](43-git-target.md) | How the worker commits the feed into the repository of your site, why each try starts from the server, what happens when two writers push at once, and how a deploy key and pinned host keys keep the push safe | |
| 44 | [Publishing to object storage](44-object-storage-target.md) | How the worker puts the feed into a bucket of R2 or S3, how a signature proves each request without the secret on the network, how the answers of AWS check our signature, and what the bucket needs from you | |
| 45 | [Publishing an item, with its preview](45-publishing-an-item.md) | How the preview of a public record is the publication itself, rolled back in a savepoint, why you publish exactly the record that you saw, and how an item keeps its public name and time | |
| 46 | [Searching the feed on the site](46-site-search.md) | How the feed gets a version and a schema, how the browser normalises text exactly as the server does, tested for each character of Unicode, how a component shows the search on a Next.js page, and what a site needs for it | |
| 47 | [Running Unshelve on a server](47-running-on-a-server.md) | How one short file of changes turns the local stack into a server stack, how Caddy gets the certificate and is the only open door, how CI runs each step of the quickstart on a fresh clone, and the bug that the first run found | |

The interactive pages are HTML files. GitHub shows their source code, not the page. Clone the repository and open the file in a browser.

## How these notes are written

Every note follows the same rules, so that it is fast to understand:

- An example with real values comes first. The general rule comes after it.
- Each technical word is explained the first time, in one plain sentence. Each note ends with a list of its words.
- One new idea in each section.
- An abstract idea gets a comparison with a thing from daily life.
- A picture is used when the subject is a flow, a sequence in time or a set of states.
- Each note ends with one exercise and with one to three links for more reading.

## For contributors

A pull request that adds a feature also adds or updates one note here. The note is reviewed together with the code. If you cannot explain the feature in simple words, the design is probably not clear enough yet.
