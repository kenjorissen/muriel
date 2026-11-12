# Psycopg 3.3.9 compatibility verification

Local implementation diagnostics on 2026-11-12, source base `bada65e`.
These are not the exact Candidate Check, independent Review, or deployed-version
verification. Candidate Check and independent Review remain required; the
coordinator owns later non-reset staging evidence.

## Upgrade and release assessment

`requirements.txt` changes only `psycopg[binary]` from 3.3.8 to 3.3.9.
Installation used the main repository's shared `.venv/bin/python`; before/after
`pip freeze` differed only in `psycopg` and `psycopg-binary`. `pip check` passed.

Observed runtime:

| Component | Version |
| --- | --- |
| Python | 3.14.4 |
| Django | 6.1.3 |
| psycopg / psycopg-binary | 3.3.9 / 3.3.9 |
| Active implementation | binary |
| Bundled libpq (`psycopg.pq.version()`) | 180006 (18.6) |
| Disposable PostgreSQL server | 18.6, Alpine, x86_64 |
| SQLite | 3.46.1 |

Reviewed the official [3.3.9 release notes](https://github.com/psycopg/psycopg/blob/3.3.9/docs/news.rst).
The patch fixes typed range/multirange binary adaptation, Decimal zero with large
exponents and negative-zero client-side quoting, repeated interruption during
cancellation (closing the connection rather than leaving it ACTIVE), and COPY
buffering/error paths. It improves sync/async waits, list dumping and UUID loading,
and reduces binary package size. Composite registration now raises DataError for
invalid namedtuple identifiers. Windows C wait functions are enabled again.

Django's conventional synchronous PostgreSQL backend remains the persistence
owner. No async, COPY, pipeline, pooling, custom adapter, transport-policy or
schema changes were introduced to exercise unused APIs. The existing month-edit
savepoint recorder and account-unlink repair are unchanged. Production Unix-socket
connections, staging identity, and deployment/migration/rollback policy are
unchanged; these local TCP fixtures do not prove commissioned service operation.

## Behavioral verification

The shared interpreter ran Django's normal `manage.py test` with `--parallel=4
--noinput` against the same five modules on both backends:

- `schedules.tests.test_scheduling_mutation`
- `schedules.tests.test_month_edits`
- `schedules.tests.test_physician_account_identity`
- `schedules.tests.test_account_administration`
- `schedules.tests.test_precepting_solver`

PostgreSQL created its test database and four clones, applied all existing
migrations through `schedules.0060` (plus Django migrations), and passed all 230
tests in 30.731 seconds without skips. SQLite ran the same 230 tests in 19.330
seconds, passing with only the two existing PostgreSQL-only unlink-lock skips.
Existing assertions cover effective dates, aware edit times, retained JSON/history,
authorization and identity, projection savepoint rollback, stale proposal refusal,
nonduplicating Apply, and all-or-nothing writes after a later failure.

The existing `PhysicianAccountUnlinkLockTests` passed on PostgreSQL: independent
thread-local connections probe each Physician/account row with ORM
`select_for_update(nowait=True)`, establish exclusion during the enclosing
transaction, and establish release after both commit and rollback, including
link/history outcomes.

A temporary two-test `TransactionTestCase` reused the accepted scheduling-authority
probe described in the Django 6.1.3 audit. A separate thread-local connection,
bypassing the Python scheduling lock, used ORM NOWAIT and a ten-second future
bound. For each of outer commit and rollback it observed:

1. Authority row available before the operation.
2. SQLSTATE `55P03` inside `scheduling_operation()`.
3. SQLSTATE `55P03` after that inner operation, before the outer transaction ended.
4. Authority row available after the outer transaction ended.

Both tests passed (0.180 seconds); probe connections closed in `finally`. No
sleep-based lock assertion or claim of full global lock-order safety was added.

## Required checks

- `./scripts/quality-check`: Ruff lint/format, Django system checks and migration
  drift passed.
- `./scripts/check`: 28 JavaScript tests and 1,635 Django tests passed; Django
  elapsed time 107.163 seconds. Eight existing skips: six PostgreSQL-only checks
  and two requiring local Nginx/OpenSSL. The two unlink checks also passed in the
  PostgreSQL focus above; the other four PostgreSQL-only checks were not part of
  this task's focused run. Browser failure-observer coverage remains unchanged.
- `./scripts/security-audit`: strict audit found no known vulnerabilities.

Output was inspected: existing DATABASES-override warnings and deliberately
injected solver timeout diagnostics appeared, with no unexpected failure or
unresolved timeout. No assertions, skip conditions, or security checks changed.

## Owned resources and cleanup

Only synthetic local data was used. The preexisting `postgres:18-alpine` image
`sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2`
was retained. This invocation created:

- Container `casd-psycopg339-2069327`, ID
  `1955c7734dd457dab1af83bde450d5ec5b4fb00db8da7741a5422819bfe7da4d`.
- Anonymous volume
  `1b485c666730d3fbb8e1259aa5a4f9f73643f77eab0112c12f27dabfa473f3a6`,
  mounted only at `/var/lib/postgresql`.
- Loopback transport `127.0.0.1:32772`, synthetic database/role `psycopg_test`.
- Worktree scratch directory `.psycopg339-verification/`, containing
  `pg_settings.py`, `authority_probe.py`, package snapshots, PostgreSQL test log
  and Python bytecode. Settings imported normal test settings and overrode only
  the disposable database connection with a five-second connect timeout.

The container was stopped and removed with its volume; exact container and volume
inspection confirmed absence. The scratch directory was removed after output
inspection. `/tmp` usage remained 22 MiB. No application database or protected
configuration was mounted; no live staging/production access or deployment was
performed. No worker-owned scratch resources remain.
