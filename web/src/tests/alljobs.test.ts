import { describe, expect, it, vi } from "vitest";
import { AllJobs } from "../lib/alljobs.svelte";
import type { JobView } from "../lib/types";
import { job } from "./fixtures";

function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void; reject: (e: unknown) => void } {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

describe("AllJobs", () => {
  it("starts with jobs=null and does nothing while inactive", () => {
    const fetchAll = vi.fn();
    const store = new AllJobs({ fetchAll });
    store.update(false, [job({ id: 1 })]);
    expect(store.jobs).toBeNull();
    expect(fetchAll).not.toHaveBeenCalled();
  });

  it("fetches on first activation, at most once while a fetch is in flight", async () => {
    const d = deferred<JobView[]>();
    const fetchAll = vi.fn().mockReturnValue(d.promise);
    const store = new AllJobs({ fetchAll });
    store.update(true, []);
    store.update(true, []);
    expect(fetchAll).toHaveBeenCalledTimes(1);
    expect(store.jobs).toBeNull();
    d.resolve([job({ id: 1 }), job({ id: 2 })]);
    await d.promise;
    expect(store.jobs?.map((j) => j.id)).toEqual([1, 2]);
  });

  it("overlays live jobs by id onto the cache; jobs missing from live are kept as-is", async () => {
    const d = deferred<JobView[]>();
    const fetchAll = vi.fn().mockReturnValue(d.promise);
    const store = new AllJobs({ fetchAll });
    store.update(true, []);
    d.resolve([job({ id: 1, name: "a" }), job({ id: 2, name: "b" })]);
    await d.promise;

    store.update(true, [job({ id: 1, name: "a-live" })]);
    expect(store.jobs?.find((j) => j.id === 1)?.name).toBe("a-live");
    expect(store.jobs?.find((j) => j.id === 2)?.name).toBe("b");
    // Overlaying an already-known id shouldn't trigger another fetch.
    expect(fetchAll).toHaveBeenCalledTimes(1);
  });

  it("does nothing while inactive, even with a stale cache", async () => {
    const d = deferred<JobView[]>();
    const fetchAll = vi.fn().mockReturnValue(d.promise);
    const store = new AllJobs({ fetchAll });
    store.update(true, []);
    d.resolve([job({ id: 1, name: "a" })]);
    await d.promise;

    store.update(false, [job({ id: 1, name: "a-live" })]);
    expect(store.jobs?.find((j) => j.id === 1)?.name).toBe("a");
    expect(fetchAll).toHaveBeenCalledTimes(1);
  });

  it("refetches when the live snapshot has an id not yet in the cache", async () => {
    const d1 = deferred<JobView[]>();
    const d2 = deferred<JobView[]>();
    const fetchAll = vi.fn().mockReturnValueOnce(d1.promise).mockReturnValueOnce(d2.promise);
    const store = new AllJobs({ fetchAll });
    store.update(true, []);
    d1.resolve([job({ id: 1 })]);
    await d1.promise;
    expect(store.jobs?.map((j) => j.id)).toEqual([1]);

    store.update(true, [job({ id: 2 })]);
    expect(fetchAll).toHaveBeenCalledTimes(2);
    // The new job is overlaid immediately even before the refetch resolves.
    expect(store.jobs?.map((j) => j.id).sort()).toEqual([1, 2]);
    d2.resolve([job({ id: 1 }), job({ id: 2 })]);
    await d2.promise;
    expect(store.jobs?.map((j) => j.id).sort()).toEqual([1, 2]);
  });

  it("does not refetch before minRefetchMs, but does once it has elapsed", async () => {
    let now = 0;
    const clock = () => now;
    const d1 = deferred<JobView[]>();
    const d2 = deferred<JobView[]>();
    const fetchAll = vi.fn().mockReturnValueOnce(d1.promise).mockReturnValueOnce(d2.promise);
    const store = new AllJobs({ fetchAll, minRefetchMs: 1000, clock });

    store.update(true, []);
    d1.resolve([job({ id: 1 })]);
    await d1.promise;
    expect(fetchAll).toHaveBeenCalledTimes(1);

    store.update(true, [job({ id: 1 })]);
    expect(fetchAll).toHaveBeenCalledTimes(1);

    now = 999;
    store.update(true, [job({ id: 1 })]);
    expect(fetchAll).toHaveBeenCalledTimes(1);

    now = 1000;
    store.update(true, [job({ id: 1 })]);
    expect(fetchAll).toHaveBeenCalledTimes(2);
    d2.resolve([job({ id: 1 })]);
    await d2.promise;
  });

  it("clears the in-flight flag on a rejected fetch, so a later update can retry", async () => {
    const d1 = deferred<JobView[]>();
    const fetchAll = vi.fn().mockReturnValueOnce(d1.promise);
    const store = new AllJobs({ fetchAll });
    store.update(true, []);
    d1.reject(new Error("boom"));
    await d1.promise.catch(() => {});
    expect(store.jobs).toBeNull(); // failed fetch, still never loaded

    const d2 = deferred<JobView[]>();
    fetchAll.mockReturnValueOnce(d2.promise);
    store.update(true, []);
    expect(fetchAll).toHaveBeenCalledTimes(2);
    d2.resolve([job({ id: 1 })]);
    await d2.promise;
    expect(store.jobs?.map((j) => j.id)).toEqual([1]);
  });
});
