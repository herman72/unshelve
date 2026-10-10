import { createBrowserApiClient } from "@unshelve/api-client";

// For client components. The URLs are relative, so the browser sends them to this app, and
// next.config.ts proxies /api to the Python API. The session cookie is HttpOnly and
// same-origin: the browser attaches it, and this code never sees it.
//
// This file stays apart from lib/api.ts on purpose. That file reads request headers with
// next/headers, which a client component must not import.
export const browserApi = createBrowserApiClient();
