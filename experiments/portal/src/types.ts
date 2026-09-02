import type { RJSFSchema, UiSchema } from "@rjsf/utils";

export const MANIFEST_API_VERSION = "ee.pingcap.net/v1alpha1";
export const RUNTIME_VERSION = "0.1.0";

export type PageType = "list" | "form" | "detail";
export type FormatName = "text" | "code" | "date-time" | "duration" | "status" | "link" | "copy";

export interface ObjectMeta {
  name: string;
  title: string;
  description?: string;
  module?: string;
  icon?: string;
  tags?: string[];
}

export interface DataSource {
  name: string;
  basePath: string;
}

export interface NavigationItem {
  label: string;
  page: string;
  icon?: string;
}

export interface ModuleManifest {
  apiVersion: typeof MANIFEST_API_VERSION;
  kind: "Module";
  metadata: ObjectMeta;
  spec: {
    dataSources: DataSource[];
    navigation: NavigationItem[];
    pages: PageManifest[];
  };
}

export interface ValueExpression {
  from?: "form" | "route" | "query" | "state" | "response";
  pointer?: string;
  const?: unknown;
  default?: unknown;
  format?: FormatName;
}

export interface RequestDefinition {
  dataSource: string;
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string;
  query?: Record<string, ValueExpression>;
  body?: unknown;
  response?: ResponseMapping;
}

export interface ResponseMapping {
  pointer?: string;
  collection?: boolean;
  aliases?: Record<string, string>;
  defaults?: Record<string, unknown>;
  statusMaps?: Record<string, Record<string, unknown>>;
}

export interface DisplayField {
  label: string;
  pointer: string;
  format?: FormatName;
  statusMap?: Record<string, "neutral" | "info" | "success" | "warning" | "danger">;
  hideWhenEmpty?: boolean;
  visibleWhen?: { pointer: string; equals: unknown };
}

export interface FilterDefinition {
  name: string;
  label: string;
  type: "select" | "scope";
  default?: string;
  options: Array<{ label: string; value: string }>;
}

export interface PollingDefinition {
  intervalMs: number;
  maxIntervalMs?: number;
  whilePointer?: string;
  whileValues?: string[];
}

export interface ListPageSpec {
  type: "list";
  route: string;
  request: RequestDefinition;
  response: { itemsPointer: string };
  itemRoute: string;
  itemIDPointer: string;
  columns: DisplayField[];
  filters?: FilterDefinition[];
  search?: { name: string; label: string; placeholder?: string };
  pagination?: { pageSize: number };
  polling?: PollingDefinition;
  empty?: { title: string; description: string };
  primaryAction?: { label: string; navigate: string };
}

export interface DynamicOptionDefinition {
  field: string;
  request: RequestDefinition;
  itemsPointer: string;
  valuePointer: string;
  labelPointer: string;
}

export interface FormPageSpec {
  type: "form";
  route: string;
  schema: RJSFSchema;
  uiSchema?: UiSchema;
  dynamicOptions?: DynamicOptionDefinition[];
  submit: {
    label: string;
    request: RequestDefinition;
    successNavigate: string;
    resultIDPointer: string;
  };
}

export interface DetailSection {
  title: string;
  fields?: DisplayField[];
  collectionPointer?: string;
  collectionFields?: DisplayField[];
  emptyText?: string;
}

export interface ActionDefinition {
  name: string;
  label: string;
  type: "request" | "navigate" | "refresh" | "copy" | "open-link" | "notify";
  request?: RequestDefinition;
  navigate?: string;
  pointer?: string;
  confirm?: { title: string; description: string };
  visibleWhen?: { pointer: string; equals: unknown };
  successNavigate?: string;
  resultIDPointer?: string;
  message?: string;
}

export interface DetailPageSpec {
  type: "detail";
  route: string;
  request: RequestDefinition;
  header: { titleTemplate: string; statusPointer?: string; subtitleFields?: DisplayField[] };
  sections: DetailSection[];
  actions?: ActionDefinition[];
  polling?: PollingDefinition;
}

export interface PageManifest {
  apiVersion: typeof MANIFEST_API_VERSION;
  kind: "Page";
  metadata: ObjectMeta & { module: string };
  spec: ListPageSpec | FormPageSpec | DetailPageSpec;
}

export interface PortalRegistry {
  apiVersion: typeof MANIFEST_API_VERSION;
  kind: "Registry";
  compilerVersion: string;
  minimumRuntimeVersion: string;
  sourceDigest: string;
  modules: ModuleManifest[];
}

export interface RuntimeContext {
  form?: unknown;
  route?: Record<string, string>;
  query?: Record<string, string>;
  state?: Record<string, unknown>;
  response?: unknown;
}
