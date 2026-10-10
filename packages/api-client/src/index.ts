import createClient, { type ClientOptions } from "openapi-fetch";
import type { paths } from "./schema";

export type { components, paths } from "./schema";

/** Client settings except the base URL, which each factory below sets. */
export type ApiClientOptions = Omit<ClientOptions, "baseUrl">;

/**
 * A typed client for the Unshelve API.
 *
 * `schema.d.ts` is generated from apps/api/openapi.json (`make openapi`), so a call to a
 * path or field that the API does not have fails at compile time.
 */
export function createApiClient(baseUrl: string, options: ApiClientOptions = {}) {
  return createClient<paths>({ ...options, baseUrl });
}

/**
 * A client for code that runs in the browser.
 *
 * Its URLs are relative (`/api/v1/...`), so a request goes to the web app, and the web app
 * proxies `/api` to the Python API. The session cookie therefore stays on one origin.
 */
export function createBrowserApiClient(options: ApiClientOptions = {}) {
  return createApiClient("", options);
}
