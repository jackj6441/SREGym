from unittest.mock import Mock, call

from clients.opencode import driver


def test_continues_same_session_until_both_stages_are_submitted(monkeypatch):
    agent = Mock()
    agent.run.return_value = 0
    agent._get_session_id.return_value = "ses-123"
    stages = iter(["diagnosis", "mitigation", "tearing_down"])
    monkeypatch.setattr(driver, "get_current_stage", lambda: next(stages))

    result = driver.run_until_conductor_finishes(agent, "initial task", max_continuations=3)

    assert result == 0
    assert agent.run.call_args_list[0] == call("initial task", export_session=False)
    assert agent.run.call_args_list[1].kwargs == {"export_session": False, "session_id": "ses-123"}
    assert "diagnosis" in agent.run.call_args_list[1].args[0]
    assert agent.run.call_args_list[2].kwargs == {"export_session": False, "session_id": "ses-123"}
    assert "mitigation" in agent.run.call_args_list[2].args[0]
    agent.export_session.assert_called_once_with()


def test_policy_denial_does_not_prevent_same_session_continuation(monkeypatch):
    """A denied tool is a recoverable sandbox event, not a terminal stage outcome."""
    agent = Mock()
    agent.run.return_value = 0
    agent._get_session_id.return_value = "ses-after-denial"
    stages = iter(["diagnosis", "mitigation", "tearing_down"])
    monkeypatch.setattr(driver, "get_current_stage", lambda: next(stages))

    result = driver.run_until_conductor_finishes(agent, "initial task", max_continuations=3)

    assert result == 0
    diagnosis_retry = agent.run.call_args_list[1].args[0]
    assert "sandbox policy" in diagnosis_retry
    assert agent.run.call_args_list[1].kwargs == {"export_session": False, "session_id": "ses-after-denial"}


def test_returns_failure_after_bounded_no_submission_retries(monkeypatch):
    agent = Mock()
    agent.run.return_value = 0
    agent._get_session_id.return_value = "ses-stuck"
    monkeypatch.setattr(driver, "get_current_stage", lambda: "diagnosis")

    result = driver.run_until_conductor_finishes(agent, "initial task", max_continuations=2)

    assert result == 1
    assert agent.run.call_count == 3
    agent.export_session.assert_called_once_with()


def test_does_not_retry_a_failed_opencode_process(monkeypatch):
    agent = Mock()
    agent.run.return_value = 7
    current_stage = Mock(return_value="diagnosis")
    monkeypatch.setattr(driver, "get_current_stage", current_stage)

    result = driver.run_until_conductor_finishes(agent, "initial task")

    assert result == 7
    current_stage.assert_not_called()
    agent.export_session.assert_called_once_with()
