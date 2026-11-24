# Notebooks

Notebook được tách khỏi dataset và artifact:

```text
notebooks/
├── dataset/       # workload/session/Jaeger analysis
├── gatgru/        # preprocessing, feature selection, ablation, final training
├── gatlstm/
├── lstm/
├── gru/
├── comparison/    # matched-window và final model comparison
└── deployment_evaluation/  # đánh giá autoscaler đã triển khai: HPA và GAT-GRU + PPO trên cùng trace
```

`deployment_evaluation/` đọc kết quả kiểm tra ngắn trong `dataset/raw-eval/` (do `autoscaler/scripts/run-comparison.sh` tạo), không đọc `dataset/raw/`.

Mọi notebook model đọc raw runs từ `dataset/raw/` và frozen split từ `dataset/splits.json`. Derived preprocessing cache và output của notebook baseline nằm trong `dataset/processed/<model>/`; checkpoint, prediction và metrics của các experiment có version nằm trong `artifacts/<model>/`.

Notebook đã train giữ nguyên saved outputs. Khi chạy notebook, chỉ sử dụng các đường dẫn canonical nêu trên.

