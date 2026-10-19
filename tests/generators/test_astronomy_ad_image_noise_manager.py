"""The opt-in ad image profile uses the existing noise lifecycle."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.generators.noise import manager
from sregym.generators.noise.impl.astronomy_ad_image import (
    ASTRONOMY_AD_IMAGE_PROFILE,
    AstronomyAdImageTreatment,
)


@pytest.fixture
def noise(monkeypatch):
    monkeypatch.setattr(manager, "KubeCtl", Mock())
    manager.NoiseManager._instance = None
    instance = manager.NoiseManager()
    yield instance
    manager.NoiseManager._instance = None


def test_ad_image_profile_starts_after_primary_fault_and_stops_on_cleanup(noise, monkeypatch):
    treatment = Mock()
    treatment.start.return_value = {"preflight": {"status": "passed"}}
    treatment.stop.return_value = {"cleanup": {"status": "confirmed"}}
    factory = Mock(return_value=treatment)
    monkeypatch.setattr(manager, "AstronomyAdImageTreatment", factory)
    problem = SimpleNamespace()
    noise.set_problem_context({
        "problem_id": "incorrect_port_assignment",
        "namespace": "astronomy-shop",
        "noise_profile": ASTRONOMY_AD_IMAGE_PROFILE,
        "problem": problem,
    })

    noise.start()
    assert noise.running
    assert noise.evidence_snapshot()["preflight"]["status"] == "passed"
    factory.assert_called_once_with(noise.kubectl, "astronomy-shop", problem)

    noise.stop()
    treatment.stop.assert_called_once()
    assert not noise.running
    assert noise.evidence_snapshot()["cleanup"]["status"] == "confirmed"


def test_ad_image_profile_rejects_other_problems(noise):
    noise.set_problem_context({
        "problem_id": "incorrect_image",
        "namespace": "astronomy-shop",
        "noise_profile": ASTRONOMY_AD_IMAGE_PROFILE,
        "problem": SimpleNamespace(),
    })

    with pytest.raises(RuntimeError, match="requires incorrect_port_assignment"):
        noise.start()


def test_non_interference_gate_resamples_a_transient_low_traffic_window(monkeypatch):
    before = {
        "primary_address": "product-catalog:8082",
        "checkout_alert_firing": True,
        "service_endpoints": {"checkout": [("10.0.0.1", 8080)]},
        "ad_server_rps": 0.1,
        "endpoint_rates": {
            name: {"request_rps": 0.05, "failure_ratio": failure_ratio}
            for name, failure_ratio in (
                ("POST /api/checkout", 1.0),
                ("GET /api/cart", 0.0),
                ("POST /api/cart", 0.0),
            )
        },
    }
    low_traffic = {
        **before,
        "endpoint_rates": {
            **before["endpoint_rates"],
            "POST /api/checkout": {"request_rps": 0.0095, "failure_ratio": 1.0},
        },
    }
    treatment = object.__new__(AstronomyAdImageTreatment)
    treatment._snapshot = Mock(side_effect=[low_traffic, before, before])
    treatment._oracle_still_fails = Mock(return_value=True)
    monkeypatch.setattr("sregym.generators.noise.impl.astronomy_ad_image.time.sleep", lambda _: None)

    assert treatment._wait_for_unchanged_behavior(before) == before
    assert treatment._snapshot.call_count == 3
