import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, cancelJob, setBid, submitJob } from "../lib/api";

function mockFetch(status: number, body: unknown) {
  const f = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", f);
  return f;
}
afterEach(() => vi.unstubAllGlobals());

describe("api", () => {
  it("sends JSON and returns the job", async () => {
    const f = mockFetch(200, { id: 3, bid: 1500 });
    expect((await setBid(3, 1500)).bid).toBe(1500);
    const [url, init] = f.mock.calls[0];
    expect(url).toBe("/api/jobs/3");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body)).toEqual({ bid: 1500 });
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
});
