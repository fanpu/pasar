import { getGpu } from "./api";
import { assignTagColors } from "./colors";
import type { GpuSeries, Snapshot } from "./types";

type Listener = (prev: Snapshot | null, next: Snapshot) => void;

export interface LiveOptions {
  url?: string;
  gpuEveryMs?: number;
  retryMs?: number;
  eventSource?: typeof EventSource;
  fetchGpu?: () => Promise<GpuSeries>;
}

/** Live connection to `/api/stream`: the current snapshot plus a slow-polled GPU series. Both
 * are replaced wholesale on every update, so `$state.raw` avoids proxying them deeply. */
export class Live {
  snapshot = $state.raw<Snapshot | null>(null);
  connected = $state(false);
  gpu = $state.raw<GpuSeries | null>(null);

  #url: string;
  #gpuEveryMs: number;
  #retryMs: number;
  #EventSourceCtor: typeof EventSource;
  #fetchGpu: () => Promise<GpuSeries>;
  #source: EventSource | null = null;
  #retryTimer: ReturnType<typeof setTimeout> | null = null;
  #gpuTimer: ReturnType<typeof setInterval> | null = null;
  #listeners = new Set<Listener>();

  constructor(opts?: LiveOptions) {
    this.#url = opts?.url ?? "/api/stream";
    this.#gpuEveryMs = opts?.gpuEveryMs ?? 15000;
    this.#retryMs = opts?.retryMs ?? 3000;
    this.#EventSourceCtor = opts?.eventSource ?? EventSource;
    this.#fetchGpu = opts?.fetchGpu ?? (() => getGpu());
  }

  start(): void {
    this.#connect();
    this.#pollGpu();
    this.#gpuTimer = setInterval(() => this.#pollGpu(), this.#gpuEveryMs);
  }

  stop(): void {
    this.#source?.close();
    this.#source = null;
    if (this.#retryTimer !== null) clearTimeout(this.#retryTimer);
    this.#retryTimer = null;
    if (this.#gpuTimer !== null) clearInterval(this.#gpuTimer);
    this.#gpuTimer = null;
  }

  onSnapshot(fn: Listener): () => void {
    this.#listeners.add(fn);
    return () => this.#listeners.delete(fn);
  }

  #connect(): void {
    const es = new this.#EventSourceCtor(this.#url);
    es.onmessage = (e: MessageEvent) => {
      const next = JSON.parse(e.data) as Snapshot;
      const prev = this.snapshot;
      for (const fn of this.#listeners) fn(prev, next);
      assignTagColors(next.jobs);
      this.snapshot = next;
      this.connected = true;
    };
    es.onerror = () => {
      this.connected = false;
      es.close();
      this.#retryTimer = setTimeout(() => this.#connect(), this.#retryMs);
    };
    this.#source = es;
  }

  #pollGpu(): void {
    this.#fetchGpu()
      .then((g) => { this.gpu = g; })
      .catch(() => {});
  }
}
