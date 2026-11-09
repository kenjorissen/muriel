# Clean CI deployment baseline

## Outcome

Establish a reviewed, reproducibly checked revision of this fresh Muriel repository that can be evaluated for staging and then production on the human's `mini-game` host. This Goal covers the known CI repairs, not deployment itself.

## Success conditions

- Declared development dependencies let Django's existing parallel runner report ordinary failures without a traceback-pickling error.
- Documented clean-environment setup supplies browser-test dependencies through supported interfaces; CI actually executes existing browser coverage without relying on a personal global installation or hiding missing prerequisites as success.
- The pinned pypdf dependency has no findings in the existing declared-dependency security audit, and existing PDF behavior remains protected.
- After explicit human publication/push authorization, both jobs of the existing GitHub workflow pass on the exact reviewed revision; retain the run identity and commit as evidence. Local Check alone does not establish GitHub success.

## Boundaries

Preserve scheduling behavior, PDF contracts, independent Review, ordinary authoritative Check, and the existing default integration-test exclusions. No unrelated dependency major upgrades, blanket test weakening, automatic merge, remote push, GitHub settings changes, or live host mutation is authorized by this definition. GitHub and deployment operations still need human authorization.

After this outcome, separately scope and authorize staging verification, then production rollout. Preserve existing staging/production data and configuration; no reset or data replacement is implied. The deployment procedures remain `docs/staging.md` and `docs/deployment.md`, with explicit SSH target and host/origin selection. No live identity is inferred from `mini-game`.

## Tasks

- [Task 000001: Parallel test failure reporting](../tasks/task-000001-parallel-test-failure-reporting.md)
- [Task 000002: Reproducible browser checks](../tasks/task-000002-reproducible-browser-checks.md)
- [Task 000003: Patched pypdf dependency](../tasks/task-000003-patched-pypdf-dependency.md)
