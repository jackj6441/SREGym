from types import SimpleNamespace
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


def _mock_treatment(monkeypatch):
    target = {"name": "recommendation-abc", "uid": "pod-uid", "namespace": "hotel-reservation"}
    treatment = Mock()
    treatment.select_target.return_value = target
    treatment.capture_baseline.return_value = "healthy-primary-baseline"
    treatment.time_chaos_spec.return_value = {
        "mode": "one",
        "selector": {"pods": {"hotel-reservation": [target["name"]]}},
        "containerNames": ["hotel-reserv-recommendation"],
        "timeOffset": "+5m",
        "duration": "3600s",
    }
    treatment_type = Mock(return_value=treatment)
    monkeypatch.setattr(manager, "ClockSkewRecommendation", treatment_type)
    return treatment, treatment_type, target


def test_clock_profile_uses_existing_recommendation_pod_and_one_time_chaos_before_agent(noise_manager, monkeypatch):
    noise, kubectl = noise_manager
    treatment, treatment_type, target = _mock_treatment(monkeypatch)
    ensure_chaos = Mock(side_effect=lambda: setattr(noise, "_chaos_mesh_ready", True))
    monkeypatch.setattr(noise, "_ensure_chaos_mesh_installed", ensure_chaos)
    noise.set_problem_context(
        {
            "namespace": "hotel-reservation",
            "protected_workload": "search-workload",
            "noise_profile": CLOCK_SKEW_PROFILE,
            "noise_duration_seconds": 3600,
        }
    )

    noise.start()

    ensure_chaos.assert_called_once_with()
    treatment_type.assert_called_once_with(kubectl, "search-workload")
    treatment.select_target.assert_called_once_with("hotel-reservation")
    treatment.capture_baseline.assert_called_once_with(target)
    treatment.wait_for_treatment_effect.assert_called_once_with(target, "healthy-primary-baseline")
    assert noise.running is True
    assert noise.active_workloads == []
    assert noise.active_experiments[0]["kind"] == "TimeChaos"
    assert noise.active_experiments[0]["name"].startswith("hotel-recommendation-")
    assert "noise" not in noise.active_experiments[0]["name"]
    assert "clock-skew" not in noise.active_experiments[0]["name"]
    assert noise._background_thread is None
    kubectl.exec_command_checked.assert_called_once()
    applied = kubectl.exec_command_checked.call_args.args[0]
    assert applied.startswith("kubectl apply -f ")


def test_evidence_records_verified_preflight_and_agent_window_pod_replacement(noise_manager, monkeypatch):
    noise, kubectl = noise_manager
    treatment, _, target = _mock_treatment(monkeypatch)
    treatment.capture_baseline.return_value = SimpleNamespace(queue_depth=256, search_success_rate=0.2)
    treatment.wait_for_treatment_effect.return_value = SimpleNamespace(
        recommendation_status=502, queue_depth=255, search_success_rate=0.19
    )
    monkeypatch.setattr(noise, "_ensure_chaos_mesh_installed", lambda: setattr(noise, "_chaos_mesh_ready", True))
    noise.set_problem_context({"namespace": "hotel-reservation", "noise_profile": CLOCK_SKEW_PROFILE})

    noise.start()
    before = noise.evidence_snapshot()
    assert before["preflight"]["recommendation_status_before"] == 200
    assert before["preflight"]["recommendation_status_after"] == 502
    assert before["preflight"]["primary_queue_depth_before"] == 256
    assert before["target"]["pod_uid"] == target["uid"]
    assert before["treatment"]["timechaos_name"] == noise.active_experiments[0]["name"]

    kubectl.core_v1_api.read_namespaced_pod.return_value.metadata.uid = "replacement-uid"
    monkeypatch.setattr(noise, "_cleanup_experiments", Mock(return_value=True))
    monkeypatch.setattr(noise, "_force_remove_all_chaos_resources", Mock())
    noise.stop()
    after = noise.evidence_snapshot()
    assert after["target"]["changed_before_cleanup"] is True
    assert after["cleanup"]["status"] == "confirmed"
    treatment.wait_for_recovery.assert_called_once_with(target)


def test_clock_profile_fails_closed_when_time_chaos_does_not_create_the_expected_fault(noise_manager, monkeypatch):
    noise, kubectl = noise_manager
    treatment, _, target = _mock_treatment(monkeypatch)
    treatment.wait_for_treatment_effect.side_effect = TimeoutError("clock skew fault did not become observable")
    monkeypatch.setattr(noise, "_ensure_chaos_mesh_installed", lambda: setattr(noise, "_chaos_mesh_ready", True))
    monkeypatch.setattr(noise, "_force_remove_all_chaos_resources", Mock())
    kubectl.exec_command_checked.return_value = ""
    noise.set_problem_context(
        {
            "namespace": "hotel-reservation",
            "noise_profile": CLOCK_SKEW_PROFILE,
            "noise_duration_seconds": 3600,
        }
    )

    with pytest.raises(TimeoutError, match="did not become observable"):
        noise.start()

    assert noise.running is False
    treatment.wait_for_treatment_effect.assert_called_once_with(target, "healthy-primary-baseline")
    assert noise.active_workloads == []
    commands = [call.args[0] for call in kubectl.exec_command_checked.call_args_list]
    assert commands[0].startswith("kubectl apply -f ")
    assert any("kubectl delete TimeChaos" in command for command in commands)


def test_clock_profile_fails_fast_when_time_chaos_apply_is_rejected(noise_manager, monkeypatch):
    noise, kubectl = noise_manager
    treatment, _, _ = _mock_treatment(monkeypatch)
    monkeypatch.setattr(noise, "_ensure_chaos_mesh_installed", lambda: setattr(noise, "_chaos_mesh_ready", True))
    monkeypatch.setattr(noise, "_force_remove_all_chaos_resources", Mock())
    kubectl.exec_command_checked.side_effect = RuntimeError("TimeChaos admission rejected")
    noise.set_problem_context(
        {
            "namespace": "hotel-reservation",
            "noise_profile": CLOCK_SKEW_PROFILE,
            "noise_duration_seconds": 3600,
        }
    )

    with pytest.raises(RuntimeError, match="admission rejected"):
        noise.start()

    assert noise.running is False
    assert noise.active_experiments == []
    treatment.wait_for_treatment_effect.assert_not_called()
    assert noise.active_workloads == []


def test_clock_profile_reports_failed_recovery_after_preflight_abort(noise_manager, monkeypatch):
    noise, kubectl = noise_manager
    treatment, _, target = _mock_treatment(monkeypatch)
    treatment.wait_for_treatment_effect.side_effect = TimeoutError("effect missing")
    treatment.wait_for_recovery.side_effect = TimeoutError("still skewed")
    monkeypatch.setattr(noise, "_ensure_chaos_mesh_installed", lambda: setattr(noise, "_chaos_mesh_ready", True))
    monkeypatch.setattr(noise, "_force_remove_all_chaos_resources", Mock())
    kubectl.exec_command_checked.return_value = ""
    noise.set_problem_context({"namespace": "hotel-reservation", "noise_profile": CLOCK_SKEW_PROFILE})

    with pytest.raises(RuntimeError, match="recovery could not be confirmed"):
        noise.start()

    assert noise.running is False
    assert noise._deterministic_target == target
    treatment.wait_for_recovery.assert_called_once_with(target)


def test_clock_profile_reports_unconfirmed_time_chaos_deletion_after_preflight_abort(noise_manager, monkeypatch):
    noise, kubectl = noise_manager
    treatment, _, _ = _mock_treatment(monkeypatch)
    treatment.wait_for_treatment_effect.side_effect = TimeoutError("effect missing")
    monkeypatch.setattr(noise, "_ensure_chaos_mesh_installed", lambda: setattr(noise, "_chaos_mesh_ready", True))
    monkeypatch.setattr(noise, "_cleanup_experiments", Mock(return_value=False))
    force_remove = Mock()
    monkeypatch.setattr(noise, "_force_remove_all_chaos_resources", force_remove)
    noise.set_problem_context({"namespace": "hotel-reservation", "noise_profile": CLOCK_SKEW_PROFILE})

    with pytest.raises(RuntimeError, match="deletion could not be confirmed"):
        noise.start()

    assert noise.running is False
    force_remove.assert_called_once_with()
    treatment.wait_for_recovery.assert_not_called()
    kubectl.exec_command_checked.assert_called_once()


def test_clock_profile_does_not_reinject_after_the_random_noise_cooldown(noise_manager, monkeypatch):
    noise, kubectl = noise_manager
    treatment, _, _ = _mock_treatment(monkeypatch)
    monkeypatch.setattr(noise, "_ensure_chaos_mesh_installed", lambda: setattr(noise, "_chaos_mesh_ready", True))
    noise.set_problem_context(
        {
            "namespace": "hotel-reservation",
            "noise_profile": CLOCK_SKEW_PROFILE,
            "noise_duration_seconds": 3600,
        }
    )

    noise.start()
    experiment_identity = noise.active_experiments[0]
    cleanup_experiments = Mock()
    cleanup_workloads = Mock()
    monkeypatch.setattr(noise, "_cleanup_experiments", cleanup_experiments)
    monkeypatch.setattr(noise, "_cleanup_workloads", cleanup_workloads)
    monkeypatch.setattr(manager.time, "time", lambda: noise._last_injection_time + manager.COOLDOWN + 1)

    noise._maybe_inject()

    treatment.select_target.assert_called_once_with("hotel-reservation")
    assert noise.active_workloads == []
    assert noise.active_experiments == [experiment_identity]
    assert noise.active_experiments[0] is experiment_identity
    kubectl.exec_command_checked.assert_called_once()
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
    kubectl.exec_command_checked.return_value = ""
    noise.noise_profile = CLOCK_SKEW_PROFILE
    noise.running = True
    noise.active_workloads = [resource]
    noise.active_experiments = [{"name": "noise-clock-skew-123", "kind": "TimeChaos"}]

    noise.stop()

    observer.delete.assert_called_once_with(resource)
    delete_command = kubectl.exec_command_checked.call_args_list[0].args[0]
    assert delete_command.startswith("kubectl delete TimeChaos noise-clock-skew-123")


def test_clock_profile_confirms_recommendation_recovery_after_time_chaos_deletion(noise_manager, monkeypatch):
    noise, _ = noise_manager
    treatment, _, target = _mock_treatment(monkeypatch)
    events = []
    noise.noise_profile = CLOCK_SKEW_PROFILE
    noise._deterministic_target = target
    noise.running = True
    monkeypatch.setattr(noise, "_cleanup_experiments", lambda: events.append("delete-timechaos") or True)
    monkeypatch.setattr(noise, "_cleanup_workloads", Mock())
    monkeypatch.setattr(noise, "_force_remove_all_chaos_resources", Mock())
    treatment.wait_for_recovery.side_effect = lambda _target: events.append("verify-recovery")

    noise.stop()

    assert events == ["delete-timechaos", "verify-recovery"]
    treatment.wait_for_recovery.assert_called_once_with(target)
    assert noise._deterministic_target is None


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
    kubectl.exec_command_checked.side_effect = RuntimeError("kubectl unavailable")
    noise.target_namespace = "hotel-reservation"

    with pytest.raises(RuntimeError, match="kubectl unavailable"):
        noise._apply_experiment({"name": "clock-skew", "kind": "TimeChaos", "spec": {}}, raise_on_error=True)

    assert not manifest.exists()
