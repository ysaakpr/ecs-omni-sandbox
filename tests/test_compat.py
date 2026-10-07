"""Fails loudly when an Omnigent upgrade moves an internal this provider reuses."""

from __future__ import annotations

import inspect

from omnigent.community.sandbox.ecs import _omnigent_compat as compat


def test_reused_renderer_keeps_its_shape() -> None:
    prep = compat.render_workspace_prep_command("/home/omnigent/workspace", (), "https://s", "h1")
    assert prep[:2] == ["bash", "-lc"] and "mkdir -p /home/omnigent/workspace" in prep[2]


def test_runner_process_names_the_supervisor_looks_for_still_exist() -> None:
    # supervisor.py spots runners by these module names in their cmdline.
    import importlib.util

    from omnigent.community.sandbox.ecs import supervisor

    for module in (supervisor.RUNNER_ENTRY, supervisor.ZYGOTE):
        assert importlib.util.find_spec(module) is not None, module


def test_start_host_signature_matches_the_base_class() -> None:
    from omnigent.community.sandbox.ecs.launcher import EcsSandboxLauncher

    base = inspect.signature(compat.SandboxHostLauncher.start_host).parameters
    ours = inspect.signature(EcsSandboxLauncher.start_host).parameters
    assert list(base) == list(ours)


def test_constants() -> None:
    assert compat.HOME_DIR.startswith("/")
    assert compat.HOST_TOKEN_ENV_VAR == "OMNIGENT_HOST_TOKEN"
    assert isinstance(compat.RUN_AS_UID, int) and compat.RUN_AS_UID != 0
