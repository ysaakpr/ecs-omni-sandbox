from __future__ import annotations

import json

import boto3
import click
import pytest

from omnigent.community.sandbox.ecs.launcher import new_sandbox_id
from tests.conftest import REGION, TOKEN, FakeEcs

HOST_ID = "0123456789abcdef0123456789abcdef"


def _start(launcher, sandbox_id: str) -> str:
    return launcher.start_host(
        sandbox_id,
        token=TOKEN,
        host_id=HOST_ID,
        host_name="managed-01234567",
        server_url="https://omnigent.example.com",
    )


def _secret_names() -> list[str]:
    return [
        s["Name"]
        for s in boto3.client("secretsmanager", region_name=REGION).list_secrets()["SecretList"]
    ]


def _access_points(fs: str) -> list[dict]:
    return boto3.client("efs", region_name=REGION).describe_access_points(FileSystemId=fs)[
        "AccessPoints"
    ]


def test_sandbox_ids_are_safe_and_unique() -> None:
    a, b = new_sandbox_id("Managed A1B2!"), new_sandbox_id("Managed A1B2!")
    assert a != b and a.startswith("omni-managed-a1b2-")
    assert new_sandbox_id("").startswith("omni-") and len(new_sandbox_id("x" * 200)) <= 60


def test_start_host_happy_path(make_launcher, fake_ecs: FakeEcs) -> None:
    launcher = make_launcher()
    sid = launcher.provision("managed-01234567")
    assert _start(launcher, sid) == "/home/omnigent/workspace"

    # Token stored once, under the sandbox's prefix, and nowhere in ECS requests.
    secrets = boto3.client("secretsmanager", region_name=REGION)
    [name] = _secret_names()
    assert name.startswith(f"omnigent-ecs/{sid}/")
    assert secrets.get_secret_value(SecretId=name)["SecretString"] == TOKEN
    ecs_traffic = json.dumps([fake_ecs.task_definitions, fake_ecs.run_requests])
    assert TOKEN not in ecs_traffic

    assert len(fake_ecs.running_tasks()) == 1
    assert launcher.is_running(sid) is True


def test_terminate_removes_everything(make_launcher, fake_ecs: FakeEcs) -> None:
    launcher = make_launcher()
    sid = launcher.provision("m")
    _start(launcher, sid)
    launcher.terminate(sid)
    assert fake_ecs.running_tasks() == []
    assert _secret_names() == []
    assert fake_ecs.task_definitions == {}
    assert launcher.is_running(sid) is False


def test_terminate_of_unknown_sandbox_is_a_no_op(make_launcher) -> None:
    make_launcher().terminate("omni-never-existed-00000000")


def test_failed_start_cleans_up(make_launcher, fake_ecs: FakeEcs) -> None:
    fake_ecs.statuses = [("PROVISIONING", "PENDING"), ("STOPPED", "STOPPED")]
    fake_ecs.stopped_reason = "Essential container in task exited"
    fake_ecs.container_exits = [{"name": "workspace-prep", "exitCode": 128}]
    launcher = make_launcher()
    sid = launcher.provision("m")
    with pytest.raises(click.ClickException) as err:
        _start(launcher, sid)
    assert "workspace-prep: exit code 128" in err.value.message
    assert "Essential container" in err.value.message
    assert fake_ecs.running_tasks() == [] and _secret_names() == []
    assert TOKEN not in err.value.message


def test_start_times_out(make_launcher, fake_ecs: FakeEcs) -> None:
    fake_ecs.statuses = [("PROVISIONING", "PENDING")]
    launcher = make_launcher(start_timeout_s=30)
    sid = launcher.provision("m")
    with pytest.raises(click.ClickException, match="did not start its host within 30s"):
        _start(launcher, sid)
    assert fake_ecs.running_tasks() == []


def test_run_task_failure_is_reported(make_launcher, fake_ecs: FakeEcs) -> None:
    fake_ecs.run_failures = [{"arn": "", "reason": "RESOURCE:ENI", "detail": "no capacity"}]
    launcher = make_launcher()
    with pytest.raises(click.ClickException, match="RESOURCE:ENI"):
        _start(launcher, launcher.provision("m"))
    assert _secret_names() == []


def test_prepare_checks_cluster(make_launcher, fake_ecs: FakeEcs) -> None:
    make_launcher().prepare()
    fake_ecs.cluster_status = "INACTIVE"
    with pytest.raises(click.ClickException, match="not ACTIVE"):
        make_launcher().prepare()


def test_efs_home_survives_resume_and_is_released_on_terminate(
    make_launcher, fake_ecs: FakeEcs, file_system_id: str
) -> None:
    launcher = make_launcher(efs={"file_system_id": file_system_id, "root_path": "/hosts"})
    sid = launcher.provision("m")
    _start(launcher, sid)
    [ap] = _access_points(file_system_id)
    assert ap["RootDirectory"]["Path"] == f"/hosts/{sid}"
    assert ap["PosixUser"]["Uid"] != 0

    # Wake: the old task goes, the access point (and so the files) stays,
    # and the relaunch mounts the same one.
    launcher.resume(sid)
    assert fake_ecs.running_tasks() == []
    assert [a["AccessPointId"] for a in _access_points(file_system_id)] == [ap["AccessPointId"]]
    fake_ecs._describe = 0
    _start(launcher, sid)
    [td] = fake_ecs.task_definitions.values()
    volume = td["volumes"][0]["efsVolumeConfiguration"]
    assert volume["authorizationConfig"]["accessPointId"] == ap["AccessPointId"]
    assert len(_secret_names()) == 1  # the old launch's secret was removed

    launcher.terminate(sid)
    assert _access_points(file_system_id) == []
