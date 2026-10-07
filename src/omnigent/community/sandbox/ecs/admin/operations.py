"""AWS operations behind ``omnigent-ecs``. Each takes its boto3 client(s) as
arguments, so tests can pass moto clients or fakes."""

from __future__ import annotations

import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

import click
import yaml
from botocore.exceptions import ClientError, WaiterError

from omnigent.community.sandbox.ecs.admin.naming import STACK_TAG, Names
from omnigent.community.sandbox.ecs.config import EcsSandboxConfig

_FAILED_SUFFIXES = ("_FAILED",)
_WAITER = {"Delay": 10, "MaxAttempts": 180}  # up to 30 minutes


def template(name: str) -> str:
    """The text of a bundled CloudFormation template (``bootstrap`` or ``stack``)."""
    return (resources.files(__package__) / "templates" / f"{name}.yaml").read_text()


# ── CloudFormation ───────────────────────────────────────────────────


def describe_stack(cfn: Any, stack: str) -> dict[str, Any] | None:
    try:
        return cfn.describe_stacks(StackName=stack)["Stacks"][0]
    except ClientError as exc:
        if "does not exist" in str(exc):
            return None
        raise


def stack_outputs(cfn: Any, stack: str) -> dict[str, str] | None:
    found = describe_stack(cfn, stack)
    if found is None:
        return None
    return {o["OutputKey"]: o["OutputValue"] for o in found.get("Outputs", [])}


def failure_reasons(cfn: Any, stack: str, limit: int = 8) -> list[str]:
    """The most recent resource failures, to explain a failed deploy."""
    try:
        events = cfn.describe_stack_events(StackName=stack)["StackEvents"]
    except ClientError:
        return []
    failed = [
        f"{e['LogicalResourceId']}: {e.get('ResourceStatusReason', e['ResourceStatus'])}"
        for e in events
        if e["ResourceStatus"].endswith(_FAILED_SUFFIXES)
    ]
    return failed[:limit]


def deploy_stack(
    cfn: Any,
    *,
    stack: str,
    template_body: str,
    parameters: dict[str, str],
    role_arn: str | None,
    tags: dict[str, str],
) -> dict[str, str]:
    """Create or update *stack*, wait for it, and return its outputs."""
    common: dict[str, Any] = {
        "StackName": stack,
        "TemplateBody": template_body,
        "Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()],
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
        "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
    }
    if role_arn:
        common["RoleARN"] = role_arn

    existing = describe_stack(cfn, stack)
    if existing is not None and existing["StackStatus"] == "ROLLBACK_COMPLETE":
        raise click.ClickException(
            f"stack {stack} is in ROLLBACK_COMPLETE from an earlier failed create; "
            "run teardown for it first"
        )
    try:
        if existing is None:
            click.echo(f"▸ Creating stack {stack} (this takes a few minutes)")
            # DELETE on failure, so a retry starts clean instead of hitting ROLLBACK_COMPLETE.
            cfn.create_stack(**common, OnFailure="DELETE")
            waiter = "stack_create_complete"
        else:
            click.echo(f"▸ Updating stack {stack}")
            try:
                cfn.update_stack(**common)
            except ClientError as exc:
                if "No updates are to be performed" in str(exc):
                    click.echo("  → already up to date")
                    return stack_outputs(cfn, stack) or {}
                raise
            waiter = "stack_update_complete"
        cfn.get_waiter(waiter).wait(StackName=stack, WaiterConfig=_WAITER)
    except (ClientError, WaiterError) as exc:
        reasons = failure_reasons(cfn, stack)
        detail = "\n  ".join(reasons) if reasons else str(exc)
        raise click.ClickException(f"deploying {stack} failed:\n  {detail}") from exc
    click.echo(f"  → {stack} is ready")
    return stack_outputs(cfn, stack) or {}


def delete_stack(cfn: Any, *, stack: str, role_arn: str | None) -> None:
    if describe_stack(cfn, stack) is None:
        click.echo(f"  → stack {stack} not found, skipping")
        return
    click.echo(f"▸ Deleting stack {stack}")
    kwargs: dict[str, Any] = {"StackName": stack}
    if role_arn:
        kwargs["RoleARN"] = role_arn
    cfn.delete_stack(**kwargs)
    try:
        cfn.get_waiter("stack_delete_complete").wait(StackName=stack, WaiterConfig=_WAITER)
    except WaiterError as exc:
        reasons = failure_reasons(cfn, stack)
        detail = "\n  ".join(reasons) if reasons else str(exc)
        raise click.ClickException(f"deleting {stack} failed:\n  {detail}") from exc
    click.echo(f"  → {stack} deleted")


# ── Networking checks ────────────────────────────────────────────────


def check_subnets(ec2: Any, subnet_ids: list[str], *, assign_public_ip: bool) -> str:
    """Validate the task subnets and return their VPC id.

    Catches the two mistakes that otherwise show up as a task stuck in
    PROVISIONING: subnets in different VPCs or the same AZ (EFS needs one mount
    target per AZ), and subnets with no route to the internet.
    """
    if not 1 <= len(subnet_ids) <= 3:
        raise click.ClickException("pass 1 to 3 subnets, one per availability zone")
    subnets = ec2.describe_subnets(SubnetIds=subnet_ids)["Subnets"]
    vpcs = {s["VpcId"] for s in subnets}
    if len(vpcs) != 1:
        raise click.ClickException(f"subnets must be in one VPC, got {sorted(vpcs)}")
    azs = [s["AvailabilityZone"] for s in subnets]
    if len(set(azs)) != len(azs):
        raise click.ClickException("use one subnet per availability zone (EFS mount targets)")
    vpc_id = vpcs.pop()
    for subnet_id in subnet_ids:
        target = _default_route_target(ec2, vpc_id, subnet_id)
        if target is None:
            raise click.ClickException(
                f"{subnet_id} has no default route; tasks couldn't reach the server or ghcr.io"
            )
        if target.startswith("igw-") and not assign_public_ip:
            raise click.ClickException(
                f"{subnet_id} is a public subnet (routes to {target}). Use private subnets "
                "with a NAT gateway, or pass --assign-public-ip"
            )
    return vpc_id


def _default_route_target(ec2: Any, vpc_id: str, subnet_id: str) -> str | None:
    tables = ec2.describe_route_tables(
        Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}]
    )["RouteTables"]
    if not tables:  # falls back to the VPC's main route table
        tables = ec2.describe_route_tables(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "association.main", "Values": ["true"]},
            ]
        )["RouteTables"]
    for table in tables:
        for route in table.get("Routes", []):
            if route.get("DestinationCidrBlock") == "0.0.0.0/0":
                return (
                    route.get("NatGatewayId")
                    or route.get("GatewayId")
                    or route.get("TransitGatewayId")
                    or route.get("NetworkInterfaceId")
                    or "other"
                )
    return None


# ── Secrets ──────────────────────────────────────────────────────────


def ensure_secret(
    sm: Any,
    *,
    name: str,
    value: Callable[[], str],
    tags: dict[str, str],
    rotate: bool,
) -> str:
    """Return the secret's ARN, creating it (or rotating it) when needed.

    *value* is only called when a value is actually needed, so an existing
    secret never triggers a prompt.
    """
    try:
        found = sm.describe_secret(SecretId=name)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        found = None
    if found is not None and found.get("DeletedDate"):
        raise click.ClickException(
            f"secret {name} is scheduled for deletion; restore it or wait for the deletion"
        )
    if found is not None and not rotate:
        click.echo(f"  → using existing secret {name}")
        return found["ARN"]
    if found is not None:
        sm.put_secret_value(SecretId=name, SecretString=value())
        click.echo(f"  → rotated secret {name}")
        return found["ARN"]
    arn = sm.create_secret(
        Name=name,
        SecretString=value(),
        Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
    )["ARN"]
    click.echo(f"  → created secret {name}")
    return arn


def delete_secrets(sm: Any, prefixes: list[str]) -> int:
    deleted = 0
    for prefix in prefixes:
        paginator = sm.get_paginator("list_secrets")
        for page in paginator.paginate(Filters=[{"Key": "name", "Values": [prefix]}]):
            for secret in page.get("SecretList", []):
                if secret["Name"].startswith(prefix):
                    sm.delete_secret(SecretId=secret["ARN"], ForceDeleteWithoutRecovery=True)
                    deleted += 1
    return deleted


# ── Teardown sweeps (resources created at runtime, outside the stack) ─


def stop_cluster_tasks(ecs: Any, cluster: str) -> int:
    arns: list[str] = []
    paginator = ecs.get_paginator("list_tasks")
    try:
        for page in paginator.paginate(cluster=cluster, desiredStatus="RUNNING"):
            arns.extend(page.get("taskArns", []))
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ClusterNotFoundException":
            return 0
        raise
    for arn in arns:
        ecs.stop_task(cluster=cluster, task=arn, reason="omnigent-ecs teardown")
    # Task network interfaces sit in the stack's security group; wait for them.
    for i in range(0, len(arns), 100):
        ecs.get_waiter("tasks_stopped").wait(
            cluster=cluster, tasks=arns[i : i + 100], WaiterConfig={"Delay": 6, "MaxAttempts": 100}
        )
    return len(arns)


def remove_task_definitions(ecs: Any, family_prefix: str) -> int:
    """Deregister and delete every revision of this deployment's task families."""
    families: list[str] = []
    paginator = ecs.get_paginator("list_task_definition_families")
    for page in paginator.paginate(familyPrefix=f"{family_prefix}-", status="ALL"):
        families.extend(page.get("families", []))

    def revisions(family: str, status: str) -> list[str]:
        return [
            arn
            for page in ecs.get_paginator("list_task_definitions").paginate(
                familyPrefix=family, status=status
            )
            for arn in page.get("taskDefinitionArns", [])
            # familyPrefix is a prefix match; keep this family only.
            if arn.rsplit("/", 1)[-1].rsplit(":", 1)[0] == family
        ]

    removed = 0
    for family in families:
        for arn in revisions(family, "ACTIVE"):
            ecs.deregister_task_definition(taskDefinition=arn)
        inactive = revisions(family, "INACTIVE")
        for i in range(0, len(inactive), 10):
            ecs.delete_task_definitions(taskDefinitions=inactive[i : i + 10])
        removed += len(inactive)
    return removed


def delete_access_points(efs: Any, file_system_id: str, stack: str) -> int:
    deleted = 0
    paginator = efs.get_paginator("describe_access_points")
    for page in paginator.paginate(FileSystemId=file_system_id):
        for ap in page.get("AccessPoints", []):
            tags = {t["Key"]: t["Value"] for t in ap.get("Tags", [])}
            if tags.get(STACK_TAG) == stack:
                efs.delete_access_point(AccessPointId=ap["AccessPointId"])
                deleted += 1
    return deleted


def delete_cached_images(ecr: Any, prefix: str) -> int:
    deleted = 0
    paginator = ecr.get_paginator("describe_repositories")
    for page in paginator.paginate():
        for repo in page.get("repositories", []):
            if repo["repositoryName"].startswith(f"{prefix}/"):
                ecr.delete_repository(repositoryName=repo["repositoryName"], force=True)
                deleted += 1
    return deleted


def delete_file_system(efs: Any, file_system_id: str, stack: str) -> bool:
    """Delete a file system only if this deployment created it (by tag)."""
    try:
        fs = efs.describe_file_systems(FileSystemId=file_system_id)["FileSystems"][0]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "FileSystemNotFound":
            return False
        raise
    tags = {t["Key"]: t["Value"] for t in fs.get("Tags", [])}
    if tags.get(STACK_TAG) != stack:
        click.echo(f"  → {file_system_id} wasn't created by {stack}; leaving it alone")
        return False
    for _ in range(60):  # mount targets go with the stack; wait until they're gone
        if not efs.describe_mount_targets(FileSystemId=file_system_id)["MountTargets"]:
            break
        time.sleep(5)
    efs.delete_file_system(FileSystemId=file_system_id)
    return True


# ── Omnigent server config ───────────────────────────────────────────


def build_sandbox_section(
    names: Names,
    outputs: dict[str, str],
    *,
    region: str,
    server_url: str,
    subnets: list[str],
    harness_secrets: dict[str, str],
    env: dict[str, str] | None = None,
    image_tag: str,
    cpu: str,
    memory: str,
    cpu_architecture: str,
    capacity_provider: str,
    assign_public_ip: bool,
    idle_timeout_s: int | None,
    idle_stop_after_s: int = 900,
) -> dict[str, Any]:
    """The server config's ``sandbox:`` section for this deployment, validated."""
    ecs: dict[str, Any] = {
        "cluster": outputs["ClusterName"],
        "region": region,
        "subnets": subnets,
        "security_groups": [outputs["TaskSecurityGroupId"]],
        "assign_public_ip": assign_public_ip,
        "execution_role_arn": outputs["ExecutionRoleArn"],
        "task_role_arn": outputs["TaskRoleArn"],
        "image": f"{outputs['ImageRepositoryPrefix']}/omnigent-ai/omnigent-host:{image_tag}",
        "cpu": cpu,
        "memory": memory,
        "cpu_architecture": cpu_architecture,
        "capacity_provider": capacity_provider,
        "secrets": harness_secrets,
        "env": with_runner_passthrough(env or {}, harness_secrets),
        "log_group": outputs["LogGroupName"],
        "idle_stop_after_s": idle_stop_after_s,
        "token_secret_prefix": names.token_secret_prefix,
        "task_family_prefix": names.task_family_prefix,
        "tags": {STACK_TAG: names.stack},
    }
    if outputs.get("FileSystemId"):
        ecs["efs"] = {"file_system_id": outputs["FileSystemId"], "root_path": "/omnigent-hosts"}
    EcsSandboxConfig(**ecs)  # fail here, not at the first launch

    section: dict[str, Any] = {
        "provider": "ecs",
        "server_url": server_url,
        "reaper": {"enabled": True, "terminate_after_offline_days": 7},
        "ecs": ecs,
    }
    if idle_timeout_s:
        section["host_config"] = {"runner": {"idle_timeout_s": idle_timeout_s}}
    return section


PASSTHROUGH_ENV = "OMNIGENT_RUNNER_ENV_PASSTHROUGH"


def with_runner_passthrough(env: dict[str, str], secrets: dict[str, str]) -> dict[str, str]:
    """Add every configured name the host wouldn't forward to the agent by itself.

    ``omnigent host`` only passes an allowlist of credential env vars (e.g.
    ``GIT_TOKEN``, ``ANTHROPIC_API_KEY``) on to the agent's runner. Anything
    else the operator configures, such as ``GH_TOKEN`` for the gh CLI or
    ``GIT_AUTHOR_NAME``, has to be named in ``OMNIGENT_RUNNER_ENV_PASSTHROUGH``.
    """
    from omnigent.host.connect import HARNESS_CREDENTIAL_ENV_VARS

    wanted = {n for n in [*env, *secrets] if n != PASSTHROUGH_ENV} - HARNESS_CREDENTIAL_ENV_VARS
    existing = {n.strip() for n in env.get(PASSTHROUGH_ENV, "").split(",") if n.strip()}
    merged = dict(env)
    if wanted | existing:
        merged[PASSTHROUGH_ENV] = ",".join(sorted(wanted | existing))
    return merged


def write_server_config(path: Path, section: dict[str, Any], *, force: bool) -> Path | None:
    """Set ``sandbox:`` in the server config, keeping everything else.

    Returns the backup path when an existing file was changed.
    """
    raw: dict[str, Any] = {}
    backup: Path | None = None
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
        current = raw.get("sandbox")
        if current and not force and current.get("provider") != "ecs":
            raise click.ClickException(
                f"{path} already has a sandbox section for provider "
                f"{current.get('provider')!r}; pass --force to replace it"
            )
        backup = path.with_name(f"{path.name}.bak-{int(time.time())}")
        backup.write_text(path.read_text())
    raw["sandbox"] = section
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return backup


def remove_from_server_config(path: Path, stack: str) -> bool:
    if not path.exists():
        return False
    raw = yaml.safe_load(path.read_text()) or {}
    current = raw.get("sandbox") or {}
    if current.get("ecs", {}).get("tags", {}).get(STACK_TAG) != stack:
        return False
    backup = path.with_name(f"{path.name}.bak-{int(time.time())}")
    backup.write_text(path.read_text())
    del raw["sandbox"]
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return True
