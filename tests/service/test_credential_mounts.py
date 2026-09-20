import pytest

from sregym.service.container_runner import ContainerConfig, ContainerRunner, ExecInput
from sregym.service.internet_policy import InternetPolicy


def make_runner(**env_vars):
    """A runner with no filtered egress, so building args never invokes Docker."""
    return ContainerRunner(ContainerConfig(env_vars=env_vars, internet_policy=InternetPolicy.from_mode("open")))


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A home directory holding ~/.aws, with no AWS state leaking from the host."""
    (tmp_path / ".aws").mkdir()
    monkeypatch.setattr("sregym.service.container_runner.Path.home", lambda: tmp_path)
    for var in (*ContainerRunner.AWS_CREDENTIAL_VARS, *ContainerRunner.MODEL_ID_VARS):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def aws_mounts(args):
    return [args[i + 1] for i, item in enumerate(args) if item == "-v" and "/root/.aws" in args[i + 1]]


def container_env(args):
    values = [args[i + 1] for i, item in enumerate(args) if item == "-e"]
    return dict(value.split("=", 1) for value in values)


def docker_command(runner):
    try:
        return runner.build_docker_command(ExecInput(command="true"))
    finally:
        runner.cleanup_credential_tmps()


def test_aws_dir_not_mounted_without_aws_credentials(fake_home, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    runner = make_runner(AGENT_MODEL_ID="gpt-5")

    assert aws_mounts(runner._build_base_docker_args()) == []


def test_region_alone_does_not_mount_aws_dir(fake_home, monkeypatch):
    # Region says where to call, not which identity to call with.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    runner = make_runner(AGENT_MODEL_ID="gpt-5")

    assert aws_mounts(runner._build_base_docker_args()) == []


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("AWS_PROFILE", "bedrock"),
        ("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE"),
        ("AWS_ROLE_ARN", "arn:aws:iam::111122223333:role/bedrock"),
        ("AWS_BEARER_TOKEN_BEDROCK", "token"),
    ],
)
def test_aws_dir_mounted_when_credentials_selected(fake_home, monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    runner = make_runner()

    assert aws_mounts(runner._build_base_docker_args()) == [f"{fake_home / '.aws'}:/root/.aws:ro"]


@pytest.mark.parametrize(
    "model_id",
    [
        "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "bedrock/converse/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "bedrock-claude-sonnet-4.5",
        "amazon-bedrock/anthropic.claude-v2",
        "sagemaker/jumpstart-model",
    ],
)
def test_aws_model_id_mounts_aws_dir_without_any_aws_var(fake_home, model_id):
    # A default profile in ~/.aws/config sets no AWS_* var, so the model id has
    # to keep the mount alive on its own.
    runner = make_runner(AGENT_MODEL_ID=model_id)

    assert aws_mounts(runner._build_base_docker_args()) == [f"{fake_home / '.aws'}:/root/.aws:ro"]


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-5",
        "anthropic/claude-sonnet-4-6-20250627",
        "local/qwen3",
        # Provider names that merely start with an AWS one.
        "bedrockery/local-model",
        "sagemakerless/mock",
    ],
)
def test_non_aws_model_id_does_not_mount_aws_dir(fake_home, model_id):
    runner = make_runner(AGENT_MODEL_ID=model_id)

    assert aws_mounts(runner._build_base_docker_args()) == []


def test_empty_kickoff_value_masks_the_host_var(fake_home, monkeypatch):
    # _build_env_flags lets ExecInput.env overwrite with "", so the container
    # never sees the host value; the gate must not mount on it either.
    monkeypatch.setenv("AWS_PROFILE", "production")
    runner = make_runner()
    exec_input = ExecInput(command="true", env={"AWS_PROFILE": ""})

    try:
        cmd = runner.build_docker_command(exec_input)
    finally:
        runner.cleanup_credential_tmps()

    assert aws_mounts(cmd) == []


def test_bedrock_judge_does_not_mount_into_agent_container(fake_home):
    # The judge runs host-side in the conductor, not in the agent container.
    runner = make_runner(
        AGENT_MODEL_ID="gpt-5",
        JUDGE_MODEL_ID="bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    )

    assert aws_mounts(runner._build_base_docker_args()) == []


def test_native_opencode_agent_does_not_receive_host_judge_credentials(fake_home, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "judge-provider-secret")
    monkeypatch.setenv("JUDGE_API_KEY", "explicit-judge-secret")
    monkeypatch.setenv("JUDGE_MODEL_ID", "gpt-4o-mini")
    runner = make_runner(AGENT_MODEL_ID="opencode/muse-spark-1.3-contributor-free")

    env = container_env(docker_command(runner))

    assert env["AGENT_MODEL_ID"] == "opencode/muse-spark-1.3-contributor-free"
    assert "OPENAI_API_KEY" not in env
    assert "JUDGE_API_KEY" not in env
    assert "JUDGE_MODEL_ID" not in env


def test_openai_agent_receives_only_its_provider_credentials(fake_home, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "agent-provider-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unrelated-provider-secret")
    runner = make_runner(AGENT_MODEL_ID="openai/gpt-5")

    env = container_env(docker_command(runner))

    assert env["OPENAI_API_KEY"] == "agent-provider-secret"
    assert "ANTHROPIC_API_KEY" not in env


def test_explicit_agent_api_key_reaches_agent_container(fake_home, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "explicit-agent-secret")
    runner = make_runner(AGENT_MODEL_ID="local/qwen3")

    env = container_env(docker_command(runner))

    assert env["AGENT_API_KEY"] == "explicit-agent-secret"


def test_non_codex_runner_does_not_mount_codex_auth(fake_home):
    auth = fake_home / ".codex" / "auth.json"
    auth.parent.mkdir()
    auth.write_text('{"tokens": {}}')
    runner = make_runner(AGENT_MODEL_ID="opencode/muse-spark-1.3-contributor-free")

    args = runner._build_base_docker_args()

    assert not any("/root/.codex/auth.json" in item for item in args)


def test_kickoff_env_reaches_the_gate(fake_home):
    # kickoff_env arrives via ExecInput, not config.env_vars or os.environ.
    runner = make_runner()
    exec_input = ExecInput(command="true", env={"AWS_PROFILE": "bedrock"})

    try:
        cmd = runner.build_docker_command(exec_input)
    finally:
        runner.cleanup_credential_tmps()

    assert aws_mounts(cmd) == [f"{fake_home / '.aws'}:/root/.aws:ro"]


def test_missing_aws_dir_mounts_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("sregym.service.container_runner.Path.home", lambda: tmp_path)
    monkeypatch.setenv("AWS_PROFILE", "bedrock")
    runner = make_runner()

    assert aws_mounts(runner._build_base_docker_args()) == []
