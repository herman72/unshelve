# 28. Reading an image

> Pull request: [#86](https://github.com/herman72/unshelve/pull/86) · Code: [`extract/image.py`](../../apps/api/src/unshelve/extract/image.py), [`extract/registry.py`](../../apps/api/src/unshelve/extract/registry.py), [`pipeline/items.py`](../../apps/api/src/unshelve/pipeline/items.py) (`fetch_item`), [`cli.py`](../../apps/api/src/unshelve/cli.py) (`extract --file`) · Tests: [`tests/extract/test_image.py`](../../apps/api/tests/extract/test_image.py), [`tests/pipeline/test_fetch.py`](../../apps/api/tests/pipeline/test_fetch.py), [`tests/test_cli.py`](../../apps/api/tests/test_cli.py)

## In one minute

You send a photo of a whiteboard to your channel, with the caption "The whiteboard of today". These are the values of a test, with a pretend model:

| | Before | Now |
|---|---|---|
| The kind of the item | note | image |
| The text of the item | `The whiteboard of today` | the caption, then what the model read (below) |
| Search finds | the words of the caption | also the words on the whiteboard |

```text
The whiteboard of today

An image: A whiteboard with a diagram of a job queue and its workers.

Text in the image:
SKIP LOCKED
lease: 30 s
```

A real model writes its own words. The card of the item ([note 17](17-model-card.md)) is then written from this text, as for each other item.

## 1. Why the extractor asks the model

Two steps could send the image to the model:

- **The extractor (ours).** The model reads the image in the fetch step, and its words become the text of the item. So full-text search, the vectors ([note 18](18-vectors-for-search.md)) and the card all see the words of a screenshot. This costs two questions for each image item: the reading, then the card.
- **The card step.** One question: the model sees the image and writes the card at once. But the text of the item stays the caption, so a search cannot find the words in a screenshot.

The search of M4 needs the words, so the extractor asks.

## 2. Which messages are images

| The message | Who reads it |
|---|---|
| A photo or a screenshot, without a link | the image extractor (score 30) |
| A photo with a link in its caption | the extractor of the link: the page is the thing that you saved, and the photo is mostly its cover |
| A forwarded post with a photo | the image extractor; the source and the date of the forward stay (as in [note 23](23-telegram-post.md)) |
| A document with an image type (`Screenshot.PNG`) | the image extractor |
| A sticker, a video, an animation | not an image: the saved text |
| An album of 10 photos | one question with the 10 images, each with its number: `Image 1:`, `Image 2:`, … |

The images come first in the question, and the request "Read the images." comes after them. The Vision docs say that Claude works best with this order. The caption is not sent: the card reads it later, with the words of the image. So the same photo with another caption is the same question, and the cache answers it.

## 3. The type comes from the first bytes

The model reads four types of image. The extractor looks at the first bytes of the file, not at its name:

| The first bytes | Type |
|---|---|
| `FF D8 FF` | JPEG |
| `89 50 4E 47 0D 0A 1A 0A` (`.PNG` and line ends) | PNG |
| `GIF87a` or `GIF89a` | GIF |
| `RIFF`, four bytes, then `WEBP` | WebP |
| anything else, for example `ftypheic` of an iPhone photo | left out |

Why not the name or the type that came with the file? A photo of Telegram comes with no type at all. A name can be wrong: a file `shot.png` can hold the bytes of a JPEG. The API refuses an image whose declared type is wrong, so the extractor sends the type of the bytes.

**A comparison from daily life:** at the border, an officer opens a box and looks inside. The label on the box can be wrong; the content cannot.

## 4. What a photo costs

The model sees an image in squares of 28 × 28 pixels, and each square is one visual token: `⌈width / 28⌉ × ⌈height / 28⌉`. Claude Haiku 5.5 is in the high-resolution group of the Vision docs (Claude 4.7 and later models): at most 2,576 pixels on the long edge and 4,784 visual tokens. The API makes a larger image smaller by itself.

| The image | Visual tokens | Claude Haiku 5.5 ($0.10 a million) | Claude Opus 5.5 ($4 a million) |
|---|---|---|---|
| The screenshot of note 26, 1100 × 956 | 40 × 35 = 1,400 | $0.00014 | $0.0056 |
| The Persian phone screenshot of note 26, 390 × 844 | 14 × 31 = 434 | $0.00004 | $0.0017 |
| A photo of Telegram, 1280 × 960 | 46 × 35 = 1,610 | $0.00016 | $0.0064 |
| A photo of Telegram, 2560 × 1920 | about 4,700 (made smaller) | $0.00047 | $0.019 |

The answer adds about 100 to 1,000 output tokens: at most $0.0005 with Haiku. These are estimates from the prices and the rule of the docs; the ledger keeps the real numbers.

The live check of 16 October sent the two screenshots of note 26 to Claude Haiku 5.5, through the ledger of the instance:

| Screenshot | Input tokens | Output tokens | Price | Time |
|---|---|---|---|---|
| English, 1100 × 956 | 1,858 = 1,400 of the image + about 460 | 389 | $0.00038 | 5.2 s |
| Persian, 390 × 844 | 892 = 434 of the image + about 460 | 305 | $0.00024 | 3.0 s |

The rule of the docs gave the tokens of each image exactly. The other 460 tokens are the same for each question: the system prompt and the JSON schema of the answer. So one small image costs more for its prompt than for its pixels.

What the model read was right: each word of the two pages, and the Persian in Persian, in the order of reading. Two small faults: the model added a full stop after «هر کار یک اجاره دارد», and it wrote the English page as one line, without its line breaks.

A question is paid one time. The cache key holds the SHA-256 of each image ([note 17](17-model-card.md), part 3), so "Process again" on the page of the item costs nothing for the same photo. A test runs the item two times and counts one question.

## 5. When the model fails

The extractor gives each error of the model to the fetch step unchanged. The fetch step then does what the card step does ([note 17](17-model-card.md), part 5):

| Group | Examples | What happens |
|---|---|---|
| It can pass | 429, 529 (busy), the daily budget is used | the fetch step waits, then tries again |
| A person must fix it | 401 (key), 402 (no credit) | the fetch step waits in the same way |
| This image cannot be read | the model declines, an answer that does not fit | the item keeps its caption; the event says `"reason": "model"` |

A file that cannot be read (too large, no bot token) is left out, and the other images of the album are read. When no image is left, the reason of the last one goes to the event.

## A choice that we made

Where does the model read the image?

| | In the extractor (ours) | In the card step |
|---|---|---|
| Questions for each image item | two: the reading and the card | one |
| Text of the item | the caption and the words of the image | only the caption |
| Search and vectors | find the words of a screenshot | do not |
| A change of the card prompt | the reading stays in the cache | each image is paid again |

## What a senior engineer checks here

- **Does a second run pay again?** No. A test sends the item back to the start and counts one question to the model.
- **Can text in an image take control?** The same three limits as for a page ([note 17](17-model-card.md), part 2): the system prompt says that the images are data, the answer must fit a schema, and the call has no tools.
- **Does the model get a type that it cannot read?** No. The type comes from the bytes, and a test sends the bytes of a JPEG under the name `shot.png`.
- **Does a photo leave the computer?** With `UNSHELVE_PROVIDERS=live`, yes: it goes to the Anthropic API, as the text of each item does. The Vision docs say that Anthropic does not use uploaded images to train models. With the pretend model, nothing leaves.
- **Is the estimate checked against a real call?** Yes. The tokens of each image were exactly as the docs say; the prompt and the schema add about 460 to each question.
- **Can the tests fail?** I broke the new code in 42 places, one at a time. The tests found 41. The one that they missed was a weak check of a PNG: it read 4 bytes of the 8 of the mark, so a PNG whose line ends a transfer changed still passed. A new test sends such bytes, and now the tests find each of the 42.
- **What is not done:**
  - The text of an image can lose its line breaks (the English page of the live check). A prompt `image-2` that asks for each line of the image on its own line would need a new paid check.
  - The image is not made smaller before it is sent: the API does that. A smaller image would cost less, but it needs an image library.
  - Videos, animations and stickers are not read.
  - Claude does not name the people in a photo.
  - An image that could not be read leaves the item not `partial`, as a sent PDF does: `partial` means a link that could not be read.

## Try it yourself

Do this after the pull request is merged.

1. Run `git pull`, and then `make up`.
2. Put a screenshot into the folder `exports` of the project. Read it with the pretend model, at no cost:

```bash
docker compose exec api unshelve extract --file /imports/screenshot.png
```

The pretend model answers with words from the request, not from the image. The line `Reader: image` shows that the image extractor took the file.

3. With your yes for a paid call, read it with the real model. With Claude Haiku 5.5 this costs less than $0.001:

```bash
docker compose exec -e UNSHELVE_PROVIDERS=live api unshelve extract --file /imports/screenshot.png
```

4. See the price in the ledger:

```bash
docker compose exec api unshelve llm costs
```

## Exercise (10 minutes)

1. Open `apps/api/src/unshelve/extract/image.py`, and go to `ImageExtractor.match`.
2. Change `return IMAGE_SCORE if ref.url is None and image_files(ref) else 0` to `return IMAGE_SCORE if image_files(ref) else 0`.
3. Run the tests of the registry in `apps/api`:

```bash
uv run pytest tests/extract/test_registry.py -q
```

Which message now goes to the wrong reader? Why is the rule "only without a link" right for a channel of posts?

<details>
<summary>The answer</summary>

One case fails:

```text
FAILED tests/extract/test_registry.py::test_the_extractor_with_the_highest_score_reads_the_source[ref24-article]
E       AssertionError: assert 'image' == 'article'
```

A photo with a link to `blog.example.com/queues` now goes to the image extractor, not to the page. The case with a YouTube link still passes: the YouTube extractor has 50, more than 30.

In a channel, a post with a link and a photo mostly shows the cover of the page. The page holds the content; the cover holds a title at most. So the extractor of the link reads it, and the photo is read only when the message has no link.

</details>

## Words used in this note

| Word | Meaning |
|---|---|
| Vision | The ability of a model to read images |
| OCR | Reading the text in an image (optical character recognition) |
| Visual token | One square of 28 × 28 pixels of an image: the unit of its price |
| File signature | The first bytes of a file, which name its format |
| HEIC | The format of the photos of an iPhone; the model does not read it |
| Album | Up to 10 photos or videos that Telegram sends as one group |

## To read more

- [Vision](https://platform.claude.com/docs/en/build-with-claude/vision), Claude Platform Docs. Read "Supported formats" and "Resolution and token cost": the four types of part 3 and the rule of part 4.
- [List of file signatures](https://en.wikipedia.org/wiki/List_of_file_signatures), Wikipedia. Read the rows of JPEG, PNG, GIF and WebP: they are the bytes of part 3.

Previous: [27. The files of an export](27-export-files.md) · Back to the [list of notes](README.md)
