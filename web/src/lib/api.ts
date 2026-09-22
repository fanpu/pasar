import type {
  GpuSeries, JobDetail, JobEvent, JobView, MascotManifest, MetricSummary, RestartBody, Series,
  SubmitBody,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function detailMessage(res: Response): Promise<string> {
  try {
    const data = await res.json();
    const detail = data?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const msgs = detail.map((d: { msg?: unknown }) => d?.msg).filter((m): m is string => typeof m === "string");
      if (msgs.length) return msgs.join("; ");
    }
  } catch {
    // body wasn't JSON (or was empty) — fall through to the generic message below
  }
  return `HTTP ${res.status}`;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const res = await fetch(path, init);
  if (!res.ok) throw new ApiError(res.status, await detailMessage(res));
  return (await res.json()) as T;
}

export function getJob(id: number): Promise<JobDetail> {
  return request("GET", `/api/jobs/${id}`);
}
export function getEvents(id: number): Promise<JobEvent[]> {
  return request("GET", `/api/jobs/${id}/events`);
}
export function getUsage(id: number): Promise<Series> {
  return request("GET", `/api/jobs/${id}/usage`);
}
export function getMetrics(id: number): Promise<MetricSummary[]> {
  return request("GET", `/api/jobs/${id}/metrics`);
}
export function getGpu(minutes = 30): Promise<GpuSeries> {
  return request("GET", `/api/gpu?minutes=${minutes}`);
}
export function getMascot(): Promise<MascotManifest> {
  return request("GET", "/api/mascot");
}
export function getLog(id: number, offset = 0): Promise<{ text: string; offset: number }> {
  return request("GET", `/api/jobs/${id}/logs?offset=${offset}`);
}
export function cancelJob(id: number): Promise<JobView> {
  return request("POST", `/api/jobs/${id}/cancel`);
}
export function setBid(id: number, bid: number): Promise<JobView> {
  return request("PATCH", `/api/jobs/${id}`, { bid });
}
export function restartJob(id: number, body?: RestartBody): Promise<JobView> {
  return request("POST", `/api/jobs/${id}/restart`, body);
}
export function submitJob(body: SubmitBody): Promise<JobView> {
  return request("POST", "/api/jobs", body);
}

export function followLog(
  id: number,
  offset: number,
  onText: (text: string) => void,
  onEnd: () => void,
): () => void {
  const es = new EventSource(`/api/jobs/${id}/logs?follow=1&offset=${offset}`);
  es.onmessage = (e) => onText(JSON.parse(e.data).text);
  es.addEventListener("end", () => {
    onEnd();
    es.close();
  });
  return () => es.close();
}
