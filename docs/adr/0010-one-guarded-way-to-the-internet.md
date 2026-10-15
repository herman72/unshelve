# 0010. One guarded way to the internet

- Status: Accepted
- Date: 2026-10-15

## Context

From milestone M2, the worker opens the links that the owner saves, to read the pages. A link is text from the internet. It can name an address inside the network where Unshelve runs: `http://169.254.169.254/` (the metadata service of a cloud machine, which answers with its credentials), `http://192.168.1.1/` (a router), or `http://db/` (the database container). If the server opens such an address for a stranger, that is server-side request forgery (SSRF).

A check of the text of the link is not enough:

- The same address has many forms: `127.0.0.1`, `127.1`, `0x7f.1`, `2130706433`, `[::ffff:127.0.0.1]`, `[64:ff9b::7f00:1]`.
- A public DNS name can point to a private address, and its answer can change between two questions (DNS rebinding).
- A public page can redirect to a private address.
- A server controls the size, the speed and the compression of its answer.

[`docs/architecture.md`](../architecture.md) sets the rule: "Fetcher connects only to validated public addresses and checks every redirect". This record says how.

## Decision

- **One way out.** `net/fetcher.py` (`LiveFetcher`) is the only code that opens an address from outside. It implements the `Fetcher` contract. The extractors get it as `ctx.fetcher`.
- **The guard is the network backend.** The fetcher uses httpcore with its own network backend, `net/guard.py` (`GuardedBackend`). httpcore opens each connection through it. The backend finds the addresses of the host one time, refuses the connection if one of them is not public, and connects to a checked address, not to the name. TLS starts after that with the name, so the certificate is still checked against the name.
- **What is public** is decided by the lists in `net/addresses.py`. For IPv4: not in a special-purpose range (private, loopback, link-local, shared, multicast, reserved, documentation, and others). For IPv6: only `2000::/3`, without its special-purpose parts. An IPv4 address inside an IPv6 address (`::ffff:a.b.c.d`, NAT64 `64:ff9b::a.b.c.d`) is decided by the IPv4 address. An address with a zone (`fe80::1%en0`) is refused.
- **Old forms of IPv4 are read locally** (`127.1`, `0x7f.1`, `2130706433`), so no DNS question is asked for them. A host that ends in a number but is no address is refused.
- **Only http and https, and only the ports 80 and 443.**
- **At most 5 redirects.** Each new connection passes the guard again.
- **Size:** `max_bytes` counts the bytes before and after decompression. The request offers only `gzip` and `deflate`, and an answer in a different encoding is refused.
- **Time:** one limit for the whole fetch with its redirects (`UNSHELVE_FETCH_TIMEOUT`, 30 s).
- **Politeness:** a pause between two requests to one host (`UNSHELVE_FETCH_HOST_INTERVAL`, 1 s), in each process. A fixed User-Agent.
- **Nothing from the environment:** no cookies, no credentials from the link, no proxy from environment variables.
- **The contract gets two additions:** the reason `no_such_host` (a dead link; not retried) and `FetchError.retry_after` (the seconds of a `Retry-After` header).

## Consequences

- A new way to open a URL would go around the guard. So no module outside `unshelve.net` may import an HTTP client library; a test checks the imports of the whole package.
- A page that only has private addresses (an intranet) cannot be fetched. That is the purpose.
- A page on a different port, such as `:8080`, cannot be fetched. Its item falls back to the saved text (partial).
- The pause is kept in each process. Two worker processes can each send one request to the same host in one second.
- An IPv6-only host with NAT64 still works, because NAT64 addresses are decided by their IPv4 address.

## Alternatives considered

- **Check the name, then let the HTTP library connect to the name.** Simple, but the library asks DNS a second time. A DNS server can answer the check with a public address and the connection with a private one.
- **aiohttp with its own resolver.** aiohttp does not ask the resolver for an address that is written in the link, so the check would need a second place.
- **An egress proxy, such as Stripe's Smokescreen.** It protects each program on the machine, also code that forgets the rule. But it is one more service to run on a laptop. It is a good candidate for the hosting decision of M6.
- **Firewall rules for the worker container.** A good second layer for M6. They are not portable for local development on macOS.
