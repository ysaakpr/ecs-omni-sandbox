# omnigent-ecs-sandbox

An [Omnigent](https://github.com/omnigent-ai/omnigent) sandbox provider that runs **server-managed hosts on AWS ECS Fargate**: one Fargate task per managed session, started on demand and stopped when the sandbox is terminated.

> **Status: early development (0.1.0.dev0).** Unit-tested against mocked AWS APIs only. It hasn't run against a real ECS cluster yet. Don't rely on it for production work.

Built for Omnigent **0.17.0** (pinned exactly; see [CONTRIBUTING.md](CONTRIBUTING.md#upgrading-omnigent)).

## How it works

Omnigent's server can create sessions with `host_type: "managed"`. For those it asks a sandbox provider for a fresh machine, runs `omnigent host` on it, and waits for that host to dial back. This package adds a provider named `ecs`.

```
Omnigent server (EC2)                      AWS
  │ provision()  → mints sandbox id
  │ start_host() ──► Secrets Manager: launch token  (one secret per launch)
  │              ──► ECS: register task definition  (family per sandbox)
  │              ──► ECS: RunTask on Fargate
  │                     ┌──────────── Fargate task ────────────┐
  │                     │ home-init      (only without EFS)    │
  │                     │ workspace-prep clone repos, config   │
  │                     │ host           omnigent host ────────┼──► dials back over HTTPS
  │                     └──── home volume: EFS or task disk ───┘
  │ resume()     ──► stop leftover task (server then calls start_host again)
  │ terminate()  ──► stop task, delete secrets, task defs, EFS access point
```

The containers run the same commands as Omnigent's built-in Kubernetes provider: the repo-clone script and the host command are reused from it.

## When tasks stop

A task runs `omnigent host` under a small supervisor (`supervisor.py`), which also stops the task when the sandbox is idle:

1. The agent's runner exits by itself after `runner.idle_timeout_s` with no activity (1 hour by default).
2. Once there's no runner and nothing else in the container is using CPU (a background job the agent started still counts as busy), the supervisor waits `idle_stop_after_s` (15 minutes by default), then stops the host cleanly. ECS stops the task and billing stops.
3. The next message to the session wakes it: Omnigent starts a new task under the same host id, and with EFS the workspace is still there.

Tasks also stop when the session is deleted, on `omnigent-ecs teardown`, on a Fargate Spot interruption, and when AWS retires a task for maintenance. Open terminals with nothing running in them don't keep a task alive. Set `idle_stop_after_s: 0` (`setup --idle-stop-after 0`) to keep tasks running until their session is deleted.

## Persistence

| Data | Stored in | Survives the task stopping? |
|---|---|---|
| Conversations, events, users, host identity, artifacts | Omnigent server (Postgres + `/data`) | Yes. The task never owns this data. |
| Workspace, `~/.omnigent`, agent state (e.g. `~/.claude`), installed dependencies | The task's home directory | **Only with EFS.** Without EFS it's lost when the task stops. |

When Omnigent wakes a stopped managed host, it reuses the same host id but doesn't re-clone repos; it expects the filesystem to still be there. On Fargate that's only true with the `efs` block configured, which gives each sandbox its own EFS access point. Without EFS, a woken session shows its history but starts with an empty workspace.

Deleting an access point doesn't delete its files. Cleaning up storage for terminated sandboxes is an [open item](#roadmap).

## Install

The package must be installed into the **Omnigent server's** Python environment. With the Docker deployment, that means a derived server image:

```dockerfile
FROM ghcr.io/omnigent-ai/omnigent-server:v0.17.0
RUN pip install --no-cache-dir "omnigent-ecs-sandbox @ git+https://github.com/ysaakpr/ecs-omni-sandbox@<tag>"
```

Then create the AWS side with the bundled `omnigent-ecs` command. An admin runs `bootstrap` once to create the roles, after which `setup` builds everything else and writes the server config, and `teardown` deletes it again:

```bash
omnigent-ecs bootstrap --server-role <omnigent-server-role-name>      # once, as an admin
omnigent-ecs setup --name prod --server-url https://omnigent.example.com \
  --subnets subnet-…,subnet-… --harness-secret ANTHROPIC_API_KEY \
  --write-config /path/to/server/config.yaml
omnigent-ecs teardown --name prod
```

Details, and the manual alternative, are in [docs/aws-setup.md](docs/aws-setup.md). An example `sandbox:` section is in [examples/server-config.yaml](examples/server-config.yaml).

The server needs AWS credentials to call ECS, Secrets Manager and EFS. On EC2, use the instance role. Never put access keys in the config.

## Security

- **Launch tokens** go into Secrets Manager and are injected by ECS using the task **execution** role. They never appear in a task definition, a RunTask request, CloudTrail request parameters, or the container's own AWS credentials.
- **Harness credentials** (LLM API keys, git tokens) are referenced by ARN in `secrets:`. The config rejects literal values, and also rejects `env:` names that look like credentials.
- **The task role** should have no permissions, or only EFS client access. Agents run arbitrary code inside the task; treat anything the task role can do as something the agent can do.
- **Containers** run as Omnigent's non-root `sandbox` user, with all Linux capabilities dropped.
- **ECS Exec** is disabled on launched tasks.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

**Never commit secrets** (keys, tokens, account-specific ARNs, `.env` files). Read [CONTRIBUTING.md](CONTRIBUTING.md) before your first commit and install the pre-commit hooks.

## Roadmap

- [ ] First end-to-end run against a real ECS cluster
- [ ] Cleanup job for EFS directories of terminated sandboxes
- [ ] Sweep for orphaned tasks, secrets and task definitions (by `managed-by` tag)
- [x] Idle stop: tasks stop themselves when idle and wake on the next message
- [ ] Optional Fargate Spot fallback to on-demand
- [ ] Publish to PyPI

## License

Apache-2.0, the same as Omnigent. See [LICENSE](LICENSE).
