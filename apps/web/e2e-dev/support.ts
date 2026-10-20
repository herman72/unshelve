import path from "node:path";

// Ports far from those of the stack (3000, 8000) and of the end-to-end tests (3900, 3901), so
// all three can run at the same time.
export const DEV_API_PORT = 3911;
export const DEV_WEB_PORT = 3910;
export const DEV_API_URL = `http://127.0.0.1:${DEV_API_PORT}`;

/** Everything that `next dev` prints in the terminal. The spec reads it at the end. */
export const DEV_LOG = path.join(__dirname, "..", "test-results", "next-dev.log");
