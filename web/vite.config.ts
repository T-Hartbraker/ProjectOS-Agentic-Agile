import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/health": "http://127.0.0.1:8787",
      "/v1": "http://127.0.0.1:8787",
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
    proxy: {
      "/health": "http://127.0.0.1:8787",
      "/v1": "http://127.0.0.1:8787",
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts"],
  },
});
