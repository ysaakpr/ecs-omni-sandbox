"""ECS Fargate launcher for Omnigent server-managed hosts.

Lifecycle, as the Omnigent server drives it:

- ``provision(label)``: only mints a sandbox id. Nothing is created yet, so
  the server can register the launch token against the id first.
- ``start_host(...)``: stores the launch token in Secrets Manager, registers
  a task definition for this sandbox, runs one Fargate task and waits until
  the ``host`` container is running. The host then dials back to the server.
- ``resume(id)``: Fargate tasks can't be restarted. Stops whatever is left
  of the old task; the server then calls ``start_host`` again under the same
  host id. With EFS the home directory (workspace, agent state) survives;
  without it the woken sandbox starts empty.
- ``terminate(id)``: stops the task and deletes the token secrets, the task
  definitions and the EFS access point. Files on EFS are kept (see README).

The launcher keeps no state between calls. Everything is found again from the
sandbox id: tasks by ``startedBy``, secrets by name prefix, task definitions
by family, access points by root path.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from typing import Any, ClassVar

import click
from botocore.exceptions import ClientError

from omnigent.community.sandbox.ecs._omnigent_compat import (
    RUN_AS_GID,
    RUN_AS_UID,
    RepoWorkspace,
    SandboxCapabilities,
    SandboxHostLauncher,
)
from omnigent.community.sandbox.ecs.config import EcsSandboxConfig
from omnigent.community.sandbox.ecs.taskdef import (
    HOST_CONTAINER,
    build_run_task,
    build_task_definition,
    resource_tags,
    task_family,
    workspace_path,
)

_POLL_INTERVAL_S = 3.0
_STOP_REASON = "omnigent: sandbox terminated"
_NOT_FOUND_CODES = frozenset({"ResourceNotFoundException", "AccessPointNotFound"})
_LABEL_RE = re.compile(r"[^a-z0-9-]+")

ClientFactory = Callable[[str], Any]


def new_sandbox_id(label: str) -> str:
    """A short, unique id that is valid as an ECS ``startedBy``, family suffix
    and Secrets Manager path segment, e.g. ``"omni-managed-a1b2-3f9c01d2"``."""
    slug = _LABEL_RE.sub("-", label.lower()).strip("-")[:40].strip("-")
    suffix = uuid.uuid4().hex[:8]
    return f"omni-{slug}-{suffix}" if slug else f"omni-{suffix}"


class EcsSandboxLauncher(SandboxHostLauncher):
    """Runs each Omnigent managed host as one ECS Fargate task."""

    provider: ClassVar[str] = "ecs"
    can_resume: ClassVar[bool] = True
    supports_cli_bootstrap: ClassVar[bool] = False
    supports_managed_launch: ClassVar[bool] = True

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            cli_bootstrap=False,
            managed_launch=True,
            local_port_forward=False,
            resume_stopped=True,
            programmatic_terminate=True,
            multi_repo=True,
            git_clone_options=True,
        )

    def __init__(
        self,
        *,
        config: EcsSandboxConfig,
        client_factory: ClientFactory | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._client_factory = client_factory or self._boto3_factory(config.region)
        self._clients: dict[str, Any] = {}
        self._sleep = sleep
        self._clock = clock

    # ── SandboxLifecycle ─────────────────────────────────────

    def prepare(self) -> None:
        """Fail fast when the cluster is missing or the credentials can't see it."""
        try:
            resp = self._ecs.describe_clusters(clusters=[self._config.cluster])
        except ClientError as exc:
            raise _click_error("describe ECS cluster", self._config.cluster, exc) from exc
        clusters = resp.get("clusters") or []
        if not clusters or clusters[0].get("status") != "ACTIVE":
            raise click.ClickException(
                f"ECS cluster {self._config.cluster!r} was not found or is not ACTIVE"
            )

    def provision(self, name: str) -> str:
        return new_sandbox_id(name)

    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repos: Sequence[RepoWorkspace] = (),
        host_config: dict[str, object] | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        if on_stage is not None:
            on_stage("starting")
        click.echo(f"▸ Starting ECS task for sandbox '{sandbox_id}' on {self._config.cluster}")
        task_arn: str | None = None
        try:
            secret_arn = self._create_token_secret(sandbox_id, host_id, token)
            access_point_id = self._ensure_access_point(sandbox_id) if self._config.efs else None
            taskdef = build_task_definition(
                self._config,
                sandbox_id=sandbox_id,
                host_id=host_id,
                host_name=host_name,
                server_url=server_url,
                token_secret_arn=secret_arn,
                repos=repos,
                host_config=host_config,
                efs_access_point_id=access_point_id,
            )
            taskdef_arn = self._ecs.register_task_definition(**taskdef)["taskDefinition"][
                "taskDefinitionArn"
            ]
            task_arn = self._run_task(sandbox_id, host_id, taskdef_arn)
            self._wait_for_host(sandbox_id, task_arn)
        except BaseException:
            # Leave nothing running or billable behind a failed launch. Storage
            # is kept: a retry under the same id finds it again.
            self._teardown(sandbox_id, delete_storage=False, quiet=True)
            raise
        click.echo(f"  → task {task_arn.rsplit('/', 1)[-1]} is running the host")
        return workspace_path(repos)

    def resume(self, sandbox_id: str) -> None:
        click.echo(f"▸ Resuming ECS sandbox '{sandbox_id}'")
        if self._config.efs is None:
            click.echo(
                "  → warning: no EFS configured; the woken sandbox starts with an empty "
                "home directory (workspace and agent state are lost)"
            )
        self._teardown(sandbox_id, delete_storage=False)

    def terminate(self, sandbox_id: str) -> None:
        click.echo(f"▸ Terminating ECS sandbox '{sandbox_id}'")
        self._teardown(sandbox_id, delete_storage=True)

    def is_running(self, sandbox_id: str) -> bool | None:
        try:
            return bool(list(self._task_arns(sandbox_id)))
        except ClientError:
            return None

    # ── launch steps ─────────────────────────────────────────

    def _create_token_secret(self, sandbox_id: str, host_id: str, token: str) -> str:
        """Store the launch token; ECS injects it via the execution role.

        Each launch gets its own secret name, because a force-deleted secret's
        name can stay reserved for a short while after a resume.
        """
        name = f"{self._secret_prefix(sandbox_id)}{uuid.uuid4().hex[:8]}"
        tags = resource_tags(self._config, sandbox_id=sandbox_id, host_id=host_id)
        kwargs: dict[str, Any] = {
            "Name": name,
            "SecretString": token,
            "Description": f"Omnigent launch token for ECS sandbox {sandbox_id}",
            "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
        }
        if self._config.token_kms_key_id:
            kwargs["KmsKeyId"] = self._config.token_kms_key_id
        try:
            return self._secrets.create_secret(**kwargs)["ARN"]
        except ClientError as exc:
            raise _click_error("create launch-token secret", name, exc) from exc

    def _run_task(self, sandbox_id: str, host_id: str, taskdef_arn: str) -> str:
        request = build_run_task(
            self._config, sandbox_id=sandbox_id, host_id=host_id, task_definition_arn=taskdef_arn
        )
        try:
            resp = self._ecs.run_task(**request)
        except ClientError as exc:
            raise _click_error("run ECS task", sandbox_id, exc) from exc
        tasks = resp.get("tasks") or []
        if not tasks:
            failures = "; ".join(
                f"{f.get('arn', '')} {f.get('reason', '')} {f.get('detail', '')}".strip()
                for f in resp.get("failures") or []
            )
            raise click.ClickException(
                f"ECS did not start a task for sandbox {sandbox_id!r}: {failures or 'no reason'}"
            )
        return tasks[0]["taskArn"]

    def _wait_for_host(self, sandbox_id: str, task_arn: str) -> None:
        """Block until the host container runs; fail fast if the task stops."""
        deadline = self._clock() + self._config.start_timeout_s
        last_status = ""
        while True:
            resp = self._ecs.describe_tasks(cluster=self._config.cluster, tasks=[task_arn])
            tasks = resp.get("tasks") or []
            if tasks:
                task = tasks[0]
                if task.get("lastStatus") == "STOPPED":
                    raise click.ClickException(_stopped_message(sandbox_id, task))
                host = next(
                    (c for c in task.get("containers", []) if c.get("name") == HOST_CONTAINER),
                    None,
                )
                if host is not None and host.get("lastStatus") == "RUNNING":
                    return
                status = task.get("lastStatus", "")
                if status != last_status:
                    click.echo(f"  → task is {status.lower() or 'pending'}")
                    last_status = status
            if self._clock() >= deadline:
                raise click.ClickException(
                    f"ECS sandbox {sandbox_id!r} did not start its host within "
                    f"{self._config.start_timeout_s}s (last status: {last_status or 'unknown'})"
                )
            self._sleep(_POLL_INTERVAL_S)

    # ── EFS ──────────────────────────────────────────────────

    def _access_point_path(self, sandbox_id: str) -> str:
        assert self._config.efs is not None
        return f"{self._config.efs.root_path.rstrip('/')}/{sandbox_id}"

    def _find_access_point(self, sandbox_id: str) -> str | None:
        assert self._config.efs is not None
        path = self._access_point_path(sandbox_id)
        paginator = self._efs.get_paginator("describe_access_points")
        for page in paginator.paginate(FileSystemId=self._config.efs.file_system_id):
            for ap in page.get("AccessPoints", []):
                if ap.get("RootDirectory", {}).get("Path") == path and ap.get("LifeCycleState") in (
                    "available",
                    "creating",
                ):
                    return ap["AccessPointId"]
        return None

    def _ensure_access_point(self, sandbox_id: str) -> str:
        """The sandbox's access point, created on first launch.

        The access point pins every file operation to the sandbox user and
        creates the directory owned by that user, so the home is writable
        without a root init step. Re-creating one on the same path after a
        terminate reattaches the same files.
        """
        assert self._config.efs is not None
        existing = self._find_access_point(sandbox_id)
        if existing is not None:
            return self._wait_access_point(existing)
        tags = resource_tags(self._config, sandbox_id=sandbox_id)
        try:
            ap = self._efs.create_access_point(
                ClientToken=uuid.uuid4().hex,
                FileSystemId=self._config.efs.file_system_id,
                PosixUser={"Uid": RUN_AS_UID, "Gid": RUN_AS_GID},
                RootDirectory={
                    "Path": self._access_point_path(sandbox_id),
                    "CreationInfo": {
                        "OwnerUid": RUN_AS_UID,
                        "OwnerGid": RUN_AS_GID,
                        "Permissions": "0750",
                    },
                },
                Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
            )
        except ClientError as exc:
            raise _click_error("create EFS access point", sandbox_id, exc) from exc
        return self._wait_access_point(ap["AccessPointId"])

    def _wait_access_point(self, access_point_id: str) -> str:
        deadline = self._clock() + 60
        while True:
            aps = self._efs.describe_access_points(AccessPointId=access_point_id)["AccessPoints"]
            if aps and aps[0].get("LifeCycleState") == "available":
                return access_point_id
            if self._clock() >= deadline:
                raise click.ClickException(
                    f"EFS access point {access_point_id} did not become available"
                )
            self._sleep(1.0)

    # ── teardown ─────────────────────────────────────────────

    def _teardown(self, sandbox_id: str, *, delete_storage: bool, quiet: bool = False) -> None:
        """Remove a sandbox's AWS resources. Missing resources count as removed.

        Each step runs even if an earlier one failed, so one stuck resource
        doesn't leave the rest behind. The first error is raised at the end
        (unless *quiet*, used while already handling a launch failure).
        """
        steps: list[tuple[str, Callable[[], None]]] = [
            ("stop tasks", lambda: self._stop_tasks(sandbox_id)),
            ("delete token secrets", lambda: self._delete_token_secrets(sandbox_id)),
            ("remove task definitions", lambda: self._remove_task_definitions(sandbox_id)),
        ]
        if delete_storage and self._config.efs is not None:
            steps.append(("delete EFS access point", lambda: self._delete_access_point(sandbox_id)))
        first_error: click.ClickException | None = None
        for action, step in steps:
            try:
                step()
            except ClientError as exc:
                if _error_code(exc) in _NOT_FOUND_CODES:
                    continue
                error = _click_error(action, sandbox_id, exc)
                click.echo(f"  → warning: {error.message}")
                first_error = first_error or error
        if first_error is not None and not quiet:
            raise first_error

    def _task_arns(self, sandbox_id: str) -> Iterator[str]:
        paginator = self._ecs.get_paginator("list_tasks")
        for page in paginator.paginate(
            cluster=self._config.cluster, startedBy=sandbox_id, desiredStatus="RUNNING"
        ):
            yield from page.get("taskArns", [])

    def _stop_tasks(self, sandbox_id: str) -> None:
        for arn in list(self._task_arns(sandbox_id)):
            self._ecs.stop_task(cluster=self._config.cluster, task=arn, reason=_STOP_REASON)

    def _secret_prefix(self, sandbox_id: str) -> str:
        return f"{self._config.token_secret_prefix}{sandbox_id}/"

    def _delete_token_secrets(self, sandbox_id: str) -> None:
        prefix = self._secret_prefix(sandbox_id)
        paginator = self._secrets.get_paginator("list_secrets")
        arns = [
            s["ARN"]
            for page in paginator.paginate(Filters=[{"Key": "name", "Values": [prefix]}])
            for s in page.get("SecretList", [])
            if s.get("Name", "").startswith(prefix)
        ]
        for arn in arns:
            self._secrets.delete_secret(SecretId=arn, ForceDeleteWithoutRecovery=True)

    def _remove_task_definitions(self, sandbox_id: str) -> None:
        family = task_family(self._config, sandbox_id)

        def revisions(status: str) -> list[str]:
            paginator = self._ecs.get_paginator("list_task_definitions")
            return [
                arn
                for page in paginator.paginate(familyPrefix=family, status=status)
                for arn in page.get("taskDefinitionArns", [])
                # familyPrefix is a prefix match; keep this family only.
                if arn.rsplit("/", 1)[-1].rsplit(":", 1)[0] == family
            ]

        for arn in revisions("ACTIVE"):
            self._ecs.deregister_task_definition(taskDefinition=arn)
        inactive = revisions("INACTIVE")
        for i in range(0, len(inactive), 10):
            self._ecs.delete_task_definitions(taskDefinitions=inactive[i : i + 10])

    def _delete_access_point(self, sandbox_id: str) -> None:
        access_point_id = self._find_access_point(sandbox_id)
        if access_point_id is not None:
            self._efs.delete_access_point(AccessPointId=access_point_id)

    # ── clients ──────────────────────────────────────────────

    @staticmethod
    def _boto3_factory(region: str | None) -> ClientFactory:
        def factory(service: str) -> Any:
            import boto3

            return boto3.session.Session(region_name=region).client(service)

        return factory

    def _client(self, service: str) -> Any:
        if service not in self._clients:
            self._clients[service] = self._client_factory(service)
        return self._clients[service]

    @property
    def _ecs(self) -> Any:
        return self._client("ecs")

    @property
    def _secrets(self) -> Any:
        return self._client("secretsmanager")

    @property
    def _efs(self) -> Any:
        return self._client("efs")


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _click_error(action: str, name: str, exc: ClientError) -> click.ClickException:
    error = exc.response.get("Error", {})
    return click.ClickException(
        f"could not {action} for {name!r}: {error.get('Code', 'Error')}: "
        f"{error.get('Message', str(exc))}"
    )


def _stopped_message(sandbox_id: str, task: dict[str, Any]) -> str:
    """Explain a stopped task: its reason plus each container's exit."""
    parts = [f"ECS sandbox {sandbox_id!r} stopped before the host started"]
    if task.get("stoppedReason"):
        parts.append(f"reason: {task['stoppedReason']}")
    for c in task.get("containers", []):
        detail = c.get("reason") or (
            f"exit code {c['exitCode']}" if c.get("exitCode") is not None else None
        )
        if detail:
            parts.append(f"{c.get('name')}: {detail}")
    return "; ".join(parts)
