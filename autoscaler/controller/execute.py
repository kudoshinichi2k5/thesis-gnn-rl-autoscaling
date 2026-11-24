"""Execute: đọc trạng thái Deployment và PATCH subresource scale qua Kubernetes API.

Quyền cần (autoscaler/deploy/rbac.yaml): get/list deployments, get/patch deployments/scale trong
namespace ứng dụng. Mọi PATCH dùng field_manager (mặc định gnn-rl-autoscaler) để managedFields của
Deployment ghi lại controller là bên đặt spec.replicas (bằng chứng triển khai).
"""
from __future__ import annotations

import logging

log = logging.getLogger("autoscaler.execute")


def parse_cpu(q) -> float | None:
    """'150m' -> 0.15, '1' -> 1.0, '0.5' -> 0.5."""
    if q is None:
        return None
    q = str(q).strip()
    try:
        return float(q[:-1]) / 1000.0 if q.endswith("m") else float(q)
    except ValueError:
        return None


class K8sClient:
    def __init__(self, namespace: str, field_manager: str, container: str = "server"):
        from kubernetes import client, config
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()                       # chạy ngoài cụm (thử nghiệm)
        self.apps = client.AppsV1Api()
        self.ns = namespace
        self.fm = field_manager
        self.container = container

    def state(self, services) -> dict:
        """-> {svc: {'replicas': spec, 'ready': ready, 'cpu_request': core/pod}}"""
        out = {}
        for d in self.apps.list_namespaced_deployment(self.ns).items:
            name = d.metadata.name
            if name not in services:
                continue
            req = None
            for c in d.spec.template.spec.containers:
                if c.name == self.container and c.resources and c.resources.requests:
                    req = parse_cpu(c.resources.requests.get("cpu"))
            out[name] = {"replicas": int(d.spec.replicas or 0), "ready": int(d.status.ready_replicas or 0),
                         "cpu_request": req}
        missing = [s for s in services if s not in out]
        if missing:
            raise RuntimeError(f"Không thấy Deployment: {missing}")
        return out

    def scale(self, service: str, replicas: int) -> None:
        self.apps.patch_namespaced_deployment_scale(
            name=service, namespace=self.ns, body={"spec": {"replicas": int(replicas)}},
            field_manager=self.fm)
        log.info("scale %s -> %d", service, replicas)
