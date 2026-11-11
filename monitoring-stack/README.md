# Monitoring stack

## Jaeger tracing và service graph data

- **Version:** `jaegertracing/all-in-one:1.60.0`
- **Zipkin receiver:** TCP `9411`, bật bằng `COLLECTOR_ZIPKIN_HOST_PORT=:9411` trong Compose.
- **Storage:** Badger DB (Docker named volume `jaeger_data`, TTL span 3 giờ cho campaign dài; xem `docker-compose.yml`)
- **Sampling configuration:** 10%. Đây là cấu hình lấy mẫu của Istio/Envoy; độ đầy đủ thực tế vẫn phải kiểm tra bằng coverage và không được suy ra chỉ từ sampling rate.
