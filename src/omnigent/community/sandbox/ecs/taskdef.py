"""Pure builders for the ECS API requests a launch makes. No AWS calls here.

One sandbox = one task definition family (``<prefix>-<sandbox_id>``) and one
running task. The task runs up to three containers that share a ``home`` volume:

1. ``home-init`` (root, only without EFS): makes the fresh task-local volume
   writable by the non-root sandbox user. With EFS the access point already
   owns the directory, so this step is skipped.
2. ``workspace-prep`` (sandbox user): Omnigent's own init script. Clones the
   requested repos and writes the host config, then exits.
3. ``host`` (sandbox user, essential): ``omnigent host`` under a PID-1 reaper.
   It dials back to the server and authenticates with the launch token.

The launch token and harness credentials reach the containers through the task
definition's ``secrets`` list. ECS resolves them with the task EXECUTION role,
whose credentials are never exposed inside the containers. The token value
never appears in a task definition, a RunTask request, or CloudTrail.
"""

from __future__ import annotations

import posixpath
import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from omnigent.community.sandbox.ecs import supervisor
from omnigent.community.sandbox.ecs._omnigent_compat import (
    DEFAULT_HOST_IMAGE,
    HOME_DIR,
    HOST_ID_ENV_VAR,
    HOST_NAME_ENV_VAR,
    HOST_TOKEN_ENV_VAR,
    RUN_AS_GID,
    RUN_AS_UID,
    RepoWorkspace,
    render_workspace_prep_command,
)
from omnigent.community.sandbox.ecs.config import EcsSandboxConfig

HOME_INIT_CONTAINER = "home-init"
PREP_CONTAINER = "workspace-prep"
HOST_CONTAINER = "host"

MANAGED_BY_TAG = ("managed-by", "omnigent-ecs-sandbox")
SANDBOX_ID_TAG = "omnigent:sandbox-id"
HOST_ID_TAG = "omnigent:host-id"

WORKSPACE_DIR = f"{HOME_DIR}/workspace"


def host_command(server_url: str) -> list[str]:
    """``omnigent host`` under the PID-1 supervisor (reaping + idle stop).

    The supervisor's source is passed to ``python3 -c`` because the host image
    doesn't have this package. ``bash -lc`` + ``exec`` puts the image's venv on
    PATH and makes the supervisor PID 1.
    """
    source = Path(supervisor.__file__).read_text()
    return [
        "bash",
        "-lc",
        f"exec python3 -c {shlex.quote(source)} omnigent host --server {shlex.quote(server_url)}",
    ]


def task_family(config: EcsSandboxConfig, sandbox_id: str) -> str:
    """The task definition family owned by one sandbox."""
    return f"{config.task_family_prefix}-{sandbox_id}"


def workspace_path(repos: Sequence[RepoWorkspace]) -> str:
    """The directory the agent starts in, matching the Kubernetes provider."""
    return f"{WORKSPACE_DIR}/{repos[0].repo_name}" if len(repos) == 1 else WORKSPACE_DIR


def resource_tags(
    config: EcsSandboxConfig, *, sandbox_id: str, host_id: str | None = None
) -> dict[str, str]:
    """Tags stamped on every AWS resource a launch creates, for cleanup and cost."""
    tags = dict(config.tags)
    tags[MANAGED_BY_TAG[0]] = MANAGED_BY_TAG[1]
    tags[SANDBOX_ID_TAG] = sandbox_id
    if host_id is not None:
        tags[HOST_ID_TAG] = host_id
    return tags


def ecs_tags(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"key": k, "value": v} for k, v in tags.items()]


def _config_home(config: EcsSandboxConfig) -> str | None:
    """``OMNIGENT_CONFIG_HOME`` from the literal env, checked to stay under HOME.

    The prep container writes the host config there, so it must be on the
    shared volume or the host would never see it.
    """
    value = config.env.get("OMNIGENT_CONFIG_HOME")
    if value is None:
        return None
    normalized = posixpath.normpath(value)
    if not (normalized == HOME_DIR or normalized.startswith(HOME_DIR + "/")):
        raise ValueError(f"OMNIGENT_CONFIG_HOME ({value!r}) must be under {HOME_DIR!r}")
    return value


def build_task_definition(
    config: EcsSandboxConfig,
    *,
    sandbox_id: str,
    host_id: str,
    host_name: str,
    server_url: str,
    token_secret_arn: str,
    repos: Sequence[RepoWorkspace] = (),
    host_config: dict[str, object] | None = None,
    efs_access_point_id: str | None = None,
) -> dict[str, Any]:
    """Keyword arguments for ``ecs.register_task_definition``."""
    image = config.image or DEFAULT_HOST_IMAGE
    run_as = f"{RUN_AS_UID}:{RUN_AS_GID}"
    home_mount = [{"sourceVolume": "home", "containerPath": HOME_DIR, "readOnly": False}]
    token_secret = {"name": HOST_TOKEN_ENV_VAR, "valueFrom": token_secret_arn}
    harness_secrets = [{"name": n, "valueFrom": ref} for n, ref in config.secrets.items()]
    hardened: dict[str, Any] = {"capabilities": {"drop": ["ALL"]}}

    containers: list[dict[str, Any]] = []
    if efs_access_point_id is None:
        containers.append(
            {
                "name": HOME_INIT_CONTAINER,
                "image": image,
                "essential": False,
                "user": "0",
                "entryPoint": ["sh", "-c"],
                "command": [f"chown {run_as} {HOME_DIR} && chmod 0750 {HOME_DIR}"],
                "mountPoints": home_mount,
                # Keeps CHOWN/FOWNER for the one command it runs. Fargate can't
                # add capabilities back, so "drop ALL" isn't an option here.
                "linuxParameters": {"capabilities": {"drop": ["NET_RAW"]}},
            }
        )

    prep_env = [{"name": "HOME", "value": HOME_DIR}]
    config_home = _config_home(config)
    if config_home is not None:
        prep_env.append({"name": "OMNIGENT_CONFIG_HOME", "value": config_home})
    prep: dict[str, Any] = {
        "name": PREP_CONTAINER,
        "image": image,
        "essential": False,
        "user": run_as,
        # The image's WORKDIR is /root, unreadable to the sandbox user, and
        # `omnigent host` reads ./.omnigent/config.yaml from its cwd at startup.
        "workingDirectory": HOME_DIR,
        "entryPoint": [],
        "command": render_workspace_prep_command(
            WORKSPACE_DIR, repos, server_url, host_id, host_config
        ),
        "environment": prep_env,
        "secrets": [token_secret, *harness_secrets],
        "mountPoints": home_mount,
        "linuxParameters": hardened,
    }
    if efs_access_point_id is None:
        prep["dependsOn"] = [{"containerName": HOME_INIT_CONTAINER, "condition": "SUCCESS"}]
    containers.append(prep)

    host_env = [
        {"name": "HOME", "value": HOME_DIR},
        {"name": "IS_SANDBOX", "value": "1"},
        {"name": HOST_ID_ENV_VAR, "value": host_id},
        {"name": HOST_NAME_ENV_VAR, "value": host_name},
        {"name": "OMNI_ECS_IDLE_STOP_AFTER_S", "value": str(config.idle_stop_after_s)},
        {"name": "OMNI_ECS_IDLE_CPU_THRESHOLD", "value": str(config.idle_cpu_threshold)},
        *({"name": n, "value": v} for n, v in config.env.items()),
    ]
    containers.append(
        {
            "name": HOST_CONTAINER,
            "image": image,
            "essential": True,
            "user": run_as,
            "workingDirectory": HOME_DIR,
            "entryPoint": [],
            "command": host_command(server_url),
            "environment": host_env,
            "secrets": [token_secret, *harness_secrets],
            "mountPoints": home_mount,
            "linuxParameters": hardened,
            "dependsOn": [{"containerName": PREP_CONTAINER, "condition": "SUCCESS"}],
        }
    )

    if config.log_group:
        for container in containers:
            container["logConfiguration"] = {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": config.log_group,
                    "awslogs-stream-prefix": sandbox_id,
                    **({"awslogs-region": config.region} if config.region else {}),
                },
            }

    volume: dict[str, Any] = {"name": "home"}
    if efs_access_point_id is not None:
        assert config.efs is not None
        volume["efsVolumeConfiguration"] = {
            "fileSystemId": config.efs.file_system_id,
            "transitEncryption": "ENABLED",
            "authorizationConfig": {
                "accessPointId": efs_access_point_id,
                # IAM auth needs a task role with elasticfilesystem:Client* rights.
                "iam": "ENABLED" if config.task_role_arn else "DISABLED",
            },
        }

    request: dict[str, Any] = {
        "family": task_family(config, sandbox_id),
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": config.cpu,
        "memory": config.memory,
        "runtimePlatform": {
            "cpuArchitecture": config.cpu_architecture,
            "operatingSystemFamily": "LINUX",
        },
        "executionRoleArn": config.execution_role_arn,
        "containerDefinitions": containers,
        "volumes": [volume],
        "tags": ecs_tags(resource_tags(config, sandbox_id=sandbox_id, host_id=host_id)),
    }
    if config.task_role_arn:
        request["taskRoleArn"] = config.task_role_arn
    if config.ephemeral_storage_gib is not None:
        request["ephemeralStorage"] = {"sizeInGiB": config.ephemeral_storage_gib}
    return request


def build_run_task(
    config: EcsSandboxConfig,
    *,
    sandbox_id: str,
    host_id: str,
    task_definition_arn: str,
) -> dict[str, Any]:
    """Keyword arguments for ``ecs.run_task``.

    ``startedBy`` carries the sandbox id so the launcher can find the task again
    with ``list_tasks`` after a server restart, without storing any state.
    """
    request: dict[str, Any] = {
        "cluster": config.cluster,
        "taskDefinition": task_definition_arn,
        "count": 1,
        "startedBy": sandbox_id,
        "platformVersion": config.platform_version,
        "enableExecuteCommand": False,
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": list(config.subnets),
                "securityGroups": list(config.security_groups),
                "assignPublicIp": "ENABLED" if config.assign_public_ip else "DISABLED",
            }
        },
        "tags": ecs_tags(resource_tags(config, sandbox_id=sandbox_id, host_id=host_id)),
    }
    if config.capacity_provider == "FARGATE":
        request["launchType"] = "FARGATE"
    else:
        request["capacityProviderStrategy"] = [
            {"capacityProvider": config.capacity_provider, "weight": 1}
        ]
    return request
