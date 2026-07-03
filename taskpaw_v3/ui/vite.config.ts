import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Single source of truth for the app version: package.json (kept in lockstep with
// tauri.conf.json by the release bump). Injected as __APP_VERSION__ so the UI never
// drifts from the packaged version again (#170 — the old hardcoded "3.0.0-dev" stuck
// while the build shipped 3.1.0). Imported (resolveJsonModule) rather than read via
// node:fs so vite.config.ts needs no @types/node.
import pkg from "./package.json";

// Tauri serves the built assets; dev server on a fixed port for the shell.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  define: { __APP_VERSION__: JSON.stringify(pkg.version) },
  server: { port: 5173, strictPort: true },
  build: { outDir: "dist", target: "es2022", sourcemap: true },
  // Frontend smoke tests (#45) — jsdom; i18n is initialized in the setup file.
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
