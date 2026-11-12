# Django 6.1.3 compatibility verification

Local implementation diagnostics, 2026-11-12, against source base
`88f82ad`. These are not an exact Candidate Check, independent Review, deployed
release verification, or authenticated staging smoke. The coordinator recorded
the accepted Gunicorn 27.0.0 staging gate before this work.

## Runtime and compatibility scope

- Shared main-repository interpreter: Python 3.14.4.
- Django: 5.2.17 -> 6.1.3; every other direct requirement retained.
- SQLite: 3.46.1 (above Django 6.1's 3.37 minimum).
- PostgreSQL: 18.6, disposable official `postgres:18` container.
- Psycopg and psycopg-binary: 3.3.8; bundled libpq: 18.6.
- No transitive package changes were required: installed asgiref 3.13.0 and
  sqlparse 0.6.1 already meet the new requirements. Before/after `pip freeze`
  differed only in Django; `pip check` reported no broken requirements.

Reviewed official release notes:
[6.0](https://docs.djangoproject.com/en/6.1/releases/6.0/),
[6.1](https://docs.djangoproject.com/en/6.1/releases/6.1/),
[6.1.1](https://docs.djangoproject.com/en/6.1/releases/6.1.1/),
[6.1.2](https://docs.djangoproject.com/en/6.1/releases/6.1.2/), and
[6.1.3](https://docs.djangoproject.com/en/6.1/releases/6.1.3/).

The owning integration remains conventional synchronous Django:

- Model and historical migration constraints already use `condition=`;
  `DEFAULT_AUTO_FIELD` explicitly selects BigAutoField. No historical migration
  edits or compensating schema changes were needed.
- `slot_projection.py` clears ordering on both Normal and Casual Rounding
  operands. Its combined values query passed on SQLite and PostgreSQL without
  an ORM workaround. No application `first()`/`last()` consumer chains an
  explicitly cleared ordering; existing explicit/model ordering is retained.
- Authentication, forms, multipart uploads/imports, PDF exports, staticfiles,
  middleware, and real synchronous Gunicorn probes passed existing owning tests.
  No parser, authentication, transport, proxy-trust, or browser harness was
  replaced. Session storage remains Django's database backend.
- Invitations use `EmailMultiAlternatives.send()` and recovery uses Django's
  built-in flow, not legacy MIME subclasses or private connection overrides.
  `-Wd` exposed the eight configured legacy email-setting deprecations.
  They remain supported in 6.1.3; the bounded pre-Django-2028 follow-up is in
  [password recovery](../password-recovery.md#django-email-compatibility-follow-up).
  No warning filters, live SMTP calls, or mailer/product changes were introduced.

## Persistence and database evidence

Before replacing the shared Django installation, `manage.py migrate --noinput`
created separate synthetic SQLite and PostgreSQL databases under 5.2.17. Service
calls populated an administrator/group, three Physicians, a Physician-account
link, effective Faculty roles, availability templates, Normal/Casual Rounding,
a Precepting assignment, Vacation, and their histories and month timestamp.

After installing 6.1.3:

- Both databases migrated successfully with no pending operations. Parsed
  `dumpdata --all` output was identical before/after: 333 records in each database,
  including all serialized fields, primary/foreign keys and M2M relationships.
- The upgraded records supported combined Rounding projection and account-link
  reads, then an assignment-lock correction. An outer rollback preserved the
  assignment, history count and month timestamp; a committed correction updated
  all three together.
- Separate empty SQLite and PostgreSQL databases applied the complete migration
  chain, including `schedules.0060`. Migration-drift checks on both populated
  backends reported no changes.
- A separate thread-local PostgreSQL connection used ORM `select_for_update`
  with `nowait=True` to probe `SchedulingMutationAuthority`. It acquired the row
  before an operation, received SQLSTATE `55P03` inside the operation and after
  its inner scope ended, and acquired it again after the outer transaction
  committed or rolled back. The probe bypassed the process-local scheduling lock.
- Existing Physician/account unlink tests separately verified both rows' lock
  exclusion/release and history through commit and rollback.

## Fixture adaptation

The initial 247-test PostgreSQL run failed
`PreceptingFillProposalTests.test_late_doc_policy_counts_existing_work_and_reports_pm_overlap`
with selected Physician 59 instead of 58. Running its whole 35-test class on
5.2.17 reproduced the same failure; running the individual test on 6.1.3 passed
with fresh IDs. This was an ambiguous fixture, not a Django or solver regression:
Fill could assign Thin Monday to the other open PM position, so both Late Doc
candidates could earn PM overlap and the required winner was an arbitrary tie.

The fixture now explicitly retains both PM positions (including the eligible
backup-only Rounder), leaving Established as the only normally available Late
Doc candidate with PM overlap. Existing current-work, selected-Physician,
overlap-report and alternatives assertions are unchanged. Production solver
policy and schema are unchanged. The full class and subsequent cross-backend
checks passed without relying on a particular sequence start or tie outcome.

## Verification results

Commands used the shared `.venv/bin/python`, Django's normal test runner and
project helpers. PostgreSQL used scratch settings importing the normal test
settings and selecting only the owned loopback database.

- PostgreSQL: 247 tests passed with `--parallel=4` across
  `test_scheduling_mutation`, `test_month_edits`, `test_precepting_solver`,
  `test_physician_account_identity`, `test_slot_projection_contract`, and
  `test_master_schedule`.
- PostgreSQL: 50 additional tests passed across `test_lazy_session_initialization`,
  `test_late_docs`, and `test_stipend_rates`, exercising their database-only locks.
  Neither PostgreSQL run skipped tests.
- SQLite `-Wd` web/email/runtime focus: 75 tests, successful with two existing
  Nginx/OpenSSL prerequisite skips. Covered password lifecycle, both invitation
  modules, public authentication, production runtime, public edge and staging.
- `./scripts/quality-check`: Ruff lint/format, system checks and migration drift
  passed, including a final rerun after cleanup.
- `./scripts/check`: 1,633 Django tests in 107.649 seconds and all 28 JavaScript
  tests passed, including browser interactions and their retained static-server
  and terminal/redirect failure-detection coverage. Eight unchanged skips: six
  PostgreSQL-only checks (run successfully above), two unavailable local
  Nginx/OpenSSL integration prerequisites. No tests were newly skipped or ignored.
- Inspected full output: only existing `DATABASES` override warnings and the
  deliberately injected solver timing-failure diagnostic; no unexpected traceback
  or unresolved timeout. Explicit `-Wd` runs separately surfaced email deprecations.
- `./scripts/security-audit`: strict audit reported no known vulnerabilities.

## Resource boundary

Only synthetic databases and test-owned listeners/artifacts were used. The worker
owned container `casd-django613-3821248` (ID
`434f97ff4c2dd9acded1489235f3ec8147be010f1ba4d4ff2a4b0cf40852d698`), bound at
`127.0.0.1:32771`, and its anonymous volume
`a6f49ccd91b7a6de6738e181cd9f668aede781641f8e703a4c78d8d34f502a18`.
Scratch `.django613-verification/` held settings, synthetic databases, snapshots,
a temporary 5.2.17 diagnostic import tree, and local logs. The shared installation
remained 6.1.3 during the baseline diagnostic. No live database, protected host
configuration, credentials, remote service or deployment was accessed.

Cleanup completed: the container was stopped and removed with its volume (absence
verified), the newly downloaded `postgres:18` image was removed, and the exact
scratch directory and `/tmp/django613-api-inventory.txt` were deleted. `/tmp` usage was
21 MiB before and after verification. No worker-owned running process or scratch
residue remains; Cassidy retains ownership of the implementation worktree.
