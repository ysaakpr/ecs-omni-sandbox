"""Shared fixtures. Every AWS call is mocked; tests never need real credentials."""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from omnigent.community.sandbox.ecs.config import EcsSandboxConfig

REGION = "ap-south-1"
TOKEN = "launch-token-for-tests-0123456789"


def base_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "cluster": "omnigent-hosts",
        "region": REGION,
        "subnets": ["subnet-0123456789abcdef0"],
        "security_groups": ["sg-0123456789abcdef0"],
        "execution_role_arn": "arn:aws:iam::123456789012:role/omnigent-ecs-execution",
        "secrets": {
            "ANTHROPIC_API_KEY": (
                "arn:aws:secretsmanager:ap-south-1:123456789012:secret:omnigent/anthropic-AbCdEf"
            )
        },
        "env": {"TZ": "UTC"},
    }
    config.update(overrides)
    return config


class _Paginator:
    def __init__(self, method: Any) -> None:
        self._method = method

    def paginate(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        yield self._method(**kwargs)


class FakeEcs:
    """Just enough of the ECS API for the launcher, with scriptable task status.

    ``statuses`` is the sequence of (task lastStatus, host container lastStatus)
    pairs that successive ``describe_tasks`` calls return.
    """

    def __init__(self, statuses: list[tuple[str, str]] | None = None) -> None:
        self.statuses = statuses or [("PROVISIONING", "PENDING"), ("RUNNING", "RUNNING")]
        self.stopped_reason: str | None = None
        self.container_exits: list[dict[str, Any]] = []
        self.task_definitions: dict[str, dict[str, Any]] = {}  # arn -> request + status
        self.tasks: dict[str, dict[str, Any]] = {}  # arn -> {startedBy, desired}
        self.run_requests: list[dict[str, Any]] = []
        self.run_failures: list[dict[str, str]] = []
        self.cluster_status = "ACTIVE"
        self._revision = itertools.count(1)
        self._describe = 0

    def get_paginator(self, name: str) -> _Paginator:
        return _Paginator(getattr(self, name))

    def describe_clusters(self, clusters: list[str]) -> dict[str, Any]:
        return {"clusters": [{"clusterName": clusters[0], "status": self.cluster_status}]}

    def register_task_definition(self, **request: Any) -> dict[str, Any]:
        arn = (
            f"arn:aws:ecs:{REGION}:123456789012:task-definition/"
            f"{request['family']}:{next(self._revision)}"
        )
        self.task_definitions[arn] = {**request, "status": "ACTIVE"}
        return {"taskDefinition": {"taskDefinitionArn": arn}}

    def run_task(self, **request: Any) -> dict[str, Any]:
        self.run_requests.append(request)
        if self.run_failures:
            return {"tasks": [], "failures": self.run_failures}
        arn = f"arn:aws:ecs:{REGION}:123456789012:task/omnigent-hosts/{len(self.tasks) + 1:032x}"
        self.tasks[arn] = {"startedBy": request["startedBy"], "desired": "RUNNING"}
        return {"tasks": [{"taskArn": arn}], "failures": []}

    def describe_tasks(self, cluster: str, tasks: list[str]) -> dict[str, Any]:
        task_status, host_status = self.statuses[min(self._describe, len(self.statuses) - 1)]
        self._describe += 1
        task: dict[str, Any] = {
            "taskArn": tasks[0],
            "lastStatus": task_status,
            "containers": [{"name": "host", "lastStatus": host_status}, *self.container_exits],
        }
        if self.stopped_reason:
            task["stoppedReason"] = self.stopped_reason
        return {"tasks": [task]}

    def list_tasks(self, cluster: str, startedBy: str, desiredStatus: str) -> dict[str, Any]:
        arns = [
            arn
            for arn, t in self.tasks.items()
            if t["startedBy"] == startedBy and t["desired"] == desiredStatus
        ]
        return {"taskArns": arns}

    def stop_task(self, cluster: str, task: str, reason: str) -> dict[str, Any]:
        self.tasks[task]["desired"] = "STOPPED"
        return {}

    def list_task_definitions(self, familyPrefix: str, status: str) -> dict[str, Any]:
        arns = [
            arn
            for arn, td in self.task_definitions.items()
            if td["family"].startswith(familyPrefix) and td["status"] == status
        ]
        return {"taskDefinitionArns": arns}

    def deregister_task_definition(self, taskDefinition: str) -> dict[str, Any]:
        self.task_definitions[taskDefinition]["status"] = "INACTIVE"
        return {}

    def delete_task_definitions(self, taskDefinitions: list[str]) -> dict[str, Any]:
        assert len(taskDefinitions) <= 10
        for arn in taskDefinitions:
            if self.task_definitions[arn]["status"] != "INACTIVE":
                raise ClientError(
                    {"Error": {"Code": "ClientException", "Message": "still active"}},
                    "DeleteTaskDefinitions",
                )
            del self.task_definitions[arn]
        return {}

    def running_tasks(self) -> list[str]:
        return [arn for arn, t in self.tasks.items() if t["desired"] == "RUNNING"]


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        yield


@pytest.fixture
def file_system_id(aws: None) -> str:
    return boto3.client("efs", region_name=REGION).create_file_system(CreationToken="tests")[
        "FileSystemId"
    ]


@pytest.fixture
def fake_ecs() -> FakeEcs:
    return FakeEcs()


@pytest.fixture
def make_launcher(aws: None, fake_ecs: FakeEcs):
    """Build a launcher with moto Secrets Manager / EFS and the fake ECS."""
    from omnigent.community.sandbox.ecs.launcher import EcsSandboxLauncher

    def factory(**overrides: Any) -> EcsSandboxLauncher:
        config = EcsSandboxConfig(**base_config(**overrides))

        def client(service: str) -> Any:
            if service == "ecs":
                return fake_ecs
            return boto3.client(service, region_name=REGION)

        return EcsSandboxLauncher(
            config=config, client_factory=client, sleep=lambda _s: None, clock=_ticking_clock()
        )

    return factory


def _ticking_clock():
    """A clock that advances 1s per read, so timeouts are reached without sleeping."""
    ticks = itertools.count()
    return lambda: float(next(ticks))
