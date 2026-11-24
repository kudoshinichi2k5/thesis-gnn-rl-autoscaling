"""Vòng lặp MAPE-K của controller autoscaler.

Mỗi step_sec (10 s):  Monitor (Prometheus + Jaeger) -> Analyze (GAT-GRU dự báo t+1..t+6)
Mỗi decision_period_sec (30 s): Plan (PPO; giữ nguyên replica khi thiếu dữ liệu) -> lớp an toàn -> Execute
Knowledge: /metrics, /healthz, Decision Log JSONL trong decision_log_dir.

Chạy trong pod:   python -m controller.main          (CONFIG_DIR=/config)
"""
from __future__ import annotations

import logging
import math
import os
import signal
import sys
import time

import forecast_data as fd

from . import telemetry as tm
from .analyze import Forecaster, ForecastErrorTracker, verify_checksums
from .config import Policy, load_policy
from .env import build_observation
from .features import build_window
from .plan import PPOPolicy, SafetyLayer

log = logging.getLogger("autoscaler")


class Controller:
    def __init__(self, policy: Policy, monitor, k8s, forecaster: Forecaster, ppo: PPOPolicy | None = None,
                 decision_log: tm.DecisionLog | None = None, health: tm.Health | None = None):
        self.p = policy
        self.monitor, self.k8s, self.fc, self.ppo = monitor, k8s, forecaster, ppo
        self.dlog = decision_log
        self.health = health or tm.Health(stale_after=3 * policy.step_sec + 30)
        self.safety = SafetyLayer(policy)
        self.errors = ForecastErrorTracker(forecaster.services, forecaster.targets)
        self.last_scale: dict[str, float] = {}
        self.next_decision = 0.0
        self.jaeger_fail = 0
        self.arrays = None
        self.forecast = None
        self.quality: dict = {}
        self.blocked_reason = None
        if ppo is None:
            raise RuntimeError(f"mode {policy.mode} cần PPO đã nạp")
        if policy.mode == "gatgru-ppo" and ppo.dummy and not policy.allow_active_with_dummy:
            self.blocked_reason = ("PPO mẫu (dummy) bị chặn ở chế độ active: "
                                   "đặt allow_active_with_dummy=true nếu chỉ thử đường ống")
        reasons = [self.blocked_reason] if self.blocked_reason else []
        self.health.set(not reasons, reasons)

    # ------------------------------------------------------------------ Monitor + Analyze
    def observe(self, now: float) -> None:
        t0 = time.time()
        try:
            snap = self.monitor.fetch(now)
            self.quality = snap.quality
            self.arrays = build_window(snap, self.fc.edges, self.fc.window)
            self.errors.observe(self.arrays)
            self.forecast = self.fc.forecast(self.arrays)
            self.errors.add_forecast(self.arrays.step_ts[-1], self.p.step_sec, self.forecast)
        except Exception as exc:  # noqa: BLE001  (giữ vòng lặp sống, ghi lý do)
            log.exception("Monitor/Analyze lỗi: %s", exc)
            self.quality = {"prometheus_ok": False, "jaeger_ok": False, "error": str(exc)}
            self.arrays, self.forecast = None, None
        self.jaeger_fail = 0 if self.quality.get("jaeger_ok") else self.jaeger_fail + 1
        tm.DATA_OK.labels("prometheus").set(1 if self.quality.get("prometheus_ok") else 0)
        tm.DATA_OK.labels("jaeger").set(1 if self.quality.get("jaeger_ok") else 0)
        if self.forecast is not None:
            for svc in self.p.scaled_services:
                s = fd.SERVICES.index(svc)
                for j, tgt in enumerate(self.fc.targets):
                    for h in (1, 3, 6):
                        tm.FORECAST.labels(svc, tgt, f"t{h}").set(float(self.forecast[h - 1, s, j]))
        for tgt, v in self.errors.mae().items():
            if v is not None:
                tm.FORECAST_ERR.labels(tgt).set(v)
        tm.CYCLE.set(time.time() - t0)
        self.health.beat()

    # ------------------------------------------------------------------ Plan + Execute
    def _policy_used(self) -> str:
        # Thiếu dữ liệu (mất Prometheus, không có dự báo, hoặc mất Jaeger đủ số chu kỳ): giữ nguyên replica
        if self.arrays is None or not self.quality.get("prometheus_ok") or self.forecast is None \
                or self.jaeger_fail >= self.p.jaeger_fail_cycles_before_hold:
            return "hold"
        return "gatgru-ppo"                                      # shadow cũng tính quyết định PPO, không thực thi

    def decide(self, now: float) -> dict:
        services = self.p.scaled_services
        state = self.k8s.state(services)
        current = {s: state[s]["replicas"] for s in services}
        since = {s: now - self.last_scale.get(s, -1e9) for s in services}
        used = self._policy_used()
        record = {"ts": now, "mode": self.p.mode, "policy_used": used, "current": current,
                  "quality": self.quality, "forecast_mae": self.errors.mae()}

        if used == "hold":
            proposed, max_step = dict(current), 0
        else:  # gatgru-ppo
            obs = build_observation(self.ppo.contract, self.arrays, self.forecast, self.fc.targets,
                                    current, since, self.p.slo_latency_p95_ms, self.p.slo_error_rate)
            raw = self.ppo.deltas(obs)
            proposed = {s: current[s] + raw.get(s, 0) for s in services}
            max_step = self.p.max_step
            record["observation"] = [round(float(x), 4) for x in obs]
            record["action_raw"] = raw

        final, notes = self.safety.apply(current, proposed, now, self.last_scale, max_step)
        execute = self.p.mode != "shadow" and self.blocked_reason is None and used != "hold"
        changed = {s: final[s] for s in services if final[s] != current[s]}
        done, failed = {}, {}
        if execute:
            for s, n in changed.items():
                try:
                    self.k8s.scale(s, n)
                    self.last_scale[s] = now
                    done[s] = n
                    tm.SCALES.labels(s, "up" if n > current[s] else "down").inc()
                except Exception as exc:  # noqa: BLE001
                    log.error("PATCH scale %s thất bại: %s", s, exc)
                    failed[s] = str(exc)

        for s in services:
            tm.ACTION_RAW.labels(s).set(proposed[s] - current[s])
            tm.ACTION.labels(s).set(final[s] - current[s])
            tm.TARGET.labels(s).set(final[s])
        tm.DECISIONS.labels(used, str(execute and bool(done)).lower()).inc()

        if self.forecast is not None:
            j = self.fc.targets.index("cpu_cores")
            k = self.fc.targets.index("rps_in")
            record["forecast"] = {s: {"rps_t1": round(float(self.forecast[0, fd.SERVICES.index(s), k]), 3),
                                      "rps_t6": round(float(self.forecast[-1, fd.SERVICES.index(s), k]), 3),
                                      "cpu_max": round(float(self.forecast[:, fd.SERVICES.index(s), j].max()), 4)}
                                  for s in services}
        record.update({"proposed": proposed, "final": final, "notes": notes, "executed": execute,
                       "scaled": done, "failed": failed,
                       "blocked_reason": self.blocked_reason,
                       "data_step_last": self.arrays.step_ts[-1] if self.arrays is not None else None})
        if self.dlog:
            self.dlog.write(record)
        log.info("quyết định [%s/%s] thay đổi=%s %s", self.p.mode, used, changed or "{}",
                 "(thực thi)" if execute else "(không thực thi)")
        return record

    def cycle(self, now: float) -> dict | None:
        self.observe(now)
        if now >= self.next_decision:
            self.next_decision = now + self.p.decision_period_sec
            try:
                return self.decide(now)
            except Exception as exc:  # noqa: BLE001
                log.exception("Plan/Execute lỗi: %s", exc)
        return None


def startup(policy: Policy):
    """Kiểm tra gói model, nạp GAT-GRU và PPO. Lỗi ở đây làm pod khởi động thất bại."""
    n = verify_checksums(policy.gatgru_dir)
    log.info("GAT-GRU: checksum %d file OK", n)
    fc = Forecaster(policy.gatgru_dir, policy.gatgru_checkpoint)
    st = fc.self_test()
    log.info("GAT-GRU: self-test OK (max_abs_diff=%.2e), window=%d, horizon=%d", st["max_abs_diff"], fc.window, fc.horizon)

    ppo = PPOPolicy(policy.ppo_dir, policy.ppo_policy, fc.model_version)
    log.info("PPO: %s (contract %s, dummy=%s)", policy.ppo_policy, ppo.contract["contract_id"], ppo.dummy)
    if list(ppo.contract["scaled_services"]) != list(policy.scaled_services):
        raise ValueError("scaled_services của PPO khác ConfigMap")
    b = ppo.contract["replica_bounds"]
    if (b["min"], b["max"], b["max_total"], b.get("max_step", policy.max_step)) != (
            policy.min_replicas, policy.max_replicas, policy.max_total_replicas, policy.max_step):
        log.warning("replica_bounds của PPO %s khác ConfigMap; lớp an toàn dùng ConfigMap", b)
    return fc, ppo


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    policy = load_policy()
    log.info("Chính sách: mode=%s, %d service, replica %d..%d, trần tổng %d, max_step %d",
             policy.mode, len(policy.scaled_services), policy.min_replicas, policy.max_replicas,
             policy.max_total_replicas, policy.max_step)
    health = tm.Health(stale_after=3 * policy.step_sec + 30)
    tm.serve(policy.http_port, health)

    from .execute import K8sClient
    from .monitor import Monitor

    fc, ppo = startup(policy)
    monitor = Monitor(policy)
    if not policy.restore_history_on_start:
        monitor.min_bucket = int(time.time() // policy.step_sec) * policy.step_sec
    ctrl = Controller(policy, monitor, K8sClient(policy.namespace, policy.field_manager), fc, ppo,
                      tm.DecisionLog(policy.decision_log_dir), health)
    tm.INFO.labels(policy.mode, "gatgru-ppo", ppo.contract["contract_id"], fc.model_version or "-",
                   str(ppo.dummy).lower()).set(1)

    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    while not stop["flag"]:
        now = time.time()
        ctrl.cycle(now)
        nxt = (math.floor(time.time() / policy.step_sec) + 1) * policy.step_sec + 1   # +1 s sau mốc bước
        while not stop["flag"] and time.time() < nxt:
            time.sleep(min(0.5, max(0.0, nxt - time.time())))
    log.info("Dừng controller")
    return 0


if __name__ == "__main__":
    sys.exit(main())
