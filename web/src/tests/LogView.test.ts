import { cleanup, render, waitFor } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import LogView from "../components/LogView.svelte";
import * as api from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, getLog: vi.fn(), followLog: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.mocked(api.getLog).mockReset();
  vi.mocked(api.followLog).mockReset();
});

describe("LogView", () => {
  it("loads the log in chunks until an empty chunk, splitting into classified lines", async () => {
    vi.mocked(api.getLog)
      .mockResolvedValueOnce({ text: "a\n──── attempt 2 ────\nValueError: x\n", offset: 40 })
      .mockResolvedValueOnce({ text: "", offset: 40 });

    const { container } = render(LogView, { id: 1, follow: false });

    await waitFor(() => expect(container.querySelectorAll(".ln").length).toBe(3));
    expect(container.querySelectorAll(".sep").length).toBe(1);
    expect(container.querySelectorAll(".err").length).toBe(1);
    expect(container.querySelector(".sep")?.textContent).toBe("──── attempt 2 ────");
    expect(container.querySelector(".err")?.textContent).toBe("ValueError: x");
  });

  it("follows from the final offset once the backlog is drained, appends streamed text, and closes on unmount", async () => {
    vi.mocked(api.getLog)
      .mockResolvedValueOnce({ text: "line1\n", offset: 6 })
      .mockResolvedValueOnce({ text: "", offset: 6 });
    let onText!: (t: string) => void;
    const close = vi.fn();
    vi.mocked(api.followLog).mockImplementation((_id, offset, onTextCb) => {
      expect(offset).toBe(6);
      onText = onTextCb;
      return close;
    });

    const { container, unmount } = render(LogView, { id: 1, follow: true });
    await waitFor(() => expect(api.followLog).toHaveBeenCalledWith(1, 6, expect.any(Function), expect.any(Function)));

    onText("line2\n");
    await waitFor(() => expect(container.textContent).toContain("line2"));

    unmount();
    expect(close).toHaveBeenCalled();
  });

  it("does not follow when follow is false", async () => {
    vi.mocked(api.getLog)
      .mockResolvedValueOnce({ text: "line1\n", offset: 6 })
      .mockResolvedValueOnce({ text: "", offset: 6 });

    render(LogView, { id: 1, follow: false });
    await waitFor(() => expect(api.getLog).toHaveBeenCalledTimes(2));

    expect(api.followLog).not.toHaveBeenCalled();
  });

  it("shows 'No output yet.' for an empty log", async () => {
    vi.mocked(api.getLog).mockResolvedValueOnce({ text: "", offset: 0 });

    const { findByText } = render(LogView, { id: 1, follow: false });

    expect(await findByText("No output yet.")).toBeInTheDocument();
  });

  it("keeps at most the last 5000 lines", async () => {
    const total = 5005;
    const text = Array.from({ length: total }, (_, i) => `line${i}`).join("\n") + "\n";
    vi.mocked(api.getLog)
      .mockResolvedValueOnce({ text, offset: text.length })
      .mockResolvedValueOnce({ text: "", offset: text.length });

    const { container } = render(LogView, { id: 1, follow: false });

    await waitFor(() => expect(container.querySelectorAll(".ln").length).toBe(5000));
    const texts = Array.from(container.querySelectorAll(".ln")).map((el) => el.textContent);
    expect(texts[0]).toBe("line5");
    expect(texts[texts.length - 1]).toBe(`line${total - 1}`);
  });

  it("shows an inline error (not 'No output yet.') on a getLog failure and retries, resuming from the last offset without duplicating text", async () => {
    vi.useFakeTimers();
    try {
      const getLog = vi.mocked(api.getLog);
      getLog
        .mockResolvedValueOnce({ text: "line1\n", offset: 6 })
        .mockRejectedValueOnce(new Error("network down"))
        .mockResolvedValueOnce({ text: "line2\n", offset: 12 })
        .mockResolvedValueOnce({ text: "", offset: 12 });

      const { container } = render(LogView, { id: 1, follow: false });

      await vi.advanceTimersByTimeAsync(0);
      expect(getLog).toHaveBeenCalledTimes(2);
      expect(container.textContent).toContain("Couldn't load the log: network down");
      expect(container.textContent).toContain("line1");

      await vi.advanceTimersByTimeAsync(3000);

      expect(getLog).toHaveBeenCalledTimes(4);
      // Retries from the offset after the last *successful* chunk (6), not from 0 — so "line1"
      // is never re-requested or re-appended.
      expect(getLog.mock.calls[2][1]).toBe(6);
      expect(container.textContent).not.toContain("Couldn't load the log");
      const texts = Array.from(container.querySelectorAll(".ln")).map((el) => el.textContent);
      expect(texts).toEqual(["line1", "line2"]);
    } finally {
      vi.useRealTimers();
    }
  });
});
