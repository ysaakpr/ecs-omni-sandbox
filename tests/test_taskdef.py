from __future__ import annotations

import json

import pytest
from omnigent.onboarding.sandboxes.types import RepoWorkspace

from omnigent.community.sandbox.ecs.config import EcsSandboxConfig
from omnigent.community.sandbox.ecs.taskdef import (
    build_run_task,
    build_task_definition,
    workspace_path,
)
from tests.conftest import TOKEN, base_config

FAKE_ACCESS_POINT = "fsap-0123456789abcdef0"  # gitleaks:allow (placeholder)
SECRET_ARN = "arn:aws:secretsmanager:ap-south-1:123456789012:secret:omnigent-ecs/sb/ab-XyZ"


def _taskdef(config: EcsSandboxConfig, **kwargs):
    return build_task_definition(
        config,
        sandbox_id="omni-sb-1234abcd",
        host_id="0123456789abcdef0123456789abcdef",
        host_name="managed-01234567",
        server_url="https://omnigent.example.com",
        token_secret_arn=SECRET_ARN,
        **kwargs,
    )


def _containers(td):
    return {c["name"]: c for c in td["containerDefinitions"]}


def test_token_reaches_containers_only_by_secret_reference() -> None:
    td = _taskdef(EcsSandboxConfig(**base_config()))
    assert TOKEN not in json.dumps(td)
    for name in ("workspace-prep", "host"):
        refs = {s["name"]: s["valueFrom"] for s in _containers(td)[name]["secrets"]}
        assert refs["OMNIGENT_HOST_TOKEN"] == SECRET_ARN
        assert refs["ANTHROPIC_API_KEY"].startswith("arn:aws:secretsmanager:")


def test_container_chain_without_efs() -> None:
    td = _taskdef(EcsSandboxConfig(**base_config()))
    c = _containers(td)
    assert list(c) == ["home-init", "workspace-prep", "host"]
    assert c["home-init"]["user"] == "0" and not c["home-init"]["essential"]
    assert c["workspace-prep"]["dependsOn"] == [
        {"containerName": "home-init", "condition": "SUCCESS"}
    ]
    assert c["host"]["dependsOn"] == [{"containerName": "workspace-prep", "condition": "SUCCESS"}]
    assert c["host"]["essential"] and c["host"]["user"] != "0"
    assert c["host"]["linuxParameters"]["capabilities"]["drop"] == ["ALL"]
    assert td["volumes"] == [{"name": "home"}]


def test_efs_home_skips_root_init() -> None:
    config = EcsSandboxConfig(
        **base_config(
            efs={"file_system_id": "fs-0123456789abcdef0"},
            task_role_arn="arn:aws:iam::123456789012:role/omnigent-ecs-task",
        )
    )
    td = _taskdef(config, efs_access_point_id=FAKE_ACCESS_POINT)
    assert "home-init" not in _containers(td)
    efs = td["volumes"][0]["efsVolumeConfiguration"]
    assert efs["transitEncryption"] == "ENABLED"
    assert efs["authorizationConfig"] == {
        "accessPointId": FAKE_ACCESS_POINT,
        "iam": "ENABLED",
    }
    assert td["taskRoleArn"].endswith("omnigent-ecs-task")


def test_host_identity_env() -> None:
    env = {
        e["name"]: e["value"]
        for e in _containers(_taskdef(EcsSandboxConfig(**base_config())))["host"]["environment"]
    }
    assert env["OMNIGENT_HOST_ID"] == "0123456789abcdef0123456789abcdef"
    assert env["HOME"] == "/home/omnigent" and env["IS_SANDBOX"] == "1" and env["TZ"] == "UTC"


def test_config_home_must_stay_on_shared_volume() -> None:
    config = EcsSandboxConfig(**base_config(env={"OMNIGENT_CONFIG_HOME": "/etc/omnigent"}))
    with pytest.raises(ValueError, match="must be under"):
        _taskdef(config)


def test_run_task_request() -> None:
    config = EcsSandboxConfig(**base_config())
    req = build_run_task(
        config, sandbox_id="omni-sb-1234abcd", host_id="h", task_definition_arn="td"
    )
    assert req["startedBy"] == "omni-sb-1234abcd" and req["launchType"] == "FARGATE"
    assert req["enableExecuteCommand"] is False
    assert req["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"] == "DISABLED"

    spot = EcsSandboxConfig(**base_config(capacity_provider="FARGATE_SPOT"))
    req = build_run_task(spot, sandbox_id="s", host_id="h", task_definition_arn="td")
    assert "launchType" not in req
    assert req["capacityProviderStrategy"] == [{"capacityProvider": "FARGATE_SPOT", "weight": 1}]


def test_workspace_path() -> None:
    one = [RepoWorkspace(url="https://github.com/o/api.git", branch=None, repo_name="api")]
    assert workspace_path(one) == "/home/omnigent/workspace/api"
    assert workspace_path([]) == "/home/omnigent/workspace"
