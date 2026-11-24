# 55. Who says that it stopped

> Pull request: [#155](https://github.com/herman72/unshelve/pull/155) · Issue: [#154](https://github.com/herman72/unshelve/issues/154) · Code: [`alive.py`](../../apps/api/src/unshelve/alive.py), [`queue/worker.py`](../../apps/api/src/unshelve/queue/worker.py), [`bot/runner.py`](../../apps/api/src/unshelve/bot/runner.py), the services `worker` and `bot` in [`deploy/compose.yaml`](../../deploy/compose.yaml), [`deploy/Dockerfile.api`](../../deploy/Dockerfile.api), [`watch/hide.py`](../../apps/api/src/unshelve/watch/hide.py), [`watch/limits.py`](../../apps/api/src/unshelve/watch/limits.py), [`watch/observations.py`](../../apps/api/src/unshelve/watch/observations.py) · Tests: [`test_alive.py`](../../apps/api/tests/test_alive.py), [`queue/test_worker_alive.py`](../../apps/api/tests/queue/test_worker_alive.py), [`bot/test_bot_alive.py`](../../apps/api/tests/bot/test_bot_alive.py), [`deploy/test_alive_compose.py`](../../apps/api/tests/deploy/test_alive_compose.py), [`watch/test_hide.py`](../../apps/api/tests/watch/test_hide.py), [`watch/test_pure.py`](../../apps/api/tests/watch/test_pure.py)

## In one minute

On 2026-11-23, somebody revoked the key of the model. Each card job got `AuthenticationError` and tried again after 2, 8, 32 and 60 minutes. That is about 1 h 40 min of tries. After the fifth try, the step gave up and saved the item without a card. No message went to anyone. `docker compose ps` showed each service as `Up`.

Two things were missing:

1. A sign from each process that it still works.
2. Somebody who reads the signs and tells a person.

This note covers the first thing. The worker and the bot now write a sign of life into a small file. Docker checks the age of the file every 30 seconds. `docker compose ps` then shows each of the two services as `healthy` or `unhealthy`.

The second thing is a separate watcher that sends a message. It comes in a later pull request, and this note then gets more parts. Until then, this table shows what the first part sees:

| What goes wrong | What `docker compose ps` shows now |
|---|---|
| The loop of the worker hangs | `worker` is `unhealthy`, about 4 minutes after its last sign of life |
| The bot runs, and then it cannot reach Telegram, or Telegram refuses its token | `bot` is `unhealthy`, about 4 minutes after its last sign of life |
| The bot starts while it cannot reach Telegram, or while Telegram refuses its token | `bot` restarts again and again: `Restarting`, or `Up` with `health: starting`, never `unhealthy`. The first request (`getMe`) fails, and the process ends before it marks the file |
| A handler of the bot hangs | `bot` is `unhealthy` |
| The provider of the model refuses its key (the case of 2026-11-23) | nothing: the loop of the worker still turns. Only the watcher can see the failed tries and the items without a card |
| The computer sleeps or is off | nothing: no check runs. Only a check outside the computer can see it |

## 1. A sign of life is a file with a time

Run each `docker compose` command of this note in the root of the clone. This is the file of a bot without a token, from a test run of the image:

```text
$ docker compose exec bot cat /app/var/alive/bot
started=1795477576
pid=1
$ docker compose exec bot python -m unshelve.alive check bot --max-age 180
bot: a sign of life 12.6 s ago (limit 180 s)
```

The lines of the file say when the process started (in seconds since 1970) and which process it is. The sign of life is not in the lines. It is the time of the last change of the file, the *mtime*. A process that works changes this time again and again. A process that hangs does not.

**A comparison from daily life:** a night guard signs a sheet each hour. The supervisor does not have to find the guard. An empty line on the sheet is the signal.

**Why the word is not "heartbeat".** In this project, a heartbeat is the message that keeps the lease of a job ([note 4](04-job-queue.md), [note 5](05-worker-loop.md)). One word has one meaning. So the new file is a *liveness file*, and a new time on it is a *sign of life*.

### Who marks the file, and when

| Process | It marks the file | The longest normal gap |
|---|---|---|
| `worker` | at each round of its loop. While a job runs, after each heartbeat of the lease (every 20 s, in the function `keep_alive`) | about 30 s |
| `bot` with a token | after each answer of `getUpdates` | about 20 s |
| `bot` without a token | on a timer, every 30 s, and once at the start | 30 s |

The function `alive.mark` changes the time of the file at most once in 10 seconds. An idle worker turns its loop each second, but it does not write to the disk each second. Usually, `alive.mark` only changes the time of the file (`os.utime`). The process writes the whole file only at three times: when it marks the file for the first time, when a fact gets a new value, and at a clean stop.

### Why only an answer of getUpdates counts for the bot

The bot asks Telegram for updates with `getUpdates` ([note 10](10-bot-capture.md)). On a quiet chat, Telegram answers after 10 seconds with no update. The polling loop of aiogram catches each error and asks again, for ever. So a bot that cannot reach Telegram still runs. It only writes "Failed to fetch updates" in its log.

A *request middleware* is a function that each request of the bot to Telegram passes through. The middleware `MarkAlive` marks the file only after `getUpdates` returned without an error:

```mermaid
sequenceDiagram
    participant L as The polling loop of aiogram
    participant M as MarkAlive
    participant T as Telegram
    participant F as /app/var/alive/bot
    L->>M: getUpdates
    M->>T: getUpdates
    T-->>M: an answer (after 10 s at most)
    M->>F: mark
    M-->>L: the updates
    L->>M: getUpdates
    M->>T: getUpdates
    T-->>M: 401 Unauthorized
    M-->>L: TelegramUnauthorizedError (F keeps its old time)
```

A message that the bot sends is no sign that the bot receives. So `sendMessage` does not mark the file either. A test has each of these cases.

### Facts, and a clean stop

The bot also writes one *fact* into its file: `chats_ready=1/2` means that the bot can read the posts of 1 of its 2 capture chats. The watcher can then tell a person about a channel that the bot cannot read.

A clean stop adds one line. In the test run, `docker stop` sent SIGTERM to the bot, and the file then held:

```text
started=1795477576
pid=1
stopped=1795477776
```

A crash or a `kill -9` writes no such line. So the watcher can count the starts that have no clean stop before them.

The check of Docker also reads this line. The stop gave the file a new time, but the process that wrote it has ended. So the check fails on a file with `stopped`, whatever its time. When the next process marks the file for the first time, it writes the whole file again, without `stopped`.

This rule matters after an update. The volume `alive` keeps the files of the old processes. Without the rule, a new container is `healthy` at its first check, 2 seconds after its start, before its own process marks the file. In a test run with the image, a new container ran a process that never marks the file. The container stayed in `health: starting`, and the check said `worker: stopped at 1795479792, no sign of life of a new process yet`. Then a process in the container marked the file, and the next check, 2 seconds later, made the container `healthy`.

### A reader never sees half a file

The process writes the whole file into a temporary file first. Then `fsync` puts the bytes on the disk, and a rename gives the file its name. A rename is one step, so a reader sees the old file or the new file, never a part. The backup does the same with its folders ([note 50](50-backup-and-restore.md)).

### The check must load little

Docker starts a new Python for each check: every 30 seconds, in two containers. So `unshelve.alive` uses only the standard library. A test starts the check in a separate Python and lists the modules that it loads. SQLAlchemy, aiogram and pydantic are not on the list.

The first measurements in the image gave 180 to 510 ms for one check. They had two causes:

| Cause | The fix |
|---|---|
| The package `unshelve` read its version from the installed metadata at each import. This read takes longer than the whole check | The package now reads the version only when a module asks for it |
| The base image has no *bytecode* of the standard library, and `PYTHONDONTWRITEBYTECODE` stops Python from writing it. So each start compiled each module again | The image compiles the bytecode once, when it is built. The image grows by about 19 MB |

After the two fixes, one check takes 37 to 65 ms in the image.

## 2. A healthcheck shows a problem, but it does not repair it

A test container of the bot, with the healthcheck of `compose.yaml`, got `SIGSTOP` 8 seconds after its last sign of life. `SIGSTOP` freezes a process: it does not run, but it does not end. These are the checks that Docker recorded:

| Time | The age of the sign of life | The check | The state |
|---|---|---|---|
| 23:41:50 | 0 s | the last sign of life | `healthy` |
| 23:41:58 | 8 s | `SIGSTOP` | `healthy` |
| 23:44:25 | 155.8 s | passes | `healthy` |
| 23:44:55 | 186.7 s | fails (1) | `healthy` |
| 23:45:27 | 217.2 s | fails (2) | `healthy` |
| 23:45:57 | 247.5 s | fails (3) | `unhealthy` |
| 23:46:37 | 287 s | 40 s later: no restart, `RestartCount` is 0 | `unhealthy` |

The rule behind the table:

- A check fails when the sign of life is older than 180 seconds (`--max-age 180`).
- Docker runs a check every 30 seconds (`interval`).
- After 3 failed checks in a row (`retries`), the service is `unhealthy`.

So a service becomes `unhealthy` 240 to 270 seconds after its last sign of life: the first failed check comes 0 to 30 seconds after the limit, and two more follow.

**Docker does not restart an unhealthy container.** The policy `restart: unless-stopped` acts only when the process ends. A frozen or hung process does not end. Docker only records the state. `docker compose ps` shows it, and `docker compose up -d --wait` ends with an error while a service is unhealthy.

**Why the stack does not restart it either.** A tool that restarts unhealthy containers needs the socket of Docker, `/var/run/docker.sock`. Access to this socket gives root on the computer. A test checks that no service of `compose.yaml` or `compose.prod.yaml` mounts it. A hung process needs a person, and the watcher will tell the person.

**The first minute after a start.** The healthcheck has a *start period* of 60 seconds. A failed check in that minute does not count, and Docker checks every 2 seconds. CI starts the stack with `docker compose up -d --build --wait` and no bot token. A bot without a token marks its file at once, so it is healthy after a few seconds. In the test run, the checks at 2 s and 4 s found no file yet, and the check at 7 s passed. On an update, the file of the old process says `stopped` (part 1). So `--wait` also waits until each new process marks its file.

**A wrong command must fail with exit code 1.** Docker reads the exit code of the check: 0 is healthy, 1 is unhealthy, and Docker reserves the code 2. `argparse` exits with 2 for a wrong command. So the check exits with 1 for each failure, also for a wrong command. A test has this case.

**A comparison from daily life:** a smoke detector. It makes a noise, but it does not put out the fire. A person must act.

The later parts of this note come with the watcher, one part with each work package. Parts 3 to 7 explain its core: the code that decides which message a person gets (m6-7c1 to m6-7c5). Part 8 explains the watcher service (m6-7d), and part 9 the ping from outside the computer (m6-7e). Part 10 explains the check on the host (m6-7f), and part 11 the verdict of the backup (m6-7g).

## 3. No secret leaves the watcher

The watcher will put texts from the database into its messages, for example the last error of a job. Such a text can hold a secret. This made-up error has 98 characters, and it starts with `embed: voyage 401: a person must fix it: `. An earlier cut ended it in the first 9 characters of a key. The table shows the end of the text after each step of the function `hide_secrets`:

| The text | After 1. values | After 2. cut ends | After 3. patterns |
|---|---|---|---|
| `…key=MADEUPKEY0123456 claude=sk-ant-MADEUP01 key=MADEUPKEY` | `…key=<secret> claude=sk-ant-MADEUP01 key=MADEUPKEY` | `…key=<secret> claude=sk-ant-MADEUP01 key=<secret>` | `…key=<secret> claude=<anthropic key> key=<secret>` |

The watcher holds the key `MADEUPKEY0123456`. The key `sk-ant-MADEUP01` has the shape of a key of Anthropic, and the watcher does not hold it. Both keys are made up. The clean step and the cut change nothing here, because the text is one line of fewer than 200 characters. So the last column is the end of the result.

The steps of `hide_secrets`, in this order:

1. **Values.** The watcher knows the value of each secret that it holds. Each place of a value becomes `<secret>`.
2. **Cut ends.** An earlier cut can leave the first part of a secret at the end of a text. If a text ends with the first 8 or more characters of a value, these characters become `<secret>`. The same rule applies to the last characters of a value at the start of a text. White space at an end does not count, because a line break can come after a cut.
3. **Patterns.** A *pattern* (a regular expression) finds each text with a given shape. The patterns find keys that the watcher does not hold, for example a key that starts with `sk-ant-`.
4. **Clean.** A *control character* or a *format character* is a character that a person cannot see. Examples are the character with the code 0 and a zero-width space. In an error text, each run of white space, such as a tab or a line break, becomes one space. The clean step removes each other such character. In a message, each `\n` stays. The clean step removes each other such character, also `\r`. The removal of a character can join two parts of a secret, so steps 1 to 3 run again on the clean text.
5. **Cut.** If an error text has more than 200 characters, it keeps its first 199 characters and gets "…" at the end. Step 5 does not cut a message.

The end rule of step 2 changes the result only when step 5 keeps the end of the text. This is so for a message, and for an error text that has 200 characters or fewer before step 5, as in the example. The worker cuts an error at 2,000 characters, and an event keeps 300 characters.

For example, put 1,902 letters before the made-up error. Then the result has 200 characters, ends in "…", and holds no `key=`. With 1,902 spaces, the clean step makes one space of them. Then the result has 90 characters, and step 2 hides the first 9 characters of the key at the end.

The start rule of step 2 matters for each text, because the cut keeps the start. For example, a text of 2,000 characters starts with `EY0123456`, the last 9 characters of the key. The result starts with `<secret>`. Without step 2, it starts with `EY0123456`.

The values come from the function `secrets_of`. It reads each setting of the type `SecretStr`. So `secrets_of` also finds a new secret setting, with no change in `hide.py`. Pydantic shows such a value as `**********` when a program prints the settings. The function also adds `ANTHROPIC_API_KEY`, which is not a setting, and the password of the database. It also adds the token in `UNSHELVE_PUBLISH_GIT_URL`, if the address holds one.

An address can write "@" as "%40". So the password is a value in three forms: as it is in the address, decoded and encoded. A query decodes "+" as a space, so that decoded form is a value too. One address can also give up to three different passwords. This table shows the result of each program for the made-up password `Q7#hk2@Lm9vX4pZr8TnW` in the address `postgresql+psycopg://unshelve:Q7#hk2@Lm9vX4pZr8TnW@db:5432/unshelve`:

| Who | The password | The rest of the address |
|---|---|---|
| SQLAlchemy, which connects | `Q7#hk2`: it ends at the first "@" | SQLAlchemy puts it into the host: `Lm9vX4pZr8TnW@db` |
| The function `urlsplit` of Python | none | `urlsplit` ends the address at the "#". It reads `unshelve:Q7` as the host and the port |
| `deploy/compose.yaml`, which writes the address | `Q7#hk2@Lm9vX4pZr8TnW`: it puts the password in as it is | |

Each of these passwords is a value if it has 12 or more characters. Here `Q7#hk2` has only 6 characters, so it is not a value. SQLAlchemy puts the *rest*, the part of the password after its first raw "@", into the host. An error of the connection can quote the host. So the rest is a value too, here `Lm9vX4pZr8TnW`. SQLAlchemy can also read a part of the rest as the port or as the database. It can also read a part as an IPv6 host, or as a key or a value of the query.

So each part of the rest from one *place* to a later place is a value too, if it has 12 or more characters. A place is the start or the end of the rest, or one of the characters "@", ":", "/", "?", "#", "=", "&", "[" and "]". A part can hold up to 8 places. The whole rest is always a value. With this limit, the number of values grows in proportion to the length of the password, not to its square. For example, with the password `Abc@[ijkl:mnop:qrst]`, SQLAlchemy reads the host `ijkl:mnop:qrst`, from "[" to "]". Each part between two ":" has only 4 characters, but the host has 14. The query of the address can also hold the password, as in `?password=...`, so that password is a value too.

The type `Secrets` holds the values. Like a `SecretStr`, it shows no value when a program prints it. For two values, `print(secrets)` shows `Secrets(2 hidden)`.

**Why the cut comes last.** In this example, a key comes after 195 or 185 characters of a long error. The result must have at most 200 characters:

| The order | The end of the result, key after 195 characters | The end of the result, key after 185 characters |
|---|---|---|
| Hide, then cut (ours) | `xxxxxxx<sec…` | `xxxxx<secret> and t…` |
| Cut, then hide | `xxxxxxxMADE…` | `xxxxxMADEUPKEY01234…` |

If the cut comes first, the text ends with "…", and not with the part of the key. So step 2 does not find the part, whatever its length. In the first case, 4 characters of the key stay, and in the second case 14. If the cut comes last, the key is already `<secret>` when the cut starts.

**Why a lookbehind, and not `\b`.** The bot sends each request to an address that holds its token. This is the address with the made-up token of the tests:

| The pattern of a bot token | The result for `https://api.telegram.org/bot123456789:MADE-UP-token_for_the_tests-only/getMe` |
|---|---|
| `(?<![0-9])[0-9]{5,12}:[A-Za-z0-9_-]{30,}` (ours) | `https://api.telegram.org/bot<bot token>/getMe` |
| `\b[0-9]{5,12}:[A-Za-z0-9_-]{30,}` | no change: the token stays |

`\b` is a *word boundary*: a place between a word character and a character that is not a word character. A letter, a digit and "_" are word characters. The "t" of "bot" and the "1" are both word characters, so `\b` does not match between them. A *lookbehind* checks the character before a place, and it does not take the character into the match. `(?<![0-9])` only says that no digit comes before the token.

**Why the default password stays.** The default password of the database is `unshelve`. Many fix texts name the command `unshelve`, for example `docker compose exec worker unshelve llm check`. A value of fewer than 12 characters is not a secret, so `secrets_of` gives no value for this password. The fix text stays as it is. A password of 13 characters, such as `Abcdefgh12345`, becomes `<secret>`.

**What stays a risk.** These secrets, or parts of them, can stay in a text:

- A key that the watcher does not hold, and that has no pattern. The secret key of S3 has no pattern. So the watcher hides it only when the watcher gets its value.
- A part of the password after a raw "@" that has fewer than 12 characters. For example, with the password `Abcdefgh12345@xyz`, the host `xyz@db` stays in an error.
- A token in the user part of the address of the git target, when the address also holds a password. For example, the user part can be the token `ghp_MADEUPtoken0123456789`, and the password can be `x-oauth-basic`. Then only `x-oauth-basic` is a value. If a cut ends a text in `ghp_MADEUPtoken0`, these 16 characters stay. If the text holds the whole address, the pattern of the user part hides the token.
- The last part of a key that an invisible character splits, when the first part has the shape of a pattern. The first run of the steps puts a *label*, such as `<anthropic key>`, in place of the first part. Then the second run cannot find the whole value or the whole shape. In a test run, 19 characters of a held key of 43 characters stayed.
- A key that a different invisible character splits. The clean step keeps U+034F and the variation selectors U+FE00 to U+FE0F. A person cannot see them, but they are not control characters or format characters.
- A value that a library cuts in its middle. For example, pydantic shows a made-up key of 98 characters as `sk-ant-api03-MADEUP0made...MADEUP0made0up0TAIL0end`. The pattern of Anthropic hides the first part. The last 23 characters stay, because step 2 looks only at the two ends of the text.

**What the watcher hides by mistake.** A pattern or step 2 can also hide a text that is not a secret. The watcher hides too much rather than too little. These results come from test runs:

| The text | The result | The cause |
|---|---|---|
| `branch task-ant-colony-optimisation-notes was deleted` | `branch ta<anthropic key> was deleted` | The pattern of Anthropic has no lookbehind |
| `config eu_pa-region-config-for-the-staging-cluster is missing` | `config eu_<voyage key> is missing` | A word after "_" or "/" can have the shape of a key of Voyage |
| `item 1795504440:the-very-long-slug-of-an-article-title failed` | `item <bot token> failed` | The text has the shape of a bot token |
| `Authentication scheme bearer is required for this endpoint` | `Authentication scheme Bearer <secret> required for this endpoint` | The pattern hides the word after each "bearer" |
| `fetch: 404 for https://example.com?email=reader@example.org` | `fetch: 404 for https://<secret>@example.org` | The pattern of the user part reads an "@" in the query |
| `Forbidden: bot was blocked by the user, bot 7412589630` | `Forbidden: bot was blocked by the user, bot <secret>` | Step 2: the id of the bot is the first part of the held token |
| `cannot reach the database` | `cannot reach the <secret>` | Step 2: with the password `Pa55@database-unshelve`, the part `database-unshelve` is a value |
| `database "unshelve_production" does not exist` | `database "<secret>" does not exist` | The address `postgresql+psycopg://unshelve:Abcdefgh12345@db:5432/unshelve_production?application_name=watch@host` has an "@" in its query. The raw password ends at that "@", so the name of the database and the key `application_name` are values |

The quickstart asks for letters and digits only in the password, so the case of `Pa55@database-unshelve` does not occur with such a password. The last case occurs also with such a password, here `Abcdefgh12345`. But `deploy/compose.yaml` writes the address with no query, so the last case does not occur with it. The work package m6-7e adds the address of the ping as a secret (part 9). Then step 2 can hide `https://` at the end of a text, so m6-7e must look at this case.

**A comparison from daily life:** an office blacks out each name in a letter before the letter leaves the office. A name that the edge of the page cut in half gets a black bar too. The office cuts the letter to one page only after the black bars are on it.

## A choice that we made

**How a process shows that it still works:**

| | A file with a time (ours) | An HTTP endpoint in each process | A row in the database |
|---|---|---|---|
| What the process needs | nothing new | a web server in the worker and in the bot | a connection to the database |
| What the check needs | the standard library | an HTTP client | a driver of the database |
| When the database is down | the sign of life is still correct | the sign of life is still correct | the sign of life is lost, so one failure hides another |
| How the watcher reads it | the shared volume `alive` | the network | the database |

A file needs no new server and no new connection. The API already has an endpoint, `/api/v1/readyz`, because a web server is its job.

## What a senior engineer checks here

- **Does the sign of life prove the right thing?** The bot marks only after an answer of `getUpdates`. The worker marks when its loop turns, also while the database is down. So the sign of life of the worker does not say that its jobs succeed. A test has the database down, and the worker still marks.
- **Is the check cheap?** One check takes 37 to 65 ms. A test lists the modules that it loads. Without the two fixes of part 1, it took 180 to 510 ms.
- **Can the clock stop the signs of life?** No. When the clock goes back, for example by one hour, the next call of `alive.mark` writes at once. Without this case, the process would not mark the file for one hour.
- **Does a failed write stop the worker?** No. `alive.mark` never raises an `OSError`. It writes one warning to the log, and the healthcheck then shows the problem.
- **Does the check need more rights?** No. No service mounts the socket of Docker.
- **Can the tests fail?** Yes. We planted 15 mistakes, one at a time. Three examples:
  - the middleware marks the file after each method, not only after `getUpdates`;
  - the limit of the check uses `>=`;
  - the worker does not mark the file after a heartbeat of the lease.

  Each mistake made a named test fail. The first run missed one mistake: the rule of one write in 10 seconds. The cause was `pytest.approx`: its relative limit is 1795 s at this size of time. The tests now compare the times of files with an absolute limit of 10 ms.
- **Does a test use the real clock?** Yes. Most tests of the check give a fake clock. So one more test starts the command of the healthcheck on a file that is 181 seconds old, with the real clock of the command. This test matters: `time.monotonic` counts from the start of the computer, not from 1970. With it as the clock, each age would be 0, and each file that exists would pass.
- **Is a folder of the socket also the socket?** Yes. A mount of `/var/run` or `/` gives the socket too. So the test of the socket also refuses a folder of the computer that holds it.
- **What is not done:**
  - no message to a person yet: the watcher comes later;
  - no restart of an unhealthy container;
  - a crash writes no `stopped`, so for up to 180 seconds, the check of a new container can pass on the file of the old process;
  - one name is one process: two workers in one stack would share the file `worker`.

## Exercise (10 minutes)

Freeze the bot of the stack, and look at what Docker does. Telegram keeps the updates for 24 hours, so no post is lost. Run the commands in the root of the clone.

```bash
docker compose kill --signal SIGSTOP bot
docker compose ps bot
docker inspect --format '{{.RestartCount}}' "$(docker compose ps -q bot)"
docker compose kill --signal SIGCONT bot
```

1. Run the first command. Then run `docker compose ps bot` every 30 seconds.
2. When the STATUS column shows `unhealthy`, run the third command.
3. Run the last command. Then run `docker compose ps bot` again.

After how many minutes is the bot `unhealthy`? Does Docker restart it? What makes it `healthy` again?

<details>
<summary>The answer</summary>

The bot is `unhealthy` 240 to 270 seconds after its last sign of life. The bot marks the file about every 10 to 20 seconds, so this is about 3.5 to 4.5 minutes after the first command. In the test run of part 2, it took 239 seconds after `SIGSTOP`.

Docker does not restart it. `RestartCount` stays 0, and the STATUS column still says `Up`. The restart policy acts only when the process ends, and a frozen process does not end.

After `SIGCONT`, the bot runs again. It marks the file again at the next answer of `getUpdates`, and the next check passes. One passed check makes the service `healthy` again. In the test run, a bot without a token was `healthy` 20 seconds after `SIGCONT`.

</details>

## Words used in this note

| Word | Meaning |
|---|---|
| Liveness file | A small file of one process. The time of its last change is the sign of life of the process |
| Sign of life | A new time on the liveness file |
| Mark | To give the liveness file a new time. The process calls the function `alive.mark` |
| Heartbeat | The message that keeps the lease of a job ([note 5](05-worker-loop.md)). It is not a sign of life |
| mtime | The time of the last change of a file. The file system keeps it |
| Healthcheck | A command that Docker runs in a container again and again. Exit code 0 means healthy, 1 means unhealthy |
| Start period | The first time after a start. A failed check in it does not count |
| Request middleware | A function that each request of the bot to Telegram passes through |
| Fact | A `key=value` line of the liveness file, such as `chats_ready=1/2` |
| Bytecode | The compiled form of a Python module, a `.pyc` file. Python makes it before it runs a module |

## To read more

- [HEALTHCHECK](https://docs.docker.com/reference/dockerfile/#healthcheck), Dockerfile reference. Read the options and the exit codes: 0 is healthy, 1 is unhealthy, 2 is reserved.
- [Start containers automatically](https://docs.docker.com/engine/containers/start-containers-automatically/), Docker documentation. Read the table of restart policies: each policy acts when a container exits, not when it is unhealthy.
- [Client session middlewares](https://docs.aiogram.dev/en/latest/api/session/middleware.html), aiogram documentation. Read the example with a class: `MarkAlive` has the same shape.

Previous: [53. Fewer minutes of CI](53-fewer-minutes-of-ci.md) · Back to the [list of notes](README.md)
