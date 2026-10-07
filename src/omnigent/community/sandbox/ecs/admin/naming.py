"""Every name the setup creates, derived from one short deployment name.

The bootstrap policies grant access by these prefixes (``omni-ecs-*``,
``omni-ecs/*``), so names must come from here and nowhere else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BOOTSTRAP_STACK = "omni-ecs-bootstrap"
CFN_SERVICE_ROLE = "omni-ecs-cfn"
BOUNDARY_POLICY = "omni-ecs-boundary"
STACK_TAG = "omni-ecs:stack"

# The ECR pull-through prefix (omni-ecs-<name>) is limited to 30 characters.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,20}$")


@dataclass(frozen=True)
class Names:
    """Resource names for one deployment, e.g. ``Names("prod")``."""

    name: str

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name) or self.name.endswith("-"):
            raise ValueError(
                "name must be 2-21 characters: lowercase letters, digits and '-', "
                "starting with a letter"
            )
        if self.stack == BOOTSTRAP_STACK:
            raise ValueError("'bootstrap' is reserved")

    @property
    def stack(self) -> str:
        return f"omni-ecs-{self.name}"

    @property
    def secret_prefix(self) -> str:
        """Parent of every secret this deployment owns."""
        return f"omni-ecs/{self.stack}/"

    @property
    def token_secret_prefix(self) -> str:
        return f"{self.secret_prefix}tokens/"

    def harness_secret(self, env_name: str) -> str:
        return f"{self.secret_prefix}harness/{env_name}"

    @property
    def ghcr_secret(self) -> str:
        # ECR requires pull-through credentials to live under this prefix.
        return f"ecr-pullthroughcache/{self.stack}"

    @property
    def task_family_prefix(self) -> str:
        return self.stack

    @property
    def image_repository(self) -> str:
        """Path of the host image under the pull-through prefix."""
        return f"{self.stack}/omnigent-ai/omnigent-host"
