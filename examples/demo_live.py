"""Live dashboard demo: fake jobs fed with synthetic data over ~10 minutes.

No torch and no GPU needed. It talks to the server's HTTP API directly and fakes everything a real run would send,
including multi-GPU telemetry, profiler traces, gradient stats, logs, samples and media. Use it to walk through the
dashboard: live updates, the GPU-load firing curve, anomalies, compare/grouping, the Profiler / Model health /
Samples / Media tabs, crashed, failed and killed runs, and per-project views.

Usage:
    pyrometer serve --no-auth                    # terminal 1 (or a server with auth: export PYROMETER_API_KEY)
    python examples/demo_live.py                 # terminal 2; open http://localhost:8321
    python examples/demo_live.py --minutes 3     # shorter
    python examples/demo_live.py --speed 4       # 4x faster (same data, less wall time)

Jobs (projects "demo-llm", "demo-rl" and "demo-bench"):
  llm-pretrain-350m      2 GPUs, loss/lr/throughput/grad-norm, val loss, loss spike anomaly near the 60% mark, eval dips
                         in GPU load, checkpoints, profiler steps, per-layer gradient stats, console logs
  lr-sweep-{1e-4,3e-4,1e-3}-s{0,1}  sweep group (compare + "group by lr, ignore seed" bands); the 1e-3 seeds diverge
                         and fail with NaN
  rlhf-ppo-r3            episode returns, KL, reward, prompt/completion/reward samples every ~30s
  vision-cls-resnet      images, histograms and a figure (media gallery)
  bench-fp8-matmul       benchmark preset tags, steps/sec, crashes part-way
  data-loader-probe      killed by the user after ~2 minutes
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import random
import struct
import sys
import time
import uuid
import zlib

import httpx

GiB = 2**30


def png(width: int, height: int, pixel) -> str:
    """Tiny dependency-free PNG encoder; ``pixel(x, y) -> (r, g, b)``. Returns base64."""
    raw = b"".join(b"\x00" + bytes(c for x in range(width) for c in pixel(x, y)) for y in range(height))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    return base64.b64encode(data).decode()


class Job:
    """One fake run. ``progress`` goes 0 -> 1 over the job's lifetime; subclasses emit data in ``tick``."""

    total_steps = 10_000
    gpus = 1
    end_at = 1.0  # fraction of the demo at which the job finishes
    start_at = 0.0
    final_status = "completed"

    def __init__(self, api: "Api", name: str, project: str, tags: list[str], hp: dict, group: str | None = None):
        self.api, self.name, self.project, self.tags, self.hp, self.group = api, name, project, tags, hp, group
        self.run_id: str | None = None
        self.step = 0
        self.last_sys = 0.0
        self.last_hb = 0.0
        self.done = False
        self.rng = random.Random(hash(name) & 0xFFFF)

    def start(self) -> None:
        body = {
            "run_id": uuid.uuid4().hex, "created_at": time.time(), "name": self.name, "project": self.project,
            "tags": self.tags, "hyperparams": self.hp, "group": self.group,
            "gpu_model": "NVIDIA H100 80GB HBM3" if self.gpus > 1 else "NVIDIA GeForce RTX 4090",
            "gpu_memory_total": (80 if self.gpus > 1 else 24) * GiB,
            "gpus": [{"index": i, "name": "NVIDIA H100 80GB HBM3" if self.gpus > 1 else "NVIDIA GeForce RTX 4090",
                      "memory_total": (80 if self.gpus > 1 else 24) * GiB} for i in range(self.gpus)],
            "gpu_count": self.gpus, "pytorch_version": "2.5.1+cu124", "cuda_version": "12.4",
            "python_version": "3.12.7", "hostname": "trainer-01", "git_hash": "3f9c2ab", "git_dirty": True,
        }
        self.run_id = self.api.post("/runs", body)["run_id"]
        self.log(f"pyrometer: run {self.name} started ({self.project})")

    # --- helpers -------------------------------------------------------------------------------------------------
    def metrics(self, values: dict[str, float], step: int | None = None) -> None:
        ts = time.time()
        s = self.step if step is None else step
        self.api.post(f"/runs/{self.run_id}/metrics",
                      [{"step": s, "timestamp": ts, "name": k, "value": v} for k, v in values.items()])

    def event(self, message: str, event_type: str = "milestone", severity: str = "info", **meta) -> None:
        self.api.post(f"/runs/{self.run_id}/events", {"timestamp": time.time(), "step": self.step,
                      "event_type": event_type, "severity": severity, "message": message, "metadata": meta})

    def log(self, line: str, stream: str = "stdout") -> None:
        self.api.post(f"/runs/{self.run_id}/logs", [{"timestamp": time.time(), "stream": stream, "line": line,
                      "step": self.step}])

    def system(self, util: float) -> None:
        ts = time.time()
        for g in range(self.gpus):
            u = max(0.0, min(100.0, util + self.rng.gauss(0, 2.5) - 4 * g))
            total = (80 if self.gpus > 1 else 24) * GiB
            proc = int(total * (0.55 + 0.25 * u / 100))
            self.api.post(f"/runs/{self.run_id}/system", {
                "timestamp": ts, "gpu_index": g, "gpu_utilization": min(100.0, u + 3), "attributed_utilization": u,
                "memory_used": proc + GiB, "process_memory_used": proc, "memory_total": total,
                "temperature": 48 + 30 * u / 100 + g, "power_draw": 90 + 560 * u / 100,
                "attributed_power": 80 + 540 * u / 100, "sm_clock_mhz": 1980 if u > 20 else 1410,
                "memory_clock_mhz": 2619, "fan_speed": 30 + 50 * u / 100, "process_exclusive": True})

    def heartbeat(self) -> None:
        self.api.post(f"/runs/{self.run_id}/heartbeat", None)

    def finish(self, status: str | None = None) -> None:
        status = status or self.final_status
        self.log(f"pyrometer: run finished ({status})")
        self.api.patch(f"/runs/{self.run_id}", {"status": status})
        self.done = True

    def util(self, p: float) -> float:
        return 90.0

    # --- driver ---------------------------------------------------------------------------------------------------
    def advance(self, demo_p: float, now: float, sys_every: float) -> None:
        if self.done or demo_p < self.start_at:
            return
        if self.run_id is None:
            self.start()
        p = min(1.0, (demo_p - self.start_at) / max(1e-9, self.end_at - self.start_at))
        prev = self.step
        self.step = int(self.total_steps * p)
        if now - self.last_sys >= sys_every:
            self.system(self.util(p))
            self.last_sys = now
        if now - self.last_hb >= 10:
            self.heartbeat()
            self.last_hb = now
        if self.step > prev:
            self.tick(p, prev)
        if p >= 1.0 and not self.done:
            self.finish()

    def tick(self, p: float, prev: int) -> None:
        raise NotImplementedError


class Pretrain(Job):
    total_steps = 41_200
    gpus = 2

    def __init__(self, api: "Api"):
        super().__init__(api, "llm-pretrain-350m", "demo-llm", ["llm-pretrain", "bf16", "fsdp"],
                         {"model": "gpt-350m", "n_layer": 24, "d_model": 1024, "lr": 3e-4, "warmup_steps": 2000,
                          "batch_size": 512, "seq_len": 2048, "optimizer": "AdamW", "weight_decay": 0.1,
                          "precision": "bf16", "dataset": "fineweb-edu-10B"}, group="pretrain-350m")
        self.profiled: set[int] = set()
        self.ckpt = 0
        self.spiked = False

    def util(self, p: float) -> float:
        if p < 0.04:
            return 25 + 1700 * p
        if any(abs(p - e) < 0.012 for e in (0.25, 0.5, 0.75)):
            return 18  # eval pauses
        if 0.6 <= p < 0.61:
            return 55
        return 93

    def loss(self, s: int) -> float:
        base = 2.15 + 8.6 * math.exp(-s / 3200)
        if self.spiked and s < self.spike_step + 600:
            base += 1.1 * math.exp(-(s - self.spike_step) / 200)
        return base * (1 + self.rng.gauss(0, 0.012))

    def tick(self, p: float, prev: int) -> None:
        s = self.step
        if not self.spiked and p >= 0.6:
            self.spiked, self.spike_step = True, s
            self.event(f"loss spike: {self.loss(s):.2f} vs rolling median 2.31 (step {s:,})", "anomaly_detected",
                       "warning", metric="auto/loss", detector="spike")
            self.log("WARNING: loss spike detected, grad norm 7.9 (clipped to 1.0)", "stderr")
        lr = 3e-4 * s / 2000 if s < 2000 else 3e-4 * 0.5 * (1 + math.cos(math.pi * (s - 2000) / 60000))
        gn = 0.85 + 3 * math.exp(-s / 2500) + (6.5 if self.spiked and s < self.spike_step + 150 else 0)
        tps = (48_200 if p > 0.04 else 20_000) * (1 + self.rng.gauss(0, 0.015))
        self.metrics({"auto/loss": self.loss(s), "auto/lr": lr, "auto/grad_norm": gn * (1 + self.rng.gauss(0, 0.08)),
                      "auto/tokens_per_sec": tps, "auto/steps_per_sec": tps / (512 * 2048 / 1e2) / 100,
                      "tokens_seen": s * 1_048_576, "train/mfu": 0.41 * (1 + self.rng.gauss(0, 0.01))})
        for frac in (0.25, 0.5, 0.75, 1.0):
            if prev < self.total_steps * frac <= s:
                vl = self.loss(s) + 0.07
                self.metrics({"val/loss": vl, "val/ppl": math.exp(vl)})
                self.event(f"eval at step {s:,}: val/loss {vl:.3f}", "eval")
                self.log(f"[eval] step {s:>6} val/loss {vl:.4f} ppl {math.exp(vl):.1f}")
        if s // 5000 > self.ckpt:
            self.ckpt = s // 5000
            path = f"/checkpoints/llm-pretrain-350m/step_{s:06d}.pt"
            self.api.post(f"/runs/{self.run_id}/artifacts", {"timestamp": time.time(), "step": s,
                          "artifact_type": "checkpoint", "path": path, "size_bytes": int(4.2 * GiB)})
            self.event(f"checkpoint saved: {path}", "checkpoint")
        if s // 500 > prev // 500:
            self.log(f"step {s:>6} | loss {self.loss(s):.4f} | lr {lr:.2e} | {tps/1000:.1f}k tok/s")
            self.param_stats()
        for ps in (2_000, 15_000, 30_000):
            if prev < ps <= s and ps not in self.profiled:
                self.profiled.add(ps)
                self.profile(ps)

    def param_stats(self) -> None:
        ts, s, rows = time.time(), self.step, []
        layers = ["embed.weight"] + [f"blocks.{i}.{k}" for i in (0, 6, 12, 18, 23) for k in ("attn.qkv.weight", "mlp.fc1.weight")] + ["lm_head.weight"]
        for i, name in enumerate(layers):
            g = (0.02 + 0.08 * math.exp(-s / 4000)) * (1.6 if "embed" in name else 1) * (0.7 + i / len(layers))
            if self.spiked and s < self.spike_step + 600:
                g *= 6
            g *= 1 + self.rng.gauss(0, 0.1)
            w = 18 + 4 * i + s / 8000
            hist = [max(0.0, math.exp(-((b - 16) / 5) ** 2) * 1000 * (1 + self.rng.gauss(0, 0.05))) for b in range(32)]
            rows.append({"step": s, "timestamp": ts, "param_name": name, "grad_norm": g, "grad_min": -g / 3,
                         "grad_max": g / 3, "grad_mean": g / 1e3, "weight_norm": w, "weight_min": -0.2,
                         "weight_max": 0.2, "weight_mean": 0.0, "grad_histogram": hist, "weight_histogram": hist[::-1]})
        self.api.post(f"/runs/{self.run_id}/param-stats", rows)

    def profile(self, step: int) -> None:
        ts = time.time()
        ops = [("aten::mm", 3100, 410), ("flash_attn::_flash_attn_forward", 1850, 120), ("flash_attn::_flash_attn_backward", 2900, 140),
               ("aten::addmm", 980, 220), ("aten::layer_norm", 410, 90), ("aten::gelu", 300, 60),
               ("aten::cross_entropy_loss", 150, 80), ("aten::copy_", 620, 340), ("aten::add_", 240, 120),
               ("Optimizer.step#AdamW.step", 870, 1300), ("aten::_foreach_mul_", 260, 300),
               ("aten::embedding", 90, 70), ("aten::view", 0, 210), ("cudaLaunchKernel", 0, 1900),
               ("aten::empty", 0, 160), ("autograd::engine::evaluate_function: MmBackward0", 2100, 260),
               ("aten::sum", 140, 70), ("nccl:all_reduce", 1400, 90)]
        events = []
        for name, cuda, cpu in ops:
            j = 1 + self.rng.gauss(0, 0.05)
            events.append({"step": step, "timestamp": ts, "name": name, "category": "cuda_kernel" if cuda else "cpu_op",
                           "duration_us": (cuda + cpu) * j, "cuda_time_us": cuda * j, "cpu_time_us": cpu * j,
                           "input_shapes": [[512, 2048, 1024]] if "mm" in name else [],
                           "memory_bytes": int(self.rng.choice([64, 128, 256, -128, 512]) * 2**20) if cuda else None,
                           "device_id": 0})
        self.api.post(f"/runs/{self.run_id}/profile", events)
        self.event(f"profiled step {step:,}", "profile")


class Sweep(Job):
    total_steps = 3_000

    def __init__(self, api: "Api", lr: float, seed: int, start_at: float, end_at: float):
        super().__init__(api, f"lr-sweep-{lr:.0e}-s{seed}".replace("e-0", "e-"), "demo-llm", ["sweep", "lr-sweep"],
                         {"lr": lr, "seed": seed, "optimizer": "AdamW", "batch_size": 64, "model": "gpt-35m",
                          "warmup_steps": 200}, group="lr-sweep")
        self.lr, self.seed, self.start_at, self.end_at = lr, seed, start_at, end_at
        self.diverge = lr >= 1e-3

    def util(self, p: float) -> float:
        return 82

    def tick(self, p: float, prev: int) -> None:
        s = self.step
        speed = {1e-4: 0.45, 3e-4: 1.0, 1e-3: 1.6}.get(self.lr, 1.0)
        loss = 3.1 + 5.5 * math.exp(-s * speed / 900) + 0.04 * self.seed
        if self.diverge and p > 0.55:
            loss *= 1 + (p - 0.55) * 12
            if p > 0.7:
                self.metrics({"auto/loss": float("nan")})
                self.event("NaN in auto/loss", "anomaly_detected", "critical", metric="auto/loss", detector="nan")
                self.log("RuntimeError: loss is NaN, stopping", "stderr")
                self.finish("failed")
                return
        self.metrics({"auto/loss": loss * (1 + self.rng.gauss(0, 0.015)), "val/loss": loss + 0.1,
                      "auto/lr": self.lr * min(1.0, s / 200), "auto/steps_per_sec": 11.5 + self.rng.gauss(0, 0.3)})


class Rlhf(Job):
    total_steps = 18_640

    def __init__(self, api: "Api"):
        super().__init__(api, "rlhf-ppo-r3", "demo-rl", ["rlhf", "ppo"],
                         {"policy": "llama-1b-sft", "reward_model": "rm-1b-v2", "kl_coef": 0.05, "lr": 1e-6,
                          "rollout_batch": 256, "ppo_epochs": 4})
        self.last_samples = -1
        self.prompts = ["Explain why the sky is blue to a 6-year-old.", "Write a haiku about gradient descent.",
                        "Summarize the plot of Hamlet in two sentences.", "What is 17 * 23?",
                        "Give one tip for debugging a CUDA out-of-memory error."]

    def util(self, p: float) -> float:
        return 72 + 18 * math.sin(p * 60)

    def tick(self, p: float, prev: int) -> None:
        s = self.step
        ret = -1.6 + 3.1 * (1 - math.exp(-s / 6000))
        self.metrics({"episode/return_mean": ret + self.rng.gauss(0, 0.08), "episode/return_std": 0.6 - 0.2 * p,
                      "episode/length_mean": 180 + 60 * p, "rlhf/kl": 0.01 + 0.09 * p + self.rng.gauss(0, 0.004),
                      "rlhf/reward_mean": ret * 0.8, "auto/loss": 0.6 * math.exp(-s / 9000) + 0.08,
                      "auto/lr": 1e-6, "env_steps": s * 256})
        bucket = int(p * 20)
        if bucket > self.last_samples:
            self.last_samples = bucket
            samples = []
            for q in self.prompts:
                good = self.rng.random() < 0.3 + 0.6 * p
                samples.append({"prompt": q, "completion": ("A clear, correct answer. " if good else "Hmm, I think maybe... ") * 2,
                                "reward": round((0.9 if good else -0.4) + self.rng.gauss(0, 0.15), 3),
                                "kl": round(0.02 + 0.08 * p, 4)})
            self.api.post(f"/runs/{self.run_id}/samples", {"name": "rollouts", "step": s, "timestamp": time.time(),
                          "samples": samples, "mode": "replace"})


class Vision(Job):
    total_steps = 9_000
    start_at = 0.1
    end_at = 0.85

    def __init__(self, api: "Api"):
        super().__init__(api, "vision-cls-resnet50", "demo-llm", ["vision", "finetune"],
                         {"model": "resnet50", "dataset": "imagenet-100", "lr": 0.1, "batch_size": 256, "mixup": 0.2})
        self.last_media = -1

    def util(self, p: float) -> float:
        return 64 + self.rng.gauss(0, 6)  # dataloader-bound

    def tick(self, p: float, prev: int) -> None:
        s = self.step
        acc = 0.92 * (1 - math.exp(-s / 2500))
        self.metrics({"auto/loss": 4.6 * math.exp(-s / 1800) + 0.35, "val/accuracy": acc, "train/accuracy": acc + 0.03,
                      "auto/samples_per_sec": 2900 + self.rng.gauss(0, 80), "auto/lr": 0.1 * 0.5 * (1 + math.cos(math.pi * p))})
        bucket = int(p * 8)
        if bucket > self.last_media:
            self.last_media = bucket
            t = p
            img = png(64, 64, lambda x, y: (int(255 * (0.5 + 0.5 * math.sin(x / 6 + t * 6))),
                                            int(255 * (0.5 + 0.5 * math.cos(y / 5 - t * 4))), int(120 + 100 * t)))
            ts = time.time()
            self.api.post(f"/runs/{self.run_id}/media", {"step": s, "timestamp": ts, "name": "augmented_batch",
                          "media_type": "image", "data": img})
            att = png(48, 48, lambda x, y: (min(255, int(255 * math.exp(-((x - 24) ** 2 + (y - 24) ** 2) / (80 + 400 * t)))), 40, 30))
            self.api.post(f"/runs/{self.run_id}/media", {"step": s, "timestamp": ts, "name": "saliency",
                          "media_type": "figure", "data": att})
            counts = [int(1000 * math.exp(-((i - 16) / (3 + 5 * t)) ** 2)) for i in range(32)]
            edges = [-1 + 2 * i / 32 for i in range(33)]
            self.api.post(f"/runs/{self.run_id}/media", {"step": s, "timestamp": ts, "name": "logits",
                          "media_type": "histogram", "data": json.dumps({"edges": edges, "counts": counts})})


class Bench(Job):
    total_steps = 7_420
    end_at = 0.45
    final_status = "crashed"

    def __init__(self, api: "Api"):
        super().__init__(api, "bench-fp8-matmul", "demo-bench", ["benchmark", "fp8"],
                         {"m": 8192, "n": 8192, "k": 8192, "dtype": "float8_e4m3fn", "compile": True})

    def util(self, p: float) -> float:
        return 99 if p < 0.97 else 0

    def tick(self, p: float, prev: int) -> None:
        self.metrics({"auto/steps_per_sec": 41.0 + self.rng.gauss(0, 0.4), "bench/tflops": 1240 + self.rng.gauss(0, 15),
                      "bench/latency_ms": 24.3 + self.rng.gauss(0, 0.3)})
        if p >= 1.0:
            self.log("Segmentation fault (core dumped)", "stderr")


class Killed(Job):
    total_steps = 2_000
    end_at = 0.2
    final_status = "killed"

    def __init__(self, api: "Api"):
        super().__init__(api, "data-loader-probe", "demo-bench", ["debug"], {"num_workers": 2, "pin_memory": False})

    def util(self, p: float) -> float:
        return 22 + self.rng.gauss(0, 5)

    def tick(self, p: float, prev: int) -> None:
        self.metrics({"auto/samples_per_sec": 640 + self.rng.gauss(0, 40), "data/wait_ms": 38 + self.rng.gauss(0, 4)})
        if p >= 1.0:
            self.log("KeyboardInterrupt", "stderr")


class Api:
    def __init__(self, server: str, api_key: str | None):
        self.c = httpx.Client(base_url=server, timeout=30,
                              headers={"Authorization": f"Bearer {api_key}"} if api_key else {})

    def post(self, path: str, body) -> dict:
        r = self.c.post(path, content=json.dumps(body, allow_nan=True) if body is not None else None,
                        headers={"Content-Type": "application/json"})
        if r.status_code >= 400:
            print(f"  ! POST {path}: {r.status_code} {r.text[:200]}", file=sys.stderr)
            return {}
        return r.json().get("data") or {}

    def patch(self, path: str, body) -> None:
        r = self.c.patch(path, json=body)
        if r.status_code >= 400:
            print(f"  ! PATCH {path}: {r.status_code} {r.text[:200]}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=os.environ.get("PYROMETER_SERVER", "http://localhost:8321"))
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--speed", type=float, default=1.0, help="run the same data this many times faster")
    args = ap.parse_args()
    api = Api(args.server, os.environ.get("PYROMETER_API_KEY"))
    for name, private in (("demo-llm", False), ("demo-rl", False), ("demo-bench", False)):
        api.c.post("/projects", json={"name": name, "private": private})  # exists already / no permission: fine

    jobs: list[Job] = [Pretrain(api), Rlhf(api), Vision(api), Bench(api), Killed(api)]
    for i, (lr, seed) in enumerate([(lr, s) for lr in (1e-4, 3e-4, 1e-3) for s in (0, 1)]):
        jobs.append(Sweep(api, lr, seed, start_at=0.05 + 0.08 * i, end_at=0.45 + 0.08 * i))

    duration = args.minutes * 60 / args.speed
    t0 = time.time()
    print(f"Demo running for {duration / 60:.1f} min against {args.server}. Open the dashboard and watch the runs.")
    last_print = -1
    while True:
        now = time.time()
        p = min(1.0, (now - t0) / duration)
        for j in jobs:
            try:
                j.advance(p, now, sys_every=2.0 / args.speed)
            except httpx.HTTPError as e:
                print(f"  ! {j.name}: {e}", file=sys.stderr)
        if int(p * 20) != last_print:
            last_print = int(p * 20)
            live = sum(1 for j in jobs if j.run_id and not j.done)
            print(f"  {p * 100:5.1f}%  {live} live runs")
        if p >= 1.0 and all(j.done for j in jobs if j.run_id):
            break
        time.sleep(max(0.05, 1.0 / args.speed))
    print("Demo finished.")


if __name__ == "__main__":
    main()
