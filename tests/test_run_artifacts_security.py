import json

from sregym.run_artifacts import RunArtifacts


def test_finalize_redacts_host_credentials_from_all_artifacts(tmp_path, monkeypatch):
    secret = "sk-test-secret-that-must-not-be-published"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    run = RunArtifacts.create(
        staging_root=tmp_path / "staging",
        results_root=tmp_path / "results",
        problem_id="problem-one",
        agent="opencode",
        attempt=0,
    )
    (run.active_dir / "opencode.txt").write_text(f"OPENAI_API_KEY={secret}\n")
    session = run.active_dir / "sessions.json"
    session.write_text(json.dumps({"tool_output": f"env said {secret}"}))

    final_dir = run.finalize_and_publish(
        snapshot={"problem_id": "problem-one"},
        fieldnames=["problem_id"],
    )

    assert secret not in (final_dir / "opencode.txt").read_text()
    assert secret not in (final_dir / "sessions.json").read_text()
    assert "[REDACTED]" in (final_dir / "opencode.txt").read_text()
    assert "[REDACTED]" in json.loads((final_dir / "sessions.json").read_text())["tool_output"]
