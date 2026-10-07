"""Tests for `omnigent-ecs` (bootstrap/setup/status/teardown).

AWS is mocked with moto, except CloudFormation, which moto can't run these
templates on; a small fake stands in for it and returns realistic outputs.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import boto3
import click
import pytest
import yaml
from botocore.exceptions import ClientError
from click.testing import CliRunner

from omnigent.community.sandbox.ecs.admin import operations as ops
from omnigent.community.sandbox.ecs.admin.cli import main
from omnigent.community.sandbox.ecs.admin.naming import STACK_TAG, Names
from tests.conftest import REGION

GHCR_TOKEN = "ghcr-token-for-tests-0123456789"
API_KEY = "llm-api-key-for-tests-0123456789"  # gitleaks:allow (fake test value)
ACCOUNT = "123456789012"


# ── fixtures ─────────────────────────────────────────────────────────


class FakeCfn:
    """Records stack calls and returns the outputs the real stack would."""

    def __init__(self) -> None:
        self.stacks: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_with: str | None = None

    def describe_stacks(self, StackName: str) -> dict[str, Any]:
        if StackName not in self.stacks:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationError",
                        "Message": f"Stack {StackName} does not exist",
                    }
                },
                "DescribeStacks",
            )
        return {"Stacks": [self.stacks[StackName]]}

    def describe_stack_events(self, StackName: str) -> dict[str, Any]:
        reason = self.fail_with or "ok"
        return {
            "StackEvents": [
                {
                    "LogicalResourceId": "ImageCache",
                    "ResourceStatus": "CREATE_FAILED",
                    "ResourceStatusReason": reason,
                }
            ]
        }

    def create_stack(self, **kwargs: Any) -> None:
        self.calls.append(("create", kwargs))
        params = {p["ParameterKey"]: p["ParameterValue"] for p in kwargs["Parameters"]}
        self.stacks[kwargs["StackName"]] = {
            "StackStatus": "CREATE_COMPLETE",
            "Outputs": self._outputs(kwargs["StackName"], params),
        }

    def update_stack(self, **kwargs: Any) -> None:
        self.calls.append(("update", kwargs))
        raise ClientError(
            {"Error": {"Code": "ValidationError", "Message": "No updates are to be performed."}},
            "UpdateStack",
        )

    def delete_stack(self, **kwargs: Any) -> None:
        self.calls.append(("delete", kwargs))
        self.stacks.pop(kwargs["StackName"], None)

    def get_waiter(self, name: str) -> Any:
        fake = self

        class _Waiter:
            def wait(self, **_kwargs: Any) -> None:
                if fake.fail_with:
                    from botocore.exceptions import WaiterError

                    raise WaiterError(name, "failed", {})

        return _Waiter()

    @staticmethod
    def _outputs(stack: str, params: dict[str, str]) -> list[dict[str, str]]:
        if stack == "omni-ecs-bootstrap":
            values = {
                "CfnServiceRoleArn": f"arn:aws:iam::{ACCOUNT}:role/omni-ecs-cfn",
                "BoundaryArn": f"arn:aws:iam::{ACCOUNT}:policy/omni-ecs-boundary",
            }
        else:
            values = {
                "ClusterName": stack,
                "TaskSecurityGroupId": "sg-0123456789abcdef0",
                "ExecutionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/{stack}-exec",
                "TaskRoleArn": f"arn:aws:iam::{ACCOUNT}:role/{stack}-task",
                "LogGroupName": f"/omni-ecs/{stack}",
                "ImageRepositoryPrefix": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{stack}",
            }
            if params.get("EnableEfs") == "true":
                values["FileSystemId"] = params.get("ExistingFileSystemId") or _efs_for(stack)
        return [{"OutputKey": k, "OutputValue": v} for k, v in values.items()]


def _efs_for(stack: str) -> str:
    efs = boto3.client("efs", region_name=REGION)
    return efs.create_file_system(CreationToken=stack, Tags=[{"Key": STACK_TAG, "Value": stack}])[
        "FileSystemId"
    ]


@pytest.fixture
def network(aws: None) -> dict[str, str]:
    """A VPC with a private subnet (default route via NAT) and a public one."""
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    private = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone=f"{REGION}a")
    private_b = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.2.0/24", AvailabilityZone=f"{REGION}b")
    public = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.3.0/24", AvailabilityZone=f"{REGION}a")
    igw = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc)
    nat = ec2.create_nat_gateway(SubnetId=public["Subnet"]["SubnetId"])["NatGateway"]
    private_rt = ec2.create_route_table(VpcId=vpc)["RouteTable"]["RouteTableId"]
    public_rt = ec2.create_route_table(VpcId=vpc)["RouteTable"]["RouteTableId"]
    ec2.create_route(
        RouteTableId=private_rt, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat["NatGatewayId"]
    )
    ec2.create_route(RouteTableId=public_rt, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw)
    for subnet in (private, private_b):
        ec2.associate_route_table(RouteTableId=private_rt, SubnetId=subnet["Subnet"]["SubnetId"])
    ec2.associate_route_table(RouteTableId=public_rt, SubnetId=public["Subnet"]["SubnetId"])
    return {
        "vpc": vpc,
        "private": private["Subnet"]["SubnetId"],
        "private_b": private_b["Subnet"]["SubnetId"],
        "public": public["Subnet"]["SubnetId"],
    }


@pytest.fixture
def fake_cfn() -> FakeCfn:
    return FakeCfn()


@pytest.fixture
def run(aws: None, fake_cfn: FakeCfn, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OMNI_ECS_GHCR_USERNAME", "octocat")
    monkeypatch.setenv("OMNI_ECS_GHCR_TOKEN", GHCR_TOKEN)
    monkeypatch.setenv("OMNI_ECS_SECRET_ANTHROPIC_API_KEY", API_KEY)

    def client(service: str) -> Any:
        if service == "cloudformation":
            return fake_cfn
        return boto3.client(service, region_name=REGION)

    def invoke(*args: str) -> Any:
        result = CliRunner().invoke(
            main, list(args), obj={"client_factory": client}, catch_exceptions=False
        )
        return result

    return invoke


# ── templates and naming ─────────────────────────────────────────────


def test_templates_pass_cfn_lint() -> None:
    templates = Path(ops.__file__).parent / "templates"
    lint = Path(sys.executable).parent / "cfn-lint"
    result = subprocess.run(  # noqa: S603 - fixed local paths
        [str(lint), *map(str, sorted(templates.glob("*.yaml")))], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_names_match_the_bootstrap_policy_prefixes() -> None:
    names = Names("prod")
    bootstrap = ops.template("bootstrap")
    assert names.stack.startswith("omni-ecs-") and "cluster/omni-ecs-*" in bootstrap
    assert (
        names.token_secret_prefix.startswith("omni-ecs/")
        and "secret:omni-ecs/*/tokens/*" in bootstrap
    )
    assert names.ghcr_secret.startswith("ecr-pullthroughcache/omni-ecs-")
    assert len(names.stack) <= 30  # ECR pull-through prefix limit
    assert len(Names("a" * 21).stack) <= 30


@pytest.mark.parametrize("bad", ["", "a", "Prod", "1prod", "pr_od", "a" * 22, "bootstrap", "prod-"])
def test_bad_names_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        Names(bad)


# ── operations ───────────────────────────────────────────────────────


def test_check_subnets(network: dict[str, str]) -> None:
    ec2 = boto3.client("ec2", region_name=REGION)
    assert ops.check_subnets(ec2, [network["private"]], assign_public_ip=False) == network["vpc"]
    assert (
        ops.check_subnets(ec2, [network["private"], network["private_b"]], assign_public_ip=False)
        == network["vpc"]
    )
    with pytest.raises(click.ClickException, match="public subnet"):
        ops.check_subnets(ec2, [network["public"]], assign_public_ip=False)
    ops.check_subnets(ec2, [network["public"]], assign_public_ip=True)
    with pytest.raises(click.ClickException, match="one subnet per availability zone"):
        ops.check_subnets(ec2, [network["private"], network["public"]], assign_public_ip=True)


def test_ensure_secret_only_asks_when_needed(aws: None) -> None:
    sm = boto3.client("secretsmanager", region_name=REGION)
    asked: list[int] = []

    def value() -> str:
        asked.append(1)
        return "v1"

    arn = ops.ensure_secret(sm, name="omni-ecs/omni-ecs-t/x", value=value, tags={}, rotate=False)
    assert (
        ops.ensure_secret(sm, name="omni-ecs/omni-ecs-t/x", value=value, tags={}, rotate=False)
        == arn
    )
    assert len(asked) == 1
    ops.ensure_secret(sm, name="omni-ecs/omni-ecs-t/x", value=lambda: "v2", tags={}, rotate=True)
    assert sm.get_secret_value(SecretId=arn)["SecretString"] == "v2"


def test_teardown_sweeps_only_touch_this_deployment(aws: None) -> None:
    ecs = boto3.client("ecs", region_name=REGION)
    for family in ("omni-ecs-t-omni-a", "omni-ecs-t-omni-b", "omni-ecs-tx-omni-c", "other"):
        ecs.register_task_definition(
            family=family, containerDefinitions=[{"name": "c", "image": "i", "memory": 16}]
        )
    assert ops.remove_task_definitions(ecs, "omni-ecs-t") == 2
    active = ecs.list_task_definitions(status="ACTIVE")["taskDefinitionArns"]
    assert sorted(a.rsplit("/", 1)[-1] for a in active) == ["omni-ecs-tx-omni-c:1", "other:1"]

    ecr = boto3.client("ecr", region_name=REGION)
    for repo in ("omni-ecs-t/omnigent-ai/omnigent-host", "omni-ecs-tx/omnigent-ai/omnigent-host"):
        ecr.create_repository(repositoryName=repo)
    assert ops.delete_cached_images(ecr, "omni-ecs-t") == 1

    sm = boto3.client("secretsmanager", region_name=REGION)
    for name in ("omni-ecs/omni-ecs-t/tokens/a", "omni-ecs/omni-ecs-tx/tokens/b", "unrelated"):
        sm.create_secret(Name=name, SecretString="x")
    assert ops.delete_secrets(sm, ["omni-ecs/omni-ecs-t/"]) == 1

    efs = boto3.client("efs", region_name=REGION)
    ours = efs.create_file_system(
        CreationToken="a", Tags=[{"Key": STACK_TAG, "Value": "omni-ecs-t"}]
    )
    theirs = efs.create_file_system(CreationToken="b")
    assert ops.delete_file_system(efs, theirs["FileSystemId"], "omni-ecs-t") is False
    assert ops.delete_file_system(efs, ours["FileSystemId"], "omni-ecs-t") is True


def test_server_config_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"other": {"keep": True}}))
    section = {"provider": "ecs", "ecs": {"tags": {STACK_TAG: "omni-ecs-t"}}}
    assert ops.write_server_config(path, section, force=False) is not None
    assert yaml.safe_load(path.read_text()) == {"other": {"keep": True}, "sandbox": section}
    assert ops.remove_from_server_config(path, "omni-ecs-other") is False
    assert ops.remove_from_server_config(path, "omni-ecs-t") is True
    assert yaml.safe_load(path.read_text()) == {"other": {"keep": True}}


def test_server_config_wont_replace_another_provider(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"sandbox": {"provider": "modal"}}))
    with pytest.raises(click.ClickException, match="--force"):
        ops.write_server_config(path, {"provider": "ecs"}, force=False)


# ── CLI end to end ───────────────────────────────────────────────────


def _setup(run, network: dict[str, str], *extra: str) -> Any:
    return run(
        "setup",
        "--name", "dev",
        "--server-url", "https://omnigent.example.com",
        "--subnets", f"{network['private']},{network['private_b']}",
        "--harness-secret", "ANTHROPIC_API_KEY",
        "--region", REGION,
        "--yes",
        *extra,
    )  # fmt: skip


def test_setup_creates_secrets_stack_and_config(
    run, network: dict[str, str], fake_cfn: FakeCfn, tmp_path: Path
) -> None:
    config = tmp_path / "config.yaml"
    result = _setup(run, network, "--write-config", str(config))
    assert result.exit_code == 0, result.output

    # Secrets live in Secrets Manager and nowhere else.
    sm = boto3.client("secretsmanager", region_name=REGION)
    assert (
        sm.get_secret_value(SecretId="omni-ecs/omni-ecs-dev/harness/ANTHROPIC_API_KEY")[
            "SecretString"
        ]
        == API_KEY
    )
    assert (
        GHCR_TOKEN
        in sm.get_secret_value(SecretId="ecr-pullthroughcache/omni-ecs-dev")["SecretString"]
    )
    everything_else = result.output + config.read_text() + repr(fake_cfn.calls)
    assert GHCR_TOKEN not in everything_else and API_KEY not in everything_else

    [(kind, call)] = fake_cfn.calls
    params = {p["ParameterKey"]: p["ParameterValue"] for p in call["Parameters"]}
    assert kind == "create" and call["RoleARN"].endswith(":role/omni-ecs-cfn")
    assert params["Subnet2"] == network["private_b"] and params["Subnet3"] == ""
    assert params["BoundaryArn"].endswith(":policy/omni-ecs-boundary")

    section = yaml.safe_load(config.read_text())["sandbox"]
    assert section["provider"] == "ecs"
    ecs = section["ecs"]
    assert ecs["cluster"] == "omni-ecs-dev" and ecs["efs"]["file_system_id"].startswith("fs-")
    assert ecs["image"].endswith("/omni-ecs-dev/omnigent-ai/omnigent-host:v0.17.0")
    assert ecs["secrets"]["ANTHROPIC_API_KEY"].startswith("arn:aws:secretsmanager:")
    assert ecs["token_secret_prefix"] == "omni-ecs/omni-ecs-dev/tokens/"
    assert ecs["idle_stop_after_s"] == 900

    # Re-running is safe: secrets reused, stack update is a no-op.
    again = _setup(run, network, "--write-config", str(config))
    assert again.exit_code == 0, again.output
    assert "using existing secret" in again.output and "already up to date" in again.output


def test_setup_reports_stack_failures(run, network: dict[str, str], fake_cfn: FakeCfn) -> None:
    fake_cfn.fail_with = "Resource handler returned message: credential secret not found"
    result = CliRunner().invoke(
        main,
        [
            "setup",
            "--name",
            "dev",
            "--server-url",
            "https://omnigent.example.com",
            "--subnets",
            network["private"],
            "--region",
            REGION,
            "--yes",
        ],  # fmt: skip
        obj={
            "client_factory": lambda s: (
                fake_cfn if s == "cloudformation" else boto3.client(s, region_name=REGION)
            )
        },
    )
    assert result.exit_code != 0
    assert "ImageCache: Resource handler returned message" in result.output


def test_teardown_removes_everything_but_keeps_data(
    run, network: dict[str, str], fake_cfn: FakeCfn, tmp_path: Path
) -> None:
    config = tmp_path / "config.yaml"
    assert _setup(run, network, "--write-config", str(config)).exit_code == 0
    fs_id = yaml.safe_load(config.read_text())["sandbox"]["ecs"]["efs"]["file_system_id"]
    boto3.client("ecr", region_name=REGION).create_repository(
        repositoryName="omni-ecs-dev/omnigent-ai/omnigent-host"
    )
    boto3.client("ecs", region_name=REGION).register_task_definition(
        family="omni-ecs-dev-omni-x",
        containerDefinitions=[{"name": "c", "image": "i", "memory": 16}],
    )

    result = run(
        "teardown", "--name", "dev", "--region", REGION, "--yes", "--write-config", str(config)
    )
    assert result.exit_code == 0, result.output
    assert "omni-ecs-dev" not in fake_cfn.stacks
    assert boto3.client("secretsmanager", region_name=REGION).list_secrets()["SecretList"] == []
    assert boto3.client("ecr", region_name=REGION).describe_repositories()["repositories"] == []
    assert "sandbox" not in (yaml.safe_load(config.read_text()) or {})
    # Data kept by default.
    efs = boto3.client("efs", region_name=REGION)
    assert efs.describe_file_systems(FileSystemId=fs_id)["FileSystems"]
    assert f"--existing-efs {fs_id}" in result.output


def test_teardown_requires_typed_confirmation(
    run, network: dict[str, str], fake_cfn: FakeCfn
) -> None:
    assert _setup(run, network).exit_code == 0
    result = CliRunner().invoke(
        main,
        ["teardown", "--name", "dev", "--region", REGION],
        input="wrong\n",
        obj={
            "client_factory": lambda s: (
                fake_cfn if s == "cloudformation" else boto3.client(s, region_name=REGION)
            )
        },
    )
    assert result.exit_code != 0
    assert "omni-ecs-dev" in fake_cfn.stacks


def test_bootstrap(run, fake_cfn: FakeCfn) -> None:
    result = run("bootstrap", "--server-role", "omnigent-server", "--region", REGION)
    assert result.exit_code == 0, result.output
    [(kind, call)] = fake_cfn.calls
    assert kind == "create" and call["StackName"] == "omni-ecs-bootstrap" and "RoleARN" not in call
    assert "omni-ecs-cfn" in result.output


def test_setup_aliases_gh_token_and_passes_extra_env(
    run, network: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNI_ECS_SECRET_GIT_TOKEN", "git-token-for-tests")  # gitleaks:allow
    config = tmp_path / "config.yaml"
    result = _setup(
        run,
        network,
        "--harness-secret", "GIT_TOKEN",
        "--harness-secret", "GH_TOKEN=GIT_TOKEN",
        "--env", "GIT_AUTHOR_NAME=Omni Agent",
        "--write-config", str(config),
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    ecs = yaml.safe_load(config.read_text())["sandbox"]["ecs"]
    assert ecs["secrets"]["GH_TOKEN"] == ecs["secrets"]["GIT_TOKEN"]
    assert ecs["env"]["GIT_AUTHOR_NAME"] == "Omni Agent"
    # Only names the host doesn't already forward to the agent.
    assert ecs["env"]["OMNIGENT_RUNNER_ENV_PASSTHROUGH"] == "GH_TOKEN,GIT_AUTHOR_NAME"
    # The alias stored nothing new.
    names = [
        s["Name"]
        for s in boto3.client("secretsmanager", region_name=REGION).list_secrets()["SecretList"]
    ]
    assert not any(n.endswith("/GH_TOKEN") for n in names)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--harness-secret", "GH_TOKEN=GIT_TOKEN"], "also pass --harness-secret GIT_TOKEN"),
        (["--env", "NOVALUE"], "expected NAME=VALUE"),
        (["--env", "GIT_TOKEN=literal"], "looks like a credential"),
    ],
)
def test_setup_rejects_bad_secret_and_env_options(
    run, network: dict[str, str], args: list[str], message: str
) -> None:
    result = CliRunner().invoke(
        main,
        [
            "setup",
            "--name",
            "dev",
            "--server-url",
            "https://omnigent.example.com",
            "--subnets",
            network["private"],
            "--region",
            REGION,
            "--yes",
            *args,
        ],  # fmt: skip
        obj={"client_factory": lambda s: boto3.client(s, region_name=REGION)},
    )
    assert result.exit_code != 0
    assert message in result.output
