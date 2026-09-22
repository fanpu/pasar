import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Live } from "../lib/live.svelte";

class FakeES {
  static last: FakeES | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  closed = false;
  constructor(public url: string) { FakeES.last = this; }
  close() { this.closed = true; }
  emit(data: unknown) { this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(data) })); }
}

const snap = (version: number) => ({ status: { version } as any, jobs: [] });

describe("Live", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("tracks snapshots, notifies with prev/next, reconnects after errors", async () => {
    const fetchGpu = vi.fn().mockResolvedValue({ power_w: [], temp_c: [], util_pct: [] });
    const live = new Live({ eventSource: FakeES as any, fetchGpu, retryMs: 1000, gpuEveryMs: 5000 });
    const seen: [number | null, number][] = [];
    live.onSnapshot((p, n) => seen.push([p?.status.version ?? null, n.status.version]));
    live.start();
    const first = FakeES.last!;
    expect(first.url).toBe("/api/stream");
    first.emit(snap(1));
    first.emit(snap(2));
    expect(live.snapshot?.status.version).toBe(2);
    expect(live.connected).toBe(true);
    expect(seen).toEqual([[null, 1], [1, 2]]);
    first.onerror?.(new Event("error"));
    expect(live.connected).toBe(false);
    expect(first.closed).toBe(true);
    vi.advanceTimersByTime(1000);
    expect(FakeES.last).not.toBe(first);
    expect(fetchGpu).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetchGpu).toHaveBeenCalledTimes(2);
    live.stop();
    expect(FakeES.last!.closed).toBe(true);
  });
});
