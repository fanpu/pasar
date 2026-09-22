import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  // Both projects share one throwaway pasard (the webServer above), so running them concurrently
  // lets their fixture jobs (both named "alpha"/"beta") collide on the same dashboard. Serialise
  // instead of adding per-project isolation the fixtures don't otherwise need.
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:18750",
    launchOptions: { executablePath: process.env.PASAR_CHROME ?? "/usr/bin/google-chrome" },
  },
  // Without gracefulShutdown, Playwright SIGKILLs the whole process group at teardown, which
  // never gives serve.sh's own EXIT/TERM trap a chance to run and clean up its tmp dir. SIGTERM
  // first lets the trap do that; the 5s timeout is a backstop in case pasard hangs on shutdown.
  webServer: {
    command: "bash e2e/serve.sh",
    url: "http://127.0.0.1:18750/api/status",
    reuseExistingServer: false,
    timeout: 60_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 },
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1280, height: 900 } } },
    { name: "phone", use: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ],
});
