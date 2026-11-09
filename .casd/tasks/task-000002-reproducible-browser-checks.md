# Reproducible browser checks

Goal: [goal-000001](../goals/goal-000001-clean-ci-deployment-baseline.md)

## Objective

Make the existing real-browser checks reproducibly executable in GitHub's clean environment and documented local/Cassidy development, without depending on the internal transitive installation of a personal global Playwright CLI.

## Context and authoritative contracts

- `README.md`, development checks and Dependency updates and security monitoring: existing complete-check entry point and clean-environment workflow.
- `docs/design.md` section 23, Application shell and navigation / Reusable scheduler form presentation: semantic server-rendered baseline, no-JavaScript fallback, application-owned minimal enhancements, and the accepted 1.35rem/600 h1 and 1.1rem section tiers. Section 32.1 and 32.5 own shared surface/layout spacing. Preserve the real-browser evidence for these contracts.
- `docs/design.md` section 26.3 and root `AGENTS.md`: pinned development tools, ordinary checks without network access, shared main virtualenv in Cassidy worktrees, existing helper commands.
- Owners: `.github/workflows/clean-environment-checks.yml`, `scripts/check`, `schedules/tests/test_page_title_typography.py`, and existing browser test consumers under `schedules/tests/`.
- Every current `@playwright/cli/node_modules/playwright` reference in `schedules/tests/` is a consumer of the same undeclared global-library assumption, including embedded Node runners and availability probes. `test_schedule_reports.py` and `test_float_schedule_inputs.py` contain browser consumers despite not having browser-only filenames. Most tests skip when the global dependency is missing; the title typography test checks only Node and Chrome before requiring it.
- Main-revision GitHub run https://github.com/kenjorissen/muriel/actions/runs/42634181537 at `95e8cf674896d49a9eb06370bf192baa8b42140b` failed in the typography browser test with MODULE_NOT_FOUND. The workflow installs Node 22 and Python 3.14 on Ubuntu 26.04, but declares no Playwright installation.
- Playwright's supported library interface and browser installation are documented at https://playwright.dev/docs/library and https://playwright.dev/docs/browsers . Use that conventional interface rather than a CLI package's nested private dependency. This is test tooling only, not frontend application infrastructure.

## Acceptance

1. Repository-declared reproducible development setup installs a pinned Playwright library and compatible Chromium browser/prerequisites through supported tooling. CI explicitly performs that setup before complete checks. Document the equivalent local setup; do not rely on preexisting global Node modules, an operator's home-directory layout, or implicit runner-image browser availability.
2. All existing real-browser tests consume that supported declared installation consistently, including availability probes and embedded runners. Browser selection and module resolution work from the canonical checkout and disposable Cassidy worktrees using a documented ownership arrangement, without copying dependencies into product source or introducing a frontend build pipeline.
3. The complete-check CI path fails usefully if required browser prerequisites are missing rather than reporting success through dependency-driven skips. Existing optional focused local inspection may retain an explicitly documented unavailable-tool policy, but cannot make the required CI path silently omit browser coverage.
4. With clean setup, existing browser checks actually run and preserve their behavioral assertions, including typography, layout, keyboard/fallback behavior and interactions. No test is weakened or blanket-skipped to make the environment appear green. If newly executed tests reveal independent product or unresolved contract defects, report those precisely rather than silently broadening this infrastructure Task.
5. `./scripts/check` remains the complete-check entry point, uses existing Django/Node orchestration, and does not install or download dependencies during its deterministic run. Browser processes and any new invocation-owned temporary resources are cleaned on success and failure. Preserve the existing integration-test exclusions and Python virtualenv conventions.

## Verification

Demonstrate the missing-dependency diagnostic and a clean declared setup without a global Playwright CLI. Execute the existing real-browser tests rather than merely proving an import or source-string change; use their current behavior evidence, not new copies of every browser test. Include worktree module/browser-resolution evidence. Run `./scripts/quality-check` and `./scripts/check`; keep local results distinct from a future exact-revision GitHub run. Equivalent dependency layout, browser paths, or helper structure may vary if these observable obligations hold. No blanket browser-test inventory assertions or new browser framework are required.

## Constraints and predecessor

Task 000001 supplies ordinary parallel failure reporting, which this Task preserves. Browser setup is development/CI-only; preserve runtime dependencies, app behavior, rendering contracts, and test semantics. Do not upgrade unrelated application dependencies or merge either Dependabot PR. No push, GitHub settings mutation, deployment, or data reset is authorized.

## Execution strategy

Inventory global Playwright references first so the runner imports and prerequisite probes move together; fixing only typography would leave the other browser suites silently skipped. Establish supported declared tooling and clean setup before assessing newly visible test failures. Reuse a narrow shared test-only resolver/setup boundary if it removes the repeated environment assumption, while leaving each test's domain interactions and assertions local. Use the repaired parallel reporting to distinguish setup failures from independent behavioral failures; it is not permission to accept or weaken either.
