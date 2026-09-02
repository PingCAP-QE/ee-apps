#!/usr/bin/env node
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parse } from "yaml";

export const API_VERSION = "ee.pingcap.net/v1alpha1";
export const COMPILER_VERSION = "0.1.0";
const ALLOWED_PAGE_TYPES = new Set(["list", "form", "detail"]);
const ALLOWED_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE"]);
const ALLOWED_PLUGINS = new Set(["core.dynamic-select"]);
const ALLOWED_ACTIONS = new Set(["request", "navigate", "confirm", "notify", "copy", "open-link", "refresh"]);
const ALLOWED_VALUE_SOURCES = new Set(["form", "route", "query", "state", "response"]);
const JSON_SCHEMA_VERSION = "https://json-schema.org/draft/2020-12/schema";

type Document = Record<string, any>;

function stable(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)).map(([key, entry]) => [key, stable(entry)]));
  }
  return value;
}

function stableJSON(value: unknown): string {
  return `${JSON.stringify(stable(value), null, 2)}\n`;
}

function filesRecursively(directory: string): string[] {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    return statSync(path).isDirectory() ? filesRecursively(path) : [path];
  }).filter((path) => /\.ya?ml$/i.test(path)).sort();
}

function fail(file: string, message: string): never {
  throw new Error(`${file}: ${message}`);
}

function inspectObject(value: unknown, file: string, dataSources: Set<string>): void {
  if (Array.isArray(value)) { value.forEach((entry) => inspectObject(entry, file, dataSources)); return; }
  if (!value || typeof value !== "object") return;
  const object = value as Record<string, unknown>;
  for (const forbidden of ["script", "javascript", "headers", "secret", "token"]) {
    if (forbidden in object) fail(file, `forbidden key '${forbidden}'`);
  }
  if (typeof object.plugin === "string" && !ALLOWED_PLUGINS.has(object.plugin)) fail(file, `unknown plugin '${object.plugin}'`);
  if (typeof object.from === "string" && !ALLOWED_VALUE_SOURCES.has(object.from)) fail(file, `unsupported value source '${object.from}'`);
  if (typeof object.dataSource === "string" || typeof object.method === "string") {
    if (!dataSources.has(String(object.dataSource))) fail(file, `unknown data source '${String(object.dataSource)}'`);
    if (!ALLOWED_METHODS.has(String(object.method))) fail(file, `unsupported HTTP method '${String(object.method)}'`);
    if (typeof object.path !== "string" || !object.path.startsWith("/") || object.path.startsWith("//") || /^https?:/i.test(object.path)) fail(file, "request path must be a relative absolute-path");
  }
  Object.values(object).forEach((entry) => inspectObject(entry, file, dataSources));
}

function validateMetadata(document: Document, file: string): void {
  if (document.apiVersion !== API_VERSION) fail(file, `apiVersion must be ${API_VERSION}`);
  if (!document.metadata?.name || !document.metadata?.title) fail(file, "metadata.name and metadata.title are required");
}

export function compileRegistry(input: string): { registry: Document; json: string } {
  const moduleDirectories = readdirSync(input).map((name) => join(input, name)).filter((path) => statSync(path).isDirectory()).sort();
  const modules: Document[] = [];
  const sourceDocuments: Document[] = [];
  const routes = new Set<string>();
  const moduleNames = new Set<string>();

  for (const directory of moduleDirectories) {
    const moduleFile = join(directory, "module.yaml");
    if (!existsSync(moduleFile)) continue;
    const moduleDocument = parse(readFileSync(moduleFile, "utf8")) as Document;
    validateMetadata(moduleDocument, moduleFile);
    if (moduleDocument.kind !== "Module") fail(moduleFile, "kind must be Module");
    if (moduleNames.has(moduleDocument.metadata.name)) fail(moduleFile, `duplicate module '${moduleDocument.metadata.name}'`);
    moduleNames.add(moduleDocument.metadata.name);
    const dataSources = new Set<string>();
    for (const source of moduleDocument.spec?.dataSources || []) {
      if (!source.name || typeof source.basePath !== "string" || !source.basePath.startsWith("/") || source.basePath.startsWith("//") || /^https?:/i.test(source.basePath)) fail(moduleFile, "data sources require unique names and same-origin basePath values");
      if (dataSources.has(source.name)) fail(moduleFile, `duplicate data source '${source.name}'`);
      dataSources.add(source.name);
    }

    const pageDirectory = join(directory, "pages");
    const pages = existsSync(pageDirectory) ? filesRecursively(pageDirectory).map((file) => {
      const document = parse(readFileSync(file, "utf8")) as Document;
      validateMetadata(document, file);
      if (document.kind !== "Page") fail(file, "kind must be Page");
      if (document.metadata.module !== moduleDocument.metadata.name) fail(file, `metadata.module must be '${moduleDocument.metadata.name}'`);
      if (!ALLOWED_PAGE_TYPES.has(document.spec?.type)) fail(file, `unsupported page type '${document.spec?.type}'`);
      if (document.spec?.schema && document.spec.schema.$schema !== JSON_SCHEMA_VERSION) fail(file, `form schema must use ${JSON_SCHEMA_VERSION}`);
      if (document.spec?.type === "form" && !document.spec?.schema) fail(file, "form pages require a JSON Schema");
      for (const action of document.spec?.actions || []) {
        if (!ALLOWED_ACTIONS.has(action.type)) fail(file, `unsupported action '${action.type}'`);
      }
      for (const dynamic of document.spec?.dynamicOptions || []) {
        if (!document.spec?.schema?.properties?.[dynamic.field]) fail(file, `dynamic option references unknown field '${dynamic.field}'`);
      }
      if (typeof document.spec?.route !== "string" || !document.spec.route.startsWith("/")) fail(file, "spec.route must start with '/'");
      if (routes.has(document.spec.route)) fail(file, `duplicate route '${document.spec.route}'`);
      routes.add(document.spec.route);
      inspectObject(document.spec, file, dataSources);
      sourceDocuments.push(document);
      return document;
    }) : [];
    const pageNames = new Set(pages.map((page) => page.metadata.name));
    for (const navigation of moduleDocument.spec?.navigation || []) {
      if (!pageNames.has(navigation.page)) fail(moduleFile, `navigation references unknown page '${navigation.page}'`);
    }
    sourceDocuments.push(moduleDocument);
    modules.push({ ...moduleDocument, spec: { ...moduleDocument.spec, pages } });
  }
  if (!modules.length) throw new Error(`${input}: no module.yaml files found`);
  const sourceDigest = createHash("sha256").update(stableJSON(sourceDocuments)).digest("hex");
  const registry = {
    apiVersion: API_VERSION,
    kind: "Registry",
    compilerVersion: COMPILER_VERSION,
    minimumRuntimeVersion: "0.1.0",
    sourceDigest,
    modules,
  };
  return { registry, json: stableJSON(registry) };
}

function main(): void {
  const args = process.argv.slice(2);
  const check = args.includes("--check");
  const value = (name: string, fallback: string) => {
    const index = args.indexOf(name);
    return resolve(index >= 0 ? args[index + 1] : fallback);
  };
  const input = value("--input", "testdata/modules");
  const output = value("--output", "public/config/registry.json");
  const { json } = compileRegistry(input);
  if (check) {
    if (!existsSync(output) || readFileSync(output, "utf8") !== json) {
      throw new Error(`${output} is stale; run config:compile`);
    }
    process.stdout.write(`Registry is up to date (${basename(output)})\n`);
    return;
  }
  writeFileSync(output, json);
  process.stdout.write(`Compiled ${input} -> ${output}\n`);
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))) main();
