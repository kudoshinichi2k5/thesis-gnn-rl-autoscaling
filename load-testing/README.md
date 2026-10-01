# Locust load testing

Locust runs on the standalone `node-loadgen` VM, outside the K3s cluster. It drives the Online Boutique frontend and writes headless CSV/HTML reports under `load-testing/results/` locally after each remote run.

## Frontend URL and port

`cluster-setup/00-generate-node-ips.sh` reads the actual `nodePort` from `frontend-external` and writes:

```text
FRONTEND_NODE_PORT="<current NodePort>"
FRONTEND_URL="http://<node-app Floating IP>:<current NodePort>"
```

For the currently inspected service, `kubectl get svc frontend-external -n online-boutique` showed Service port `80` and NodePort `32058`. The LoadBalancer status listed the two worker private IPs, not the control-plane. Therefore this setup targets the control-plane Floating IP on the service's NodePort; the existing Neutron `nodeport` rule covers `30000-32767`. Always regenerate `node-ips.env` after recreating the service or changing node IPs.

Run `kubectl get svc frontend-external -n online-boutique` to inspect the current mapping. Service port `80` is the port inside the LoadBalancer service; when addressing a node IP directly, use the `nodePort` shown after the colon.

## Scenarios

All scenarios last 10 minutes. Each user browses the home page, views random products, views/adds items in a cookie-backed cart, occasionally checks out, and occasionally changes currency. Task weights make browsing more common than checkout.

- `normal`: 20 users, ramped at 1 user/second, then held steady. This is the baseline for resource use and scale-up latency.
- `spike`: 10 users for 3 minutes, ramp to 100 over about 5 seconds, hold for 3 minutes, then rapidly return to 10. The sharp rise illustrates proactive scaling versus reactive HPA delay.
- `bursty`: alternates 10 and 60 users every 60 seconds for 10 minutes. Repeated transitions expose scale lag and replica oscillation.

Each `scenarios/*.py` file defines exactly one `LoadTestShape`. The runner loads only the requested shape file alongside `locustfile.py`. Locust's `--class-picker` is a web-UI feature, not a headless shape selector, so the script intentionally selects scenarios through `-f locustfile.py,scenarios/<scenario>.py`.

## Install and run

Run from WSL at repository root after K3s, frontend and `node-ips.env` are ready. The first command installs Ubuntu Python/venv/pip and pinned Locust on `node-loadgen`, then syncs the test files:

```bash
bash cluster-setup/06-setup-loadgen.sh
```

The SSH key may prompt for its passphrase. To avoid repeated prompts, load it into `ssh-agent` in your WSL session first.

Run one scenario at a time; the script checks frontend reachability from `node-loadgen` before starting Locust:

```bash
bash load-testing/run-scenario.sh normal
bash load-testing/run-scenario.sh spike
bash load-testing/run-scenario.sh bursty
```

The runner uses headless mode and produces `results/<scenario>_test1_stats.csv`, related CSVs and `results/<scenario>_test1.html`. Running the same scenario again replaces that sample set. Results and the remote venv are gitignored.

`requirements.txt` pins Locust to `2.31.8`; Python dependencies are installed into `~/load-testing/.venv` on the load generator.

## Troubleshooting

If the runner reports a frontend connection error, verify in this order:

1. `kubectl get svc frontend-external -n online-boutique` and regenerate `node-ips.env` to refresh the NodePort.
2. Confirm the Neutron security group allows the NodePort range `30000-32767` (the current Terraform rules already include it).
3. Confirm frontend pods are `Running`: `kubectl get pods -n online-boutique`.
4. From `node-loadgen`, test the generated `FRONTEND_URL` with `curl` before rerunning Locust.
