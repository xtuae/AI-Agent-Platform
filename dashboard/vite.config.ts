import { fileURLToPath, URL } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The API is same-origin in production (Caddy routes /api/* to FastAPI). In development Vite
// proxies it, so the refresh cookie (Path=/api/v1/auth) behaves exactly as it will in production.
const api = process.env.API_ORIGIN ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: { port: 5173, proxy: { "/api": { target: api, changeOrigin: false }, "/q/": { target: api, changeOrigin: false } } },
  preview: { port: 4173, proxy: { "/api": { target: api, changeOrigin: false }, "/q/": { target: api, changeOrigin: false } } },
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false, target: "es2022" },
  test: { environment: "jsdom", include: ["src/**/*.test.ts"] },
});
