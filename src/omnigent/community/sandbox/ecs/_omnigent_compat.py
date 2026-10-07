"""Every Omnigent internal this provider depends on, in one place.

The ECS task runs the same commands as Omnigent's Kubernetes Job: an init step
that prepares the workspace (clones repos with brokered git credentials, writes
the host config) and a host container that runs ``omnigent host`` under a
PID-1 reaper. Rather than copy that security-sensitive shell, we reuse the
Kubernetes provider's renderers. They are private names, so an Omnigent upgrade
can break them; ``tests/test_compat.py`` fails loudly when it does.
"""

from __future__ import annotations

from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR
from omnigent.onboarding.sandboxes.base import DEFAULT_HOST_IMAGE, SandboxHostLauncher
from omnigent.onboarding.sandboxes.kubernetes import (
    _HOME_DIR as HOME_DIR,
)
from omnigent.onboarding.sandboxes.kubernetes import (
    _RUN_AS_GID as RUN_AS_GID,
)
from omnigent.onboarding.sandboxes.kubernetes import (
    _RUN_AS_UID as RUN_AS_UID,
)
from omnigent.onboarding.sandboxes.kubernetes import (
    _render_workspace_prep_command as render_workspace_prep_command,
)
from omnigent.onboarding.sandboxes.registry import (
    SandboxProviderContribution,
    SandboxProviderMetadata,
)
from omnigent.onboarding.sandboxes.types import RepoWorkspace, SandboxCapabilities

__all__ = [
    "DEFAULT_HOST_IMAGE",
    "HOME_DIR",
    "HOST_ID_ENV_VAR",
    "HOST_NAME_ENV_VAR",
    "HOST_TOKEN_ENV_VAR",
    "RUN_AS_GID",
    "RUN_AS_UID",
    "RepoWorkspace",
    "SandboxCapabilities",
    "SandboxHostLauncher",
    "SandboxProviderContribution",
    "SandboxProviderMetadata",
    "render_workspace_prep_command",
]
