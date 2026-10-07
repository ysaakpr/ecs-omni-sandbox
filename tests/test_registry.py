"""The provider plugs into Omnigent the way the server loads it."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnigent.onboarding.sandboxes import registry

from tests.conftest import base_config


@pytest.fixture(autouse=True)
def _fresh_registry():
    registry.reset_plugin_state_for_tests()
    yield
    registry.reset_plugin_state_for_tests()


def test_entry_point_registers_ecs() -> None:
    assert registry.plugin_state().load_errors == {}
    meta = registry.get_provider_metadata("ecs")
    assert meta is not None and meta.managed_token_ttl_s == 7 * 24 * 3600


def test_registry_instantiates_launcher_with_validated_config() -> None:
    from omnigent.community.sandbox.ecs.launcher import EcsSandboxLauncher

    launcher = registry.instantiate("ecs", config=base_config())
    assert isinstance(launcher, EcsSandboxLauncher)
    assert launcher.capabilities.managed_launch and launcher.capabilities.resume_stopped


def test_server_accepts_example_sandbox_section() -> None:
    from omnigent.server.managed_hosts import parse_sandbox_config

    raw = yaml.safe_load((Path(__file__).parent.parent / "examples/server-config.yaml").read_text())
    parsed = parse_sandbox_config(raw["sandbox"])
    assert parsed is not None
