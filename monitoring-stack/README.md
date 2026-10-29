# Monitoring stack

## Jaeger tracing và service graph data

- **Version:** `jaegertracing/all-in-one:1.60.0`
- **Zipkin receiver:** TCP `9411`, bật bằng `COLLECTOR_ZIPKIN_HOST_PORT=:9411` trong Compose.
- **Storage:** Badger DB (Docker named volume `jaeger_data`, xem `docker-compose.yml`)
- **Sampling configuration:** 100%. Đây là cấu hình lấy mẫu của Istio/Envoy; độ đầy đủ thực tế vẫn phải kiểm tra bằng coverage và không được suy ra chỉ từ sampling rate.
