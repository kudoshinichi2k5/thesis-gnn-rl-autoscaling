"""
collect_metrics.py
Export the telemetry of ONE finished load-test run into CSV files, on a fixed
time grid (STEP seconds). Runs on node-loadgen right after Locust exits, so it
adds no load to the cluster while the test is running.

Sources (all already deployed by cluster-setup/):
  * Prometheus  (node-observability:9090) - cAdvisor on every node + kube-state-metrics
  * Jaeger      (node-observability:16686) - Envoy/Istio spans, 100% sampling

Outputs in --out:
  node_metrics.csv     timestamp, service, resource/capacity features (Prometheus)
  edge_metrics.csv     timestamp, source, target, call_count, error_count, p50/p95 latency (Jaeger)
  service_metrics.csv  timestamp, service, inbound count/errors/p50/p95 latency (Jaeger)
  collect_report.json  coverage summary (series found, empty windows, truncated windows)

Usage:
  python collect_metrics.py --start 1727700000 --end 1727701800 --out results/spike/run_01 \
      --prometheus http://10.42.0.93:9090 --jaeger http://10.42.0.93:16686
"""
import argparse
import csv
import json
import math
import os
import re
import sys
import time
from collections import defaultdict

import requests

SERVICES = [
    "adservice", "cartservice", "checkoutservice", "currencyservice",
    "emailservice", "frontend", "paymentservice", "productcatalogservice",
    "recommendationservice", "redis-cart", "shippingservice",
]
# Services that emit Envoy spans (redis-cart is plain TCP: no HTTP/gRPC spans).
TRACED_SERVICES = [s for s in SERVICES if s != "redis-cart"]

# Deployment pods are named <deployment>-<replicaset hash>-<pod suffix>.
POD_RE = re.compile(r"^(?P<deploy>.+)-[a-z0-9]{5,10}-[a-z0-9]{5}$")
APP_CONTAINERS = 'container!~"istio-proxy|istio-init|POD|"'


def prom_queries(ns: str, window: str) -> dict:
    """name -> (promql, label to group by). Pod-keyed results are folded into services."""
    app = f'namespace="{ns}", {APP_CONTAINERS}'
    return {
        "cpu_cores": (f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{app}}}[{window}]))', "pod"),
        "cpu_sidecar_cores": (
            f'sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace="{ns}", container="istio-proxy"}}[{window}]))',
            "pod"),
        "cpu_throttled_periods": (
            f'sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{{app}}}[{window}]))', "pod"),
        "cpu_total_periods": (f'sum by (pod) (rate(container_cpu_cfs_periods_total{{{app}}}[{window}]))', "pod"),
        "mem_bytes": (f'sum by (pod) (container_memory_working_set_bytes{{{app}}})', "pod"),
        # Network counters are per pod sandbox; max() avoids double counting if
        # cAdvisor repeats them per container.
        "net_rx_bps": (
            f'max by (pod) (rate(container_network_receive_bytes_total{{namespace="{ns}", interface="eth0"}}[{window}]))',
            "pod"),
        "net_tx_bps": (
            f'max by (pod) (rate(container_network_transmit_bytes_total{{namespace="{ns}", interface="eth0"}}[{window}]))',
            "pod"),
        "cpu_request_cores": (
            f'sum by (pod) (kube_pod_container_resource_requests{{namespace="{ns}", resource="cpu", container!="istio-proxy"}})',
            "pod"),
        "cpu_limit_cores": (
            f'sum by (pod) (kube_pod_container_resource_limits{{namespace="{ns}", resource="cpu", container!="istio-proxy"}})',
            "pod"),
        "restarts_total": (
            f'sum by (pod) (kube_pod_container_status_restarts_total{{namespace="{ns}", container!="istio-proxy"}})',
            "pod"),
        "replicas": (f'kube_deployment_status_replicas_available{{namespace="{ns}"}}', "deployment"),
        "replicas_desired": (f'kube_deployment_spec_replicas{{namespace="{ns}"}}', "deployment"),
    }


NODE_FIELDS = [
    "cpu_cores", "cpu_sidecar_cores", "cpu_throttle_ratio", "mem_bytes", "net_rx_bps",
    "net_tx_bps", "cpu_request_cores", "cpu_limit_cores", "restarts_total",
    "replicas", "replicas_desired",
]


def _service_of(key: str, by: str):
    if by == "deployment":
        return key if key in SERVICES else None
    m = POD_RE.match(key)
    return m.group("deploy") if m and m.group("deploy") in SERVICES else None


def _float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def collect_prometheus(base: str, ns: str, start: int, end: int, step: int, window: str, report: dict):
    """Return {(ts, service): {field: value}} on the step grid."""
    rows = defaultdict(dict)
    url = f"{base.rstrip('/')}/api/v1/query_range"
    for name, (promql, by) in prom_queries(ns, window).items():
        try:
            r = requests.get(url, params={"query": promql, "start": start, "end": end, "step": step}, timeout=60)
            r.raise_for_status()
            result = r.json()["data"]["result"]
        except Exception as exc:
            print(f"[collect] WARN prometheus '{name}' failed: {exc}", file=sys.stderr)
            result = []
        report["prometheus_series"][name] = len(result)
        acc = defaultdict(float)
        for series in result:
            svc = _service_of(series["metric"].get(by, ""), by)
            if svc is None:
                continue
            for ts, val in series["values"]:
                f = _float(val)
                if f is not None:
                    acc[(int(float(ts)), svc)] += f
        for key, val in acc.items():
            rows[key][name] = val

    for fields in rows.values():
        thr, per = fields.pop("cpu_throttled_periods", None), fields.pop("cpu_total_periods", None)
        fields["cpu_throttle_ratio"] = (thr / per) if thr is not None and per else 0.0
    return rows


# ---------------------------------------------------------------- Jaeger -----
def _tags(span: dict) -> dict:
    return {t["key"]: t.get("value") for t in span.get("tags", [])}


def _svc_name(process_service: str):
    # Istio default tracing service name is "<app>.<namespace>".
    name = process_service.split(".")[0]
    return name if name in SERVICES else None


def _upstream_service(tags: dict):
    # e.g. "outbound|3550||productcatalogservice.online-boutique.svc.cluster.local"
    cluster = tags.get("upstream_cluster") or tags.get("upstream_cluster.name") or ""
    parts = cluster.split("|")
    if len(parts) == 4 and parts[0] == "outbound":
        name = parts[3].split(".")[0]
        return name if name in SERVICES else None
    return None


def _is_error(tags: dict) -> bool:
    if str(tags.get("error", "")).lower() == "true":
        return True
    try:
        if int(tags.get("http.status_code", 0)) >= 500:
            return True
    except (TypeError, ValueError):
        pass
    grpc = tags.get("grpc.status_code")
    return grpc not in (None, "", "0", 0)


def fetch_traces(base: str, service: str, start_us: int, end_us: int, limit: int, report: dict, depth=0):
    """GET /api/traces for one service and window; split the window when the limit is hit."""
    url = f"{base.rstrip('/')}/api/traces"
    params = {"service": f"{service}", "start": start_us, "end": end_us, "limit": limit}
    try:
        r = requests.get(url, params=params, timeout=120)
        r.raise_for_status()
        data = r.json().get("data") or []
    except Exception as exc:
        print(f"[collect] WARN jaeger {service} [{start_us},{end_us}) failed: {exc}", file=sys.stderr)
        return []
    if len(data) >= limit:
        if end_us - start_us > 1_000_000 and depth < 4:
            mid = (start_us + end_us) // 2
            return (fetch_traces(base, service, start_us, mid, limit, report, depth + 1)
                    + fetch_traces(base, service, mid, end_us, limit, report, depth + 1))
        report["jaeger_truncated_windows"] += 1
    return data


def jaeger_service_names(base: str, ns: str) -> dict:
    """Map short service name -> name registered in Jaeger (e.g. frontend.online-boutique)."""
    try:
        r = requests.get(f"{base.rstrip('/')}/api/services", timeout=30)
        r.raise_for_status()
        registered = r.json().get("data") or []
    except Exception as exc:
        print(f"[collect] WARN cannot list Jaeger services: {exc}", file=sys.stderr)
        registered = []
    mapping = {}
    for svc in TRACED_SERVICES:
        for cand in (f"{svc}.{ns}", svc):
            if cand in registered:
                mapping[svc] = cand
                break
    return mapping


def _pct(values, q):
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, max(0, int(math.ceil(q * len(values))) - 1))
    return values[idx]


def collect_jaeger(base: str, ns: str, start: int, end: int, step: int, limit: int, report: dict):
    """Bucket Envoy spans by start time into step windows.
    Edge = client span of service A whose upstream is service B (or whose child
    server span belongs to B). Inbound = server spans of a service."""
    names = jaeger_service_names(base, ns)
    report["jaeger_services"] = names
    if not names:
        return {}, {}

    edges = defaultdict(lambda: {"n": 0, "err": 0, "lat": []})
    inbound = defaultdict(lambda: {"n": 0, "err": 0, "lat": []})
    seen = set()

    for w_start in range(start, end, step):
        w_end = min(w_start + step, end)
        for svc, jaeger_name in names.items():
            for trace in fetch_traces(base, jaeger_name, w_start * 1_000_000, w_end * 1_000_000, limit, report):
                procs = {pid: _svc_name(p.get("serviceName", "")) for pid, p in trace.get("processes", {}).items()}
                spans = trace.get("spans", [])
                children = defaultdict(list)
                for s in spans:
                    for ref in s.get("references", []):
                        if ref.get("refType") == "CHILD_OF":
                            children[ref.get("spanID")].append(s)
                for s in spans:
                    if s["spanID"] in seen:
                        continue
                    seen.add(s["spanID"])
                    bucket = start + ((s["startTime"] // 1_000_000 - start) // step) * step
                    if not (start <= bucket < end):
                        continue
                    owner = procs.get(s.get("processID"))
                    if owner is None:
                        continue
                    tags = _tags(s)
                    kind = tags.get("span.kind")
                    lat_ms = s.get("duration", 0) / 1000.0
                    err = _is_error(tags)
                    if kind == "server":
                        acc = inbound[(bucket, owner)]
                    elif kind == "client":
                        dst = _upstream_service(tags)
                        if dst is None:
                            for child in children.get(s["spanID"], []):
                                dst = procs.get(child.get("processID"))
                                if dst:
                                    break
                        if dst is None or dst == owner:
                            continue
                        acc = edges[(bucket, owner, dst)]
                    else:
                        continue
                    acc["n"] += 1
                    acc["err"] += int(err)
                    acc["lat"].append(lat_ms)
    return edges, inbound


# ----------------------------------------------------------------- output ----
def write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _r(v, nd=6):
    return "" if v is None else round(v, nd)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", type=int, required=True, help="run start, unix seconds")
    p.add_argument("--end", type=int, required=True, help="run end, unix seconds")
    p.add_argument("--out", required=True)
    p.add_argument("--prometheus", default=os.environ.get("PROMETHEUS_URL", "http://localhost:9090"))
    p.add_argument("--jaeger", default=os.environ.get("JAEGER_URL", "http://localhost:16686"))
    p.add_argument("--namespace", default=os.environ.get("NAMESPACE", "online-boutique"))
    p.add_argument("--step", type=int, default=int(os.environ.get("STEP_SEC", 10)))
    p.add_argument("--rate-window", default=os.environ.get("RATE_WINDOW", "40s"),
                   help="rate() window, >= 3x the 10s scrape interval")
    p.add_argument("--trace-limit", type=int, default=int(os.environ.get("TRACE_LIMIT", 1500)))
    p.add_argument("--skip-jaeger", action="store_true")
    args = p.parse_args()

    start = args.start - args.start % args.step
    end = args.end
    os.makedirs(args.out, exist_ok=True)
    report = {"start": start, "end": end, "step": args.step, "prometheus_series": {},
              "jaeger_truncated_windows": 0}
    t0 = time.time()

    node = collect_prometheus(args.prometheus, args.namespace, start, end, args.step, args.rate_window, report)
    write_csv(
        os.path.join(args.out, "node_metrics.csv"),
        ["timestamp", "service"] + NODE_FIELDS,
        [[ts, svc] + [_r(fields.get(k)) for k in NODE_FIELDS] for (ts, svc), fields in sorted(node.items())],
    )

    if not args.skip_jaeger:
        edges, inbound = collect_jaeger(args.jaeger, args.namespace, start, end, args.step,
                                        args.trace_limit, report)
        write_csv(
            os.path.join(args.out, "edge_metrics.csv"),
            ["timestamp", "source", "target", "call_count", "error_count", "latency_p50_ms", "latency_p95_ms"],
            [[ts, s, d, a["n"], a["err"], _r(_pct(a["lat"], .5), 3), _r(_pct(a["lat"], .95), 3)]
             for (ts, s, d), a in sorted(edges.items())],
        )
        write_csv(
            os.path.join(args.out, "service_metrics.csv"),
            ["timestamp", "service", "request_count", "error_count", "latency_p50_ms", "latency_p95_ms"],
            [[ts, s, a["n"], a["err"], _r(_pct(a["lat"], .5), 3), _r(_pct(a["lat"], .95), 3)]
             for (ts, s), a in sorted(inbound.items())],
        )
        report["edge_rows"] = len(edges)
        report["service_rows"] = len(inbound)

    report["node_rows"] = len(node)
    report["elapsed_sec"] = round(time.time() - t0, 1)
    with open(os.path.join(args.out, "collect_report.json"), "w") as f:
        json.dump(report, f, indent=2)

    empty = [k for k, v in report["prometheus_series"].items() if v == 0]
    print(f"[collect] node rows={len(node)} edges={report.get('edge_rows', '-')} "
          f"inbound={report.get('service_rows', '-')} truncated={report['jaeger_truncated_windows']} "
          f"in {report['elapsed_sec']}s")
    if empty:
        print(f"[collect] WARN empty Prometheus groups: {', '.join(empty)}", file=sys.stderr)
    if not node:
        sys.exit("[collect] no Prometheus data for this window - check targets in Prometheus UI")


if __name__ == "__main__":
    main()
