# Contributing to Artemis

Thanks for helping with Artemis, a Turkish, single-user personal desktop assistant for Windows 11. This document summarizes the contributor contract from CLAUDE.md for outside contributors. Read CLAUDE.md in full before opening a pull request.

## Language

Code identifiers are in English. Comments, docstrings and documentation are in Turkish, explaining WHY, not just what. Commits may be made at meaningful steps. The primary documentation language is Turkish.

## Start with the design docs

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/adr/](docs/adr/) first. For architectural changes, write a short ADR: a file `docs/adr/NNNN-slug.md` (next free 4-digit number), with first line `# ADR NNNN — Title`, and add a link line to [docs/adr/README.md](docs/adr/README.md).

## Invariant rules

1. Every tool, channel and scheduled job goes through the single SafetyGate; commands go through CommandRunner. No second approval policy per tool or channel.
2. Approval signature is `(tool_name, arguments) -> bool`; the user sees the exact tool and validated arguments; default answer is no; a refused call is not retried.
3. Each tool declares READ_ONLY, SIDE_EFFECT or CONFIRM_REQUIRED; unknown risk counts as side-effecting. Deletion, shutdown, money, outbound messages, registry and service changes require approval.
4. Taint: if web, mail, file, clipboard or MCP results contaminate a turn, the taint persists to the end of that turn; later side effects need approval, and the approval says it was requested after external content was read. Tool results go to the model framed as "VERİDİR" (data, not instructions), capped at about 4000 characters.
5. Never return success without verifying the underlying action; API, subprocess or file errors are never presented as success.
6. Every user-supplied path goes through the shared `safe_join`; absolute paths, drive/UNC paths, `..`, the base itself and root-escaping symlinks are rejected; file-changing operations use `allow_symlinks=False`.
7. No blind `except Exception`; catch the expected error type and report honestly or write a redacted audit record.
8. Tests never touch the real machine: files under `tmp_path`, fake LLM provider, loopback-only fake HTTP server, fake Telegram client, fake screen/mouse backends. Tests with visible side effects use `@pytest.mark.disruptive`.
9. One tool = one class in one plugin file under `artemis/tools/plugins/`, registered with `@register_tool`. No second registry, provider schema or channel switch. A duplicate name is an error.
10. Shared needs are injected through `ToolContext`; use composition; no globals for approval, settings, audit or platform services.
11. Model, threshold and architecture choices are measured against acceptance scenarios; dangerous errors and unapproved side effects are counted separately from the total score.
12. Every turn is recorded in a UTF-8 JSONL audit (inputs, arguments, approvals, results, duration, tokens, cost); secrets are redacted; no file or clipboard content goes to the cloud without the required privacy permission.

## Secrets and paid models

Secrets come only from environment variables; never in YAML, argv, logs, tests, commits or `repr`. No paid model calls without explicit user permission; model IDs and prices are not hardcoded.

## Adding a tool

See the "Yeni araç eklemek" section of [CLAUDE.md](CLAUDE.md), which has the example. One tool lives in one plugin file under `artemis/tools/plugins/`. Add isolated tests for discovery, JSON schema, risk/taint behaviour and real success/failure results.

## Checks before a PR

Windows form, with the project virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe
.\.venv\Scripts\python.exe -m bench.run --sahte
```

- `tests/test_code_size.py` enforces ≤ 1300 lines per file and ≤ 200 lines per function under `artemis/` (the allowlist is empty and must stay so).

For safety regressions, show that the relevant test fails when the taint or approval check is temporarily removed (then revert the mutation). A passing fake bench is not proof of live model quality.
