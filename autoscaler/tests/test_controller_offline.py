"""Kiểm tra controller KHÔNG cần cụm: phát lại một run trong dataset như thể đang chạy online.

1. Chuyển đổi định dạng: node_frame/svc_frame/edge_frame cho cùng kết quả với file CSV collect_metrics.py ghi.
2. Feature online == feature huấn luyện (dataset/processed/gatgru/runs_raw.npz) trên nhiều cửa sổ.
3. Đầu-cuối: CSV run test -> feature online -> GAT-GRU -> khớp dự báo test đã lưu
   (artifacts/gatgru/v05_selected_features_final/gatgru_selected_predictions_test.npz).
4. Lớp an toàn: biên độ, khoảng 1..15, cooldown, trần tổng.
5. Vòng lặp đầy đủ với Kubernetes giả và PPO giả đúng hợp đồng: gatgru-ppo (thực thi),
   shadow (không thực thi), dummy bị chặn, mất Jaeger -> hold, mất Prometheus -> hold.

Chạy từ thư mục gốc repo (cần numpy, pandas, torch, prometheus_client; KHÔNG cần stable-baselines3,
kubernetes hay cụm):
    python autoscaler/tests/test_controller_offline.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "autoscaler", ROOT / "modeling-common", ROOT / "load-testing"):
    sys.path.insert(0, str(p))

import numpy as np            # noqa: E402
import pandas as pd           # noqa: E402

import collect_metrics as cm  # noqa: E402
import forecast_data as fd    # noqa: E402
from controller import monitor as mon                       # noqa: E402
from controller.analyze import Forecaster, verify_checksums  # noqa: E402
from controller.config import Policy                        # noqa: E402
from controller.env import build_observation, load_contract  # noqa: E402
from controller.features import build_window                # noqa: E402
from controller.main import Controller                      # noqa: E402
from controller.plan import SafetyLayer                     # noqa: E402

ART = ROOT / "artifacts/gatgru/v05_selected_features_final"
RAW = ROOT / "dataset/raw"
NPZ = ROOT / "dataset/processed/gatgru/runs_raw.npz"
CONTRACT = ROOT / "autoscaler/rl/observation_contract.json"
STEP = fd.STEP_SEC
N_WIN = 18 + fd.WARMUP_DROP
PASS = []


def ok(msg):
    PASS.append(msg)
    print(f"  [ OK ] {msg}")


class ReplaySource:
    """Đọc CSV của một run như fd.load_runs, trả Snapshot cho cửa sổ [start, end)."""

    def __init__(self, run_dir):
        d = Path(run_dir)
        self.meta = json.loads((d / "meta.json").read_text())
        self.S = self.meta["start"] - self.meta["start"] % STEP
        self.node = fd._read(d / "node_metrics.csv")
        self.svc = fd._read(d / "service_metrics.csv")
        self.edge = fd._read(d / "edge_metrics.csv")
        self.drop_jaeger = False
        self.drop_prom = False

    def ts_of_index(self, i):            # chỉ số X (sau warm-up) -> đầu bước
        return self.S + (i + fd.WARMUP_DROP) * STEP

    def snapshot(self, end):
        start = end - N_WIN * STEP
        cut = lambda df: df[(df.timestamp >= start) & (df.timestamp < end)].reset_index(drop=True)  # noqa: E731
        node = cut(self.node).iloc[0:0] if self.drop_prom else cut(self.node)
        svc = cut(self.svc).iloc[0:0] if self.drop_jaeger else cut(self.svc)
        edge = cut(self.edge).iloc[0:0] if self.drop_jaeger else cut(self.edge)
        last = node[node.timestamp == end - STEP]
        q = {"prometheus_ok": last["service"].nunique() >= 10, "jaeger_ok": not self.drop_jaeger}
        return mon.Snapshot(start, end, STEP, node, svc, edge, q)


class ReplayMonitor:
    def __init__(self, src, lag=0):
        self.src, self.lag = src, lag

    def fetch(self, now):
        end = int((now - self.lag) // STEP) * STEP
        return self.src.snapshot(end)


class FakeK8s:
    def __init__(self, services, req=0.15):
        self.rep = {s: 1 for s in services}
        self.req = req
        self.calls = []

    def state(self, services):
        return {s: {"replicas": self.rep[s], "ready": self.rep[s], "cpu_request": self.req} for s in services}

    def scale(self, s, n):
        self.calls.append((s, n))
        self.rep[s] = n


class StubPPO:
    """PPO giả đúng hợp đồng (không cần stable-baselines3): luôn đề xuất +2 cho frontend."""

    def __init__(self, dummy=False):
        self.contract = load_contract(CONTRACT)
        self.dummy = dummy
        self.offset = 2

    def deltas(self, obs):
        assert obs.shape == (self.contract["observation_dim"],)
        return {s: (2 if s == "frontend" else 0) for s in self.contract["scaled_services"]}


def model_dir(tmp):
    d = Path(tmp) / "gatgru" / "v05"
    d.mkdir(parents=True)
    for f in ["gatgru_selected_model_seed_0.pt", "handoff_manifest.json", "reference_input.npz", "reference_output.npz"]:
        shutil.copy(ART / f, d / f)
    for f in ["graph_metadata.json", "feature_contract_v01.json"]:
        shutil.copy(ROOT / "configs/gatgru" / f, d / f)
    import hashlib
    lines = [f"{hashlib.sha256((d / f).read_bytes()).hexdigest()}  {f}" for f in sorted(os.listdir(d))]
    (d / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    return d


def test_frames():
    print("1. Định dạng CSV online == collect_metrics.write_csv")
    inbound = {(100, "frontend"): {"n": 7, "err": 1, "lat": [12.0, 30.5, 8.25, 100.0]},
               (100, "cartservice"): {"n": 2, "err": 0, "lat": [5.0, 6.0]}}
    edges = {(100, "frontend", "cartservice"): {"n": 2, "err": 0, "lat": [4.0, 9.0]}}
    rows = {(100, "frontend"): {"cpu_cores": 0.1234567891, "replicas": 2.0, "cpu_throttle_ratio": 0.0}}
    with tempfile.TemporaryDirectory() as t:
        p = Path(t)
        cm.write_csv(p / "s.csv", mon.SVC_COLS, [[ts, s, a["n"], a["err"], cm._r(cm._pct(a["lat"], .5), 3),
                                                  cm._r(cm._pct(a["lat"], .95), 3)] for (ts, s), a in sorted(inbound.items())])
        cm.write_csv(p / "e.csv", mon.EDGE_COLS, [[ts, s, d, a["n"], a["err"], cm._r(cm._pct(a["lat"], .5), 3),
                                                   cm._r(cm._pct(a["lat"], .95), 3)] for (ts, s, d), a in sorted(edges.items())])
        cm.write_csv(p / "n.csv", mon.NODE_COLS, [[ts, s] + [cm._r(f.get(k)) for k in cm.NODE_FIELDS]
                                                  for (ts, s), f in sorted(rows.items())])
        pd.testing.assert_frame_equal(mon.svc_frame(inbound), fd._read(p / "s.csv"))
        pd.testing.assert_frame_equal(mon.edge_frame(edges), fd._read(p / "e.csv"))
        pd.testing.assert_frame_equal(mon.node_frame(rows), fd._read(p / "n.csv"))
    ok("svc/edge/node frame giống hệt file CSV của dataset")


def test_feature_parity():
    print("2. Feature online == feature huấn luyện (runs_raw.npz)")
    runs, meta = fd.load_runs_npz(NPZ)
    edges = [tuple(e) for e in meta["edges"]]
    checked = 0
    for key in ["math-load-testing/mmpp/run_01", "math-load-testing/nhpp/run_03", "math-load-testing/onoff/run_05"]:
        r = next(x for x in runs if x["key"] == key)
        src = ReplaySource(RAW / key.split("/", 1)[1])
        n = len(r["X"])
        for L in sorted({17, 18, 40, n // 2, n - 1}):
            end = src.ts_of_index(L) + STEP
            arr = build_window(src.snapshot(end), edges, 18)
            np.testing.assert_allclose(arr.X, r["X"][L - 17:L + 1], rtol=1e-6, atol=1e-6)
            np.testing.assert_allclose(arr.E, r["E"][L - 17:L + 1], rtol=1e-6, atol=1e-6)
            np.testing.assert_allclose(arr.G, r["G"][L - 17:L + 1], rtol=1e-6, atol=1e-6)
            checked += 1
    ok(f"X/E/G khớp tuyệt đối trên {checked} cửa sổ của 3 run (mọi feature ứng viên)")


def test_golden_forecast(fc):
    print("3. Đầu-cuối: CSV -> feature online -> GAT-GRU == dự báo test đã lưu")
    gold = np.load(ART / "gatgru_selected_predictions_test.npz", allow_pickle=True)
    seed_idx = json.loads((ART / "gatgru_selected_training_meta.json").read_text())["seeds"].index(0) \
        if (ART / "gatgru_selected_training_meta.json").exists() else 0
    info = gold["info"]
    picks = []
    for key in sorted({k for k, _, _ in info}):
        idx = [i for i, (k, _, _) in enumerate(info) if k == key]
        picks += [idx[0], idx[len(idx) // 2], idx[-1]]
    worst = 0.0
    for i in picks:
        key, _, t0 = info[i]
        src = ReplaySource(RAW / key.split("/", 1)[1])
        L = int(t0) - 1
        arr = build_window(src.snapshot(src.ts_of_index(L) + STEP), fc.edges, fc.window)
        y = fc.forecast(arr)
        exp = gold["Y_pred"][seed_idx][i]
        np.testing.assert_allclose(y, exp, rtol=1e-4, atol=1e-4)
        worst = max(worst, float(np.max(np.abs(y - exp))))
    ok(f"{len(picks)} cửa sổ của {len(picks) // 3} run test khớp dự báo đã lưu (max_abs_diff={worst:.2e})")


def test_safety():
    print("4. Lớp an toàn")
    p = Policy.from_dict({"max_total_replicas": "14"})
    sl = SafetyLayer(p)
    cur = {s: 1 for s in p.scaled_services}
    prop = dict(cur, frontend=9, currencyservice=0, cartservice=3)
    fin, notes = sl.apply(cur, prop, now=1000, last_scale={}, max_step=2)
    assert fin["frontend"] == 3 and fin["currencyservice"] == 1, fin
    assert sum(fin.values()) <= 14, fin
    cur2 = dict(cur, frontend=5)
    fin2, _ = sl.apply(cur2, dict(cur2, frontend=3), now=1000, last_scale={"frontend": 950}, max_step=2)
    assert fin2["frontend"] == 5, "cooldown phải hoãn scale down"
    fin3, _ = sl.apply(cur2, dict(cur2, frontend=3), now=1000, last_scale={"frontend": 800}, max_step=2)
    assert fin3["frontend"] == 3
    big = {s: 15 for s in p.scaled_services}
    fin4, _ = sl.apply(cur, big, now=1000, last_scale={}, max_step=4)
    assert sum(fin4.values()) == 14 and max(fin4.values()) <= 5, fin4
    ok("kẹp |Δ|, khoảng 1..15, cooldown scale down, trần tổng")


def run_loop(fc, src, mode, ppo=None, extra=None, decisions=4):
    pol = Policy.from_dict({"mode": mode, "ingest_lag_sec": "0", **(extra or {})})
    k8s = FakeK8s(pol.scaled_services)
    with tempfile.TemporaryDirectory() as t:
        from controller.telemetry import DecisionLog
        ctrl = Controller(pol, ReplayMonitor(src), k8s, fc, ppo, DecisionLog(t))
        # chọn đoạn tải tăng: bắt đầu tại chỉ số có rps frontend thấp, chạy vài quyết định
        now0 = src.ts_of_index(60) + STEP
        recs = []
        for k in range(decisions * 3):
            r = ctrl.cycle(now0 + k * STEP)
            if r:
                recs.append(r)
        lines = sum(1 for f in Path(t).iterdir() for _ in open(f, encoding="utf-8"))
    return ctrl, k8s, recs, lines


def test_loop(fc):
    print("5. Vòng lặp đầy đủ với Kubernetes giả")
    src = ReplaySource(RAW / "mmpp/run_01")
    ctrl, k8s, recs, lines = run_loop(fc, src, "gatgru-ppo", ppo=StubPPO())
    assert recs and all(r["policy_used"] == "gatgru-ppo" and r["executed"] for r in recs) and lines == len(recs)
    assert ("frontend", 3) in k8s.calls and len(recs[0]["observation"]) == 132
    ok(f"gatgru-ppo (PPO giả đúng hợp đồng): observation 132 chiều, Δ +2 được thực thi, "
       f"{len(recs)} quyết định, decision log {lines} dòng")

    ctrl, k8s, recs, _ = run_loop(fc, src, "shadow", ppo=StubPPO())
    assert recs and not k8s.calls and all(not r["executed"] and r["policy_used"] == "gatgru-ppo" for r in recs)
    ok("shadow: tính quyết định PPO nhưng không PATCH")

    ctrl, k8s, recs, _ = run_loop(fc, src, "gatgru-ppo", ppo=StubPPO(dummy=True))
    assert not k8s.calls and recs[0]["blocked_reason"] and not ctrl.health.status()[0]
    ok("PPO dummy bị chặn ở active khi allow_active_with_dummy=false (healthz 503)")

    src.drop_jaeger = True
    ctrl, k8s, recs, _ = run_loop(fc, src, "gatgru-ppo", ppo=StubPPO(), extra={"jaeger_fail_cycles_before_hold": "1"})
    assert all(r["policy_used"] == "hold" for r in recs) and not k8s.calls
    ok("mất Jaeger -> hold (không đổi replica)")
    src.drop_jaeger, src.drop_prom = False, True
    ctrl, k8s, recs, _ = run_loop(fc, src, "gatgru-ppo", ppo=StubPPO())
    assert all(r["policy_used"] == "hold" for r in recs) and not k8s.calls
    ok("mất Prometheus -> hold (không đổi replica)")
    src.drop_prom = False

    arr = build_window(src.snapshot(src.ts_of_index(100) + STEP), fc.edges, fc.window)
    obs = build_observation(load_contract(CONTRACT), arr, fc.forecast(arr), fc.targets,
                            {s: 1 for s in fd.SERVICES}, {}, 300, 0.01)
    assert obs.shape == (132,) and np.isfinite(obs).all()
    ok("build_observation đúng hợp đồng ppo_obs_v3_10svc_d2")


def test_monitor_cache():
    print("6. Monitor: đệm Jaeger theo bước, cửa sổ, chất lượng dữ liệu (giả lập Prometheus/Jaeger)")
    calls = {"jaeger": [], "prom": []}
    services = Policy.from_dict({}).scaled_services + ["redis-cart"]

    def fake_prom(base, ns, start, end, step, window, report):
        calls["prom"].append((start, end))
        report["prometheus_series"] = {"cpu_cores": 11}
        return {(ts, s): {"cpu_cores": 0.1, "replicas": 1.0, "cpu_throttle_ratio": 0.0}
                for ts in range(start, end + 1, step) for s in services}

    def fake_jaeger(base, ns, start, end, step, limit, report):
        calls["jaeger"].append(start)
        report["jaeger_services"] = {"frontend": "frontend.online-boutique"}
        return ({(start, "frontend", "cartservice"): {"n": 3, "err": 0, "lat": [5.0, 7.0, 9.0]}},
                {(start, "frontend"): {"n": 10, "err": 0, "lat": [10.0] * 10}})

    orig = cm.collect_prometheus, cm.collect_jaeger
    cm.collect_prometheus, cm.collect_jaeger = fake_prom, fake_jaeger
    try:
        p = Policy.from_dict({"ingest_lag_sec": "20"})
        m = mon.Monitor(p)
        now = 1_000_000.0
        s1 = m.fetch(now)
        assert s1.n_steps == N_WIN and s1.end == int((now - 20) // STEP) * STEP
        assert len(calls["jaeger"]) == N_WIN and s1.quality["jaeger_ok"] and s1.quality["prometheus_ok"]
        assert calls["prom"][-1] == (s1.start, s1.end - STEP)
        s2 = m.fetch(now + STEP)
        assert len(calls["jaeger"]) == N_WIN + 1, "chỉ được truy vấn thêm đúng 1 bước mới"
        assert min(m._jaeger) == s2.start, "bước ra khỏi cửa sổ phải bị xóa khỏi đệm"
        arr = build_window(s2, [tuple(e) for e in json.loads((ROOT / "configs/gatgru/graph_metadata.json").read_text())["edges"]], 18)
        assert arr.X.shape == (18, 11, len(fd.NF)) and float(arr.X[-1, 0, fd.NF.index("rps_in")]) == 1.0
        m2 = mon.Monitor(p)
        m2.min_bucket = s1.end - 2 * STEP                     # restore_history_on_start=false
        s3 = m2.fetch(now)
        assert not s3.quality["jaeger_ok"] and s3.quality["jaeger_buckets_cached"] == 2
    finally:
        cm.collect_prometheus, cm.collect_jaeger = orig
    ok("24 bước lúc khởi động, sau đó 1 bước/chu kỳ; xóa bước cũ; rps_in = request_count/10; chờ đủ cửa sổ khi không nạp lịch sử")


def main():
    test_monitor_cache()
    with tempfile.TemporaryDirectory() as tmp:
        d = model_dir(tmp)
        test_frames()
        test_feature_parity()
        n = verify_checksums(d)
        fc = Forecaster(str(d), str(d / "gatgru_selected_model_seed_0.pt"))
        st = fc.self_test()
        ok(f"gói model: checksum {n} file, self-test max_abs_diff={st['max_abs_diff']:.2e}")
        test_golden_forecast(fc)
        test_safety()
        test_loop(fc)
    print(f"\nĐẠT {len(PASS)} kiểm tra.")


if __name__ == "__main__":
    main()
