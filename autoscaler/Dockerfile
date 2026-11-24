# Image controller autoscaler (GAT-GRU + PPO). Build từ THƯ MỤC GỐC repo:
#   docker build -t gnn-rl-autoscaler:0.1.0 -f autoscaler/Dockerfile .
#
# Trước khi build, chạy test offline (không cần cụm):
#   python autoscaler/tests/test_controller_offline.py
FROM python:3.12-slim
WORKDIR /app

# torch CPU đúng phiên bản trong handoff_manifest.json (golden test đã kiểm với bản này)
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.14.1
COPY autoscaler/requirements-controller.txt /tmp/requirements-controller.txt
RUN pip install --no-cache-dir -r /tmp/requirements-controller.txt

# Code dùng chung, KHÔNG sửa: forecast_data.py và forecast_torch.py được hash trong handoff_manifest.json;
# collect_metrics.py là code đã thu dataset, controller dùng lại để thu dữ liệu online giống hệt.
# (forecast_torch.py chỉ import numpy, pandas, torch ở đầu file; optuna chỉ cần khi huấn luyện.)
COPY modeling-common/forecast_data.py modeling-common/forecast_torch.py \
     modeling-common/gatgru_inference.py load-testing/collect_metrics.py /app/modeling_common/
COPY autoscaler/controller/ /app/controller/

ENV PYTHONPATH=/app/modeling_common:/app PYTHONUNBUFFERED=1 CONFIG_DIR=/config
USER 10001
EXPOSE 8000
CMD ["python", "-m", "controller.main"]
