import AddRounded from "@mui/icons-material/AddRounded";
import NavigateBeforeRounded from "@mui/icons-material/NavigateBeforeRounded";
import NavigateNextRounded from "@mui/icons-material/NavigateNextRounded";
import RefreshRounded from "@mui/icons-material/RefreshRounded";
import SearchRounded from "@mui/icons-material/SearchRounded";
import {
  Alert,
  Box,
  Button,
  Card,
  CircularProgress,
  FormControl,
  InputAdornment,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from "@mui/material";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { executeRequest } from "../lib/runtime";
import { getPointer, interpolate } from "../lib/pointer";
import type { ListPageSpec, ModuleManifest, PageManifest } from "../types";
import { FieldValue } from "./FieldValue";
import { PageFrame } from "./PageFrame";

export function listNeedsPolling(items: unknown[], polling?: ListPageSpec["polling"]): boolean {
  if (!polling) return false;
  if (!polling.whilePointer || !polling.whileValues) return true;
  return items.some((item) => polling.whileValues!.includes(String(getPointer(item, polling.whilePointer!))));
}

export function ListPage({ module, page }: { module: ModuleManifest; page: PageManifest }) {
  const spec = page.spec as ListPageSpec;
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [items, setItems] = useState<unknown[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const timer = useRef<number | undefined>(undefined);
  const pageNumber = Math.max(1, Number(params.get("page") || 1));
  const searchValue = params.get(spec.search?.name || "q") || "";
  const state = useMemo(() => {
    const values: Record<string, unknown> = { page: pageNumber, pageSize: spec.pagination?.pageSize || 30 };
    spec.filters?.forEach((filter) => { values[filter.name] = params.get(filter.name) ?? filter.default ?? ""; });
    if (spec.search) values[spec.search.name] = searchValue;
    return values;
  }, [pageNumber, params, searchValue, spec]);

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    setError(undefined);
    try {
      const response = await executeRequest(spec.request, module.spec.dataSources, { state });
      const selected = getPointer(response, spec.response.itemsPointer);
      setItems(Array.isArray(selected) ? selected : []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load builds");
    } finally {
      setLoading(false);
    }
  }, [module.spec.dataSources, spec, state]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    window.clearInterval(timer.current);
    const polling = spec.polling;
    if (!polling || !listNeedsPolling(items, polling) || document.hidden) return;
    timer.current = window.setInterval(() => {
      if (!document.hidden) void load(true);
    }, polling.intervalMs);
    return () => window.clearInterval(timer.current);
  }, [items, load, spec.polling]);

  const updateParam = (name: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(name, value); else next.delete(name);
    if (name !== "page") next.set("page", "1");
    setParams(next);
  };

  const itemPath = (item: unknown) => interpolate(spec.itemRoute, { id: getPointer(item, spec.itemIDPointer) });
  return (
    <PageFrame
      eyebrow={module.metadata.title}
      title={page.metadata.title}
      description={page.metadata.description}
      action={spec.primaryAction && <Button component={Link} to={spec.primaryAction.navigate} variant="contained" startIcon={<AddRounded />}>{spec.primaryAction.label}</Button>}
    >
      <Card variant="outlined" sx={{ mb: 2, p: 2 }}>
        <Stack direction={{ xs: "column", md: "row" }} sx={{ gap: 1.5, alignItems: { md: "center" } }}>
          {spec.search && (
            <TextField
              size="small"
              value={searchValue}
              onChange={(event) => updateParam(spec.search!.name, event.target.value)}
              placeholder={spec.search.placeholder}
              sx={{ minWidth: { md: 320 } }}
              slotProps={{
                htmlInput: { "aria-label": spec.search.label },
                input: { startAdornment: <InputAdornment position="start"><SearchRounded fontSize="small" /></InputAdornment> },
              }}
            />
          )}
          {spec.filters?.map((filter) => (
            <FormControl size="small" key={filter.name} sx={{ minWidth: 150 }}>
              <InputLabel>{filter.label}</InputLabel>
              <Select label={filter.label} value={String(state[filter.name] ?? "")} onChange={(event) => updateParam(filter.name, String(event.target.value))}>
                {filter.options.map((option) => <MenuItem key={option.value} value={option.value}>{option.label}</MenuItem>)}
              </Select>
            </FormControl>
          ))}
          <Button sx={{ ml: { md: "auto" } }} color="inherit" startIcon={<RefreshRounded />} onClick={() => load()}>Refresh</Button>
        </Stack>
      </Card>
      {error && <Alert severity="error" action={<Button color="inherit" onClick={() => load()}>Retry</Button>}>{error}</Alert>}
      {loading && !items.length ? <Box className="center-state"><CircularProgress size={28} /><Typography>Loading builds…</Typography></Box> : (
        <>
          <TableContainer component={Card} variant="outlined" className="desktop-table">
            <Table>
              <TableHead><TableRow>{spec.columns.map((field) => <TableCell key={field.pointer}>{field.label}</TableCell>)}</TableRow></TableHead>
              <TableBody>
                {items.map((item, index) => (
                  <TableRow hover key={String(getPointer(item, spec.itemIDPointer) ?? index)} tabIndex={0} onClick={() => navigate(itemPath(item))} onKeyDown={(event) => event.key === "Enter" && navigate(itemPath(item))} sx={{ cursor: "pointer" }}>
                    {spec.columns.map((field) => <TableCell key={field.pointer}><FieldValue field={field} data={item} compact /></TableCell>)}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
          <Stack className="mobile-cards" sx={{ gap: 1.25 }}>
            {items.map((item, index) => (
              <Card variant="outlined" key={String(getPointer(item, spec.itemIDPointer) ?? index)} onClick={() => navigate(itemPath(item))} sx={{ p: 2 }}>
                {spec.columns.map((field) => <Box key={field.pointer} sx={{ display: "flex", justifyContent: "space-between", gap: 2, py: 0.55 }}><Typography variant="caption" color="text.secondary">{field.label}</Typography><FieldValue field={field} data={item} compact /></Box>)}
              </Card>
            ))}
          </Stack>
          {!items.length && !error && <Box className="center-state"><Typography variant="h6">{spec.empty?.title || "No results"}</Typography><Typography color="text.secondary">{spec.empty?.description}</Typography></Box>}
          <Stack direction="row" sx={{ justifyContent: "flex-end", alignItems: "center", gap: 1, mt: 2 }}>
            <Button disabled={pageNumber <= 1} startIcon={<NavigateBeforeRounded />} onClick={() => updateParam("page", String(pageNumber - 1))}>Previous</Button>
            <Typography variant="body2" color="text.secondary">Page {pageNumber}</Typography>
            <Button disabled={items.length < (spec.pagination?.pageSize || 30)} endIcon={<NavigateNextRounded />} onClick={() => updateParam("page", String(pageNumber + 1))}>Next</Button>
          </Stack>
        </>
      )}
    </PageFrame>
  );
}
