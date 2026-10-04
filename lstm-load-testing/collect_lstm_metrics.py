"""
collect_lstm_metrics.py
Export the telemetry of ONE finished run with only the signals the LSTM baseline uses:

  node_metrics.csv     Prometheus (cAdvisor + kube-state-metrics): CPU, memory, network,
                       throttling, request/limit, replicas, restarts per service
  service_metrics.csv  Jaeger server spans: inbound requests, errors, p50/p95 latency per
                       service (rps_in is a forecasting target, so Jaeger is still required)
  collect_report.json  coverage summary

edge_metrics.csv (caller -> callee) is NOT written: the LSTM has no graph.

The queries are imported from load-testing/collect_metrics.py so that both datasets are
computed by exactly the same code (same PromQL, same span parsing).

Usage (on node-loadgen):
  ~/load-testing/.venv/bin/python collect_lstm_metrics.py --load-testing-dir ~/load-testing \
      --start <unix> --end <unix> --out results/spike/run_01 \
      --prometheus http://<obs-ip>:9090 --jaeger http://<obs-ip>:16686
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path


def main():
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--load-testing-dir', default=os.environ.get('LOAD_TESTING_DIR', str(here.parent / 'load-testing')))
    p.add_argument('--start', type=int, required=True)
    p.add_argument('--end', type=int, required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--prometheus', default=os.environ.get('PROMETHEUS_URL', 'http://localhost:9090'))
    p.add_argument('--jaeger', default=os.environ.get('JAEGER_URL', 'http://localhost:16686'))
    p.add_argument('--namespace', default=os.environ.get('NAMESPACE', 'online-boutique'))
    p.add_argument('--step', type=int, default=int(os.environ.get('STEP_SEC', 10)))
    p.add_argument('--rate-window', default=os.environ.get('RATE_WINDOW', '40s'))
    p.add_argument('--trace-limit', type=int, default=int(os.environ.get('TRACE_LIMIT', 1500)))
    args = p.parse_args()

    sys.path.insert(0, args.load_testing_dir)
    import collect_metrics as cm  # noqa: E402  (shared queries / span parsing)

    start = args.start - args.start % args.step
    os.makedirs(args.out, exist_ok=True)
    report = {'start': start, 'end': args.end, 'step': args.step, 'pipeline': 'lstm',
              'prometheus_series': {}, 'jaeger_truncated_windows': 0}
    t0 = time.time()

    node = cm.collect_prometheus(args.prometheus, args.namespace, start, args.end, args.step,
                                 args.rate_window, report)
    cm.write_csv(os.path.join(args.out, 'node_metrics.csv'), ['timestamp', 'service'] + cm.NODE_FIELDS,
                 [[ts, svc] + [cm._r(f.get(k)) for k in cm.NODE_FIELDS] for (ts, svc), f in sorted(node.items())])

    _edges, inbound = cm.collect_jaeger(args.jaeger, args.namespace, start, args.end, args.step,
                                        args.trace_limit, report)
    cm.write_csv(os.path.join(args.out, 'service_metrics.csv'),
                 ['timestamp', 'service', 'request_count', 'error_count', 'latency_p50_ms', 'latency_p95_ms'],
                 [[ts, s, a['n'], a['err'], cm._r(cm._pct(a['lat'], .5), 3), cm._r(cm._pct(a['lat'], .95), 3)]
                  for (ts, s), a in sorted(inbound.items())])

    report.update(node_rows=len(node), service_rows=len(inbound), elapsed_sec=round(time.time() - t0, 1))
    with open(os.path.join(args.out, 'collect_report.json'), 'w') as f:
        json.dump(report, f, indent=2)

    empty = [k for k, v in report['prometheus_series'].items() if v == 0]
    print(f"[collect-lstm] node rows={len(node)} inbound={len(inbound)} "
          f"truncated={report['jaeger_truncated_windows']} in {report['elapsed_sec']}s")
    if empty:
        print(f"[collect-lstm] WARN empty Prometheus groups: {', '.join(empty)}", file=sys.stderr)
    if not node or not inbound:
        sys.exit('[collect-lstm] missing Prometheus or Jaeger data for this window')


if __name__ == '__main__':
    main()
