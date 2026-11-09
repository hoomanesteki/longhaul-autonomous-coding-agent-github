import { defineConfig, devices } from "@playwright/test";

// The one browser test of the console (LH-808). It runs against the real stack: the web server
// below is scripts/seed_demo.sh, which starts a worker with a scripted model, puts three tasks and
// two lesson candidates into their states, and only then starts the API that serves the built
// console. The URL it waits for is the health check of that API, so the test never starts on a
// half-seeded stack.
const PORT = process.env.LH_DEMO_PORT ?? "8765";
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "*.spec.ts",
  // The steps change the state of the stack, so they run in order, once. A retry would start
  // from a stack that already took the hint.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "bash ../scripts/seed_demo.sh",
    url: `${BASE_URL}/healthz`,
    // Three sandboxes set up their workspace from PyPI before the stack counts as ready.
    timeout: 600_000,
    reuseExistingServer: false,
    // The script removes its containers, volumes, blobs, database and workflows when it gets
    // SIGTERM. The default is SIGKILL, which would leave them behind.
    gracefulShutdown: { signal: "SIGTERM", timeout: 120_000 },
    stdout: "pipe",
    stderr: "pipe",
    env: { LH_DEMO_PORT: PORT },
  },
});
