# Triển khai autoscaler GAT-GRU + PPO lên cụm K3s

Tài liệu hướng dẫn đưa controller autoscaler (GAT-GRU dự báo tải, PPO quyết định số replica) vào cụm K3s chạy Online Boutique, chứng minh bằng một **kiểm tra ngắn** rằng mô hình đã điều khiển cụm thật, rồi **gỡ sạch** về trạng thái ban đầu.

Chưa có PPO thật nên dùng **PPO mẫu** cùng hợp đồng. Khi có PPO thật chỉ cần thay file mô hình (Bước 9), không build lại image.

> **Chưa chạy thử trên cụm.** Controller đã qua test offline (Bước 4.2). Phần chạm cụm thật (Prometheus/Jaeger, Kubernetes API, RBAC) chỉ kiểm được từ Bước 6.

## Nguyên tắc

1. **HPA mặc định tắt.** Cụm luôn ở trạng thái không có HPA. HPA chỉ được bật trong **ngữ cảnh `hpa` của kiểm tra ngắn** (mục 10) để làm mốc so sánh, và được tắt lại ngay khi kiểm tra kết thúc. Không bước nào khác bật HPA.
2. **Không dùng HPA làm dự phòng.** Khi autoscaler chạy, không có HPA nào hoạt động. Khi thiếu dữ liệu (mất Prometheus, hoặc mất Jaeger 3 chu kỳ liên tiếp), controller giữ nguyên số replica (`hold`). Controller không có quyền tạo HPA (RBAC). Khi kiểm tra ngắn lỗi giữa chừng, script không tự bật HPA.
3. **Gỡ sạch được.** Snapshot được chụp trước mọi thay đổi; cuối cùng cụm được đưa về đúng snapshot và đối chiếu lại.
4. **Repo chỉ nằm trên máy build.** Các node không `git clone`/`git pull`; chỉ nhận những thư mục cần thiết. Kết quả sinh ra trên node được chuyển về repo.
5. **Không sửa `cluster-setup/`** (thuộc thành viên khác). Mọi thứ mới nằm trong [`autoscaler/`](autoscaler/), [`notebooks/deployment_evaluation/`](notebooks/deployment_evaluation/) và namespace `autoscaler`.
6. **Quyền:** chỉ SSH vào từng node. Mọi lệnh `kubectl` chạy trên `node-app` qua `sudo k3s kubectl`.

## Máy và vai trò

Mọi máy đều là **Ubuntu 22.04** (`python3` mặc định là 3.10). Nhãn ở đầu mỗi khối lệnh cho biết nơi chạy:

| Nhãn | Máy | Vai trò | Python |
|---|---|---|---|
| `[build]` | Máy build (Ubuntu Server, có Docker, chứa repo) | Tạo model, test offline, build image, điều phối kiểm tra ngắn qua SSH, tính chỉ số | **3.12** trong `.venv` (Bước 1.1) |
| `[app]` | `node-app` (control-plane K3s) | Chạy `kubectl`, chứa image và pod controller | `python3` 3.10 có sẵn (chỉ dùng thư viện chuẩn); controller chạy 3.12 **trong image** |
| `[loadgen]` | `node-loadgen` | Phát tải bằng Locust, xuất telemetry | `python3` 3.10 kèm gói `python3-venv` |
| `[obs]` | `node-observability` | Prometheus, Jaeger | không dùng |

Không cài Python 3.12 lên các node.

### Chép file lên node và chuyển kết quả về repo

| Node | Nhận từ máy build | Kết quả sinh ra trên node | Đích trong repo |
|---|---|---|---|
| `node-app` | `autoscaler/scripts/` → `~/autoscaler-scripts/` (Bước 1.3) | `~/snapshots/pre-autoscaler-<thời_điểm>/` | `snapshots/` (chỉ chuyển **sau Bước 11**, không commit) |
| | `bundle/` → `~/autoscaler-bundle/` (Bước 5, 9) | `~/evidence/<kịch_bản>-seed<seed>-<ngữ_cảnh>-rep<k>/` | `evidence/` |
| | `k8s-manifests/hpa/hpa-online-boutique.yaml` → `~/hpa-baseline.yaml` (Bước 10.1) | | |
| `node-loadgen` | `load-testing/` → `~/load-testing/` (Bước 2 của `load-testing/README.md`, kèm `node-ips.env`) | `~/dataset/raw-eval/<kịch_bản>-seed<seed>-<độ_dài>s/<ngữ_cảnh>/<kịch_bản>/run_XX/` | `dataset/raw-eval/` |
| | `autoscaler/scripts/` → `~/autoscaler-scripts/` | `~/eval-logs/` (log từng run) | không cần |
| `node-observability` | không cần | | |

Quy tắc:

- **Chép lại một thư mục:** xóa thư mục đích trên node trước rồi mới `scp -r`. Nếu thư mục đích đã tồn tại, `scp -r` chép lồng vào trong (ví dụ `~/autoscaler-scripts/scripts/`) và node vẫn chạy bản cũ.
- **Chép script từ máy build Linux, không chép từ máy Windows.** Trên Windows, Git (`core.autocrlf=true`) và trình soạn thảo có thể đổi script sang CRLF, làm bash lỗi `$'\r': command not found`. Repo có `.gitattributes` buộc `*.sh` và `Dockerfile` dùng LF.
- **Chuyển kết quả về repo** bằng `rsync --remove-source-files` (chép về rồi xóa bản trên node), sau đó xóa thư mục rỗng trên node (Bước 10.4). Cấu trúc dưới `~` trên node trùng cấu trúc trong repo.
- **Snapshot ở lại `node-app`** cho tới hết Bước 11, vì các bước khôi phục đọc snapshot trên node. Trước đó chỉ sao chép về máy build để dự phòng.
- **Chỉ chuyển kết quả của một preset khi preset đó đã chạy xong.** Sau khi chuyển, chạy lại cùng preset sẽ tạo lại `run_01` với cùng seed, và lệnh chuyển sẽ ghi đè `run_01` trong repo. Muốn giữ cả hai thì đổi tên thư mục cũ trong repo trước.

## Tổng quan quy trình

| Bước | Nơi chạy | Thay đổi cụm | Nội dung |
|---|---|---|---|
| [0](#0-phạm-vi-hợp-đồng-và-giới-hạn-replica) | | Không | Phạm vi, hợp đồng PPO, giới hạn replica |
| [1](#1-chuẩn-bị) | build, app, loadgen | Không | Chuẩn bị máy build, chép script lên node, kiểm tra |
| [2](#2-snapshot-cụm) | app | Không | Chụp trạng thái ban đầu (HPA tắt) |
| [3](#3-chuẩn-bị-gói-model) | build | Không | Xác định file model đưa lên cụm, đóng gói GAT-GRU, tạo PPO mẫu |
| [4](#4-test-offline-build-image-và-đóng-gói) | build | Không | Test offline, build image, đóng gói |
| [5](#5-chuyển-bundle-lên-node-app-và-import-image) | build, app | Có (image) | Chép bundle, import image vào containerd |
| [6](#6-tạo-namespace-rbac-cấu-hình) | app | Có | Namespace, RBAC, ConfigMap, PVC |
| [7](#7-nạp-model-vào-pvc) | app | Có | Nạp model vào PVC |
| [8](#8-chạy-controller-ở-chế-độ-shadow) | app | Có (pod controller, không scale) | Chạy shadow: dự báo và quyết định, chỉ ghi log |
| [9](#9-thay-ppo-mẫu-bằng-ppo-thật) | build, app | Có | Thay PPO mẫu bằng PPO thật |
| [10](#10-kiểm-tra-ngắn-hpa-và-autoscaler-trên-cùng-trace) | build (điều phối) | **Có: bật/tắt HPA, đổi replica** | So sánh HPA và autoscaler trên cùng trace; chuyển kết quả; tính chỉ số |
| [11](#11-gỡ-sạch) | app, build | Có (gỡ) | Về đúng snapshot, gỡ autoscaler, đối chiếu |
| [12](#12-lỗi-thường-gặp) | | | Lỗi thường gặp |

Mỗi bước có **Kiểm tra trước**. Nếu kiểm tra FAIL thì sửa rồi mới chạy lệnh.

## 0. Phạm vi, hợp đồng và giới hạn replica

| Mục | Giá trị |
|---|---|
| Service được scale (10) | `frontend, cartservice, checkoutservice, currencyservice, productcatalogservice, recommendationservice, adservice, emailservice, paymentservice, shippingservice` |
| Cố định | `redis-cart` (Redis đơn; thêm replica sẽ tách dữ liệu giỏ hàng) |
| Replica mỗi service / trần tổng | 1 đến 15 / **26** |
| Chế độ controller | `shadow` (dự báo và quyết định, chỉ ghi log) · `gatgru-ppo` (dự báo, quyết định, scale) |
| Observation PPO | 132 chiều (13 feature × 10 service + 2 toàn cục) |
| Action PPO | `MultiDiscrete([5]*10)`, **Δ = a − 2 ∈ {−2, −1, 0, +1, +2}** mỗi service, mỗi **30 s** |
| Chu kỳ | Thu số liệu 10 s, quyết định 30 s; cần 18 bước (180 s) lịch sử mới dự báo được |

**Hợp đồng PPO** nằm trong [`observation_contract.json`](autoscaler/rl/observation_contract.json) (`contract_id: ppo_obs_v3_10svc_d2`). PPO thật phải huấn luyện đúng hợp đồng này: đúng thứ tự service và feature, đúng `nvec`, đúng `max_total = 26`. Đổi hợp đồng thì phải sửa controller và tạo lại PPO mẫu.

**Vì sao trần tổng là 26.** Tổng replica của 10 service được scale trong `dataset/raw/` (thu khi HPA bật):

| Kịch bản | Tải thiết kế đỉnh (rps) | Tổng replica sẵn sàng max | HPA muốn max |
|---|---:|---:|---:|
| `mmpp` | 90,0 | 26 | 33 |
| `nhpp` | 76,1 | 26 | 30 |
| `onoff` | 49,5 | 19 | 19 |
| `pareto` | 20,3 | 23 | 23 |

HPA muốn 33 nhưng chỉ 26 pod chạy được cùng lúc, nên cụm được coi là hết chỗ ở 26. Đây là suy luận từ dataset, chưa phải năng lực đo trực tiếp. Xác nhận trên cụm bằng `kubectl describe nodes` (CPU allocatable của các worker chia cho CPU request mỗi pod) hoặc quan sát pod `Pending` có sự kiện `FailedScheduling`. Nếu khác 26, đổi cùng lúc `max_total_replicas` trong [`configmap-policy.yaml`](autoscaler/deploy/configmap-policy.yaml) và `replica_bounds.max_total` trong hợp đồng, **trước Bước 3**.

**Các file:**

```text
autoscaler/
├── Dockerfile                       # build từ thư mục gốc repo
├── requirements-controller.txt      # thư viện của image (phiên bản khớp handoff_manifest.json)
├── controller/                      # vòng lặp MAPE-K: monitor, features, analyze, env, plan, execute, telemetry, main
├── tests/test_controller_offline.py # test không cần cụm (Bước 4.1)
├── deploy/                          # namespace, rbac, configmap-policy, pvc, deployment (.yaml)
├── rl/                              # observation_contract.json, make_dummy_policy.py
└── scripts/
    ├── preflight.sh                 # [build|app|loadgen] kiểm tra trạng thái, chỉ đọc
    ├── snapshot.sh                  # [app] chụp trạng thái ban đầu
    ├── set-context.sh               # [app] chuyển ngữ cảnh: hpa | autoscaler | off | restore
    ├── collect-evidence.sh          # [app] thu bằng chứng controller đã chạy và đã scale
    ├── teardown.sh, verify-clean.sh # [app] gỡ, đối chiếu với snapshot
    ├── run-eval.sh                  # [loadgen] chạy 1 kịch bản tải trong 1 ngữ cảnh
    └── run-comparison.sh            # [build] điều phối kiểm tra ngắn qua SSH

notebooks/deployment_evaluation/
└── evaluate_short_test.ipynb        # [build] tính chỉ số + vẽ hình chuỗi thời gian (Bước 10.5)
```

## 1. Chuẩn bị

### 1.1 `[build]` Chuẩn bị máy build (một lần)

**Kiểm tra trước:** `df -h /` còn trống **ít nhất 8 GB**: venv khoảng 2 GB, image Docker khoảng 1,5 GB, file `.tar` của image khoảng 1,5 GB, cộng cache khi build. Nếu thiếu, xem dòng "hết dung lượng" ở mục 12.

```bash
sudo apt-get update && sudo apt-get install -y git rsync software-properties-common
# Python 3.12: runtime đã kiểm chứng trong handoff_manifest.json, cùng bản với image controller.
# Ubuntu 22.04 chỉ có 3.10 nên cài 3.12 từ PPA deadsnakes (cài song song, không thay python3 của hệ thống).
sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv python3.12-dev
sudo usermod -aG docker "$USER"            # rồi đăng xuất/đăng nhập lại SSH

git clone <URL repo> thesis-gnn-rl-autoscaling && cd thesis-gnn-rl-autoscaling
git checkout <nhánh có autoscaler/ và artifacts/gatgru/v05_selected_features_final>

rm -rf .venv                               # xóa venv hỏng nếu lần tạo trước bị lỗi
python3.12 -m venv .venv && source .venv/bin/activate
python --version                           # kỳ vọng Python 3.12.x
python -m pip install --upgrade pip

# torch bản CPU TRƯỚC: nếu để pip tự chọn, Linux nhận bản CUDA kèm vài GB thư viện NVIDIA (máy không có GPU)
pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.14.1
pip install --no-cache-dir numpy==2.5.4 pandas==3.0.6
pip install --no-cache-dir -r modeling-common/requirements-ml.txt stable-baselines3 gymnasium prometheus-client
python -c "import torch, numpy, pandas; print(torch.__version__, numpy.__version__, pandas.__version__)"
# kỳ vọng: 2.14.1+cpu 2.5.4 3.0.6
```

- Phiên bản torch, numpy, pandas khớp `handoff_manifest.json` và image controller, nên kết quả test offline trên máy build phản ánh đúng hành vi trong pod.
- Không cài `python3.10-venv` theo gợi ý của thông báo `ensurepip is not available`: venv 3.10 khác runtime đã kiểm chứng.
- Nếu `add-apt-repository` không kết nối được Launchpad (mạng lab chặn), dùng `uv`: `curl -LsSf https://astral.sh/uv/install.sh | sh`, rồi `uv venv --python 3.12 .venv` và cài thư viện bằng `uv pip install ...`.

### 1.2 `[build]` Kiểm tra máy build

```bash
bash autoscaler/scripts/preflight.sh build
```

Kỳ vọng: không có `[FAIL]` (có `python3.12`, `.venv` là 3.12, Docker chạy không cần `sudo`, có checkpoint GAT-GRU và SSH key).

### 1.3 `[build]` Kiểm tra SSH và chép script lên các node

```bash
KEY=~/.ssh/kltn_autoscaling
ssh -i $KEY <user>@<FIP_node-app> hostname            # kỳ vọng in tên node
for h in <user>@<FIP_node-app> <user>@<FIP_node-loadgen>; do
  ssh -i $KEY "$h" 'rm -rf ~/autoscaler-scripts'     # tránh scp chép lồng vào thư mục cũ
  scp -i $KEY -r autoscaler/scripts "$h":~/autoscaler-scripts
done
```

`<user>` là user mặc định của image Ubuntu, `<FIP_...>` là Floating IP của node (hỏi người đã chạy `cluster-setup/` nếu chưa biết). Chỉ thư mục `autoscaler/scripts/` được chép lên node. Từ mục 10, `run-comparison.sh` tự đồng bộ lại thư mục này trước mỗi lần chạy.

### 1.4 `[obs]` Lấy IP nội bộ của node-observability

```bash
hostname -I          # ví dụ 10.42.0.x -> dùng làm <OBS_IP>
```

### 1.5 `[app]` và `[loadgen]` Kiểm tra các node

```bash
OBS_IP=<OBS_IP> bash ~/autoscaler-scripts/preflight.sh node-app      # [app]   OBS_IP phải cùng dòng với lệnh
bash ~/autoscaler-scripts/preflight.sh loadgen                       # [loadgen]
```

Kỳ vọng: không có `[FAIL]`.

- `node-app`: `sudo` không mật khẩu, 3 node `Ready`, 11/11 Deployment sẵn sàng, không pod `Pending`, **HPA đang tắt**, metrics-server, StorageClass `local-path`, nhãn/taint của `node-app`, containerd, ổ đĩa, Prometheus/Jaeger, `python3`, `rsync`.
- `loadgen`: `~/load-testing/` và `node-ips.env`, frontend, Prometheus, Jaeger, `python3-venv` (khi chưa có Locust venv), `rsync`.

Nếu preflight báo **HPA đang bật**: cụm không ở trạng thái mặc định. Hỏi người phụ trách cụm trước khi đi tiếp; không tự xóa HPA. Nếu FAIL ở `sudo`: nhờ người phụ trách cụm chạy các lệnh `[app]`.

## 2. Snapshot cụm

Làm **trước mọi thay đổi**. Snapshot ghi lại trạng thái ban đầu (HPA, số replica, danh sách đối tượng) để khôi phục và đối chiếu ở Bước 11.

```bash
# [app]
bash ~/autoscaler-scripts/snapshot.sh            # dòng cuối in <SNAP_DIR>, ghi lại
ls <SNAP_DIR>                                    # kỳ vọng có hpa.restore.json, replicas.txt, namespaces.txt, ...
```

Kỳ vọng `HPA đang có: 0`, vì HPA mặc định tắt. Dòng `No resources found` (nếu có) đến từ lệnh liệt kê PV/PVC khi cụm chưa có PV/PVC nào, không phải lỗi.

```bash
# [build]  SAO CHÉP (không chuyển) snapshot về máy build để dự phòng; bản trên node-app giữ tới hết Bước 11
mkdir -p snapshots
scp -i $KEY -r <user>@<FIP_node-app>:<SNAP_DIR> snapshots/
```

## 3. Chuẩn bị gói model

`[build]`, thư mục gốc repo, venv đã kích hoạt. **Kiểm tra trước:** `preflight.sh build` đạt; hợp đồng ở mục 0 đã đúng.

### 3.1 Các file được đưa lên cụm

Chỉ những file dưới đây được nạp vào PVC `models` và được controller đọc khi chạy. Mọi file khác của quá trình huấn luyện ở lại repo.

**GAT-GRU** (`models/gatgru/v05/` → `/models/gatgru/v05/` trong pod):

| File | Lấy từ | Nguồn gốc | Controller dùng để |
|---|---|---|---|
| `gatgru_selected_model_seed_0.pt` | `artifacts/gatgru/v05_selected_features_final/` | **Model đã huấn luyện**: seed 0 trong 5 seed của `notebooks/gatgru/train_gatgru_v05_selected_features_final.ipynb`. Chứa trọng số, danh sách feature/target, scaler | Dự báo `rps_in`, `cpu_cores` t+1..t+6 cho 11 service |
| `reference_input.npz`, `reference_output.npz` | `artifacts/gatgru/v05_selected_features_final/` | Một cửa sổ đầu vào mẫu và dự báo tương ứng của chính các checkpoint trên | Tự kiểm tra khi khởi động: dự báo trên đầu vào mẫu phải khớp đầu ra đã lưu, nếu lệch thì pod không khởi động |
| `graph_metadata.json` | `configs/gatgru/` | Hợp đồng đồ thị lúc tiền xử lý: thứ tự 15 cạnh, `model_version` | Dựng tensor cạnh đúng thứ tự; đối chiếu `model_version` với hợp đồng PPO |
| `SHA256SUMS` | tạo ở Bước 3.2 | | Kiểm toàn vẹn các file trên khi khởi động |

**PPO** (`models/ppo/v00-dummy/` hoặc `v01/` → `/models/ppo/...` trong pod):

| File | Nguồn gốc | Controller dùng để |
|---|---|---|
| `policy.zip` | PPO mẫu: `make_dummy_policy.py` (Bước 3.3). PPO thật: quá trình huấn luyện PPO (Bước 9) | Chọn Δ replica |
| `observation_contract.json` | Bản sao hợp đồng lúc huấn luyện PPO | Kiểm hợp đồng và phiên bản GAT-GRU |
| `training_meta.json` | Sinh khi huấn luyện | Cờ `dummy` (chặn PPO mẫu ở chế độ active) |
| `vecnormalize.pkl` (nếu có) | Sinh khi huấn luyện với VecNormalize | Chuẩn hóa observation |
| `SHA256SUMS` | Sinh cùng gói | Kiểm toàn vẹn khi khởi động |

**Không đưa lên cụm:** checkpoint seed 1–4, `gatgru_selected_history_*.csv`, các file `*_metrics_*`, thư mục `tuning/`, `handoff_manifest.json`, `feature_contract_v01.json` (controller không đọc), `gatgru_selected_predictions_test.npz` (chỉ test offline ở Bước 4.1 dùng).

`handoff_manifest.json` ghi `deployment_checkpoint: null` (chưa chọn seed chính thức), nên tạm dùng **seed 0**. Nếu huấn luyện lại, lấy các file trên từ thư mục artifact mới; nếu tên phiên bản đổi thì cập nhật `model_version` trong `graph_metadata.json` và `gatgru_model_version` trong hợp đồng PPO.

### 3.2 Đóng gói GAT-GRU

```bash
SRC=artifacts/gatgru/v05_selected_features_final; DST=models/gatgru/v05
rm -rf "$DST" && mkdir -p "$DST"
cp "$SRC/gatgru_selected_model_seed_0.pt" "$SRC/reference_input.npz" "$SRC/reference_output.npz" "$DST/"
cp configs/gatgru/graph_metadata.json "$DST/"
( cd "$DST" && sha256sum * | grep -v SHA256SUMS > SHA256SUMS )
ls "$DST"       # kỳ vọng đúng 5 file: checkpoint, 2 file reference, graph_metadata.json, SHA256SUMS
```

### 3.3 PPO mẫu (môi trường giả, vài phút; chỉ để thử đường ống)

```bash
python autoscaler/rl/make_dummy_policy.py models/ppo/v00-dummy 50000
```

Script tự kiểm `observation_dim`, `nvec`, `mapping` và `max_step` khớp hợp đồng, rồi ghi `policy.zip`, `observation_contract.json`, `training_meta.json` (`"dummy": true`) và `SHA256SUMS`.

## 4. Test offline, build image và đóng gói

`[build]`, thư mục gốc repo, venv đã kích hoạt.

### 4.1 Test offline (bắt buộc trước khi build, không cần cụm)

```bash
python autoscaler/tests/test_controller_offline.py
```

Kỳ vọng: `ĐẠT 12 kiểm tra`. Đây là bước duy nhất chứng minh controller đưa vào GAT-GRU **đúng loại dữ liệu model đã học**. Test phát lại các run trong dataset như thể đang chạy online và kiểm tra:

- **Feature online khớp tuyệt đối** với feature lúc huấn luyện (`dataset/processed/gatgru/runs_raw.npz`).
- **Đầu-cuối:** từ CSV qua feature online và checkpoint seed 0, kết quả **khớp dự báo test đã lưu** khi huấn luyện (`gatgru_selected_predictions_test.npz`).
- Lớp an toàn, hai chế độ với PPO giả đúng hợp đồng, chặn PPO mẫu, `hold` khi mất Jaeger/Prometheus, bộ đệm Jaeger của Monitor.

**`runs_raw.npz` không có trong Git** (`dataset/.gitignore` bỏ qua `/processed/`). Chép đúng file đã dùng khi huấn luyện từ máy huấn luyện sang cùng đường dẫn trên máy build:

```bash
# chạy trên máy huấn luyện, từ thư mục gốc repo
ssh <user>@<máy_build> 'mkdir -p ~/thesis-gnn-rl-autoscaling/dataset/processed/gatgru'
scp dataset/processed/gatgru/runs_raw.npz <user>@<máy_build>:~/thesis-gnn-rl-autoscaling/dataset/processed/gatgru/
```

Nếu không còn file gốc, tạo lại trên máy build: `jupyter nbconvert --to notebook --execute notebooks/gatgru/preprocess_gatgru.ipynb --output-dir /tmp/nbrun`.

### 4.2 Controller (tham khảo)

Mã nguồn trong [`autoscaler/controller/`](autoscaler/controller/), vòng lặp MAPE-K:

| Module | Pha | Việc |
|---|---|---|
| `monitor.py` | Monitor | Thu Prometheus + Jaeger bằng **chính** `load-testing/collect_metrics.py` (code đã thu dataset). Cửa sổ 24 bước (240 s), chỉ dùng bước đã kết thúc ít nhất 20 s; Jaeger được đệm theo bước |
| `features.py` | Analyze | Dựng X/E bằng **chính** `forecast_data.build_run_arrays` (bỏ 6 bước warm-up như lúc huấn luyện) |
| `analyze.py` | Analyze | GAT-GRU dự báo t+1..t+6; kiểm `SHA256SUMS`, tự kiểm tra với `reference_input/output.npz`; MAE online của dự báo t+1 |
| `env.py` | Plan | Dựng observation 132 chiều đúng hợp đồng |
| `plan.py` | Plan | PPO (kiểm `contract_id`, `observation_dim`, `nvec`, phiên bản GAT-GRU); lớp an toàn (Δ, 1..15, chờ 120 s trước khi giảm, trần tổng) |
| `execute.py` | Execute | PATCH `deployments/scale` với `field_manager=gnn-rl-autoscaler` |
| `telemetry.py`, `main.py` | Knowledge | `/metrics`, `/healthz` (cổng 8000), Decision Log JSONL; vòng lặp 10 s, quyết định mỗi 30 s |

Hành vi theo dữ liệu: mất Prometheus, hoặc mất Jaeger 3 chu kỳ liên tiếp, thì `hold`; mọi thay đổi replica đều do PPO quyết định. Cả hai chế độ đều cần PPO nạp được. PPO mẫu (`"dummy": true`) bị chặn ở `gatgru-ppo` khi `allow_active_with_dummy` là `"false"`: không scale và `/healthz` trả 503. Lỗi gói model hoặc hợp đồng làm pod khởi động thất bại (thấy ngay ở `rollout status`).

### 4.3 Build image và đóng gói

```bash
docker build -t gnn-rl-autoscaler:0.1.0 -f autoscaler/Dockerfile .
docker run --rm --entrypoint python gnn-rl-autoscaler:0.1.0 -c "import controller.main, gatgru_inference, collect_metrics; print('import OK')"
rm -rf bundle && mkdir bundle
docker save -o bundle/gnn-rl-autoscaler.tar gnn-rl-autoscaler:0.1.0
cp -r autoscaler/deploy bundle/deploy && cp -r models bundle/models
ls -lh bundle/gnn-rl-autoscaler.tar              # so với dung lượng trống của node-app
```

Image cố định torch 2.14.1 (CPU), numpy 2.5.4, pandas 3.0.6, đúng runtime trong `handoff_manifest.json` ([`requirements-controller.txt`](autoscaler/requirements-controller.txt)). `bundle/` và `models/` không commit (đã có trong `.gitignore`).

## 5. Chuyển bundle lên node-app và import image

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

Pod controller không pull image từ registry: `deployment.yaml` đặt `imagePullPolicy: Never` và ghim pod vào `node-app`, nên kubelet dùng image vừa import vào containerd.

## 6. Tạo namespace, RBAC, cấu hình

**Kiểm tra trước (`[app]`):** `sudo k3s kubectl get ns autoscaler` báo `NotFound`.

```bash
# [app]
k() { sudo k3s kubectl "$@"; }
cd ~/autoscaler-bundle/deploy
OBS_IP=<IP nội bộ của node-observability, lấy ở Bước 1.4>
sed -i "s/<OBS_IP>/$OBS_IP/g" configmap-policy.yaml
grep -nE "9090|16686|max_total_replicas|max_step|mode:" configmap-policy.yaml   # IP đã thay, trần 26, max_step 2, mode shadow

k apply -f namespace.yaml -f rbac.yaml -f configmap-policy.yaml -f pvc.yaml
SA=system:serviceaccount:autoscaler:gnn-rl-autoscaler
k auth can-i patch deployments --subresource=scale -n online-boutique --as=$SA   # kỳ vọng yes
k auth can-i get deployments                        -n online-boutique --as=$SA   # kỳ vọng yes
k auth can-i patch deployments                      -n online-boutique --as=$SA   # kỳ vọng no (chỉ sửa replica qua scale)
k auth can-i delete deployments                     -n online-boutique --as=$SA   # kỳ vọng no
k auth can-i create hpa                             -n online-boutique --as=$SA   # kỳ vọng no
k -n autoscaler get pvc                                            # Pending là bình thường (WaitForFirstConsumer)
```

Subresource phải hỏi bằng cờ `--subresource=scale`. Viết `deployments/scale` sẽ được hiểu là "Deployment có tên `scale`" và luôn trả về `no`. Hai PVC ở trạng thái `Pending` cho tới khi có pod đầu tiên dùng chúng (Bước 7 và 8).

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
k -n autoscaler exec model-loader -- ls /models/gatgru/v05 /models/ppo/v00-dummy
k -n autoscaler delete pod model-loader
```

Không cần kiểm checksum thủ công: controller kiểm `SHA256SUMS` và tự kiểm tra GAT-GRU khi khởi động (Bước 8); file hỏng hoặc thiếu làm pod khởi động thất bại.

Pod `busybox` kéo image từ Docker Hub qua Internet của `node-app`. Nếu `ImagePullBackOff`, chuẩn bị sẵn `busybox` bằng cách `docker save`/`ctr images import` như Bước 5.

## 8. Chạy controller ở chế độ shadow

**Kiểm tra trước (`[app]`):** image đã import, model đã nạp, ConfigMap `mode: "shadow"`, HPA vẫn tắt.

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

Kỳ vọng: model nạp được, observation đủ 132 chiều, forecast có giá trị ngay (nhờ nạp lại lịch sử khi khởi động), action Δ ∈ {−2..+2} được ghi log. **Shadow không thay đổi cụm.** Dừng nhanh: `k -n autoscaler scale deploy/gnn-rl-autoscaler --replicas=0`.

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
k -n autoscaler delete pod model-loader
k -n autoscaler patch configmap autoscaler-policy --type merge \
  -p '{"data":{"ppo_policy":"/models/ppo/v01/policy.zip","ppo_dir":"/models/ppo/v01","allow_active_with_dummy":"false"}}'
k -n autoscaler rollout restart deploy/gnn-rl-autoscaler
k -n autoscaler rollout status deploy/gnn-rl-autoscaler --timeout=180s
```

Chạy lại Bước 8, rồi chạy kiểm tra ngắn (mục 10) với ngữ cảnh `autoscaler` và bỏ `--allow-dummy`.

## 10. Kiểm tra ngắn: HPA và autoscaler trên cùng trace

**Mục tiêu:** (a) chứng minh mô hình đã chạy và đã điều khiển cụm thật; (b) so sánh với HPA trên **cùng một trace tải** của bộ sinh tải dataset. Cách làm theo GRAF (IEEE/ACM ToN 2024) và Agarwal et al. (IEEE TSC 2024): mỗi kịch bản một lần chạy ngắn, so sánh chuỗi thời gian replica và độ trễ. Đây là nghiên cứu tình huống, không phải kiểm định thống kê.

**Hai ngữ cảnh:**

- `hpa`: HPA baseline (CPU 70%, 1..15 replica, như khi thu dataset), áp từ `~/hpa-baseline.yaml`. **Đây là lúc duy nhất HPA được bật.**
- `autoscaler`: GAT-GRU + PPO, không có HPA. Với PPO mẫu (`--allow-dummy`), kết quả gắn nhãn `gatgru-ppo-dummy`, chỉ chứng minh đường ống, không quy cải thiện cho PPO.

**Vòng đời của HPA trong kiểm tra ngắn:**

| Thời điểm | HPA | Do ai thực hiện |
|---|---|---|
| Trước khi chạy | Tắt (mặc định, đúng snapshot) | |
| Ngữ cảnh `hpa` | Bật từ `~/hpa-baseline.yaml` sau khi mọi service về 1 replica | `set-context.sh hpa` |
| Ngữ cảnh `autoscaler` | Tắt; chỉ controller điều khiển | `set-context.sh autoscaler` |
| Kết thúc thành công | Tắt lại; số replica về như snapshot | `set-context.sh restore` (tự động) |
| Lỗi hoặc bị ngắt | **Không tự khôi phục, không tự bật HPA.** Cụm giữ nguyên ngữ cảnh đang chạy để kiểm tra lỗi; nếu lỗi xảy ra trong ngữ cảnh `hpa` thì HPA vẫn đang bật | Người vận hành chạy `set-context.sh restore` (Bước 10.3) |

**Preset trace 15 phút** (đã chọn seed để hình dạng tải rõ):

| Preset | Kịch bản, seed | Diễn biến | Thể hiện |
|---|---|---|---|
| `short-mmpp` | `mmpp`, 2030, 900 s | Normal/Idle 0–254 s → **High 254–360 s** → Normal → Idle 424–771 s | Phản ứng với đợt tăng tải đột ngột; sau đó tải thấp để thấy HPA giữ replica 5 phút |
| `short-nhpp` | `nhpp`, 2016, 900 s | Tải thấp ~9,5 rps → **đỉnh 76 rps tại ~400 s** → giảm | Tải có xu hướng, dự báo được (lợi thế chủ động) |

**Kiểm tra trước:**

- Controller đã qua Bước 8 (shadow).
- **Người phụ trách cụm đồng ý**: kiểm tra ngắn bật/tắt HPA và đổi replica của ứng dụng đang chạy (khoảng 50 phút mỗi preset).
- `[loadgen]` đã có `~/load-testing/` kèm `node-ips.env`, `preflight.sh loadgen` đạt; `[app]` `preflight.sh node-app` đạt (HPA đang tắt).
- Kết quả của lần chạy trước (nếu có) đã được chuyển về repo, hoặc đã đổi tên thư mục cũ trong repo.

### 10.1 Chuẩn bị HPA baseline (một lần)

Chỉ chép file manifest lên `node-app`. File trong repo ghi `maxReplicas: 10`, còn HPA khi thu dataset dùng **15**; sửa trên bản chép rồi kiểm tra. Bước này chỉ chép file, **chưa bật HPA**.

```bash
# [build]
scp -i $KEY k8s-manifests/hpa/hpa-online-boutique.yaml <user>@<FIP_node-app>:~/hpa-baseline.yaml
```
```bash
# [app]
sed -i 's/^\( *maxReplicas:\) *[0-9]\+/\1 15/' ~/hpa-baseline.yaml
grep -c 'kind: HorizontalPodAutoscaler' ~/hpa-baseline.yaml     # kỳ vọng 10
grep 'maxReplicas' ~/hpa-baseline.yaml | sort | uniq -c          # kỳ vọng 10 dòng "maxReplicas: 15"
```

### 10.2 `[build]` Chạy

```bash
COMMON="--node-app <user>@<FIP_node-app> --loadgen <user>@<FIP_node-loadgen> --snap <SNAP_DIR> --hpa-manifest /home/<user>/hpa-baseline.yaml"

# xem kế hoạch (không thực thi)
bash autoscaler/scripts/run-comparison.sh $COMMON --preset short-mmpp --contexts "hpa autoscaler" --allow-dummy --dry-run

# chạy: mỗi preset 1 run cho mỗi ngữ cảnh (2 run × ~24 phút)
bash autoscaler/scripts/run-comparison.sh $COMMON --preset short-mmpp --contexts "hpa autoscaler" --allow-dummy --yes
bash autoscaler/scripts/run-comparison.sh $COMMON --preset short-nhpp --contexts "hpa autoscaler" --allow-dummy --yes
```

`run-comparison.sh` lần lượt:

1. Kiểm tra SSH, snapshot và `~/hpa-baseline.yaml` trên `node-app` (cảnh báo nếu `maxReplicas` khác 15), đồng bộ `autoscaler/scripts/` lên hai node.
2. Với mỗi ngữ cảnh: `set-context.sh` dừng controller, xóa HPA, đưa mọi service về 1 replica rồi bật đúng một bộ điều khiển (controller được chờ 200 s để đủ lịch sử); `run-eval.sh` phát trace trên loadgen; với ngữ cảnh `autoscaler`, `collect-evidence.sh` thu bằng chứng trên `node-app`.
3. Khi mọi run xong: `set-context.sh restore` đưa cụm về snapshot (HPA tắt), rồi in lệnh chuyển kết quả.

Mỗi (kịch bản, seed, độ dài) ghi vào thư mục riêng, nên `run_01` luôn đúng seed của preset.

### 10.3 `[app]` Khi kiểm tra ngắn dừng giữa chừng

Sau khi xử lý lỗi, đưa cụm về trạng thái mặc định (HPA tắt, replica như snapshot):

```bash
bash ~/autoscaler-scripts/set-context.sh restore --yes --snap <SNAP_DIR>
sudo k3s kubectl -n online-boutique get hpa          # kỳ vọng: No resources found
```

### 10.4 `[build]` Chuyển kết quả về repo (sau khi preset đã chạy xong)

```bash
rsync -av --remove-source-files -e "ssh -i $KEY" <user>@<FIP_node-loadgen>:~/dataset/raw-eval/ dataset/raw-eval/
rsync -av --remove-source-files -e "ssh -i $KEY" <user>@<FIP_node-app>:~/evidence/ evidence/
# rsync không xóa thư mục: dọn thư mục rỗng còn lại trên node
ssh -i $KEY <user>@<FIP_node-loadgen> 'find ~/dataset/raw-eval -mindepth 1 -type d -empty -delete'
ssh -i $KEY <user>@<FIP_node-app> 'find ~/evidence -mindepth 1 -type d -empty -delete'
```

`run-comparison.sh` in sẵn các lệnh này khi chạy xong. Không chuyển `~/snapshots` ở bước này.

### 10.5 `[build]` Tính chỉ số

Chạy notebook [`evaluate_short_test.ipynb`](notebooks/deployment_evaluation/evaluate_short_test.ipynb) bằng kernel của `.venv`. Notebook mặc định đọc mọi thư mục trong `dataset/raw-eval/` (chọn tay bằng `ROOTS`), in bảng theo ngữ cảnh và chênh lệch so với `hpa`, ghi `<root>/report/<kịch_bản>_summary.csv` và hình `<kịch_bản>_timeline.png` (tải thiết kế, tổng replica, p95 frontend). Notebook cảnh báo khi seed khác nhau, driver bỏ arrival (`dropped > 0`) hoặc run không bắt đầu từ 1 replica: khi đó cặp so sánh không hợp lệ.

| Chỉ số | Ý nghĩa | Tốt khi |
|---|---|---|
| `replica-phút`, `replica-phút sau đỉnh` | Chi phí; HPA giữ replica 5 phút sau khi tải giảm, controller chờ 120 s | Thấp |
| `scale-up đầu (s)` | Lần tăng replica đầu tiên so với lúc bắt đầu đợt tăng tải (âm = scale trước tải) | Nhỏ / âm |
| `đạt R* (s)` | Thời gian để tổng replica đạt mức chung R* | Ngắn |
| `p95 FE đầu đợt TB/max`, `p95 hội tụ (s)` | p95 frontend (Jaeger, cửa sổ 10 s) trong 120 s đầu đợt, và thời gian về lại mức trước đợt | Thấp / ngắn |
| `bước p95 FE > SLO %`, `p95 client cả run`, `lỗi %` | Chất lượng dịch vụ cả run | Thấp |

Độ trễ theo thời gian lấy từ Jaeger, **không** từ `locust_stats_history.csv` (percentile trong file đó lũy kế từ đầu run).

### 10.6 Bằng chứng "đã đưa mô hình vào thực tế"

Thư mục `evidence/<kịch_bản>-seed<seed>-autoscaler-rep1/`:

| File | Chứng minh |
|---|---|
| `pods.txt`, `controller-deployment.yaml`, `image.txt` | Pod controller chạy trong cụm, trên `node-app`, bằng image đã import |
| `model-files.txt`, `policy-configmap.yaml` | Model đang mount (checksum, `training_meta`), chế độ và tham số |
| `replica-managers.tsv` | `spec.replicas` của các Deployment do manager `gnn-rl-autoscaler` đặt qua subresource `scale` (HPA sẽ hiện `kube-controller-manager`) |
| `controller.log`, `decision-log/` | Từng chu kỳ: forecast, action thô, action sau lớp an toàn |
| `events-online-boutique.txt`, `hpa.txt` | Sự kiện scale của Kubernetes; không có HPA nào hoạt động trong lúc autoscaler chạy |

Kết hợp với hình `*_timeline.png`, đây là bộ bằng chứng theo cách GRAF và Agarwal et al. trình bày.

**Diễn giải trung thực:** mỗi kịch bản chỉ có một cặp run. Kết luận ở dạng "trên trace X, autoscaler dùng ít hơn N replica-phút và p95 đầu đợt thấp hơn M%", không khái quát hóa. Dataset huấn luyện nhỏ (3.661 cửa sổ huấn luyện, chồng lấn) so với 50.000 mẫu của GRAF; cần nêu khi so sánh với các bài báo.

## 11. Gỡ sạch

**Kiểm tra trước:** có `<SNAP_DIR>` trên `node-app`.

```bash
# [app]  chỉ chạy shadow (cụm chưa bị đổi replica)
bash ~/autoscaler-scripts/teardown.sh
# [app]  đã chạy kiểm tra ngắn: dừng controller, về đúng snapshot (HPA tắt, replica như lúc chụp), rồi gỡ
bash ~/autoscaler-scripts/teardown.sh --restore <SNAP_DIR>

# [app]  đối chiếu với snapshot, dọn các file đã chép lên
bash ~/autoscaler-scripts/verify-clean.sh <SNAP_DIR>       # kỳ vọng OK mọi mục, mã thoát 0
sudo k3s kubectl -n online-boutique get hpa                # kỳ vọng: No resources found
rm -rf ~/autoscaler-bundle ~/autoscaler-scripts ~/hpa-baseline.yaml
```
```bash
# [build]  chuyển hẳn snapshot về máy build (không commit), rồi dọn loadgen
mkdir -p snapshots
rsync -av --remove-source-files -e "ssh -i $KEY" <user>@<FIP_node-app>:~/snapshots/ snapshots/
ssh -i $KEY <user>@<FIP_node-app> 'find ~/snapshots -depth -type d -empty -delete'
ssh -i $KEY <user>@<FIP_node-loadgen> 'rm -rf ~/autoscaler-scripts ~/eval-logs'
```

`teardown.sh` lần lượt: dừng controller; với `--restore`, gọi `set-context.sh restore` (áp lại HPA của snapshot nếu có, đặt số replica như snapshot); xóa Role/RoleBinding theo nhãn; xóa namespace `autoscaler` (kèm PVC); gỡ image khỏi containerd. `~/load-testing/` trên loadgen thuộc quy trình thu dataset, không xóa ở bước này. Không dùng `cluster-setup/07-setup-hpa.sh delete` để khôi phục: lệnh đó scale mọi Deployment về 1, không giữ đúng snapshot.

## 12. Lỗi thường gặp

| Triệu chứng | Nguyên nhân / cách xử lý |
|---|---|
| `ensurepip is not available` khi tạo venv | Chưa cài `python3.12-venv`, hoặc đang dùng `python3` (3.10). Làm lại Bước 1.1 với `python3.12` |
| `Command 'python3.12' not found` | Chưa thêm PPA deadsnakes hoặc chưa cài `python3.12` (Bước 1.1) |
| `No space left on device` khi `pip install` | pip tải torch bản CUDA (vài GB thư viện `nvidia-*`). Xóa `.venv` và `~/.cache/pip`, cài torch CPU trước như Bước 1.1. Nếu `/` vẫn thiếu: `sudo vgs` xem `VFree`; nếu còn, `sudo lvextend -r -l +100%FREE /dev/ubuntu-vg/ubuntu-lv` |
| `$'\r': command not found`, `set: pipefail: invalid option name` | Script bị CRLF (chép từ bản checkout trên Windows). Trên node: `sed -i 's/\r$//' ~/autoscaler-scripts/*.sh`; lần sau chép từ máy build Linux |
| `preflight.sh node-app` báo chưa đặt `OBS_IP` dù đã gán | Gán biến trên dòng riêng không truyền sang lệnh `bash`. Viết cùng dòng: `OBS_IP=<IP> bash ~/autoscaler-scripts/preflight.sh node-app`, hoặc `export OBS_IP=<IP>` |
| `preflight.sh node-app` báo HPA đang bật | Cụm không ở trạng thái mặc định (HPA tắt). Hỏi người phụ trách cụm; nếu do kiểm tra ngắn dừng giữa chừng, chạy Bước 10.3 |
| `preflight` FAIL ở `sudo` | Không có sudo trên `node-app`: nhờ người phụ trách chạy các lệnh `[app]` |
| `connection to the server localhost:8080 refused` | Không chạy qua `sudo k3s kubectl`, hoặc sai node |
| Test offline báo thiếu `runs_raw.npz` | `dataset/processed/` không có trong Git: chép file từ máy huấn luyện (Bước 4.1) |
| Pod controller `CrashLoopBackOff` | Xem `k -n autoscaler logs`: sai checksum, tự kiểm tra lệch, PPO sai hợp đồng hoặc ConfigMap không hợp lệ |
| `ErrImageNeverPull` | Image chưa import vào `node-app`, hoặc pod bị xếp sang node khác |
| `scp`/import hết dung lượng trên `node-app` | Xóa `.tar` cũ, kiểm tra `df -h` |
| Pod ứng dụng `Pending` khi scale | Cụm hết chỗ: hạ `max_total_replicas` (mục 0) |
| `observation_dim mismatch`, `contract_id` lệch | PPO không huấn luyện theo hợp đồng `ppo_obs_v3_10svc_d2` |
| `ContractError` về cạnh hoặc shape | Thứ tự cạnh khác `graph_metadata.json`, hoặc chưa đủ 18 bước lịch sử |
| `403 Forbidden` khi PATCH scale | Thiếu quyền `deployments/scale` hoặc sai namespace |
| `replica-managers.tsv` không có `gnn-rl-autoscaler` | Controller đang ở `shadow`, hoặc không đặt `field_manager` |
| `run-comparison.sh` báo thiếu `--hpa-manifest` | Ngữ cảnh `hpa` cần manifest trên `node-app` (Bước 10.1) |
| `run-comparison.sh` báo số run lệch | Còn run dở/thừa trong `~/dataset/raw-eval/<kịch_bản>-seed...` trên loadgen: xóa rồi chạy lại |
| Notebook cảnh báo `dropped` hoặc `replicas_at_start` | Cặp so sánh không hợp lệ: chạy lại cặp đó |
| Replica dao động liên tục khi autoscaler chạy | Còn HPA trên Deployment đó, hai bộ điều khiển tranh nhau: chạy lại `set-context.sh autoscaler` |

## Bảo mật và dữ liệu

- Không đưa `node-ips.env`, token, kubeconfig hay SSH key vào image, ConfigMap hay `bundle/`; không sao chép `/etc/rancher/k3s/k3s.yaml` khỏi `node-app`.
- Không commit `snapshots/` (bản sao cấu hình cụm, chỉ dùng để khôi phục), `bundle/` và `models/` (đã có trong `.gitignore`).
- `dataset/raw-eval/` và `evidence/` là kết quả, được chuyển vào repo. Trước khi commit `evidence/`, rà lại vì `policy-configmap.yaml` và `pods.txt` chứa IP nội bộ của cụm.
- Checkpoint là pickle nên mount read-only và kiểm checksum trước khi nạp.
