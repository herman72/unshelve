# 0009. Capture rules from a test with a real bot and real exports

- Status: Accepted
- Date: 2026-10-13

## Context

Milestone M1 captures the posts of a Telegram channel in two ways. A bot receives the new posts. An importer reads an export of the old posts. The two parts depend on details that the Telegram documentation does not make clear.

So we tested before we wrote code. The test used a real bot, a private test channel with 11 messages, and exports from the two Telegram apps of the Mac App Store. Each statement in this record was seen on 2026-10-13 in a real answer of Telegram or in a real export, unless the statement names a different source. Short copies of the answers are at the end of this record, with private values masked.

## What we saw

### The bot

| # | Question | What Telegram does |
|---|---|---|
| 1 | Does a bot get the posts of a channel? | Yes, as `channel_post` updates, when the bot is an administrator of the channel. It gets only the posts that arrive after it became an administrator. Five older posts never arrived. |
| 2 | Who wrote a post? | Telegram does not say. A channel post has `sender_chat` (the channel) and no `from`. |
| 3 | What is an album? | One message for each photo. The three messages had the ids 7, 8 and 9, the same `date` and the same `media_group_id`. Nothing else connects them. |
| 4 | What is in a post with a link? | The text, an entity of the type `url`, and `link_preview_options.url`. That address is a cleaned form of the link. In one post `www.` was removed, and in a different post `http` became `https`. |
| 5 | What is in a forwarded post? | `forward_origin`: the type, the source channel with its title and username, the id of the original message, and its date. |
| 6 | How does the bot learn that it is an administrator? | It gets a `my_chat_member` update with the new status and the list of its rights. |
| 7 | Can the bot put a button under a post of the owner? | Yes, under a post with text (`editMessageReplyMarkup`). The post gets an `edit_date`, and the phone app does not show the word "edited". No, under a message of an album: the answer is 400 "message is not modified". |
| 8 | What does a button press give? | A `callback_query` with the user who pressed (`from`). This is also true for a button in a channel. |
| 9 | How fast must the bot answer a button press? | At once. Without an answer, the button looks busy and the person presses again: the owner pressed five times. A late answer was refused with "query is too old". |
| 10 | Can the bot react to a post? | Yes (`setMessageReaction`). The owner sees the reaction. |
| 11 | What does a reaction of the owner give? | A `message_reaction_count` update: a count for each emoji (here 👍 with the count 2). It does not say who reacted. The bot asked for this kind of update. The documentation says that Telegram does not send it by default. |
| 12 | Does `getChat` say if reactions are possible? | No. `available_reactions` was an empty list, and reactions worked for the bot and for the owner. |
| 13 | What happens to updates that nobody confirms? | They wait. Six reads without an offset removed nothing: the list had 0, 1, 4, 9, 9 and 17 updates. The documentation gives a limit of 24 hours. |

### The export

| # | Question | What we saw |
|---|---|---|
| 14 | Can each user get a JSON export of one channel? | No. The two apps of the Mac App Store, "Telegram" and "Telegram Lite", make HTML only for one chat. The source code of Telegram Desktop builds the line with the format choice only outside the App Store build (`export/view/export_view_settings.cpp`, the flag `OS_MAC_STORE`). |
| 15 | What does the HTML export hold for a message? | The id, the date, the text, the links, the source of a forward with its date, and the path of an attached file. |
| 16 | Are the ids the same as the ids that the bot gets? | The export had the ids 1 to 6. The next posts arrived at the bot with the ids 7 to 11. One message was not yet compared in the two sources. |
| 17 | Does the export name the channel? | Only by its title. The numeric id of the channel is not in the HTML. |
| 18 | Are the dates correct? | The clock time is. The time zone is not. The export of "Telegram" has no time zone. The export of "Telegram Lite" said `UTC+02:00` on a computer that was at UTC+03:00, in summer time. |

## Decision

1. **A capture is identified by its chat and its message id.** The key `(owner, chat_id, message_id, edit_seq)` stays. The bot accepts a channel post because of its chat id, which must be in `UNSHELVE_CAPTURE_CHAT_IDS`. It cannot check a user, because a channel post has none.
2. **An album becomes one item through `media_group_id`.** Each message of an album is stored as a capture of its own. One `ingest_album` job for each `media_group_id` starts some seconds later and joins them, as ADR 0003 planned.
3. **The bot does not write in the channel.** Version 1 has no buttons and no reactions there. A button does not work on an album, and it changes a post of the owner. The controls are in the private chat with the bot and in the web app. A button press there names the user, so the bot can check that the owner pressed.
4. **The bot answers a button press before it does the work.** The handler calls `answerCallbackQuery` first. Then it puts the work into a job.
5. **The bot asks for the kinds of updates that it uses:** `message`, `channel_post`, `edited_channel_post`, `my_chat_member` and `callback_query`. Reactions are not in the list.
6. **At its start, the bot checks its place in each capture chat** with `getChatMember`. If it is not an administrator, it logs a clear error.
7. **The importer reads the HTML export of Telegram Desktop and Telegram Lite.** This replaces "JSON only" in the plan. The importer also reads JSON for a user who has it. The user gives the importer the channel, because its id is not in the export, and the name of a time zone. The importer does not use the time zone text of the export.
8. **Our own code makes the canonical address of a link.** `link_preview_options.url` is a help, and the resolver of M1 does not depend on it.

## Consequences

- The history of a channel comes only from an export, because the bot gets no old posts. A post that is in the export and also arrived at the bot is stored one time, because the two have the same key.
- An album arrives in parts. Its item exists some seconds after its last part.
- The importer has two formats to read. The HTML format has no documentation, and a new version of Telegram can change it. So the importer is tested against real exports, and it stops with a clear error when it finds a structure that it does not know.
- A forwarded post from an export has the name of its source and a date, and no ids. So it gets no `tg_fwd` key (ADR 0003). The same post from the bot gets one. The two are still stored one time, because the capture key is the same.
- The time of an old post depends on the time zone that the user gives. A wrong zone moves the time by one hour or more. It does not change the identity of a post.
- Reactions as a way to label an item stay possible. In a channel with one person, a count is sufficient. This is not part of version 1.

Not tested, and thus still open:

- **The smallest set of rights.** The bot had all rights except "add new admins". Before the bot goes into the real channel of the owner, the capture test is repeated with fewer rights.
- **Other kinds of posts:** an edited post, a video, a file, and an album with a caption. The package M1-1 records them as fixtures.
- **One message in the two sources.** A new export that includes the ids 7 to 11 will show that the ids and the dates agree.

## Alternatives considered

- **Buttons under each post in the channel.** The owner could mark a post as read where he saved it. But a button is refused on an album, each button is an edit of a post of the owner, and the channel must stay the place where saving costs nothing.
- **A reaction of the bot as a "saved" mark.** It works and it is small. It is left out of version 1 because it mixes with the reactions of the owner in the same count. It can come back as a setting.
- **JSON as the only import format.** JSON is the cleaner format. But a user with an app from the Mac App Store cannot make it for one chat, and the first step of a new user must not need a third app.
- **Use `available_reactions` to know if reactions work.** The field was empty when reactions worked.

## The real answers

Private values are masked: the owner has the id `111111111`, the test channel `-1001234567890`, the bot `2222222222`. File ids are cut.

**One message of the album.** The other two differ in `message_id` (8 and 9) and in the photo. Only the largest photo size is shown.

```json
{
  "update_id": 370472926,
  "channel_post": {
    "message_id": 7,
    "sender_chat": {
      "id": -1001234567890,
      "title": "Unshelve test",
      "type": "channel"
    },
    "chat": {
      "id": -1001234567890,
      "title": "Unshelve test",
      "type": "channel"
    },
    "date": 1791923117,
    "media_group_id": "14335384938541892",
    "photo": [
      {
        "file_id": "AgACAgQA…",
        "file_unique_id": "AQADcxFr…",
        "file_size": 105571,
        "width": 588,
        "height": 1280
      }
    ]
  }
}
```

**The post with a link.**

```json
{
  "update_id": 370472929,
  "channel_post": {
    "message_id": 10,
    "sender_chat": {
      "id": -1001234567890,
      "title": "Unshelve test",
      "type": "channel"
    },
    "chat": {
      "id": -1001234567890,
      "title": "Unshelve test",
      "type": "channel"
    },
    "date": 1791923167,
    "text": "https://www.youtube.com/watch?v=UPtmKh1vMN8",
    "entities": [
      {
        "offset": 0,
        "length": 43,
        "type": "url"
      }
    ],
    "link_preview_options": {
      "url": "https://youtube.com/watch?v=UPtmKh1vMN8"
    }
  }
}
```

**The forwarded post.** The older `forward_from_*` fields are left out.

```json
{
  "update_id": 370472930,
  "channel_post": {
    "message_id": 11,
    "sender_chat": {
      "id": -1001234567890,
      "title": "Unshelve test",
      "type": "channel"
    },
    "chat": {
      "id": -1001234567890,
      "title": "Unshelve test",
      "type": "channel"
    },
    "date": 1791923190,
    "forward_origin": {
      "type": "channel",
      "chat": {
        "id": -1001111111111,
        "title": "Tech Notes",
        "username": "technotes",
        "type": "channel"
      },
      "message_id": 590,
      "date": 1789917302
    },
    "text": "http://github.com/cloudflare/security-audit-skill",
    "link_preview_options": {
      "url": "https://github.com/cloudflare/security-audit-skill"
    }
  }
}
```

**The bot becomes an administrator.**

```json
{
  "update_id": 370472925,
  "my_chat_member": {
    "chat": {
      "id": -1001234567890,
      "title": "Unshelve test",
      "type": "channel"
    },
    "from": {
      "id": 111111111,
      "is_bot": false,
      "first_name": "…",
      "last_name": "…",
      "username": "…",
      "language_code": "en"
    },
    "date": 1791922411,
    "old_chat_member": {
      "status": "left"
    },
    "new_chat_member": {
      "user": {
        "id": 2222222222,
        "is_bot": true,
        "first_name": "Unshelve",
        "username": "mattsh_unshelve_bot"
      },
      "status": "administrator",
      "can_manage_chat": true,
      "can_change_info": true,
      "can_post_messages": true,
      "can_edit_messages": true,
      "can_delete_messages": true,
      "can_invite_users": true,
      "can_restrict_members": true,
      "can_manage_video_chats": true,
      "can_post_stories": true,
      "can_edit_stories": true,
      "can_delete_stories": true,
      "can_manage_direct_messages": true,
      "can_send_welcome_messages": true,
      "can_manage_voice_chats": true
    }
  }
}
```

**A button press in the channel.**

```json
{
  "update_id": 370472931,
  "callback_query": {
    "id": "414056…",
    "from": {
      "id": 111111111,
      "is_bot": false,
      "first_name": "…",
      "last_name": "…",
      "username": "…",
      "language_code": "en"
    },
    "message": {
      "message_id": 10,
      "chat": {
        "id": -1001234567890,
        "title": "Unshelve test",
        "type": "channel"
      },
      "date": 1791923167,
      "edit_date": 1791923347,
      "reply_markup": {
        "inline_keyboard": [
          [
            {
              "text": "Unshelve test button",
              "callback_data": "spike:link"
            }
          ]
        ]
      }
    },
    "chat_instance": "…",
    "data": "spike:link"
  }
}
```

**A reaction of the owner.**

```json
{
  "update_id": 370472938,
  "message_reaction_count": {
    "chat": {
      "id": -1001234567890,
      "title": "Unshelve test",
      "type": "channel"
    },
    "message_id": 11,
    "date": 1791923543,
    "reactions": [
      {
        "type": {
          "type": "emoji",
          "emoji": "👍"
        },
        "total_count": 2
      }
    ]
  }
}
```

**A button on a message of an album is refused.**

```json
{
  "ok": false,
  "error_code": 400,
  "description": "Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message"
}
```

**A late answer to a button press is refused.**

```json
{
  "ok": false,
  "error_code": 400,
  "description": "Bad Request: query is too old and response timeout expired or query ID is invalid"
}
```

**The channel, as `getChat` shows it.** Other fields are left out.

```json
{
  "id": -1001234567890,
  "type": "channel",
  "title": "Unshelve test",
  "available_reactions": [],
  "max_reaction_count": 11,
  "has_visible_history": true
}
```
