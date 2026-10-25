# Quickstart

Unshelve runs in Docker. You can start it in two ways:

- **A. On this computer**, to try it. The web app is at `http://localhost:3000`, and only this computer can open it.
- **B. On a server**, to use it every day. The web app is at `https://` and your domain, with a certificate that Caddy gets and renews by itself.

CI runs each marked step of this guide on a fresh clone, as written here, for A and for B. So these steps work.

## What you need

- git, and an SSH key that GitHub knows for your account.
- Docker with Compose 2.24 or newer. `docker compose version` shows the version.
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
   docker compose up -d --build --wait
   ```

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
   EOF
   ```

   `COMPOSE_FILE=compose.prod.yaml` makes each `docker compose` command in this folder use the production stack: Caddy in front of the web app, and no other open port.

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
- **Update.** Run `git pull`, then `docker compose up -d --build --wait`. The service `migrate` updates the database before the API starts.
- **Stop.** `docker compose stop` stops the stack, and `docker compose start` starts it again. `docker compose down` removes the containers and keeps the data.
- **The data.** The database is in the Docker volume `db-data`, the files that you send are in `blobs`, and the certificates are in `caddy-data`. `docker compose down --volumes` deletes all of them, and nothing brings them back. Issue #40 adds a backup and a restore drill.
- **A new database password.** Postgres reads `UNSHELVE_DB_PASSWORD` only when it makes the volume `db-data`. To change it later, change it in the database first, then in `.env`, then restart:

  ```bash
  docker compose exec db psql -U unshelve -c "ALTER USER unshelve PASSWORD 'the-new-password'"
  ```

  Then set the same value in `.env` and run `docker compose up -d --wait`. Use letters and digits only, because the password is also part of the database address.
