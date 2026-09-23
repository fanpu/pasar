import { describe, expect, it, vi } from "vitest";
import { Sparks } from "../lib/sparks.svelte";
import type { SparkMap } from "../lib/types";

function spark(key: string, latest: number): SparkMap[number] {
  return { key, points: [[1, latest + 1], [2, latest]], latest };
}

describe("Sparks", () => {
  it("starts empty and doesn't fetch when no rows are visible", () => {
    const fetchSparks = vi.fn();
    const store = new Sparks({ fetchSparks });
    store.update([]);
    expect(store.data).toEqual({});
    expect(fetchSparks).not.toHaveBeenCalled();
  });

  it("fetches the visible ids and exposes them by id", async () => {
    const fetchSparks = vi.fn().mockResolvedValue({ 2: spark("loss", 0.4) });
    const store = new Sparks({ fetchSparks });
    store.update([2, 1]);
    expect(fetchSparks).toHaveBeenCalledWith([1, 2]); // sorted, so id order can't cause a refetch
    await vi.waitFor(() => expect(store.data[2]?.latest).toBe(0.4));
  });

  it("doesn't refetch for the same ids until the interval has passed", async () => {
    let now = 1000;
    const fetchSparks = vi.fn().mockResolvedValue({});
    const store = new Sparks({ fetchSparks, minRefetchMs: 10000, clock: () => now });
    store.update([1]);
    await vi.waitFor(() => expect(fetchSparks).toHaveBeenCalledTimes(1));

    now = 5000;
    store.update([1]);
    expect(fetchSparks).toHaveBeenCalledTimes(1);

    now = 11001;
    store.update([1]);
    expect(fetchSparks).toHaveBeenCalledTimes(2);
  });

  it("refetches immediately when a row appears that it has no data for", async () => {
    const fetchSparks = vi.fn().mockResolvedValue({});
    const store = new Sparks({ fetchSparks, minRefetchMs: 10000, clock: () => 1000 });
    store.update([1]);
    await vi.waitFor(() => expect(fetchSparks).toHaveBeenCalledTimes(1));
    store.update([1, 7]);
    expect(fetchSparks).toHaveBeenCalledWith([1, 7]);
  });

  it("keeps at most one fetch in flight", () => {
    const fetchSparks = vi.fn().mockReturnValue(new Promise(() => {}));
    const store = new Sparks({ fetchSparks, clock: () => 1000 });
    store.update([1]);
    store.update([1, 2]);
    expect(fetchSparks).toHaveBeenCalledTimes(1);
  });

  it("keeps the last good data when a fetch fails", async () => {
    const fetchSparks = vi.fn()
      .mockResolvedValueOnce({ 1: spark("loss", 0.4) })
      .mockRejectedValueOnce(new Error("offline"));
    let now = 1000;
    const store = new Sparks({ fetchSparks, minRefetchMs: 1, clock: () => now });
    store.update([1]);
    await vi.waitFor(() => expect(store.data[1]?.latest).toBe(0.4));
    now = 9000;
    store.update([1]);
    await vi.waitFor(() => expect(fetchSparks).toHaveBeenCalledTimes(2));
    expect(store.data[1]?.latest).toBe(0.4);
  });
});
