from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.generators.noise.impl.clock_skew_recommendation import (
    ClockSkewRecommendation,
    TreatmentBaseline,
    TreatmentEffect,
)


def _pod(name, *, ready=True, container="hotel-reserv-recommendation"):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, uid=f"uid-{name}"),
        spec=SimpleNamespace(containers=[SimpleNamespace(name=container)]),
        status=SimpleNamespace(
            phase="Running", conditions=[SimpleNamespace(type="Ready", status="True" if ready else "False")]
        ),
    )


def test_time_chaos_targets_one_existing_recommendation_pod_and_container():
    kubectl = Mock()
    kubectl.apps_v1_api.read_namespaced_deployment.return_value = SimpleNamespace(
        spec=SimpleNamespace(selector=SimpleNamespace(match_labels={"io.kompose.service": "recommendation"}))
    )
    kubectl.core_v1_api.list_namespaced_pod.return_value.items = [_pod("recommendation-abc")]
    target = ClockSkewRecommendation(kubectl).select_target("hotel-reservation")

    assert target == {"name": "recommendation-abc", "uid": "uid-recommendation-abc", "namespace": "hotel-reservation"}
    assert ClockSkewRecommendation.time_chaos_spec(target, duration_seconds=3600) == {
        "mode": "one",
        "selector": {"pods": {"hotel-reservation": ["recommendation-abc"]}},
        "containerNames": ["hotel-reserv-recommendation"],
        "timeOffset": "+5m",
        "duration": "3600s",
    }


def test_time_chaos_refuses_ambiguous_or_unready_recommendation_targets():
    kubectl = Mock()
    kubectl.apps_v1_api.read_namespaced_deployment.return_value = SimpleNamespace(
        spec=SimpleNamespace(selector=SimpleNamespace(match_labels={"io.kompose.service": "recommendation"}))
    )
    kubectl.core_v1_api.list_namespaced_pod.return_value.items = [_pod("recommendation-a"), _pod("recommendation-b")]

    with pytest.raises(RuntimeError, match="exactly one Ready"):
        ClockSkewRecommendation(kubectl).select_target("hotel-reservation")


def test_preflight_requires_healthy_recommendations_and_active_primary_fault(monkeypatch):
    workload = SimpleNamespace(
        metrics=SimpleNamespace(snapshot=Mock(return_value={"rate_queue_depth": 256})),
        snapshot=Mock(return_value=SimpleNamespace(success_rate=0.2)),
    )
    treatment = ClockSkewRecommendation(Mock(), workload)
    monkeypatch.setattr(treatment, "_recommendation_response", lambda _namespace: SimpleNamespace(status_code=200))
    monkeypatch.setattr(treatment, "_endpoints", lambda _namespace: {"search": ("10.0.0.1",)})

    baseline = treatment.capture_baseline({"namespace": "hotel-reservation"})

    assert baseline == TreatmentBaseline({"search": ("10.0.0.1",)}, 256, 0.2)
    monkeypatch.setattr(treatment, "_recommendation_response", lambda _namespace: SimpleNamespace(status_code=502))
    with pytest.raises(RuntimeError, match="not healthy before"):
        treatment.capture_baseline({"namespace": "hotel-reservation"})


def test_preflight_accepts_real_recommendation_failure_without_changing_primary_fault(monkeypatch):
    kubectl = Mock()
    target = {"namespace": "hotel-reservation", "name": "recommendation-abc", "uid": "uid-recommendation-abc"}
    kubectl.core_v1_api.read_namespaced_pod.return_value = _pod(target["name"])
    workload = SimpleNamespace(
        metrics=SimpleNamespace(snapshot=Mock(return_value={"rate_queue_depth": 256})),
        snapshot=Mock(return_value=SimpleNamespace(success_rate=0.2)),
    )
    treatment = ClockSkewRecommendation(kubectl, workload)
    endpoints = {"frontend": ("10.0.0.1",), "search": ("10.0.0.2",), "rate": ("10.0.0.3",)}
    monkeypatch.setattr(treatment, "_endpoints", lambda _namespace: endpoints)
    monkeypatch.setattr(treatment, "_optional_failure_logged", lambda _namespace: True)
    monkeypatch.setattr(
        treatment,
        "_recommendation_response",
        lambda _namespace: SimpleNamespace(
            status_code=502, text="recommendation result timestamp is ahead of frontend clock"
        ),
    )

    effect = treatment.wait_for_treatment_effect(target, TreatmentBaseline(endpoints, 256, 0.2))

    assert effect == TreatmentEffect(502, 256, 0.2)
    workload.metrics.snapshot.assert_called_once_with()
    workload.snapshot.assert_called_once_with(10)


def test_preflight_rejects_primary_path_endpoint_change(monkeypatch):
    kubectl = Mock()
    target = {"namespace": "hotel-reservation", "name": "recommendation-abc", "uid": "uid-recommendation-abc"}
    kubectl.core_v1_api.read_namespaced_pod.return_value = _pod(target["name"])
    workload = SimpleNamespace(
        metrics=SimpleNamespace(snapshot=Mock(return_value={"rate_queue_depth": 256})),
        snapshot=Mock(return_value=SimpleNamespace(success_rate=0.2)),
    )
    treatment = ClockSkewRecommendation(kubectl, workload)
    monkeypatch.setattr(treatment, "_optional_failure_logged", lambda _namespace: True)
    monkeypatch.setattr(
        treatment,
        "_recommendation_response",
        lambda _namespace: SimpleNamespace(status_code=502, text="ahead of frontend clock"),
    )
    monkeypatch.setattr(treatment, "_endpoints", lambda _namespace: {"search": ("10.0.0.9",)})

    with pytest.raises(RuntimeError, match="changed primary-path Service endpoints"):
        treatment.wait_for_treatment_effect(target, TreatmentBaseline({"search": ("10.0.0.1",)}, 256, 0.2))


def test_cleanup_waits_for_recommendations_to_be_healthy_again(monkeypatch):
    kubectl = Mock()
    target = {"namespace": "hotel-reservation", "name": "recommendation-abc", "uid": "uid-recommendation-abc"}
    kubectl.core_v1_api.read_namespaced_pod.return_value = _pod(target["name"])
    treatment = ClockSkewRecommendation(kubectl)
    monkeypatch.setattr(treatment, "_recommendation_response", lambda _namespace: SimpleNamespace(status_code=200))

    treatment.wait_for_recovery(target)

    kubectl.core_v1_api.read_namespaced_pod.assert_called_once_with(
        name="recommendation-abc", namespace="hotel-reservation"
    )
