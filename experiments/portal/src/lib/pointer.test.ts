import { describe, expect, it } from "vitest";
import { getPointer, interpolate, setPointer } from "./pointer";

describe("JSON Pointer helpers", () => {
  it("reads escaped keys and creates nested values", () => {
    expect(getPointer({ "a/b": { "~value": 42 } }, "/a~1b/~0value")).toBe(42);
    const result: Record<string, unknown> = {};
    setPointer(result, "/request/product", "tidb");
    expect(result).toEqual({ request: { product: "tidb" } });
  });

  it("encodes route substitutions", () => {
    expect(interpolate("/builds/{id}", { id: "a/b" })).toBe("/builds/a%2Fb");
  });
});
