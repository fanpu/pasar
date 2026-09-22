import { describe, expect, it } from "vitest";
import { jobColor, mutedJobColor, JOB_COLORS } from "../lib/colors";

describe("colors", () => {
  it("untagged jobs keep the old per-id colour", () => {
    expect(JOB_COLORS).toEqual(["#3b8fd9", "#d9577f", "#8a63d2", "#23a47a", "#c9761f"]);
    expect(jobColor({ id: 42, tags: [] })).toBe(JOB_COLORS[((42 % 5) + 5) % 5]);
    expect(jobColor({ id: 45, tags: [] })).toBe(JOB_COLORS[((45 % 5) + 5) % 5]);
  });

  it("jobs sharing a first tag share a colour, regardless of id", () => {
    const a = jobColor({ id: 1, tags: ["sweep-a"] });
    const b = jobColor({ id: 999, tags: ["sweep-a"] });
    expect(a).toBe(b);
    expect(JOB_COLORS).toContain(a);
  });

  it("only the first tag decides the colour", () => {
    const a = jobColor({ id: 1, tags: ["sweep-a", "gpu-large"] });
    const b = jobColor({ id: 2, tags: ["sweep-a", "gpu-small"] });
    expect(a).toBe(b);
  });

  it("different tags spread across the palette", () => {
    const tags = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"];
    const colors = new Set(tags.map((t) => jobColor({ id: 1, tags: [t] })));
    expect(colors.size).toBeGreaterThan(1);
  });

  it("is a pure hash: same tag always gives the same colour, with no hidden state", () => {
    expect(jobColor({ id: 7, tags: ["repeat-me"] })).toBe(jobColor({ id: 12345, tags: ["repeat-me"] }));
  });

  it("muted colour is a translucent tint and border of the job's own colour", () => {
    const c = jobColor({ id: 3, tags: [] });
    const m = mutedJobColor({ id: 3, tags: [] });
    expect(m.fill).toBe(`${c}17`);
    expect(m.border).toBe(`${c}66`);
  });
});
