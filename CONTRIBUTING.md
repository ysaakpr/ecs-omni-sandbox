# Contributing

## Setup

```bash
uv sync
uv run pre-commit install    # gitleaks + ruff on every commit
uv run pytest
```

## Rules for every change

1. **No secrets, ever.** This repository is public. Read [SECURITY.md](SECURITY.md#no-secrets-in-this-repository) for what counts and what placeholders to use. The pre-commit hook and CI will block obvious leaks, but they can't catch everything, so check your diff yourself.
2. **No real environment details.** Account ids, ARNs, subnet/VPC/security-group ids and hostnames from a live account belong in your deployment config, not in examples, tests, docs or commit messages.
3. **Tests use mocked AWS** (moto or stubs). Tests must never need real AWS credentials or make network calls.
4. **Keep the token out of plain-text surfaces.** The launch token may only travel through Secrets Manager. Don't log it, put it in a task definition, a RunTask override, an exception message or a tag.
5. **Least privilege.** If a change needs a new AWS permission, add it to `examples/iam/` and explain why in the pull request.

## Upgrading Omnigent

The provider depends on Omnigent internals (`_omnigent_compat.py`), so the Omnigent version is pinned exactly.

1. Bump `omnigent==X.Y.Z` in `pyproject.toml` and run `uv sync`.
2. Run `uv run pytest`. `tests/test_compat.py` fails if a reused internal moved or changed shape.
3. Diff Omnigent's `onboarding/sandboxes/kubernetes.py` and `base.py` against the previous version. Port any change to the workspace-prep or host commands, the launcher interface, or capability flags.
4. Release a new version of this package that pins the same Omnigent version as the server.

## Pull requests

- Keep them small and focused, and describe how you tested.
- CI must be green: tests, ruff and gitleaks.
