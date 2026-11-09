# GitHub Actions v7

Goal: [goal-000002](../goals/goal-000002-staged-dependency-upgrades.md)

## Objective

Upgrade the existing clean-environment workflow's checkout, Python setup and Node setup actions to v7 while retaining the workflow's existing security and verification behavior. This is the scoped equivalent of Dependabot PR #1, not an application dependency upgrade.

## Contracts and owning evidence

- `README.md`, Development checks and Dependency updates and security monitoring: clean-environment checks on pull requests/main pushes/manual dispatch; separate strict declared-dependency audit; explicit browser setup followed by offline quality and complete checks.
- `docs/design.md` section 26.3 and root `AGENTS.md`: pinned development tooling and ordinary helper/virtualenv conventions.
- Owning workflow: `.github/workflows/clean-environment-checks.yml`; execution owners: `scripts/setup-browser`, `scripts/quality-check`, `scripts/check`, `scripts/security-audit`. Baseline `9e16310d1c78ace9dd60f5d2342a21c8606563a8` has a successful exact GitHub run at https://github.com/kenjorissen/muriel/actions/runs/42657266122 .
- Official v7 release notes: https://github.com/actions/checkout/releases/tag/v7.0.0 , https://github.com/actions/setup-python/releases/tag/v7.0.0 , https://github.com/actions/setup-node/releases/tag/v7.0.0 . Consult their actual action inputs/runtime requirements where relevant. Notable changes include ESM/dependency updates, checkout restrictions on fork checkout from pull_request_target/workflow_run, and removal of setup-python's pip-install input. This workflow does not use those privileged events or the removed input; do not introduce them.

## Acceptance

1. All current checkout/setup-python/setup-node uses in this workflow select v7, with compatible existing inputs. Keep Python 3.14, Node 22 and Ubuntu 26.04 unchanged.
2. Preserve event triggers and contents:read permissions, job separation, explicit dependency/browser provisioning, strict auditing, and existing quality/full-check commands. No permissions expansion, secret addition, cache-policy redesign, action omission, or weakening/skipping verification.
3. The workflow remains conventional GitHub Actions YAML; the change is bounded to action compatibility and any directly needed documentation. Runtime requirements, product source/tests and deployment helpers remain unchanged.

## Verification and authority

Inspect the official action contract against current inputs and preserve the existing helper tests/checks rather than adding a permanent test that only freezes action-version text. Run `./scripts/quality-check` and `./scripts/check` for regression evidence as required. Local tests cannot execute GitHub's action runner: explicitly report this limitation. After Candidate acceptance, the authorized Architect pushes and inspects both jobs on the exact resulting revision; only that run establishes workflow integration success.

Do not push, merge or close PRs, change GitHub settings, or deploy from the bounded worker. PR reconciliation and remote evidence belong to the coordinator after acceptance. No other dependency upgrade is included; the successor Ruff Task extends the same workflow without replacing its verification guarantees.
