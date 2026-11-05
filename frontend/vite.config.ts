import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/** 后端只监听 IPv4 `127.0.0.1:8000`。
 *
 * 这里显式写 `127.0.0.1` 而不是 `localhost`：Node 解析 `localhost` 时可能优先返回
 * IPv6 `::1`，而后端没有监听 `::1`，会导致代理连接失败（接口表现为 500/超时）。
 */
const API_PROXY = {
  "/api": {
    target: "http://127.0.0.1:8000",
    changeOrigin: true,
  },
} as const;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 同时监听 IPv4/IPv6 与局域网地址，避免只绑定 ::1 时 127.0.0.1 打不开
    host: true,
    port: 5173,
    proxy: API_PROXY,
  },
  // `vite preview` 提供的是构建产物：同样需要转发 /api，
  // 否则登录等接口会 404（表现为"请求失败（HTTP 404）"）。
  preview: {
    host: true,
    port: 4173,
    proxy: API_PROXY,
  },
  build: {
    outDir: "dist",
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
