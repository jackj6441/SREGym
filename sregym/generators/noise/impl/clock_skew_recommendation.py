"""Pod-scoped clock skew in Hotel Reservation's optional recommendation path.

Design: docs/agents/clock-skew-search-rate-design.md.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests
from kubernetes.client.rest import ApiException

from sregym.generators.noise.impl.clock_skew import TIME_OFFSET
from sregym.generators.workload.hotel_search import KubectlPortForward
from sregym.service.kubectl import KubeCtl

RECOMMENDATION_CONTAINER = "hotel-reserv-recommendation"
RECOMMENDATION_PATH = "/recommendations?require=dis&lat=38.0235&lon=-122.095"


@dataclass(frozen=True)
class TreatmentBaseline:
    endpoints: dict[str, tuple[str, ...]]
    queue_depth: float
    search_success_rate: float


@dataclass(frozen=True)
class TreatmentEffect:
    recommendation_status: int
    queue_depth: float
    search_success_rate: float


class ClockSkewRecommendation:
    """Target one existing Pod and fail closed unless its business result breaks."""

    treatment_effect_timeout_seconds = 90
    recovery_timeout_seconds = 30
    poll_interval_seconds = 2

    def __init__(self, kubectl: KubeCtl | None = None, workload=None):
        self.kubectl = kubectl or KubeCtl()
        self.workload = workload

    @staticmethod
    def _ready(pod) -> bool:
        return bool(
            pod.status
            and pod.status.phase == "Running"
            and any(
                condition.type == "Ready" and condition.status == "True" for condition in pod.status.conditions or []
            )
        )

    def select_target(self, namespace: str) -> dict[str, str]:
        deployment = self.kubectl.apps_v1_api.read_namespaced_deployment(name="recommendation", namespace=namespace)
        labels = dict(deployment.spec.selector.match_labels or {})
        if not labels:
            raise RuntimeError("recommendation Deployment has no Pod selector")
        selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        pods = self.kubectl.core_v1_api.list_namespaced_pod(namespace=namespace, label_selector=selector).items
        ready = [pod for pod in pods if self._ready(pod)]
        if len(ready) != 1:
            raise RuntimeError(f"expected exactly one Ready recommendation Pod, found {len(ready)}")
        pod = ready[0]
        if RECOMMENDATION_CONTAINER not in {container.name for container in pod.spec.containers}:
            raise RuntimeError("recommendation Pod does not contain the expected application container")
        return {"name": pod.metadata.name, "uid": pod.metadata.uid, "namespace": namespace}

    @staticmethod
    def time_chaos_spec(target: dict[str, str], *, duration_seconds: int) -> dict:
        if duration_seconds < 1:
            raise ValueError("clock-skew duration must be positive")
        return {
            "mode": "one",
            "selector": {"pods": {target["namespace"]: [target["name"]]}},
            "containerNames": [RECOMMENDATION_CONTAINER],
            "timeOffset": TIME_OFFSET,
            "duration": f"{duration_seconds}s",
        }

    def _endpoints(self, namespace: str) -> dict[str, tuple[str, ...]]:
        result = {}
        for name in ("frontend", "search", "rate"):
            endpoint = self.kubectl.core_v1_api.read_namespaced_endpoints(name=name, namespace=namespace)
            result[name] = tuple(
                sorted(address.ip for subset in endpoint.subsets or [] for address in subset.addresses or [])
            )
            if not result[name]:
                raise RuntimeError(f"primary path Service {name} has no Ready endpoints")
        return result

    def _recommendation_response(self, namespace: str) -> requests.Response:
        forward = KubectlPortForward(namespace, "frontend", 5000)
        try:
            port = forward.start()
            return requests.get(f"http://127.0.0.1:{port}{RECOMMENDATION_PATH}", timeout=5)
        finally:
            forward.stop()

    def _optional_failure_logged(self, namespace: str) -> bool:
        pods = self.kubectl.core_v1_api.list_namespaced_pod(
            namespace=namespace, label_selector="io.kompose.service=frontend"
        ).items
        ready = [pod for pod in pods if self._ready(pod)]
        if len(ready) != 1:
            raise RuntimeError("expected exactly one Ready frontend Pod during clock-skew preflight")
        logs = self.kubectl.core_v1_api.read_namespaced_pod_log(
            name=ready[0].metadata.name,
            namespace=namespace,
            container="hotel-reserv-frontend",
            since_seconds=60,
            tail_lines=1000,
        )
        return "Optional hotel recommendation unavailable" in logs and "ahead of frontend clock" in logs

    def capture_baseline(self, target: dict[str, str]) -> TreatmentBaseline:
        response = self._recommendation_response(target["namespace"])
        if response.status_code != 200:
            raise RuntimeError(f"recommendation was not healthy before TimeChaos: HTTP {response.status_code}")
        if self.workload is None:
            raise RuntimeError("clock-skew preflight requires the protected search workload")
        metrics = self.workload.metrics.snapshot()
        observed = self.workload.snapshot(10)
        queue_depth = metrics.get("rate_queue_depth", -1)
        if queue_depth < 20 or observed.success_rate > 0.5:
            raise RuntimeError("the primary search-rate fault was not active before TimeChaos")
        return TreatmentBaseline(self._endpoints(target["namespace"]), queue_depth, observed.success_rate)

    def wait_for_treatment_effect(self, target: dict[str, str], baseline: TreatmentBaseline) -> TreatmentEffect:
        deadline = time.monotonic() + self.treatment_effect_timeout_seconds
        while time.monotonic() < deadline:
            pod = self.kubectl.core_v1_api.read_namespaced_pod(name=target["name"], namespace=target["namespace"])
            if pod.metadata.uid != target["uid"] or not self._ready(pod):
                raise RuntimeError("recommendation Pod changed identity or readiness during TimeChaos")
            response = self._recommendation_response(target["namespace"])
            if (
                response.status_code == 502
                and "ahead of frontend clock" in response.text
                and self._optional_failure_logged(target["namespace"])
            ):
                metrics = self.workload.metrics.snapshot()
                observed = self.workload.snapshot(10)
                if self._endpoints(target["namespace"]) != baseline.endpoints:
                    raise RuntimeError("TimeChaos changed primary-path Service endpoints")
                if metrics.get("rate_queue_depth", -1) < 20 or observed.success_rate > 0.5:
                    raise RuntimeError("the primary search-rate fault changed during clock-skew preflight")
                if abs(observed.success_rate - baseline.search_success_rate) > 0.3:
                    raise RuntimeError("primary search success rate changed too much during clock-skew preflight")
                return TreatmentEffect(response.status_code, metrics["rate_queue_depth"], observed.success_rate)
            time.sleep(self.poll_interval_seconds)
        raise TimeoutError("TimeChaos did not produce a future-dated recommendation failure")

    def wait_for_recovery(self, target: dict[str, str]) -> None:
        """Confirm normal recommendation results after removing TimeChaos."""
        deadline = time.monotonic() + self.recovery_timeout_seconds
        while time.monotonic() < deadline:
            try:
                pod = self.kubectl.core_v1_api.read_namespaced_pod(name=target["name"], namespace=target["namespace"])
            except ApiException as exc:
                if exc.status == 404:
                    return
                raise
            if pod.metadata.uid != target["uid"]:
                # An agent replaced the target Pod. The old treatment selector
                # cannot affect this new Pod; application cleanup follows.
                return
            if self._ready(pod) and self._recommendation_response(target["namespace"]).status_code == 200:
                return
            time.sleep(self.poll_interval_seconds)
        raise TimeoutError("recommendation responses did not recover after deleting TimeChaos")
