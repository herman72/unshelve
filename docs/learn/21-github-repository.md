# 21. Reading a GitHub repository

> Pull request: [#77](https://github.com/herman72/unshelve/pull/77) · Code: [`extract/github.py`](../../apps/api/src/unshelve/extract/github.py), [`extract/registry.py`](../../apps/api/src/unshelve/extract/registry.py) · Tests: [`tests/extract/test_github.py`](../../apps/api/tests/extract/test_github.py), [`tests/extract/test_github_live.py`](../../apps/api/tests/extract/test_github_live.py)

## In one minute

The article extractor already read the page of a repository. So what does an extractor of its own add? A real repository, `pgvector/pgvector`:

| | The article extractor (before) | The GitHub extractor (now) |
|---|---|---|
| Kind of the item | article | repo |
| Title | "GitHub - pgvector/pgvector: Open-source vector …" | "pgvector/pgvector: Open-source vector …" |
| Text | 32,712 characters, from the HTML | 42,255 characters: the whole README, as its author wrote it |
| Facts | none | "A repository on GitHub, in C. Topics: approximate-nearest-neighbor-search, …" |
| Date | none | the day the repository was made: 20 April 2021 |

The kind "repo" lets the web app and the digest show repositories apart from articles.

## 1. Two requests to the API, and the page as a safety net

```text
GET https://api.github.com/repos/pgvector/pgvector           the facts, as JSON
GET https://api.github.com/repos/pgvector/pgvector/readme    the README, as base64 in JSON
```

The API answers without a key, but it allows only 60 requests an hour from one address. When it does not answer, the extractor gives the work to the article extractor, on the page `https://github.com/pgvector/pgvector`. The page shows the same README. If the page also fails, its error goes to the fetch step: a 404 keeps the saved text, a 503 tries again later.

| What happens | Result |
|---|---|
| The facts and the README come | the full text |
| The repository has no README (404 for the README) | the facts only |
| The API answers 403 (its limit), 429, 503, a timeout, or JSON in a new form | the page, read by the article extractor, kind "repo" |
| The repository does not exist | the 404 of the page: the item keeps its saved text |

**A comparison from daily life:** you ask the librarian for the record of a book. If the librarian is busy, you read the back cover of the book yourself.

## 2. Which links are repositories

Only the page of a repository: `github.com/owner/name`, also with `.git`, a slash at the end, `?tab=readme-ov-file` or `#readme`. The resolver gives each of them one canonical form ([note 12](12-resolver.md)). An issue, a pull request, a file or a folder is a different page, and the article extractor reads it, as before.

Some pages of GitHub also have two parts in their path, such as `github.com/topics/postgres`. GitHub keeps such names for itself, so no user can have them. The extractor has a list of them.

## 3. A README without its noise

A README is Markdown, often with HTML in it:

```text
<!-- A comment -->
<p align="center"><img src="logo.png"></p>
[![CI](…/badge.svg)](…/actions) [![PyPI](…/pypi/v/queue-kit.svg)](…)
```

A badge is a small picture with a status, such as "tests: passing". It says nothing to the model, and its address is noise in a vector. The extractor removes comments, pictures, badges and empty paragraphs. The words, the headings, the code and the real links stay.

The text has no stars and no forks: they change each day ([note 19](19-youtube-video.md), part 4).

## A choice that we made

| | The API, then the page (ours) | The page only | The API with a key |
|---|---|---|---|
| The README | complete, as its author wrote it | as trafilatura reads the HTML: 77 % of it for pgvector | complete |
| The facts | language, license, topics, date | none | the same |
| The limit | 60 requests an hour; then the page | none | 5,000 requests an hour |
| A secret to keep | none | none | a key; and the fetcher must never send it to another host |

A key would raise the limit, but the fetcher opens saved links: a key in its requests could go to a host that a redirect names. The pages of your saved repositories are few, so 60 an hour is enough, and the page is the safety net.

## What a senior engineer checks here

- **What happens at the limit of the API?** The page: a test for 403, 429, 503 and a timeout.
- **Is the README read exactly?** The base64 text of GitHub has a line break after each 60 characters; the fixture has them too, and the decoder skips them.
- **Is a repository told apart from other GitHub pages?** In a test, nine links and a note are no repository.
- **Is the text stable?** No stars, no forks: a test.

## Try it yourself

After the merge, run `git pull` and `make up`. Then:

```bash
docker compose exec worker unshelve extract https://github.com/pgvector/pgvector
```

## Exercise (10 minutes)

1. Open `apps/api/src/unshelve/extract/github.py`, and find `_NOISE`.
2. Remove its fourth pattern, the one for an empty link: `\[\s*\]\([^)]*\)`.
3. Run `uv run pytest tests/extract/test_github.py -q` in `apps/api`.

What stays in the text, and why does the pattern for a picture alone not remove a badge?

<details>
<summary>The answer</summary>

`test_the_readme_keeps_its_words_and_loses_its_badges_logos_and_comments` fails. A badge is a picture inside a link: `[![CI](badge.svg)](actions)`. The pattern for a picture removes the inner part, `![CI](badge.svg)`, and leaves an empty link: `[](actions)`. The fourth pattern removes that empty link. The order of the patterns matters: the picture first, then the empty link.

</details>

## Words used in this note

| Word | Meaning |
|---|---|
| Repository | The project of code on GitHub, with its files and its README |
| README | The first page of a repository: what it is and how to use it |
| REST API | Addresses that give data as JSON, for programs |
| Base64 | A way to write bytes as letters and digits, for JSON |
| Badge | A small picture with a status, at the top of a README |
| Rate limit | The most requests that a service allows in a time |

## To read more

- [Get a repository](https://docs.github.com/en/rest/repos/repos#get-a-repository), GitHub REST API. Read the example answer: it has each field that the extractor reads.
- [Rate limits for the REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api), GitHub. Read "Primary rate limit for unauthenticated users": the 60 requests an hour.

Previous: [20. Reading a post on X](20-post-on-x.md) · Back to the [list of notes](README.md)
