# 0008. Telegram long polling and bot-issued login links

- Status: Accepted
- Date: 2026-10-10

## Context

Two questions depend on how the instance is reached from outside:

1. How does the bot receive updates? A webhook needs a public HTTPS endpoint. Long polling needs only outbound connections.
2. How does the single user log in to the web app without the project owning a password system?

Self-hosters often run behind NAT. Telegram keeps undelivered updates for at most 24 hours.

## Decision

**Updates.** Long polling is the default in every environment. A webhook mode can be added later as an option; it is not required.

**Login.**

- The user sends `/login` to the bot in the private chat. The bot answers only the allow-listed Telegram user id.
- The bot replies with a link that carries a one-time token in the URL fragment. Link previews are disabled for that message.
- The web app reads the fragment and sends the token in a POST request. The server consumes it and sets a session cookie.
- Tokens are stored hashed, expire after 5 minutes and work once.
- Sessions are server-side rows that can be revoked. The web app and the API are served from the same origin.
- A CLI command, `unshelve auth link`, issues a link for development, first run and recovery when the bot is down.

## Consequences

- Capture works on a laptop, a home server or a VPS with no inbound port and no TLS setup.
- An instance that is offline for more than 24 hours misses updates. Re-importing a Telegram Desktop export fills the gap, because import is idempotent.
- A token in the fragment is not sent to the server by a GET request, so Telegram's link preview fetcher cannot consume or leak it.
- There are no passwords to store, reset or leak.

## Alternatives considered

- **Webhook as default.** Lower latency, but a public endpoint and TLS become requirements for every installation.
- **One-time token in the query string, consumed by GET.** The preview fetcher would use the token before the user does.
- **Telegram Login Widget.** Needs a public domain registered with BotFather, so it does not work on localhost or on private networks.
