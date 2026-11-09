# Muriel: Physician Scheduling

Muriel is a server-rendered Django application for maintaining physician
scheduling facts and preparing reviewed Precepting schedules. It is currently
configured for one organization and uses
SQLite for local development.

## Product overview

Authenticated schedulers use these current workspaces:

- **Schedule** is the read-only, print-first Master Schedule for a selected
  week. It combines Precepting, Late Doc, Interviews, Rounding, Community
  Precepting, Vacation, Academic Events, and Holiday closures. Its local
  **Work Remaining** view shows organization-wide actionable work, while
  **Lead Times** lets Scheduling Administrators set when each work family needs
  attention.
- **Precepting** provides a month calendar, chronological list, and focused
  slot editors opened directly from the month, returning there after save,
  removal or cancel. Direct editors return to the assignment date's month.
  Session findings and history remain in the editors; week findings remain in
  List and focused editors with access to Rounding. The month calendar's week
  date-row gutters have no warning or status badges. Assignment, session, and
  Late Doc findings stay beside their slots, and the month Work remaining
  summary retains its counts. Work Remaining opens the relevant month.
  Above the calendar, the selected month's last edit appears in America/Chicago
  time, independently of physician focus. Untouched months show `no edits yet`.
  This is the retained time of a calendar-changing edit, not the viewing time;
  it remains after assignments are removed or findings age out.
  The current month **PDF** includes that same last-edit value with CST/CDT and
  America/Chicago context, or `no edits yet`, separately from its generation time.
  Downloading again or filtering physicians does not change the recorded value.
  Schedulers maintain primary, optional secondary, and
  Monday Late Doc assignments and review availability and findings. **Schedule
  Bot** creates explainable Fill or Repair proposals for human review; applying
  a proposal changes the current schedule.
- **Rounding** records one normal rounder per Monday-Sunday week and
  effective-dated casual-rounder recurrence rules. Calendar and list views show
  the resolved schedule.
- **Vacation** records whole-day, AM-only, or PM-only absences. Its
  **Holidays** view maintains the clinic holiday occurrences that close affected
  Precepting, Community Precepting, Interview, and Late Doc coverage.
- **Academic Events** maintains annual event definitions and their dated,
  participant-based occurrences. **Community Precepting** maintains expected
  recurring commitments, distinct from authoritative Precepting assignments.
- **Interviews** manages academic-year AM Interview shifts and their Faculty
  assignments.
- **Physicians** is the identity-centered workspace for roles, Faculty
  Schedule templates, monthly availability rules, Community Preceptor rules,
  and Casual Rounder rules.
- **Reports** provides date-range schedule workload reports plus Community
  Preceptor and Late Doc stipend reports. Scheduling Administrators maintain
  the effective-dated stipend rates.

Muriel records retained change history for scheduling facts and assignments.
The canonical product decisions, including open domain questions and deferred
direction, are in [docs/design.md](docs/design.md). Operational solver detail
is in [docs/precepting-solver.md](docs/precepting-solver.md). The maintained
[documentation map](docs/README.md) links the current architecture, production
runbooks, and historical audits.

## License

Muriel's first-party source is licensed under the [MIT License](LICENSE).
Bundled PDF fonts are separately licensed under the
[SIL Open Font License 1.1](schedules/fonts/LICENSE-OFL.txt); see their
[provenance records](schedules/fonts/README.md). MIT does not replace those
terms or the licenses of third-party dependencies.

## Local development

Create a virtual environment and install the pinned runtime and development
dependencies:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r requirements-dev.txt
```

Run the development server from the canonical `main` worktree:

```sh
scripts/start-dev
```

The launcher uses that worktree's `.venv`, applies pending migrations, and runs
the foreground server at <http://127.0.0.1:8000/> by default. It does not create an
account or seed/reset data. Set `PRECEPTOR_BIND_ADDRESS` to listen elsewhere
(port remains 8000). Nondefault access also needs an appropriate explicit
comma-separated `PRECEPTOR_ALLOWED_HOSTS` list; selecting a bind address does
not change Django's trusted hosts. Development settings trust only `localhost`
and `127.0.0.1` by default. `PRECEPTOR_ALLOWED_HOSTS` replaces that list, trims
whitespace and ignores blank entries; empty lists and wildcard (`*`) entries
are refused. For example, on a host reachable as `dev.example`:

```sh
PRECEPTOR_BIND_ADDRESS=0.0.0.0 \
PRECEPTOR_ALLOWED_HOSTS=dev.example \
scripts/start-dev
```

`scripts/start-dev` and `scripts/reset-demo` intentionally refuse disposable
worktrees. Use `scripts/start-preview WORKTREE` to serve a disposable worktree
against an isolated copy of the main database at <http://127.0.0.1:8001/> by
default. It uses the main worktree's `.venv`, migrates only the temporary copy,
and removes its runtime storage when the foreground preview stops. Set
`PRECEPTOR_BIND_ADDRESS` to listen elsewhere (port remains 8001). Remote access
also needs an appropriate explicit `PRECEPTOR_ALLOWED_HOSTS` list; selecting a
bind address does not change Django's trusted hosts. For example, on a host
reachable as `preview.example`:

```sh
PRECEPTOR_BIND_ADDRESS=0.0.0.0 \
PRECEPTOR_ALLOWED_HOSTS=preview.example \
scripts/start-preview /path/to/worktree
```

Both launchers use the same development allowed-host policy. These are local
development workflows, not deployment or public-release guidance.

## Demonstration data

To replace all local development data with the deterministic synthetic demo
dataset, run this destructive command from the canonical `main` worktree:

```sh
scripts/reset-demo --confirm
```

The reset refuses production mode and any database other than that worktree's
`db.sqlite3`. It runs migrations, deletes application and authentication data,
and seeds 10 Faculty and 3 Community Preceptors for August 1, 2026 through July
31, 2027. The fixture includes availability templates and rules, Vacation,
Academic Events, Rounding, Community Precepting, incomplete and complete August
and September Precepting drafts, and a 15-shift October-January Interview
season. It is synthetic UI and query data, not scheduling policy.

The reset creates this ordinary scheduler account when it does not already
exist:

```text
username: demo-scheduler
password: demo-password
```

It also creates a `demo-scheduling-administrator` account with the Scheduling
Administrators group; establish its password through Django Admin if needed.

To retain existing Django login accounts and authority while replacing every
Muriel domain record, first create an administrator if necessary, then use:

```sh
.venv/bin/python manage.py createsuperuser
scripts/reset-demo --confirm --except-logins
```

This still removes sessions, invitations, account history, Physician links,
Physicians, scheduling data, and retained domain history. All accounts must
sign in again.

To rebuild a migrated but empty local database without creating demo accounts
or scheduling data, use:

```sh
scripts/reset-demo --confirm --empty
```

To retain existing Django login accounts and authority while leaving the domain
empty, use:

```sh
scripts/reset-demo --confirm --empty --except-logins
```

Both empty modes still remove sessions, invitations, account history, Physician
links, Physicians, scheduling data, and retained domain history.

## Synthetic history reset

To replace only the local SQLite scheduling domain with the multi-year synthetic
scenario while retaining Django login records, groups, permissions, and password
hashes, run this from the canonical `main` worktree. The actor must be an active
superuser; name it when more than one exists.

```sh
scripts/reset-synthetic --confirm --actor existing-admin --seed demo-2026
```

The scenario imports the bundled demonstration roster of 16 fictional Faculty
and creates three generated Community Preceptors. It starts in AY 2020-2021 and
completes Precepting through the organization-local current calendar month.
Every later attention-visible Precepting and Late Doc slot remains open.
Generation retains ordinary source and assignment change history. Current assignments are the schedule; current month
PDFs and reports read those facts. Publication storage and its service have been
removed through Django migration 0059, preserving ordinary scheduling data and
history. Schedule Bot retains proposal revisions, fingerprints, and human Apply
checks through the unchanged shared scheduling authority.

This command refuses production settings, non-SQLite databases, non-canonical
worktrees, missing confirmation, and ambiguous actors. It does not call or fall
back to `reset_demo`. Its final output gives generated counts, elapsed time,
database size, direct historical validation, and the remaining future work.

## Faculty roster import

`data/faculty_template_roster.csv` is demonstration input with 16 visibly
fictional Faculty identities and varied ten-slot weekly templates, including
Rounds selections. It is not current clinic data or scheduling policy.
A Scheduling Administrator can first preview its exact creates, reuses, and
blockers without changing the database:

```sh
.venv/bin/python manage.py import_faculty_template_roster \
  data/faculty_template_roster.csv \
  --effective-start 2026-08-01 \
  --scheduler SCHEDULER_USERNAME
```

After reviewing the preview, apply the same bounded import explicitly:

```sh
.venv/bin/python manage.py import_faculty_template_roster \
  data/faculty_template_roster.csv \
  --effective-start 2026-08-01 \
  --scheduler SCHEDULER_USERNAME \
  --apply
```

Replace `SCHEDULER_USERNAME` and the effective date with the intended active
Scheduling Administrator and start date. You can supply your own roster CSV
with the same columns and supported slot values; fictional labels are example
contents, not an import requirement. The command never alters an overlapping
role or template; it reports those facts as blockers.

## Vacation calendar import

Scheduling Administrators can open **Vacation** > **Import** to upload the
workbook, preview its interpretation without changing data, or apply the
validated import. The page shows the workbook window, matched Faculty, exact
Vacation creates/reuses/removals, ignored event and interview markers, warnings,
and blockers. Replacement is a separate explicit option.

The legacy July-through-June Excel vacation calendar can be previewed without
changing the database:

```sh
.venv/bin/python manage.py import_vacation_calendar \
  "/path/to/Vacation Calendar 2026-2027.xlsx" \
  --scheduler SCHEDULER_USERNAME
```

The importer reads only the workbook's `FACULTY:` sections. It converts `V`
day markers into whole-day Vacation ranges, ignores residents, and reports but
does not import `E` event or `I` interview markers because the workbook does
not identify those source facts sufficiently. After reviewing the exact
creates, reuses, warnings, and blockers, apply the same import explicitly:

```sh
.venv/bin/python manage.py import_vacation_calendar \
  "/path/to/Vacation Calendar 2026-2027.xlsx" \
  --scheduler SCHEDULER_USERNAME \
  --apply
```

If the workbook becomes authoritative, preview with `--replace`. Replacement
synchronizes vacations only for matched Faculty and only inside the workbook's
12-month window. It retains exact matches, creates new ranges, and removes
stale in-window ranges with normal Vacation history. It refuses a Vacation
that crosses the workbook boundary. Review the replacement preview, then use
both `--replace` and `--apply` to write it.

## Precepting workbook import

Scheduling Administrators can use **Precepting** > **Import Schedule** to preview a
calendar-year workbook and explicitly apply its reviewed assignments. Names
match normalized Physician full names, including `Last, First` input. An
otherwise unmatched one-word name may match a unique surname. Missing or
ambiguous matches block import; the importer does not infer nicknames or ship
clinic-specific name aliases.

## Accounts and invitations

Create the first non-demo administrator with Django's built-in command:

```sh
.venv/bin/python manage.py createsuperuser
```

Superusers use **Accounts** for ordinary scheduler usernames, official email,
activation, Scheduling Administrator membership, explicit Physician links, and
scheduler invitations. Staff/superuser authority, passwords, and Django Admin
remain Django Admin's break-glass boundary. The Scheduling Administrators group
is required to create Physicians and change their identities or effective roles;
it does not grant Django Admin access.

**Accounts** > **Invite scheduler** creates an inactive ordinary account. The
recipient sets its first password through a single-use invitation, which expires
after seven days by default. In development or demonstrations, set
`PRECEPTOR_SCHEDULER_INVITATION_DELIVERY_MODE=manual` to display a copyable
setup link. Set it to `smtp` only with Django email configured. Pending
invitations may be resent or revoked; resending invalidates earlier links.

## Checks and formatting

Run the deterministic project check:

```sh
scripts/check
```

It verifies Ruff, Django checks, browser interaction tests, and the parallel
Django test suite with `preceptor_project.test_settings`. It excludes the
deterministic, representative reset-tool and multi-year synthetic-scenario
integration cases; run them when changing those owners:

```sh
scripts/check-reset-tool-integration
scripts/check-synthetic-scenario-integration
```

Each command uses only a disposable Django test database and fixed test inputs.
To run a focused Django test module with the same settings:

```sh
DJANGO_SETTINGS_MODULE=preceptor_project.test_settings \
  .venv/bin/python manage.py test schedules.tests.test_views
```

Apply Ruff fixes and formatting with `scripts/format`; use the non-mutating
`scripts/quality-check` for Ruff, Django system, and migration-drift checks.

## Dependency updates and security monitoring

GitHub Actions runs the clean-environment checks for pull requests, pushes to
`main`, and manual dispatch. Its separate **Dependency security** check runs
the pinned `pip-audit` tool against the application dependencies declared in
`requirements.txt`; it resolves and audits their dependency closure and fails
when a known vulnerability is found. It does not audit whichever packages
happen to be installed in the runner or a developer's environment.

After installing the development dependencies, run the same audit locally:

```sh
scripts/security-audit
```

Dependabot opens weekly grouped updates for the root Python requirements and
GitHub Actions dependencies. A repository maintainer must enable GitHub Actions
and, to receive security-update pull requests in addition to version updates,
enable Dependabot alerts and Dependabot security updates in the repository's
**Settings** security configuration. This repository configuration does not
enable automatic merging or change GitHub settings itself.

## Production and operator documentation

Production is a separate PostgreSQL deployment, not an extension of the local
SQLite launcher or demo reset. See [docs/production.md](docs/production.md) for
the runtime contract and [docs/deployment.md](docs/deployment.md),
[docs/backup-recovery.md](docs/backup-recovery.md), and
[docs/password-recovery.md](docs/password-recovery.md) for operator guidance.
