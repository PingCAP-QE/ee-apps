import type {
  DataSource,
  FormatName,
  RequestDefinition,
  ResponseMapping,
  RuntimeContext,
  ValueExpression,
} from "../types";
import { getPointer, interpolate } from "./pointer";

export class PortalRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly details?: unknown,
  ) {
    super(message);
  }
}

function mapResponseItem(value: unknown, mapping: ResponseMapping): unknown {
  if (!mapping.aliases && !mapping.defaults && !mapping.statusMaps) return value;
  const source = value && typeof value === "object" ? value : {};
  const output: Record<string, unknown> = mapping.aliases
    ? Object.fromEntries(Object.entries(mapping.aliases).map(([alias, pointer]) => [alias, getPointer(source, pointer)]))
    : { ...(source as Record<string, unknown>) };
  for (const [key, fallback] of Object.entries(mapping.defaults || {})) {
    if (output[key] === undefined || output[key] === null || output[key] === "") output[key] = fallback;
  }
  for (const [key, values] of Object.entries(mapping.statusMaps || {})) {
    const current = output[key];
    if (Object.hasOwn(values, String(current))) output[key] = values[String(current)];
  }
  return output;
}

export function mapResponse(value: unknown, mapping?: ResponseMapping): unknown {
  if (!mapping) return value;
  const selected = mapping.pointer ? getPointer(value, mapping.pointer) : value;
  if (mapping.collection) return Array.isArray(selected) ? selected.map((item) => mapResponseItem(item, mapping)) : [];
  return mapResponseItem(selected, mapping);
}

export function evaluate(expression: ValueExpression, context: RuntimeContext): unknown {
  if (Object.hasOwn(expression, "const")) return expression.const;
  const source = expression.from ? context[expression.from] : undefined;
  const selected = getPointer(source, expression.pointer || "");
  return selected ?? expression.default;
}

function mapValue(value: unknown, context: RuntimeContext): unknown {
  if (Array.isArray(value)) return value.map((entry) => mapValue(entry, context));
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    if ("from" in record || "const" in record) return evaluate(record as ValueExpression, context);
    return Object.fromEntries(Object.entries(record).map(([key, entry]) => [key, mapValue(entry, context)]));
  }
  return value;
}

export async function executeRequest(
  request: RequestDefinition,
  dataSources: DataSource[],
  context: RuntimeContext,
  signal?: AbortSignal,
): Promise<unknown> {
  const source = dataSources.find((candidate) => candidate.name === request.dataSource);
  if (!source) throw new Error(`Unknown data source: ${request.dataSource}`);
  const routeValues = context.route || {};
  const path = interpolate(request.path, routeValues);
  const base = new URL(source.apiUrl, window.location.origin);
  const joinedPath = `${base.pathname.replace(/\/$/, "")}/${path.replace(/^\//, "")}`;
  base.pathname = joinedPath;
  const url = base;

  for (const [name, expression] of Object.entries(request.query || {})) {
    const value = evaluate(expression, context);
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(name, String(value));
  }

  const response = await fetch(url, {
    method: request.method,
    credentials: "include",
    headers: request.body === undefined ? undefined : { "Content-Type": "application/json" },
    body: request.body === undefined ? undefined : JSON.stringify(mapValue(request.body, context)),
    signal,
  });
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message =
      typeof body === "object" && body && "message" in body
        ? String((body as Record<string, unknown>).message)
        : `Request failed (${response.status})`;
    throw new PortalRequestError(message, response.status, body);
  }
  return mapResponse(body, request.response);
}

export function formatValue(value: unknown, format: FormatName = "text"): string {
  if (value === undefined || value === null || value === "") return "—";
  if (format === "date-time") {
    const date = new Date(String(value));
    return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(date);
  }
  if (format === "duration") {
    const seconds = Number(value);
    if (!Number.isFinite(seconds)) return String(value);
    const minutes = Math.floor(seconds / 60);
    return minutes ? `${minutes}m ${Math.round(seconds % 60)}s` : `${Math.round(seconds)}s`;
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function actionVisible(data: unknown, condition?: { pointer: string; equals: unknown }): boolean {
  return !condition || getPointer(data, condition.pointer) === condition.equals;
}
