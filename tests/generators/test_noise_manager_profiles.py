import threading
from unittest.mock import Mock, patch

import pytest

from sregym.generators.noise import manager
from sregym.generators.noise.impl.cpu_noisy_neighbor import CPU_NOISY_NEIGHBOR_PROFILE


class _Thread:
    def __init__(self, *args, **kwargs):
        self.started = False

    def start(self):
        self.started = True

    def join(self, timeout=None):
        return None


@pytest.fixture
def noise_manager():
    with patch.object(manager.NoiseManager, "_instance", None), patch.object(manager, "KubeCtl") as kubectl:
        noise = manager.NoiseManager()
        yield noise, kubectl.return_value
        manager.NoiseManager._instance = None


def test_cpu_profile_starts_synchronously_without_installing_chaos_mesh(noise_manager, monkeypatch):
    noise, kubectl = noise_manager
    resource = {
        "kind": "pod",
        "name": "analytics-backfill-run",
        "namespace": "hotel-reservation",
        "node": "kind-worker2",
    }
    injector = Mock()
    injector.inject.return_value = resource
    monkeypatch.setattr(manager, "CpuNoisyNeighbor", Mock(return_value=injector))
    monkeypatch.setattr(manager.threading, "Thread", _Thread)
    ensure_chaos = Mock()
    monkeypatch.setattr(noise, "_ensure_chaos_mesh_installed", ensure_chaos)
    noise.set_problem_context(
        {
            "namespace": "hotel-reservation",
            "target_service": "frontend",
            "noise_profile": CPU_NOISY_NEIGHBOR_PROFILE,
            "noise_cpu_workers": 2,
            "noise_duration_seconds": 120,
        }
    )

    noise.start()

    ensure_chaos.assert_not_called()
    injector.inject.assert_called_once_with(
        namespace="hotel-reservation",
        target_deployment="frontend",
        cpu_workers=2,
        duration_seconds=120,
    )
    assert noise.running is True
    assert noise.active_workloads == [resource]
    assert noise._background_thread.started is True
    kubectl.exec_command.assert_not_called()


def test_default_noise_mode_keeps_the_existing_chaos_mesh_path(noise_manager, monkeypatch):
    noise, _ = noise_manager
    ensure_chaos = Mock(side_effect=lambda: setattr(noise, "_chaos_mesh_ready", True))
    cpu_injector = Mock()
    monkeypatch.setattr(noise, "_ensure_chaos_mesh_installed", ensure_chaos)
    monkeypatch.setattr(manager, "CpuNoisyNeighbor", cpu_injector)
    monkeypatch.setattr(manager.threading, "Thread", _Thread)
    noise.set_problem_context({"namespace": "hotel-reservation", "noise_profile": None})

    noise.start()

    ensure_chaos.assert_called_once_with()
    cpu_injector.assert_not_called()
    assert noise.running is True
    assert noise._background_thread.started is True


def test_unknown_profile_is_rejected(noise_manager):
    noise, _ = noise_manager
    noise.set_problem_context({"namespace": "hotel-reservation", "noise_profile": "unknown-profile"})

    with pytest.raises(ValueError, match="Unknown noise profile"):
        noise.start()


def test_cpu_profile_stop_deletes_only_the_owned_workload(noise_manager, monkeypatch):
    noise, kubectl = noise_manager
    resource = {
        "kind": "pod",
        "name": "analytics-backfill-owned",
        "namespace": "hotel-reservation",
        "node": "kind-worker",
    }
    injector = Mock()
    monkeypatch.setattr(manager, "CpuNoisyNeighbor", Mock(return_value=injector))
    noise.noise_profile = CPU_NOISY_NEIGHBOR_PROFILE
    noise.running = True
    noise.active_workloads = [resource]

    noise.stop()

    injector.delete.assert_called_once_with(resource)
    assert noise.active_workloads == []
    assert noise.running is False
    kubectl.exec_command.assert_not_called()


def test_selected_profile_failure_is_not_silently_ignored(noise_manager, monkeypatch):
    noise, _ = noise_manager
    injector = Mock()
    injector.inject.side_effect = RuntimeError("stress image unavailable")
    monkeypatch.setattr(manager, "CpuNoisyNeighbor", Mock(return_value=injector))
    noise.set_problem_context(
        {
            "namespace": "hotel-reservation",
            "target_service": "frontend",
            "noise_profile": CPU_NOISY_NEIGHBOR_PROFILE,
        }
    )

    with pytest.raises(RuntimeError, match="stress image unavailable"):
        noise.start()

    assert noise.running is False
    assert noise.active_workloads == []


def test_workload_created_after_stop_is_immediately_deleted(noise_manager, monkeypatch):
    noise, _ = noise_manager
    injection_started = threading.Event()
    release_injection = threading.Event()
    resource = {
        "kind": "pod",
        "name": "analytics-backfill-late",
        "namespace": "hotel-reservation",
        "node": "kind-worker",
    }
    injector = Mock()

    def inject(**kwargs):
        injection_started.set()
        release_injection.wait(2)
        return resource

    injector.inject.side_effect = inject
    monkeypatch.setattr(manager, "CpuNoisyNeighbor", Mock(return_value=injector))
    noise.set_problem_context(
        {
            "namespace": "hotel-reservation",
            "target_service": "frontend",
            "noise_profile": CPU_NOISY_NEIGHBOR_PROFILE,
        }
    )
    noise.running = True

    worker = threading.Thread(target=noise._maybe_inject)
    worker.start()
    assert injection_started.wait(1)
    noise.stop()
    release_injection.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    injector.delete.assert_called_once_with(resource)
    assert noise.active_workloads == []
