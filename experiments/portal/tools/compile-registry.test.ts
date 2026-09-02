import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import { compileRegistry } from "./compile-registry";

function fixture(pagePatch = ""): string {
  const root = mkdtempSync(join(tmpdir(), "portal-registry-"));
  mkdirSync(join(root, "sample", "pages"), { recursive: true });
  writeFileSync(join(root, "sample", "module.yaml"), `apiVersion: ee.pingcap.net/v1alpha1\nkind: Module\nmetadata: {name: sample, title: Sample}\nspec:\n  dataSources: [{name: api, basePath: /ee/api/sample}]\n  navigation: [{label: Items, page: items}]\n`);
  writeFileSync(join(root, "sample", "pages", "items.yaml"), `apiVersion: ee.pingcap.net/v1alpha1\nkind: Page\nmetadata: {name: items, module: sample, title: Items}\nspec:\n  type: list\n  route: /sample/items\n  request: {dataSource: api, method: GET, path: /items}\n  response: {itemsPointer: /}\n  itemRoute: /sample/items/{id}\n  itemIDPointer: /id\n  columns: [{label: ID, pointer: /id}]\n${pagePatch}`);
  return root;
}

describe("registry compiler", () => {
  test("produces deterministic output", () => {
    const root = fixture();
    expect(compileRegistry(root).json).toBe(compileRegistry(root).json);
  });
  test("rejects cross-origin requests", () => {
    const root = fixture("  primaryAction:\n    request: {dataSource: api, method: GET, path: 'https://example.com'}\n");
    expect(() => compileRegistry(root)).toThrow(/relative absolute-path/);
  });
  test("rejects arbitrary scripts", () => {
    const root = fixture("  script: alert(1)\n");
    expect(() => compileRegistry(root)).toThrow(/forbidden key 'script'/);
  });
  test("rejects unknown plugins and actions", () => {
    expect(() => compileRegistry(fixture("  plugin: arbitrary.widget\n"))).toThrow(/unknown plugin/);
    expect(() => compileRegistry(fixture("  actions: [{name: run, label: Run, type: execute-code}]\n"))).toThrow(/unsupported action/);
  });
  test("rejects bad page references", () => {
    const root = fixture();
    const moduleFile = join(root, "sample", "module.yaml");
    writeFileSync(moduleFile, readFileSync(moduleFile, "utf8").replace("page: items", "page: missing"));
    expect(() => compileRegistry(root)).toThrow(/unknown page/);
  });
  test("rejects duplicate routes", () => {
    const root = fixture();
    const pages = join(root, "sample", "pages");
    writeFileSync(join(pages, "duplicate.yaml"), readFileSync(join(pages, "items.yaml"), "utf8").replace("name: items", "name: duplicate"));
    expect(() => compileRegistry(root)).toThrow(/duplicate route/);
  });
  test("rejects incompatible JSON Schema versions and value sources", () => {
    expect(() => compileRegistry(fixture("  schema: {$schema: 'http://json-schema.org/draft-07/schema#'}\n"))).toThrow(/draft\/2020-12/);
    expect(() => compileRegistry(fixture("  primaryAction:\n    request: {dataSource: api, method: GET, path: /items, query: {q: {from: environment, pointer: /q}}}\n"))).toThrow(/unsupported value source/);
  });
});
