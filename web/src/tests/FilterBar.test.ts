import { render, screen, fireEvent } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import FilterBar from "../components/FilterBar.svelte";
import { EMPTY_FILTER, type Filter, type StateFilter } from "../lib/jobfilter";

const ZERO_COUNTS: Record<StateFilter, number> = { running: 0, queued: 0, completed: 0, failed: 0, cancelled: 0 };

function filter(overrides: Partial<Filter> = {}): Filter {
  return { ...EMPTY_FILTER, ...overrides };
}

describe("FilterBar", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("debounces the search box and updates the URL with replace", () => {
    const onchange = vi.fn();
    render(FilterBar, { filter: EMPTY_FILTER, counts: ZERO_COUNTS, known: { tags: [], by: [] }, onchange });
    const input = screen.getByRole("searchbox");
    fireEvent.input(input, { target: { value: "lr" } });
    expect(onchange).not.toHaveBeenCalled();
    vi.advanceTimersByTime(200);
    expect(onchange).toHaveBeenCalledWith({ ...EMPTY_FILTER, q: "lr" }, true);
  });

  it("toggles a state chip and pushes the change", async () => {
    const onchange = vi.fn();
    render(FilterBar, { filter: EMPTY_FILTER, counts: { ...ZERO_COUNTS, running: 2 }, known: { tags: [], by: [] }, onchange });
    await fireEvent.click(screen.getByRole("button", { name: /running/ }));
    expect(onchange).toHaveBeenCalledWith({ ...EMPTY_FILTER, states: ["running"] }, false);
  });

  it("marks an unselected, zero-count state chip as faint", () => {
    render(FilterBar, { filter: EMPTY_FILTER, counts: ZERO_COUNTS, known: { tags: [], by: [] }, onchange: () => {} });
    expect(screen.getByRole("button", { name: /cancelled/ })).toHaveClass("off");
  });

  it("shows an active tag chip and removes it via its ×", async () => {
    const onchange = vi.fn();
    const f = filter({ tags: ["sweep-a"] });
    render(FilterBar, { filter: f, counts: ZERO_COUNTS, known: { tags: ["sweep-a"], by: [] }, onchange });
    expect(screen.getByText(/tag: sweep-a/)).toBeInTheDocument();
    await fireEvent.click(screen.getByRole("button", { name: "remove tag sweep-a" }));
    expect(onchange).toHaveBeenCalledWith({ ...EMPTY_FILTER, tags: [] }, false);
  });

  it("removes an active submitter chip via its ×", async () => {
    const onchange = vi.fn();
    const f = filter({ by: ["opus-triage"] });
    render(FilterBar, { filter: f, counts: ZERO_COUNTS, known: { tags: [], by: ["opus-triage"] }, onchange });
    await fireEvent.click(screen.getByRole("button", { name: "remove submitter opus-triage" }));
    expect(onchange).toHaveBeenCalledWith({ ...EMPTY_FILTER, by: [] }, false);
  });

  it("adds a tag from the + tag select, excluding tags already selected", async () => {
    const onchange = vi.fn();
    const f = filter({ tags: ["already"] });
    render(FilterBar, {
      filter: f, counts: ZERO_COUNTS, known: { tags: ["already", "sweep-a"], by: [] }, onchange,
    });
    const select = screen.getByLabelText("add tag filter") as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual(["", "sweep-a"]);
    await fireEvent.change(select, { target: { value: "sweep-a" } });
    expect(onchange).toHaveBeenCalledWith({ ...f, tags: ["already", "sweep-a"] }, false);
  });

  it("adds a submitter from the + submitter select", async () => {
    const onchange = vi.fn();
    render(FilterBar, { filter: EMPTY_FILTER, counts: ZERO_COUNTS, known: { tags: [], by: ["opus-triage"] }, onchange });
    await fireEvent.change(screen.getByLabelText("add submitter filter"), { target: { value: "opus-triage" } });
    expect(onchange).toHaveBeenCalledWith({ ...EMPTY_FILTER, by: ["opus-triage"] }, false);
  });

  it("shows clear only when the filter is active, and it resets to empty", async () => {
    const onchange = vi.fn();
    const { rerender } = render(FilterBar, {
      filter: EMPTY_FILTER, counts: ZERO_COUNTS, known: { tags: [], by: [] }, onchange,
    });
    expect(screen.queryByText("clear")).toBeNull();

    await rerender({ filter: filter({ q: "x" }) });
    await fireEvent.click(screen.getByText("clear"));
    expect(onchange).toHaveBeenCalledWith(EMPTY_FILTER, false);
  });
});
