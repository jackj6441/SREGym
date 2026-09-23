from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes.client.rest import ApiException

from sregym.generators.images import CLOCK_SKEW_OBSERVER_IMAGE
from sregym.generators.noise.impl.clock_skew import DEFAULT_DURATION_SECONDS, ClockSkewObserver


def test_default_duration_covers_a_two_stage_agent_attempt():
    assert DEFAULT_DURATION_SECONDS == 3600


def _pod(name="frontend-abc", *, node="kind-worker2", phase="Running", ready=True):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(node_name=node),
        status=SimpleNamespace(
            phase=phase,
            conditions=[SimpleNamespace(type="Ready", status="True" if ready else "False")],
        ),
    )


def _observer(pods):
    core = Mock()
    core.list_namespaced_pod.return_value.items = pods
    core.read_node.return_value.metadata.labels = {}
    core.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(
            phase="Running",
            conditions=[SimpleNamespace(type="Ready", status="True")],
            container_statuses=[
                SimpleNamespace(name="observer", ready=True, state=SimpleNamespace(running=SimpleNamespace())),
                SimpleNamespace(name="reference", ready=True, state=SimpleNamespace(running=SimpleNamespace())),
            ],
        )
    )
    apps = Mock()
    apps.read_namespaced_deployment.return_value.spec.selector.match_labels = {"io.kompose.service": "frontend"}
    kubectl = SimpleNamespace(core_v1_api=core, apps_v1_api=apps)
    return ClockSkewObserver(kubectl), core, apps


def test_observer_is_colocated_with_the_ready_target_pod_and_has_an_exact_selector_label():
    observer, core, apps = _observer([_pod(node="kind-worker2")])

    resource = observer.inject(namespace="hotel-reservation", target_deployment="frontend")

    apps.read_namespaced_deployment.assert_called_once_with(name="frontend", namespace="hotel-reservation")
    core.list_namespaced_pod.assert_called_once_with(
        namespace="hotel-reservation", label_selector="io.kompose.service=frontend"
    )
    body = core.create_namespaced_pod.call_args.kwargs["body"]
    assert body["metadata"]["name"].startswith("analytics-clock-observer-")
    assert body["spec"]["nodeSelector"] == {"kubernetes.io/hostname": "kind-worker2"}
    assert body["spec"]["restartPolicy"] == "Never"
    assert body["spec"]["automountServiceAccountToken"] is False
    assert body["metadata"]["labels"]["sregym.io/noise-profile"] == "clock-skew"
    assert body["metadata"]["labels"]["sregym.io/noise-run"] == resource["selector_value"]
    containers = {container["name"]: container for container in body["spec"]["containers"]}
    assert set(containers) == {"observer", "reference"}
    assert body["spec"]["volumes"] == [{"name": "clock-reference", "emptyDir": {}}]
    assert containers["reference"]["volumeMounts"] == [{"name": "clock-reference", "mountPath": "/clock-reference"}]
    assert containers["observer"]["volumeMounts"] == [{"name": "clock-reference", "mountPath": "/clock-reference"}]
    assert containers["reference"]["image"] == CLOCK_SKEW_OBSERVER_IMAGE
    assert containers["observer"]["image"] == CLOCK_SKEW_OBSERVER_IMAGE
    assert containers["reference"]["command"] == ["/usr/local/bin/clock-skew-observer", "reference"]
    assert containers["observer"]["command"] == ["/usr/local/bin/clock-skew-observer", "observe"]
    assert containers["observer"]["readinessProbe"] == {
        "exec": {
            "command": ["/usr/local/bin/clock-skew-observer", "healthcheck", "/clock-reference/clock-skew-active"]
        },
        "initialDelaySeconds": 1,
        "periodSeconds": 2,
    }
    assert resource["node"] == "kind-worker2"
    assert resource["namespace"] == "hotel-reservation"


def test_observer_falls_back_to_ready_control_plane_target_on_single_node_cluster():
    observer, core, _ = _observer([_pod(node="single-control-plane")])
    core.read_node.return_value.metadata.labels = {"node-role.kubernetes.io/control-plane": ""}

    resource = observer.inject(namespace="hotel-reservation", target_deployment="frontend")

    assert resource["node"] == "single-control-plane"
    body = core.create_namespaced_pod.call_args.kwargs["body"]
    assert body["spec"]["nodeSelector"] == {"kubernetes.io/hostname": "single-control-plane"}


def test_observer_prefers_ready_worker_even_when_control_plane_pod_sorts_first():
    observer, core, _ = _observer(
        [
            _pod(name="a-frontend-control-plane", node="control-plane"),
            _pod(name="z-frontend-worker", node="worker"),
        ]
    )

    def read_node(*, name):
        labels = {"node-role.kubernetes.io/control-plane": ""} if name == "control-plane" else {}
        return SimpleNamespace(metadata=SimpleNamespace(labels=labels))

    core.read_node.side_effect = read_node

    resource = observer.inject(namespace="hotel-reservation", target_deployment="frontend")

    assert resource["node"] == "worker"
    body = core.create_namespaced_pod.call_args.kwargs["body"]
    assert body["spec"]["nodeSelector"] == {"kubernetes.io/hostname": "worker"}


def test_observer_rejects_deployment_without_a_ready_target_pod():
    observer, _, _ = _observer([_pod(phase="Pending", ready=False)])

    with pytest.raises(RuntimeError, match="No Ready pod found for Deployment hotel-reservation/frontend"):
        observer.inject(namespace="hotel-reservation", target_deployment="frontend")


def test_time_chaos_spec_selects_only_the_owned_observer():
    resource = {
        "name": "analytics-clock-observer-abc123",
        "namespace": "hotel-reservation",
        "node": "kind-worker2",
        "selector_value": "abc123",
    }

    spec = ClockSkewObserver.time_chaos_spec(resource, duration_seconds=120)

    assert spec == {
        "mode": "one",
        "selector": {
            "namespaces": ["hotel-reservation"],
            "labelSelectors": {"sregym.io/noise-run": "abc123"},
        },
        "containerNames": ["observer"],
        "timeOffset": "+5m",
        "duration": "120s",
    }


def test_observer_accepts_only_a_real_clock_skew_fault_signal():
    observer, core, _ = _observer([_pod()])
    resource = {
        "name": "analytics-clock-observer-abc123",
        "namespace": "hotel-reservation",
        "node": "kind-worker2",
        "selector_value": "abc123",
    }
    core.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(
            phase="Running",
            conditions=[SimpleNamespace(type="Ready", status="False")],
            container_statuses=[
                SimpleNamespace(name="observer", ready=False, state=SimpleNamespace(running=SimpleNamespace())),
                SimpleNamespace(name="reference", ready=True, state=SimpleNamespace(running=SimpleNamespace())),
            ],
        )
    )
    core.read_namespaced_pod_log.return_value = "CLOCK_SKEW_FAULT offset_seconds=300\n"

    observer.wait_for_treatment_effect(resource)

    core.read_namespaced_pod_log.assert_called_with(
        name=resource["name"], namespace=resource["namespace"], container="observer", tail_lines=20
    )


def test_observer_rejects_an_unready_pod_without_clock_skew_evidence():
    observer, core, _ = _observer([_pod()])
    observer.treatment_effect_timeout_seconds = 0
    resource = {
        "name": "analytics-clock-observer-abc123",
        "namespace": "hotel-reservation",
        "node": "kind-worker2",
        "selector_value": "abc123",
    }
    core.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(
            phase="Running",
            conditions=[SimpleNamespace(type="Ready", status="False")],
            container_statuses=[
                SimpleNamespace(name="observer", ready=False, state=SimpleNamespace(running=SimpleNamespace())),
                SimpleNamespace(name="reference", ready=True, state=SimpleNamespace(running=SimpleNamespace())),
            ],
        )
    )
    core.read_namespaced_pod_log.return_value = "observer still starting\n"

    with pytest.raises(TimeoutError, match="did not become observable"):
        observer.wait_for_treatment_effect(resource)


@pytest.mark.parametrize(
    "logs",
    [
        "CLOCK_SKEW_FAULT offset_seconds=-300\n",
        "CLOCK_SKEW_FAULT offset_seconds=241\n",
        "CLOCK_SKEW_FAULT offset_seconds=600\n",
    ],
)
def test_observer_rejects_a_fault_log_that_is_not_the_configured_positive_five_minute_offset(logs):
    observer, core, _ = _observer([_pod()])
    observer.treatment_effect_timeout_seconds = 0
    resource = {
        "name": "analytics-clock-observer-abc123",
        "namespace": "hotel-reservation",
        "node": "kind-worker2",
        "selector_value": "abc123",
    }
    core.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(
            phase="Running",
            conditions=[SimpleNamespace(type="Ready", status="False")],
            container_statuses=[
                SimpleNamespace(name="observer", ready=False, state=SimpleNamespace(running=SimpleNamespace())),
                SimpleNamespace(name="reference", ready=True, state=SimpleNamespace(running=SimpleNamespace())),
            ],
        )
    )
    core.read_namespaced_pod_log.return_value = logs

    with pytest.raises(TimeoutError, match="did not become observable"):
        observer.wait_for_treatment_effect(resource)


def test_observer_retries_a_transient_kubernetes_read_failure_before_accepting_the_fault(monkeypatch):
    observer, core, _ = _observer([_pod()])
    resource = {
        "name": "analytics-clock-observer-abc123",
        "namespace": "hotel-reservation",
        "node": "kind-worker2",
        "selector_value": "abc123",
    }
    faulted_pod = SimpleNamespace(
        status=SimpleNamespace(
            phase="Running",
            conditions=[SimpleNamespace(type="Ready", status="False")],
            container_statuses=[
                SimpleNamespace(name="observer", ready=False, state=SimpleNamespace(running=SimpleNamespace())),
                SimpleNamespace(name="reference", ready=True, state=SimpleNamespace(running=SimpleNamespace())),
            ],
        )
    )
    core.read_namespaced_pod.side_effect = [ApiException(status=429, reason="Too Many Requests"), faulted_pod]
    core.read_namespaced_pod_log.return_value = "CLOCK_SKEW_FAULT offset_seconds=300\n"
    monkeypatch.setattr("sregym.generators.noise.impl.clock_skew.time.sleep", lambda _: None)

    observer.wait_for_treatment_effect(resource)

    assert core.read_namespaced_pod.call_count == 2


def test_observer_fails_immediately_when_the_treatment_pod_disappears():
    observer, core, _ = _observer([_pod()])
    resource = {
        "name": "analytics-clock-observer-abc123",
        "namespace": "hotel-reservation",
        "node": "kind-worker2",
        "selector_value": "abc123",
    }
    core.read_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")

    with pytest.raises(RuntimeError, match="disappeared"):
        observer.wait_for_treatment_effect(resource)
