"""GPU-wide metrics from Prometheus (dcgm-exporter): live series and per-attempt summaries."""

import logging
import threading

import httpx

log = logging.getLogger(__name__)

POWER = "avg(DCGM_FI_DEV_POWER_USAGE)"
TEMP = "max(DCGM_FI_DEV_GPU_TEMP)"
UTIL = "avg(DCGM_FI_DEV_GPU_UTIL)"
ENERGY = "sum(DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION)"  # millijoules, monotonic


class Prometheus:
    def __init__(self, url: str, client: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.client = client or httpx.Client(timeout=5)

    def range(self, query: str, start: float, end: float, step: float) -> list[tuple[float, float]]:
        try:
            r = self.client.get(f"{self.url}/api/v1/query_range",
                                params={"query": query, "start": start, "end": end, "step": step})
            r.raise_for_status()
            result = r.json()["data"]["result"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.warning("prometheus query failed (%s): %s", query, e)
            return []
        if not result:
            return []
        return [(float(t), float(v)) for t, v in result[0]["values"]]

    def summarize(self, start: float, end: float) -> dict[str, dict]:
        step = max(5.0, (end - start) / 500)
        out: dict[str, dict] = {}
        for key, query in (("power_w", POWER), ("temp_c", TEMP), ("util_pct", UTIL)):
            vals = [v for _, v in self.range(query, start, end, step)]
            if vals:
                out[key] = {"avg": sum(vals) / len(vals), "max": max(vals)}
        energy = self.range(ENERGY, start, end, step)
        if len(energy) >= 2:
            out["energy_j"] = {"total": (energy[-1][1] - energy[0][1]) / 1000}
        return out

    def gpu_series(self, minutes: int, now: float) -> dict[str, list[list[float]]]:
        start = now - minutes * 60
        step = max(5.0, minutes * 60 / 240)
        return {key: [[t, v] for t, v in self.range(q, start, now, step)]
                for key, q in (("power_w", POWER), ("temp_c", TEMP), ("util_pct", UTIL))}


class MetricsRecorder:
    """Stores GPU metric summaries for each finished attempt, off the scheduler's thread."""

    def __init__(self, prom: Prometheus, store):
        self.prom = prom
        self.store = store

    def record(self, job_id: int, attempt: int, start: float, end: float) -> threading.Thread:
        def work():
            for metric, vals in self.prom.summarize(start, end).items():
                self.store.set_metric_summary(job_id, attempt, metric, vals.get("avg"),
                                              vals.get("max"), vals.get("total"))
        t = threading.Thread(target=work, name=f"metrics-{job_id}-{attempt}", daemon=True)
        t.start()
        return t
