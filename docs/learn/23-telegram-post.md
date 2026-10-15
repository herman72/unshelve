# 23. Reading a post on Telegram

> Pull request: [#79](https://github.com/herman72/unshelve/pull/79) · Code: [`extract/telegram.py`](../../apps/api/src/unshelve/extract/telegram.py), [`extract/registry.py`](../../apps/api/src/unshelve/extract/registry.py) · Tests: [`tests/extract/test_telegram.py`](../../apps/api/tests/extract/test_telegram.py), [`tests/extract/test_telegram_live.py`](../../apps/api/tests/extract/test_telegram_live.py)

## In one minute

Telegram gives you two ways to save a post of another channel: you forward it, or you save its link. Both now give a post with its channel and its date.

| You save | Before | Now |
|---|---|---|
| A forwarded post | a "note" by nobody | a "post" by its channel, with the date of the first post; no request |
| A link `t.me/telegram/460` | a page without an article: the link only | the text, the channel, the date, the preview of its link |

A real link, `https://t.me/telegram/460`:

```text
A post by Telegram News (@telegram) on Telegram, from 26 August 2026.

For all the info from today's update and a look back at 13 years of Telegram features — check out our blog:
https://telegram.org/blog/welcome-messages-buttons-TG-13

It links to "Welcome Messages, Buttons in Messages and Signed Gifts" (Telegram): Introducing Welcome Messages …
```

## 1. A forwarded post needs no request

When you forward a post into your channel, the bot gets its whole text, and the origin of the forward ([note 10](10-bot-capture.md)): the type of the source, its name, and the date of the first post. That is enough for the card:

```text
origin   type "channel", name "Other Channel", date 1 October 2026
text     A post by Other Channel on Telegram, from 1 October 2026.

         Leases make a job queue safe.
```

A post of a person says "A message by …". An export gives only the name, so its type is "unknown", and it says "A post by …".

If the forwarded post has a link, the extractor of that link reads it: the page is the thing that you saved, and the post is often only a pointer to it.

## 2. A link: the embed page

The page `t.me/telegram/460` is for people and for previews. The embed page (`?embed=1&mode=tme`) shows the post in a small block, the same block that a website shows when it embeds a post:

| Part | In the embed page |
|---|---|
| Text | `.tgme_widget_message_text`: HTML with bold, line breaks and links |
| Channel | `.tgme_widget_message_owner_name` |
| Date | `time[datetime]`: `2026-08-26T19:12:34+00:00` |
| Preview of a link | `.tgme_widget_message_link_preview`: site, title, description |
| Photos and video | `.tgme_widget_message_photo_wrap`, `.tgme_widget_message_video_player` |
| Views | `.tgme_widget_message_views`: "1.89M"; not in the text, it changes each day |
| A post that does not exist | `.tgme_widget_message_error`: "Post not found" |

The addresses `t.me/s/telegram/460`, `telegram.me/…` and `telegram.dog/…` have one canonical form ([note 12](12-resolver.md)), so the extractor sees one address.

## 3. A source that is not used: the plain page

The plain page `t.me/telegram/460` has a meta tag `og:description` with the text of the post. It looks like a simple second way, but it is not safe: for a post without text, the tag holds the description of the channel. For `t.me/durov/1`, it says "Founder of Telegram.", a sentence that is not in that post. So the extractor does not use it.

**A comparison from daily life:** a label on a box tells you what is in it. If an empty box gets the label of the shelf instead, the label lies, and you trust it.

## 4. When the post is not there

| What happens | Result |
|---|---|
| "Post not found", or a page without a post | an `ExtractError`: the item keeps its saved text |
| A private channel (`t.me/c/…`), an invitation, the page of a channel | not claimed: other extractors |
| 429, 503, a timeout, a 404 of the page | the `FetchError` goes up to the fetch step |

## A choice that we made

| | The embed page (ours) | The API of Telegram (MTProto) | The plain page |
|---|---|---|---|
| A key | none | an account and an app id | none |
| What comes | text, channel, date, preview, media | everything | the text, or the description of the channel |
| The risk | Telegram changes the block: the live test sees it | the account of a person in a program, and its session to keep secret | a wrong text for a post without text |

## What a senior engineer checks here

- **Can a second source say something false?** Yes: the plain page gives the description of the channel for a post without text. So it is not a fallback.
- **Is a forward attributed to its source?** The channel and the date of the first post, without a request: a test for each kind of forward.
- **Is the text stable?** No views: a test.
- **Does a link win over a forward?** Yes: the page is the thing that you saved; a test.

## Try it yourself

After the merge, run `git pull` and `make up`. Then:

```bash
docker compose exec worker unshelve extract https://t.me/telegram/460
```

Forward a post of a public channel into "Unshelve test". Its card has the name of the channel.

## Exercise (10 minutes)

1. Open `apps/api/src/unshelve/extract/telegram.py`, and go to `match`.
2. Remove the condition `ref.url is None and` from the line for a forwarded message.
3. Run `uv run pytest tests/extract/test_telegram.py -q` in `apps/api`.

Which test fails? Why must a forwarded post with a link go to the extractor of the link?

<details>
<summary>The answer</summary>

`test_a_forwarded_message_with_a_link_is_left_to_the_extractor_of_the_link` fails: the Telegram extractor (20) would claim the post, and the article extractor (10) would lose. Then the item would have the short text of the post ("A good read: https://…") and not the article. People forward a post with a link to keep the page, so the page must be read.

</details>

## Words used in this note

| Word | Meaning |
|---|---|
| Forward | A copy of a message that keeps the name of its source |
| Origin (of a forward) | The source of a forwarded message: a channel, a group or a person |
| Embed page | A small page that shows one post, for other websites |
| Link preview | The small box with the title and the description of a link in a post |
| Meta tag | A line in the head of a page with a fact for previews and search engines |

## To read more

- [MessageOrigin](https://core.telegram.org/bots/api#messageorigin), Telegram Bot API. Read the four kinds of origin: they are the four kinds of forward in this note.
- [Post widget](https://core.telegram.org/widgets/post), Telegram. Read how a website embeds a post: the extractor reads the same block.

Previous: [22. Reading a paper on arXiv](22-arxiv-paper.md) · Back to the [list of notes](README.md)
