# Model artifacts

Thư mục này chứa checkpoint, prediction, metrics, tuning history và handoff contract theo model:

```text
artifacts/
├── gatgru/
├── gatlstm/
├── lstm/
├── gru/
└── comparison/
```

Artifact GAT-GRU bàn giao cho RL nằm tại `gatgru/v05_selected_features_final/`. Binary `.pt/.npz` nên được quản lý bằng Git LFS hoặc GitHub Release nếu không commit bằng Git thường; checksum nằm trong `handoff_manifest.json`.

Không ghi output training vào `notebooks/` hoặc `dataset/raw/`.
