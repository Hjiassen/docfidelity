import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * 开发期的后端地址。
 *
 * 前端只认**相对路径**（``/api/v1/...``），由这里代理到后端——这样：
 * ① 不引入 CORS（后端不必为浏览器开后门）；
 * ② 生产部署时前端与 API 同源，前端代码里没有一处需要改。
 */
const BACKEND = process.env["DOCFIDELITY_BACKEND"] ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: true },
      "/health": { target: BACKEND, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
