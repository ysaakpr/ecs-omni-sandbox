"""The ``sandbox.ecs`` block of the Omnigent server config.

Everything here is an identifier (ARNs, subnet ids, names). Secret VALUES never
appear in this config: harness credentials are referenced by Secrets Manager or
SSM ARN and injected by ECS through the task execution role.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Env names a literal (non-secret) passthrough may not use: the task sets them
# itself, and a duplicate could shadow the host identity or HOME.
RESERVED_ENV_NAMES: frozenset[str] = frozenset(
    {"HOME", "IS_SANDBOX", "OMNIGENT_HOST_ID", "OMNIGENT_HOST_NAME", "OMNIGENT_HOST_TOKEN"}
)

# A literal env name containing one of these ``_``-delimited segments looks like
# a credential. It must go in ``secrets`` (an ARN reference), not ``env`` (which
# is stored in plain text in the task definition).
_SENSITIVE_SEGMENTS: frozenset[str] = frozenset(
    {"TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL", "CREDENTIALS"}
)

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_REF_RE = re.compile(r"^arn:aws[a-z-]*:(secretsmanager|ssm):")
_NAME_PREFIX_RE = re.compile(r"^[A-Za-z0-9/_+=.@-]{1,64}$")
_FAMILY_PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def env_name_is_sensitive(name: str) -> bool:
    """Whether *name* looks like it holds a credential, e.g. ``GIT_TOKEN``."""
    return any(segment in _SENSITIVE_SEGMENTS for segment in name.upper().split("_"))


class EfsConfig(BaseModel):
    """Persistent home directories on EFS, one access point per sandbox.

    :param file_system_id: e.g. ``"fs-0123456789abcdef0"``. The file system
        needs mount targets in every subnet listed in ``subnets``.
    :param root_path: Directory on the file system that parents every
        sandbox's home, e.g. ``"/omnigent-hosts"``.
    """

    model_config = ConfigDict(extra="forbid")

    file_system_id: str = Field(pattern=r"^fs-[0-9a-f]{8,40}$")
    root_path: str = Field(default="/omnigent-hosts", pattern=r"^/[A-Za-z0-9/_.-]*$")


class EcsSandboxConfig(BaseModel):
    """Validated ``sandbox.ecs`` block.

    See ``examples/server-config.yaml`` for a commented example.
    """

    model_config = ConfigDict(extra="forbid")

    cluster: str
    subnets: list[str] = Field(min_length=1)
    security_groups: list[str] = Field(min_length=1)
    execution_role_arn: str
    task_role_arn: str | None = None
    region: str | None = None

    image: str | None = None
    cpu: str = "1024"
    memory: str = "4096"
    cpu_architecture: Literal["ARM64", "X86_64"] = "ARM64"
    ephemeral_storage_gib: int | None = Field(default=None, ge=21, le=200)
    capacity_provider: Literal["FARGATE", "FARGATE_SPOT"] = "FARGATE"
    platform_version: str = "LATEST"
    assign_public_ip: bool = False

    # Env name -> Secrets Manager / SSM parameter ARN, injected by ECS.
    secrets: dict[str, str] = Field(default_factory=dict)
    # Non-sensitive literal env for the host container.
    env: dict[str, str] = Field(default_factory=dict)

    efs: EfsConfig | None = None

    token_secret_prefix: str = "omnigent-ecs/"  # noqa: S105 - a name prefix, not a secret
    token_kms_key_id: str | None = None
    task_family_prefix: str = "omnigent-ecs"
    log_group: str | None = None
    start_timeout_s: int = Field(default=300, ge=30, le=1800)
    tags: dict[str, str] = Field(default_factory=dict)

    @field_validator("env")
    @classmethod
    def _env_is_not_secret(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            if not _ENV_NAME_RE.match(name):
                raise ValueError(f"env name {name!r} is not a valid variable name")
            if name in RESERVED_ENV_NAMES:
                raise ValueError(f"env name {name!r} is reserved; the task sets it itself")
            if env_name_is_sensitive(name):
                raise ValueError(
                    f"env name {name!r} looks like a credential; put it in 'secrets' as an "
                    "ARN reference instead of a literal value"
                )
        return value

    @field_validator("secrets")
    @classmethod
    def _secrets_are_references(cls, value: dict[str, str]) -> dict[str, str]:
        for name, ref in value.items():
            if not _ENV_NAME_RE.match(name):
                raise ValueError(f"secret env name {name!r} is not a valid variable name")
            if name in RESERVED_ENV_NAMES:
                raise ValueError(f"secret env name {name!r} is reserved")
            if not _SECRET_REF_RE.match(ref):
                raise ValueError(
                    f"secrets.{name} must be a Secrets Manager or SSM parameter ARN, "
                    "never a literal value"
                )
        return value

    @field_validator("token_secret_prefix")
    @classmethod
    def _valid_secret_prefix(cls, value: str) -> str:
        if not _NAME_PREFIX_RE.match(value):
            raise ValueError("token_secret_prefix may use letters, digits and /_+=.@- only")
        return value

    @field_validator("task_family_prefix")
    @classmethod
    def _valid_family_prefix(cls, value: str) -> str:
        if not _FAMILY_PREFIX_RE.match(value):
            raise ValueError("task_family_prefix may use letters, digits, - and _ only")
        return value

    @model_validator(mode="after")
    def _no_overlap(self) -> EcsSandboxConfig:
        both = set(self.env) & set(self.secrets)
        if both:
            raise ValueError(f"names set in both env and secrets: {sorted(both)}")
        return self
