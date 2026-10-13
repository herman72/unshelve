# Recorded Telegram updates

Each line of a file is one update, as `getUpdates` gives it.

| File | What it holds |
|---|---|
| `seen.jsonl` | The shapes that a real bot received in the test of 2026-10-13 (docs/adr/0009): a private `/start`, the bot becomes an administrator, an album of 3 photos, a post with a link, a forwarded post, a button press. Ids, names and file ids are replaced. |
| `made_up.jsonl` | Kinds of updates that were not seen for real yet. They follow the Bot API documentation: Persian text with a link after an emoji, a file with a caption, a video, an animation, an edit of a post, a post of a channel that is not allowed, and three other kinds of forward. |

The test channel has the id `-1001234567890`, and the owner has the id `111111111`.

When a kind from `made_up.jsonl` is seen for real, move its line to `seen.jsonl` and correct it.
