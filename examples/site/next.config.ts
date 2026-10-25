import type { NextConfig } from "next";

// A site that searches the public feed of Unshelve, as mattsh.dev will. The package exports
// TypeScript source, so Next.js compiles it with the files of the site.
const nextConfig: NextConfig = {
  transpilePackages: ["@unshelve/site-search"],
};

export default nextConfig;
