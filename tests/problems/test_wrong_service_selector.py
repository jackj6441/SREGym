from types import SimpleNamespace

import pytest

from sregym.conductor.problems.wrong_service_selector import WrongServiceSelector


def _problem(*, matching_pods=None, ready_endpoints=None, selector=None):
    problem = object.__new__(WrongServiceSelector)
    problem.namespace = "hotel-reservation"
    problem.faulty_service = "rate"
    core_v1 = SimpleNamespace(
        read_namespaced_service=lambda **_: SimpleNamespace(
            spec=SimpleNamespace(selector=selector or {"app": "rate", "current_service_name": "rate"})
        ),
        list_namespaced_pod=lambda **_: SimpleNamespace(items=matching_pods or []),
        read_namespaced_endpoints=lambda **_: SimpleNamespace(
            subsets=[SimpleNamespace(addresses=ready_endpoints or [])]
        ),
    )
    problem.kubectl = SimpleNamespace(core_v1_api=core_v1)
    return problem


def test_preflight_accepts_the_expected_selector_mismatch_with_no_ready_endpoints():
    _problem().validate_fault_preflight()


def test_preflight_rejects_a_selector_without_the_injected_key():
    problem = _problem(selector={"app": "rate"})

    with pytest.raises(RuntimeError, match="does not contain the injected selector"):
        problem.validate_fault_preflight()


def test_preflight_rejects_when_the_service_still_selects_pods():
    problem = _problem(matching_pods=[SimpleNamespace()])

    with pytest.raises(RuntimeError, match="still selects Pod"):
        problem.validate_fault_preflight()


def test_preflight_rejects_when_the_service_still_has_ready_endpoints():
    problem = _problem(ready_endpoints=[SimpleNamespace(ip="10.0.0.8")])

    with pytest.raises(RuntimeError, match="still has ready endpoints"):
        problem.validate_fault_preflight()
