import { render } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";
import ReasonBox from "../components/ReasonBox.svelte";
import { mascot } from "../lib/mascot.svelte";
import { job } from "./fixtures";

describe("ReasonBox", () => {
  it("does not re-pick the mascot image when the job prop is a new object with the same reason", async () => {
    const spy = vi.spyOn(mascot, "pick");
    const j1 = job({ id: 1, state: "failed", reason: "gpu_oom" });
    const { rerender } = render(ReasonBox, { job: j1, detail: null });
    const callsAfterMount = spy.mock.calls.length;

    // A new job object each snapshot tick, same reason: must not re-randomise.
    const j2 = job({ id: 1, state: "failed", reason: "gpu_oom" });
    await rerender({ job: j2, detail: null });
    const j3 = job({ id: 1, state: "failed", reason: "gpu_oom" });
    await rerender({ job: j3, detail: null });

    expect(spy.mock.calls.length).toBe(callsAfterMount);
  });

  it("re-picks the mascot image when the failure reason actually changes", async () => {
    const spy = vi.spyOn(mascot, "pick");
    const j1 = job({ id: 1, state: "failed", reason: "gpu_oom" });
    const { rerender } = render(ReasonBox, { job: j1, detail: null });
    const callsAfterMount = spy.mock.calls.length;

    const j2 = job({ id: 1, state: "failed", reason: "lost" });
    await rerender({ job: j2, detail: null });

    expect(spy.mock.calls.length).toBeGreaterThan(callsAfterMount);
  });
});
