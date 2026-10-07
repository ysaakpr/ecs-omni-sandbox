"""``omnigent-ecs``: create and delete the AWS side of the ECS sandbox provider.

    omnigent-ecs bootstrap   once per account, by an admin: roles and policies
    omnigent-ecs setup       create or update one deployment's infrastructure
    omnigent-ecs status      show a deployment and anything it left running
    omnigent-ecs teardown    delete a deployment and everything it created

Secrets are read from a hidden prompt (or an environment variable for
automation) and go straight to Secrets Manager. They're never printed, never
written to the server config, and never passed to CloudFormation.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from getpass import getpass
from pathlib import Path
from typing import Any

import click
import yaml

from omnigent.community.sandbox.ecs.admin import operations as ops
from omnigent.community.sandbox.ecs.admin.naming import (
    BOOTSTRAP_STACK,
    BOUNDARY_POLICY,
    CFN_SERVICE_ROLE,
    STACK_TAG,
    Names,
)

ClientFactory = Callable[[str], Any]


class _Aws:
    """Lazily built boto3 clients for one region; replaceable in tests."""

    def __init__(self, region: str | None, factory: ClientFactory | None = None) -> None:
        self._factory = factory
        self._region = region
        self._session: Any = None
        self._clients: dict[str, Any] = {}

    def client(self, service: str) -> Any:
        if service not in self._clients:
            if self._factory is not None:
                self._clients[service] = self._factory(service)
            else:
                if self._session is None:
                    import boto3

                    self._session = boto3.session.Session(region_name=self._region)
                self._clients[service] = self._session.client(service)
        return self._clients[service]

    @property
    def region(self) -> str:
        if self._region:
            return self._region
        region = self.client("ecs").meta.region_name
        if not region:
            raise click.ClickException("no AWS region set; pass --region or set AWS_REGION")
        return region

    def identity(self) -> tuple[str, str]:
        """(account id, partition) of the caller."""
        arn = self.client("sts").get_caller_identity()["Arn"]
        parts = arn.split(":")
        return parts[4], parts[1]


def _aws(ctx: click.Context, region: str | None) -> _Aws:
    factory = (ctx.obj or {}).get("client_factory")
    return _Aws(region, factory)


def _secret_value(label: str, env_var: str) -> Callable[[], str]:
    """Read a secret from *env_var*, else a hidden prompt. Never echoed."""

    def read() -> str:
        value = os.environ.get(env_var)
        if value:
            return value
        if not os.isatty(0):
            raise click.ClickException(f"{label} is required: set {env_var} or run interactively")
        value = getpass(f"{label} (input hidden): ")
        if not value:
            raise click.ClickException(f"{label} can't be empty")
        return value

    return read


def _default_image_tag() -> str:
    from omnigent.version import VERSION

    return f"v{VERSION}"


region_option = click.option("--region", help="AWS region. Defaults to your AWS config.")
name_option = click.option(
    "--name",
    required=True,
    help="Short deployment name, e.g. 'prod'. Resources are named omni-ecs-<name>.",
)


@click.group()
def main() -> None:
    """Set up and tear down AWS infrastructure for Omnigent ECS sandboxes."""


# ── bootstrap ────────────────────────────────────────────────────────


@main.command()
@click.option("--server-role", help="IAM role NAME the Omnigent server runs as.")
@click.option("--operator-role", default="", help="IAM role NAME that runs setup/teardown.")
@click.option("--print-template", is_flag=True, help="Print the template for review and exit.")
@click.option("--delete", is_flag=True, help="Delete the bootstrap stack.")
@region_option
@click.pass_context
def bootstrap(
    ctx: click.Context,
    server_role: str | None,
    operator_role: str,
    print_template: bool,
    delete: bool,
    region: str | None,
) -> None:
    """Create the roles and policies setup needs. Run once, as an admin.

    This is the only step that needs IAM admin rights. Review the template
    first with --print-template, or hand it to whoever administers your AWS
    account to deploy themselves.
    """
    if print_template:
        click.echo(ops.template("bootstrap"))
        return
    aws = _aws(ctx, region)
    cfn = aws.client("cloudformation")
    if delete:
        click.confirm(f"Delete {BOOTSTRAP_STACK} and its roles and policies?", abort=True)
        ops.delete_stack(cfn, stack=BOOTSTRAP_STACK, role_arn=None)
        return
    if not server_role:
        raise click.UsageError("--server-role is required")
    outputs = ops.deploy_stack(
        cfn,
        stack=BOOTSTRAP_STACK,
        template_body=ops.template("bootstrap"),
        parameters={"ServerRoleName": server_role, "OperatorRoleName": operator_role},
        role_arn=None,
        tags={"managed-by": "omnigent-ecs"},
    )
    click.echo(f"\nCloudFormation role: {outputs.get('CfnServiceRoleArn')}")
    click.echo(f"Permissions boundary: {outputs.get('BoundaryArn')}")
    click.echo(f"Runtime policy attached to: {server_role}")
    click.echo(f"Operator policy attached to: {operator_role or server_role}")


# ── setup ────────────────────────────────────────────────────────────


@main.command()
@name_option
@click.option("--server-url", required=True, help="Public URL sandboxes dial back to.")
@click.option(
    "--subnets", required=True, help="1-3 comma-separated subnet ids, one per AZ (private + NAT)."
)
@click.option("--assign-public-ip", is_flag=True, help="Give tasks public IPs (public subnets).")
@click.option("--efs/--no-efs", default=True, show_default=True, help="Persistent homes on EFS.")
@click.option("--existing-efs", default="", help="Reuse a file system kept by a past teardown.")
@click.option("--spot", is_flag=True, help="Use Fargate Spot (cheaper, can be interrupted).")
@click.option("--cpu", default="1024", show_default=True, help="Task CPU units.")
@click.option("--memory", default="4096", show_default=True, help="Task memory in MiB.")
@click.option("--arch", type=click.Choice(["ARM64", "X86_64"]), default="ARM64", show_default=True)
@click.option("--image-tag", default=None, help="Host image tag. Defaults to this Omnigent's.")
@click.option(
    "--harness-secret",
    "harness_secrets",
    multiple=True,
    metavar="ENV_NAME",
    help="Env var to give sandboxes from Secrets Manager, e.g. ANTHROPIC_API_KEY. Repeatable. "
    "You're prompted for the value (or set OMNI_ECS_SECRET_<ENV_NAME>).",
)
@click.option("--rotate-secrets", is_flag=True, help="Prompt again for existing secrets.")
@click.option(
    "--idle-timeout", default=3600, show_default=True, help="Seconds before an idle runner exits."
)
@click.option("--log-retention-days", default=30, show_default=True)
@click.option(
    "--write-config",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Server config file to update. Without it, the section is printed.",
)
@click.option("--force", is_flag=True, help="Replace another provider's sandbox section.")
@click.option("--yes", is_flag=True, help="Don't ask for confirmation.")
@region_option
@click.pass_context
def setup(
    ctx: click.Context,
    name: str,
    server_url: str,
    subnets: str,
    assign_public_ip: bool,
    efs: bool,
    existing_efs: str,
    spot: bool,
    cpu: str,
    memory: str,
    arch: str,
    image_tag: str | None,
    harness_secrets: tuple[str, ...],
    rotate_secrets: bool,
    idle_timeout: int,
    log_retention_days: int,
    write_config: Path | None,
    force: bool,
    yes: bool,
    region: str | None,
) -> None:
    """Create or update a deployment. Safe to re-run."""
    names = _names(name)
    aws = _aws(ctx, region)
    account, partition = aws.identity()
    subnet_ids = [s.strip() for s in subnets.split(",") if s.strip()]
    vpc_id = ops.check_subnets(aws.client("ec2"), subnet_ids, assign_public_ip=assign_public_ip)
    image_tag = image_tag or _default_image_tag()

    click.echo(f"Deployment {names.stack} in {aws.region} (account {account})")
    click.echo(f"  VPC {vpc_id}, subnets {', '.join(subnet_ids)}")
    click.echo(f"  {'Fargate Spot' if spot else 'Fargate'} {arch}, {cpu} CPU / {memory} MiB")
    click.echo(f"  EFS: {'existing ' + existing_efs if existing_efs else 'new' if efs else 'off'}")
    click.echo(f"  Image: ghcr.io/omnigent-ai/omnigent-host:{image_tag} via ECR cache")
    if not yes:
        click.confirm("Continue?", abort=True)

    sm = aws.client("secretsmanager")
    tags = {STACK_TAG: names.stack, "managed-by": "omnigent-ecs"}
    click.echo("▸ Secrets")
    ghcr_user = _secret_value("GitHub username for ghcr.io", "OMNI_ECS_GHCR_USERNAME")
    ghcr_token = _secret_value(
        "GitHub token with only read:packages, for ghcr.io", "OMNI_ECS_GHCR_TOKEN"
    )
    ghcr_arn = ops.ensure_secret(
        sm,
        name=names.ghcr_secret,
        value=lambda: _json({"username": ghcr_user(), "accessToken": ghcr_token()}),
        tags=tags,
        rotate=rotate_secrets,
    )
    harness_arns = {
        env: ops.ensure_secret(
            sm,
            name=names.harness_secret(env),
            value=_secret_value(env, f"OMNI_ECS_SECRET_{env}"),
            tags=tags,
            rotate=rotate_secrets,
        )
        for env in harness_secrets
    }

    cfn_role = f"arn:{partition}:iam::{account}:role/{CFN_SERVICE_ROLE}"
    boundary = f"arn:{partition}:iam::{account}:policy/{BOUNDARY_POLICY}"
    padded = [*subnet_ids, "", ""][:3]
    outputs = ops.deploy_stack(
        aws.client("cloudformation"),
        stack=names.stack,
        template_body=ops.template("stack"),
        parameters={
            "StackLabel": names.stack,
            "VpcId": vpc_id,
            "Subnet1": padded[0],
            "Subnet2": padded[1],
            "Subnet3": padded[2],
            "EnableEfs": "true" if (efs or existing_efs) else "false",
            "ExistingFileSystemId": existing_efs,
            "GhcrCredentialArn": ghcr_arn,
            "BoundaryArn": boundary,
            "LogRetentionDays": str(log_retention_days),
        },
        role_arn=cfn_role,
        tags=tags,
    )

    section = ops.build_sandbox_section(
        names,
        outputs,
        region=aws.region,
        server_url=server_url,
        subnets=subnet_ids,
        harness_secrets=harness_arns,
        image_tag=image_tag,
        cpu=cpu,
        memory=memory,
        cpu_architecture=arch,
        capacity_provider="FARGATE_SPOT" if spot else "FARGATE",
        assign_public_ip=assign_public_ip,
        idle_timeout_s=idle_timeout,
    )
    if write_config is not None:
        backup = ops.write_server_config(write_config, section, force=force)
        click.echo(f"\n✓ Wrote the sandbox section to {write_config}")
        if backup:
            click.echo(f"  (previous version saved as {backup.name})")
    else:
        click.echo("\nAdd this to the Omnigent server config:\n")
        click.echo(yaml.safe_dump({"sandbox": section}, sort_keys=False))
    click.echo(
        "Next: make sure the server image includes omnigent-ecs-sandbox, then restart the "
        "server and create a session with a managed host."
    )


# ── status ───────────────────────────────────────────────────────────


@main.command()
@name_option
@region_option
@click.pass_context
def status(ctx: click.Context, name: str, region: str | None) -> None:
    """Show a deployment's stack and the sandboxes it's running."""
    names = _names(name)
    aws = _aws(ctx, region)
    stack = ops.describe_stack(aws.client("cloudformation"), names.stack)
    if stack is None:
        click.echo(f"{names.stack}: not deployed")
    else:
        click.echo(f"{names.stack}: {stack['StackStatus']}")
        for out in stack.get("Outputs", []):
            click.echo(f"  {out['OutputKey']}: {out['OutputValue']}")
    ecs = aws.client("ecs")
    try:
        running = ecs.list_tasks(cluster=names.stack, desiredStatus="RUNNING")["taskArns"]
        click.echo(f"Running sandboxes: {len(running)}")
    except Exception:  # noqa: BLE001 - the cluster may not exist
        click.echo("Running sandboxes: cluster not found")
    families = ecs.list_task_definition_families(
        familyPrefix=f"{names.task_family_prefix}-", status="ACTIVE"
    )["families"]
    click.echo(f"Sandbox task definitions: {len(families)}")
    # The name filter matches loosely, so check the prefix ourselves.
    tokens = [
        secret
        for secret in aws.client("secretsmanager").list_secrets(
            Filters=[{"Key": "name", "Values": [names.token_secret_prefix]}]
        )["SecretList"]
        if secret["Name"].startswith(names.token_secret_prefix)
    ]
    click.echo(f"Launch-token secrets: {len(tokens)}")


# ── teardown ─────────────────────────────────────────────────────────


@main.command()
@name_option
@click.option(
    "--delete-data",
    is_flag=True,
    help="Also delete the EFS file system with every sandbox's files. Irreversible.",
)
@click.option(
    "--write-config",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Server config file to remove the sandbox section from.",
)
@click.option("--yes", is_flag=True, help="Don't ask for confirmation.")
@region_option
@click.pass_context
def teardown(
    ctx: click.Context,
    name: str,
    delete_data: bool,
    write_config: Path | None,
    yes: bool,
    region: str | None,
) -> None:
    """Delete a deployment and everything it created, in a safe order."""
    names = _names(name)
    aws = _aws(ctx, region)
    account, partition = aws.identity()
    cfn = aws.client("cloudformation")
    outputs = ops.stack_outputs(cfn, names.stack) or {}
    file_system_id = outputs.get("FileSystemId")

    click.echo(f"This stops every sandbox in {names.stack} and deletes its AWS resources.")
    if file_system_id:
        click.echo(
            f"EFS {file_system_id}: "
            + ("DELETED with all files." if delete_data else "kept (pass --delete-data to delete).")
        )
    if not yes:
        typed = click.prompt(f"Type {names.stack} to confirm", default="", show_default=False)
        if typed != names.stack:
            raise click.Abort()

    ecs = aws.client("ecs")
    click.echo("▸ Stopping sandboxes")
    click.echo(f"  → stopped {ops.stop_cluster_tasks(ecs, names.stack)} task(s)")
    click.echo("▸ Removing task definitions")
    click.echo(f"  → removed {ops.remove_task_definitions(ecs, names.task_family_prefix)}")
    if file_system_id:
        click.echo("▸ Removing EFS access points")
        removed = ops.delete_access_points(aws.client("efs"), file_system_id, names.stack)
        click.echo(f"  → removed {removed}")
    click.echo("▸ Deleting cached images")
    click.echo(f"  → deleted {ops.delete_cached_images(aws.client('ecr'), names.stack)} repo(s)")

    cfn_role = f"arn:{partition}:iam::{account}:role/{CFN_SERVICE_ROLE}"
    ops.delete_stack(cfn, stack=names.stack, role_arn=cfn_role)

    if file_system_id and delete_data:
        click.echo(f"▸ Deleting file system {file_system_id}")
        if ops.delete_file_system(aws.client("efs"), file_system_id, names.stack):
            click.echo("  → deleted")
    elif file_system_id:
        click.echo(
            f"  → kept {file_system_id}. Reuse it with `setup --existing-efs {file_system_id}`."
        )

    # Last, so a failed stack delete can be retried with its secrets in place.
    click.echo("▸ Deleting secrets")
    count = ops.delete_secrets(
        aws.client("secretsmanager"), [names.secret_prefix, names.ghcr_secret]
    )
    click.echo(f"  → deleted {count}")

    if write_config is not None and ops.remove_from_server_config(write_config, names.stack):
        click.echo(f"▸ Removed the sandbox section from {write_config}")
    click.echo(f"\n✓ {names.stack} is gone. Restart the Omnigent server to drop the provider.")


def _names(name: str) -> Names:
    try:
        return Names(name)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--name") from exc


def _json(value: dict[str, str]) -> str:
    import json

    return json.dumps(value)


if __name__ == "__main__":
    main()
