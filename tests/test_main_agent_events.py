import json
from types import SimpleNamespace

from main import _agent_infrastructure_event


def test_tool_policy_denial_is_classified_as_an_infrastructure_event(tmp_path):
    (tmp_path / "opencode_results_problem_20260923.json").write_text(
        json.dumps({"tool_policy_denials": 1}),
        encoding="utf-8",
    )

    assert _agent_infrastructure_event(SimpleNamespace(active_dir=tmp_path)) == "tool_policy_rejected"


def test_absent_or_zero_tool_policy_denials_are_not_classified(tmp_path):
    (tmp_path / "opencode_results_problem_20260923.json").write_text(
        json.dumps({"tool_policy_denials": 0}),
        encoding="utf-8",
    )

    assert _agent_infrastructure_event(SimpleNamespace(active_dir=tmp_path)) is None
