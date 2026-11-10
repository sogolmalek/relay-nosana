// Replay journey (free): the app against committed real runs, verified live against Solana and IPFS.
//   cd tests/e2e_journey && npx playwright test            # local static server on :8766
//   APP_URL=https://sogolmalek.github.io/relay-nosana/app/ npx playwright test   # the deployed page
const { defineConfig } = require("@playwright/test");

const local = !process.env.APP_URL;
module.exports = defineConfig({
  testDir: ".",
  timeout: 600_000,
  expect: { timeout: 180_000 },
  workers: 1,
  reporter: [["list"], ["json", { outputFile: "../../out/e2e/replay/results.json" }]],
  outputDir: "../../out/e2e/replay/artifacts",
  use: {
    baseURL: process.env.APP_URL || "http://127.0.0.1:8766/app/",
    video: "on", screenshot: "on", trace: "retain-on-failure",
  },
  webServer: local ? {
    command: "../../.venv/bin/python -m http.server 8766 --bind 127.0.0.1 --directory ../../docs",
    url: "http://127.0.0.1:8766/app/", reuseExistingServer: true,
  } : undefined,
});
