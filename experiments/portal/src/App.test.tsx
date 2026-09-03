// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ModuleBoundary } from "./components/ModuleBoundary";

function BrokenModule(): never {
  throw new Error("invalid module configuration");
}

describe("module error isolation", () => {
  it("renders a module-local failure instead of crashing the portal", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(<ModuleBoundary module="broken"><BrokenModule /></ModuleBoundary>);
    expect(screen.getByRole("alert").textContent).toContain("invalid module configuration");
    vi.restoreAllMocks();
  });
});
