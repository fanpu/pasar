import { render, screen } from "@testing-library/svelte";
import { describe, expect, it } from "vitest";
import Facts from "../components/Facts.svelte";
import { attempt, job, jobDetail, NOW } from "./fixtures";

describe("Facts", () => {
  it("says 'first run' for a job on its first attempt", () => {
    const j = job({ state: "running", attempts: 1 });
    render(Facts, { job: j, detail: jobDetail({ ...j, attempts: [attempt({ n: 1 })] }), now: NOW });
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.getByText("first run")).toBeInTheDocument();
  });

  it("breaks the attempt count down by preemptions and retries instead of assuming all preemptions", () => {
    const j = job({ state: "running", attempts: 3 });
    const detail = jobDetail({
      ...j,
      attempts: [
        attempt({ n: 1, end_kind: "preempted" }),
        attempt({ n: 2, end_kind: "failed" }),
        attempt({ n: 3, end_time: null }),
      ],
    });
    render(Facts, { job: j, detail, now: NOW });
    expect(screen.getByText("attempt 3")).toBeInTheDocument();
    expect(screen.getByText("after 1 preemption, 1 retry")).toBeInTheDocument();
  });

  it("says 'restarted' generically when detail hasn't loaded yet", () => {
    const j = job({ state: "running", attempts: 3 });
    render(Facts, { job: j, detail: null, now: NOW });
    expect(screen.getByText("attempt 3")).toBeInTheDocument();
    expect(screen.getByText("restarted")).toBeInTheDocument();
  });

  it("shows the expected run time from progress next to the estimate", () => {
    const j = job({ state: "running", attempts: 1, run_time: 720, est_runtime: 600, expected_runtime: 3600, eta_source: "progress" });
    render(Facts, { job: j, detail: jobDetail({ ...j, attempts: [attempt({ n: 1 })] }), now: NOW });
    expect(screen.getByText("of ~1h00 from progress (est. 10m)")).toBeInTheDocument();
  });
});
