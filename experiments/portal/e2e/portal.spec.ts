import { expect, test, type Page } from "@playwright/test";

const build = {
  id: 42,
  meta: { createdBy: "developer@pingcap.com", createdAt: "2026-09-02T06:00:00Z", updatedAt: "2026-09-02T06:05:00Z" },
  spec: { product: "tidb", edition: "community", platform: "linux/amd64", gitRef: "branch/master", gitHash: "0123456789abcdef0123456789abcdef01234567" },
  status: {
    status: "SUCCESS",
    pipelineViewURL: "https://tekton.example.com/build/42",
    tektonStatus: { pipelines: [{ name: "tidb-amd64", platform: "linux/amd64", status: "Succeeded", url: "https://tekton.example.com/run/42" }] },
    buildReport: {
      images: [{ platform: "linux/amd64", url: "ghcr.io/pingcap/tidb:test" }],
      binaries: [{ component: "tidb", platform: "linux/amd64", url: "https://downloads.example.com/tidb.tar.gz", sha256URL: "https://downloads.example.com/tidb.sha256" }],
    },
  },
  permissions: { canRerun: true },
};

async function mockAPI(page: Page) {
  await page.route("**/tibuild/api/v2/devbuilds**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/capabilities")) return route.fulfill({
      json: { products: [{ id: "tidb", label: "TiDB", editions: ["community"], platforms: ["linux", "linux/amd64", "linux/arm64"] }], pipelineEngines: ["tekton"] },
    });
    if (path.endsWith("/42/rerun")) return route.fulfill({ json: { ...build, id: 43 } });
    if (path.endsWith("/42")) return route.fulfill({ json: build });
    if (route.request().method() === "POST") return route.fulfill({ json: build });
    return route.fulfill({ json: [build] });
  });
}

test.beforeEach(async ({ page }) => mockAPI(page));

test("catalog, filters, create, artifacts, and rerun are keyboard accessible", async ({ page }, testInfo) => {
  await page.goto("/ee/");
  await expect(page.getByRole("heading", { name: "Engineering tools" })).toBeVisible();
  await page.getByPlaceholder("Search tools").fill("build");
  await page.locator(".tool-card a").click();

  await expect(page.getByRole("heading", { name: "Development builds" })).toBeVisible();
  const buildSurface = page.locator(testInfo.project.name === "mobile" ? ".mobile-cards" : ".desktop-table");
  await expect(buildSurface.getByText("branch/master")).toBeVisible();
  await page.getByLabel("Search builds").fill("42");
  await page.getByRole("link", { name: "New build" }).click();

  await page.getByLabel("Product").click();
  await page.getByRole("option", { name: "TiDB" }).click();
  await page.getByLabel("Git ref").fill("branch/master");
  await page.getByRole("button", { name: "Start build" }).click();

  await expect(page).toHaveURL(/\/ee\/tibuild\/builds\/42$/);
  await expect(page.getByRole("heading", { name: "Build #42" })).toBeVisible();
  await expect(page.getByRole("link", { name: /Download: https:\/\/downloads\.example\.com\/tidb\.tar\.gz/ })).toBeVisible();
  await page.getByRole("button", { name: "Rerun build" }).click();
  await page.getByRole("button", { name: "Confirm" }).click();
  await expect(page).toHaveURL(/\/ee\/tibuild\/builds\/43$/);
});

test("mobile build list uses cards", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "mobile-only assertion");
  await page.goto("/ee/tibuild/builds");
  await expect(page.locator(".desktop-table")).toBeHidden();
  await expect(page.locator(".mobile-cards")).toBeVisible();
  await expect(page.locator(".mobile-cards").getByText("branch/master")).toBeVisible();
});
