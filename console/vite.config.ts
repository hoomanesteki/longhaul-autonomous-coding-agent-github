import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// In development the page is served by Vite and the API by `longhaul api`. The proxy keeps both
// on one origin, so the client needs no CORS and no base URL. In production the API serves the
// built files itself (LONGHAUL_CONSOLE_DIR), which is the same single origin.
const API = process.env.LONGHAUL_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": API,
      "/healthz": API,
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
