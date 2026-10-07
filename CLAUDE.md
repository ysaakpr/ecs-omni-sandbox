# Instructions for AI coding agents

This file applies to Claude Code, Codex, Cursor and any other agent working in this repository. `AGENTS.md` points here.

## Hard rules

- **This repository is public. Never write secrets or real environment details into any file, commit message, PR, issue or comment.** That includes AWS keys, tokens, passwords, real account ids, ARNs, VPC/subnet/security-group/file-system ids, hostnames and IPs, and output copied from a real AWS account or Omnigent server. Use placeholders such as `123456789012` and `subnet-0123456789abcdef0`.
- Never read `.env` files, `~/.aws/`, `~/.omnigent/` or other credential stores to fill in examples or tests.
- Never log, print, tag or put the Omnigent launch token into an exception message. It only travels through Secrets Manager.
- Tests must use mocked AWS (moto). Never call real AWS from tests.
- Don't commit or push unless the user asks. Don't bypass the pre-commit hooks (`--no-verify`).

## Project facts

- Provider code lives in `src/omnigent/community/sandbox/ecs/`. Omnigent's registry rejects provider modules outside the `omnigent.community.sandbox.` namespace.
- Don't add `__init__.py` files to `src/omnigent/`, `src/omnigent/community/` or `src/omnigent/community/sandbox/`. Those packages belong to the omnigent distribution, and shipping them would overwrite its files.
- All imports of Omnigent internals go through `_omnigent_compat.py`.
- `taskdef.py` is pure (builds request dicts, no AWS calls); `launcher.py` makes the calls.

## Commands

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```
