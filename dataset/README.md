# Dataset dùng chung

Repository chỉ có một bộ dataset canonical. GAT-GRU, GAT-LSTM, LSTM, GRU và RL đều đọc cùng raw runs trong `dataset/raw/` và cùng frozen split trong `dataset/splits.json`.

```text
dataset/
├── raw/                         # bắt buộc Git track và push
│   ├── mmpp/run_01..run_08/
│   ├── nhpp/run_01..run_08/
│   ├── onoff/run_01..run_08/
│   └── pareto/run_01..run_08/
├── processed/                   # derived cache, bị ignore
│   ├── gatgru/
│   └── lstm/
├── splits.json                  # frozen mapping 24 train / 4 val / 4 test
├── DATASET.md
└── DATASET_GUIDE.md
```

`raw/` hiện có 448 file, khoảng 88,76 MiB; file lớn nhất khoảng 1,54 MiB. Không thêm `dataset/raw/` vào `.gitignore`.

`processed/` không phải dataset thứ hai. Đây là cache dẫn xuất theo pipeline/model, gồm `runs_raw.npz`, window cache, scaler baseline và các file tiện cho offline analysis. Thành viên RL online không cần toàn bộ cache này; RL offline có thể nhận cache qua release/storage riêng hoặc dựng lại từ raw data.

## Run ID namespace

Các split và artifact sử dụng run ID ổn định dạng:

```text
math-load-testing/<scenario>/run_XX
```

`math-load-testing` là namespace định danh, không phải đường dẫn thư mục. Preprocessing giữ nguyên namespace này để dataset fingerprint, split fingerprint và checkpoint contract nhất quán. Vị trí vật lý của raw run luôn là `dataset/raw/<scenario>/run_XX/`.

## Nguồn liên quan

- Sinh tải và thu raw telemetry: [`../load-testing/`](../load-testing/README.md).
- Notebook preprocessing/analysis: [`../notebooks/`](../notebooks/README.md).
- Feature và model contract: [`../configs/`](../configs/).
- Model output: [`../artifacts/`](../artifacts/README.md).

