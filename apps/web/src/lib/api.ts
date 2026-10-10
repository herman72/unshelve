import { createApiClient } from "@unshelve/api-client";

// Server components call the API directly on the internal network.
// Browser code uses relative /api paths, which next.config.ts proxies.
const API_INTERNAL_URL = process.env.UNSHELVE_API_INTERNAL_URL ?? "http://localhost:8000";

export const serverApi = createApiClient(API_INTERNAL_URL);
