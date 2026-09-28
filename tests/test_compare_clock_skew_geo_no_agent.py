import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest


def _compare_windows():
    path = Path(__file__).resolve().parents[1] / "scripts" / "compare_clock_skew_geo_no_agent.py"
    spec = importlib.util.spec_from_file_location("sregym_geo_window_comparison_for_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.compare_windows


@pytest.fixture
def control_window():
    return {
        "topology": {
            "digests": {
                name: "sha256:" + "a" * 64
                for name in ("frontend", "geo", "profile", "rate", "recommendation", "reservation", "search", "user")
            },
            "endpoints": {name: ["10.0.0.1"] for name in ("frontend", "search", "geo", "rate")},
        },
        "workload": {"submitted": 480, "completed": 480, "success_rate": 0.0},
        "rate_queue_depth_before": 256,
        "rate_queue_depth_after": 255,
        "counters": {
            "search_requests_total": 480,
            "search_rate_attempts_total": 1441,
            "search_rate_timeouts_total": 1202,
            "rate_requests_received_total": 1440,
            "rate_requests_completed_total": 1202,
            "rate_requests_expired_total": 1202,
            "rate_requests_rejected_total": 239,
        },
    }


def test_matched_geo_window_accepts_unchanged_primary_failure(control_window):
    treatment = deepcopy(control_window)
    treatment["counters"]["search_rate_attempts_total"] = 1438
    treatment["counters"]["rate_requests_rejected_total"] = 238
    treatment["topology"]["endpoints"]["rate"] = ["10.0.0.2"]  # Re-deployment changes Pod IP.

    _compare_windows()(control_window, treatment)


def test_matched_geo_window_rejects_new_search_path_failure(control_window):
    treatment = deepcopy(control_window)
    treatment["counters"]["search_rate_attempts_total"] = 10

    with pytest.raises(AssertionError, match="Search-to-Rate retry amplification"):
        _compare_windows()(control_window, treatment)


def test_matched_geo_window_rejects_digest_drift(control_window):
    treatment = deepcopy(control_window)
    treatment["topology"]["digests"]["geo"] = "sha256:" + "b" * 64

    with pytest.raises(AssertionError, match="different resolved image digests"):
        _compare_windows()(control_window, treatment)


def test_matched_geo_window_rejects_endpoint_drift(control_window):
    treatment = deepcopy(control_window)
    treatment["topology"]["endpoints"]["rate"] = []

    with pytest.raises(AssertionError, match="different critical-path endpoint counts"):
        _compare_windows()(control_window, treatment)
