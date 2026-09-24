import { fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ApproveDialog from "../components/ApproveDialog.svelte";
import CloudCard from "../components/CloudCard.svelte";
import * as api from "../lib/api";
import { ApiError } from "../lib/api";
import { NOW } from "./fixtures";
import { awaitingFresh, awaitingReapproval, cloudBlock, cloudTarget, needsTime, recent, running } from "./fixtures/cloud";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, approve: vi.fn(), reject: vi.fn() };
});

function section(container: HTMLElement, name: string): HTMLElement {
  return container.querySelector(`[data-section="${name}"]`) as HTMLElement;
}

function row(container: HTMLElement, id: number): HTMLElement {
  return container.querySelector(`[data-job="${id}"]`) as HTMLElement;
}

describe("CloudCard", () => {
  beforeEach(() => {
    vi.mocked(api.approve).mockReset();
    vi.mocked(api.reject).mockReset();
  });

  it("shows each target's spend against its budgets, and its job cap", () => {
    render(CloudCard, { cloud: cloudBlock(), jobs: running, now: NOW });
    expect(screen.getByText(/modal-a · \$6\.40 today \/ \$30\.00/)).toBeInTheDocument();
    expect(screen.getByText(/\$18\.20 this month \/ \$30\.00 · job cap \$10\.00/)).toBeInTheDocument();
    const bar = screen.getByRole("meter", { name: "modal-a this month" });
    expect(bar.querySelector("i")!.getAttribute("style")).toContain("width: 60.6");
  });

  it("renders the three sections from the fixture", () => {
    const { container } = render(CloudCard, { cloud: cloudBlock(), jobs: running, now: NOW });

    const awaiting = section(container, "awaiting");
    expect(within(awaiting).getByText("Awaiting your OK")).toBeInTheDocument();
    expect(awaiting.querySelectorAll(".crow").length).toBe(2);
    const fresh = row(awaiting, 201);
    expect(within(fresh).getByText("sweep-wd-3")).toBeInTheDocument();
    expect(fresh.textContent).toContain("agent-3");
    expect(fresh.textContent).toContain("H100 · 80GB");
    expect(fresh.textContent).toContain("buys 3h00 · est $4.20 · up to $8.00");
    expect(fresh.textContent).toContain("$0.00 of $10.00 job cap spent");
    expect(fresh.querySelector(".reason")).toBeNull();
    const back = row(awaiting, 202);
    expect(back.textContent).toContain("A100 · 40GB");
    expect(back.textContent).toContain("$4.90 of $10.00 job cap spent");
    expect(within(back).getByText("paused at its approved run time; approve it again to carry on")).toBeInTheDocument();

    const run = section(container, "running");
    expect(run.querySelectorAll(".crow").length).toBe(3);
    // The one that needs a person comes first, flagged.
    const rows = run.querySelectorAll(".crow");
    expect(rows[0].getAttribute("data-job")).toBe("212");
    expect(rows[0].classList.contains("flag")).toBe(true);
    expect(rows[0].textContent).toContain("needs ~30m more");
    expect(rows[0].textContent).toContain("5h16 / 5h00 approved");
    expect(rows[0].textContent).toContain("$8.70 spent or held · cap $10.00");
    expect(within(row(run, 210)).getByText("starting")).toBeInTheDocument();
    expect(row(run, 210).textContent).toContain("30s / 2h00 approved");

    const res = section(container, "recent");
    expect(row(res, 220).textContent).toContain("pulled 0.5 GiB → /home/agent-3/pasar-pulled/220");
    expect(row(res, 221).textContent).toContain("copy at modal-a until Sep 27");
    expect(row(res, 222).textContent).toContain("nothing saved");
    expect(within(row(res, 220)).getByText("completed")).toBeInTheDocument();
  });

  it("links the provider's console only when the job has one", () => {
    const { container } = render(CloudCard, { cloud: cloudBlock(), jobs: running, now: NOW });
    const links = screen.getAllByRole("link", { name: "Modal ↗" });
    expect(links.length).toBe(1);
    expect(links[0].getAttribute("href")).toBe("https://modal.com/apps/placeholder/eval-batch");
    expect(within(row(container, 211)).getByRole("link")).toBe(links[0]);
  });

  it("hides empty sections", () => {
    const { container } = render(CloudCard, {
      cloud: cloudBlock({ awaiting: [], needs_time: [], recent: [] }),
      jobs: [],
      now: NOW,
    });
    expect(screen.getByText("Cloud")).toBeInTheDocument();
    expect(section(container, "awaiting")).toBeNull();
    expect(section(container, "running")).toBeNull();
    expect(section(container, "recent")).toBeNull();
  });

  it("renders nothing when there are no targets", () => {
    const { container } = render(CloudCard, { cloud: cloudBlock({ targets: [] }), jobs: running, now: NOW });
    expect(container.querySelector("section")).toBeNull();
  });

  it("offers Approve and Give more time only on rows a person must act on", () => {
    const { container } = render(CloudCard, { cloud: cloudBlock(), jobs: running, now: NOW });
    expect(screen.getAllByRole("button", { name: "Approve…" }).length).toBe(2);
    for (const b of screen.getAllByRole("button", { name: "Approve…" })) {
      expect(b.closest("[data-section]")!.getAttribute("data-section")).toBe("awaiting");
    }
    const more = screen.getAllByRole("button", { name: "Give more time…" });
    expect(more.length).toBe(1);
    expect(more[0].closest("[data-job]")!.getAttribute("data-job")).toBe("212");
    for (const id of [210, 211, 220, 221, 222]) {
      expect(within(row(container, id)).queryByRole("button", { name: /approve|more time|reject/i })).toBeNull();
    }
  });

  it("Approve opens the dialog with the facts and the worst case on its button", async () => {
    render(CloudCard, { cloud: cloudBlock(), jobs: running, now: NOW });
    const btns = screen.getAllByRole("button", { name: "Approve…" });
    await fireEvent.click(btns[1]); // #202, the re-approval

    const dialog = screen.getByRole("dialog", { name: "Approve #202 finetune-a?" });
    const text = dialog.textContent!;
    expect(text).toContain("A100 on modal-a · First Owner's account");
    expect(text).toContain("40GB · $1.60/hour now");
    expect(text).toContain("1h00");
    expect(text).toContain("of the 1h30 it asked for; the job cap cuts it short");
    expect(text).toContain("$3.10");
    expect(text).toContain("up to $9.00");
    expect(text).toContain("$4.90 of $10.00 spent · $5.10 left");
    expect(text).toMatch(/pull directory/);
    expect(within(dialog).getByRole("button", { name: "Approve · up to $9.00" })).toBeInTheDocument();
    expect(api.approve).not.toHaveBeenCalled();
  });

  it("confirming approves (not extend), closes and tells the app", async () => {
    // The daemon prices it afresh when it approves: the toast says what it agreed to.
    vi.mocked(api.approve).mockResolvedValue({ ...awaitingFresh, state: "queued", cloud: { ...awaitingFresh.cloud!, max_cost: 7.9 } });
    const onnotice = vi.fn();
    render(CloudCard, { cloud: cloudBlock(), jobs: running, now: NOW, onnotice });
    await fireEvent.click(screen.getAllByRole("button", { name: "Approve…" })[0]);
    await fireEvent.click(screen.getByRole("button", { name: "Approve · up to $8.00" }));
    await waitFor(() => expect(api.approve).toHaveBeenCalledWith(201, false));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(onnotice).toHaveBeenCalledWith("#201 approved · up to $7.90");
  });

  it("Give more time opens the dialog in extend mode and approves with extend", async () => {
    vi.mocked(api.approve).mockResolvedValue({ ...needsTime[0], cloud: { ...needsTime[0].cloud!, max_cost: 9.95 } });
    const onnotice = vi.fn();
    render(CloudCard, { cloud: cloudBlock(), jobs: running, now: NOW, onnotice });
    await fireEvent.click(screen.getByRole("button", { name: "Give more time…" }));
    const dialog = screen.getByRole("dialog", { name: "Give #212 train-long more time?" });
    expect(dialog.textContent).toContain("about 30m more");
    expect(dialog.textContent).toContain("priced at today's rate when you confirm");
    await fireEvent.click(within(dialog).getByRole("button", { name: "Give more time · up to $1.30 more" }));
    await waitFor(() => expect(api.approve).toHaveBeenCalledWith(212, true));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(onnotice).toHaveBeenCalledWith("#212 got more time · up to $9.95");
  });

  it("shows the server's refusal inline and keeps the dialog open", async () => {
    vi.mocked(api.approve).mockRejectedValue(new ApiError(409, "job 201's price rose to $3.10/hour; approve it again"));
    const onnotice = vi.fn();
    render(CloudCard, { cloud: cloudBlock(), jobs: running, now: NOW, onnotice });
    await fireEvent.click(screen.getAllByRole("button", { name: "Approve…" })[0]);
    await fireEvent.click(screen.getByRole("button", { name: "Approve · up to $8.00" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("job 201's price rose to $3.10/hour; approve it again");
    expect(screen.getByRole("dialog", { name: "Approve #201 sweep-wd-3?" })).toBeInTheDocument();
    expect(onnotice).not.toHaveBeenCalled();
    expect(api.approve).toHaveBeenCalledTimes(1);
  });

  it("Reject asks first, and only then calls the server", async () => {
    vi.mocked(api.reject).mockResolvedValue(awaitingReapproval);
    const onnotice = vi.fn();
    const { container } = render(CloudCard, { cloud: cloudBlock(), jobs: running, now: NOW, onnotice });
    await fireEvent.click(within(row(container, 202)).getByRole("button", { name: "Reject" }));
    const dialog = screen.getByRole("dialog", { name: "Reject #202?" });
    expect(dialog.textContent).toContain("Reject #202? It won't run.");
    expect(api.reject).not.toHaveBeenCalled();

    await fireEvent.click(within(dialog).getByRole("button", { name: "Keep it" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(api.reject).not.toHaveBeenCalled();

    await fireEvent.click(within(row(container, 202)).getByRole("button", { name: "Reject" }));
    await fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(api.reject).toHaveBeenCalledWith(202));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(onnotice).toHaveBeenCalledWith("#202 rejected");
  });

  it("reads the job live while the dialog is open: a new price shows on the button", async () => {
    const { rerender } = render(CloudCard, { cloud: cloudBlock(), jobs: running, now: NOW });
    await fireEvent.click(screen.getAllByRole("button", { name: "Approve…" })[0]);
    expect(screen.getByRole("button", { name: "Approve · up to $8.00" })).toBeEnabled();
    const repriced = { ...awaitingFresh, cloud: { ...awaitingFresh.cloud!, max_cost: 8.8 } };
    await rerender({ cloud: cloudBlock({ awaiting: [repriced, awaitingReapproval] }), jobs: running, now: NOW });
    expect(screen.getByRole("button", { name: "Approve · up to $8.80" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Approve · up to $8.00" })).toBeNull();
  });

  it("says so, and won't approve, once the job has left the awaiting list", async () => {
    const { rerender } = render(CloudCard, { cloud: cloudBlock(), jobs: running, now: NOW });
    await fireEvent.click(screen.getAllByRole("button", { name: "Approve…" })[0]);
    await rerender({ cloud: cloudBlock({ awaiting: [awaitingReapproval] }), jobs: running, now: NOW });
    const dialog = screen.getByRole("dialog", { name: "Approve #201 sweep-wd-3?" });
    expect(dialog.textContent).toContain("#201 isn't waiting for approval any more");
    const confirm = within(dialog).getByRole("button", { name: "Approve · up to $8.00" });
    expect(confirm).toBeDisabled();
    await fireEvent.click(confirm);
    expect(api.approve).not.toHaveBeenCalled();
  });

  it("won't close the approve dialog while its request is in flight", async () => {
    let settle!: (j: typeof awaitingFresh) => void;
    vi.mocked(api.approve).mockReturnValue(new Promise((resolve) => { settle = resolve; }));
    render(CloudCard, { cloud: cloudBlock(), jobs: running, now: NOW });
    await fireEvent.click(screen.getAllByRole("button", { name: /^Approve…/ })[0]);
    await fireEvent.click(screen.getByRole("button", { name: "Approve · up to $8.00" }));
    await fireEvent.keyDown(window, { key: "Escape" });
    for (const close of screen.getAllByRole("button", { name: "Close" })) await fireEvent.click(close);
    await fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("dialog", { name: "Approve #201 sweep-wd-3?" })).toBeInTheDocument();
    settle({ ...awaitingFresh, state: "queued" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("won't close the reject question while its request is in flight", async () => {
    let settle!: (j: typeof awaitingFresh) => void;
    vi.mocked(api.reject).mockReturnValue(new Promise((resolve) => { settle = resolve; }));
    const { container } = render(CloudCard, { cloud: cloudBlock(), jobs: running, now: NOW });
    await fireEvent.click(within(row(container, 201)).getByRole("button", { name: /^Reject/ }));
    await fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Reject" }));
    await fireEvent.keyDown(window, { key: "Escape" });
    for (const close of screen.getAllByRole("button", { name: "Close" })) await fireEvent.click(close);
    await fireEvent.click(screen.getByRole("button", { name: "Keep it" }));
    expect(screen.getByRole("dialog", { name: "Reject #201?" })).toBeInTheDocument();
    settle({ ...awaitingFresh, state: "cancelled" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("opens a job's panel from its name", async () => {
    const onopen = vi.fn();
    render(CloudCard, { cloud: cloudBlock(), jobs: running, now: NOW, onopen });
    await fireEvent.click(screen.getByRole("button", { name: recent[0].name }));
    expect(onopen).toHaveBeenCalledWith(220);
  });
});

describe("ApproveDialog", () => {
  beforeEach(() => {
    vi.mocked(api.approve).mockReset();
  });

  it("won't approve a job it can't price: no one may approve a worst case they never saw", async () => {
    const unpriced = { ...awaitingFresh, cloud: { ...awaitingFresh.cloud!, estimated_cost: null, max_cost: null, approved_seconds: null } };
    render(ApproveDialog, { props: { job: unpriced, target: cloudTarget(), onclose: () => {} } });
    expect(screen.getByText("can't be priced right now")).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "Approve" });
    expect(confirm).toBeDisabled();
    await fireEvent.click(confirm);
    expect(api.approve).not.toHaveBeenCalled();
  });

  it("leaves the account out when the target is no longer listed", () => {
    render(ApproveDialog, { props: { job: awaitingFresh, target: null, onclose: () => {} } });
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toContain("H100 on modal-a");
    expect(dialog.textContent).not.toContain("account");
  });

  it("puts the most an extension can add on its button, and won't extend a job with no cap", async () => {
    const job = needsTime[0]; // $8.70 spent or held of a $10.00 cap
    const { unmount } = render(ApproveDialog, { props: { job, target: cloudTarget(), extend: true, onclose: () => {} } });
    expect(screen.getByRole("button", { name: "Give more time · up to $1.30 more" })).toBeEnabled();
    unmount();

    const uncapped = { ...job, cloud: { ...job.cloud!, job_cap: null } };
    render(ApproveDialog, { props: { job: uncapped, target: null, extend: true, onclose: () => {} } });
    const confirm = screen.getByRole("button", { name: /^Give more time/ });
    expect(confirm).toBeDisabled();
    await fireEvent.click(confirm);
    expect(api.approve).not.toHaveBeenCalled();
  });

  it("names the submitter's own --max-cost when that is what shortens the run", () => {
    const capped = { ...awaitingFresh, cloud: { ...awaitingFresh.cloud!, user_capped: true, approved_seconds: 3600, full_seconds: 10800 } };
    render(ApproveDialog, { props: { job: capped, target: cloudTarget(), onclose: () => {} } });
    expect(screen.getByText(/of the 3h00 it asked for; its own --max-cost cuts it short/)).toBeInTheDocument();
  });
});
