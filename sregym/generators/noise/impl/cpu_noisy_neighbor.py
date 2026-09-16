"""A bounded CPU-heavy workload colocated with an application pod."""

import logging
import time
import uuid

from kubernetes.client.rest import ApiException

from sregym.generators.images import STRESS_IMAGE
from sregym.service.kubectl import KubeCtl

CPU_NOISY_NEIGHBOR_PROFILE = "cpu-noisy-neighbor"
CPU_NOISE_PROFILES = (CPU_NOISY_NEIGHBOR_PROFILE,)
DEFAULT_CPU_WORKERS = 2
DEFAULT_DURATION_SECONDS = 120
WORKLOAD_NAME = "analytics-backfill"

logger = logging.getLogger(__name__)


class CpuNoisyNeighbor:
    """Run a CPU-bound pod on the same worker as a target Deployment pod."""

    readiness_timeout_seconds = 60
    cleanup_timeout_seconds = 30
    poll_interval_seconds = 1

    def __init__(self, kubectl: KubeCtl | None = None):
        self.kubectl = kubectl or KubeCtl()
        self.core_v1 = self.kubectl.core_v1_api
        self.apps_v1 = self.kubectl.apps_v1_api

    @staticmethod
    def _validate(cpu_workers: int, duration_seconds: int) -> None:
        if cpu_workers < 1:
            raise ValueError("CPU noisy neighbor workers must be at least 1")
        if duration_seconds < 1:
            raise ValueError("CPU noisy neighbor duration must be at least 1 second")

    @staticmethod
    def _pod_ready(pod) -> bool:
        if not pod.status or pod.status.phase != "Running" or not pod.spec.node_name:
            return False
        return any(
            condition.type == "Ready" and condition.status == "True" for condition in (pod.status.conditions or [])
        )

    def _target_node(self, namespace: str, target_deployment: str) -> str:
        deployment = self.apps_v1.read_namespaced_deployment(name=target_deployment, namespace=namespace)
        match_labels = dict(deployment.spec.selector.match_labels or {})
        if not match_labels:
            raise RuntimeError(f"Deployment {namespace}/{target_deployment} has no matchLabels selector")

        selector = ",".join(f"{key}={value}" for key, value in sorted(match_labels.items()))
        pods = self.core_v1.list_namespaced_pod(namespace=namespace, label_selector=selector).items
        ready_pods = sorted((pod for pod in pods if self._pod_ready(pod)), key=lambda pod: pod.metadata.name)
        for pod in ready_pods:
            node_name = pod.spec.node_name
            node = self.core_v1.read_node(name=node_name)
            node_labels = node.metadata.labels or {}
            if not any(
                role in node_labels
                for role in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")
            ):
                return node_name

        raise RuntimeError(f"No Ready worker pod found for Deployment {namespace}/{target_deployment}")

    @staticmethod
    def _pod_body(name: str, node_name: str, cpu_workers: int, duration_seconds: int) -> dict:
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "labels": {
                    "app.kubernetes.io/name": WORKLOAD_NAME,
                    "app.kubernetes.io/component": "batch",
                },
            },
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "terminationGracePeriodSeconds": 0,
                "nodeSelector": {"kubernetes.io/hostname": node_name},
                "containers": [
                    {
                        "name": "worker",
                        "image": STRESS_IMAGE,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["/bin/sh", "-c"],
                        "args": [f"exec stress --cpu {cpu_workers} --timeout {duration_seconds}s"],
                        # A CPU limit would turn this into CFS quota throttling
                        # instead of contention with a neighboring workload.
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "16Mi"},
                            "limits": {"memory": "64Mi"},
                        },
                    }
                ],
            },
        }

    def inject(
        self,
        *,
        namespace: str,
        target_deployment: str,
        cpu_workers: int = DEFAULT_CPU_WORKERS,
        duration_seconds: int = DEFAULT_DURATION_SECONDS,
    ) -> dict[str, str]:
        """Create the neighbor and return the exact resource identity it owns."""
        self._validate(cpu_workers, duration_seconds)
        node_name = self._target_node(namespace, target_deployment)
        name = f"{WORKLOAD_NAME}-{uuid.uuid4().hex[:8]}"
        body = self._pod_body(name, node_name, cpu_workers, duration_seconds)

        created = False
        try:
            self.core_v1.create_namespaced_pod(namespace=namespace, body=body)
            created = True
            deadline = time.monotonic() + self.readiness_timeout_seconds
            while True:
                pod = self.core_v1.read_namespaced_pod(name=name, namespace=namespace)
                phase = pod.status.phase if pod.status else "Pending"
                container_statuses = (pod.status.container_statuses or []) if pod.status else []
                if phase == "Running" and any(
                    status.state and status.state.running is not None for status in container_statuses
                ):
                    return {"kind": "pod", "name": name, "namespace": namespace, "node": node_name}
                if phase in {"Failed", "Succeeded"}:
                    raise RuntimeError(f"CPU noisy neighbor {namespace}/{name} ended before becoming active ({phase})")
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"CPU noisy neighbor {namespace}/{name} did not start within the timeout")
                time.sleep(self.poll_interval_seconds)
        except Exception:
            if created:
                try:
                    self.core_v1.delete_namespaced_pod(name=name, namespace=namespace, grace_period_seconds=0)
                except Exception as cleanup_error:  # noqa: BLE001 - preserve the injection failure
                    logger.warning(
                        "Failed to remove partially started workload %s/%s: %s", namespace, name, cleanup_error
                    )
            raise

    def delete(self, resource: dict[str, str]) -> None:
        """Delete one owned neighbor pod and wait until it is actually gone."""
        name = resource["name"]
        namespace = resource["namespace"]
        try:
            self.core_v1.delete_namespaced_pod(name=name, namespace=namespace, grace_period_seconds=0)
        except ApiException as exc:
            if exc.status == 404:
                return
            raise

        deadline = time.monotonic() + self.cleanup_timeout_seconds
        while True:
            try:
                self.core_v1.read_namespaced_pod(name=name, namespace=namespace)
            except ApiException as exc:
                if exc.status == 404:
                    return
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError(f"CPU noisy neighbor {namespace}/{name} was not deleted within the timeout")
            time.sleep(self.poll_interval_seconds)
