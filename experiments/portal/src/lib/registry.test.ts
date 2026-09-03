import { describe, expect, it } from "vitest";
import { validateRegistry } from "./registry";

describe("registry compatibility", () => {
  it("accepts the current contract", () => {
    const registry = validateRegistry({
      registryVersion: 1,
      compilerVersion: "0.1.0",
      minimumRuntimeVersion: "0.1.0",
      sourceDigest: "digest",
      modules: [],
    });
    expect(registry.modules).toEqual([]);
  });

  it("isolates incompatible configuration before rendering", () => {
    expect(() => validateRegistry({
      registryVersion: 1,
      minimumRuntimeVersion: "99.0.0",
      modules: [],
    })).toThrow(/requires Portal/);
  });
});
