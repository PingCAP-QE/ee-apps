#!/usr/bin/env node
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { basename, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parse } from "yaml";

export const COMPILER_VERSION = "0.2.0";
export const REGISTRY_VERSION = 1;
const ALLOWED_PAGE_TYPES = new Set(["list", "form", "detail"]);
const ALLOWED_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE"]);
const ALLOWED_PLUGINS = new Set(["core.dynamic-select"]);
const ALLOWED_ACTIONS = new Set(["request", "navigate", "confirm", "notify", "copy", "open-link", "refresh"]);
const ALLOWED_VALUE_SOURCES = new Set(["form", "route", "query", "state", "response"]);
const JSON_SCHEMA_VERSION = "https://json-schema.org/draft/2020-12/schema";
type Document = Record<string, any>;
function stable(value: unknown): unknown { if (Array.isArray(value)) return value.map(stable); if (value && typeof value === "object") return Object.fromEntries(Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)).map(([key, entry]) => [key, stable(entry)])); return value; }
function stableJSON(value: unknown): string { return `${JSON.stringify(stable(value), null, 2)}\n`; }
function filesRecursively(directory: string): string[] { return readdirSync(directory).flatMap((name) => { const path = join(directory, name); return statSync(path).isDirectory() ? filesRecursively(path) : [path]; }).filter((path) => /\.ya?ml$/i.test(path)).sort(); }
function fail(file: string, message: string): never { throw new Error(`${file}: ${message}`); }
function inspectObject(value: unknown, file: string, dataSources: Set<string>): void {
  if (Array.isArray(value)) { value.forEach((entry) => inspectObject(entry, file, dataSources)); return; }
  if (!value || typeof value !== "object") return;
  const object = value as Record<string, unknown>;
  for (const forbidden of ["script", "javascript", "headers", "secret", "token"]) if (forbidden in object) fail(file, `forbidden key '${forbidden}'`);
  if (typeof object.plugin === "string" && !ALLOWED_PLUGINS.has(object.plugin)) fail(file, `unknown plugin '${object.plugin}'`);
  if (typeof object.from === "string" && !ALLOWED_VALUE_SOURCES.has(object.from)) fail(file, `unsupported value source '${object.from}'`);
  if (typeof object.dataSource === "string" || typeof object.method === "string") {
    if (!dataSources.has(String(object.dataSource))) fail(file, `unknown data source '${String(object.dataSource)}'`);
    if (!ALLOWED_METHODS.has(String(object.method))) fail(file, `unsupported HTTP method '${String(object.method)}'`);
    if (typeof object.path !== "string" || !object.path.startsWith("/") || object.path.startsWith("//") || /^https?:/i.test(object.path)) fail(file, "request path must be a relative path");
  }
  Object.values(object).forEach((entry) => inspectObject(entry, file, dataSources));
}
function requiredString(document: Document, key: string, file: string): string { if (typeof document[key] !== "string" || !document[key]) fail(file, `${key} is required`); return document[key]; }

export function compileRegistry(input: string): { registry: Document; json: string } {
  const moduleDirectories = readdirSync(input).map((name) => join(input, name)).filter((path) => statSync(path).isDirectory()).sort();
  const modules: Document[] = []; const sourceDocuments: Document[] = []; const routes = new Set<string>(); const moduleNames = new Set<string>();
  for (const directory of moduleDirectories) {
    const moduleFile = join(directory, "module.yaml"); if (!existsSync(moduleFile)) continue;
    let source = parse(readFileSync(moduleFile, "utf8")) as Document;
    if (source.metadata && source.spec) source = { id: source.metadata.name, title: source.metadata.title, description: source.metadata.description, icon: source.metadata.icon, tags: source.metadata.tags, ...source.spec, dataSources: (source.spec.dataSources || []).map((item: Document) => ({ name: item.name, apiUrl: item.apiUrl || item.basePath })) };
    const id = requiredString(source, "id", moduleFile); const title = requiredString(source, "title", moduleFile);
    if (moduleNames.has(id)) fail(moduleFile, `duplicate module '${id}'`); moduleNames.add(id);
    const dataSources = new Set<string>();
    for (const item of source.dataSources || []) { if (!item.name || typeof item.apiUrl !== "string" || !item.apiUrl) fail(moduleFile, "data sources require a name and apiUrl"); if (dataSources.has(item.name)) fail(moduleFile, `duplicate data source '${item.name}'`); dataSources.add(item.name); }
    const pageDirectory = join(directory, "pages");
    const pages = existsSync(pageDirectory) ? filesRecursively(pageDirectory).map((file) => {
      let page = parse(readFileSync(file, "utf8")) as Document;
      if (page.metadata && page.spec) page = { id: page.metadata.name, module: page.metadata.module, title: page.metadata.title, description: page.metadata.description, ...page.spec };
      const pageID = requiredString(page, "id", file); requiredString(page, "title", file);
      if (page.module !== id) fail(file, `module must be '${id}'`); if (!ALLOWED_PAGE_TYPES.has(page.type)) fail(file, `unsupported page type '${page.type}'`);
      if (page.schema && page.schema.$schema !== JSON_SCHEMA_VERSION) fail(file, `form schema must use ${JSON_SCHEMA_VERSION}`); if (page.type === "form" && !page.schema) fail(file, "form pages require a JSON Schema");
      for (const action of page.actions || []) if (!ALLOWED_ACTIONS.has(action.type)) fail(file, `unsupported action '${action.type}'`);
      for (const dynamic of page.dynamicOptions || []) if (!page.schema?.properties?.[dynamic.field]) fail(file, `dynamic option references unknown field '${dynamic.field}'`);
      if (typeof page.route !== "string" || !page.route.startsWith("/")) fail(file, "route must start with '/'"); if (routes.has(page.route)) fail(file, `duplicate route '${page.route}'`); routes.add(page.route);
      inspectObject(page, file, dataSources); sourceDocuments.push(page); return page;
    }) : [];
    const pageNames = new Set(pages.map((page) => page.id)); for (const navigation of source.navigation || []) if (!pageNames.has(navigation.page)) fail(moduleFile, `navigation references unknown page '${navigation.page}'`);
    sourceDocuments.push(source); modules.push({ id, title, description: source.description, icon: source.icon, tags: source.tags, dataSources: source.dataSources || [], navigation: source.navigation || [], pages });
  }
  if (!modules.length) throw new Error(`${input}: no module.yaml files found`);
  const sourceDigest = createHash("sha256").update(stableJSON(sourceDocuments)).digest("hex");
  const registry = { registryVersion: REGISTRY_VERSION, compilerVersion: COMPILER_VERSION, minimumRuntimeVersion: "0.1.0", sourceDigest, modules };
  return { registry, json: stableJSON(registry) };
}
function main(): void { const args = process.argv.slice(2); const check = args.includes("--check"); const value = (name: string, fallback: string) => { const index = args.indexOf(name); return resolve(index >= 0 ? args[index + 1] : fallback); }; const input = value("--input", "testdata/modules"); const output = value("--output", "public/config/registry.json"); const { json } = compileRegistry(input); if (check) { if (!existsSync(output) || readFileSync(output, "utf8") !== json) throw new Error(`${output} is stale; run config:compile`); process.stdout.write(`Registry is up to date (${basename(output)})\n`); return; } writeFileSync(output, json); process.stdout.write(`Compiled ${input} -> ${output}\n`); }
if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))) main();
