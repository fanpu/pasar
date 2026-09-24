// The cloud card against a throwaway pasard whose one cloud target is an in-memory fake provider
// (serve-cloud.sh / fake_cloud.py): approving here launches nothing real and spends nothing.
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { fileURLToPath } from "node:url";
import path from "node:path";

// A cloud submit bundles its working directory, which must be a git checkout with a lockfile:
// this repository is one.
const REPO_ROOT = fileURLToPath(new URL("../..", import.meta.url));

async function submitCloud(request: APIRequestContext, name: string): Promise<number> {
  const r = await request.post("/api/jobs", {
    data: { command: "python train.py", time: "30m", cwd: REPO_ROOT, target: "modal-a", gpu: "H100", name, submitter: "e2e" },
  });
  expect(r.ok(), await r.text()).toBeTruthy();
  return (await r.json()).id;
}

async function stateOf(request: APIRequestContext, id: number): Promise<string> {
  return (await (await request.get(`/api/jobs/${id}`)).json()).state;
}

async function jobIdByName(request: APIRequestContext, name: string): Promise<number> {
  const jobs: { id: number; name: string }[] = await (await request.get("/api/jobs?all=true")).json();
  const job = jobs.find((j) => j.name === name);
  if (job === undefined) throw new Error(`no job named ${name} in the seeded fixture`);
  return job.id;
}

function awaitingRow(page: Page, id: number) {
  return page.getByRole("region", { name: "Cloud" }).locator(`[data-section="awaiting"] [data-job="${id}"]`);
}

test("approving a cloud job goes through the dialog and its worst case", async ({ page, request }) => {
  const id = await submitCloud(request, "e2e-approve");
  await page.goto("/");
  const row = awaitingRow(page, id);
  await expect(row).toBeVisible({ timeout: 15_000 });
  await expect(row).toContainText("e2e-approve");
  await expect(row).toContainText(/up to \$\d+\.\d\d/);

  await row.getByRole("button", { name: /^Approve…/ }).click();
  const dialog = page.getByRole("dialog", { name: `Approve #${id} e2e-approve?` });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("H100 on modal-a · First Owner's account");
  await expect(dialog).toContainText(/Estimated cost\s*\$\d+\.\d\d/);
  await expect(dialog).toContainText(/This attempt can spend\s*up to \$\d+\.\d\d/);
  expect(await stateOf(request, id)).toBe("awaiting"); // opening the dialog spent nothing

  const confirm = dialog.getByRole("button", { name: /^Approve · up to \$\d+\.\d\d$/ });
  await expect(confirm).toBeVisible();
  await confirm.click();

  await expect(dialog).toBeHidden();
  await expect(row).toHaveCount(0);
  expect(["queued", "running"]).toContain(await stateOf(request, id));
  // It never vanishes from the card: approved, it waits to launch (or runs) in the next section.
  await expect(page.getByRole("region", { name: "Cloud" }).locator(`[data-section="running"] [data-job="${id}"]`)).toBeVisible();
});

test("reject asks first, and Keep it keeps the job waiting", async ({ page, request }) => {
  const id = await submitCloud(request, "e2e-reject");
  await page.goto("/");
  const row = awaitingRow(page, id);
  await expect(row).toBeVisible({ timeout: 15_000 });

  await row.getByRole("button", { name: /^Reject #/ }).click();
  const ask = page.getByRole("dialog", { name: `Reject #${id}?` });
  await expect(ask).toContainText(`Reject #${id}? It won't run.`);
  await ask.getByRole("button", { name: "Keep it" }).click();
  await expect(ask).toBeHidden();
  await expect(row).toBeVisible();
  expect(await stateOf(request, id)).toBe("awaiting");

  await row.getByRole("button", { name: /^Reject #/ }).click();
  await ask.getByRole("button", { name: "Reject", exact: true }).click();
  await expect(ask).toBeHidden();
  await expect(row).toHaveCount(0);
  expect(await stateOf(request, id)).toBe("cancelled");
});

// Where the lane screenshots go: the repository's own private/mockups unless told otherwise.
const MOCKUPS = process.env.PASAR_MOCKUPS ?? path.join(REPO_ROOT, "private/mockups");

function schedule(page: Page) {
  return page.locator(".sec", { has: page.getByRole("heading", { name: /^Schedule/ }) });
}

test.describe("cloud lanes", () => {
  test.use({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 });

  test("the schedule shows each account's attempts in a lane under the chart", async ({ page, request }) => {
    await page.goto("/");
    const sec = schedule(page);
    const lanes = sec.locator("svg.tl g.lane");
    await expect(lanes).toHaveCount(2, { timeout: 15_000 });
    await expect(lanes.nth(0)).toContainText("modal-a · First Owner");
    await expect(lanes.nth(1)).toContainText("modal-b · Second Owner");
    // Seeded by fake_cloud.py: taken back by the provider, then resumed on a new approval.
    const resumed = await jobIdByName(request, "resume-ft");
    await expect(sec.getByRole("button", { name: new RegExp(`^#${resumed} resume-ft on modal-b`) })).toHaveCount(2);
    // Awaiting jobs have run nothing yet, so they are not drawn.
    const waiting = await jobIdByName(request, "rlhf-policy");
    await expect(sec.getByRole("button", { name: new RegExp(`^#${waiting} `) })).toHaveCount(0);

    await sec.screenshot({ path: path.join(MOCKUPS, "cloud-lanes.png") });

    const running = sec.getByRole("button", { name: new RegExp(`^#${resumed} resume-ft on modal-b, running since`) });
    await running.hover();
    await expect(page.locator(".tip")).toContainText("Second Owner's account");
    await expect(page.locator(".tip")).toContainText("spent or held");
    await sec.screenshot({ path: path.join(MOCKUPS, "cloud-lanes-hover.png") });
    await running.click();
    await expect(page).toHaveURL(new RegExp(`/jobs/${resumed}$`));
    await expect(page.getByRole("dialog")).toBeVisible();
  });
});

test.describe("phone width", () => {
  // Same width JobTable's own mobile card view kicks in at; deviceScaleFactor matches a real
  // phone's for the mockup screenshot below.
  test.use({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 3 });

  test("a recent result's status text keeps a full-width line, not a squeezed column", async ({ page, request }) => {
    await page.goto("/");
    // Seeded by fake_cloud.py: finished with a nonzero exit, its results held at the target
    // (pull_settle) so its row reads "at modal-a until ... · pulling…" — the long status text
    // that used to be squeezed into a one-word column on a narrow screen.
    const id = await jobIdByName(request, "grid-search-3");
    const row = page.getByRole("region", { name: "Cloud" }).locator(`[data-section="recent"] [data-job="${id}"]`);
    await expect(row).toBeVisible({ timeout: 15_000 });
    const status = row.locator(".money .cap");
    await expect(status).toContainText("pulling");
    const box = await status.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.width).toBeGreaterThanOrEqual(150);

    await page.getByRole("region", { name: "Cloud" }).screenshot({
      path: path.join(REPO_ROOT, "private/mockups/cloud-mobile.png"),
    });
  });

  test("the cloud lanes fit a phone", async ({ page }) => {
    await page.goto("/");
    const sec = schedule(page);
    await expect(sec.locator("svg.tl g.lane")).toHaveCount(2, { timeout: 15_000 });
    const box = await sec.boundingBox();
    expect(box!.width).toBeLessThanOrEqual(390);
    await sec.screenshot({ path: path.join(MOCKUPS, "cloud-lanes-phone.png") });
  });
});
