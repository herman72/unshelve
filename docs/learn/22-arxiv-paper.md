# 22. Reading a paper on arXiv

> Pull request: [#78](https://github.com/herman72/unshelve/pull/78) · Code: [`extract/arxiv.py`](../../apps/api/src/unshelve/extract/arxiv.py), [`net/pacing.py`](../../apps/api/src/unshelve/net/pacing.py), [`net/fetcher.py`](../../apps/api/src/unshelve/net/fetcher.py) (`SLOW_HOSTS`) · Tests: [`tests/extract/test_arxiv.py`](../../apps/api/tests/extract/test_arxiv.py), [`tests/extract/test_arxiv_live.py`](../../apps/api/tests/extract/test_arxiv_live.py), [`tests/net/test_pacing.py`](../../apps/api/tests/net/test_pacing.py)

## In one minute

A link to a paper often points to its PDF: `https://arxiv.org/pdf/1706.03762v7`. The article extractor reads no PDF, so the item kept only the link. Now the fetch step reads the paper from the API of arXiv. A real paper:

```text
Attention Is All You Need
A paper on arXiv by Ashish Vaswani, Noam Shazeer, Niki Parmar, …, from 12 June 2017.
Categories: cs.CL, cs.LG.
Comments: 15 pages, 5 figures

The dominant sequence transduction models are based on complex recurrent or …
```

| Field | Value |
|---|---|
| Kind | paper |
| Author | "Ashish Vaswani, Noam Shazeer, Niki Parmar and 5 more" |
| Minutes | 45: 15 pages, 3 minutes a page |
| Extra links | the PDF, and each link in the comments or the abstract (code, slides) |

## 1. One request, in Atom

```text
GET https://export.arxiv.org/api/query?id_list=1706.03762
```

The answer is Atom, a form of XML: one `<entry>` for each paper, with its title, its authors, its abstract, its first date and its categories. Each address of a paper (its page, its PDF, each version) has one canonical form ([note 12](12-resolver.md)), so one request serves all of them.

## 2. Three seconds between two requests

The terms of the API say: "make no more than one request every three seconds, and limit requests to a single connection at a time". The fetcher paces each host for one second ([note 14](14-fetcher-and-guard.md)). For this host it now waits three:

```text
SLOW_HOSTS = {"export.arxiv.org": 3.0}

request 1 to export.arxiv.org   at 0 s
request 2 to export.arxiv.org   at 3 s
request 1 to example.com        at 0 s   (another host does not wait)
```

The worker runs one job at a time, so there is one connection at a time.

**A comparison from daily life:** a library lets each reader take one book every three minutes. You keep that rule also when you could walk faster.

## 3. When the API does not answer

| What happens | Result |
|---|---|
| The API gives the paper | the full text |
| The API answers 429 or 503, does not answer in time, or gives no Atom | the article extractor reads the page of the abstract; the kind stays "paper" |
| The API does not know the paper | the page; a paper that does not exist ends with the 404 of its page, and the item keeps its saved text |

## 4. XML from the internet

XML can carry traps. One is the "billion laughs": a small entity that expands into gigabytes. Another is an entity that makes the parser read a local file. The standard library of Python reads XML with expat. The Python documentation says: "Expat versions lower than 2.7.2 may be vulnerable to the 'billion laughs', 'quadratic blowup' and 'large tokens' vulnerabilities". It also says that expat, by default, opens no local file and no network connection.

So the extractor checks the version of expat each time. With 2.7.2 or newer, it parses the XML. With an older one, it reads the page, and the worker writes a warning (`arxiv.old_expat`) to its log. A test checks the rule, and a second test sends a billion laughs: the parser refuses it, and the extractor reads the page. A comment at the parser says why it is safe; the linter's warning there is silenced on purpose.

The version is a fact of the computer, not of the code:

| Where | Python | expat |
|---|---|---|
| the worker image (Docker) | 3.13.16 | 2.8.5 |
| a Mac with Homebrew | 3.13.1 | 2.7.4 |
| CI (the Python that uv downloads) | 3.13.9 | 2.6.3 |

My first tests expected the API on each computer, and they failed on CI: there the guard did its work and read the page. The tests now set the version themselves, and they pass with the Python of CI too.

## 5. The minutes of a paper

The reading time of an abstract is one minute: wrong for a paper. Many comments name the pages ("15 pages, 5 figures"). A page of a paper has about 660 words, and the pipeline counts 220 words a minute, so a page takes about 3 minutes. Without a number of pages, the pipeline counts the time to read the abstract.

The text keeps the date of the first version. A new version of the paper gives it a new "updated" date; with that date in the text, each new version would make a new card.

## A choice that we made

| | The API, then the page (ours) | The page only | The PDF |
|---|---|---|---|
| What comes | title, all authors, abstract, categories, comments, journal, DOI | title and abstract, as trafilatura finds them | the whole paper |
| A link to the PDF | read through the API | not read: a PDF is no page | read |
| Cost | one small request, three seconds apart | one page | a large file; a PDF extractor (M2-2h) |

The PDF is the next step for the whole text of a paper, in package M2-2h.

## What a senior engineer checks here

- **Does the code keep the terms of a service?** Three seconds for this host: a test of the pacer, and a test of the pacer that the fetcher really builds.
- **Is XML from the internet read safely?** Only with expat 2.7.2 or newer, checked each time; else the page, and a warning in the log. A test sends a billion laughs.
- **Does a test depend on the computer that runs it?** My first tests did: they passed on a Mac and failed on CI, which has an older expat. A test that sets the fact itself passes everywhere.
- **Is a number in the text stable?** The first date, not the date of the latest version: a test.
- **Is a long list of authors cut?** 30 names in the text, 3 as the author: a test with 45 authors.

## Try it yourself

After the merge, run `git pull` and `make up`. Then:

```bash
docker compose exec worker unshelve extract https://arxiv.org/pdf/1706.03762v7
```

## Exercise (10 minutes)

1. Open `apps/api/src/unshelve/net/fetcher.py`.
2. In `__init__` of `LiveFetcher`, change `HostPacer(host_interval, slower=SLOW_HOSTS)` to `HostPacer(host_interval)`.
3. Run `uv run pytest tests/net/test_pacing.py -q` in `apps/api`.

Which test fails? Why is a test of the constant `SLOW_HOSTS` alone not enough?

<details>
<summary>The answer</summary>

`test_the_fetcher_paces_the_api_of_arxiv_as_its_terms_ask` fails: the pacer of the fetcher gives arXiv one second. A test of the constant would still pass, because the constant did not change: only the line that uses it changed. Test the thing that the code builds, not only the value that it should use.

</details>

## Words used in this note

| Word | Meaning |
|---|---|
| Abstract | The short summary at the start of a paper |
| Atom | A form of XML for lists of entries, such as feeds |
| Entity | A name in XML that stands for a text |
| Terms of use | The rules of a service for its users and programs |
| Version (of a paper) | A new upload of the same paper: v1, v2, … |

## To read more

- [Terms of Use for arXiv APIs](https://info.arxiv.org/help/api/tou.html), arXiv. Read the part about rate limits: the three seconds.
- [XML security](https://docs.python.org/3/library/xml.html#xml-vulnerabilities), the Python documentation. Read the part about expat 2.7.2 and the "billion laughs".

Previous: [21. Reading a GitHub repository](21-github-repository.md) · Next: [23. Reading a post on Telegram](23-telegram-post.md) · Back to the [list of notes](README.md)
