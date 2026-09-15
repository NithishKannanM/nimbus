import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dashboard is served separately from the API in development, so every
// /api call is proxied to the control plane. In production the built assets are
// served by the control plane itself and the prefix resolves without a proxy,
// which is why the client uses a relative base URL rather than an absolute one.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.NIMBUS_API ?? "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
