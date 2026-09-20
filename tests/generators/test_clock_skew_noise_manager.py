from unittest.mock import Mock, patch

import pytest

from sregym.generators.noise import manager
from sregym.generators.noise.impl.clock_skew import CLOCK_SKEW_PROFILE


@pytest.fixture
def noise_manager():
    with patch.object(manager.NoiseManager, "_instance", None), patch.object(manager, "KubeCtl") as kubectl:
        noise = manager.NoiseManager()
        yield noise, kubectl.return_value
        manager.NoiseManager._instance = None


def test_clock_profile_creates_the_observer_and_time_chaos_before_the_agent_starts(noise_manager, monkeypatch):
    noise, kubectl = noise_manager
    resource = {
        "name": "analytics-clock-observer-run",
        "namespace": "hotel-reservation",
        "node": "kind-worker2",
        "selector_value": "run",
    }
    observer = Mock()
    observer.inject.return_value = resource
    observer_type = Mock(return_value=observer)
    observer_type.time_chaos_spec.return_value = {
        "mode": "one",
        "selector": {"namespaces": ["hotel-reservation"], "labelSelectors": {"sregym.io/noise-run": "run"}},
        "containerNames": ["observer"],
        "timeOffset": "+5m",
        "duration": "120s",
    }
    monkeypatch.setattr(manager, "ClockSkewObserver", observer_type)
    ensure_chaos = Mock(side_effect=lambda: setattr(noise, "_chaos_mesh_ready", True))
    monkeypatch.setattr(noise, "_ensure_chaos_mesh_installed", ensure_chaos)
    noise.set_problem_context(
        {
            "namespace": "hotel-reservation",
            "target_deployment": "frontend",
            "noise_profile": CLOCK_SKEW_PROFILE,
            "noise_duration_seconds": 120,
        }
    )

    noise.start()

    ensure_chaos.assert_called_once_with()
    observer.inject.assert_called_once_with(namespace="hotel-reservation", target_deployment="frontend")
    assert noise.running is True
    assert noise.active_workloads == [resource]
    assert noise.active_experiments[0]["kind"] == "TimeChaos"
    assert noise._background_thread is None
    kubectl.exec_command.assert_called_once()
    applied = kubectl.exec_command.call_args.args[0]
    assert applied.startswith("kubectl apply -f ")


def test_clock_profile_does_not_reinject_after_the_random_noise_cooldown(noise_manager, monkeypatch):
    noise, kubectl = noise_manager
    resource = {
        "name": "analytics-clock-observer-run",
        "namespace": "hotel-reservation",
        "node": "kind-worker2",
        "selector_value": "run",
    }
    observer = Mock()
    observer.inject.return_value = resource
    observer_type = Mock(return_value=observer)
    observer_type.time_chaos_spec.return_value = {
        "mode": "one",
        "selector": {"namespaces": ["hotel-reservation"], "labelSelectors": {"sregym.io/noise-run": "run"}},
        "containerNames": ["observer"],
        "timeOffset": "+5m",
        "duration": "3600s",
    }
    monkeypatch.setattr(manager, "ClockSkewObserver", observer_type)
    monkeypatch.setattr(noise, "_ensure_chaos_mesh_installed", lambda: setattr(noise, "_chaos_mesh_ready", True))
    noise.set_problem_context(
        {
            "namespace": "hotel-reservation",
            "target_deployment": "frontend",
            "noise_profile": CLOCK_SKEW_PROFILE,
            "noise_duration_seconds": 3600,
        }
    )

    noise.start()
    workload_identity = noise.active_workloads[0]
    experiment_identity = noise.active_experiments[0]
    cleanup_experiments = Mock()
    cleanup_workloads = Mock()
    monkeypatch.setattr(noise, "_cleanup_experiments", cleanup_experiments)
    monkeypatch.setattr(noise, "_cleanup_workloads", cleanup_workloads)
    monkeypatch.setattr(manager.time, "time", lambda: noise._last_injection_time + manager.COOLDOWN + 1)

    noise._maybe_inject()

    observer.inject.assert_called_once_with(namespace="hotel-reservation", target_deployment="frontend")
    assert noise.active_workloads == [workload_identity]
    assert noise.active_workloads[0] is workload_identity
    assert noise.active_experiments == [experiment_identity]
    assert noise.active_experiments[0] is experiment_identity
    kubectl.exec_command.assert_called_once()
    cleanup_experiments.assert_not_called()
    cleanup_workloads.assert_not_called()


def test_clock_profile_stop_removes_the_time_chaos_before_its_owned_observer(noise_manager, monkeypatch):
    noise, kubectl = noise_manager
    resource = {
        "name": "analytics-clock-observer-run",
        "namespace": "hotel-reservation",
        "node": "kind-worker2",
        "selector_value": "run",
    }
    observer = Mock()
    monkeypatch.setattr(manager, "ClockSkewObserver", Mock(return_value=observer))
    monkeypatch.setattr(noise, "_force_remove_all_chaos_resources", Mock())
    noise.noise_profile = CLOCK_SKEW_PROFILE
    noise.running = True
    noise.active_workloads = [resource]
    noise.active_experiments = [{"name": "noise-clock-skew-123", "kind": "TimeChaos"}]

    noise.stop()

    observer.delete.assert_called_once_with(resource)
    delete_command = kubectl.exec_command.call_args_list[0].args[0]
    assert delete_command.startswith("kubectl delete TimeChaos noise-clock-skew-123")


def test_clock_profile_removes_the_temporary_manifest_when_apply_fails(noise_manager, monkeypatch, tmp_path):
    noise, kubectl = noise_manager
    manifest = tmp_path / "clock-skew.yaml"

    class _Manifest:
        name = str(manifest)

        def __enter__(self):
            self.handle = manifest.open("w")
            return self.handle

        def __exit__(self, *args):
            self.handle.close()

    monkeypatch.setattr(manager.tempfile, "NamedTemporaryFile", lambda **_kwargs: _Manifest())
    kubectl.exec_command.side_effect = RuntimeError("kubectl unavailable")
    noise.target_namespace = "hotel-reservation"

    with pytest.raises(RuntimeError, match="kubectl unavailable"):
        noise._apply_experiment({"name": "clock-skew", "kind": "TimeChaos", "spec": {}}, raise_on_error=True)

    assert not manifest.exists()
