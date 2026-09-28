from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.generators.noise.impl.clock_skew_geo import ClockSkewGeo


def _pod(name, *, ready=True):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, uid=f"uid-{name}"),
        spec=SimpleNamespace(containers=[SimpleNamespace(name="hotel-reserv-geo")]),
        status=SimpleNamespace(
            phase="Running", conditions=[SimpleNamespace(type="Ready", status="True" if ready else "False")]
        ),
    )


def test_geo_profile_targets_exact_existing_geo_application_container():
    kubectl = Mock()
    kubectl.apps_v1_api.read_namespaced_deployment.return_value = SimpleNamespace(
        spec=SimpleNamespace(selector=SimpleNamespace(match_labels={"io.kompose.service": "geo"}))
    )
    kubectl.core_v1_api.list_namespaced_pod.return_value.items = [_pod("geo-abc")]

    target = ClockSkewGeo(kubectl).select_target("hotel-reservation")

    assert target == {"name": "geo-abc", "uid": "uid-geo-abc", "namespace": "hotel-reservation"}
    assert ClockSkewGeo.time_chaos_spec(target, duration_seconds=3600) == {
        "mode": "one",
        "selector": {"pods": {"hotel-reservation": ["geo-abc"]}},
        "containerNames": ["hotel-reserv-geo"],
        "timeOffset": "+5m",
        "duration": "3600s",
    }


def test_geo_profile_refuses_multiple_ready_targets():
    kubectl = Mock()
    kubectl.apps_v1_api.read_namespaced_deployment.return_value = SimpleNamespace(
        spec=SimpleNamespace(selector=SimpleNamespace(match_labels={"io.kompose.service": "geo"}))
    )
    kubectl.core_v1_api.list_namespaced_pod.return_value.items = [_pod("geo-a"), _pod("geo-b")]

    with pytest.raises(RuntimeError, match="exactly one Ready Geo Pod"):
        ClockSkewGeo(kubectl).select_target("hotel-reservation")


def test_geo_profile_includes_geo_service_in_primary_endpoint_gate():
    kubectl = Mock()
    kubectl.core_v1_api.read_namespaced_endpoints.side_effect = lambda name, namespace: SimpleNamespace(
        subsets=[
            SimpleNamespace(addresses=[SimpleNamespace(ip=f"10.0.0.{dict(frontend=1, search=2, rate=3, geo=4)[name]}")])
        ]
    )

    endpoints = ClockSkewGeo(kubectl)._endpoints("hotel-reservation")

    assert endpoints == {
        "frontend": ("10.0.0.1",),
        "search": ("10.0.0.2",),
        "rate": ("10.0.0.3",),
        "geo": ("10.0.0.4",),
    }
