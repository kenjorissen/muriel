# Psycopg 3.3.9

Goal: [goal-000002](../goals/goal-000002-staged-dependency-upgrades.md)

## Objective and predecessors

Upgrade the newly refreshed `psycopg[binary]` target from accepted 3.3.8 to 3.3.9 while retaining actual PostgreSQL 18 compatibility, Django-owned persistence and SQLite development. The planned sequence follows task-000020, with Django 6.1.3 already accepted. Task-000011's account-unlink repair and task-000009's driver/transaction evidence remain applicable; do not restore old packages or unaccepted diagnostic edits from those historical Attempts.

## Contracts and owning evidence

- `docs/design.md` section 4 SchedulingMutationAuthority, section 7.2 retained history/time, section 15.2 human-reviewed proposal Apply and section 26 Python 3.14/PostgreSQL/SQLite. The accepted month-edit recorder requires atomic writes/history/timestamps and rolled-back projection savepoints. Account unlink must preserve both-row locks, history, rollback and already-unlinked no-op without changing authorization.
- `docs/production.md` runtime/readiness, `docs/staging.md` isolated identity/non-reset update and `docs/deployment.md` migration/readiness/no automatic database rollback. Keep local Unix-socket production connections and existing staging identity; no pooling, adapter framework or connection-policy redesign.
- Owners: `requirements.txt`, Django's PostgreSQL backend and project settings; `schedules/services/{_mutation,month_edits,accounts,precepting_solver}.py`. Existing behavioral evidence: `test_scheduling_mutation.py`, `test_month_edits.py`, `test_physician_account_identity.py`, `test_account_administration.py`, and retained-proposal tests in `test_precepting_solver.py`. Mocked database validation cannot establish real backend compatibility; process-local serialization cannot alone establish database row locking.
- Read `https://github.com/psycopg/psycopg/blob/3.3.9/docs/news.rst`. The patch changes range/Decimal adaptation, interrupted cancellation/connection closure, COPY buffering/failure paths and performance. The application uses ordinary synchronous Django ORM connections; no async/COPY/pipeline subsystem is to be added just to exercise unused features. Record actual libpq bundled with the binary package rather than assuming it equals the predecessor.

## Acceptance and verification

1. Declare/install `psycopg[binary]==3.3.9` in the shared main virtualenv; Python package and binary implementation versions agree. Preserve all other direct pins and preceding accepted repairs/upgrades. Record Python, driver, binary/libpq and PostgreSQL versions.
2. Actual disposable PostgreSQL 18 creates the test database, applies the complete existing migrations, and passes the same focused owning persistence/retained-proposal tests as SQLite. Preserve dates, aware timestamps, JSON/history, authorization/identity, stale/duplicate Apply refusal, atomicity and partial-write rollback. No ORM bypass or schema change to mask a driver problem.
3. Reuse the accepted actual independent-connection exclusion/release probes for scheduling mutation and the Physician/account unlink locks. Demonstrate exclusion while held and release after transaction completion/rollback with bounded deterministic synchronization or PostgreSQL NOWAIT, not sleeps or only Python locks. This does not claim full global lock-order safety.
4. `./scripts/quality-check`, `./scripts/check` and `./scripts/security-audit` pass with output inspected, preserving the browser failure observer and all behavioral assertions. Exact Candidate Check and independent Review are required before coordinator acceptance. Report a material failure instead of changing security/transaction requirements or silently downgrading.

## Resource and authority boundary

Use shared main Python and the normal Django runner. An invocation-owned local `postgres:18-alpine` test container and temporary settings are allowed, with loopback-only exposed transport, synthetic credentials/data and no application database/protected configuration mounts. Record exact container, volumes, settings and other scratch ownership. The worker removes only these owned resources on success/failure and retains results in Attempt evidence; do not touch unrelated containers/processes. No live staging/production access, deployment, reset, pushes/PR disposition, credential changes or sudo. The coordinator owns exact-revision GitHub checks and later non-reset staging evidence; local TCP fixtures do not prove commissioned socket/service operation.

## Execution strategy

Install only the driver patch after release-note assessment, then exercise migrated PostgreSQL transactions and the accepted lock evidence before broad SQLite checks. Reuse conventional temporary Django settings and existing tests, not a permanent second runner or test platform. Cleanup is required regardless of outcome; report uncertain residue. Distinguish resolved dependency currency and advisory evidence from actual deployed versions.
