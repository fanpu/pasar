// The cloud card against a throwaway pasard whose one cloud target is an in-memory fake provider
// (serve-cloud.sh / fake_cloud.py): approving here launches nothing real and spends nothing.
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { fileURLToPath } from "node:url";

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

  await row.getByRole("button", { name: "Approve…" }).click();
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
});

test("reject asks first, and Keep it keeps the job waiting", async ({ page, request }) => {
  const id = await submitCloud(request, "e2e-reject");
  await page.goto("/");
  const row = awaitingRow(page, id);
  await expect(row).toBeVisible({ timeout: 15_000 });

  await row.getByRole("button", { name: "Reject", exact: true }).click();
  const ask = page.getByRole("dialog", { name: `Reject #${id}?` });
  await expect(ask).toContainText(`Reject #${id}? It won't run.`);
  await ask.getByRole("button", { name: "Keep it" }).click();
  await expect(ask).toBeHidden();
  await expect(row).toBeVisible();
  expect(await stateOf(request, id)).toBe("awaiting");

  await row.getByRole("button", { name: "Reject", exact: true }).click();
  await ask.getByRole("button", { name: "Reject", exact: true }).click();
  await expect(ask).toBeHidden();
  await expect(row).toHaveCount(0);
  expect(await stateOf(request, id)).toBe("cancelled");
});
