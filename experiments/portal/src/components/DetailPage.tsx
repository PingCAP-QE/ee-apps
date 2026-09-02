import RefreshRounded from "@mui/icons-material/RefreshRounded";
import ReplayRounded from "@mui/icons-material/ReplayRounded";
import {
  Alert,
  Box,
  Button,
  Card,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  Divider,
  Snackbar,
  Stack,
  Typography,
} from "@mui/material";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { actionVisible, executeRequest } from "../lib/runtime";
import { getPointer, interpolate } from "../lib/pointer";
import type { ActionDefinition, DetailPageSpec, ModuleManifest, PageManifest } from "../types";
import { FieldValue } from "./FieldValue";
import { PageFrame } from "./PageFrame";

export function shouldPoll(data: unknown, spec: DetailPageSpec): boolean {
  if (!spec.polling) return false;
  if (!spec.polling.whilePointer || !spec.polling.whileValues) return true;
  return spec.polling.whileValues.includes(String(getPointer(data, spec.polling.whilePointer)));
}

export function nextPollDelay(base: number, maximum: number, failures: number): number {
  return Math.min(maximum, base * Math.max(1, 2 ** failures));
}

export function DetailPage({ module, page }: { module: ModuleManifest; page: PageManifest }) {
  const spec = page.spec as DetailPageSpec;
  const params = useParams();
  const route = useMemo(() => ({ id: params.id || "" }), [params.id]);
  const navigate = useNavigate();
  const [data, setData] = useState<unknown>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const [pendingAction, setPendingAction] = useState<ActionDefinition>();
  const [runningAction, setRunningAction] = useState(false);
  const [pageVisible, setPageVisible] = useState(() => !document.hidden);
  const [pollRevision, setPollRevision] = useState(0);
  const [notification, setNotification] = useState<string>();
  const timer = useRef<number | undefined>(undefined);
  const failureCount = useRef(0);

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const response = await executeRequest(spec.request, module.spec.dataSources, { route });
      setData(response);
      setError(undefined);
      failureCount.current = 0;
    } catch (err) {
      failureCount.current += 1;
      setError(err instanceof Error ? err.message : "Unable to load build");
    } finally {
      setLoading(false);
      if (silent) setPollRevision((value) => value + 1);
    }
  }, [module.spec.dataSources, route, spec.request]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const handleVisibility = () => setPageVisible(!document.hidden);
    document.addEventListener("visibilitychange", handleVisibility);
    return () => document.removeEventListener("visibilitychange", handleVisibility);
  }, []);
  useEffect(() => {
    window.clearTimeout(timer.current);
    if (!data || !shouldPoll(data, spec) || !pageVisible) return;
    const base = spec.polling?.intervalMs || 5000;
    const delay = nextPollDelay(base, spec.polling?.maxIntervalMs || 30000, failureCount.current);
    timer.current = window.setTimeout(() => {
      if (!document.hidden) void load(true);
    }, delay);
    return () => window.clearTimeout(timer.current);
  }, [data, load, pageVisible, pollRevision, spec]);

  const runAction = async (action: ActionDefinition) => {
    if (action.confirm && pendingAction?.name !== action.name) { setPendingAction(action); return; }
    if (action.type === "refresh") { await load(); return; }
    if (action.type === "navigate" && action.navigate) { navigate(interpolate(action.navigate, route)); return; }
    if (action.type === "copy" && action.pointer) { await navigator.clipboard.writeText(String(getPointer(data, action.pointer) ?? "")); return; }
    if (action.type === "open-link" && action.pointer) {
      const url = String(getPointer(data, action.pointer) ?? "");
      if (/^https?:\/\//.test(url)) window.open(url, "_blank", "noopener,noreferrer");
      return;
    }
    if (action.type === "notify") { setNotification(action.message || action.label); return; }
    if (action.type === "request" && action.request) {
      setRunningAction(true);
      try {
        const result = await executeRequest(action.request, module.spec.dataSources, { route, response: data });
        if (action.successNavigate) {
          const id = getPointer(result, action.resultIDPointer || "/id");
          navigate(interpolate(action.successNavigate, { ...route, id }));
        } else await load();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Action failed");
      } finally {
        setRunningAction(false);
        setPendingAction(undefined);
      }
    }
  };

  if (loading && !data) return <Box className="full-center"><CircularProgress size={30} /><Typography>Loading build…</Typography></Box>;
  if (!data) return <PageFrame back="/tibuild/builds" title="Build unavailable"><Alert severity="error" action={<Button onClick={() => load()}>Retry</Button>}>{error}</Alert></PageFrame>;
  const title = spec.header.titleTemplate.replace(/\{([^}]+)\}/g, (_, pointer: string) => String(getPointer(data, pointer) ?? ""));

  return (
    <PageFrame
      back="/tibuild/builds"
      eyebrow={module.metadata.title}
      title={title}
      description={spec.header.subtitleFields?.map((field) => `${field.label}: ${String(getPointer(data, field.pointer) ?? "—")}`).join(" · ")}
      action={<Stack direction="row" sx={{ gap: 1 }}>{(spec.actions || []).filter((action) => actionVisible(data, action.visibleWhen)).map((action) => <Button key={action.name} variant={action.type === "request" ? "contained" : "outlined"} startIcon={action.type === "request" ? <ReplayRounded /> : <RefreshRounded />} disabled={runningAction} onClick={() => runAction(action)}>{action.label}</Button>)}</Stack>}
    >
      {error && <Alert severity="error" sx={{ mb: 2 }} action={<Button color="inherit" onClick={() => load()}>Retry</Button>}>{error}</Alert>}
      {spec.header.statusPointer && <Chip className="detail-status" label={String(getPointer(data, spec.header.statusPointer) || "UNKNOWN")} />}
      <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", xl: "repeat(2, minmax(0, 1fr))" }, gap: 2, mt: 2 }}>
        {spec.sections.map((section) => {
          const collection = section.collectionPointer ? getPointer(data, section.collectionPointer) : undefined;
          return (
            <Card variant="outlined" key={section.title} sx={{ p: { xs: 2, sm: 2.5 } }}>
              <Typography variant="h6">{section.title}</Typography>
              <Divider sx={{ my: 1.5 }} />
              {section.fields && <Box className="detail-grid">{section.fields.map((field) => <Box key={field.pointer}><Typography variant="caption" color="text.secondary">{field.label}</Typography><Box sx={{ mt: 0.35 }}><FieldValue field={field} data={data} /></Box></Box>)}</Box>}
              {Array.isArray(collection) && collection.length > 0 && <Stack sx={{ gap: 1.25 }}>{collection.map((item, index) => <Box className="collection-row" key={index}>{section.collectionFields?.map((field) => <Box key={field.pointer} sx={{ minWidth: 0 }}><Typography variant="caption" color="text.secondary">{field.label}</Typography><FieldValue field={field} data={item} compact /></Box>)}</Box>)}</Stack>}
              {section.collectionPointer && (!Array.isArray(collection) || !collection.length) && <Typography color="text.secondary">{section.emptyText || "Nothing to show yet."}</Typography>}
            </Card>
          );
        })}
      </Box>
      <Dialog open={Boolean(pendingAction)} onClose={() => setPendingAction(undefined)}>
        <DialogTitle>{pendingAction?.confirm?.title}</DialogTitle>
        <DialogContent><DialogContentText>{pendingAction?.confirm?.description}</DialogContentText></DialogContent>
        <DialogActions><Button onClick={() => setPendingAction(undefined)}>Cancel</Button><Button variant="contained" onClick={() => pendingAction && runAction(pendingAction)}>Confirm</Button></DialogActions>
      </Dialog>
      <Snackbar open={Boolean(notification)} autoHideDuration={4000} onClose={() => setNotification(undefined)} message={notification} />
    </PageFrame>
  );
}
