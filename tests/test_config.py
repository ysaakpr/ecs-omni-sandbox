from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnigent.community.sandbox.ecs.config import EcsSandboxConfig
from tests.conftest import base_config

EXAMPLE = Path(__file__).parent.parent / "examples" / "server-config.yaml"


def test_example_config_is_valid() -> None:
    raw = yaml.safe_load(EXAMPLE.read_text())
    EcsSandboxConfig(**raw["sandbox"]["ecs"])


@pytest.mark.parametrize("name", ["GIT_TOKEN", "OPENAI_API_KEY", "DB_PASSWORD", "AWS_SECRET"])
def test_credential_like_env_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError, match="looks like a credential"):
        EcsSandboxConfig(**base_config(env={name: "x"}))


def test_keyboard_is_not_a_credential() -> None:
    EcsSandboxConfig(**base_config(env={"KEYBOARD_LAYOUT": "us"}))


def test_literal_secret_values_are_rejected() -> None:
    with pytest.raises(ValidationError, match="never a literal value"):
        EcsSandboxConfig(**base_config(secrets={"ANTHROPIC_API_KEY": "sk-ant-literal"}))


@pytest.mark.parametrize("name", ["HOME", "OMNIGENT_HOST_ID", "OMNIGENT_HOST_TOKEN"])
def test_reserved_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError, match="reserved"):
        EcsSandboxConfig(
            **base_config(
                env={name: "x"} if "TOKEN" not in name else {},
                secrets={name: "arn:aws:secretsmanager:r:123456789012:secret:x"}
                if "TOKEN" in name
                else {},
            )
        )


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        EcsSandboxConfig(**base_config(access_key="AKIA..."))


def test_ephemeral_storage_bounds() -> None:
    with pytest.raises(ValidationError):
        EcsSandboxConfig(**base_config(ephemeral_storage_gib=20))
    EcsSandboxConfig(**base_config(ephemeral_storage_gib=200))
