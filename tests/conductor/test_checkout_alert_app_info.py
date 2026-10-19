"""Expose the assigned alert without exposing the injected fault."""

import asyncio
from types import SimpleNamespace

from sregym.conductor import conductor_api


def test_checkout_problem_exposes_only_target_alert_metadata(monkeypatch):
    app = SimpleNamespace(
        app_name="OpenTelemetry Demo Astronomy Shop",
        namespace="astronomy-shop",
        namespaces=["astronomy-shop"],
        description="A sample ecommerce application",
    )
    monkeypatch.setattr(conductor_api, "_conductor", SimpleNamespace(app=app, problem_id="incorrect_port_assignment"))

    result = asyncio.run(conductor_api.get_app())

    assert result["target_alert"] == {
        "alertname": "HighRequestErrorRate", "method": "POST", "name": "/api/checkout",
    }
    assert "problem_id" not in result
    assert "8082" not in str(result)


def test_other_problems_do_not_get_checkout_target(monkeypatch):
    app = SimpleNamespace(app_name="Other", namespace="astronomy-shop", namespaces=None, description="Other")
    monkeypatch.setattr(conductor_api, "_conductor", SimpleNamespace(app=app, problem_id="incorrect_image"))

    assert "target_alert" not in asyncio.run(conductor_api.get_app())


def test_multiapp_ladder_assigns_only_the_astronomy_checkout_alert(monkeypatch):
    app = SimpleNamespace(
        app_name="CompositeApp",
        namespace="astronomy-shop",
        namespaces=["astronomy-shop", "hotel-reservation", "social-network", "fleetcast",
                    "blueprint-hotel-reservation"],
        description="Five applications with hidden injected faults",
    )
    primary = SimpleNamespace(app=SimpleNamespace(description="Astronomy Shop application"))
    monkeypatch.setattr(
        conductor_api, "_conductor",
        SimpleNamespace(app=app, primary=primary, problem=SimpleNamespace(primary=primary),
                        problem_id="multiapp_checkout_alert_5"),
    )

    result = asyncio.run(conductor_api.get_app())

    assert result["app_name"] == "Astronomy Shop"
    assert result["namespace"] == "astronomy-shop"
    assert result["target_alert"] == {
        "alertname": "HighRequestErrorRate", "method": "POST", "name": "/api/checkout",
    }
    assert len(result["namespaces"]) == 5
    assert "fault" not in str(result).lower()
