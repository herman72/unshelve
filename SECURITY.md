# Security

## Report a vulnerability

Report a vulnerability in private. Do not open a public issue for it.

1. Open the [advisories page](https://github.com/herman72/unshelve/security/advisories) of the repository.
2. Select **Report a vulnerability**.
3. Fill in the form: a summary, the details, a proof of concept and the impact. Name the file or the endpoint.

The report is private: only the reporter and the maintainers of the repository can see it.

## Supported versions

Unshelve has no releases. Only the `main` branch gets fixes.

## In scope

The parts that keep private data private are the most important:

- **The login.** The bot sends a one-time link only to the owner. The token is in the URL fragment, expires after 5 minutes and works once ([ADR 0008](docs/adr/0008-long-polling-and-bot-issued-login-links.md)).
- **The public feed.** The publisher reads only the `pub` view, through a role that can read nothing else ([ADR 0004](docs/adr/0004-public-data-is-a-pushed-static-subset.md)). Private text in `feed.json` is a vulnerability.
- **The fetcher.** Only `unshelve.net` opens the URL of a saved post. It refuses private, loopback and link-local addresses, also after a redirect ([ADR 0010](docs/adr/0010-one-guarded-way-to-the-internet.md)).
- **Text from the internet.** It goes only to a model with no tools, and the web app escapes it. A saved page that runs a script in the web app is a vulnerability.

## Not in scope

- The database password `unshelve` in `deploy/compose.yaml`. It is the default for a trial on one computer. `deploy/compose.prod.yaml` stops when `UNSHELVE_DB_PASSWORD` is not set.
- An attack that needs the Telegram account of the owner, the `.env` file or a shell on the server.

## Response

The project has one maintainer, so there is no promised time for an answer or a fix.
