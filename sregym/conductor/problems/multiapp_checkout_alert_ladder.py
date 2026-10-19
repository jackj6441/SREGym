"""Five-application checkout alert experiment with one target mitigation oracle.

Design and runtime gates: docs/agents/multiapp-checkout-alert-ladder-design.md.
"""

from __future__ import annotations

import copy
import os
import time

import requests

from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.incorrect_port_assignment import IncorrectPortAssignment
from sregym.conductor.problems.resource_request import ResourceRequestTooLarge
from sregym.conductor.problems.scale_pod import ScalePodSocialNet
from sregym.conductor.problems.wrong_service_selector import WrongServiceSelector
from sregym.generators.noise.impl.astronomy_ad_image import AstronomyAdImageTreatment
from sregym.service.apps.app_registry import AppRegistry
from sregym.service.apps.composite_app import CompositeApp
from sregym.service.container_runner import get_container_host_bind_address
from sregym.utils.decorators import mark_fault_injected


PROBLEM_PREFIX = "multiapp_checkout_alert_"
_ALERT_TIMEOUT_SECONDS = 180
_BASELINE_ALERT_NAMES = frozenset({
    "FailedPodsDetected", "KubePodNotReady", "PodStatusError", "PendingPodsDetected",
    "PodSchedulingFailure", "DeploymentNotReady", "ServiceEndpointDown", "HTTPProbeFailure",
    "TiDBStatefulSetNotReady", "TiDBInstanceNotReady", "HighRequestErrorRate",
})


class _ExperimentApps(CompositeApp):
    """Make setup and cleanup failures visible instead of hiding map iterators."""

    def deploy(self):
        for app in self.apps.values():
            app.deploy()

    def start_workload(self):
        for app in self.apps.values():
            if app.name in ("Hotel Reservation", "Social Network"):
                app.create_workload(rate=10, connections=10, duration=30, threads=2)
            app.start_workload()

    def cleanup(self):
        errors = []
        for app in reversed(list(self.apps.values())):
            try:
                app.cleanup()
            except Exception as exc:
                errors.append(f"{app.name}: {type(exc).__name__}: {exc}")
        if errors:
            raise RuntimeError("Five-application cleanup failed: " + "; ".join(errors))


class _MongoRateMemoryRequest(ResourceRequestTooLarge):
    """Use the existing resource-request injector with a valid absent-resources case."""

    def __init__(self):
        super().__init__(app_name="hotel_reservation", faulty_service="mongodb-rate")

    def set_memory_limit(self, deployment_yaml):
        deployment_yaml = copy.deepcopy(deployment_yaml)
        container = deployment_yaml["spec"]["template"]["spec"]["containers"][0]
        container.setdefault("resources", {}).setdefault("requests", {})["memory"] = "1Ti"
        return deployment_yaml


class MultiAppCheckoutAlertLadder(Problem):
    """Hold a fixed fault prefix while grading only the checkout alert repair."""

    def __init__(self, total_faults: int):
        if total_faults not in range(1, 6):
            raise ValueError("total_faults must be between 1 and 5")
        self.total_faults = total_faults
        self.primary = IncorrectPortAssignment()
        registry = AppRegistry()
        apps = [self.primary.app] + [
            registry.get_app_instance(name)
            for name in ("Hotel Reservation", "Social Network", "Fleet Cast", "Blueprint Hotel Reservation")
        ]
        super().__init__(app=_ExperimentApps(apps))
        self.root_cause = self.primary.root_cause
        self.mitigation_oracle = self.primary.mitigation_oracle
        self.ad = AstronomyAdImageTreatment(self.primary.kubectl, self.primary.namespace, self.primary)
        self.secondary = [
            self.ad,
            WrongServiceSelector(app_name="hotel_reservation", faulty_service="recommendation"),
            ScalePodSocialNet(),
            _MongoRateMemoryRequest(),
        ]
        self._active = []
        self.preflight_evidence = {"total_faults": total_faults, "steps": []}

    def _wait_for_clean_baseline(self):
        deadline = time.monotonic() + _ALERT_TIMEOUT_SECONDS
        namespaces = set(self.app.namespaces) | {"tidb-cluster"}
        unexpected = []
        last_observer_error = None
        while time.monotonic() < deadline:
            try:
                unexpected = [
                    alert["labels"] for alert in self.ad._firing_alerts({})
                    if alert.get("labels", {}).get("alertname") in _BASELINE_ALERT_NAMES
                    and (alert.get("labels", {}).get("namespace") in namespaces
                         or alert.get("labels", {}).get("alertname") == "TiDBStatefulSetNotReady")
                ]
                last_observer_error = None
            except RuntimeError as exc:
                last_observer_error = str(exc)
                time.sleep(5)
                continue
            if not unexpected:
                self.preflight_evidence["baseline_alerts"] = []
                return
            time.sleep(5)
        raise RuntimeError(
            f"Five-application baseline has unrelated firing alerts: {unexpected}; "
            f"last observer error: {last_observer_error}"
        )

    @staticmethod
    def _check_agent_tool_connection():
        port = int(os.environ.get("MCP_SERVER_PORT", "9954"))
        hosts = {"127.0.0.1", get_container_host_bind_address()}
        with requests.Session() as session:
            session.trust_env = False
            for host in hosts:
                for service in ("kubectl", "jaeger", "prometheus"):
                    with session.get(f"http://{host}:{port}/{service}/sse", stream=True, timeout=5) as response:
                        response.raise_for_status()

    def _wait_for_alert(self, labels: dict[str, str], symptom_check):
        deadline = time.monotonic() + _ALERT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if symptom_check():
                alerts = self.ad._firing_alerts(labels)
                if alerts:
                    return copy.deepcopy(alerts[0]["labels"])
            time.sleep(5)
        raise RuntimeError(f"Fault symptom and firing alert not confirmed: {labels}")

    def _wait_for_mongodb_scheduling_alert(self):
        deadline = time.monotonic() + _ALERT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            pod = self._unschedulable_mongodb_rate()
            if pod:
                alerts = self.ad._firing_alerts({
                    "alertname": "PodSchedulingFailure", "namespace": "hotel-reservation", "pod": pod,
                })
                if alerts:
                    return copy.deepcopy(alerts[0]["labels"])
            time.sleep(5)
        raise RuntimeError("mongodb-rate was not unschedulable with a firing PodSchedulingFailure")

    def _recommendation_zero_endpoints(self):
        problem = self.secondary[1]
        try:
            problem.validate_fault_preflight()
            deployment = self.primary.kubectl.apps_v1_api.read_namespaced_deployment(
                "recommendation", "hotel-reservation"
            )
            return (deployment.status.available_replicas or 0) > 0
        except Exception:
            return False

    def _social_zero_replicas(self):
        deployment = self.primary.kubectl.apps_v1_api.read_namespaced_deployment(
            "user-service", "social-network"
        )
        return deployment.spec.replicas == 0 and (deployment.status.available_replicas or 0) == 0

    def _unschedulable_mongodb_rate(self):
        pods = self.primary.kubectl.core_v1_api.list_namespaced_pod("hotel-reservation").items
        for pod in pods:
            if not pod.metadata.name.startswith("mongodb-rate-"):
                continue
            if any(condition.type == "PodScheduled" and condition.status == "False"
                   and condition.reason == "Unschedulable" for condition in pod.status.conditions or []):
                return pod.metadata.name
        return None

    def _validate_primary_unchanged(self, before):
        return self.ad._wait_for_unchanged_behavior(before)

    @mark_fault_injected
    def inject_fault(self):
        try:
            self._wait_for_clean_baseline()
            self._active.append(self.primary)
            self.primary.inject_fault()
            before = self.ad._wait_for_primary()
            if not self.ad._oracle_still_fails():
                raise RuntimeError("Primary checkout mitigation oracle did not fail after injection")
            self.preflight_evidence["steps"].append({"fault": 1, "snapshot": before})

            for index, fault in enumerate(self.secondary[: self.total_faults - 1], start=2):
                self._active.append(fault)
                if fault is self.ad:
                    treatment = fault.start()
                    alert_labels = {"alertname": "PodStatusError", "namespace": "astronomy-shop",
                                    "pod": treatment["treatment"]["pod"]}
                else:
                    fault.inject_fault()
                    if index == 3:
                        alert_labels = self._wait_for_alert(
                            {"alertname": "ServiceEndpointDown", "namespace": "hotel-reservation",
                             "instance": "recommendation.hotel-reservation:8085"},
                            self._recommendation_zero_endpoints,
                        )
                    elif index == 4:
                        alert_labels = self._wait_for_alert(
                            {"alertname": "DeploymentNotReady", "namespace": "social-network",
                             "deployment": "user-service"},
                            self._social_zero_replicas,
                        )
                    else:
                        alert_labels = self._wait_for_mongodb_scheduling_alert()
                after = self._validate_primary_unchanged(before)
                self.preflight_evidence["steps"].append(
                    {"fault": index, "alert_labels": alert_labels, "snapshot": after}
                )
                before = after
        except Exception:
            self.recover_fault()
            raise

    def validate_fault_preflight(self):
        if len(self.preflight_evidence["steps"]) != self.total_faults:
            raise RuntimeError("Multi-app alert ladder preflight did not complete")
        if not self.ad._firing_alerts({"alertname": "HighRequestErrorRate", "namespace": "astronomy-shop",
                                       "method": "POST", "name": "/api/checkout"}):
            raise RuntimeError("Target checkout alert stopped firing before agent launch")
        self._check_agent_tool_connection()

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        errors = []
        for fault in reversed(self._active):
            try:
                fault.stop() if fault is self.ad else fault.recover_fault()
            except Exception as exc:
                errors.append(f"{type(fault).__name__}: {type(exc).__name__}: {exc}")
        self._active.clear()
        if errors:
            raise RuntimeError("Multi-app fault recovery failed: " + "; ".join(errors))
