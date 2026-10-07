"""AWS ECS Fargate sandbox provider for Omnigent server-managed hosts.

Omnigent discovers this package through the ``omnigent.sandbox_providers``
entry point and calls :func:`contribution` at server startup. Keep this module
light: it must not import boto3 or the launcher, so a broken AWS install can't
stop the server from starting.
"""

from __future__ import annotations

from omnigent.community.sandbox.ecs._omnigent_compat import (
    SandboxProviderContribution,
    SandboxProviderMetadata,
)
from omnigent.community.sandbox.ecs.config import EcsSandboxConfig

PROVIDER_NAME = "ecs"

# How long a launch token stays valid. A Fargate task has no lifetime cap, and
# a wake re-arms a fresh token, so this matches Daytona's 7 days.
MANAGED_TOKEN_TTL_S = 7 * 24 * 3600


def contribution() -> SandboxProviderContribution:
    """The provider registration Omnigent loads from the entry point."""
    return SandboxProviderContribution(
        name="omnigent-ecs-sandbox",
        providers={
            PROVIDER_NAME: SandboxProviderMetadata(
                name=PROVIDER_NAME,
                launcher_class="omnigent.community.sandbox.ecs.launcher:EcsSandboxLauncher",
                config_model=EcsSandboxConfig,
                managed_token_ttl_s=MANAGED_TOKEN_TTL_S,
            )
        },
    )


__all__ = ["MANAGED_TOKEN_TTL_S", "PROVIDER_NAME", "EcsSandboxConfig", "contribution"]
