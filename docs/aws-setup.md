# AWS setup

All ids below are placeholders; keep your real values in a git-ignored `server-config.local.yaml`.

There are two ways to set up the AWS side: the `omnigent-ecs` command (recommended), or by hand. The command creates exactly what the manual steps describe, as CloudFormation stacks, so it can update and delete everything cleanly.

> The `omnigent-ecs` command and its templates have been tested against mocked AWS and checked with `cfn-lint`, but haven't been run against a real account yet. Expect the first real run to need fixes, most likely in IAM permissions.

## Automated setup

`omnigent-ecs` is installed with the package. It needs AWS credentials (an instance role, SSO login or similar) and a region (`--region` or `AWS_REGION`).

### What you need first

- **Private subnets** (1 to 3, one per availability zone) with a NAT gateway, in the VPC where tasks should run. `setup` checks the routes and refuses public subnets unless you pass `--assign-public-ip`.
- **A GitHub token** with only the `read:packages` scope, for the ghcr.io image cache.
- **Your LLM API keys**, for each `--harness-secret`.

### Step 1: bootstrap (once per account, by an admin)

This is the only step that needs IAM admin rights. It creates:

| Created | Purpose |
|---|---|
| `omni-ecs-cfn` role | CloudFormation uses it to build and delete deployments. It can only touch resources named `omni-ecs-*`. |
| `omni-ecs-boundary` policy | Upper limit on every role a deployment creates. The `omni-ecs-cfn` role can't create a role without it, or remove it. |
| `omni-ecs-server-runtime` policy | Attached to the Omnigent server's role: launch and stop sandboxes. |
| `omni-ecs-operator` policy | Attached to whoever runs `setup`/`teardown` (the server role unless you name another). |

```bash
omnigent-ecs bootstrap --print-template > bootstrap.yaml   # review it first
omnigent-ecs bootstrap --server-role <omnigent-server-role-name> [--operator-role <name>]
```

An admin can also deploy the printed template themselves, as a stack named `omni-ecs-bootstrap`.

### Step 2: setup (by the operator)

```bash
omnigent-ecs setup \
  --name prod \
  --server-url https://omnigent.example.com \
  --subnets subnet-0123456789abcdef0,subnet-0123456789abcdef1 \
  --harness-secret ANTHROPIC_API_KEY \
  --write-config /path/to/omnigent/server/config.yaml
```

It:
1. Checks the subnets (one VPC, one per AZ, a route to the internet).
2. Prompts for the GitHub token and each harness secret with hidden input, and stores them in Secrets Manager. Existing secrets are reused without prompting; `--rotate-secrets` asks again. For automation, set `OMNI_ECS_GHCR_USERNAME`, `OMNI_ECS_GHCR_TOKEN` and `OMNI_ECS_SECRET_<NAME>` instead.
3. Creates or updates the `omni-ecs-prod` stack: ECS cluster (Fargate and Fargate Spot), security groups, encrypted EFS with mount targets, the ghcr.io pull-through cache rule, a log group, and the task execution and task roles (under the boundary).
4. Writes the `sandbox:` section into the server config (the previous file is kept as a `.bak-<time>` copy), or prints it if you leave out `--write-config`. Secret values never go into the config, only their ARNs.

Re-running `setup` is safe; it updates the stack in place. Useful options: `--spot`, `--cpu`/`--memory`, `--arch X86_64`, `--no-efs`, `--image-tag`, `--idle-timeout`. See `omnigent-ecs setup --help`.

Then make sure the server image includes this package, and restart the server.

### Check a deployment

```bash
omnigent-ecs status --name prod
```

Shows the stack, how many sandboxes are running, and how many task definitions and launch-token secrets exist.

### Step 3: teardown

```bash
omnigent-ecs teardown --name prod --write-config /path/to/omnigent/server/config.yaml
```

You're asked to type the stack name to confirm. Then, in this order:

1. Stops every running sandbox task.
2. Removes the sandboxes' task definitions and EFS access points (the plugin creates these at runtime, outside the stack).
3. Deletes the cached images in ECR.
4. Deletes the stack.
5. Deletes the deployment's secrets (last, so a failed stack delete can be retried).
6. Removes the `sandbox:` section from the server config.

**The EFS file system is kept by default**, because it holds every sandbox's workspace. Re-attach it with `setup --existing-efs fs-…`, or delete it with `teardown --delete-data`. `--delete-data` only deletes a file system this deployment created.

To remove the bootstrap as well: `omnigent-ecs bootstrap --delete` (after every deployment is torn down).

## Manual setup

The same resources, created by hand. Use this if you manage infrastructure with your own tooling.

## 1. Networking

- **Subnets:** private subnets in at least two availability zones, with a **NAT gateway** for outbound traffic. Tasks need to reach the Omnigent server's public URL, the container registry, Secrets Manager, CloudWatch Logs, and any git or LLM endpoints the agents use.
- **Security group for tasks:** no inbound rules; outbound HTTPS (443), plus NFS (2049) to the EFS mount targets if you use EFS.
- **Omnigent server:** tasks connect to `sandbox.server_url` over HTTPS. With the Docker deployment that's the public Caddy endpoint; the server's own port only listens on localhost.

## 2. ECS cluster

- Create a cluster (e.g. `omnigent-hosts`) with no EC2 capacity.
- To use Fargate Spot, attach the `FARGATE` and `FARGATE_SPOT` capacity providers to the cluster.

## 3. Image

Serve the official host image (`ghcr.io/omnigent-ai/omnigent-host`) from ECR in the same region, so tasks pull it over the AWS network and don't depend on GitHub's registry being up.

**Recommended: an ECR pull-through cache rule.** ECR fetches the image from `ghcr.io` the first time a task asks for it, then serves it from your account and keeps it in sync. Nothing to copy by hand when you upgrade; you change the tag in the config.

1. **GitHub credentials.** ECR requires credentials for `ghcr.io`, even for public images. Create a GitHub token with only the `read:packages` scope and store it in Secrets Manager. The secret name **must** start with `ecr-pullthroughcache/`, and it must be in the same region as the rule:

   ```bash
   # Run from your own shell; never commit this token or paste it into the repo.
   aws secretsmanager create-secret \
     --name ecr-pullthroughcache/ghcr \
     --secret-string '{"username":"<github-user>","accessToken":"<token>"}'
   ```

2. **The rule.** Map an ECR namespace (here `ghcr`) to `ghcr.io`:

   ```bash
   aws ecr create-pull-through-cache-rule \
     --ecr-repository-prefix ghcr \
     --upstream-registry-url ghcr.io \
     --credential-arn arn:aws:secretsmanager:REGION:123456789012:secret:ecr-pullthroughcache/ghcr-AbCdEf
   ```

3. **The image reference** in `sandbox.ecs.image` becomes the upstream path under that namespace. Pin the tag that matches your server version:

   ```
   123456789012.dkr.ecr.REGION.amazonaws.com/ghcr/omnigent-ai/omnigent-host:v0.17.0
   ```

4. **First pull.** The ECR repository is created on the first pull, by whoever pulls. For ECS that's the task **execution role**, so it also needs `ecr:CreateRepository` and `ecr:BatchImportUpstreamImage` on the `ghcr/*` repositories (included in [`execution-role-policy.json`](../examples/iam/execution-role-policy.json)). If you'd rather not grant those, pull the image once yourself before the first launch, or pre-create the repository with an ECR repository creation template.

Things to know:
- ECR checks the upstream for a newer version of a tag at most once every 24 hours. With a pinned version tag that doesn't matter; with `:latest` it means you won't see a new release straight away. Pin version tags.
- The first launch after a version bump is slower, because ECR fetches the image from `ghcr.io` during that task's start.
- Add an ECR lifecycle policy to the `ghcr/*` repositories so old versions don't pile up.

**Alternative: copy the image yourself** (`docker pull`, `docker tag`, `docker push` into your own repository). Only worth it if you build your own host image on top of the official one, e.g. with extra tools baked in. In that case push your image to a normal ECR repository from CI.

## 4. IAM roles

| Role | Trusted by | Policy | Notes |
|---|---|---|---|
| Server (EC2 instance role) | `ec2.amazonaws.com` | [`server-policy.json`](../examples/iam/server-policy.json) | Runs tasks, manages token secrets and access points. Passes only the two roles below. |
| Task execution role | `ecs-tasks.amazonaws.com` | `AmazonECSTaskExecutionRolePolicy` + [`execution-role-policy.json`](../examples/iam/execution-role-policy.json) | Pulls the image, writes logs, injects secrets. Its credentials are never visible inside the containers. |
| Task role (optional) | `ecs-tasks.amazonaws.com` | [`task-role-policy.json`](../examples/iam/task-role-policy.json) | Only needed for EFS IAM authorization. **Agents run arbitrary code with this role. Give it nothing else.** |

If you encrypt secrets with a customer-managed KMS key (`token_kms_key_id`), the server role needs `kms:GenerateDataKey` and the execution role needs `kms:Decrypt` on that key.

## 5. Harness secrets

Store LLM keys and similar in Secrets Manager and reference them by ARN under `sandbox.ecs.secrets`. Grant the execution role `GetSecretValue` on exactly those ARNs.

## 6. EFS (recommended)

- Create an encrypted file system with a mount target in every task subnet. Its security group must allow NFS (2049) from the task security group.
- Set `sandbox.ecs.efs.file_system_id`. The provider creates one access point per sandbox under `root_path`, owned by the sandbox user.
- Containers can't mount other file systems or access points themselves (they have no mount capability), so one sandbox can't reach another's home through the task role.
- Back up the file system with AWS Backup if workspaces matter to you.

## 7. Logs

Create the CloudWatch log group named in `log_group` (e.g. `/omnigent/ecs-hosts`) and set a retention period. The provider doesn't create it.

## 8. Server

1. Build a server image that includes this package (see the README).
2. Add the `sandbox:` section (see [`examples/server-config.yaml`](../examples/server-config.yaml)).
3. Restart the server and create a session with **managed** as its host.
