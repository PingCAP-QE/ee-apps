import { describe, expect, it } from "vitest";
import { validateRegistry } from "./registry";

describe("registry compatibility", () => {
  it("accepts the current contract", () => {
    const registry = validateRegistry({
      apiVersion: "ee.pingcap.net/v1alpha1",
      kind: "Registry",
      compilerVersion: "0.1.0",
      minimumRuntimeVersion: "0.1.0",
      sourceDigest: "digest",
      modules: [],
    });
    expect(registry.modules).toEqual([]);
  });

  it("isolates incompatible configuration before rendering", () => {
    expect(() => validateRegistry({
      apiVersion: "ee.pingcap.net/v1alpha1",
      kind: "Registry",
      minimumRuntimeVersion: "99.0.0",
      modules: [],
    })).toThrow(/requires Portal/);
  });
});
