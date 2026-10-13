# Exports of a Telegram chat

Each folder is one export, as "Export chat history" makes it. Only the pages are here. A real export has also the folders `css`, `js`, `images`, and the folders of the files.

| Folder | What it holds |
|---|---|
| `export_desktop` | The page that Telegram Desktop wrote for the test channel on 2026-10-13 (docs/adr/0009). In the Mac App Store the name of this app is Telegram Lite. The structure is as it was. The texts, the links and the names are replaced. |
| `export_macos` | The same five posts, as Telegram for macOS wrote them. The structure is as it was. The texts, the links and the names are replaced. |
| `export_made_up` | Kinds of posts that were not seen in a real export yet. They follow the code that writes the export in Telegram Desktop (`export_output_html.cpp`): an album of three photos, a file that the export did not include, a video, a poll, a sticker, two posts of one person that were forwarded together, a rich message, a voice message, an audio file, two documents that were sent together, a location, and a second page. |

The five posts of the two real exports have the ids 2 to 6. The made-up export continues with the ids 7 to 26. Its posts 7 to 11 are the posts that the bot recorded (`tests/bot/fixtures/seen.jsonl`): the same ids, the same texts, and the same moments when the time zone is `Europe/Helsinki`.

When a kind from `export_made_up` is seen in a real export, move it to the folder of that app and correct it.
