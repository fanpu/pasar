import { describe, expect, it } from "vitest";
import { ago, dur, fmtGib, gib, GIB, hm, isOom, metric, reasonLabel } from "../lib/format";
import { resultSize } from "../lib/cloud";

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
    // A cloud job's reason reads as words too, never as its raw code.
    for (const code of ["account_unusable", "time_limit", "job_cap", "cloud_preempted",
      "price_rose", "pause_limit", "target_gone", "launch_error", "moved", "out_of_credit",
      "approval_expired"]) {
      expect(reasonLabel(code)).not.toBe(code);
      expect(reasonLabel(code)).not.toMatch(/_/);
    }
    expect(reasonLabel("account_unusable")).toBe("its account refused to start it");
    expect(["oom", "gpu_oom", "kernel_oom"].every(isOom)).toBe(true);
    expect(isOom("exit")).toBe(false);
    expect(isOom(null)).toBe(false);
  });
});

describe("metric", () => {
  it("keeps three decimals, and falls back to exponent for vanishingly small values", () => {
    expect(metric(0.412)).toBe("0.412");
    expect(metric(12)).toBe("12.000");
    expect(metric(0)).toBe("0.000");
    expect(metric(0.00004)).toBe("4.0e-5");
    expect(metric(-0.00004)).toBe("-4.0e-5");
  });
});

describe("resultSize", () => {
  it("keeps GiB for real results and says small ones in MiB or KiB, never 0.0 GiB", () => {
    expect(resultSize(1.2 * GIB)).toBe("1.2 GiB");
    expect(resultSize(0.1 * GIB)).toBe("0.1 GiB");
    expect(resultSize(12.5 * 1024 ** 2)).toBe("12.5 MiB");
    expect(resultSize(4096)).toBe("4 KiB");
    expect(resultSize(10)).toBe("1 KiB");
    expect(resultSize(null)).toBe("–");
  });
});
