import { describe, expect, it } from "vitest";
import { ago, dur, fmtGib, gib, GIB, hm, isOom, reasonLabel } from "../lib/format";
import { jobColor, JOB_COLORS } from "../lib/colors";

describe("format", () => {
  it("gib", () => {
    expect(gib(24 * GIB)).toBe("24.0");
    expect(gib(31.24 * GIB)).toBe("31.2");
    expect(fmtGib(3.5 * GIB)).toBe("3.5 GiB");
    expect(fmtGib(null)).toBe("–");
  });
  it("dur", () => {
    expect(dur(0)).toBe("0s");
    expect(dur(-5)).toBe("0s");
    expect(dur(45)).toBe("45s");
    expect(dur(60)).toBe("1m");
    expect(dur(12 * 60 + 59)).toBe("12m");
    expect(dur(3600)).toBe("1h00");
    expect(dur(2 * 3600 + 5 * 60)).toBe("2h05");
    expect(dur(47 * 3600 + 59 * 60)).toBe("47h59");
    expect(dur(3 * 86400 + 4 * 3600 + 30 * 60)).toBe("3d4h");
  });
  it("hm and ago (TZ=UTC)", () => {
    const ts = Date.UTC(2026, 8, 21, 14, 26) / 1000;
    expect(hm(ts)).toBe("14:26");
    expect(hm(ts - 14 * 3600 - 26 * 60)).toBe("00:00");
    expect(ago(ts - 30, ts)).toBe("just now");
    expect(ago(ts - 6 * 60, ts)).toBe("6m ago");
  });
  it("reasons", () => {
    expect(reasonLabel("gpu_oom")).toBe("GPU out of memory");
    expect(reasonLabel("lost")).toBe("went missing");
    expect(reasonLabel(null)).toBe("");
    expect(reasonLabel("weird")).toBe("weird");
    expect(["oom", "gpu_oom", "kernel_oom"].every(isOom)).toBe(true);
    expect(isOom("exit")).toBe(false);
    expect(isOom(null)).toBe(false);
  });
  it("job colours follow the id", () => {
    expect(JOB_COLORS).toEqual(["#3b8fd9", "#d9577f", "#8a63d2", "#23a47a", "#c9761f"]);
    expect(jobColor(42)).toBe("#8a63d2");
    expect(jobColor(45)).toBe("#3b8fd9");
  });
});
