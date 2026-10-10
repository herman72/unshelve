import path from "node:path";
import type { NextConfig } from "next";

// The browser only talks to this app; /api is proxied to the Python API. That keeps the
// session cookie same-origin and needs no CORS. The value is baked in at build time.
const apiUrl = process.env.UNSHELVE_API_INTERNAL_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  output: "standalone",
  // Scripts run with apps/web as the working directory; trace from the monorepo root.
  outputFileTracingRoot: path.join(process.cwd(), "../.."),
  transpilePackages: ["@unshelve/api-client"],
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiUrl}/api/:path*` }];
  },
};

export default nextConfig;
