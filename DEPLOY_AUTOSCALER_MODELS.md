# Triển khai GAT-GRU và PPO lên cụm (có thể gỡ sạch)

Đưa controller autoscaler (GAT-GRU + PPO) vào cụm K3s, **gỡ được hoàn toàn** về trạng thái ban đầu, và chứng minh bằng một **kiểm tra ngắn** rằng mô hình đã chạy thật trong cụm. Chưa có PPO thật nên dùng **PPO mẫu** cùng hợp đồng; có PPO thật chỉ thay file, không build lại image.

- **Phạm vi:** 10 service (mọi service trừ `redis-cart`), mỗi service 1 đến 15 replica, trần tổng **26** (rút từ dataset, mục 0).
- **Hai chế độ controller:** `gatgru-ppo` (GAT-GRU dự báo, PPO quyết định, có scale), `shadow` (cùng đường ống, chỉ ghi log).
- **Không dùng HPA làm dự phòng:** khi autoscaler chủ động chạy, HPA bị xóa và không được bật lại tự động khi có lỗi. Khi thiếu dữ liệu, controller giữ nguyên replica (`hold`); controller không có quyền tạo HPA (RBAC). HPA chỉ xuất hiện ở hai nơi: ngữ cảnh `hpa` để so sánh, và bước khôi phục trạng thái ban đầu do người vận hành chủ động thực hiện.
- **Quyền:** chỉ SSH vào từng node. Mọi `kubectl` chạy trên `node-app` (cần `sudo`).
- **Không sửa `cluster-setup/`** (của thành viên khác). Mọi thứ mới nằm trong [`autoscaler/`](autoscaler/) và namespace `autoscaler`.

> **Chưa chạy thử trên cụm.** Controller đã có và đã qua test offline (Bước 4.2: feature online khớp dữ liệu huấn luyện, dự báo khớp dự báo test đã lưu). Phần chạm cụm thật (Prometheus/Jaeger thật, Kubernetes API, RBAC) chỉ kiểm được khi chạy Bước 6 đến 8.

**Nhãn nơi chạy:** `[build]` = máy Ubuntu Server có Docker · `[app]` = SSH vào `node-app` · `[obs]` = SSH vào `node-observability` · `[loadgen]` = SSH vào `node-loadgen`.

Mỗi bước có **Kiểm tra trước**. Nếu kiểm tra FAIL thì sửa rồi mới chạy lệnh.

**Quy ước chuyển file giữa máy build và các node.** Repo chỉ nằm trên máy build. Các node **không** `git clone`/`git pull`; máy build chỉ chép sang node những thư mục cần thiết, và script chạy trên node chỉ dùng đường dẫn dưới `~`. Kết quả sinh ra trên node được **chuyển** (chép về rồi xóa bản trên node) vào đúng thư mục tương ứng trong repo.

| Node | Nhận từ máy build | Kết quả sinh ra trên node | Đích trong repo |
|---|---|---|---|
| `node-app` | `autoscaler/scripts/` → `~/autoscaler-scripts/` (Bước 1.3); `bundle/` (image `.tar`, `deploy/`, `models/`) → `~/autoscaler-bundle/` (Bước 5, 9) | `~/evidence/<kịch_bản>-seed<seed>-<ngữ_cảnh>-rep<k>/` | `evidence/` |
| | | `~/snapshots/pre-autoscaler-<thời_điểm>/` | `snapshots/` (chỉ chuyển hẳn **sau Bước 11**, không commit) |
| `node-loadgen` | `load-testing/` → `~/load-testing/` (theo `load-testing/README.md`, Bước 2; kiểm tra ngắn không cần `dataset/raw/`); `autoscaler/scripts/` → `~/autoscaler-scripts/` | `~/dataset/raw-eval/<kịch_bản>-seed<seed>-<độ_dài>s/<ngữ_cảnh>/<kịch_bản>/run_XX/` | `dataset/raw-eval/` |
| | | `~/eval-logs/` (log và mã thoát của từng run) | không cần (chỉ để gỡ lỗi) |
| `node-observability` | không cần | | |

- **Chép lại một thư mục lên node:** xóa thư mục đích trên node trước rồi mới `scp -r`. Nếu thư mục đích đã tồn tại, `scp -r` chép lồng vào trong (ví dụ `~/autoscaler-scripts/scripts/`) và node vẫn chạy bản cũ.
- **Chuyển kết quả về repo:** `rsync --remove-source-files` rồi xóa thư mục rỗng trên node (mục 10). Cấu trúc thư mục dưới `~` trên node trùng với cấu trúc trong repo.
- **Snapshot ở lại `node-app`** cho tới khi xong Bước 11: `set-context.sh hpa`, `run-comparison.sh`, `teardown.sh --restore-hpa` và `verify-clean.sh` đều đọc snapshot trên node. Trước đó chỉ sao chép về máy build để dự phòng.
- **Chỉ chuyển kết quả của một preset khi preset đó đã chạy xong.** Sau khi chuyển, node không còn run cũ, nên chạy lại cùng preset sẽ tạo lại `run_01` với cùng seed. Khi chuyển về, `rsync` sẽ ghi đè `run_01` đã có trong repo. Muốn giữ cả hai thì đổi tên thư mục cũ trong repo trước khi chuyển.

## Mục lục

0. [Phạm vi, hợp đồng và giới hạn replica](#0-phạm-vi-hợp-đồng-và-giới-hạn-replica)
1. [Kiểm tra hệ thống](#1-kiểm-tra-hệ-thống)
2. [Snapshot cụm](#2-snapshot-cụm)
3. [Tạo model (PPO mẫu, GAT-GRU)](#3-tạo-model-ppo-mẫu-gat-gru)
4. [Build image và đóng gói](#4-build-image-và-đóng-gói)
5. [Chuyển lên node-app và import image](#5-chuyển-lên-node-app-và-import-image)
6. [Tạo namespace, RBAC, cấu hình](#6-tạo-namespace-rbac-cấu-hình)
7. [Nạp model vào PVC](#7-nạp-model-vào-pvc)
8. [Chạy controller ở chế độ shadow](#8-chạy-controller-ở-chế-độ-shadow)
9. [Thay PPO mẫu bằng PPO thật](#9-thay-ppo-mẫu-bằng-ppo-thật)
10. [Kiểm tra ngắn: HPA và autoscaler trên cùng trace](#10-kiểm-tra-ngắn-hpa-và-autoscaler-trên-cùng-trace)
11. [Gỡ sạch](#11-gỡ-sạch)
12. [Lỗi thường gặp](#12-lỗi-thường-gặp)

## 0. Phạm vi, hợp đồng và giới hạn replica

| Mục | Giá trị |
|---|---|
| Service được scale (10) | `frontend, cartservice, checkoutservice, currencyservice, productcatalogservice, recommendationservice, adservice, emailservice, paymentservice, shippingservice` |
| Cố định | `redis-cart` (Redis đơn, thêm replica sẽ tách dữ liệu giỏ hàng) |
| Replica mỗi service / trần tổng | 1 đến 15 / **26** |
| Observation PPO | 132 chiều (13 feature x 10 service + 2 toàn cục) |
| Action PPO | `MultiDiscrete([5]*10)`, **Δ = a − 2 ∈ {−2, −1, 0, +1, +2}** mỗi service, mỗi **30 s** |
| Chu kỳ | Monitor 10 s, quyết định 30 s, cần 18 bước (180 s) lịch sử mới dự báo được |

**Hợp đồng PPO đã chốt** trong [`observation_contract.json`](autoscaler/rl/observation_contract.json) (`contract_id: ppo_obs_v3_10svc_d2`). Action Δ ∈ {−2..+2} mỗi 30 s theo Agarwal et al. (IEEE TSC 2024). **PPO thật phải huấn luyện đúng hợp đồng này**: đúng thứ tự feature, đúng `nvec`, đúng `max_total = 26`. Đổi hợp đồng thì phải sửa controller và tạo lại PPO mẫu.

**Vì sao trần tổng là 26 (số tương đối).** Tổng replica của 10 service được scale (không tính `redis-cart`) trong `dataset/raw/`, thu khi HPA bật:

| Kịch bản | Tải thiết kế đỉnh (rps) | Tổng replica sẵn sàng max | HPA muốn max |
|---|---:|---:|---:|
| `mmpp` | 90,0 | 26 | 33 |
| `nhpp` | 76,1 | 26 | 30 |
| `onoff` | 49,5 | 19 | 19 |
| `pareto` | 20,3 | 23 | 23 |

HPA muốn 33 nhưng chỉ 26 pod chạy được cùng lúc, nên cụm được coi là hết chỗ ở 26. Đây là suy luận từ dataset, chưa phải năng lực đo trực tiếp: xác nhận trên cụm thật bằng `kubectl describe nodes` (CPU allocatable của các worker chia cho CPU request mỗi pod) hoặc quan sát pod `Pending` với sự kiện `FailedScheduling`. Nếu khác 26, đổi cùng giá trị ở [`configmap-policy.yaml`](autoscaler/deploy/configmap-policy.yaml) (`max_total_replicas`) và hợp đồng (`replica_bounds.max_total`) **trước Bước 3**.

Các file:

```text
autoscaler/
├── Dockerfile                       # build từ thư mục gốc repo
├── requirements-controller.txt      # thư viện của image (phiên bản khớp handoff)
├── controller/                      # vòng lặp MAPE-K: monitor, features, analyze, env, plan, execute, telemetry, main
├── tests/test_controller_offline.py # test không cần cụm (Bước 4.2)
├── deploy/                          # namespace, rbac, configmap-policy, pvc, deployment (.yaml)
├── rl/                              # observation_contract.json, make_dummy_policy.py
└── scripts/
    ├── preflight.sh                 # [build|app|loadgen] kiểm tra trạng thái, chỉ đọc
    ├── snapshot.sh, teardown.sh, verify-clean.sh   # [app] chụp, gỡ, đối chiếu
    ├── set-context.sh               # [app] chuyển ngữ cảnh: hpa | autoscaler | off
    ├── collect-evidence.sh          # [app] thu bằng chứng controller đã chạy và đã scale
    ├── run-eval.sh                  # [loadgen] chạy 1 kịch bản tải trong 1 ngữ cảnh
    └── run-comparison.sh            # [build] điều phối kiểm tra ngắn qua SSH

notebooks/deployment_evaluation/
└── evaluate_short_test.ipynb        # [build] tính chỉ số + vẽ hình chuỗi thời gian (mục 10)
```

## 1. Kiểm tra hệ thống

**1.1 `[build]` chuẩn bị máy (một lần)**

```bash
sudo apt-get update && sudo apt-get install -y git rsync python3-venv python3-pip
sudo usermod -aG docker "$USER"            # rồi đăng xuất/đăng nhập lại SSH
git clone <URL repo> thesis-gnn-rl-autoscaling && cd thesis-gnn-rl-autoscaling
git checkout <nhánh có autoscaler/ và artifacts/gatgru/v05_selected_features_final>
python3 -m venv .venv && source .venv/bin/activate
pip install -r modeling-common/requirements-ml.txt stable-baselines3 gymnasium prometheus-client
```

Ubuntu 22.04 mặc định Python 3.10: cài 3.12 bằng PPA deadsnakes (`python3.12 python3.12-venv`) hoặc chạy lệnh Python trong container `python:3.12-slim`. Ubuntu 24.04 có sẵn 3.12.

**1.2 `[build]` kiểm tra**

```bash
bash autoscaler/scripts/preflight.sh build
```

Kỳ vọng: không có `[FAIL]`. Nếu `uname -m` là `aarch64`, build bằng `docker buildx build --platform linux/amd64`.

**1.3 `[build]` kiểm tra SSH và đưa script lên các node**

```bash
KEY=~/.ssh/kltn_autoscaling
ssh -i $KEY <user>@<FIP_node-app> hostname           # kỳ vọng in tên node
for h in <user>@<FIP_node-app> <user>@<FIP_node-loadgen>; do
  ssh -i $KEY "$h" 'rm -rf ~/autoscaler-scripts'    # tránh scp chép lồng vào thư mục cũ
  scp -i $KEY -r autoscaler/scripts "$h":~/autoscaler-scripts
done
```

Chỉ thư mục `autoscaler/scripts/` được chép lên node, không chép cả repo. Sau này `run-comparison.sh` tự đồng bộ lại thư mục này trước mỗi lần chạy.

`<user>` là user mặc định của image Ubuntu, `<FIP_...>` là Floating IP (hỏi người đã chạy `cluster-setup/` nếu chưa biết).

**1.4 `[obs]` lấy IP private của node-observability**

```bash
hostname -I          # ví dụ 10.42.0.x -> dùng làm <OBS_IP>
```

**1.5 `[app]` và `[loadgen]` kiểm tra**

```bash
OBS_IP=<OBS_IP> bash ~/autoscaler-scripts/preflight.sh node-app      # [app]
bash ~/autoscaler-scripts/preflight.sh loadgen                       # [loadgen]
```

Kỳ vọng: không có `[FAIL]`. `node-app`: `sudo` không mật khẩu, 3 node `Ready`, 11/11 Deployment sẵn sàng, không pod `Pending`, metrics-server, StorageClass `local-path`, nhãn/taint `node-app`, containerd, ổ đĩa, Prometheus/Jaeger, **min/max của HPA** (cảnh báo nếu max khác 15). `loadgen`: `node-ips.env`, frontend, Prometheus, Jaeger.

Nếu FAIL ở `sudo`: nhờ người phụ trách cụm chạy các lệnh `[app]`.

## 2. Snapshot cụm

Làm **trước mọi thay đổi**. Snapshot dùng để khôi phục HPA và đối chiếu sau khi gỡ.

```bash
# [app]
bash ~/autoscaler-scripts/snapshot.sh            # dòng cuối in <SNAP_DIR>, ghi lại
ls <SNAP_DIR>                                    # kỳ vọng có hpa.restore.json, replicas.txt, namespaces.txt, ...
```
```bash
# [build]  SAO CHÉP (không chuyển) snapshot về máy để dự phòng; bản trên node-app phải giữ tới hết Bước 11
mkdir -p snapshots
scp -i $KEY -r <user>@<FIP_node-app>:<SNAP_DIR> snapshots/
```

## 3. Tạo model (PPO mẫu, GAT-GRU)

`[build]`, thư mục gốc repo, venv đã kích hoạt. **Kiểm tra trước:** `preflight.sh build` đạt (trừ mục controller); hợp đồng ở mục 0 đã đúng.

**3.1 PPO mẫu** (môi trường giả, vài phút; chỉ để thử đường ống)

```bash
python autoscaler/rl/make_dummy_policy.py models/ppo/v00-dummy 50000
python - <<'EOF'
import numpy as np
from stable_baselines3 import PPO
m = PPO.load("models/ppo/v00-dummy/policy.zip", device="cpu")
a, _ = m.predict(np.zeros(132, np.float32), deterministic=True)
print("action:", a, "-> delta:", a - 2)       # kỳ vọng 10 giá trị trong {-2..+2}
EOF
```

Script tự kiểm `observation_dim`, `nvec`, `mapping` và `max_step` khớp hợp đồng.

**3.2 GAT-GRU v05.** `handoff_manifest.json` ghi `deployment_checkpoint: null` (chưa chọn seed); dùng tạm `seed_0`.

```bash
python modeling-common/verify_gatgru_v05_compatibility.py      # kỳ vọng mismatch_count = 0
SRC=artifacts/gatgru/v05_selected_features_final; DST=models/gatgru/v05
mkdir -p "$DST"
cp "$SRC/gatgru_selected_model_seed_0.pt" "$SRC/handoff_manifest.json" \
   "$SRC/reference_input.npz" "$SRC/reference_output.npz" "$DST/"
cp configs/gatgru/graph_metadata.json configs/gatgru/feature_contract_v01.json "$DST/"
( cd "$DST" && sha256sum * | grep -v SHA256SUMS > SHA256SUMS )
```

## 4. Build image và đóng gói

`[build]`, thư mục gốc repo, venv đã kích hoạt.

**4.1 Controller** nằm trong [`autoscaler/controller/`](autoscaler/controller/), vòng lặp MAPE-K:

| Module | Pha | Việc |
|---|---|---|
| `monitor.py` | Monitor | Thu Prometheus + Jaeger bằng **chính** `load-testing/collect_metrics.py` (code đã thu dataset), đưa qua đúng định dạng CSV của dataset. Cửa sổ 24 bước (240 s), chỉ dùng bước đã kết thúc ít nhất 20 s; Jaeger lưu đệm theo bước (mỗi chu kỳ chỉ truy vấn bước mới) |
| `features.py` | Analyze | Dựng X/E bằng **chính** `forecast_data.build_run_arrays` (bỏ 6 bước warm-up như lúc huấn luyện), chọn feature theo tên trong metadata checkpoint |
| `analyze.py` | Analyze | GAT-GRU dự báo t+1..t+6; kiểm `SHA256SUMS`, self-test với `reference_input/output.npz`; MAE online của dự báo t+1 |
| `env.py` | Plan | Dựng observation 132 chiều đúng hợp đồng; lớp Gymnasium `ClusterEnv` bọc cụm thật (dùng khi huấn luyện online sau này) |
| `plan.py` | Plan | PPO (kiểm `contract_id`, `observation_dim`, `nvec`, phiên bản GAT-GRU), lớp an toàn (biên độ, 1..15, cooldown 120 s, trần tổng) |
| `execute.py` | Execute | Đọc Deployment, PATCH `deployments/scale` với `field_manager=gnn-rl-autoscaler` |
| `telemetry.py`, `main.py` | Knowledge | `/metrics`, `/healthz` (cổng 8000), Decision Log JSONL; vòng lặp 10 s, quyết định mỗi 30 s |

Hành vi theo dữ liệu: mất Prometheus, hoặc mất Jaeger 3 chu kỳ liên tiếp (`jaeger_fail_cycles_before_hold`), thì `hold` (không đổi replica); mọi thay đổi replica đều do PPO quyết định. Cả hai chế độ đều cần PPO nạp được. PPO mẫu (`"dummy": true`) bị chặn ở `gatgru-ppo` khi `allow_active_with_dummy` là `"false"`: không PATCH và `/healthz` trả 503. Lỗi gói model hoặc hợp đồng làm pod khởi động thất bại (thấy ngay ở `rollout status`).

**4.2 Test offline (bắt buộc trước khi build, không cần cụm)**

```bash
pip install prometheus-client                  # nếu venv chưa có
python autoscaler/tests/test_controller_offline.py
```

Kỳ vọng: `ĐẠT 12 kiểm tra`. Test phát lại các run trong dataset như thể đang chạy online và kiểm:
- **Feature online khớp tuyệt đối** với `dataset/processed/gatgru/runs_raw.npz`.
- **Đầu-cuối**: từ CSV qua feature online và GAT-GRU **khớp dự báo test đã lưu** (`gatgru_selected_predictions_test.npz`).
- Lớp an toàn, hai chế độ với PPO giả đúng hợp đồng, chặn PPO dummy, `hold` khi mất Jaeger/Prometheus, đệm Jaeger của Monitor.

Test cần `dataset/processed/gatgru/runs_raw.npz` (sinh bởi notebook preprocess; thư mục này bị Git ignore nên phải có sẵn hoặc chạy lại notebook).

**4.3 Build và đóng gói**

```bash
docker build -t gnn-rl-autoscaler:0.1.0 -f autoscaler/Dockerfile .
docker run --rm --entrypoint python gnn-rl-autoscaler:0.1.0 -c "import controller.main, gatgru_inference, collect_metrics; print('import OK')"
rm -rf bundle && mkdir bundle
docker save -o bundle/gnn-rl-autoscaler.tar gnn-rl-autoscaler:0.1.0
cp -r autoscaler/deploy bundle/deploy && cp -r models bundle/models
ls -lh bundle/gnn-rl-autoscaler.tar              # so với ổ đĩa node-app
```

Image cố định torch 2.14.1 (CPU), numpy 2.5.4, pandas 3.0.6 đúng runtime trong `handoff_manifest.json` ([`requirements-controller.txt`](autoscaler/requirements-controller.txt)).

## 5. Chuyển lên node-app và import image

**Kiểm tra trước:** `[app]` `df -h /` còn trống lớn hơn file `.tar` cộng khoảng 2 GB.

```bash
# [build]  chỉ chép bundle/ (image, manifest, model), không chép repo
ssh -i $KEY <user>@<FIP_node-app> 'rm -rf ~/autoscaler-bundle'
scp -i $KEY -r bundle <user>@<FIP_node-app>:~/autoscaler-bundle
```
```bash
# [app]
cd ~/autoscaler-bundle
sudo k3s ctr images import gnn-rl-autoscaler.tar
sudo k3s ctr images ls | grep gnn-rl-autoscaler        # kỳ vọng thấy image
rm gnn-rl-autoscaler.tar
```

## 6. Tạo namespace, RBAC, cấu hình

**Kiểm tra trước (`[app]`):** `sudo k3s kubectl get ns autoscaler` báo `NotFound`.

```bash
# [app]
k() { sudo k3s kubectl "$@"; }
cd ~/autoscaler-bundle/deploy
OBS_IP=<IP thật của node-observability, lấy ở Bước 1.4>
sed -i "s/<OBS_IP>/$OBS_IP/g" configmap-policy.yaml
grep -nE "9090|16686|max_total_replicas|max_step|mode:" configmap-policy.yaml   # IP đã thay, trần 26, max_step 2, mode shadow

k apply -f namespace.yaml -f rbac.yaml -f configmap-policy.yaml -f pvc.yaml
SA=system:serviceaccount:autoscaler:gnn-rl-autoscaler
k auth can-i patch deployments/scale -n online-boutique --as=$SA   # kỳ vọng yes
k auth can-i delete deployments      -n online-boutique --as=$SA   # kỳ vọng no
k auth can-i delete hpa              -n online-boutique --as=$SA   # kỳ vọng no
k -n autoscaler get pvc                                            # Pending là bình thường (WaitForFirstConsumer)
```

## 7. Nạp model vào PVC

**Kiểm tra trước:** `[app]` `k -n autoscaler get pvc` có `models` và `decision-log`.

```bash
# [app]
k() { sudo k3s kubectl "$@"; }
k -n autoscaler apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata: { name: model-loader, labels: { part-of: gnn-rl-autoscaler } }
spec:
  nodeSelector: { node-role: control-plane }
  tolerations: [{ key: dedicated, operator: Equal, value: control-plane, effect: NoSchedule }]
  containers:
    - { name: l, image: busybox, command: ["sleep","3600"], volumeMounts: [{ name: m, mountPath: /models }] }
  volumes: [{ name: m, persistentVolumeClaim: { claimName: models } }]
EOF
k -n autoscaler wait --for=condition=Ready pod/model-loader --timeout=120s
k -n autoscaler exec model-loader -- mkdir -p /models/gatgru /models/ppo
k -n autoscaler cp ~/autoscaler-bundle/models/gatgru/v05    model-loader:/models/gatgru/v05
k -n autoscaler cp ~/autoscaler-bundle/models/ppo/v00-dummy model-loader:/models/ppo/v00-dummy
k -n autoscaler exec model-loader -- sh -c \
  "cd /models/gatgru/v05 && sha256sum -c SHA256SUMS && cd /models/ppo/v00-dummy && sha256sum -c SHA256SUMS"
k -n autoscaler delete pod model-loader
```

Pod `busybox` kéo image từ Internet; nếu `ImagePullBackOff`, nhờ team cung cấp image sẵn.

## 8. Chạy controller ở chế độ shadow

**Kiểm tra trước (`[app]`):** image đã import, model đã nạp, ConfigMap `mode: "shadow"`, `preflight.sh node-app` vẫn đạt.

```bash
# [app]
k() { sudo k3s kubectl "$@"; }
k apply -f ~/autoscaler-bundle/deploy/deployment.yaml
k -n autoscaler rollout status deploy/gnn-rl-autoscaler --timeout=180s     # kỳ vọng successfully rolled out
k -n autoscaler logs deploy/gnn-rl-autoscaler --tail=50                    # không lỗi nạp model/hợp đồng
k -n autoscaler port-forward deploy/gnn-rl-autoscaler 8000:8000 &
sleep 3; curl -s localhost:8000/healthz
curl -s localhost:8000/metrics | grep -E "autoscaler_(forecast|action|forecast_error)"
kill %1
k top node node-app                                                        # node-app chỉ có 2 vCPU
```

Kỳ vọng: model nạp được, observation đủ 132 chiều, forecast có giá trị ngay (nhờ nạp lại lịch sử), action Δ ∈ {−2..+2} được ghi log. **Shadow không thay đổi cụm.** Dừng nhanh: `k -n autoscaler scale deploy/gnn-rl-autoscaler --replicas=0`.

## 9. Thay PPO mẫu bằng PPO thật

**Kiểm tra trước:** PPO thật có `contract_id: ppo_obs_v3_10svc_d2`, `observation_dim` = 132, `nvec` = `[5]*10`, `training_meta.json` có `"dummy": false`, đủ `policy.zip`, `observation_contract.json`, `SHA256SUMS` (kèm `vecnormalize.pkl` nếu có).

```bash
# [build]
ssh -i $KEY <user>@<FIP_node-app> 'rm -rf ~/autoscaler-bundle/models/ppo/v01'
scp -i $KEY -r models/ppo/v01 <user>@<FIP_node-app>:~/autoscaler-bundle/models/ppo/v01
```
```bash
# [app]  tạo lại pod model-loader như Bước 7, rồi:
k -n autoscaler cp ~/autoscaler-bundle/models/ppo/v01 model-loader:/models/ppo/v01
k -n autoscaler exec model-loader -- sh -c "cd /models/ppo/v01 && sha256sum -c SHA256SUMS"
k -n autoscaler delete pod model-loader
k -n autoscaler patch configmap autoscaler-policy --type merge \
  -p '{"data":{"ppo_policy":"/models/ppo/v01/policy.zip","ppo_dir":"/models/ppo/v01","allow_active_with_dummy":"false"}}'
k -n autoscaler rollout restart deploy/gnn-rl-autoscaler
k -n autoscaler rollout status deploy/gnn-rl-autoscaler --timeout=180s
```

Chạy lại Bước 8, rồi lặp lại kiểm tra ngắn ở mục 10 với ngữ cảnh `autoscaler` (bỏ `--allow-dummy`).

## 10. Kiểm tra ngắn: HPA và autoscaler trên cùng trace

Mục tiêu: (a) **chứng minh mô hình đã chạy và đã điều khiển cụm thật**; (b) so sánh với HPA trên **cùng một trace tải** của bộ sinh tải dataset. Cách làm theo GRAF (IEEE/ACM ToN 2024) và Agarwal et al. (IEEE TSC 2024): mỗi kịch bản **một lần chạy ngắn**, so sánh chuỗi thời gian replica và độ trễ. Đây là nghiên cứu tình huống, không phải kiểm định thống kê; cần nêu rõ giới hạn này trong báo cáo.

**Ngữ cảnh:**
- `hpa`: HPA truyền thống của cụm (khôi phục từ snapshot).
- `autoscaler`: GAT-GRU + PPO. Với PPO mẫu (`--allow-dummy`) chỉ chứng minh đường ống; kết quả gắn nhãn `gatgru-ppo-dummy`, không quy cải thiện cho PPO.

**Preset trace 15 phút** (đã chọn seed để hình dạng tải rõ):

| Preset | Kịch bản, seed | Diễn biến | Thể hiện |
|---|---|---|---|
| `short-mmpp` | `mmpp`, 2030, 900 s | Normal/Idle 0–254 s → **High 254–360 s** → Normal → Idle 424–771 s | Phản ứng với đợt tăng tải đột ngột; sau đó tải thấp để thấy HPA giữ replica 5 phút |
| `short-nhpp` | `nhpp`, 2016, 900 s | Tải thấp ~9,5 rps → **đỉnh 76 rps tại ~400 s** → giảm | Tải có xu hướng, dự báo được (lợi thế chủ động) |

**Kiểm tra trước:**
- Controller đã qua Bước 8 (shadow) ở chế độ sẽ dùng.
- **Người phụ trách cụm đồng ý**: các ngữ cảnh controller xóa HPA và đổi replica của ứng dụng đang chạy (khoảng 50 phút mỗi preset).
- `[loadgen]` đã có `~/load-testing/` (chép thư mục `load-testing/` theo Bước 2 của `load-testing/README.md`, kèm `node-ips.env`) và `preflight.sh loadgen` đạt; `[app]` `preflight.sh node-app` đạt.
- Kết quả của lần chạy trước (nếu có) đã được chuyển về repo, hoặc đã đổi tên thư mục cũ trong repo (xem quy ước chuyển file ở đầu tài liệu).

**Chạy (`[build]`):**

```bash
COMMON="--node-app <user>@<FIP_node-app> --loadgen <user>@<FIP_node-loadgen> --snap <SNAP_DIR>"

# 1) xem kế hoạch (không thực thi)
bash autoscaler/scripts/run-comparison.sh $COMMON --preset short-mmpp --contexts "hpa autoscaler" --allow-dummy --dry-run

# 2) chạy: mỗi preset 1 run cho mỗi ngữ cảnh (2 run x ~24 phút)
bash autoscaler/scripts/run-comparison.sh $COMMON --preset short-mmpp --contexts "hpa autoscaler" --allow-dummy --yes
bash autoscaler/scripts/run-comparison.sh $COMMON --preset short-nhpp --contexts "hpa autoscaler" --allow-dummy --yes
```

Script làm theo thứ tự cho mỗi ngữ cảnh: `set-context.sh` đưa mọi service về 1 replica và bật đúng một bộ điều khiển (controller được chờ **200 s** để đủ lịch sử); `run-eval.sh` phát trace trên loadgen; với ngữ cảnh controller, `collect-evidence.sh` thu bằng chứng trên node-app. Khi mọi run hoàn tất, script khôi phục trạng thái ban đầu (HPA từ snapshot). Khi lỗi hoặc bị ngắt, script **không** bật HPA: cụm giữ nguyên ngữ cảnh đang chạy để kiểm tra lỗi, và script in lệnh khôi phục để chạy tay. Mỗi (kịch bản, seed, độ dài) ghi vào thư mục riêng nên `run_01` luôn đúng seed của preset.

**Chuyển kết quả về repo và tính chỉ số (`[build]`, sau khi preset đã chạy xong):**

```bash
# chép về repo rồi xóa bản trên node
rsync -av --remove-source-files -e "ssh -i $KEY" <user>@<FIP_node-loadgen>:~/dataset/raw-eval/ dataset/raw-eval/
rsync -av --remove-source-files -e "ssh -i $KEY" <user>@<FIP_node-app>:~/evidence/ evidence/
# rsync không xóa thư mục: dọn thư mục rỗng còn lại trên node
ssh -i $KEY <user>@<FIP_node-loadgen> 'find ~/dataset/raw-eval -mindepth 1 -type d -empty -delete'
ssh -i $KEY <user>@<FIP_node-app> 'find ~/evidence -mindepth 1 -type d -empty -delete'
```

`run-comparison.sh` in sẵn các lệnh này khi chạy xong. Không chuyển `~/snapshots`: snapshot phải ở lại `node-app` tới hết Bước 11.

Sau đó chạy notebook [`evaluate_short_test.ipynb`](notebooks/deployment_evaluation/evaluate_short_test.ipynb) (mặc định đọc mọi thư mục trong `dataset/raw-eval/`; chọn tay bằng `ROOTS`). Notebook in bảng theo ngữ cảnh, chênh lệch so với `hpa`, và ghi `<root>/report/<kịch_bản>_summary.csv` cùng hình `<kịch_bản>_timeline.png` (tải thiết kế, tổng replica, p95 frontend). Notebook cảnh báo nếu seed khác nhau, driver bỏ arrival (`dropped > 0`) hoặc run không bắt đầu từ 1 replica: khi đó cặp so sánh không hợp lệ.

| Chỉ số | Ý nghĩa | Tốt khi |
|---|---|---|
| `replica-phút`, `replica-phút sau đỉnh` | Chi phí; HPA giữ replica 5 phút sau khi tải giảm, controller cooldown 120 s | Thấp |
| `scale-up đầu (s)` | Lúc tăng replica đầu tiên so với lúc bắt đầu đợt tăng tải (âm = scale trước) | Nhỏ/âm |
| `đạt R* (s)` | Thời gian để tổng replica đạt mức chung R* | Ngắn |
| `p95 FE đầu đợt TB/max`, `p95 hội tụ (s)` | p95 frontend (Jaeger, cửa sổ 10 s) trong 120 s đầu đợt và thời gian về lại mức trước đợt | Thấp/ngắn |
| `bước p95 FE > SLO %`, `p95 client cả run`, `lỗi %` | Chất lượng dịch vụ cả run | Thấp |

Độ trễ theo thời gian lấy từ Jaeger, **không** từ `locust_stats_history.csv`: percentile trong file đó là lũy kế từ đầu run (giá trị cuối trùng p95 cả run), nên không phản ánh từng thời điểm.

**Bằng chứng "đã đưa mô hình vào thực tế"** (thư mục `evidence/<kịch_bản>-seed<seed>-<ngữ_cảnh>-rep1/`):

| File | Chứng minh |
|---|---|
| `pods.txt`, `controller-deployment.yaml`, `image.txt` | Pod controller chạy trong cụm, trên `node-app`, bằng image đã import |
| `model-files.txt`, `policy-configmap.yaml` | Model nào đang mount (checksum, `training_meta`), chế độ và tham số |
| `replica-managers.tsv` | `spec.replicas` của các Deployment do manager `gnn-rl-autoscaler` đặt qua subresource `scale` (HPA sẽ hiện `kube-controller-manager`) |
| `controller.log`, `decision-log/` | Từng chu kỳ: forecast, action thô, action sau lớp an toàn |
| `events-online-boutique.txt`, `hpa.txt` | Sự kiện scale của Kubernetes; không có HPA nào hoạt động trong lúc chạy |

Kết hợp với hình `*_timeline.png` (replica do controller thay đổi theo tải trên cùng trace với HPA), đây là bộ bằng chứng giống cách GRAF và Agarwal et al. trình bày.

**Diễn giải trung thực:** kiểm tra ngắn chỉ có một cặp run mỗi kịch bản. Kết luận ở dạng "trên trace X, controller dùng ít hơn N replica-phút và p95 đầu đợt thấp hơn M%", không khái quát hóa. Dataset huấn luyện nhỏ (3.661 cửa sổ huấn luyện, chồng lấn) so với 50.000 mẫu của GRAF; cần nêu khi so sánh với các bài báo.

## 11. Gỡ sạch

**Kiểm tra trước:** có `<SNAP_DIR>`. Nếu đã chạy ngữ cảnh controller (HPA bị xóa) và `run-comparison.sh` dừng do lỗi (script không tự khôi phục), phải khôi phục HPA bằng `--restore-hpa` bên dưới.

```bash
# [app]  chỉ dùng shadow
bash ~/autoscaler-scripts/teardown.sh
# [app]  đã chạy ngữ cảnh controller: dừng controller trước, rồi khôi phục HPA
bash ~/autoscaler-scripts/teardown.sh --restore-hpa <SNAP_DIR>
# [app]  đối chiếu với snapshot, dọn thư mục đã chép lên
bash ~/autoscaler-scripts/verify-clean.sh <SNAP_DIR>       # kỳ vọng OK mọi mục, mã thoát 0
rm -rf ~/autoscaler-bundle ~/autoscaler-scripts            # ~/evidence đã được chuyển về repo ở mục 10
```
```bash
# [build]  giờ mới chuyển hẳn snapshot về máy build (không commit), rồi dọn loadgen
mkdir -p snapshots
rsync -av --remove-source-files -e "ssh -i $KEY" <user>@<FIP_node-app>:~/snapshots/ snapshots/
ssh -i $KEY <user>@<FIP_node-app> 'find ~/snapshots -depth -type d -empty -delete'
ssh -i $KEY <user>@<FIP_node-loadgen> 'rm -rf ~/autoscaler-scripts ~/eval-logs'
```

`~/load-testing/` trên loadgen thuộc quy trình thu dataset (`load-testing/README.md`), không xóa ở bước này.

`teardown.sh` dừng controller, (khôi phục HPA), xóa Role/RoleBinding theo nhãn, xóa namespace `autoscaler` (kèm PVC), gỡ image. Không dùng `07-setup-hpa.sh delete` để khôi phục: nó xóa mọi HPA và scale về 1, không giữ đúng snapshot.

## 12. Lỗi thường gặp

| Triệu chứng | Nguyên nhân / cách xử lý |
|---|---|
| `preflight` FAIL ở `sudo` | Không có sudo trên `node-app`: nhờ người phụ trách chạy lệnh `[app]` |
| `connection to the server localhost:8080 refused` | Không chạy qua `sudo k3s kubectl`, hoặc sai node |
| Test offline báo thiếu `runs_raw.npz` | Chạy lại `notebooks/gatgru/preprocess_gatgru.ipynb` (thư mục `dataset/processed/` không có trong Git) |
| Pod controller `CrashLoopBackOff` | Xem `k -n autoscaler logs`: sai checksum, self-test lệch, PPO sai hợp đồng hoặc ConfigMap không hợp lệ |
| `$'\r': command not found` | Script bị CRLF: `sed -i 's/\r$//' autoscaler/scripts/*.sh` |
| `ErrImageNeverPull` | Image chưa import vào `node-app`, hoặc pod bị xếp sang node khác |
| `scp`/import hết dung lượng | Boot volume `node-app` 20 GB: xóa `.tar` cũ, `df -h` |
| Pod ứng dụng `Pending` khi scale | Cụm hết chỗ: hạ `max_total_replicas` (mục 0) |
| `observation_dim mismatch`, `contract_id` lệch | PPO không huấn luyện theo hợp đồng `ppo_obs_v3_10svc_d2` |
| `ContractError` về cạnh hoặc shape | Thứ tự cạnh khác `graph_metadata.json`, hoặc chưa đủ 18 bước lịch sử |
| `403 Forbidden` khi PATCH scale | Thiếu quyền `deployments/scale` hoặc sai namespace |
| `replica-managers.tsv` không có `gnn-rl-autoscaler` | Controller không đặt `field_manager`, hoặc đang ở `shadow` |
| `run-comparison.sh` báo số run lệch | Còn run dở/thừa trong `~/dataset/raw-eval/<kịch_bản>-seed...`: xóa rồi chạy lại |
| `evaluate_short_test.ipynb` cảnh báo `dropped` hoặc `replicas_at_start` | Cặp so sánh không hợp lệ: chạy lại cặp đó |
| Replica dao động liên tục | Còn HPA trên Deployment đó, hai bộ điều khiển tranh nhau |

**Bảo mật:** không đưa `node-ips.env`, token, kubeconfig hay SSH key vào image, ConfigMap hay `bundle/`; không sao chép `/etc/rancher/k3s/k3s.yaml` khỏi `node-app`; không commit `snapshots/` (bản sao cấu hình cụm, chỉ dùng để khôi phục), `bundle/` và `models/` (đã có trong `.gitignore`). `dataset/raw-eval/` và `evidence/` là kết quả, được chuyển vào repo; trước khi commit `evidence/`, rà lại vì `policy-configmap.yaml` và `pods.txt` chứa IP nội bộ của cụm. Checkpoint là pickle nên mount read-only và kiểm checksum.
