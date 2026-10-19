"""Ad rollout failure for the checkout alert experiment.

Design and runtime gates: docs/agents/astronomy-checkout-ad-image-noise-design.md.
"""

from __future__ import annotations

import copy
import json
import subprocess
import time
import uuid
from urllib.parse import quote

from sregym.service.rollout import deployment_rollout_complete

ASTRONOMY_AD_IMAGE_PROFILE = "astronomy-ad-image"
_NAMESPACE = "astronomy-shop"
_PRIMARY_SERVICES = ("frontend-proxy", "frontend", "checkout", "cart", "product-catalog")
_IMAGE_PULL_REASONS = frozenset({"ErrImagePull", "ImagePullBackOff"})
_PROMETHEUS_PROXY = "/api/v1/namespaces/observe/services/prometheus-server:80/proxy/api/v1"


class AstronomyAdImageTreatment:
    """Keep a failed ad image rollout active until final cleanup."""

    def __init__(self, kubectl, namespace: str, problem):
        if namespace != _NAMESPACE:
            raise ValueError(f"{ASTRONOMY_AD_IMAGE_PROFILE} requires {_NAMESPACE}")
        self.kubectl = kubectl
        self.namespace = namespace
        self.problem = problem
        self.original_image: str | None = None
        self.original_replicas: int | None = None
        self.container_name: str | None = None
        self.bad_image: str | None = None
        self._patched = False
        self.evidence: dict = {"profile": ASTRONOMY_AD_IMAGE_PROFILE, "preflight": {"status": "not_started"}}

    def _query(self, expression: str) -> list[dict]:
        path = f"{_PROMETHEUS_PROXY}/query?query={quote(expression, safe='')}"
        result = subprocess.run(["kubectl", "get", "--raw", path], text=True, capture_output=True, timeout=15)
        if result.returncode:
            raise RuntimeError(f"Prometheus query failed: {result.stderr.strip()}")
        payload = json.loads(result.stdout)
        if payload.get("status") != "success":
            raise RuntimeError(f"Prometheus query unsuccessful: {payload.get('error', 'unknown error')}")
        return payload["data"]["result"]

    def _firing_alerts(self, match_labels: dict[str, str]) -> list[dict]:
        result = subprocess.run(
            ["kubectl", "get", "--raw", f"{_PROMETHEUS_PROXY}/alerts"],
            text=True, capture_output=True, timeout=15,
        )
        if result.returncode:
            raise RuntimeError(f"Prometheus alert query failed: {result.stderr.strip()}")
        payload = json.loads(result.stdout)
        if payload.get("status") != "success" or not isinstance(payload.get("data", {}).get("alerts"), list):
            raise RuntimeError("Prometheus alert response was incomplete")
        return [
            alert for alert in payload["data"]["alerts"]
            if alert.get("state") == "firing"
            and all(alert.get("labels", {}).get(key) == value for key, value in match_labels.items())
        ]

    def _scalar(self, expression: str) -> float | None:
        result = self._query(expression)
        return float(result[0]["value"][1]) if result else None

    def _endpoint_rates(self, metric: str) -> dict[tuple[str, str], float]:
        expression = (
            f'sum by (method,name) (rate({metric}{{namespace="{self.namespace}",name!="Aggregated"}}[2m]))'
        )
        return {
            (item["metric"].get("method", ""), item["metric"].get("name", "")): float(item["value"][1])
            for item in self._query(expression)
        }

    def _endpoints(self) -> dict[str, tuple]:
        endpoints = {}
        for service in (*_PRIMARY_SERVICES, "ad"):
            resource = self.kubectl.core_v1_api.read_namespaced_endpoints(service, self.namespace)
            endpoints[service] = tuple(
                sorted(
                    (address.ip, port.port)
                    for subset in resource.subsets or []
                    for address in subset.addresses or []
                    for port in subset.ports or []
                )
            )
            if not endpoints[service]:
                raise RuntimeError(f"{service} has no Ready Service endpoint")
        return endpoints

    def _primary_address(self) -> str | None:
        deployment = self.kubectl.get_deployment("checkout", self.namespace)
        return next(
            (
                env.value
                for container in deployment.spec.template.spec.containers
                for env in container.env or []
                if env.name == "PRODUCT_CATALOG_ADDR"
            ),
            None,
        )

    def _snapshot(self) -> dict:
        requests = self._endpoint_rates("locust_requests_num_requests")
        failures = self._endpoint_rates("locust_requests_num_failures")
        endpoints = {}
        for key in (("POST", "/api/checkout"), ("GET", "/api/cart"), ("POST", "/api/cart")):
            rate = requests.get(key, 0.0)
            endpoints[f"{key[0]} {key[1]}"] = {
                "request_rps": rate,
                "failure_rps": failures.get(key, 0.0),
                "failure_ratio": failures.get(key, 0.0) / rate if rate else None,
            }
        alert = self._firing_alerts({
            "namespace": self.namespace, "alertname": "HighRequestErrorRate",
            "method": "POST", "name": "/api/checkout",
        })
        ad_rps = self._scalar(
            f'sum(rate(traces_span_metrics_calls_total{{namespace="{self.namespace}",'
            'service_name="ad",span_kind="SPAN_KIND_SERVER"}[5m]))'
        )
        return {
            "primary_address": self._primary_address(),
            "service_endpoints": self._endpoints(),
            "endpoint_rates": endpoints,
            "checkout_alert_firing": bool(alert),
            "ad_server_rps": ad_rps,
        }

    @staticmethod
    def _primary_symptom_present(snapshot: dict) -> bool:
        checkout = snapshot["endpoint_rates"]["POST /api/checkout"]
        carts = (snapshot["endpoint_rates"]["GET /api/cart"], snapshot["endpoint_rates"]["POST /api/cart"])
        return bool(
            snapshot["primary_address"] == "product-catalog:8082"
            and snapshot["checkout_alert_firing"]
            and checkout["request_rps"] >= 0.01
            and checkout["failure_ratio"] is not None
            and checkout["failure_ratio"] >= 0.9
            and all(
                cart["request_rps"] >= 0.01
                and cart["failure_ratio"] is not None
                and cart["failure_ratio"] <= 0.05
                for cart in carts
            )
        )

    @classmethod
    def _behavior_unchanged(cls, before: dict, after: dict) -> bool:
        if not cls._primary_symptom_present(after):
            return False
        for endpoint in ("POST /api/checkout", "GET /api/cart", "POST /api/cart"):
            if after["endpoint_rates"][endpoint]["request_rps"] < max(
                0.01, before["endpoint_rates"][endpoint]["request_rps"] * 0.2
            ):
                return False
        return (after["ad_server_rps"] or 0) >= max(0.01, (before["ad_server_rps"] or 0) * 0.2)

    def _oracle_still_fails(self) -> bool:
        result = self.problem.mitigation_oracle.evaluate()
        return result.get("success") is False

    def _wait_for_unchanged_behavior(self, before: dict, timeout: float = 90) -> dict:
        """Require two passing traffic snapshots after a noise fault."""
        deadline = time.monotonic() + timeout
        consecutive_passes = 0
        after = None
        while time.monotonic() < deadline:
            after = self._snapshot()
            if after["service_endpoints"] != before["service_endpoints"]:
                raise RuntimeError("Astronomy Service endpoints changed under noise")
            if not self._oracle_still_fails():
                raise RuntimeError("Primary checkout dependency oracle passed under noise")
            if self._behavior_unchanged(before, after):
                consecutive_passes += 1
                if consecutive_passes == 2:
                    return after
            else:
                consecutive_passes = 0
            time.sleep(5)
        raise RuntimeError(
            "Checkout/cart traffic did not satisfy the non-interference gate under noise: "
            f"before={before['endpoint_rates']}, after={after['endpoint_rates'] if after else None}, "
            f"ad_rps_before={before['ad_server_rps']}, "
            f"ad_rps_after={after['ad_server_rps'] if after else None}"
        )

    def _wait_for_primary(self, timeout: float = 300) -> dict:
        deadline = time.monotonic() + timeout
        last_error = "no checkout traffic or firing endpoint alert yet"
        while time.monotonic() < deadline:
            try:
                snapshot = self._snapshot()
                if self._primary_symptom_present(snapshot) and (snapshot["ad_server_rps"] or 0) >= 0.05:
                    return snapshot
                last_error = (
                    f"primary symptom or ad traffic missing: endpoint_rates={snapshot['endpoint_rates']}, "
                    f"ad_server_rps={snapshot['ad_server_rps']}"
                )
            except (RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                last_error = str(exc)
            time.sleep(5)
        raise RuntimeError(f"Astronomy noise primary preflight timed out: {last_error}")

    def _bad_pod(self):
        for pod in self.kubectl.core_v1_api.list_namespaced_pod(self.namespace).items:
            if not pod.metadata.name.startswith("ad-"):
                continue
            if not any(container.image == self.bad_image for container in pod.spec.containers or []):
                continue
            for status in pod.status.container_statuses or []:
                waiting = status.state.waiting if status.state else None
                if waiting and waiting.reason in _IMAGE_PULL_REASONS:
                    return pod, waiting.reason
        return None

    def _wait_for_noise_alert(self, timeout: float = 180) -> tuple[str, str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            bad_pod = self._bad_pod()
            if bad_pod:
                pod, reason = bad_pod
                alerts = self._firing_alerts({
                    "namespace": self.namespace, "alertname": "PodStatusError", "pod": pod.metadata.name,
                })
                if alerts:
                    return pod.metadata.name, reason
            time.sleep(5)
        raise RuntimeError("ad image-pull PodStatusError did not fire")

    def start(self) -> dict:
        deployment = self.kubectl.get_deployment("ad", self.namespace)
        if deployment.spec.replicas != 1 or deployment.spec.strategy.type != "RollingUpdate":
            raise RuntimeError("ad must have one replica and a RollingUpdate strategy")
        container = deployment.spec.template.spec.containers[0]
        self.container_name = container.name
        self.original_image = container.image
        self.original_replicas = deployment.spec.replicas

        before = self._wait_for_primary()
        if not self._oracle_still_fails():
            raise RuntimeError("primary checkout dependency oracle did not fail before noise")
        self.evidence["preflight"] = {"status": "baseline_confirmed", "before": before}

        self.bad_image = f"ghcr.io/open-telemetry/demo:ad-release-{uuid.uuid4().hex[:12]}"
        try:
            self._patched = True
            self.kubectl.patch_deployment(
                "ad", self.namespace,
                {"spec": {"template": {"spec": {
                    "containers": [{"name": self.container_name, "image": self.bad_image}]
                }}}},
            )
            pod_name, reason = self._wait_for_noise_alert()
            after = self._wait_for_unchanged_behavior(before)
            self.evidence["preflight"] = {"status": "passed", "before": before, "after": after}
            self.evidence["treatment"] = {"pod": pod_name, "reason": reason, "bad_image": self.bad_image}
            return copy.deepcopy(self.evidence)
        except Exception:
            self.evidence["preflight"]["status"] = "failed"
            self.stop()
            raise

    def stop(self) -> dict:
        if not self._patched:
            return copy.deepcopy(self.evidence)
        self.evidence["lifetime"] = {"bad_pod_waiting_before_cleanup": bool(self._bad_pod())}
        self.evidence["cleanup"] = {"status": "incomplete"}
        self.kubectl.patch_deployment(
            "ad", self.namespace,
            {"spec": {"replicas": self.original_replicas, "template": {"spec": {
                "containers": [{"name": self.container_name, "image": self.original_image}]
            }}}},
        )
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            deployment = self.kubectl.get_deployment("ad", self.namespace)
            bad_pods = [
                pod.metadata.name
                for pod in self.kubectl.core_v1_api.list_namespaced_pod(self.namespace).items
                if pod.metadata.name.startswith("ad-")
                and any(container.image == self.bad_image for container in pod.spec.containers or [])
            ]
            if (
                deployment.spec.template.spec.containers[0].image == self.original_image
                and deployment.spec.replicas == self.original_replicas
                and deployment_rollout_complete(deployment)
                and not bad_pods
                and self._endpoints()["ad"]
            ):
                self._patched = False
                self.evidence["cleanup"]["status"] = "confirmed"
                return copy.deepcopy(self.evidence)
            time.sleep(5)
        raise RuntimeError("ad image and rollout restoration could not be confirmed")
