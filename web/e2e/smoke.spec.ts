import { expect, test } from "@playwright/test";

let ids: number[] = [];

test.beforeAll(async ({ request }) => {
  for (const [name, bid] of [["alpha", 1000], ["beta", 1500]] as const) {
    const r = await request.post("/api/jobs", { data: { command: "sleep 300", time: "10m", cwd: "/tmp", mem: "1G", bid, name } });
    ids.push((await r.json()).id);
  }
});

test.afterAll(async ({ request }) => {
  for (const id of ids) await request.post(`/api/jobs/${id}/cancel`).catch(() => {});
});

test("dashboard shows the mascot, tiles, timeline and jobs", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText(/cooking|packed|\bwaiting/)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("memory pool")).toBeVisible();
  await expect(page.getByText("alpha").filter({ visible: true }).first()).toBeVisible();
  await expect(page.getByText("★ 1500").filter({ visible: true }).first()).toBeVisible();
});

test("job panel opens from the list and deep-links", async ({ page }) => {
  await page.goto("/");
  await page.getByText("beta").filter({ visible: true }).first().click();
  await expect(page).toHaveURL(new RegExp(`/jobs/${ids[1]}$`));
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page).toHaveURL(/\/$/);
  await page.goto(`/jobs/${ids[0]}`);
  await expect(page.getByRole("dialog").getByText("alpha")).toBeVisible();
});

test("phone layout uses cards and tabs", async ({ page }, info) => {
  test.skip(info.project.name !== "phone");
  await page.goto(`/jobs/${ids[0]}`);
  await expect(page.getByRole("tab", { name: "Logs" })).toBeVisible();
  await page.getByRole("tab", { name: "Logs" }).click();
  await expect(page.getByText(/No output yet|────/).first()).toBeVisible();
});

test("screenshot the dashboard", async ({ page }, info) => {
  await page.goto("/");
  await expect(page.getByText("memory pool")).toBeVisible();
  await page.screenshot({ path: `/tmp/pasar-e2e/${info.project.name}.png`, fullPage: true });
});
