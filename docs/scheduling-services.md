# Scheduling service owners

`schedules.services` contains the application's scheduling owners. Import a
named module directly; its package root intentionally exports no service API.
`schedules.models` is the Django-discovery exception: it re-exports concrete
models while their definitions remain in domain modules. The `forms` and
`views` package roots do not re-export domain surfaces.

## Current owner map

| Owner | Retained facts or read/mutation responsibility | Direct HTTP/form surface and focused evidence |
| --- | --- | --- |
| `accounts` | Scheduler invitations, account identity, and account history | `forms.accounts`, `views.accounts`; `test_account_administration`, `test_scheduler_invitations` |
| `physicians` | Physicians, effective roles, Faculty templates, monthly availability rules, and their history | `forms.physicians`, `views.physicians`; `test_physicians`, `test_monthly_availability_rules` |
| `availability_categories` | Custom availability categories, assignments, lifecycle, and history | `forms.physicians`, `views.physicians`; `test_availability_categories` |
| `vacations` | Vacation reads, lifecycle mutations, and history | `forms.vacations`, `views.vacations`; `test_services`, `test_views` |
| `academic_events` | Definitions, annual event decisions and participants, lifecycle, history, and Academic Year Calendar definition import | `forms.academic_events`, `views.academic_events`; `test_academic_event_definitions`, `test_import_academic_event_definitions`, `test_annual_academic_event_lifecycle` |
| `rounding` | Normal and casual Rounding recurrence, projections, lifecycle, and history | `forms.rounding`, `views.rounding`; `test_services`, `test_projected_rounding_and_interview_work` |
| `community` | Community Preceptor recurrence, derived commitments, lifecycle, and history | `forms.community_preceptors`, `views.community_preceptors`; `test_services` |
| `holidays` | Clinic-holiday defaults and occurrences, closure projection, scheduling, history, and holiday work items | `forms.holidays`, `views.holidays`; `test_holiday_occurrences`, `test_holiday_closures`, `test_holiday_workspace` |
| `availability` | Single-slot Tier 0 effective availability and its source explanation | Consumers use it through `slot_projection`; `test_monthly_availability_rules` |
| `slot_projection` | Bounded all-physician Tier 0 rows plus current Preceptor/Interview occupancy | Scheduling read owners; `test_slot_projection_contract` |
| `preceptors` | Draft initialization, Preceptor assignments, alternatives, findings, weekly work items, and assignment history | `forms.preceptors`, `views.preceptors`; `test_preceptors`, `test_lazy_session_initialization` |
| `late_docs` | Monday PM Late Doc candidates, assignments, findings inputs, and history | `forms.late_docs`, `views.late_docs`; `test_late_docs` |
| `interviews` | Interview shifts, candidate/finding policy, lifecycle, history, and work items | `forms.interviews`, `views.interviews`; `test_interview_schedule`, `test_interview_workspace` |
| `operational_attention` | Organization-local date and current operational-range boundary | Read by work, schedule, and solver owners; focused tests inject this boundary where needed |
| `lead_times` | Audited lead-time settings and changes | `forms.lead_times`, `views.lead_times`; `test_lead_times`, `test_configured_lead_time_projections` |
| `work_remaining` | Shared immutable work-item vocabulary and global/scoped work summaries | `views.work_remaining`; `test_work_remaining`, `test_global_work_remaining` |
| `schedule_reports` | Read-only date-range schedule reports and report findings | `views.reports`; `test_schedule_reports` |
| `stipend_rates`, `stipend_reports` | Rate history and read-only Community Preceptor/Late Doc stipend reports | `forms.stipend_rates`, `views.stipend_rates` and `views.reports`; `test_stipend_rates`, `test_stipend_reports` |
| `master_schedule` | Read-only weekly cross-schedule projection | `views.master_schedule`; `test_master_schedule`, `test_master_schedule_workspace` |
| `precepting_solver` | Schedule Bot capacity validation (at most three calendar months for a solve range and 24 for a balance window), solving, retained proposals, review, reconciliation, and apply operations | `forms.solver`, `views.solver`; `PreceptingSolverInputTests.test_capacity_uses_calendar_month_boundaries`, `PreceptingSolverWorkspaceTests.test_oversized_solve_is_rejected_before_initialization_or_preflight`, `test_precepting_solver`, `test_precepting_solver_workspace` |

Private helpers remain deliberately narrow: `_mutation` owns shared scheduling
and scheduling-administration serialization/guards, and `_recurrence` owns
pure recurrence mechanics. Neither is a domain policy owner or a public service
API.

The shared authority model lives in `schedules.models.mutation`; moving
it there preserves the existing table and singleton row. Scheduling writes,
coherent reads, acknowledgement retirement, and solver Apply use this same
authority. `test_scheduling_mutation`, `test_scheduling_exceptions`, solver
tests, and the storage-removal migration test protect these obligations.

## Dependency and authority boundaries

Models own retained facts and intrinsic validation. Source services own their
domain reads, mutations, and domain change rows. `availability` composes source
facts for one slot; `slot_projection` loads those facts and Tier 1 occupancy for
a caller-bounded set of slots. Preceptor, Late Doc, Interview, Master Schedule,
reports, and Schedule Bot consume those reads without owning a
generic rule framework or a second source of truth.

```text
domain models and source services
             |
availability -> slot projection -> schedule-specific policy and findings
             |                         |
     calendars, Master Schedule, reports, work remaining, Schedule Bot
                                       |
                    authenticated mutations, history, proposals
```

`holidays` is a source boundary rather than a calendar-only feature. Its
projection is consulted by calendars, availability consumers, Master Schedule,
reports, work remaining, and Schedule Bot. Default closures
are projected without a database write; the holiday workspace is the explicit
occurrence writer.

`lead_times` owns organization-wide advance-notice settings. `work_remaining`
owns the shared projected-item shape and aggregates only the currently accepted
producers: Holidays, Academic Events, Precepting, Normal Rounding, and
Interviews. Vacation entry, Community recurrence, Casual Rounding, physician
setup, and report/rate administration deliberately do not assert a completeness
schedule and are not global work producers.

For operators, the two work policies answer different questions. `Schedule >
Work Remaining` uses `current-attention`: an unresolved item appears only when
its configured lead time has reached its domain-provided `matters_on` date and
remains operationally current. A workspace's explicitly selected period uses
`focused-range`: it shows unresolved current or future facts in that period,
including facts that have not yet reached global attention. `retained-history`
is correction context only; historical facts and correction affordances remain
available without contributing to either operational count. `work_remaining`
owns membership and quantity rollups, while domains own their affected and
actionable ranges, reasons, targets, and lead-time anchors.

The same owner exposes `focused_range_has_membership` for workspace detail
rows. Precepting, Normal Rounding, and Interview consumers use that operation
instead of local organization-date/range comparisons; this keeps displayed
unresolved details aligned with the focused summary and its scheduling actions.

When investigating an unexpected count, inspect the domain's produced item and
its configured lead time first, then the projection selected by the consumer.
Do not add date-cutoff arithmetic, count aggregation, or substitute action
construction to a workspace view: focused workspace summaries and the global
view must consume the projection owner's membership and the item's target.

`master_schedule.master_schedule_week` is read-only. It combines slot rows with
direct Precepting, Late Doc, and Interview facts plus supporting Rounding,
Community Precepting, Vacation, Academic Event, and holiday facts. It neither
creates sessions nor mutates source schedules. `schedule_reports` and
`stipend_reports` are likewise read-only projections, not policy owners.

Schedule Bot is the retained-proposal boundary in `precepting_solver`, not an
autonomous assigner. Its workspace validates its normalized solve range and
balance window before structural session initialization: the maximums are three
and 24 calendar months respectively. It also validates the same policy at
preflight, so retained-proposal revalidation cannot bypass the boundary. It then reads a
bounded projection, produces a proposal for human review, records
proposal/review/reconciliation state, and applies only through the ordinary
scheduling mutation and history paths. Late Doc review remains explicit before
proposal apply.

## Presentation callers

Forms own submitted-value validation and trusted choice binding. Views own
request authentication, URL/query context, calls to named services, and local
presentation values. They do not recreate scheduling policy or write retained
facts directly. `views._shared`, `forms.shared`, `calendar_presentation`, and
`table_presentation` supply mechanics and immutable presentation values only;
they do not own domain rules.

Calendar domains provide `CalendarLattice` values and domain item partials to
the shared lattice. Workspace shells, ordinary content, forms, and data tables
use shared surface primitives only for presentation. See
[`audits/calendar-presentation-variants.md`](audits/calendar-presentation-variants.md)
for the current presentation boundary.
