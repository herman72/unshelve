# YouTube fixtures

Made-up values in the structure of the real pages of YouTube, as they were on 15 October 2026:
`<script nonce="…">var ytInitialPlayerResponse = {…};</script>`, with `\u` escapes in the JSON,
and the keys of `playabilityStatus`, `videoDetails` and `microformat`. No text of a real video is here.

`tests/extract/test_youtube_live.py` checks the structure of a real page (`-m live`).
