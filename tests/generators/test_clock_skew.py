from types import SimpleNamespace
from unittest.mock import Mock

import pytest

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
            container_statuses=[SimpleNamespace(state=SimpleNamespace(running=SimpleNamespace()))],
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
    container = body["spec"]["containers"][0]
    assert container["name"] == "observer"
    assert container["args"] == ["while true; do date -u '+%Y-%m-%dT%H:%M:%SZ'; sleep 5; done"]
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
