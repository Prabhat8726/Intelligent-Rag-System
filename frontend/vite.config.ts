import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// In development the SPA and API share an origin through this proxy (no CORS needed),
// mirroring the nginx reverse proxy used in the container image.
const apiTarget = process.env.VITE_DEV_API_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": apiTarget,
      "/health": apiTarget,
    },
  },
  build: {
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    restoreMocks: true,
    unstubGlobals: true,
  },
});
