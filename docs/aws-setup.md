# AWS setup

One-time setup for the `ecs` provider. All ids below are placeholders; keep your real values in a git-ignored `server-config.local.yaml`.

## 1. Networking

- **Subnets:** private subnets in at least two availability zones, with a **NAT gateway** for outbound traffic. Tasks need to reach the Omnigent server's public URL, the container registry, Secrets Manager, CloudWatch Logs, and any git or LLM endpoints the agents use.
- **Security group for tasks:** no inbound rules; outbound HTTPS (443), plus NFS (2049) to the EFS mount targets if you use EFS.
- **Omnigent server:** tasks connect to `sandbox.server_url` over HTTPS. With the Docker deployment that's the public Caddy endpoint; the server's own port only listens on localhost.

## 2. ECS cluster

- Create a cluster (e.g. `omnigent-hosts`) with no EC2 capacity.
- To use Fargate Spot, attach the `FARGATE` and `FARGATE_SPOT` capacity providers to the cluster.

## 3. Image

Mirror the official host image into ECR in the same region, so tasks start faster and don't depend on a public registry:

```bash
# Pick the tag matching your server version; ARM64 for Graviton.
docker pull ghcr.io/omnigent-ai/omnigent-host:v0.17.0
docker tag  ghcr.io/omnigent-ai/omnigent-host:v0.17.0 <account>.dkr.ecr.<region>.amazonaws.com/omnigent-host:v0.17.0
docker push <account>.dkr.ecr.<region>.amazonaws.com/omnigent-host:v0.17.0
```

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
