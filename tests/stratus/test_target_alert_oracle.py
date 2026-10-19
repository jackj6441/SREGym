"""The checkout weak oracle must ignore independently firing ad alerts."""

import json
from types import SimpleNamespace

import pytest

from clients.stratus.weak_oracles import alert_oracle


TARGET = {"alertname": "HighRequestErrorRate", "method": "POST", "name": "/api/checkout"}


def test_checkout_filter_ignores_noise_and_other_error_rate_instances(monkeypatch):
    payload = {
        "status": "success",
        "data": {
            "alerts": [
                {"state": "firing", "labels": {"namespace": "astronomy-shop", "alertname": "PodStatusError", "pod": "ad-new"}},
                {"state": "firing", "labels": {"namespace": "astronomy-shop", "alertname": "HighRequestErrorRate", "service_name": "checkout"}},
                {"state": "firing", "labels": {"namespace": "astronomy-shop", **TARGET}},
            ]
        },
    }
    monkeypatch.setattr(
        alert_oracle.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )
    oracle = alert_oracle.AlertOracle("astronomy-shop", match_labels=TARGET)

    assert [item["labels"] for item in oracle._query_firing_alerts()] == [{"namespace": "astronomy-shop", **TARGET}]
    payload["data"]["alerts"].pop()
    assert oracle._query_firing_alerts() == []


def test_checkout_filter_does_not_pass_when_prometheus_query_fails(monkeypatch):
    monkeypatch.setattr(
        alert_oracle.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="connection refused"),
    )
    oracle = alert_oracle.AlertOracle("astronomy-shop", match_labels=TARGET)

    with pytest.raises(RuntimeError, match="Prometheus alert query failed"):
        oracle._query_firing_alerts()


def test_agent_kickoff_waits_for_exact_alert_to_fire(monkeypatch):
    oracle = alert_oracle.AlertOracle("astronomy-shop", match_labels=TARGET, poll_interval_seconds=0)
    states = iter([[], [{"labels": TARGET, "state": "firing"}]])
    monkeypatch.setattr(oracle, "_query_firing_alerts", lambda: next(states))

    oracle.wait_for_firing(timeout_seconds=1)


def test_checkout_validation_allows_alert_rate_window_to_clear(monkeypatch):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(alert_oracle.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(alert_oracle.time, "sleep", lambda seconds: setattr(clock, "now", clock.now + seconds))
    monkeypatch.setattr(alert_oracle, "_get_benchmark_status", lambda: "mitigation")
    oracle = alert_oracle.AlertOracle(
        "astronomy-shop", match_labels=TARGET, buffer_seconds=0,
        poll_interval_seconds=10, sustained_silence_seconds=20, recovery_timeout_seconds=30,
    )
    states = iter([[{"labels": TARGET}], [{"labels": TARGET}], [], [], []])
    monkeypatch.setattr(oracle, "_query_firing_alerts", lambda: next(states))

    assert oracle.validate().success is True
    assert clock.now == 40


def test_checkout_validation_fails_if_alert_never_clears(monkeypatch):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(alert_oracle.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(alert_oracle.time, "sleep", lambda seconds: setattr(clock, "now", clock.now + seconds))
    monkeypatch.setattr(alert_oracle, "_get_benchmark_status", lambda: "mitigation")
    oracle = alert_oracle.AlertOracle(
        "astronomy-shop", match_labels=TARGET, buffer_seconds=0,
        poll_interval_seconds=10, sustained_silence_seconds=20, recovery_timeout_seconds=30,
    )
    monkeypatch.setattr(oracle, "_query_firing_alerts", lambda: [{"labels": TARGET}])

    result = oracle.validate()
    assert result.success is False
    assert clock.now == 30
