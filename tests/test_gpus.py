"""Multi-GPU monitoring: device selection, per-process attribution, storage and summary."""

import time
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from pyrometer import _gpu
from pyrometer.server import auth, db
from pyrometer.server.app import app


class FakeNvml:
    NVML_TEMPERATURE_GPU = 0
    NVML_CLOCK_SM = 1
    NVML_CLOCK_MEM = 2

    def __init__(self, n=4, procs=None, proc_util=None):
        self.n = n
        self.procs = procs or {}  # index -> [NS(pid, usedGpuMemory)]
        self.proc_util = proc_util  # index -> [NS(pid, smUtil, timeStamp)] or None -> unsupported

    def nvmlDeviceGetCount(self):
        return self.n

    def nvmlDeviceGetHandleByIndex(self, i):
        if i >= self.n:
            raise RuntimeError("invalid")
        return i

    def nvmlDeviceGetHandleByUUID(self, u):
        if not u.startswith("GPU-"):
            raise RuntimeError("invalid")
        return int(u.split("-")[1])

    def nvmlDeviceGetMemoryInfo(self, h):
        return NS(used=1000 * (h + 1), total=10000)

    def nvmlDeviceGetUtilizationRates(self, h):
        return NS(gpu=50 + h)

    def nvmlDeviceGetTemperature(self, h, _k):
        return 60 + h

    def nvmlDeviceGetPowerUsage(self, h):
        return 200000

    def nvmlDeviceGetFanSpeed(self, h):
        raise RuntimeError("n/a")

    def nvmlDeviceGetClockInfo(self, h, k):
        return 1000

    def nvmlDeviceGetComputeRunningProcesses(self, h):
        return self.procs.get(h, [])

    def nvmlDeviceGetProcessUtilization(self, h, _ts):
        if self.proc_util is None:
            raise RuntimeError("not supported")
        return self.proc_util.get(h, [])


def test_all_gpus_visible_by_default():
    assert [p for p, _ in _gpu._resolve_handles(FakeNvml(3), None)] == [0, 1, 2]


def test_cuda_visible_devices_maps_to_ordinals():
    handles = _gpu._resolve_handles(FakeNvml(4), "2,3")
    assert handles == [(0, 2), (1, 3)]
    assert _gpu._resolve_handles(FakeNvml(4), "GPU-1,GPU-3") == [(0, 1), (1, 3)]
    assert _gpu._resolve_handles(FakeNvml(4), "") == []
    assert _gpu._resolve_handles(FakeNvml(2), "1,7,0") == [(0, 1)]  # stops at first invalid, like CUDA


def test_process_attribution_exact_and_children():
    nv = FakeNvml(
        2,
        procs={0: [NS(pid=10, usedGpuMemory=300), NS(pid=11, usedGpuMemory=100), NS(pid=99, usedGpuMemory=600)]},
        proc_util={0: [NS(pid=10, smUtil=20, timeStamp=1), NS(pid=10, smUtil=30, timeStamp=2),
                       NS(pid=11, smUtil=5, timeStamp=2), NS(pid=99, smUtil=40, timeStamp=2)]},
    )
    snap = _gpu.sample_gpu(nv, 0, 0, {10, 11})
    assert snap["process_memory_used"] == 400
    assert snap["process_exclusive"] is False
    assert snap["attributed_utilization"] == 35.0  # latest sample of each of our pids, summed
    assert snap["gpu_utilization"] == 50.0 and snap["memory_used"] == 1000
    assert snap["fan_speed"] is None
    # A GPU we do not run on: device numbers only, process fields null.
    other = _gpu.sample_gpu(nv, 1, 1, {10, 11})
    assert other["gpu_index"] == 1
    assert other["process_memory_used"] is None and other["attributed_utilization"] is None


def test_fallback_when_process_utilization_unsupported():
    nv = FakeNvml(1, procs={0: [NS(pid=10, usedGpuMemory=250), NS(pid=99, usedGpuMemory=750)]})
    snap = _gpu.sample_gpu(nv, 0, 0, {10})
    assert snap["attributed_utilization"] == pytest.approx(50 * 0.25)
    solo = _gpu.sample_gpu(FakeNvml(1, procs={0: [NS(pid=10, usedGpuMemory=1)]}), 0, 0, {10})
    assert solo["process_exclusive"] is True and solo["attributed_utilization"] == 50.0


def test_sample_all_shares_timestamp_and_skips_broken_devices():
    nv = FakeNvml(3)
    broken = nv.nvmlDeviceGetMemoryInfo
    nv.nvmlDeviceGetMemoryInfo = lambda h: (_ for _ in ()).throw(RuntimeError()) if h == 1 else broken(h)
    snaps = _gpu.sample_all(nv, [(0, 0), (1, 1), (2, 2)], own_pid=1)
    assert [s["gpu_index"] for s in snaps] == [0, 2]
    assert len({s["timestamp"] for s in snaps}) == 1


@pytest.fixture
def server(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("PYROMETER_AUTH_FILE", str(tmp_path / "auth.json"))
    auth._cache = None
    auth._setup_token = None
    key = auth.create_key("sdk")[1]
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def post(server, rid, ts, gpu, **kw):
    body = {"timestamp": ts, "gpu_index": gpu, "gpu_utilization": 10.0, "memory_used": 100, "memory_total": 1000, **kw}
    assert server.post(f"/runs/{rid}/system", json=body).status_code in (200, 201)


def test_snapshots_store_gpu_index_and_old_rows_are_gpu0(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    post(server, rid, 1.0, 1)
    with db._lock:  # legacy row written before the column existed
        db.get_conn().execute(
            "INSERT INTO system_snapshots (run_id, timestamp, gpu_utilization) VALUES (?, 2.0, 5.0)", [rid]
        )
    rows = server.get(f"/runs/{rid}/system").json()["data"]
    assert [r["gpu_index"] for r in rows] == [1, 0]
    assert db.db_gpu_count(rid) == 2


def test_summary_uses_process_numbers_over_all_gpus(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    now = time.time()
    for t in (now, now + 2):
        post(server, rid, t, 0, gpu_utilization=90.0, memory_used=500,
             process_memory_used=300, attributed_utilization=80.0)
        post(server, rid, t, 1, gpu_utilization=100.0, memory_used=700,
             process_memory_used=200, attributed_utilization=60.0)
    s = db.db_compute_run_summary(rid)
    assert s["avg_gpu_utilization"] == 70.0
    assert s["peak_process_memory_bytes"] == 500  # summed across GPUs per poll


def test_summary_idle_gpu_without_process_rows_does_not_dilute(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    post(server, rid, 1.0, 0, gpu_utilization=90.0, attributed_utilization=80.0, process_memory_used=300)
    post(server, rid, 1.0, 1, gpu_utilization=0.0)
    assert db.db_compute_run_summary(rid)["avg_gpu_utilization"] == 80.0


def test_summary_device_fallback_without_process_data(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    post(server, rid, 1.0, 0, gpu_utilization=40.0, memory_used=100)
    post(server, rid, 1.0, 1, gpu_utilization=60.0, memory_used=200)
    s = db.db_compute_run_summary(rid)
    assert s["avg_gpu_utilization"] == 50.0 and s["peak_process_memory_bytes"] == 300
