import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Tauri serves the built assets; dev server on a fixed port for the shell.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: { port: 5173, strictPort: true },
  build: { outDir: "dist", target: "es2022", sourcemap: true },
});
