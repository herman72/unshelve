# 19. Reading a YouTube video

> Pull request: [#75](https://github.com/herman72/unshelve/pull/75) · Code: [`extract/youtube.py`](../../apps/api/src/unshelve/extract/youtube.py), [`extract/registry.py`](../../apps/api/src/unshelve/extract/registry.py), [`pipeline/items.py`](../../apps/api/src/unshelve/pipeline/items.py) (the minutes) · Tests: [`tests/extract/test_youtube.py`](../../apps/api/tests/extract/test_youtube.py), [`tests/extract/test_youtube_live.py`](../../apps/api/tests/extract/test_youtube_live.py), [`tests/pipeline/test_fetch.py`](../../apps/api/tests/pipeline/test_fetch.py)

## In one minute

In the comparison of the models ([note 17](17-model-card.md)), a bare YouTube link gave each model only the link. Both models wrote "a bare YouTube link, the topic is unknown".

Now the fetch step reads the video. A real public video, Andrej Karpathy's "Let's build GPT":

| Before | After |
|---|---|
| the text is the link: 28 characters | the text has 5,236 characters: title, channel, length, date, keywords, description |
| the item takes "1 min" | the item takes 117 minutes, the length of the video |
| no links | 11 links from the description: the code, a notebook, the playlist |

The first two lines of the text:

```text
Let's build GPT: from scratch, in code, spelled out.
A video by Andrej Karpathy on YouTube, 117 minutes, from 17 January 2023.
```

The model writes the card from this text. Voyage makes one vector for the card and 6 for the pieces of the text ([note 18](18-vectors-for-search.md)).

## 1. Which links are videos

A video has many addresses: `youtu.be/ID`, `/watch?v=ID`, `/shorts/ID`, `/live/ID`, `/embed/ID`, with or without `www.` or `m.`. The resolver already turns each of them into one canonical form ([note 12](12-resolver.md)):

```text
https://youtu.be/kCc8FmEb1nY?si=abc&t=42        →  https://youtube.com/watch?v=kCc8FmEb1nY
https://www.youtube.com/live/kCc8FmEb1nY        →  https://youtube.com/watch?v=kCc8FmEb1nY
```

So the extractor needs one rule: the canonical form is `https://youtube.com/watch?v=` and an id of 11 characters. A channel (`/@name`) or a playlist is no video, and the article extractor gets it.

The score of the YouTube extractor is 50. The article extractor has 10 for each page, so the extractor for one site wins ([note 16](16-fetch-step.md), part 1).

## 2. What the watch page holds

The watch page is about 2 MB of HTML. In it, one script holds the facts of the player as JSON:

```text
<script nonce="…">var ytInitialPlayerResponse = {"playabilityStatus": {"status": "OK"},
  "videoDetails": {"title": "…", "author": "…", "lengthSeconds": "6980",
                   "shortDescription": "…", "keywords": ["…"]},
  "microformat": {"playerMicroformatRenderer": {"publishDate": "2023-01-17T08:33:27-08:00"}}};</script>
```

The extractor finds `ytInitialPlayerResponse =`, reads one JSON object from that place, and checks the fields that it needs with Pydantic. The rest of the object (about 0.5 MB) is not used.

The request has `hl=en`. Without it, YouTube writes its messages in the language of the country of the IP. From your computer in Finland, the reason for a video that does not exist came back in Finnish: "Video ei ole käytettävissä".

## 3. When the page has no facts

| The page | What happens |
|---|---|
| The facts are there (also for a video with an age limit) | the full text |
| `status: "ERROR"`: the video does not exist | an `ExtractError`; the item keeps its saved text and is partial |
| No facts: a page that asks for consent, a check for robots, a new form of the page | oEmbed: the title and the channel |
| 429, 503, a timeout | the `FetchError` goes up; the fetch step tries again later |

**oEmbed** is an open standard: a site answers the question "what is at this address?" with a small JSON. YouTube's answer has the title and the channel, but no description and no length. It is the second way in, because it works when the page does not.

## 4. Only facts that do not change

The page also has the number of views. The text does not take it. Why?

The fetch step stores a hash of the text. After an edit of your message, the pipeline runs again. If the number of views were in the text, the text would be new each day. Then the model would write a new card, and Voyage would make new vectors, for a video that did not change. A test checks that "12345", the views in the fixture, is not in the text.

## 5. The minutes of a video

`est_minutes` is the time that an item takes. For a text, the enrich step counts it from the words: 220 words a minute. For a video, the time is its length. So the contract `Extracted` has a new field, `minutes`:

```text
fetch step    est_minutes = extracted.minutes     None for a text, 117 for the video
enrich step   est_minutes = the stored value, or the reading time of the text
```

The fetch step writes the field on each run, also `None`. So after an edit that turns a video into an article, the old length does not stay. A test checks it.

## 6. Why no transcript

The list of subtitles is in the page, but YouTube now asks for a token of origin ("PO token") for each one. A plain request got an answer of 0 bytes. To make the token, a program must run the JavaScript of YouTube. That is a browser in the worker, for a part of the text that the description often covers. The extractor does not read transcripts now.

## A choice that we made

How do we read a video?

| | The page and oEmbed (ours) | YouTube Data API | yt-dlp, a library |
|---|---|---|---|
| A key | none | a Google key, and a quota of 10,000 units a day | none |
| The facts | title, channel, length, date, description, keywords | the same, and more | the same, and some transcripts |
| The risk | YouTube changes its page: the live test finds it | a key to keep secret, and a quota | a large library that opens connections itself, outside the guard of [note 14](14-fetcher-and-guard.md) |

## What a senior engineer checks here

- **Is the text stable?** No number of views or likes: a test.
- **Does a failure that can pass get a second try?** Yes: a 429, a 503 and a timeout go up unchanged, and the fetch step waits. Four tests.
- **Does a page in a new form break the pipeline?** No: the extractor falls back to oEmbed, and then to the saved text. The live test shows the change.
- **Are the fixtures like the real pages?** They are made-up values in the exact form of the real pages of 15 October 2026. The live test reads a real page and checks the same fields.
- **Can a video be counted as one minute?** No: its length is its minutes. Two tests in the pipeline.

## Try it yourself

After the merge, run `git pull` and `make up`. Then post a YouTube link in "Unshelve test". The card in the web app has the title, the channel and the length in minutes.

To see what the worker reads from one link, without the pipeline. The command chooses the reader as the worker does, and nothing is stored:

```bash
docker compose exec worker unshelve extract https://youtu.be/kCc8FmEb1nY
```

## Exercise (10 minutes)

1. Open `apps/api/src/unshelve/extract/youtube.py`, and go to `_from_player`.
2. Add the views to the facts: `facts.append(f"{details.view_count} views")`, after you add `view_count: int = Field(default=0, alias="viewCount")` to `_Details`.
3. Run `uv run pytest tests/extract/test_youtube.py -q` in `apps/api`.

Which test fails? And why is that a cost, not a matter of style?

<details>
<summary>The answer</summary>

`test_the_number_of_views_is_not_in_the_text` fails. With the views in the text, each new fetch of the video gives a new text and a new hash. The pipeline then pays for a new card and new vectors of a video that did not change. A fact in the text must change only when the thing changes.

</details>

## Words used in this note

| Word | Meaning |
|---|---|
| Watch page | The web page of one video on YouTube |
| Player response | The JSON with the facts of the video, in the watch page |
| oEmbed | An open standard: a small JSON about the content at an address |
| Canonical form | The one address that stands for all addresses of the same thing |
| Fixture | A file that a test reads instead of the real thing |
| Transcript | The spoken words of a video, as text |

## To read more

- [oEmbed](https://oembed.com/), the specification. Read part 2.2, "Consumer Request", and the example: it is the request that the extractor sends.
- [Videos: list](https://developers.google.com/youtube/v3/docs/videos/list), YouTube Data API. Read the part "Quota impact", to see what the other way would cost.

Previous: [18. Vectors for search](18-vectors-for-search.md) · Next: [20. Reading a post on X](20-post-on-x.md) · Back to the [list of notes](README.md)
