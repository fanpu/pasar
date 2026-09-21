import threading

import httpx

from pasar.db import Store
from pasar.metrics import MetricsRecorder, Prometheus
from pasar.models import JobSpec

SERIES = {
    "avg(DCGM_FI_DEV_POWER_USAGE)": [[0, "50"], [15, "70"]],
    "max(DCGM_FI_DEV_GPU_TEMP)": [[0, "60"], [15, "66"]],
    "avg(DCGM_FI_DEV_GPU_UTIL)": [[0, "80"], [15, "100"]],
    "sum(DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION)": [[0, "1000000"], [15, "1600000"]],
}


def transport():
    def handler(request: httpx.Request):
        q = request.url.params["query"]
        values = SERIES.get(q)
        result = [{"metric": {}, "values": values}] if values else []
        return httpx.Response(200, json={"status": "success", "data": {"result": result}})
    return httpx.MockTransport(handler)


def prom():
    return Prometheus("http://prom", client=httpx.Client(transport=transport()))


def test_summarize():
    s = prom().summarize(0, 15)
    assert s["power_w"] == {"avg": 60.0, "max": 70.0}
    assert s["temp_c"]["max"] == 66.0
    assert s["util_pct"]["avg"] == 90.0
    assert s["energy_j"] == {"total": 600.0}


def test_unreachable_prometheus_gives_empty_results():
    def boom(request):
        raise httpx.ConnectError("down")
    p = Prometheus("http://prom", client=httpx.Client(transport=httpx.MockTransport(boom)))
    assert p.summarize(0, 15) == {}
    assert p.gpu_series(30, now=100) == {"power_w": [], "temp_c": [], "util_pct": []}


def test_recorder_stores_summaries(tmp_path):
    store = Store(tmp_path / "db")
    store.insert_job(JobSpec("x", 1, "/"), 1, 0, None)
    rec = MetricsRecorder(prom(), store)
    t = rec.record(1, 1, 0, 15)
    t.join(timeout=5)
    metrics = {m["metric"]: m for m in store.metric_summaries(1)}
    assert metrics["power_w"]["avg"] == 60.0 and metrics["energy_j"]["total"] == 600.0
    assert isinstance(t, threading.Thread)
