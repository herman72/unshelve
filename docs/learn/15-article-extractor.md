# 15. Reading an article: from bytes to clean text

> Pull request: [#67](https://github.com/herman72/unshelve/pull/67) · Code: [`extract/article.py`](../../apps/api/src/unshelve/extract/article.py), [`extract/html.py`](../../apps/api/src/unshelve/extract/html.py) · Tests: [`tests/extract/`](../../apps/api/tests/extract/)

## In one minute

The fetcher of [note 14](14-fetcher-and-guard.md) brings the bytes of a page. The article extractor turns these bytes into the text of the article: without the menu, the footer and the comments. It also finds the title, the author, the date and the language of the page.

It works in two steps:

1. It turns the bytes into letters. For this it must know the encoding of the page (part 2).
2. It finds the main text among all the text of the page (part 1). The library trafilatura does this step.

Two Persian test pages found two problems in the libraries. Parts 2 and 4 show them.

## 1. Most of a page is not the article

A test page has these parts:

```text
nav       Home  About  Sign in
header    Queues in Postgres, by Ada Example
article   A job queue in Postgres needs one statement ...      ← this is wanted
comments  Great post! I use this in production.
aside     Popular posts: Ten tips for your database
footer    Copyright 2026. Subscribe to our newsletter.
```

The extractor gives only the article. Headings and lists stay, in Markdown:

```text
A job queue in Postgres needs one statement that many workers can run at the same time ...

## Why SKIP LOCKED

Without SKIP LOCKED, the second worker waits for the lock of the first one. ...

- One row for each job.
- A lease with a time limit, so that a job of a dead worker comes back.
```

Headings and lists help the model of milestone M2-4 to write a summary, and they help a person who reads the text.

trafilatura decides with its own rules: it removes parts that it knows (navigation, footers, comments), and it looks for the elements that have much text and few links. Two other known algorithms, jusText and readability, are its fallbacks.

A real run: the article [Event Sourcing](https://martinfowler.com/eaaDev/EventSourcing.html) by Martin Fowler gave 6,241 words, the author and the date (12 December 2005) in 0.51 s.

**A comparison from daily life:** you read a newspaper page. Your eyes skip the advertisements and the list of other articles, and they follow the columns of the one article.

## 2. Bytes become letters: which encoding?

A page arrives as bytes. The same bytes mean different letters in different encodings. Here is a Persian title in the encoding windows-1256. trafilatura got the bytes, guessed a wrong encoding, and gave this title:

```text
ћ” ћж? »—ѕ«—? ѕ— Б” Р—”         (letters of the Cyrillic alphabet)
جستجوي برداري در پستگرس          (the correct title)
```

The page named its encoding in a meta tag, but the guess did not use it. Old Iranian sites still use windows-1256. So the extractor decodes the bytes itself, and it gives trafilatura letters, not bytes.

The rules come in the order of the HTML standard:

| Rule | Example |
|---|---|
| 1. A byte order mark at the start | the bytes `EF BB BF` mean UTF-8 |
| 2. The charset in the HTTP header | `Content-Type: text/html; charset=windows-1256` |
| 3. A meta tag near the start of the page | `<meta charset="windows-1256">` |
| 4. UTF-8 | most pages today |
| 5. A guess | the library charset-normalizer reads the bytes and guesses |

Two details:

- A page that says `iso-8859-1` (latin-1) is read as windows-1252, as browsers do. In windows-1252 the bytes `0x93` and `0x94` are quotation marks. In latin-1 they are control characters.
- A page cannot choose a codec of Python that is no encoding of a page. Python knows a codec `undefined`, which always fails, and `unicode-escape`, which reads `A` as `A`. Such a name is left out, and the next rule reads the page.

**A comparison from daily life:** a letter in a foreign alphabet. Before you read it, you must know the alphabet. The envelope (the HTTP header) can say it. If the envelope says nothing, the first line of the letter (the meta tag) can say it.

## 3. Two spellings of one letter

The encoding windows-1256 has no Persian ye (`ی`, U+06CC). So old Persian pages write the Arabic ye (`ي`, U+064A) in its place. Our test page does the same.

The extractor keeps the text as the page wrote it. The database joins the two spellings for search: the function `u_norm()` of [note 3](03-database-schema.md) turns `ي` into `ی`.

## 4. The invisible letter that trafilatura removed

Persian needs a character that you cannot see: the zero-width non-joiner (ZWNJ, U+200C). It stops two letters from joining:

```text
همسایه‌ها     with the ZWNJ: correct
همسایهها      without it: the letters join, and the word looks wrong
```

The test of the Persian page failed: each ZWNJ was gone. The cause is in trafilatura. It keeps only the characters that Python calls printable (`str.isprintable()`), and Python does not call the ZWNJ printable. The zero-width joiner (ZWJ, U+200D), which holds emoji such as 👨‍👩‍👧 together, has the same problem.

A measurement on a real page: on the Persian Wikipedia page about PostgreSQL, trafilatura alone removed all 288 ZWNJ.

The fix is a swap. Before trafilatura reads the page, each ZWNJ and each ZWJ becomes a rare printable symbol: `𝌆` and `𝌇`, from the block "Tai Xuan Jing Symbols". After trafilatura, the symbols become joiners again. With the swap, the same page keeps all 288.

If a page already has one of these symbols, there is no swap. Then the page loses its joiners, but no real symbol becomes a joiner by mistake. A test checks this case too.

**A comparison from daily life:** you send a box through a sorting machine. The machine throws away each small thing without a label. So you put a label on the small things before the machine, and you take the labels off after it.

## 5. When a page is no article

Some pages have almost no text:

```text
unshelve extract https://x.com/     No article: the page has only 106 characters of text.
```

Such a page is a frame for JavaScript, a sign-in wall or an error page. Its one sentence ("You need to enable JavaScript to run this app.") is no article. With less than 200 characters, the extractor gives up with an `ExtractError`. Then package M2-3 keeps the text of the saved message, and that text is better than the sentence.

An error of the fetcher goes up unchanged: a 404, a 429 with its wait, a timeout. Package M2-3 decides between "try again" and "keep the saved text".

## 6. Which links it claims

Each extractor gives a score to a link. The highest score wins:

| Link | Score of the article extractor |
|---|---|
| no link | 0 |
| `ftp://…` | 0 |
| a file: `.pdf`, `.jpg`, `.mp4`, `.zip` | 0 |
| any other web page | 10 |

The extractors for one site (YouTube, GitHub, arXiv) come in the next packages. They give 100 to their own links, so they win over the article extractor.

## 7. A slow reading does not stop the worker

trafilatura needs up to a second of processor time for a long page. The worker sends the heartbeat of its job from the same event loop ([note 5](05-worker-loop.md)). A reading that holds the event loop also holds the heartbeat, and then the lease of the job can end.

So the reading runs in a thread (`asyncio.to_thread`). A test proves it: a small task counts every 0.02 s while a slow reading runs. It counts about 15 times. Without the thread it would count 0 times.

## A choice that we made

How do we find the main text of a page?

| | trafilatura (ours) | readability | Ask the model |
|---|---|---|---|
| What it gives | text, title, author, date, site | text and title | what you ask for |
| Cost | free, on this computer | free, on this computer | money for each page |
| Time | 0.2 s to 0.9 s for the real pages, with the download | similar | seconds |
| Risk | rules can miss a strange page | the same | the page can give the model instructions |

The documentation of trafilatura lists who uses it, for example the Dolma toolkit for the language model OLMo and the RefinedWeb dataset for the language model Falcon. So the main text of web pages for training data often comes from this library.

## What a senior engineer checks here

- **Who decodes the bytes, and with which rules?** The extractor, with the rules of the HTML standard. A library that guesses is the last rule, not the first.
- **What does a library remove without a word?** The test of a Persian page found it, and a real page measured it: 288 of 288 ZWNJ. A test for each script that you serve finds such things.
- **What does a page that is no article give?** An error, and not a sentence about JavaScript.
- **Can a page choose something dangerous?** A codec name such as `undefined` or `unicode-escape` is left out.
- **Does work for the processor stop the event loop?** No. It runs in a thread, and a test proves it.
- **Was it tried with real pages?** Yes: two English pages, two Persian pages, an app frame (x.com) and a link to a PDF (`Not fetched: unsupported`).
- **Can the tests fail?** I broke the new code in 27 places, one at a time. The tests found 25. The two misses taught something:
  - A catch for an error that no codec of Python gives. A test of each codec showed it, and I removed the catch.
  - The setting `include_comments=False`. trafilatura puts comments into a field of their own, and the extractor does not read that field. So the setting changes nothing in the text, and no test can see it. It stays, because it saves work.
- **What is not done:**
  - The pipeline does not use the extractor yet. Package M2-3 chooses the extractor of each item and calls it.
  - The text of a Wikipedia page keeps small marks: a footnote is `<sup>\[1\]</sup>`.
  - The language comes only from `<html lang="...">`. The page of Martin Fowler names none.
  - The date of a home page is a guess. The home page of BBC Persian got a date of 2020.

## Try it yourself

Do this after the pull request is merged.

1. Run `git pull`, and then `make up`.
2. Read an English article:

```bash
docker compose exec worker unshelve extract https://martinfowler.com/eaaDev/EventSourcing.html
```

You see the title, the site, the date, the author, the number of words, and the start of the text.

3. Read a Persian page:

```bash
docker compose exec worker unshelve extract "https://fa.wikipedia.org/wiki/پستگرس‌کیوال"
```

The title keeps its ZWNJ: `پستگرس‌کیوال - ویکی‌پدیا، دانشنامهٔ آزاد`.

4. Try a page that is no article: `docker compose exec worker unshelve extract https://x.com/`.

## Exercise (10 minutes)

Turn off the swap of part 4, and see which tests find it.

1. Open `apps/api/src/unshelve/extract/article.py`, and go to the function `_read`.
2. Change `swap = not any(symbol in html for symbol in _JOINERS.values())` to `swap = False`.
3. Run the tests of the extractor:

```bash
cd apps/api && uv run pytest tests/extract/test_article.py -q
```

4. Read the names of the tests that fail. Then put the line back with the undo of your editor.

Then answer this question: in the output of a failing test, the two Persian lines look almost the same. What do the `-` marks under them point at?

<details>
<summary>The answer</summary>

Two tests fail, and 25 pass:

```text
FAILED tests/extract/test_article.py::test_a_persian_page_in_an_old_encoding_is_read_correctly
FAILED tests/extract/test_article.py::test_the_joiners_of_persian_and_of_emoji_survive
```

The second one shows this difference:

```text
- نزديک‌ترين همسايه‌ها
?      -           -
+ نزديکترين همسايهها
```

The `-` marks point at the two places where a ZWNJ was. You cannot see the character, but the text is different: the letters join. A test that compares text sees what your eyes do not see. So check text with a test, and not by looking at it.

</details>

## Words used in this note

| Word | Meaning |
|---|---|
| Encoding | The rule that turns bytes into letters, such as UTF-8 or windows-1256 |
| Byte order mark | A few bytes at the start of a text that name its encoding |
| Boilerplate | The parts of a page that are on each page of a site: menu, footer, lists of other articles |
| ZWNJ | Zero-width non-joiner, U+200C: an invisible character that stops two letters from joining |
| ZWJ | Zero-width joiner, U+200D: an invisible character that holds the parts of some emoji together |
| Event loop | The part of an async program that runs the tasks one after another |
| Thread | A second line of work in the same program, so that slow work does not stop the event loop |

## To read more

- [Trafilatura](https://trafilatura.readthedocs.io/en/latest/), the documentation. Read the page "Benchmarks and evaluation" for comparisons with readability and jusText.
- [Determining the character encoding](https://html.spec.whatwg.org/multipage/parsing.html#determining-the-character-encoding), the HTML Standard. It lists the rules of part 2 in their order.

Previous: [14. The fetcher and its guard](14-fetcher-and-guard.md) · Next: [16. The fetch step](16-fetch-step.md)
