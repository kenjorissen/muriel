# Psycopg 3.3.8

Goal: [goal-000002](../goals/goal-000002-staged-dependency-upgrades.md)

## Objective

Upgrade `psycopg[binary]` from 3.2.10 to 3.3.8 while retaining Django-owned PostgreSQL persistence, transactional scheduling authority, and SQLite development behavior. Prove actual PostgreSQL 18 compatibility on disposable test data, not only mocked settings or SQLite.

## Contracts and owning evidence

- `docs/design.md` sections 4 (SchedulingMutationAuthority), 7.2 (retained history and organization time), 15.2 (human-reviewed proposal Apply), and 26 (Python 3.14, PostgreSQL production and SQLite development). The accepted month-edit recorder section requires atomic assignment/history/timestamp changes and rolled-back projection savepoints.
- `docs/production.md`, Runtime installation and Readiness; `docs/staging.md`, isolated database identity and non-destructive update; `docs/deployment.md`, release/readiness and no automatic rollback after migrations. Existing runtime connection settings and fixed local Unix-socket topology remain unchanged.
- Owners: `requirements.txt`, `preceptor_project/production_settings.py`, `preceptor_project/staging_settings.py`, Django's PostgreSQL backend, `schedules/services/_mutation.py`, `schedules/services/month_edits.py`, and `apply_precepting_solver_proposal` in `schedules/services/precepting_solver.py`. There is no application-owned direct psycopg adapter, pooling, GSSAPI or SSL transport policy to redesign.
- Existing evidence: `schedules/tests/test_scheduling_mutation.py` (including serialization), `schedules/tests/test_month_edits.py` (including nested mutation/rollback), and `schedules/tests/test_precepting_solver.py` / `RetainedPreceptingSolverProposalTests` (reviewed Apply, stale refusal, partial-write rollback, history). `test_production_database_validation.py` mocks endpoint facts and cannot establish real driver compatibility. Existing serialization testing also exercises a process-local lock, not independently PostgreSQL row-lock exclusion.
- Captured target: psycopg portion of Dependabot PR #2 at `bfb734df6079ecb45b51591eee96ed744d2737a6`. Consult official https://github.com/psycopg/psycopg/blob/3.3.8/docs/news.rst and Django 5.2 PostgreSQL backend documentation at https://docs.djangoproject.com/en/5.2/ref/databases/#postgresql-notes . Intervening changes include cursor iteration, transaction/cancellation fixes, binary-package GSSAPI default changes, and the 3.3.8 SSL-directory reversal. Assess relevance to this synchronous Django/local-socket deployment; do not adopt unrelated new APIs.

## Acceptance

1. Pin and install `psycopg[binary]==3.3.8` in the shared main virtualenv. Confirm the installed Python package and binary implementation agree. Preserve all other pins and preceding accepted upgrades.
2. Under Python 3.14 and actual disposable PostgreSQL 18, Django can create its test database, apply the existing migration chain, and exercise the same owning scheduling/retained-proposal behavioral tests used on SQLite. Preserve dates, aware timestamps, JSON/history, atomic mutation and rollback, and stale/duplicate Apply protections without changing scheduling semantics or bypassing the ORM.
3. Direct PostgreSQL evidence establishes that the existing mutation authority's row lock excludes a separate database connection while held and becomes available after the transaction ends. Existing Python-lock tests alone are insufficient for this adapter-specific obligation. Use bounded deterministic synchronization or PostgreSQL NOWAIT semantics, not sleeps/race-dependent assertions.
4. The normal SQLite-based `scripts/quality-check` and `scripts/check` pass. No altered production/staging credentials, identities, connection limits, middleware, schema, test exclusions, or runtime topology; no replacement database framework, pool, broad refactor, or new test platform. Escalate a consequential compatibility failure instead of weakening contracts.

## Verification and resource boundary

Use the existing Django test runner and relevant tests on both backends. One-time disposable settings/evidence may select PostgreSQL without adding a permanent runner or version-string test. Local Docker is available and already has `postgres:18-alpine` (inspected image digest `sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2`); an invocation-owned local test instance is permitted, not a new deployment architecture. Restrict exposed test transport to loopback, use only disposable test data and credentials, never mount application databases or protected configuration, and record exact created resources. The worker owns stopping/removing only its container, any owned volumes and scratch settings on success or failure; retain results in Attempt evidence and report uncertain residue. Do not touch existing containers. Use the shared main virtualenv for all project Python commands.

Record installed driver/binary/libpq and database versions, tested revision, migration/transaction results, focused cross-backend results, normal quality/full results, and cleanup. Reuse existing assertions; any necessary fixture adaptation must preserve behavioral protection. A PostgreSQL fixture proves backend compatibility, not the commissioned Ubuntu socket/service path. The authorized coordinator separately owns exact-revision GitHub checks and later non-reset staging readiness/HTTP verification; do not claim those from local tests.

Worker authority excludes pushes, PR disposition, deployment, live staging/production access, resets, or remote configuration changes. No production rollout.

## Execution strategy

Inspect release notes and existing backend use first, then install only the target driver. Establish a uniquely owned disposable PostgreSQL instance and a temporary settings override using ordinary Django configuration; keep application code and project commands in the supplied worktree and use the shared interpreter. Run the relevant existing mutation, month-edit and retained-Apply tests against that migrated test database, and separately demonstrate actual row-lock exclusion because the process-local lock can mask it. This isolates driver compatibility before broader SQLite checks. Cleanup of test resources is independent of test success. An equally sound existing-Django-runner approach is allowed; the disposable container is a practical available resource, not a new supported runtime.
