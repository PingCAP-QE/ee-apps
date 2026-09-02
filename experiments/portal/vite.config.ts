import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/ee/",
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/ee/api": {
        target: process.env.PORTAL_API_PROXY || "http://127.0.0.1:8080",
        changeOrigin: true,
      },
    },
  },
});
