"""Monitor: thu telemetry của cửa sổ gần nhất từ Prometheus + Jaeger.

Dùng CHÍNH các hàm của load-testing/collect_metrics.py (đã dùng để thu dataset), rồi đưa kết quả
qua đúng định dạng CSV mà dataset lưu (ghi vào bộ nhớ rồi pd.read_csv). Nhờ vậy dữ liệu online đi
qua cùng đường với dữ liệu huấn luyện, không có bản cài đặt thứ hai để lệch.

Cửa sổ: n = history_steps + WARMUP_DROP bước 10 s (24 bước = 240 s). build_run_arrays bỏ 6 bước
đầu (warm-up) giống lúc huấn luyện, phần còn lại đúng 18 bước cho GAT-GRU. Chỉ dùng các bước đã kết
thúc ít nhất ingest_lag_sec giây, vì span Jaeger đến trễ.

Jaeger được thu theo từng bước và lưu đệm: mỗi chu kỳ chỉ truy vấn bước mới (khoảng 10 request),
không truy vấn lại cả cửa sổ. Prometheus truy vấn lại cả cửa sổ (12 query_range, rẻ).
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field

import pandas as pd

import collect_metrics as cm
import forecast_data as fd

log = logging.getLogger("autoscaler.monitor")

NODE_COLS = ["timestamp", "service"] + cm.NODE_FIELDS
SVC_COLS = ["timestamp", "service", "request_count", "error_count", "latency_p50_ms", "latency_p95_ms"]
EDGE_COLS = ["timestamp", "source", "target", "call_count", "error_count", "latency_p50_ms", "latency_p95_ms"]


@dataclass
class Snapshot:
    start: int                      # đầu bước đầu tiên (unix s, chia hết cho step)
    end: int                        # cuối cửa sổ (không gồm)
    step: int
    node: pd.DataFrame
    svc: pd.DataFrame
    edge: pd.DataFrame
    quality: dict = field(default_factory=dict)

    @property
    def n_steps(self) -> int:
        return (self.end - self.start) // self.step


def _frame(header, rows) -> pd.DataFrame:
    """Đi qua đúng định dạng CSV của dataset (giống collect_metrics.write_csv + fd._read)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    buf.seek(0)
    return pd.read_csv(buf)


def node_frame(rows: dict) -> pd.DataFrame:
    """rows: {(ts, svc): {field: value}} như collect_metrics.collect_prometheus trả về."""
    return _frame(NODE_COLS, [[ts, svc] + [cm._r(f.get(k)) for k in cm.NODE_FIELDS]
                              for (ts, svc), f in sorted(rows.items())])


def svc_frame(inbound: dict) -> pd.DataFrame:
    return _frame(SVC_COLS, [[ts, s, a["n"], a["err"], cm._r(cm._pct(a["lat"], .5), 3), cm._r(cm._pct(a["lat"], .95), 3)]
                             for (ts, s), a in sorted(inbound.items())])


def edge_frame(edges: dict) -> pd.DataFrame:
    return _frame(EDGE_COLS, [[ts, s, d, a["n"], a["err"], cm._r(cm._pct(a["lat"], .5), 3), cm._r(cm._pct(a["lat"], .95), 3)]
                              for (ts, s, d), a in sorted(edges.items())])


class Monitor:
    def __init__(self, policy):
        self.p = policy
        self.step = policy.step_sec
        self.n_steps = policy.history_steps + fd.WARMUP_DROP
        self._jaeger = {}            # bucket_ts -> (edges dict, inbound dict, ok)
        # restore_history_on_start=false: không thu các bước trước lúc controller khởi động,
        # nên phải chờ đủ cửa sổ (240 s) mới có dữ liệu hợp lệ.
        self.min_bucket = None

    def bounds(self, now: float) -> tuple[int, int]:
        end = int((now - self.p.ingest_lag_sec) // self.step) * self.step
        return end - self.n_steps * self.step, end

    def _jaeger_bucket(self, b: int):
        report = {"jaeger_truncated_windows": 0}
        edges, inbound = cm.collect_jaeger(self.p.jaeger_url, self.p.namespace, b, b + self.step, self.step,
                                           self.p.trace_limit, report)
        ok = bool(report.get("jaeger_services"))
        return edges, inbound, ok, report.get("jaeger_truncated_windows", 0)

    def fetch(self, now: float) -> Snapshot:
        start, end = self.bounds(now)

        # Prometheus: cả cửa sổ; điểm tại start, start+step, ..., end-step
        prom_report = {"prometheus_series": {}}
        rows = cm.collect_prometheus(self.p.prometheus_url, self.p.namespace, start, end - self.step,
                                     self.step, self.p.rate_window, prom_report)

        # Jaeger: thu bước còn thiếu, bỏ bước đã ra khỏi cửa sổ
        for b in [b for b in self._jaeger if b < start]:
            del self._jaeger[b]
        todo = [b for b in range(start, end, self.step)
                if b not in self._jaeger and (self.min_bucket is None or b >= self.min_bucket)]
        truncated = 0
        for b in todo:
            e, i, ok, tr = self._jaeger_bucket(b)
            truncated += tr
            if ok:                                  # chỉ lưu đệm khi Jaeger trả được danh sách service
                self._jaeger[b] = (e, i, ok)
        edges, inbound = {}, {}
        for b in range(start, end, self.step):
            if b in self._jaeger:
                edges.update(self._jaeger[b][0])
                inbound.update(self._jaeger[b][1])

        last = end - self.step
        services_last = {svc for (ts, svc), f in rows.items() if ts == last and f.get("cpu_cores") is not None}
        quality = {
            "prometheus_ok": len(services_last) >= len(self.p.scaled_services),
            "prometheus_services_last_step": len(services_last),
            "prometheus_empty_queries": [k for k, v in prom_report["prometheus_series"].items() if v == 0],
            "jaeger_ok": all(b in self._jaeger for b in range(start, end, self.step)),
            "jaeger_buckets_cached": sum(1 for b in range(start, end, self.step) if b in self._jaeger),
            "jaeger_new_buckets": len(todo),
            "jaeger_truncated_windows": truncated,
        }
        return Snapshot(start, end, self.step, node_frame(rows), svc_frame(inbound), edge_frame(edges), quality)
