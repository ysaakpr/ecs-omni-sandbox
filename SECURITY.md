# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Use GitHub's private reporting: **Security → Report a vulnerability** on this repository. Include what you found, how to reproduce it, and the impact you expect.

We aim to acknowledge reports within 5 working days.

## Scope

In scope: anything in this repository. That includes how launch tokens and credentials are handled, IAM guidance in `docs/` and `examples/`, and isolation between sandboxes.

Problems in Omnigent itself should go to the [Omnigent project](https://github.com/omnigent-ai/omnigent).

## No secrets in this repository

This is a **public** repository. Never commit:

- AWS access keys, session tokens, or any other credentials
- Omnigent launch tokens, API keys for LLM providers, GitHub tokens
- Real AWS account ids, ARNs, VPC/subnet/security-group ids, or file system ids from a live environment
- `.env` files, kubeconfigs, `*.pem` / `*.key` files, Terraform state
- Logs, task descriptions or CloudTrail output copied from a real account

Use obvious placeholders instead: `123456789012`, `subnet-0123456789abcdef0`, `fs-0123456789abcdef0`, `arn:aws:secretsmanager:REGION:ACCOUNT:secret:NAME`.

### Guardrails

- **pre-commit:** `gitleaks` runs on every commit (see `.pre-commit-config.yaml`).
- **CI:** gitleaks scans every push and pull request.
- **GitHub:** secret scanning and push protection are enabled on the repository.

### If a secret is committed

1. **Revoke or rotate it first.** Assume it's compromised the moment it was pushed. Deleting the commit isn't enough; public pushes are copied and indexed within minutes.
2. Then remove it from the history and tell the maintainers through private reporting.
