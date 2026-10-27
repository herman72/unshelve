# Quickstart

Unshelve runs in Docker. You can start it in two ways:

- **A. On this computer**, to try it. The web app is at `http://localhost:3000`, and only this computer can open it.
- **B. On a server**, to use it every day. The web app is at `https://` and your domain, with a certificate that Caddy gets and renews by itself.

CI runs each marked step of this guide on a fresh clone, as written here, for A and for B. So these steps work. [Note 47](learn/47-running-on-a-server.md) explains how.

## What you need

- git, and an SSH key that GitHub knows for your account.
- Docker Engine 25 or newer, with Compose 2.24 or newer. `docker version` and `docker compose version` show the versions.
- For B: a Linux server that the internet can reach on ports 80 and 443, and a domain name that points to the address of the server, for example `unshelve.example.com`.

## A. On this computer

1. Get the code:

   ```bash
   git clone git@github.com:herman72/unshelve.git
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

   `COMPOSE_FILE=compose.prod.yaml` makes each `docker compose` command in this folder use the production stack: Caddy in front of the web app, and no other open port. `UNSHELVE_BACKUP_DIR` keeps the backups outside the clone, so that a new clone cannot delete them. Give it an absolute path: Docker Compose reads a relative path from the folder `deploy/`, not from the clone.

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
- **Update.** Run `git pull`, then `docker compose up -d --build --wait`. The service `migrate` updates the database before the API starts. The first update that brings the service `backup` needs a folder for the backups first: `mkdir -p backups` on this computer, or, on a server, `UNSHELVE_BACKUP_DIR=<an absolute path>` in `.env` and `mkdir -p` of that path. If Docker already made the folder for root, run `sudo chown -R "$(id -u):$(id -g)" backups`.
- **Stop.** `docker compose stop` stops the stack, and `docker compose start` starts it again. `docker compose down` removes the containers and keeps the data.
- **The data.** The database is in the Docker volume `db-data`, the files that you send are in `blobs`, and the certificates are in `caddy-data`. `docker compose down --volumes` deletes these volumes. The backups are in a folder of the computer, not in a volume: see "Backups".
- **A new database password.** Postgres reads `UNSHELVE_DB_PASSWORD` only when it makes the volume `db-data`. To change it later, change it in the database first, then in `.env`, then restart:

  ```bash
  docker compose exec db psql -U unshelve -c "ALTER USER unshelve PASSWORD 'the-new-password'"
  ```

  Then set the same value in `.env` and run `docker compose up -d --wait`. Use letters and digits only, because the password is also part of the database address.

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
