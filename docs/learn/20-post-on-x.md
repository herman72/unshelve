# 20. Reading a post on X

> Pull request: [#76](https://github.com/herman72/unshelve/pull/76) · Code: [`extract/x_post.py`](../../apps/api/src/unshelve/extract/x_post.py), [`extract/registry.py`](../../apps/api/src/unshelve/extract/registry.py) · Tests: [`tests/extract/test_x_post.py`](../../apps/api/tests/extract/test_x_post.py), [`tests/extract/test_x_post_live.py`](../../apps/api/tests/extract/test_x_post_live.py)

## In one minute

The page of a post on X is an application in JavaScript. The article extractor found only 106 characters on it ([note 16](16-fetch-step.md)), so a saved post kept only its link.

Now the fetch step reads the post. A real public post, the launch of ChatGPT:

```text
A post by Sam Altman (@sama) on X, from 30 November 2022.

today we launched ChatGPT. try talking with it here:

http://chat.openai.com
```

The short link `https://t.co/uWra8LKFMN` became the address that it stands for, and that address is an extra link of the item. The model writes the title of the card: "A post on X" says nothing.

## 1. Two ways in, and then the saved text

| Way | What it gives | What it lacks |
|---|---|---|
| 1. The endpoint of the embed widgets (`cdn.syndication.twimg.com/tweet-result`) | JSON: the text, the real address of each short link, the date and time, the language, a quoted post, the photos and videos | X documents it nowhere: it can change |
| 2. oEmbed (`publish.x.com/oembed`) | the official answer: the text as HTML, the author, the day | the links stay short; no time, no quoted post |
| 3. The saved text | your message | everything else |

The first way is richer, so it comes first. The second way is official, so it is the safety net. Any failure of the first way goes on to the second: they are two different services of X. A failure of the second way that can pass (429, 503, a timeout) goes up, and the fetch step tries again later.

**A comparison from daily life:** you ask a friend who knows the whole story. If the friend does not answer, you read the official notice on the door. If the door is locked, you keep your own note.

## 2. The token that nobody checks

The first endpoint asks for a parameter `token`. A test on 15 October 2026:

| Request | Answer |
|---|---|
| without `token` | 200, but an empty object |
| `token=a` or `token=0` | 200, the full post |

So X asks for a token, and does not check its value. The extractor sends the value of the formula that the embed widget uses (from the library react-tweet). If X starts to check, the formula is the best guess, and oEmbed still works.

## 3. Short links, photos and the text

A post keeps each link short: `https://t.co/uWra8LKFMN`. The answer lists each one with its real address, so the extractor puts the real one into the text. The short link of a photo is different: it is only a way to the photo. The extractor removes it, and writes one sentence instead: "It has 1 photo."

The text has no number of likes, for the reason of [note 19](19-youtube-video.md), part 4: a number that changes each day would make a new card and new vectors.

## 4. What X does not give

- **A long post** (more than 280 characters): the answer has only its start, and a mark `note_tweet`. The text then ends with "(X gives only the start of this long post.)", so the model knows.
- **A thread:** only the post at the link.
- **A post that is gone:** the answer is a "tombstone" with a reason, such as "This Post is from an account that no longer exists." That is an `ExtractError`, and the item keeps your saved text.

## A choice that we made

| | The two endpoints (ours) | The API of X | A browser in the worker |
|---|---|---|---|
| Cost | none | a paid plan to read posts | none, but much CPU and memory |
| Key | none | yes | none |
| Risk | the first endpoint can change: the live test sees it, and oEmbed stays | the price can change | X blocks automated browsers |

## What a senior engineer checks here

- **Does the extractor depend on a service without documentation?** Yes, the first endpoint. So the official oEmbed stands behind it, and a live test checks both ways.
- **Is a date read in the language of the computer?** No. `strptime("%B")` reads the names of the months in the language of the computer. The names are in the code, and a test reads "September 30, 2026".
- **Is the text stable?** No likes in it: a test.
- **Is a link that you will need kept?** The real address of each short link is an extra link of the item.

## Try it yourself

After the merge, run `git pull` and `make up`. Then:

```bash
docker compose exec worker unshelve extract https://twitter.com/sama/status/1598038815599661056
```

The command prints the reader (`x`), the author and the text.

## Exercise (10 minutes)

1. Open `apps/api/src/unshelve/extract/x_post.py`, and go to `_text_of`.
2. Remove the loop that removes the short links of the media.
3. Run `uv run pytest tests/extract/test_x_post.py -q` in `apps/api`.

Which tests fail? Why is a short link of a photo worse than no link, for the model and for search?

<details>
<summary>The answer</summary>

Two tests fail: the text of a post, and `test_a_short_link_becomes_the_address_that_it_stands_for` ("t.co" is in the text). A short link of a photo is a word without meaning: the model cannot open it, and a vector of the text gets a little noise. The sentence "It has 1 photo." says the same, in words that the model and search can use.

</details>

## Words used in this note

| Word | Meaning |
|---|---|
| Embed widget | The box in which a web page shows a post of X |
| Endpoint | One address of a service that answers requests |
| Short link | An address of `t.co` that X puts in place of each link |
| Tombstone | The answer for a post that is gone, with the reason |
| Locale | The language and the forms of a computer, such as the names of the months |

## To read more

- [oEmbed API](https://docs.x.com/x-for-websites/oembed-api), X documentation. Read the table of parameters, with `omit_script` and `dnt`: the extractor sends both.
- [react-tweet](https://github.com/vercel/react-tweet), the library of the embed widget. Read `packages/react-tweet/src/api/fetch-tweet.ts`: the request and the token.

Previous: [19. Reading a YouTube video](19-youtube-video.md) · Next: [21. Reading a GitHub repository](21-github-repository.md) · Back to the [list of notes](README.md)
