import createClient from "openapi-fetch";
import type { paths } from "./schema";

export type { components, paths } from "./schema";

/**
 * A typed client for the Unshelve API.
 *
 * `schema.d.ts` is generated from apps/api/openapi.json (`make openapi`), so a call to a
 * path or field that the API does not have fails at compile time.
 */
export function createApiClient(baseUrl: string) {
  return createClient<paths>({ baseUrl });
}
