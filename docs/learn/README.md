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
