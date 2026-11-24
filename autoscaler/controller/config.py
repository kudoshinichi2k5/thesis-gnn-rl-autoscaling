"""Đọc chính sách controller từ ConfigMap autoscaler-policy.

ConfigMap được mount thành thư mục (mặc định /config): mỗi key là một file. Biến môi trường
AUTOSCALER_<KEY_IN_HOA> (ví dụ AUTOSCALER_MODE=shadow) ghi đè giá trị trong file, tiện khi test.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS = {
    "mode": "shadow",
    "namespace": "online-boutique",
    "prometheus_url": "http://localhost:9090",
    "jaeger_url": "http://localhost:16686",
    "step_sec": "10",
    "decision_period_sec": "30",
    "history_steps": "18",
    "restore_history_on_start": "true",
    "ingest_lag_sec": "20",             # chỉ dùng bước 10 s đã kết thúc ít nhất 20 s (span Jaeger đến trễ)
    "rate_window": "40s",               # giống collect_metrics.py
    "trace_limit": "1500",
    "scaled_services": "frontend,cartservice,checkoutservice,currencyservice,productcatalogservice,"
                       "recommendationservice,adservice,emailservice,paymentservice,shippingservice",
    "fixed_services": "redis-cart",
    "min_replicas": "1",
    "max_replicas": "15",
    "max_total_replicas": "26",
    "max_step": "2",
    "scale_down_cooldown_sec": "120",
    "slo_latency_p95_ms": "300",
    "slo_error_rate": "0.01",
    "gatgru_checkpoint": "/models/gatgru/v05/gatgru_selected_model_seed_0.pt",
    "gatgru_dir": "/models/gatgru/v05",
    "ppo_policy": "/models/ppo/v00-dummy/policy.zip",
    "ppo_dir": "/models/ppo/v00-dummy",
    "allow_active_with_dummy": "false",
    "field_manager": "gnn-rl-autoscaler",
    "jaeger_fail_cycles_before_hold": "3",
    "decision_log_dir": "/decision-log",
    "http_port": "8000",
}

MODES = ("shadow", "gatgru-ppo")


def _bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _list(v: str) -> list[str]:
    return [x.strip() for x in str(v).split(",") if x.strip()]


@dataclass
class Policy:
    raw: dict = field(repr=False)
    mode: str = "shadow"
    namespace: str = "online-boutique"
    prometheus_url: str = ""
    jaeger_url: str = ""
    step_sec: int = 10
    decision_period_sec: int = 30
    history_steps: int = 18
    restore_history_on_start: bool = True
    ingest_lag_sec: int = 20
    rate_window: str = "40s"
    trace_limit: int = 1500
    scaled_services: list = field(default_factory=list)
    fixed_services: list = field(default_factory=list)
    min_replicas: int = 1
    max_replicas: int = 15
    max_total_replicas: int = 26
    max_step: int = 2
    scale_down_cooldown_sec: int = 120
    slo_latency_p95_ms: float = 300.0
    slo_error_rate: float = 0.01
    gatgru_checkpoint: str = ""
    gatgru_dir: str = ""
    ppo_policy: str = ""
    ppo_dir: str = ""
    allow_active_with_dummy: bool = False
    field_manager: str = "gnn-rl-autoscaler"
    jaeger_fail_cycles_before_hold: int = 3
    decision_log_dir: str = "/decision-log"
    http_port: int = 8000

    @classmethod
    def from_dict(cls, d: dict) -> "Policy":
        v = {**DEFAULTS, **{k: str(x) for k, x in d.items()}}
        p = cls(
            raw=v,
            mode=v["mode"].strip(),
            namespace=v["namespace"].strip(),
            prometheus_url=v["prometheus_url"].strip(),
            jaeger_url=v["jaeger_url"].strip(),
            step_sec=int(v["step_sec"]),
            decision_period_sec=int(v["decision_period_sec"]),
            history_steps=int(v["history_steps"]),
            restore_history_on_start=_bool(v["restore_history_on_start"]),
            ingest_lag_sec=int(v["ingest_lag_sec"]),
            rate_window=v["rate_window"].strip(),
            trace_limit=int(v["trace_limit"]),
            scaled_services=_list(v["scaled_services"]),
            fixed_services=_list(v["fixed_services"]),
            min_replicas=int(v["min_replicas"]),
            max_replicas=int(v["max_replicas"]),
            max_total_replicas=int(v["max_total_replicas"]),
            max_step=int(v["max_step"]),
            scale_down_cooldown_sec=int(v["scale_down_cooldown_sec"]),
            slo_latency_p95_ms=float(v["slo_latency_p95_ms"]),
            slo_error_rate=float(v["slo_error_rate"]),
            gatgru_checkpoint=v["gatgru_checkpoint"].strip(),
            gatgru_dir=v["gatgru_dir"].strip(),
            ppo_policy=v["ppo_policy"].strip(),
            ppo_dir=v["ppo_dir"].strip(),
            allow_active_with_dummy=_bool(v["allow_active_with_dummy"]),
            field_manager=v["field_manager"].strip(),
            jaeger_fail_cycles_before_hold=int(v["jaeger_fail_cycles_before_hold"]),
            decision_log_dir=v["decision_log_dir"].strip(),
            http_port=int(v["http_port"]),
        )
        p.validate()
        return p

    def validate(self) -> None:
        errs = []
        if self.mode not in MODES:
            errs.append(f"mode '{self.mode}' không hợp lệ, chọn một trong {MODES}")
        if self.step_sec <= 0 or self.decision_period_sec < self.step_sec:
            errs.append("cần step_sec > 0 và decision_period_sec >= step_sec")
        if not (1 <= self.min_replicas <= self.max_replicas):
            errs.append("cần 1 <= min_replicas <= max_replicas")
        if set(self.scaled_services) & set(self.fixed_services):
            errs.append("một service vừa nằm trong scaled_services vừa trong fixed_services")
        if not self.scaled_services:
            errs.append("scaled_services rỗng")
        if self.max_total_replicas < len(self.scaled_services) * self.min_replicas:
            errs.append("max_total_replicas nhỏ hơn tổng min_replicas")
        if errs:
            raise ValueError("Chính sách không hợp lệ: " + "; ".join(errs))


def load_policy(config_dir: str | None = None) -> Policy:
    config_dir = config_dir or os.environ.get("CONFIG_DIR", "/config")
    values: dict[str, str] = {}
    d = Path(config_dir)
    if d.is_dir():
        for f in d.iterdir():
            # ConfigMap mount có các mục ẩn "..data", "..2024_..." : bỏ qua
            if f.name.startswith(".") or not f.is_file():
                continue
            values[f.name] = f.read_text(encoding="utf-8").strip()
    for k in DEFAULTS:
        env = os.environ.get("AUTOSCALER_" + k.upper())
        if env is not None:
            values[k] = env
    return Policy.from_dict(values)
