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


# --- P3-E: run-level inventory, MIG, per-GPU summary -------------------------------------------------

class InvNvml(FakeNvml):
    """FakeNvml plus inventory calls; GPU 1 is MIG-enabled with two instances."""

    NVML_DEVICE_MIG_ENABLE = 1

    def nvmlSystemGetDriverVersion(self):
        return b"535.54"

    def nvmlDeviceGetUUID(self, h):
        return f"MIG-{h}" if isinstance(h, str) else f"GPU-{h}"

    def nvmlDeviceGetName(self, h):
        return "Fake MIG 1g.5gb" if isinstance(h, str) else f"Fake GPU {h}"

    def nvmlDeviceIsMigDeviceHandle(self, h):
        return isinstance(h, str)

    def nvmlDeviceGetMigMode(self, h):
        return (1 if h == 1 else 0, 0)

    def nvmlDeviceGetMaxMigDeviceCount(self, h):
        return 3

    def nvmlDeviceGetMigDeviceHandleByIndex(self, h, i):
        if i == 2:
            raise RuntimeError("no such instance")
        return f"{h}-{i}"

    def nvmlDeviceGetMemoryInfo(self, h):
        if isinstance(h, str):
            return NS(used=10, total=5 * 2**30)
        return super().nvmlDeviceGetMemoryInfo(h)

    def nvmlDeviceGetUtilizationRates(self, h):
        if isinstance(h, str):
            raise RuntimeError("not supported on MIG")
        return super().nvmlDeviceGetUtilizationRates(h)

    def nvmlDeviceGetPowerUsage(self, h):
        if isinstance(h, str):
            raise RuntimeError("not supported on MIG")
        return super().nvmlDeviceGetPowerUsage(h)

    def nvmlDeviceGetTemperature(self, h, k):
        if isinstance(h, str):
            raise RuntimeError("not supported on MIG")
        return super().nvmlDeviceGetTemperature(h, k)


def test_inventory_lists_all_gpus_with_mig():
    nv = InvNvml(3)
    inv = _gpu.gpu_inventory(nv, _gpu._resolve_handles(nv, None))
    assert [g["index"] for g in inv] == [0, 1, 2]
    assert {g["driver_version"] for g in inv} == {"535.54"}
    assert [g["mig_mode"] for g in inv] == ["disabled", "enabled", "disabled"]
    assert inv[0]["uuid"] == "GPU-0" and inv[0]["memory_total"] == 10000
    mig = inv[1]["mig_devices"]
    assert [m["index"] for m in mig] == [0, 1] and mig[0]["memory_total"] == 5 * 2**30
    assert all(m["utilization"] is None for m in mig) and "MIG" in inv[1]["note"]


def test_inventory_mig_uuid_in_cuda_visible_devices():
    nv = InvNvml(2)
    nv.nvmlDeviceGetHandleByUUID = lambda u: "mig0" if u.startswith("MIG-") else int(u.split("-")[1])
    handles = _gpu._resolve_handles(nv, "MIG-abc,GPU-1")
    inv = _gpu.gpu_inventory(nv, handles)
    assert inv[0]["is_mig"] and inv[0]["mig_mode"] == "enabled" and inv[0]["memory_total"] == 5 * 2**30
    assert not inv[1]["is_mig"]


def test_mig_snapshot_has_null_utilization_but_memory():
    nv = InvNvml(1)
    snap = _gpu.sample_gpu(nv, "mig0", 0, {1})
    assert snap["memory_total"] == 5 * 2**30
    assert snap["gpu_utilization"] is None and snap["attributed_utilization"] is None
    assert snap["temperature"] is None and snap["power_draw"] is None and snap["attributed_power"] is None


def test_fingerprint_slow_reports_gpu_list_and_compat_fields(monkeypatch):
    import sys

    from pyrometer import _hardware

    nv = InvNvml(2)
    nv.nvmlInit = lambda: None
    monkeypatch.setitem(sys.modules, "pynvml", nv)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setitem(sys.modules, "torch", None)  # import torch -> ImportError
    info = _hardware.fingerprint_slow()
    assert info["gpu_count"] == 2 and info["driver_version"] == "535.54"
    assert info["gpu_model"] == "Fake GPU 0" and info["gpu_memory_total"] == 10000
    assert [g["name"] for g in info["gpus"]] == ["Fake GPU 0", "Fake GPU 1"]


def test_run_gpus_stored_and_in_llm_summary(server):
    gpus = [
        {"index": 0, "name": "A", "uuid": "GPU-0", "memory_total": 2**30, "driver_version": "535", "mig_mode": "disabled",
         "is_mig": False, "mig_devices": []},
        {"index": 1, "name": "B", "uuid": "GPU-1", "memory_total": 2**31, "driver_version": "535", "mig_mode": "enabled",
         "is_mig": False, "mig_devices": [{"index": 0, "uuid": "MIG-1", "name": "1g", "memory_total": 2**30, "utilization": None}]},
    ]
    rid = server.post("/runs", json={"gpu_model": "A"}).json()["data"]["run_id"]
    assert server.get(f"/runs/{rid}").json()["data"]["gpus"] == []
    r = server.patch(f"/runs/{rid}", json={"gpus": gpus, "gpu_count": 2, "driver_version": "535"})
    assert r.status_code == 200
    run = server.get(f"/runs/{rid}").json()["data"]
    assert run["gpus"] == gpus and run["gpu_count"] == 2 and run["driver_version"] == "535"
    text = server.get(f"/runs/{rid}/llm-summary").text
    assert "GPU 0: A (1.0 GB)" in text and "GPU 1: B (2.0 GB)" in text and "MIG 0: 1g" in text and "Driver: 535" in text


def test_summary_per_gpu_breakdown(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    for t in (1.0, 3.0):
        post(server, rid, t, 0, gpu_utilization=90.0, memory_used=500, process_memory_used=300, attributed_utilization=80.0)
        post(server, rid, t, 1, gpu_utilization=40.0, memory_used=700, process_memory_used=200, attributed_utilization=20.0)
    post(server, rid, 5.0, 1, gpu_utilization=None, memory_used=900, process_memory_used=250)
    s = server.get(f"/runs/{rid}/summary").json()["data"]
    g0, g1 = s["per_gpu"]
    assert g0["gpu_index"] == 0 and g0["avg_gpu_utilization"] == 80.0 and g0["peak_process_memory_bytes"] == 300
    assert g1["gpu_index"] == 1 and g1["avg_gpu_utilization"] == 20.0 and g1["peak_process_memory_bytes"] == 250
    assert g1["peak_memory_bytes"] == 900 and g1["samples"] == 3
    text = server.get(f"/runs/{rid}/llm-summary").text
    assert "GPU 1: avg util 20.0%" in text


def test_old_db_migrates_gpu_columns(tmp_path):
    import duckdb

    con = duckdb.connect(str(tmp_path / "old.duckdb"))
    db._init_schema(con)
    for col in ("gpus", "gpu_count", "driver_version"):  # a database from before this schema version
        con.execute(f"ALTER TABLE runs DROP COLUMN {col}")
    con.execute("ALTER TABLE run_summaries DROP COLUMN per_gpu")
    db._init_schema(con)
    assert {"gpus", "gpu_count", "driver_version"} <= {r[0] for r in con.execute("DESCRIBE runs").fetchall()}
    assert "per_gpu" in {r[0] for r in con.execute("DESCRIBE run_summaries").fetchall()}
    con.close()


def test_unsupported_power_does_not_drop_the_gpu():
    """Real bug found on a Quadro P1000 (no power reporting): the whole device used to be skipped."""
    nv = FakeNvml(1)
    nv.nvmlDeviceGetPowerUsage = lambda h: (_ for _ in ()).throw(RuntimeError("Not Supported"))
    snaps = _gpu.sample_all(nv, [(0, 0)], own_pid=1)
    assert len(snaps) == 1 and snaps[0]["power_draw"] is None and snaps[0]["attributed_power"] is None
    assert snaps[0]["gpu_utilization"] == 50.0 and snaps[0]["memory_used"] == 1000


def test_summary_has_no_grade_for_a_run_that_measured_nothing(server):
    rid = server.post("/runs", json={}).json()["data"]["run_id"]
    assert db.db_compute_run_summary(rid)["efficiency_score"] is None
    server.post(f"/runs/{rid}/metrics", json=[{"name": "loss", "value": 1.0, "step": 0, "timestamp": 1.0}])
    assert db.db_compute_run_summary(rid)["efficiency_score"] == "A"
