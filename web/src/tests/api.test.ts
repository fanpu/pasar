import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, approve, cancelJob, followLog, reject, setBid, submitJob } from "../lib/api";

function mockFetch(status: number, body: unknown) {
  const f = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", f);
  return f;
}
afterEach(() => vi.unstubAllGlobals());

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  closed = false;
  #listeners = new Map<string, ((e: Event) => void)[]>();
  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }
  close(): void {
    this.closed = true;
  }
  addEventListener(type: string, cb: (e: Event) => void): void {
    const list = this.#listeners.get(type) ?? [];
    list.push(cb);
    this.#listeners.set(type, list);
  }
  emit(data: unknown): void {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(data) }));
  }
  fireEnd(): void {
    for (const cb of this.#listeners.get("end") ?? []) cb(new Event("end"));
  }
}

describe("api", () => {
  it("sends JSON and returns the job", async () => {
    const f = mockFetch(200, { id: 3, bid: 1500 });
    expect((await setBid(3, 1500, true)).bid).toBe(1500);
    const [url, init] = f.mock.calls[0];
    expect(url).toBe("/api/jobs/3");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body)).toEqual({ bid: 1500, preempt: true });
    expect(init.headers["Content-Type"]).toBe("application/json");
  });
  it("posts without a body for cancel", async () => {
    const f = mockFetch(200, { id: 3, state: "cancelled" });
    await cancelJob(3);
    expect(f.mock.calls[0][0]).toBe("/api/jobs/3/cancel");
    expect(f.mock.calls[0][1].method).toBe("POST");
  });
  it("raises ApiError with the detail string", async () => {
    mockFetch(409, { detail: "job 3 already cancelled" });
    await expect(cancelJob(3)).rejects.toMatchObject({ status: 409, message: "job 3 already cancelled" });
  });
  it("joins pydantic validation details", async () => {
    mockFetch(422, { detail: [{ msg: "Field required" }, { msg: "bad time" }] });
    const err = await submitJob({ command: "x", time: "1h", cwd: "/" }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.message).toBe("Field required; bad time");
  });
  it("approves a job with no query string by default", async () => {
    const f = mockFetch(200, { id: 3, state: "queued" });
    await approve(3);
    expect(f.mock.calls[0][0]).toBe("/api/jobs/3/approve");
    expect(f.mock.calls[0][1].method).toBe("POST");
  });
  it("sends a raised --max-cost as max_cost", async () => {
    const f = mockFetch(200, {});
    await approve(3, true, undefined, 6.95);
    expect(f.mock.calls[0][0]).toBe("/api/jobs/3/approve?extend=1&max_cost=6.95");
  });
  it("approves with extend=1 to raise a running job's ceiling", async () => {
    const f = mockFetch(200, { id: 3, state: "running" });
    await approve(3, true);
    expect(f.mock.calls[0][0]).toBe("/api/jobs/3/approve?extend=1");
    expect(f.mock.calls[0][1].method).toBe("POST");
  });
  it("sends the account the person was shown, so a moved job is refused", async () => {
    const f = mockFetch(200, { id: 3, state: "queued" });
    await approve(3, false, "modal-b");
    expect(f.mock.calls[0][0]).toBe("/api/jobs/3/approve?target=modal-b");
  });
  it("sends extend and the account together", async () => {
    const f = mockFetch(200, { id: 3, state: "running" });
    await approve(3, true, "modal-b");
    expect(f.mock.calls[0][0]).toBe("/api/jobs/3/approve?extend=1&target=modal-b");
  });
  it("sends the account and a raised --max-cost together", async () => {
    const f = mockFetch(200, { id: 3, state: "running" });
    await approve(3, true, "modal-b", 6.95);
    expect(f.mock.calls[0][0]).toBe("/api/jobs/3/approve?extend=1&target=modal-b&max_cost=6.95");
  });
  it("posts without a body for reject", async () => {
    const f = mockFetch(200, { id: 3, state: "cancelled" });
    await reject(3);
    expect(f.mock.calls[0][0]).toBe("/api/jobs/3/reject");
    expect(f.mock.calls[0][1].method).toBe("POST");
  });
});

describe("followLog", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("tracks the latest offset from each message and reconnects from it after an error", () => {
    const onText = vi.fn();
    const onEnd = vi.fn();
    const close = followLog(1, 10, onText, onEnd);

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe("/api/jobs/1/logs?follow=1&offset=10");

    FakeEventSource.instances[0].emit({ text: "a", offset: 20 });
    expect(onText).toHaveBeenCalledWith("a");
    FakeEventSource.instances[0].emit({ text: "b", offset: 26 });
    expect(onText).toHaveBeenCalledWith("b");

    FakeEventSource.instances[0].onerror?.(new Event("error"));
    expect(FakeEventSource.instances[0].closed).toBe(true);
    // Doesn't reconnect immediately (and never to the EventSource's own auto-retry, which would
    // replay from offset=10 and duplicate "a" and "b").
    expect(FakeEventSource.instances).toHaveLength(1);

    vi.advanceTimersByTime(2000);

    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.instances[1].url).toBe("/api/jobs/1/logs?follow=1&offset=26");
    expect(onEnd).not.toHaveBeenCalled();

    close();
  });

  it("stops reconnecting once close() is called mid-delay", () => {
    const close = followLog(1, 0, vi.fn(), vi.fn());
    FakeEventSource.instances[0].onerror?.(new Event("error"));

    close();
    vi.advanceTimersByTime(10000);

    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it("stops reconnecting and doesn't call onEnd's close twice after the 'end' event", () => {
    const onEnd = vi.fn();
    followLog(1, 0, vi.fn(), onEnd);

    FakeEventSource.instances[0].fireEnd();

    expect(onEnd).toHaveBeenCalledTimes(1);
    expect(FakeEventSource.instances[0].closed).toBe(true);

    vi.advanceTimersByTime(10000);
    expect(FakeEventSource.instances).toHaveLength(1);
  });
});
