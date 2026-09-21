#!/bin/bash
# Chạy từ WSL2/Windows

export KUBECONFIG=~/.kube/config
OB_DIR="k8s-manifests/online-boutique"
mkdir -p $OB_DIR/chart

echo "[1/7] Clone repository chính thức của microservices-demo..."
rm -rf /tmp/microservices-demo
git clone --depth 1 https://github.com/GoogleCloudPlatform/microservices-demo.git /tmp/microservices-demo
CURRENT_HASH=$(cd /tmp/microservices-demo && git rev-parse HEAD)

echo "[2/7] Vendor Helm Chart vào repo KLTN (giữ nguyên gốc)..."
rsync -av --delete /tmp/microservices-demo/helm-chart/ $OB_DIR/chart/

echo "[3/7] Xây dựng values-override.yaml dựa trên Baseline..."
# Tính toán tài nguyên (Per Service):
# Cần chạy 10 services. Nếu mỗi service xin 150m CPU / 128Mi RAM
# Envoy sidecar xin ~ 100m CPU / 128Mi RAM
# Tổng request per pod = 250m CPU / 256Mi RAM
# Tổng 10 pods = 2500m CPU / 2560Mi RAM => Hoàn toàn nằm trong phần trống (3600m / 7.5GB) của node-app.
cat << YAML_EOF > $OB_DIR/values-override.yaml
frontend:
  type: NodePort
  nodePort: 30080

# Kỹ thuật vô hiệu hóa loadgenerator bằng cách ép replicas về 0 
# (Tránh phải xóa Deployment thủ công và giữ vẹn toàn Helm release)
loadGenerator:
  replicas: 0

# Tối ưu hóa tài nguyên cho từng nhóm ngôn ngữ lập trình
# Java (Ad, Payment), C# (Cart) ăn RAM nhiều hơn. Go/C++ ăn CPU nhiều hơn khi spike.
adservice:
  resources: { requests: { cpu: 150m, memory: 256Mi }, limits: { cpu: 500m, memory: 512Mi } }
cartservice:
  resources: { requests: { cpu: 150m, memory: 256Mi }, limits: { cpu: 500m, memory: 512Mi } }
checkoutservice:
  resources: { requests: { cpu: 150m, memory: 128Mi }, limits: { cpu: 500m, memory: 256Mi } }
currencyservice:
  resources: { requests: { cpu: 150m, memory: 128Mi }, limits: { cpu: 500m, memory: 256Mi } }
emailservice:
  resources: { requests: { cpu: 100m, memory: 128Mi }, limits: { cpu: 300m, memory: 256Mi } }
frontend:
  resources: { requests: { cpu: 150m, memory: 128Mi }, limits: { cpu: 500m, memory: 256Mi } }
paymentservice:
  resources: { requests: { cpu: 150m, memory: 256Mi }, limits: { cpu: 500m, memory: 512Mi } }
productcatalogservice:
  resources: { requests: { cpu: 150m, memory: 128Mi }, limits: { cpu: 500m, memory: 256Mi } }
recommendationservice:
  resources: { requests: { cpu: 150m, memory: 256Mi }, limits: { cpu: 500m, memory: 512Mi } }
shippingservice:
  resources: { requests: { cpu: 150m, memory: 128Mi }, limits: { cpu: 500m, memory: 256Mi } }
YAML_EOF

echo "[4/7] Viết script patch nodeSelector (chỉ định chạy trên role=app)..."
cat << 'SH_EOF' > $OB_DIR/patch-nodeselector.sh
#!/bin/bash
export KUBECONFIG=~/.kube/config
echo "Patching nodeSelector cho namespace online-boutique..."
DEPLOYS=$(kubectl get deployment -n online-boutique -o custom-columns=":metadata.name" --no-headers)
for dep in $DEPLOYS; do
  kubectl patch deployment $dep -n online-boutique -p '{"spec":{"template":{"spec":{"nodeSelector":{"role":"app"}}}}}'
done
echo "Patch hoàn tất!"
SH_EOF
chmod +x $OB_DIR/patch-nodeselector.sh

echo "[5/7] Deploy qua Helm..."
helm upgrade --install online-boutique $OB_DIR/chart/ \
  -n online-boutique \
  -f $OB_DIR/values-override.yaml

echo "[6/7] Áp dụng Patch NodeSelector..."
# Tạm gán nhãn cho node-app để patch khớp
kubectl label nodes node-app role=app --overwrite
$OB_DIR/patch-nodeselector.sh

echo "[7/7] Tạo README document..."
cat << MD_EOF > $OB_DIR/README.md
# Online Boutique - Microservices Benchmark

## Nguồn gốc
- **Vendor từ:** \`GoogleCloudPlatform/microservices-demo\`
- **Commit hash:** \`${CURRENT_HASH}\`
- **Thời điểm copy:** $(date +'%Y-%m-%d')

## Tùy chỉnh (Overrides)
Mọi tùy chỉnh được đặt tại \`values-override.yaml\`:
1. **Frontend:** Cố định NodePort \`30080\`.
2. **LoadGenerator:** Đã bị vô hiệu hóa (\`replicas: 0\`) để nhường quyền sinh tải cho hệ thống Locust (node-loadgen).
3. **Tài nguyên (Resources):** Đã phân chia cứng Requests/Limits cho 10 dịch vụ. Dành ra không gian đệm cho Envoy sidecar. Tổng requests ~2500m CPU, nằm trong giới hạn 4 vCPU của cụm K3s.
4. **NodeSelector:** Bản chart gốc không hỗ trợ global nodeSelector. Cần chạy script \`patch-nodeselector.sh\` sau mỗi lần \`helm install/upgrade\` để ép toàn bộ pod chạy trên node có label \`role=app\`.

## Quản trị Vòng đời (Lifecycle)
Chạy lại thực nghiệm hoặc reset môi trường:
\`\`\`bash
helm upgrade --install online-boutique ./chart -n online-boutique -f values-override.yaml
./patch-nodeselector.sh
\`\`\`
MD_EOF

echo "Dọn dẹp rác..."
rm -rf /tmp/microservices-demo

echo "Commit vào git..."
git add $OB_DIR/ cluster-setup/03-deploy-online-boutique.sh
git commit -m "feat(infra): deploy online-boutique with strict resources and zero loadgenerator"

echo "✅ Hoàn tất! Chờ 1-2 phút để các pod Pull Image và khởi động."
