from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes.client.rest import ApiException

from sregym.generators.noise.impl.cpu_noisy_neighbor import CpuNoisyNeighbor


def _pod(name="frontend-abc", *, node="kind-worker2", phase="Running", ready=True):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(node_name=node),
        status=SimpleNamespace(
            phase=phase,
            conditions=[SimpleNamespace(type="Ready", status="True" if ready else "False")],
        ),
    )


def _injector(pods):
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
    return CpuNoisyNeighbor(kubectl), core, apps


def test_injection_colocates_a_bounded_neighbor_with_the_ready_target_pod():
    injector, core, apps = _injector([_pod(node="kind-worker2")])

    resource = injector.inject(
        namespace="hotel-reservation",
        target_deployment="frontend",
        cpu_workers=2,
        duration_seconds=120,
    )

    apps.read_namespaced_deployment.assert_called_once_with(name="frontend", namespace="hotel-reservation")
    core.list_namespaced_pod.assert_called_once_with(
        namespace="hotel-reservation", label_selector="io.kompose.service=frontend"
    )
    core.read_node.assert_called_once_with(name="kind-worker2")
    body = core.create_namespaced_pod.call_args.kwargs["body"]
    assert body["metadata"]["name"].startswith("analytics-backfill-")
    assert "noise" not in body["metadata"]["name"]
    assert body["metadata"]["labels"] == {
        "app.kubernetes.io/name": "analytics-backfill",
        "app.kubernetes.io/component": "batch",
    }
    assert body["spec"]["nodeSelector"] == {"kubernetes.io/hostname": "kind-worker2"}
    assert body["spec"]["restartPolicy"] == "Never"
    assert body["spec"]["automountServiceAccountToken"] is False
    container = body["spec"]["containers"][0]
    assert container["args"] == ["exec stress --cpu 2 --timeout 120s"]
    assert "cpu" not in container["resources"]["limits"]
    assert resource == {
        "kind": "pod",
        "name": body["metadata"]["name"],
        "namespace": "hotel-reservation",
        "node": "kind-worker2",
    }


def test_target_selection_ignores_unready_and_unscheduled_pods():
    injector, _, _ = _injector(
        [
            _pod(name="frontend-a", node="kind-worker", ready=False),
            _pod(name="frontend-b", node=None),
        ]
    )

    with pytest.raises(RuntimeError, match="No Ready worker pod"):
        injector.inject(namespace="hotel-reservation", target_deployment="frontend")


def test_target_selection_rejects_control_plane_nodes():
    injector, core, _ = _injector([_pod(node="kind-control-plane")])
    core.read_node.return_value.metadata.labels = {"node-role.kubernetes.io/control-plane": ""}

    with pytest.raises(RuntimeError, match="No Ready worker pod"):
        injector.inject(namespace="hotel-reservation", target_deployment="frontend")

    core.create_namespaced_pod.assert_not_called()


@pytest.mark.parametrize(("cpu_workers", "duration"), [(0, 120), (2, 0)])
def test_injection_rejects_non_positive_work(cpu_workers, duration):
    injector, core, _ = _injector([_pod()])

    with pytest.raises(ValueError):
        injector.inject(
            namespace="hotel-reservation",
            target_deployment="frontend",
            cpu_workers=cpu_workers,
            duration_seconds=duration,
        )

    core.create_namespaced_pod.assert_not_called()


def test_failed_start_deletes_the_partially_created_neighbor():
    injector, core, _ = _injector([_pod()])
    injector.readiness_timeout_seconds = 0
    core.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(phase="Pending", container_statuses=[])
    )

    with pytest.raises(TimeoutError, match="did not start"):
        injector.inject(namespace="hotel-reservation", target_deployment="frontend")

    name = core.create_namespaced_pod.call_args.kwargs["body"]["metadata"]["name"]
    core.delete_namespaced_pod.assert_called_once_with(
        name=name,
        namespace="hotel-reservation",
        grace_period_seconds=0,
    )


def test_running_pod_is_not_active_until_the_worker_container_starts():
    injector, core, _ = _injector([_pod()])
    injector.readiness_timeout_seconds = 0
    core.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[SimpleNamespace(state=SimpleNamespace(running=None))],
        )
    )

    with pytest.raises(TimeoutError, match="did not start"):
        injector.inject(namespace="hotel-reservation", target_deployment="frontend")


def test_delete_is_idempotent_when_the_neighbor_is_already_gone():
    injector, core, _ = _injector([_pod()])
    core.delete_namespaced_pod.side_effect = ApiException(status=404)

    injector.delete(
        {
            "kind": "pod",
            "name": "analytics-backfill-old",
            "namespace": "hotel-reservation",
            "node": "kind-worker",
        }
    )

    core.read_namespaced_pod.assert_not_called()


def test_delete_waits_until_the_exact_neighbor_is_gone():
    injector, core, _ = _injector([_pod()])
    core.read_namespaced_pod.side_effect = [
        SimpleNamespace(status=SimpleNamespace(phase="Running")),
        ApiException(status=404),
    ]
    injector.poll_interval_seconds = 0

    injector.delete(
        {
            "kind": "pod",
            "name": "analytics-backfill-owned",
            "namespace": "hotel-reservation",
            "node": "kind-worker",
        }
    )

    core.delete_namespaced_pod.assert_called_once_with(
        name="analytics-backfill-owned",
        namespace="hotel-reservation",
        grace_period_seconds=0,
    )
    assert core.read_namespaced_pod.call_count == 2
