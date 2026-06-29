import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Tauri serves the built assets; dev server on a fixed port for the shell.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
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
