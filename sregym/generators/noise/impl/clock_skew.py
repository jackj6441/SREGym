"""A non-critical, colocated Pod whose clock is shifted with Chaos Mesh."""

import contextlib
import time
import uuid

from kubernetes.client.rest import ApiException

from sregym.generators.images import STRESS_IMAGE
from sregym.service.kubectl import KubeCtl

CLOCK_SKEW_PROFILE = "clock-skew"
NOISE_PROFILES = (CLOCK_SKEW_PROFILE,)
# Cover both default 1200-second agent stages plus oracle evaluation without
# expiring and recreating the deterministic treatment mid-attempt.
DEFAULT_DURATION_SECONDS = 3600
TIME_OFFSET = "+5m"
WORKLOAD_NAME = "analytics-clock-observer"
RUN_LABEL = "sregym.io/noise-run"


class ClockSkewObserver:
    """Create an owned observer Pod and describe the exact TimeChaos target."""

    readiness_timeout_seconds = 60
    cleanup_timeout_seconds = 30
    poll_interval_seconds = 1

    def __init__(self, kubectl: KubeCtl | None = None):
        self.kubectl = kubectl or KubeCtl()
        self.core_v1 = self.kubectl.core_v1_api
        self.apps_v1 = self.kubectl.apps_v1_api

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
    def _pod_body(name: str, node_name: str, selector_value: str) -> dict:
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "labels": {
                    "app.kubernetes.io/name": WORKLOAD_NAME,
                    "app.kubernetes.io/component": "noise-observer",
                    "sregym.io/noise-profile": CLOCK_SKEW_PROFILE,
                    RUN_LABEL: selector_value,
                },
            },
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "terminationGracePeriodSeconds": 0,
                "nodeSelector": {"kubernetes.io/hostname": node_name},
                "containers": [
                    {
                        "name": "observer",
                        "image": STRESS_IMAGE,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["/bin/sh", "-c"],
                        "args": ["while true; do date -u '+%Y-%m-%dT%H:%M:%SZ'; sleep 5; done"],
                        "resources": {
                            "requests": {"cpu": "10m", "memory": "16Mi"},
                            "limits": {"cpu": "50m", "memory": "64Mi"},
                        },
                    }
                ],
            },
        }

    def inject(self, *, namespace: str, target_deployment: str) -> dict[str, str]:
        node_name = self._target_node(namespace, target_deployment)
        selector_value = uuid.uuid4().hex[:12]
        name = f"{WORKLOAD_NAME}-{selector_value[:8]}"
        body = self._pod_body(name, node_name, selector_value)

        created = False
        try:
            self.core_v1.create_namespaced_pod(namespace=namespace, body=body)
            created = True
            deadline = time.monotonic() + self.readiness_timeout_seconds
            while True:
                pod = self.core_v1.read_namespaced_pod(name=name, namespace=namespace)
                phase = pod.status.phase if pod.status else "Pending"
                statuses = (pod.status.container_statuses or []) if pod.status else []
                if phase == "Running" and any(status.state and status.state.running is not None for status in statuses):
                    return {
                        "name": name,
                        "namespace": namespace,
                        "node": node_name,
                        "selector_value": selector_value,
                    }
                if phase in {"Failed", "Succeeded"}:
                    raise RuntimeError(f"Clock skew observer {namespace}/{name} ended before becoming active ({phase})")
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Clock skew observer {namespace}/{name} did not start within the timeout")
                time.sleep(self.poll_interval_seconds)
        except Exception:
            if created:
                with contextlib.suppress(Exception):
                    self.core_v1.delete_namespaced_pod(name=name, namespace=namespace, grace_period_seconds=0)
            raise

    @staticmethod
    def time_chaos_spec(resource: dict[str, str], *, duration_seconds: int) -> dict:
        if duration_seconds < 1:
            raise ValueError("Clock skew duration must be at least 1 second")
        return {
            "mode": "one",
            "selector": {
                "namespaces": [resource["namespace"]],
                "labelSelectors": {RUN_LABEL: resource["selector_value"]},
            },
            "containerNames": ["observer"],
            "timeOffset": TIME_OFFSET,
            "duration": f"{duration_seconds}s",
        }

    def delete(self, resource: dict[str, str]) -> None:
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
                raise TimeoutError(f"Clock skew observer {namespace}/{name} was not deleted within the timeout")
            time.sleep(self.poll_interval_seconds)
