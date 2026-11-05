# Notebooks

```text
notebooks/
├── dataset/       # phân tích workload và chất lượng telemetry
├── gatgru/        # GAT-GRU final và ablation
├── gatlstm/       # baseline graph recurrent
├── lstm/          # baseline temporal
├── gru/           # baseline temporal
├── comparison/    # so sánh forecast trên frozen split
└── rl/            # Digital Twin + PPO trên Kaggle
```

Notebook RL hiện hành duy nhất là
[train_ppo_kaggle.ipynb](rl/train_ppo_kaggle.ipynb). Notebook:

1. nạp `digital_twin_manifest.json` bằng `KAGGLE_TWIN_MANIFEST`;
2. chỉ dùng calibration train/validation để fit CPU, capacity, startup delay và
   mixture-density latency;
3. dựng Gymnasium Digital Twin với lifecycle Pod, adjacency tĩnh + edge động, lan truyền
   tải caller→callee, queue/capacity và surrogate latency; replica nằm trong 1–3;
4. gọi frozen GAT-GRU bằng `gatgru_inference.py`;
5. chặn PPO bằng automatic gates và `TWIN_APPROVED=False`;
6. train PPO multi-seed, chọn best checkpoint trên validation và chỉ mở test sau khi
   protocol đã đóng băng.

Kaggle cần attach repository/dataset calibration và checkpoint
`gatgru_selected_model_seed_4.pt`. Có thể override bằng:

```python
KAGGLE_REPO_ROOT=/kaggle/input/<repo-dataset>
KAGGLE_TWIN_MANIFEST=/kaggle/input/<calibration>/digital_twin_manifest.json
KAGGLE_GATGRU_CHECKPOINT=/kaggle/input/<model>/gatgru_selected_model_seed_4.pt
```

Kết quả surrogate v5–v16 nằm trong `artifacts/rl/` chỉ để audit và không thuộc đường
train hiện tại. Thiết kế đầy đủ ở [RL_DESIGN_PLAN.md](../docs/RL_DESIGN_PLAN.md).
