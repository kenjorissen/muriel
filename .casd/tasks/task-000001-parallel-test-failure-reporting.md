# Parallel test failure reporting

Goal: [goal-000001](../goals/goal-000001-clean-ci-deployment-baseline.md)

## Objective

Make ordinary Django parallel-test failures report the failing test and traceback rather than crash while serializing the traceback, using Django's supported tblib integration.

## Context and authoritative contracts

- `README.md`, development checks and Dependency updates and security monitoring: declared reproducible development setup and existing check commands.
- `docs/design.md` section 26.3: pinned development-only tooling, ordinary development/Cassidy worktrees, non-networked deterministic checks.
- Root `AGENTS.md`: shared main-repository virtualenv and fixed check helpers.
- Owning files: `requirements-dev.txt`, `scripts/check`, and `.github/workflows/clean-environment-checks.yml`. The helper uses Django's ordinary `--parallel=4` runner, not a project-owned test scheduler.
- Django 5.2's official django-admin test `--parallel` documentation names tblib for correct traceback reporting: https://docs.djangoproject.com/en/5.2/ref/django-admin/#cmdoption-test-parallel . The installed `django/test/runner.py` already detects and uses that optional package.
- Main-revision GitHub run https://github.com/kenjorissen/muriel/actions/runs/42634181537 at `95e8cf674896d49a9eb06370bf192baa8b42140b` retained an ordinary browser assertion followed by `TypeError: cannot pickle 'traceback' object`. The declared dev requirements omit tblib. This Task addresses the secondary reporting defect, not the independent browser setup defect.

## Acceptance

1. Installing the declared development requirements supplies a pinned compatible tblib dependency. It is not added to production runtime requirements.
2. A controlled ordinary failing test through the existing Django parallel runner produces a nonzero test result with attributable test/traceback information, without the observed traceback-pickling crash. Include an ordinary assertion and ordinary exception if needed to establish the runner integration; do not replace Django's runner or invent a reporting framework.
3. Existing passing tests remain passing with the existing parallel policy. Update development instructions only where needed to explain declared setup and failure diagnostics.

## Verification

Use the existing main virtualenv and test settings. A temporary invocation-owned test fixture is sufficient for the intentional failure probe: retain exact command/result evidence and remove that fixture safely afterward. No permanently failing test or elaborate harness is required. The probe must actually exercise a multi-process run; a serial rerun alone cannot establish this behavior. Keep the expected nonzero probe outcome separate from the passing project Check. Run `./scripts/quality-check` and `./scripts/check` as applicable; report unrelated baseline failures honestly rather than claiming this fixes browser provisioning or dependency-security findings.

## Constraints

Do not disable parallel execution, suppress failures, skip browser tests, weaken assertions, alter scheduling/PDF behavior, or upgrade unrelated packages. No push, PR merge, live host action, or disposition is authorized. Own and clean only scratch resources created for the probe. Ordinary package resolution/version choice remains implementation discretion within the supported integration.
