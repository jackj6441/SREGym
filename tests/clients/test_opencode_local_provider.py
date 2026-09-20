import json

import pytest

from clients.opencode.opencode_agent import OpenCodeAgent, write_local_provider_config


def read_config(path):
    with open(path) as handle:
        return json.load(handle)


def test_declares_the_local_provider_for_the_requested_model(tmp_path):
    path = write_local_provider_config(
        "local/qwen3", tmp_path / "opencode.json", {"AGENT_API_BASE": "http://host:11434/v1"}
    )
    provider = read_config(path)["provider"]["local"]

    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "{env:AGENT_API_BASE}"
    assert "qwen3" in provider["models"]


def test_model_name_keeps_slashes_after_the_provider(tmp_path):
    path = write_local_provider_config(
        "local/org/model-v2", tmp_path / "opencode.json", {"AGENT_API_BASE": "http://host:11434/v1"}
    )

    assert "org/model-v2" in read_config(path)["provider"]["local"]["models"]


def test_api_key_is_referenced_only_when_set(tmp_path):
    env = {"AGENT_API_BASE": "http://host:11434/v1"}
    without = read_config(write_local_provider_config("local/m", tmp_path / "a.json", env))
    with_key = read_config(write_local_provider_config("local/m", tmp_path / "b.json", {**env, "AGENT_API_KEY": "k"}))

    assert "apiKey" not in without["provider"]["local"]["options"]
    assert with_key["provider"]["local"]["options"]["apiKey"] == "{env:AGENT_API_KEY}"


def test_secrets_are_referenced_not_inlined(tmp_path):
    # The config lands in the logs dir, so it must hold env references only.
    path = write_local_provider_config(
        "local/m", tmp_path / "opencode.json", {"AGENT_API_BASE": "http://host/v1", "AGENT_API_KEY": "sk-secret"}
    )

    assert "sk-secret" not in path.read_text()


def test_missing_api_base_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="AGENT_API_BASE"):
        write_local_provider_config("local/m", tmp_path / "opencode.json", {})


def test_config_allows_read_only_access_to_mounted_app_manifests(tmp_path):
    config = read_config(
        write_local_provider_config(
            "local/m",
            tmp_path / "opencode.json",
            {"AGENT_API_BASE": "http://host:11434/v1"},
        )
    )

    assert config["permission"]["external_directory"]["/opt/sregym/SREGym-applications/**"] == "allow"
    assert config["permission"]["edit"]["/opt/sregym/SREGym-applications/**"] == "deny"


def test_native_opencode_model_also_uses_the_safe_permission_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "judge-secret")
    monkeypatch.setenv("JUDGE_API_KEY", "explicit-judge-secret")
    agent = OpenCodeAgent(tmp_path, "opencode/muse-spark-1.3-contributor-free")

    env = agent._build_env()
    config = read_config(env["OPENCODE_CONFIG"])

    assert "OPENAI_API_KEY" not in env
    assert "JUDGE_API_KEY" not in env
    assert "provider" not in config
    assert config["permission"]["external_directory"]["/opt/sregym/SREGym-applications/**"] == "allow"
    assert config["permission"]["edit"]["/opt/sregym/SREGym-applications/**"] == "deny"
