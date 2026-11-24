"""Controller autoscaler GAT-GRU + PPO cho Online Boutique (vòng lặp MAPE-K).

Module:
  config      đọc chính sách từ ConfigMap (mount thành thư mục /config)
  monitor     Monitor: thu Prometheus + Jaeger bằng CHÍNH code của load-testing/collect_metrics.py
  features    dựng X/E bằng CHÍNH forecast_data.build_run_arrays (giống hệt lúc huấn luyện)
  analyze     Analyze: GAT-GRU dự báo t+1..t+6, tự kiểm tra với fixture, theo dõi sai số online
  env         hợp đồng quan sát/hành động PPO + lớp Gymnasium bọc cụm thật
  plan        Plan: PPO, lớp an toàn
  execute     Execute: đọc trạng thái và PATCH deployments/scale qua Kubernetes API
  telemetry   /metrics, /healthz, Decision Log (JSONL)
  main        vòng lặp chính
"""
