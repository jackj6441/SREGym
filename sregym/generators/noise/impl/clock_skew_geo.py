"""Pod-scoped Geo clock skew for the read-only nearby-hotel path.

Design: docs/agents/clock-skew-geo-search-rate-design.md.
"""

from __future__ import annotations

import requests

from sregym.generators.noise.impl.clock_skew_recommendation import ClockSkewRecommendation
from sregym.generators.workload.hotel_search import KubectlPortForward

GEO_CONTAINER = "hotel-reserv-geo"
GEO_PATH = "/nearby-hotels?lat=38.0235&lon=-122.095"


class ClockSkewGeo(ClockSkewRecommendation):
    """Reuse the proven one-Pod lifecycle, with Geo-specific probes and target."""

    def select_target(self, namespace: str) -> dict[str, str]:
        deployment = self.kubectl.apps_v1_api.read_namespaced_deployment(name="geo", namespace=namespace)
        labels = dict(deployment.spec.selector.match_labels or {})
        if not labels:
            raise RuntimeError("Geo Deployment has no Pod selector")
        selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        pods = self.kubectl.core_v1_api.list_namespaced_pod(namespace=namespace, label_selector=selector).items
        ready = [pod for pod in pods if self._ready(pod)]
        if len(ready) != 1:
            raise RuntimeError(f"expected exactly one Ready Geo Pod, found {len(ready)}")
        pod = ready[0]
        if GEO_CONTAINER not in {container.name for container in pod.spec.containers}:
            raise RuntimeError("Geo Pod does not contain the expected application container")
        return {"name": pod.metadata.name, "uid": pod.metadata.uid, "namespace": namespace}

    @staticmethod
    def time_chaos_spec(target: dict[str, str], *, duration_seconds: int) -> dict:
        spec = ClockSkewRecommendation.time_chaos_spec(target, duration_seconds=duration_seconds)
        spec["containerNames"] = [GEO_CONTAINER]
        return spec

    def _endpoints(self, namespace: str) -> dict[str, tuple[str, ...]]:
        result = super()._endpoints(namespace)
        endpoint = self.kubectl.core_v1_api.read_namespaced_endpoints(name="geo", namespace=namespace)
        result["geo"] = tuple(
            sorted(address.ip for subset in endpoint.subsets or [] for address in subset.addresses or [])
        )
        if not result["geo"]:
            raise RuntimeError("primary path Service geo has no Ready endpoints")
        return result

    def _recommendation_response(self, namespace: str) -> requests.Response:
        # Parent lifecycle invokes this hook before, during, and after TimeChaos.
        forward = KubectlPortForward(namespace, "frontend", 5000)
        try:
            port = forward.start()
            return requests.get(f"http://127.0.0.1:{port}{GEO_PATH}", timeout=5)
        finally:
            forward.stop()

    def _optional_failure_logged(self, namespace: str) -> bool:
        pods = self.kubectl.core_v1_api.list_namespaced_pod(
            namespace=namespace, label_selector="io.kompose.service=frontend"
        ).items
        ready = [pod for pod in pods if self._ready(pod)]
        if len(ready) != 1:
            raise RuntimeError("expected exactly one Ready frontend Pod during Geo clock-skew preflight")
        logs = self.kubectl.core_v1_api.read_namespaced_pod_log(
            name=ready[0].metadata.name,
            namespace=namespace,
            container="hotel-reserv-frontend",
            since_seconds=60,
            tail_lines=1000,
        )
        return "Optional nearby hotel browse unavailable" in logs and "ahead of frontend clock" in logs
