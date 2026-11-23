# Quickstart

Unshelve runs in Docker. You can start it in two ways:

- **A. On this computer**, to try it. The web app is at `http://localhost:3000`, and only this computer can open it.
- **B. On a server**, to use it every day. The web app is at `https://` and your domain, with a certificate that Caddy gets and renews by itself.

CI runs each marked step of this guide on a fresh clone, as written here: A, B, a backup and a restore. So these steps work. The sections [Update](#update) and [Start again with an empty instance](#start-again-with-an-empty-instance) have no marked step, and CI does not run them. [Note 47](learn/47-running-on-a-server.md) explains how.

## What you need

- git.
- Docker Engine 25 or newer, with Compose 2.24 or newer. `docker version` and `docker compose version` show the versions.
- For B: a Linux server that the internet can reach on ports 80 and 443, and a domain name that points to the address of the server, for example `unshelve.example.com`.

Run each command of this guide in the root of the clone: the folder with `compose.yaml` and `.env`. Do not run Compose in `deploy/`. In that folder, Compose does not put the values of `.env` into its own file, so the address of the database gets the default password. `COMPOSE_FILE` and `COMPOSE_PROJECT_NAME` do not apply there, and the project gets the name `unshelve` from `deploy/compose.yaml`. The containers of the app (`migrate`, `api`, `worker` and `bot`) still get the values of `.env` through `env_file`.

In a subfolder such as `apps/api`, Compose reads `.env` but ignores `COMPOSE_FILE`: on a server, it uses the stack of a laptop, without Caddy. [Note 54](learn/54-compose-from-the-root.md) shows each case.

## A. On this computer

1. Get the code:

   ```bash
   git clone https://github.com/herman72/unshelve.git
   cd unshelve
   ```

2. Make the settings and start the stack. The stack needs a Telegram user id for its owner; for a trial, any number works. The first start builds the images and takes a few minutes.

   ```bash quickstart-local
   cp .env.example .env
   echo "UNSHELVE_OWNER_TELEGRAM_ID=1" >> .env
   mkdir -p backups
   docker compose up -d --build --wait
   ```

   `mkdir -p backups` makes the folder of the backups yours. Without it, Docker on Linux makes the folder for root, and you need `sudo` to read your backups.

3. Get a sign-in link:

   ```bash quickstart-local
   docker compose exec -T api unshelve auth link
   ```

   Open the link that it prints, `http://localhost:3000/login#token=…`, in your browser. A link works once, for 5 minutes.

4. Check the stack, if you want. The script asks for a second link and signs in with it:

   ```bash quickstart-local-check
   deploy/check-instance.sh http://localhost:3000
   ```

   ```text
   ok: http://localhost:3000/api/v1/readyz answers
   ok: a sign-in link gives a session
   ```

## B. On a server

Do these steps on the server.

1. Get the code, as in A.

2. Write your two values into the shell. The next step reads them:

   ```bash
   DOMAIN=unshelve.example.com
   OWNER_TELEGRAM_ID=123456789
   ```

   `DOMAIN` is the name that points to the server. `OWNER_TELEGRAM_ID` is your numeric Telegram user id: the bot and the sign-in answer this user only.

3. Make the settings. This block copies the example settings, lets only you read them, and adds the production values at the end, with a new random password for the database:

   ```bash quickstart-server
   cp .env.example .env
   chmod 600 .env
   cat >> .env <<EOF

   # --- Production (docs/quickstart.md) --------------------------------------------
   # The last line of a name counts, so these lines win over the same names above.
   COMPOSE_FILE=compose.prod.yaml
   UNSHELVE_DOMAIN=$DOMAIN
   UNSHELVE_DB_PASSWORD=$(openssl rand -hex 24)
   UNSHELVE_OWNER_TELEGRAM_ID=$OWNER_TELEGRAM_ID
   UNSHELVE_BACKUP_DIR=$HOME/unshelve-backups
   EOF
   mkdir -p "$HOME/unshelve-backups"
   ```

   `COMPOSE_FILE=compose.prod.yaml` makes each `docker compose` command in the root of the clone use the production stack. That stack has Caddy in front of the web app, and no other open port. `UNSHELVE_BACKUP_DIR` keeps the backups outside the clone, so that a new clone cannot delete them. Give it an absolute path: Docker Compose reads a relative path from the folder `deploy/`, not from the clone.

4. Start the stack:

   ```bash quickstart-server
   docker compose up -d --build --wait
   ```

   At the first start, Caddy asks Let's Encrypt for a certificate for your domain. `docker compose logs caddy` shows each step. When the DNS record does not point to the server yet, or when port 80 is closed, Caddy tries again later.

5. Get a sign-in link:

   ```bash quickstart-server
   docker compose exec -T api unshelve auth link
   ```

   Open the link that it prints, `https://unshelve.example.com/login#token=…`, in your browser.

6. Check the stack, if you want:

   ```bash quickstart-server-check
   deploy/check-instance.sh "https://$DOMAIN"
   ```

   ```text
   ok: https://unshelve.example.com/api/v1/readyz answers
   ok: a sign-in link gives a session
   ok: the session cookie is Secure
   ok: a browser keeps to HTTPS for this name (HSTS)
   ok: http://unshelve.example.com/ sends the browser to https://unshelve.example.com/
   ```

## After the first start

- **Telegram.** To capture what you post, set `UNSHELVE_TELEGRAM_BOT_TOKEN` and `UNSHELVE_CAPTURE_CHAT_IDS` in `.env`, then run `docker compose up -d --wait`. The comments in `.env.example` explain each setting. Then the bot also sends you a sign-in link when you write `/login` to it.
- **Real providers.** By default, the stack uses fake providers and no web, so it costs nothing. `UNSHELVE_PROVIDERS=live` with `ANTHROPIC_API_KEY`, and `UNSHELVE_FETCHER=live`, make real cards from real pages. The comments in `.env.example` give the prices and the limits. A key in `.env` is a secret: on a computer that others use too, run `chmod 600 .env`, as step 3 of B does.
- **The public feed.** Nothing is published by default. To commit the feed of the public items to the git repository of a site, follow [keys/README.md](../keys/README.md): it makes a deploy key, and it gives the lines for `.env`. [Note 43](learn/43-git-target.md) explains the target, and `.env.example` lists the other targets.
- **Update.** Follow [Update](#update): a backup comes first, and a check of the database comes after `git pull`.
- **Stop.** `docker compose stop` stops the stack, and `docker compose start` starts it again. `docker compose down` removes the containers and keeps the data.
- **The data.** The database is in the Docker volume `db-data`, the files of the posts are in `blobs`, and the certificates are in `caddy-data`. `docker compose down --volumes` deletes these volumes. The backups are in a folder of the computer, not in a volume: see "Backups". To delete the data and start with an empty instance, follow [Start again with an empty instance](#start-again-with-an-empty-instance).
- **A new database password.** Postgres reads `UNSHELVE_DB_PASSWORD` only when it makes the volume `db-data`. To change it later, change it in the database first, then in `.env`, then restart:

  ```bash
  docker compose exec db psql -U unshelve -c "ALTER USER unshelve PASSWORD 'the-new-password'"
  ```

  Then set the same value in `.env` and run `docker compose up -d --wait`. Use letters and digits only, because the password is also part of the database address.

## Update

Run these steps in the root of the clone. A backup comes before the new code. The bot, the worker, the API and the web app stop before the backup, and they start again in step 7. So no post comes in after the backup, and a restore of the backup loses no post.

1. Stop the bot, the worker, the API and the web app. Telegram keeps the new posts for the bot for 24 hours, and the bot gets them in step 7:

   ```bash
   docker compose stop bot worker api web
   ```

2. Make a backup (or `make backup`). If the update goes wrong, this backup brings the data back:

   ```bash
   docker compose exec -T backup bash /deploy/backup/backup.sh once
   ```

3. Get the new code:

   ```bash
   git pull
   ```

4. Start the database with the image of the new code:

   ```bash
   docker compose up -d --wait db
   ```

5. Compare two versions of the C library: the version that the database recorded, and the version in its image:

   ```bash
   docker compose exec -T db psql -U unshelve -d unshelve -Atc "SELECT datcollversion, pg_database_collation_actual_version(oid) FROM pg_database WHERE datname = current_database()"
   ```

   Two equal values, for example `2.36|2.36`, show that the C library did not change. Then go to step 7.

6. If the two values are different, for example `2.36|2.41`, do this step. psql then also prints the warning `database "unshelve" has a collation version mismatch`. The new image has a new C library (glibc), and that library sorts text and makes small letters. So the indexes of text and the search columns can hold old results ([note 46](learn/46-site-search.md)). Write the search columns again, build each index again, and record the new version:

   ```bash
   docker compose exec -T db psql -U unshelve -d unshelve -v ON_ERROR_STOP=1 \
     -c "UPDATE items SET title = title" \
     -c "UPDATE item_contents SET text = text" \
     -c "REINDEX DATABASE unshelve" \
     -c "ALTER DATABASE unshelve REFRESH COLLATION VERSION" \
     -c "ALTER DATABASE template1 REFRESH COLLATION VERSION" \
     -c "ALTER DATABASE postgres REFRESH COLLATION VERSION"
   ```

   Each `UPDATE` writes each row again, so Postgres computes the search columns with the new library. The last two commands record the new version for `template1` and `postgres`. These two databases hold no table of the app. `CREATE DATABASE` copies `template1`, and Postgres refuses a template with an old version, for example in the tests. After this step, the command of step 5 prints two equal values.

7. Start the stack with the new code. The service `migrate` updates the database before the API starts:

   ```bash
   docker compose up -d --build --wait
   ```

If step 6 stops with an error, an index can be broken. Then restore the backup of step 2: follow [Restore a backup](#restore-a-backup) from step 3. A restore builds each index again from the data.

An instance without the service `backup` cannot do step 2. For it, make the folder of the backups before step 7: `mkdir -p backups` on this computer. On a server, set `UNSHELVE_BACKUP_DIR=<an absolute path>` in `.env`, and run `mkdir -p` for that path. If Docker already made the folder for root, run `sudo chown -R "$(id -u):$(id -g)" backups`.

## Backups

The service `backup` makes a backup of the database and of the files that you sent, when the newest backup is older than a day. The first backup comes some seconds after the first start. A computer that slept through the time makes the backup when it wakes; a computer that is off makes none. [Note 50](learn/50-backup-and-restore.md) explains how a backup and a restore work.

The backups are in the folder `backups/` of the clone, or in `UNSHELVE_BACKUP_DIR` (an absolute path). The service runs as root; after each backup, it gives the new files to the owner of the folder. Each backup is a folder with the time of its start in UTC, for example `backups/db/20261026T090000Z/`. The files that you sent are in one mirror, `backups/blobs/`, for all the backups. The service keeps the 7 newest backups and the newest one of each of the last 6 calendar weeks, so the oldest backup is 29 to 35 days old.

1. Make a backup now, and see the state of the backups (or `make backup` and `make backup-status`):

   ```bash quickstart-backup
   docker compose exec -T backup bash /deploy/backup/backup.sh once
   docker compose exec -T backup bash /deploy/backup/backup.sh status
   ```

2. When the last backup failed, or when no backup worked for two days, `docker compose ps` shows the service `backup` as `unhealthy`, and `make backup-status` says why. While the service is unhealthy, `docker compose up -d --wait` ends with an error about it, after the other services started. When the cause is fixed, `make backup` makes the service healthy again; without it, the service tries again after 60 minutes.

   `docker compose logs backup` has one JSON line for each backup that the service starts. A backup that you start with `make backup` writes its line in your terminal.

3. Copy a backup to a second place. The backups are on the same disk as the data, so a lost or broken computer takes both. This command writes the newest backup, with each file that it needs, into one tar file that only you can read:

   ```bash quickstart-export
   (umask 077; docker compose exec -T backup bash /deploy/backup/backup.sh export latest > ~/unshelve-backup.tar)
   ```

   The tar holds your private data. Encrypt it before it leaves the computer, for example with [age](https://github.com/FiloSottile/age): `age -r <your public key> -o ~/unshelve-backup.tar.age ~/unshelve-backup.tar`, then remove the tar and copy the `.age` file.

4. Test the backups once a month with a drill. The drill restores the newest backup into a second, empty stack, checks the data, and removes the second stack. Your stack runs on, and the backups do not change (or `make restore-drill`):

   ```bash quickstart-drill
   deploy/backup/drill.sh
   ```

   The last line says `the data check passed`. A backup that a drill cannot restore is not a backup: find the cause before you need the backup.

## Restore a backup

A restore puts a backup into an empty instance, and then it checks the data. Use it after a lost or broken computer, or after a mistake in the data.

1. If the old instance still runs on another computer, stop it there first: `docker compose stop bot worker`. Two bots with one token get errors from Telegram, and two workers send the digest twice.

2. On a new computer only: get the code (step 1 of A or of B), and make `.env` as in step 2 of A or step 3 of B, but do not start the stack yet: its first backup would be an empty one. In `.env`, keep `UNSHELVE_OWNER_TELEGRAM_ID` and `UNSHELVE_CAPTURE_CHAT_IDS` of the old instance, or the restored data belongs to another user. Copy the secrets from your password manager, and give the database a new password: a restore needs no old password. Then put the backup in place, and see its name:

   ```bash quickstart-restore-files
   mkdir -p backups
   tar -xf ~/unshelve-backup.tar -C backups
   tar -tf ~/unshelve-backup.tar | sed -n 1p
   ```

   The last line prints `db/<the name of the backup>/`. (Not `head -n 1`: it closes the pipe early, and GNU tar then stops with a write error.) For a copy that you encrypted, use `age -d -i <your key file> ~/unshelve-backup.tar.age | tar -x -C backups`. On a server, use the folder of `UNSHELVE_BACKUP_DIR`.

3. Choose the backup by its name. `docker compose run --rm restore list` shows each backup with its time and its rows, the newest last:

   ```text
   20261027T173143Z	made 2026-10-27 17:31 UTC	11 items	3 files
   ```

   After a mistake in the data, choose a backup from before the mistake. Then keep its name for the next steps, for example `NAME=20261027T173143Z`.

4. Test that backup with a drill first. The drill restores it into a second, empty stack and checks it; your instance does not change:

   ```bash quickstart-restore-test
   deploy/backup/drill.sh "$NAME"
   ```

   Go on only when the last line says `the data check passed`. If the drill says that a file of the mirror is damaged, do not delete anything: while your instance still has its files, remove the damaged files from `backups/blobs`, run `make backup`, and test again.

5. Restore. A restore needs an empty instance, so the first command deletes the database and the files of this stack. The backups stay, because they are in a folder, not in a volume. On a server, the certificates go too, and Caddy gets new ones. The commands are one chain: when one fails, the next ones do not run.

   ```bash quickstart-restore
   docker compose down --volumes &&
   docker compose up -d --wait db &&
   docker compose run --rm restore "$NAME" &&
   docker compose up -d --build --wait
   ```

   The restore checks the backup before it changes the database: a damaged backup, a backup from newer code, or a database that is not empty stops it. Then it restores the database and the files, and checks each fact of the backup:

   ```text
   ok: the backup 20261027T173143Z is whole: its checksums, its dump and the 3 files that it needs
   the backup: 20261027T173143Z made 2026-10-27 17:31 UTC 11 items 3 files
   ok: the versions: Postgres 17, revision 0006, and the extensions
   ok: the database: pg_restore of 20261027T173143Z, then ANALYZE
   ok: the files: 3 copied into the volume, for the user 10001
   ok: the facts of the database equal the facts of the backup (192 lines)
   …
   the data check passed: the database and the files equal the backup 20261027T173143Z
   ```

   When a step stops, the restore says why. A restore that stopped after the database goes on with the files when you run `docker compose run --rm restore "$NAME"` again.

6. The work after the backup is not in it. The posts that you saved after it come back with an import of an export of your channel from Telegram Desktop: `docker compose exec api unshelve import /imports/<folder> --timezone <your zone> --since <a day before the date of the backup>`. The name of a backup is in UTC, and the import skips each post that it has already, so a day earlier is safe. Then make a new backup: `make backup`.

## Start again with an empty instance

These steps delete the data of an instance and start an empty instance, for example after a trial with test posts. The old backups go to an archive outside the clone, and a drill tests the archive before the delete. So a restore can still bring the old data back.

Run each command in the root of the clone, in one shell. Steps 2 and 3 set `BACKUPS` and `ARCHIVE` for the later steps. Post nothing in the channel before the end of step 12. Telegram keeps the posts for the bot, and the bot stores them in the new database when it starts.

1. Stop the bot, the worker, the API, the web app and the service `backup`. Then nothing writes to the database, and no backup removes an old backup:

   ```bash
   docker compose stop bot worker api web backup
   ```

2. Find the folder of the backups, as the stack and the drill find it. They look in the shell first, then in the last `UNSHELVE_BACKUP_DIR` line of `.env`, then in `backups/` of the clone. A relative path that starts with `./` or `../` counts from `deploy/`. The last command lists the backups by name:

   ```bash
   BACKUPS=${UNSHELVE_BACKUP_DIR:-$(sed -n 's/^UNSHELVE_BACKUP_DIR=//p' .env | tail -n 1 | sed "s/^[\"']//; s/[\"']$//")}
   BACKUPS=${BACKUPS:-$PWD/backups}
   case $BACKUPS in /*) ;; *) BACKUPS=$PWD/deploy/$BACKUPS ;; esac
   ls "$BACKUPS/db"
   ```

   If `ls` shows an error or no backup, stop here: the folder is not correct.

3. Make an archive folder that only this user account can read (mode 700), and copy the backups into it. Copy them before any new backup, because each backup removes old backups by the rules of [Backups](#backups):

   ```bash
   ARCHIVE="$HOME/unshelve-archive-$(date -u +%Y%m%dT%H%M%SZ)"
   mkdir -m 700 "$ARCHIVE"
   cp -Rp "$BACKUPS" "$ARCHIVE/backups"
   ```

4. Make a last backup, stop the stack, and copy again. The second copy adds the last backup to the archive:

   ```bash
   docker compose start backup
   docker compose exec -T backup bash /deploy/backup/backup.sh once
   docker compose stop
   cp -Rp "$BACKUPS/." "$ARCHIVE/backups/"
   ```

5. Keep the old ids and the old exports in the archive. The file `old-ids.env` keeps the lines of the owner id and of the capture chats. An import of an old export needs the id of the old channel when `.env` names another channel. Move each export folder (`ChatExport_…`) of `exports/`, and keep `exports/README.md`:

   ```bash
   (umask 077; grep -E '^(UNSHELVE_OWNER_TELEGRAM_ID|UNSHELVE_CAPTURE_CHAT_IDS)=' .env > "$ARCHIVE/old-ids.env")
   mkdir "$ARCHIVE/exports"
   find exports -mindepth 1 -maxdepth 1 -name 'ChatExport_*' -exec mv {} "$ARCHIVE/exports/" ';'
   ```

6. Test the archive with a drill. The drill restores the newest backup of the archive into a second, empty stack, and checks it:

   ```bash
   UNSHELVE_BACKUP_DIR="$ARCHIVE/backups" deploy/backup/drill.sh
   ```

   Go on only when the last line says `the data check passed`.

7. Delete the old instance. First, check the project:

   ```bash
   docker compose config | sed -n 1p
   docker compose ls --all
   ```

   The first command prints the name of the project, for example `name: unshelve`. Keep `sed -n 1p`: the other lines of `docker compose config` show each secret of `.env`. In the list of `docker compose ls --all`, the line of that name must show a Compose file of this clone. If the line shows a Compose file of another folder, stop here: `down --volumes` would delete the volumes of that other stack.

   Then delete the containers and the volumes of the project. Also remove the old backups from `$BACKUPS`, the folder of the backups. The archive has each of them, and the drill of step 6 tested the archive:

   ```bash
   docker compose down --volumes
   rm -rf -- "${BACKUPS:?}/db" "${BACKUPS:?}/blobs"
   ```

   This step deletes the database, the files that the owner sent and the state of the backups. On a server, the certificates go too, and Caddy gets new ones.

8. Give the new database a new password. Postgres reads `UNSHELVE_DB_PASSWORD` only when it makes the volume of the database. Step 7 deleted the old volume, so a new line in `.env` is sufficient. The last line of a name counts, and the command shows the password nowhere:

   ```bash
   printf '\nUNSHELVE_DB_PASSWORD=%s\n' "$(openssl rand -hex 24)" >> .env
   chmod 600 .env
   ```

   These settings are optional. Add each one as a new line at the end of `.env`:

   - `COMPOSE_PROJECT_NAME=<a name>`, for example `unshelve-live`, gives the new volumes a name of their own. Then a command in `deploy/`, or in another clone with the same folder name, cannot reach them. Set it only in this step: with a new name, Compose uses new, empty volumes. Do not copy a `.env` with this line into another clone or worktree. There, each Compose command acts on this project, also `down --volumes`.
   - `UNSHELVE_BACKUP_DIR=<an absolute path>` keeps the new backups outside the clone. Make the folder with `mkdir -p` first.
   - For a new bot or a new channel, also change `UNSHELVE_TELEGRAM_BOT_TOKEN` and `UNSHELVE_CAPTURE_CHAT_IDS` ([Connect the Telegram bot](../README.md#connect-the-telegram-bot)).

9. If the same bot stays, clear the history of the chat with the bot in Telegram. Each button of an old message names an item by its id. The new database gives the ids from 1 again, so "Done" in an old digest would mark a new item as done. A new bot has no old messages.

10. Start the stack, and check it:

    ```bash
    docker compose up -d --build --wait
    deploy/check-instance.sh http://localhost:3000
    docker compose exec -T db psql -U unshelve -d unshelve -Atc "SELECT count(*) FROM items"
    docker compose exec -T db psql -U unshelve -d unshelve -Atc "SELECT version_num FROM alembic_version"
    ls apps/api/migrations/versions/[0-9]*.py | tail -n 1
    ```

    The count of the items must be `0`. The revision, for example `0006`, must be the number at the start of the newest file, for example `0006_digest_stale_prompt.py`. On a server, give the check the `https://` address of the domain.

11. If `.env` sets `UNSHELVE_PUBLISH_TARGET` to `file`, `git` or `s3`, write the empty feed over the old feed. The site shows the old items until the next publish, and the worker publishes only after a change of a public item. First, see the feed of the new database:

    ```bash
    docker compose exec -T worker unshelve publish show
    ```

    ```text
    {
      "version": 1,
      "items": []
    }
    ```

    Then publish it:

    ```bash
    docker compose exec -T worker unshelve publish run
    ```

    An old public address can come back later with another item. A new item gets the first free public name in the new database, and that database does not know the old names.

12. Sign in again, because the sessions were in the old database. Run `docker compose exec -T api unshelve auth link`, or write `/login` to the bot. Then add the goals on the page **Goals** before the first post. A post that comes before its goals gets no score until `unshelve goals rescore` runs. With a paid model, a rescore costs money.
