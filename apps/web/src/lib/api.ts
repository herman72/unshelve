import { createApiClient } from "@unshelve/api-client";
import { headers } from "next/headers";
import { connection } from "next/server";

// Server components call the API directly on the internal network.
// Browser code uses relative /api paths, which next.config.ts proxies: see lib/browser-api.ts.
const API_INTERNAL_URL = process.env.UNSHELVE_API_INTERNAL_URL ?? "http://localhost:8000";

// The cookie that the API sets at sign-in (SESSION_COOKIE in apps/api/src/unshelve/api/deps.py).
const SESSION_COOKIE = "unshelve_session";

/** For calls that need no sign-in, such as the readiness probe. */
export const serverApi = createApiClient(API_INTERNAL_URL);

/**
 * One `name=value` pair of a Cookie header, exactly as the browser sent it.
 *
 * The value is not decoded and encoded again, because that can change it. Only this one cookie
 * is returned, so the visitor's other cookies never leave the web app.
 */
function pickCookie(cookieHeader: string | null, name: string): string | undefined {
  return (cookieHeader ?? "")
    .split(";")
    .map((pair) => pair.trim())
    .find((pair) => pair.startsWith(`${name}=`));
}

/**
 * A client that acts as the visitor: it sends the session cookie of the incoming request.
 *
 * It reads the request headers, so a server component calls it inside <Suspense>. The answers
 * belong to one person, so nothing the client fetches is cached.
 *
 * It also waits for the real request (`connection()`). `next dev` renders a page in stages, to
 * check what a prerender would see, and it holds a `no-store` fetch back until the last stage.
 * But Next.js 16.4 looks for that option only in the second argument of `fetch()`, and
 * openapi-fetch puts it on a `Request`. So the call ran too early, Node's fetch read the clock
 * (`Date.now()`) when the body ended, and Next.js logged an error for each page with this client.
 * `connection()` also throws inside "use cache" (the private kind too), so these answers cannot
 * go into a cache by mistake.
 */
export async function sessionApi() {
  await connection();
  const cookie = pickCookie((await headers()).get("cookie"), SESSION_COOKIE);
  return createApiClient(API_INTERNAL_URL, {
    cache: "no-store",
    headers: cookie ? { cookie } : {},
  });
}
