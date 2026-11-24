"""Knowledge/quan sát: /metrics (Prometheus), /healthz và Decision Log (JSONL)."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, generate_latest

REGISTRY = CollectorRegistry()
FORECAST = Gauge("autoscaler_forecast", "Dự báo GAT-GRU", ["service", "target", "horizon"], registry=REGISTRY)
FORECAST_ERR = Gauge("autoscaler_forecast_error", "MAE online của dự báo t+1 (trượt 10 phút)", ["target"], registry=REGISTRY)
ACTION = Gauge("autoscaler_action", "Δ replica sau lớp an toàn ở quyết định gần nhất", ["service"], registry=REGISTRY)
ACTION_RAW = Gauge("autoscaler_action_raw", "Δ replica đề xuất trước lớp an toàn", ["service"], registry=REGISTRY)
TARGET = Gauge("autoscaler_replicas_target", "Số replica mục tiêu", ["service"], registry=REGISTRY)
DECISIONS = Counter("autoscaler_decisions_total", "Số quyết định", ["mode", "executed"], registry=REGISTRY)
SCALES = Counter("autoscaler_scale_events_total", "Số lần PATCH scale", ["service", "direction"], registry=REGISTRY)
CYCLE = Gauge("autoscaler_cycle_seconds", "Thời gian một chu kỳ Monitor+Analyze", registry=REGISTRY)
DATA_OK = Gauge("autoscaler_data_ok", "1 nếu dữ liệu Prometheus/Jaeger đủ", ["source"], registry=REGISTRY)
INFO = Gauge("autoscaler_info", "Thông tin controller", ["mode", "effective_mode", "contract_id", "gatgru", "ppo_dummy"],
             registry=REGISTRY)


class Health:
    def __init__(self, stale_after: float):
        self.ready = False
        self.reasons: list[str] = ["đang khởi động"]
        self.last_cycle = 0.0
        self.stale_after = stale_after
        self.lock = threading.Lock()

    def set(self, ready: bool, reasons: list[str]):
        with self.lock:
            self.ready, self.reasons = ready, list(reasons)

    def beat(self):
        self.last_cycle = time.time()

    def status(self) -> tuple[bool, dict]:
        with self.lock:
            fresh = time.time() - self.last_cycle < self.stale_after
            ok = self.ready and fresh
            reasons = list(self.reasons) + ([] if fresh else ["vòng lặp không chạy"])
        return ok, {"ok": ok, "reasons": reasons, "last_cycle": self.last_cycle}


def serve(port: int, health: Health) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/metrics"):
                body, ctype, code = generate_latest(REGISTRY), CONTENT_TYPE_LATEST, 200
            elif self.path.startswith("/healthz"):
                ok, st = health.status()
                body, ctype, code = json.dumps(st, ensure_ascii=False).encode(), "application/json", 200 if ok else 503
            else:
                body, ctype, code = b"not found", "text/plain", 404
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # tắt log truy cập
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class DecisionLog:
    """Một dòng JSON mỗi quyết định, file theo ngày: decisions-YYYYMMDD.jsonl."""

    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict) -> None:
        name = time.strftime("decisions-%Y%m%d.jsonl", time.gmtime(record.get("ts", time.time())))
        with open(self.dir / name, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=float) + "\n")
