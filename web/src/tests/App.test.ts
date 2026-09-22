import { render, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App.svelte";
import * as api from "../lib/api";
import { mascot } from "../lib/mascot.svelte";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, getGpu: vi.fn(), getMascot: vi.fn() };
});

class FakeEventSource {
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  constructor(public url: string) {}
  close(): void {}
}

describe("App", () => {
  beforeEach(() => {
    vi.mocked(api.getGpu).mockReset().mockResolvedValue({ power_w: [], temp_c: [], util_pct: [] });
    vi.mocked(api.getMascot).mockReset();
    vi.stubGlobal("EventSource", FakeEventSource);
    history.pushState({}, "", "/");
    // Reset the shared mascot singleton so an earlier test's loaded manifest doesn't leak in.
    mascot.manifest = {};
    mascot.loaded = false;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("re-picks the header mascot once the manifest loads, even though the mood state hasn't changed", async () => {
    let resolveManifest!: (m: Record<string, string[]>) => void;
    vi.mocked(api.getMascot).mockReturnValue(
      new Promise((resolve) => {
        resolveManifest = resolve;
      }),
    );
    const { container } = render(App);

    // No snapshot has arrived yet, so the mood stays "thinking" the whole time: before the
    // manifest resolves, the header must be showing the built-in art for that state.
    const imgBefore = container.querySelector<HTMLImageElement>(".mascot")!;
    expect(imgBefore.src).toContain("/mascot/builtin/thinking.png");

    resolveManifest({ thinking: ["/mascot/thinking-1.png"] });
    await waitFor(() => {
      const img = container.querySelector<HTMLImageElement>(".mascot")!;
      expect(img.src).toContain("/mascot/thinking-1.png");
    });
  });
});
