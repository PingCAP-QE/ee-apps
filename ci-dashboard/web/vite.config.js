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

function buildProxy(targetPrefix, rewritePrefix, target) {
  return {
    target,
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
  const apiTarget =
    env.CI_DASHBOARD_DEV_API_TARGET ||
    process.env.CI_DASHBOARD_DEV_API_TARGET ||
    "http://127.0.0.1:8000";
  const proxy = {
    "/api": apiTarget,
    "/healthz": apiTarget,
    "/livez": apiTarget,
    "/readyz": apiTarget,
  };

  if (apiPrefix) {
    proxy[`${apiPrefix}/api`] = buildProxy(`${apiPrefix}/api`, "/api", apiTarget);
    proxy[`${apiPrefix}/healthz`] = buildProxy(`${apiPrefix}/healthz`, "/healthz", apiTarget);
    proxy[`${apiPrefix}/livez`] = buildProxy(`${apiPrefix}/livez`, "/livez", apiTarget);
    proxy[`${apiPrefix}/readyz`] = buildProxy(`${apiPrefix}/readyz`, "/readyz", apiTarget);
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
