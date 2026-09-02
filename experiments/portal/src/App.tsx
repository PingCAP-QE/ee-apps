import { Alert, Box, Button, CircularProgress, Typography } from "@mui/material";
import { lazy, Suspense, useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { CatalogPage } from "./components/CatalogPage";
import { ModuleBoundary } from "./components/ModuleBoundary";
import { Shell } from "./components/Shell";
import { loadRegistry } from "./lib/registry";
import type { ModuleManifest, PageManifest, PortalRegistry } from "./types";

const DetailPage = lazy(() => import("./components/DetailPage").then((module) => ({ default: module.DetailPage })));
const FormPage = lazy(() => import("./components/FormPage").then((module) => ({ default: module.FormPage })));
const ListPage = lazy(() => import("./components/ListPage").then((module) => ({ default: module.ListPage })));

function ManifestPage({ module, page }: { module: ModuleManifest; page: PageManifest }) {
  if (page.type === "list") return <Suspense fallback={<Box className="full-center"><CircularProgress size={28} /></Box>}><ListPage module={module} page={page} /></Suspense>;
  if (page.type === "form") return <Suspense fallback={<Box className="full-center"><CircularProgress size={28} /></Box>}><FormPage module={module} page={page} /></Suspense>;
  if (page.type === "detail") return <Suspense fallback={<Box className="full-center"><CircularProgress size={28} /></Box>}><DetailPage module={module} page={page} /></Suspense>;
  throw new Error(`Unsupported page type: ${(page as { type: string }).type}`);
}

export default function App() {
  const [registry, setRegistry] = useState<PortalRegistry>();
  const [error, setError] = useState<string>();
  const reload = () => {
    setError(undefined);
    void loadRegistry().then(setRegistry).catch((err: unknown) => setError(err instanceof Error ? err.message : "Unable to load portal"));
  };
  useEffect(reload, []);

  if (error) return <Box className="full-center"><Alert severity="error" action={<Button color="inherit" onClick={reload}>Retry</Button>}>{error}</Alert></Box>;
  if (!registry) return <Box className="full-center"><CircularProgress size={30} /><Typography>Loading EE Portal…</Typography></Box>;

  return (
    <BrowserRouter basename={import.meta.env.BASE_URL.replace(/\/$/, "")}>
      <Shell modules={registry.modules}>
        <Routes>
          <Route path="/" element={<CatalogPage modules={registry.modules} />} />
          {registry.modules.flatMap((module) => module.pages.map((page) => (
            <Route key={`${module.id}-${page.id}`} path={page.route} element={<ModuleBoundary module={module.id}><ManifestPage module={module} page={page} /></ModuleBoundary>} />
          )))}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Shell>
    </BrowserRouter>
  );
}
