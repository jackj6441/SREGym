"""A non-critical, colocated Pod whose clock is shifted with Chaos Mesh."""

import contextlib
import logging
import time
import uuid

from kubernetes.client.rest import ApiException

from sregym.generators.images import STRESS_IMAGE
from sregym.service.kubectl import KubeCtl

logger = logging.getLogger(__name__)

CLOCK_SKEW_PROFILE = "clock-skew"
NOISE_PROFILES = (CLOCK_SKEW_PROFILE,)
# Cover both default 1200-second agent stages plus oracle evaluation without
# expiring and recreating the deterministic treatment mid-attempt.
DEFAULT_DURATION_SECONDS = 3600
TIME_OFFSET = "+5m"
WORKLOAD_NAME = "analytics-clock-observer"
RUN_LABEL = "sregym.io/noise-run"
OBSERVER_CONTAINER = "observer"
REFERENCE_CONTAINER = "reference"
CLOCK_REFERENCE_VOLUME = "clock-reference"
CLOCK_REFERENCE_PATH = "/clock-reference"
CLOCK_SKEW_MARKER = f"{CLOCK_REFERENCE_PATH}/clock-skew-active"
CLOCK_SKEW_FAULT_LOG = "CLOCK_SKEW_FAULT"
CLOCK_SKEW_THRESHOLD_SECONDS = 240


class ClockSkewObserver:
    """Create an isolated, observable clock-skew fault and its exact TimeChaos target."""

    readiness_timeout_seconds = 60
    treatment_effect_timeout_seconds = 90
    cleanup_timeout_seconds = 30
    poll_interval_seconds = 1

    def __init__(self, kubectl: KubeCtl | None = None):
        self.kubectl = kubectl or KubeCtl()
        self.core_v1 = self.kubectl.core_v1_api
        self.apps_v1 = self.kubectl.apps_v1_api

    @staticmethod
    def _pod_is_ready(pod) -> bool:
        if not pod.status or pod.status.phase != "Running":
            return False
        return any(
            condition.type == "Ready" and condition.status == "True" for condition in (pod.status.conditions or [])
        )

    @classmethod
    def _pod_ready(cls, pod) -> bool:
        return bool(cls._pod_is_ready(pod) and pod.spec and pod.spec.node_name)

    @staticmethod
    def _container_running(pod, container_name: str) -> bool:
        statuses = (pod.status.container_statuses or []) if pod.status else []
        return any(
            status.name == container_name and status.state and status.state.running is not None for status in statuses
        )

    @classmethod
    def _is_healthy_before_treatment(cls, pod) -> bool:
        return (
            cls._pod_is_ready(pod)
            and cls._container_running(pod, OBSERVER_CONTAINER)
            and cls._container_running(pod, REFERENCE_CONTAINER)
        )

    @classmethod
    def _has_treatment_effect(cls, pod) -> bool:
        if not pod.status or pod.status.phase != "Running" or cls._pod_is_ready(pod):
            return False

        statuses = {status.name: status for status in pod.status.container_statuses or []}
        observer = statuses.get(OBSERVER_CONTAINER)
        reference = statuses.get(REFERENCE_CONTAINER)
        return bool(
            observer
            and observer.state
            and observer.state.running is not None
            and observer.ready is False
            and reference
            and reference.state
            and reference.state.running is not None
            and reference.ready is True
        )

    def _target_node(self, namespace: str, target_deployment: str) -> str:
        deployment = self.apps_v1.read_namespaced_deployment(name=target_deployment, namespace=namespace)
        match_labels = dict(deployment.spec.selector.match_labels or {})
        if not match_labels:
            raise RuntimeError(f"Deployment {namespace}/{target_deployment} has no matchLabels selector")

        selector = ",".join(f"{key}={value}" for key, value in sorted(match_labels.items()))
        pods = self.core_v1.list_namespaced_pod(namespace=namespace, label_selector=selector).items
        ready_pods = sorted((pod for pod in pods if self._pod_ready(pod)), key=lambda pod: pod.metadata.name)
        fallback_control_plane_node = None
        for pod in ready_pods:
            node_name = pod.spec.node_name
            node = self.core_v1.read_node(name=node_name)
            node_labels = node.metadata.labels or {}
            if not any(
                role in node_labels
                for role in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")
            ):
                return node_name
            if fallback_control_plane_node is None:
                fallback_control_plane_node = node_name

        if fallback_control_plane_node is not None:
            return fallback_control_plane_node

        raise RuntimeError(f"No Ready pod found for Deployment {namespace}/{target_deployment}")

    @staticmethod
    def _pod_body(name: str, node_name: str, selector_value: str) -> dict:
        volume_mount = {"name": CLOCK_REFERENCE_VOLUME, "mountPath": CLOCK_REFERENCE_PATH}
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
                "volumes": [{"name": CLOCK_REFERENCE_VOLUME, "emptyDir": {}}],
                "containers": [
                    {
                        "name": REFERENCE_CONTAINER,
                        "image": STRESS_IMAGE,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["/bin/sh", "-ec"],
                        "args": [
                            "while true; do "
                            f"date +%s > {CLOCK_REFERENCE_PATH}/epoch.tmp && "
                            f"mv {CLOCK_REFERENCE_PATH}/epoch.tmp {CLOCK_REFERENCE_PATH}/epoch; "
                            "sleep 1; "
                            "done"
                        ],
                        "volumeMounts": [volume_mount],
                        "resources": {
                            "requests": {"cpu": "10m", "memory": "16Mi"},
                            "limits": {"cpu": "50m", "memory": "64Mi"},
                        },
                    },
                    {
                        "name": OBSERVER_CONTAINER,
                        "image": STRESS_IMAGE,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["/bin/sh", "-c"],
                        "args": [
                            "consecutive=0; reported=0; "
                            "while true; do "
                            f"reference=$(cat {CLOCK_REFERENCE_PATH}/epoch 2>/dev/null || true); "
                            "observed=$(date +%s); "
                            'if [ -n "$reference" ]; then '
                            "delta=$((observed - reference)); "
                            'absolute=$delta; if [ "$absolute" -lt 0 ]; then absolute=$((-absolute)); fi; '
                            f'if [ "$absolute" -ge {CLOCK_SKEW_THRESHOLD_SECONDS} ]; then '
                            "consecutive=$((consecutive + 1)); "
                            "else consecutive=0; reported=0; "
                            f"rm -f {CLOCK_SKEW_MARKER}; fi; "
                            'if [ "$consecutive" -ge 3 ]; then '
                            f"touch {CLOCK_SKEW_MARKER}; "
                            'if [ "$reported" -eq 0 ]; then '
                            f"echo '{CLOCK_SKEW_FAULT_LOG} offset_seconds='\"$delta\"; reported=1; fi; "
                            "fi; "
                            "fi; sleep 1; "
                            "done"
                        ],
                        "volumeMounts": [volume_mount],
                        "readinessProbe": {
                            "exec": {"command": ["sh", "-ec", f"test ! -f {CLOCK_SKEW_MARKER}"]},
                            "initialDelaySeconds": 1,
                            "periodSeconds": 2,
                        },
                        "resources": {
                            "requests": {"cpu": "10m", "memory": "16Mi"},
                            "limits": {"cpu": "50m", "memory": "64Mi"},
                        },
                    },
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
                if self._is_healthy_before_treatment(pod):
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

    def wait_for_treatment_effect(self, resource: dict[str, str]) -> None:
        """Fail closed unless TimeChaos produces the intended isolated Pod fault."""
        deadline = time.monotonic() + self.treatment_effect_timeout_seconds
        while True:
            try:
                pod = self.core_v1.read_namespaced_pod(name=resource["name"], namespace=resource["namespace"])
                if self._has_treatment_effect(pod):
                    logs = self.core_v1.read_namespaced_pod_log(
                        name=resource["name"],
                        namespace=resource["namespace"],
                        container=OBSERVER_CONTAINER,
                        tail_lines=20,
                    )
                    if CLOCK_SKEW_FAULT_LOG in (logs or ""):
                        return
            except ApiException as exc:
                if exc.status == 404:
                    raise RuntimeError(
                        f"Clock skew observer {resource['namespace']}/{resource['name']} disappeared before treatment"
                    ) from exc
                # API-server throttling and a briefly unavailable log endpoint
                # are expected setup races. Keep trying until the fixed deadline.
                logger.warning("Waiting for clock-skew treatment observation: %s", exc)
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Clock skew fault did not become observable for {resource['namespace']}/{resource['name']}"
                )
            time.sleep(self.poll_interval_seconds)

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
            "containerNames": [OBSERVER_CONTAINER],
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
