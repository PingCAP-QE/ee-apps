import { afterEach, describe, expect, it, vi } from "vitest";
import { actionVisible, evaluate, executeRequest, formatValue, mapResponse } from "./runtime";

afterEach(() => vi.unstubAllGlobals());

describe("configuration runtime", () => {
  it("maps only declared context values", () => {
    expect(evaluate({ from: "form", pointer: "/product" }, { form: { product: "tidb" } })).toBe("tidb");
    expect(evaluate({ const: "community" }, {})).toBe("community");
    expect(actionVisible({ permissions: { canRerun: true } }, { pointer: "/permissions/canRerun", equals: true })).toBe(true);
  });

  it("builds same-origin requests and response errors", async () => {
    vi.stubGlobal("window", { location: { origin: "https://prow.tidb.net" } });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 12 }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetch);
    await expect(executeRequest({
      dataSource: "tibuild",
      method: "POST",
      path: "/devbuilds",
      body: { request: { product: { from: "form", pointer: "/product" }, edition: { const: "community" } } },
    }, [{ name: "tibuild", basePath: "/ee/api/tibuild/v2" }], { form: { product: "pd" } })).resolves.toEqual({ id: 12 });
    expect(fetch).toHaveBeenCalledWith(new URL("https://prow.tidb.net/ee/api/tibuild/v2/devbuilds"), expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ request: { product: "pd", edition: "community" } }),
    }));
  });

  it("uses registered formatters", () => {
    expect(formatValue(65, "duration")).toBe("1m 5s");
    expect(formatValue(null)).toBe("—");
  });

  it("maps response pointers, aliases, defaults, collections, and statuses", () => {
    expect(mapResponse({ items: [{ state: "ok", owner: null }] }, {
      pointer: "/items",
      collection: true,
      aliases: { status: "/state", creator: "/owner" },
      defaults: { creator: "unknown" },
      statusMaps: { status: { ok: "SUCCESS" } },
    })).toEqual([{ status: "SUCCESS", creator: "unknown" }]);
  });
});
