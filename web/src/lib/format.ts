export const GIB = 1024 ** 3;

export function gib(bytes: number, digits = 1): string {
  return (bytes / GIB).toFixed(digits);
}

export function fmtGib(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "–";
  return `${gib(bytes)} GiB`;
}

export function dur(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 48 * 3600) {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return `${h}h${String(m).padStart(2, "0")}`;
  }
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  return `${d}d${h}h`;
}

export function hm(ts: number): string {
  const d = new Date(ts * 1000);
  const h = String(d.getHours()).padStart(2, "0");
  const m = String(d.getMinutes()).padStart(2, "0");
  return `${h}:${m}`;
}

export function ago(ts: number, now: number): string {
  const diff = now - ts;
  if (diff < 60) return "just now";
  return `${dur(diff)} ago`;
}

const REASON_LABELS: Record<string, string> = {
  oom: "over its memory limit",
  gpu_oom: "GPU out of memory",
  kernel_oom: "out of memory (kernel)",
  gpu_xid: "GPU error",
  signal: "killed by a signal",
  exit: "crashed",
  lost: "went missing",
  launch_error: "couldn't start",
  cancelled: "cancelled",
  preempted: "preempted",
};

export function reasonLabel(reason: string | null): string {
  if (reason === null) return "";
  return REASON_LABELS[reason] ?? reason;
}

const OOM_REASONS = new Set(["oom", "gpu_oom", "kernel_oom"]);

export function isOom(reason: string | null): boolean {
  return reason !== null && OOM_REASONS.has(reason);
}
