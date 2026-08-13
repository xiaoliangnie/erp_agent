import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 源码在 frontend/，产物落 frontend/dist/，由 backend/app.py 直接托管。
// 开发时 /api 代理到 server.py，避免跨域也避免另配一套 CORS。
export default defineConfig({
  root: "frontend",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    port: 5177,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8777",
        changeOrigin: true,
      },
    },
  },
});
