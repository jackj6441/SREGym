"""Safety and protocol tests for the physical CPU-frequency profile."""

import io
from types import SimpleNamespace as Obj
from unittest.mock import Mock

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from kubernetes.client.exceptions import ApiException

from sregym.generators.noise.impl.cpu_frequency import (
    BareMetalCpuFreq,
    CpuFrequencyTreatment,
    WorkerIdentity,
    candidate_caps,
)
from sregym.generators.noise.impl.recommendation_rpc import _request_payload
from sregym.generators.workload.wrk2 import Wrk2


def _pod(name, node="worker-a", namespace="hotel-reservation", owner_references=None):
    return Obj(
        metadata=Obj(name=name, uid="pod-uid", namespace=namespace, owner_references=owner_references),
        spec=Obj(node_name=node),
        status=Obj(
            phase="Running",
            conditions=[Obj(type="Ready", status="True")],
        ),
    )


def _kube_with_pods(colocated):
    kube = Mock()
    kube.apps_v1_api.read_namespaced_deployment.return_value = Obj(
        spec=Obj(selector=Obj(match_labels={"io.kompose.service": "recommendation"}))
    )
    kube.core_v1_api.list_namespaced_pod.return_value = Obj(items=[_pod("recommendation-1")])
    kube.core_v1_api.read_node.return_value = Obj(spec=Obj(provider_id=""), metadata=Obj(labels={}))
    kube.core_v1_api.list_pod_for_all_namespaces.return_value = Obj(items=colocated)
    return kube


def test_refuses_to_cap_worker_with_protected_pod():
    kube = _kube_with_pods([_pod("recommendation-1"), _pod("rate-1")])
    with pytest.raises(RuntimeError, match="not isolated"):
        BareMetalCpuFreq.identify(kube, "hotel-reservation")


def test_allows_only_known_node_daemonsets_on_recommendation_worker(monkeypatch):
    colocated = [
        _pod("recommendation-1"),
        _pod("calico-node-abc", namespace="kube-system", owner_references=[Obj(kind="DaemonSet", name="calico-node")]),
        _pod("openebs-ndm-abc", namespace="openebs", owner_references=[Obj(kind="DaemonSet", name="openebs-ndm")]),
    ]
    kube = _kube_with_pods(colocated)
    inventory = "all:\n  children:\n    worker_nodes:\n      hosts:\n        worker-a:\n          ansible_host: host-a\n          ansible_user: user\n"
    monkeypatch.setattr(
        "sregym.generators.noise.impl.cpu_frequency.Path.open", lambda *_args, **_kwargs: io.StringIO(inventory)
    )
    identity = BareMetalCpuFreq.identify(kube, "hotel-reservation")
    assert identity.node_name == "worker-a"


def test_refuses_unknown_daemonset_even_when_it_is_a_node_daemon():
    kube = _kube_with_pods(
        [
            _pod("recommendation-1"),
            _pod(
                "storage-worker", namespace="storage", owner_references=[Obj(kind="DaemonSet", name="storage-worker")]
            ),
        ]
    )
    with pytest.raises(RuntimeError, match="storage/storage-worker"):
        BareMetalCpuFreq.identify(kube, "hotel-reservation")


def test_refuses_kind_node_even_if_recommendation_is_ready():
    kube = _kube_with_pods([_pod("recommendation-1")])
    kube.core_v1_api.read_node.return_value.spec.provider_id = "kind://docker/kind-worker"
    with pytest.raises(RuntimeError, match="not Kind"):
        BareMetalCpuFreq.identify(kube, "hotel-reservation")


def test_refuses_control_plane_node_even_on_bare_metal():
    kube = _kube_with_pods([_pod("recommendation-1")])
    kube.core_v1_api.read_node.return_value.metadata.labels = {"node-role.kubernetes.io/control-plane": ""}
    with pytest.raises(RuntimeError, match="control-plane"):
        BareMetalCpuFreq.identify(kube, "hotel-reservation")


def test_candidate_caps_are_bounded_and_progressively_lower():
    original = {
        "policy0": {
            "min": 1000,
            "max": 3000,
            "hardware_min": 800,
            "available": "1000 1500 2000 2500 3000",
        },
        "policy1": {
            "min": 1200,
            "max": 3200,
            "hardware_min": 1200,
            "available": None,
        },
    }
    caps = candidate_caps(original)
    assert caps
    assert caps[-1] == {"policy0": 1000, "policy1": 1200}
    assert all(1000 <= cap["policy0"] < 3000 for cap in caps)
    assert all(1200 <= cap["policy1"] < 3200 for cap in caps)


def test_restore_replays_every_saved_policy_and_checks_governors():
    host = BareMetalCpuFreq(WorkerIdentity("worker-a", "host-a", "user", "recommendation-1", "uid"))
    host.original = {
        "policy0": {"max": 3000, "min": 1000, "governor": "performance"},
        "policy1": {"max": 3200, "min": 1200, "governor": "schedutil"},
    }
    host._policy = Mock(return_value=host.original)
    host.restore()
    host._policy.assert_called_once_with("restore", host.original)


def test_restore_rejects_governor_drift():
    host = BareMetalCpuFreq(WorkerIdentity("worker-a", "host-a", "user", "recommendation-1", "uid"))
    host.original = {"policy0": {"max": 3000, "min": 1000, "governor": "performance"}}
    host._policy = Mock(return_value={"policy0": {"max": 3000, "min": 1000, "governor": "powersave"}})
    with pytest.raises(RuntimeError, match="original state"):
        host.restore()


def test_direct_rpc_request_encodes_all_required_fields():
    payload = _request_payload("dis")
    descriptor = descriptor_pb2.FileDescriptorProto(name="recommendation_test.proto", package="recommendation")
    request = descriptor.message_type.add(name="Request")
    for number, name, kind in (
        (1, "require", descriptor_pb2.FieldDescriptorProto.TYPE_STRING),
        (2, "lat", descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE),
        (3, "lon", descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE),
    ):
        field = request.field.add(name=name, number=number, type=kind)
        field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    parsed_type = message_factory.GetMessageClass(
        descriptor_pool.DescriptorPool().Add(descriptor).message_types_by_name["Request"]
    )
    parsed = parsed_type.FromString(payload)
    assert (parsed.require, parsed.lat, parsed.lon) == ("dis", 38.0235, -122.095)


def test_profile_rejects_other_primary_problem():
    with pytest.raises(RuntimeError, match="limited"):
        CpuFrequencyTreatment(Mock(), "hotel-reservation", "cfs_cpu_throttling_hotel_reservation")


def test_control_cleanup_does_not_write_cpu_frequency():
    treatment = CpuFrequencyTreatment(
        Mock(), "hotel-reservation", "wrong_service_selector_hotel_reservation", control=True
    )
    host = Mock()
    host.read.return_value = {"policy0": {"max": 3000}}
    treatment.host = host
    treatment.stop()
    host.restore.assert_not_called()


def test_treatment_cleanup_restores_cpu_frequency():
    treatment = CpuFrequencyTreatment(Mock(), "hotel-reservation", "wrong_service_selector_hotel_reservation")
    host = Mock()
    host.read.return_value = {"policy0": {"max": 3000}}
    treatment.host = host
    treatment.stop()
    host.restore.assert_called_once_with()


def test_temporary_selector_repair_restores_fault_when_probe_cleanup_fails(monkeypatch):
    treatment = CpuFrequencyTreatment(Mock(), "hotel-reservation", "wrong_service_selector_hotel_reservation")
    forward = Mock()
    forward.stop.side_effect = RuntimeError("forward cleanup failed")
    monkeypatch.setattr("sregym.generators.noise.impl.cpu_frequency.KubectlPortForward", lambda *_: forward)
    treatment._wait_endpoints = Mock(side_effect=[RuntimeError("probe failed"), ()])
    faulty = {"io.kompose.service": "frontend", "current_service_name": "frontend"}

    with pytest.raises(RuntimeError, match="forward cleanup failed"):
        treatment._hotel_success_while_repaired(faulty)

    selectors = [
        call.args[2][0]["value"] for call in treatment.kubectl.core_v1_api.patch_namespaced_service.call_args_list
    ]
    assert selectors == [{"io.kompose.service": "frontend"}, faulty]


def test_wrk2_job_is_excluded_from_dedicated_recommendation_worker(monkeypatch):
    batch = Mock()
    batch.read_namespaced_job.side_effect = ApiException(status=404)
    batch.create_namespaced_job.return_value = Obj(metadata=Obj(name="wrk2-job"))
    monkeypatch.setattr("sregym.generators.workload.wrk2.client.BatchV1Api", lambda: batch)
    wrk = Wrk2.__new__(Wrk2)
    wrk.namespace = "hotel-reservation"
    wrk.excluded_node = "worker-rec"

    wrk.create_wrk_job("wrk2-job", "hotel-reservation", "mixed-workload_type_1.lua")

    job = batch.create_namespaced_job.call_args.kwargs["body"]
    requirement = job["spec"]["template"]["spec"]["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]["matchFields"][0]
    assert requirement == {"key": "metadata.name", "operator": "NotIn", "values": ["worker-rec"]}
