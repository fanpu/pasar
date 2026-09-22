import { defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { svelteTesting } from "@testing-library/svelte/vite";

const api = process.env.PASAR_URL ?? "http://127.0.0.1:8750";

export default defineConfig({
  plugins: [svelte(), svelteTesting()],
  build: { outDir: "../src/pasar/webui", emptyOutDir: true },
  server: {
    // `npm run dev -- --host <addr>` to reach it over a private network; extra Host names
    // (e.g. a tailnet DNS name) come from PASAR_DEV_HOSTS=name1,name2
    allowedHosts: process.env.PASAR_DEV_HOSTS?.split(",").filter(Boolean) ?? [],
    proxy: { "/api": { target: api, changeOrigin: true }, "/mascot": { target: api, changeOrigin: true } },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/tests/setup.ts"],
    include: ["src/**/*.test.ts"],
    globals: true,
  },
});
