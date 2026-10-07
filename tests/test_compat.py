"""Fails loudly when an Omnigent upgrade moves an internal this provider reuses."""

from __future__ import annotations

import inspect

from omnigent.community.sandbox.ecs import _omnigent_compat as compat


def test_reused_renderers_keep_their_shape() -> None:
    prep = compat.render_workspace_prep_command("/home/omnigent/workspace", (), "https://s", "h1")
    host = compat.render_host_command("https://s")
    assert prep[:2] == ["bash", "-lc"] and "mkdir -p /home/omnigent/workspace" in prep[2]
    assert host[:2] == ["bash", "-lc"] and "omnigent host --server https://s" in host[2]


def test_start_host_signature_matches_the_base_class() -> None:
    from omnigent.community.sandbox.ecs.launcher import EcsSandboxLauncher

    base = inspect.signature(compat.SandboxHostLauncher.start_host).parameters
    ours = inspect.signature(EcsSandboxLauncher.start_host).parameters
    assert list(base) == list(ours)


def test_constants() -> None:
    assert compat.HOME_DIR.startswith("/")
    assert compat.HOST_TOKEN_ENV_VAR == "OMNIGENT_HOST_TOKEN"
    assert isinstance(compat.RUN_AS_UID, int) and compat.RUN_AS_UID != 0
