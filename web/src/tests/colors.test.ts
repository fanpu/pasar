import { describe, expect, it } from "vitest";
import { assignTagColors, jobColor, mutedJobColor, JOB_COLORS } from "../lib/colors";

describe("colors", () => {
  it("untagged jobs are coloured by id, cycling through all ten colours", () => {
    expect(JOB_COLORS).toHaveLength(10);
    expect(new Set(JOB_COLORS).size).toBe(10);
    expect(jobColor({ id: 42, tags: [] })).toBe(JOB_COLORS[2]);
    expect(jobColor({ id: 45, tags: [] })).toBe(JOB_COLORS[5]);
    expect(jobColor({ id: 52, tags: [] })).toBe(JOB_COLORS[2]);
  });

  it("tags on screen together get different colours, oldest first", () => {
    const tags = ["heads", "seedsmany", "gdnseeds"];
    const hashed = new Set(tags.map((t) => jobColor({ id: 1, tags: [t] })));
    expect(hashed.size).toBeLessThan(3); // plain hashing collides for these three
    const jobs = tags.map((t, i) => ({ id: i + 1, tags: [t] }));
    assignTagColors(jobs);
    expect(new Set(jobs.map(jobColor)).size).toBe(3);
    const oldest = jobColor(jobs[0]);
    assignTagColors(jobs.slice(0, 1));
    expect(jobColor(jobs[0])).toBe(oldest); // the oldest tag keeps its colour
    assignTagColors([]);
  });

  it("more tags than colours wrap around instead of looping forever", () => {
    const jobs = Array.from({ length: 25 }, (_, i) => ({ id: i, tags: [`t${i}`] }));
    assignTagColors(jobs);
    expect(new Set(jobs.slice(0, 10).map(jobColor)).size).toBe(10);
    assignTagColors([]);
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
