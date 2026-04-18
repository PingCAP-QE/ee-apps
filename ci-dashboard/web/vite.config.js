import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

function normalizeBasePath(value) {
  if (!value || value === "/") {
    return "/";
  }
  return `/${value.trim().replace(/^\/+|\/+$/g, "")}/`;
}

function normalizePrefix(value) {
  if (!value || value === "/") {
    return "";
  }
  const trimmed = value.trim().replace(/^\/+|\/+$/g, "");
  return trimmed ? `/${trimmed}` : "";
}

function buildProxy(targetPrefix, rewritePrefix) {
  return {
    target: "http://127.0.0.1:8000",
    changeOrigin: true,
    rewrite: (path) => `${rewritePrefix}${path.slice(targetPrefix.length)}`,
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const basePath = normalizeBasePath(env.VITE_BASE_PATH || process.env.VITE_BASE_PATH || "/");
  const apiPrefix = normalizePrefix(
    env.VITE_API_BASE_URL || process.env.VITE_API_BASE_URL || basePath,
  );
  const proxy = {
    "/api": "http://127.0.0.1:8000",
    "/healthz": "http://127.0.0.1:8000",
    "/livez": "http://127.0.0.1:8000",
    "/readyz": "http://127.0.0.1:8000",
  };

  if (apiPrefix) {
    proxy[`${apiPrefix}/api`] = buildProxy(`${apiPrefix}/api`, "/api");
    proxy[`${apiPrefix}/healthz`] = buildProxy(`${apiPrefix}/healthz`, "/healthz");
    proxy[`${apiPrefix}/livez`] = buildProxy(`${apiPrefix}/livez`, "/livez");
    proxy[`${apiPrefix}/readyz`] = buildProxy(`${apiPrefix}/readyz`, "/readyz");
  }

  return {
    base: basePath,
    plugins: [react()],
    server: {
      host: "0.0.0.0",
      port: 5173,
      proxy,
    },
  };
});
