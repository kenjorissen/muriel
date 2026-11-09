# Domain and application ownership health audit

**Status:** Historical source audit (2026-08-26), reconciled to the
then-current implementation on 2026-08-31. Publication workflow was retired on
2026-09-11, so the publication and Publish-form observations below are retained
audit evidence, not current product behavior. The maintained current owner map
is [`scheduling-services.md`](../scheduling-services.md). This is not an
accepted refactoring plan.

## Scope and conclusion

Muriel has clear owners for its retained scheduling facts, effective
availability, schedule-specific policy, mutations, history, publication,
reports, work projection, and Schedule Bot proposals. The principal dependency
direction is sound: source facts and intrinsic validation feed availability;
the bounded slot projection feeds the scheduling consumers; authenticated
services own writes and retained history; forms and views adapt those owners to
HTTP. The application deliberately retains a few concrete cross-domain reads,
not a generic scheduling abstraction.

The original audit's primary input-capacity risk is resolved: Schedule Bot now
accepts at most three calendar months in a solve range and 24 in a balance window,
with validation before structural session initialization. Its original
organization-date bypass inventory is also resolved: organization-local policy
and defaults use the injectable `organization_today()` owner. The dated
observations and their reconciliation are retained below.

The remaining lower-consequence boundary is retained history: ordinary change
rows are append-only in application paths but not technically immutable like a
published revision.

## Evidence and current map

The canonical design defines source data, resolution, editable draft, review,
and immutable publication as separate lifecycle stages
([`design.md` sections 4, 6, 7.2, and 15](../design.md)). The maintained
service-owner map is [`scheduling-services.md`](../scheduling-services.md).
The references below name the current source symbols rather than historical
monolith locations.

| Boundary | Current owner and source reference | Responsibility |
| --- | --- | --- |
| Retained source models | `models/physicians.py`: `Physician`, `EffectiveRole`, `FacultyAvailabilityTemplate`, `MonthlyPhysicianAvailabilityRule`; `models/vacations.py`: `Vacation`; `models/academic_events.py`, `rounding.py`, and `community_preceptors.py` | Source-specific facts and intrinsic date, role, recurrence, and overlap validation. |
| Source mutations and history | `services/physicians.py`: `correct_faculty_availability_template`; `services/vacations.py`: `correct_vacation`; corresponding source services and `*Change` models | Authenticated mutations, record validation, current-fact write, and prior/resulting-value history. |
| Organization-local time | `services/operational_attention.py`: `organization_today`, `is_operational_date`, `is_operational_preceptor_week` | The authoritative Chicago date and operational/historical classification. |
| Effective availability | `services/availability.py`: `_compose_effective_preceptor_availability` | Single-physician Tier 0 availability, eligibility, qualification, and source explanation. |
| Bounded projection | `services/slot_projection.py`: `RequestedSlot`, `slot_projection`, `_slot_projection_source_facts` | Loads Tier 0 sources and current Precepting/Interview occupancy once for a caller-selected set of slots. |
| Precepting draft and history | `services/preceptors.py`: `initialize_preceptor_periods`, `assign_preceptor`, `remove_preceptor_assignment`, `_draft_findings_for_sessions`; `models/preceptors.py`: `PreceptorAssignmentChange` | Structural weekday sessions, editable assignments, findings, alternatives, locks, and assignment history. |
| Late Doc and Interviews | `services/late_docs.py`: `assign_late_doc`; `services/interviews.py`: `assign_interview_shift` | Separate schedule policy and history, consuming the projection rather than merging schedules. |
| Publication | `services/publication.py`: `publication_fingerprint_for_week`, `publish_week`; `models/publication.py`: `PublishedWeekRevision` | Current weekly validation/fingerprint and immutable published snapshot/revision. |
| Schedule Bot | `services/precepting_solver.py`: `validate_precepting_solver_capacity`, `preflight_precepting_solve`, `retain_fill_proposal`, `edit_proposed_assignment`, `reconcile_precepting_solver_proposal`, `apply_precepting_solver_proposal`; `models/solver.py`: proposal and review records | Three-calendar-month solve and 24-calendar-month balance validation before structural initialization, read-only solving, retained and human-edited proposal state, stale detection/reconciliation, and delegation to ordinary mutations. |
| Reports and cross-schedule read views | `services/schedule_reports.py`: `*_schedule_report`; `services/stipend_reports.py`: `monthly_stipend_report`; `services/master_schedule.py`: `master_schedule_week` | Read-only range and weekly projections. |
| Publication/work attention | `services/work_remaining.py`: `ProjectedWorkItem`, `global_work_remaining`; `services/lead_times.py`: `set_lead_time` | Immutable projection vocabulary and audited lead-time policy. |
| HTTP and submitted values | `forms/<domain>.py`; `views/<domain>.py` | Forms validate representations and trusted choices; views authenticate, choose request context, call named owners, and build presentation values. |

The mutation guard in `services/_mutation.py` is intentionally a narrow shared
mechanism, not a policy owner. `scheduling_mutation` acquires the singleton
`SchedulingMutationAuthority` row in an atomic block, with a same-process lock
for SQLite development. `require_scheduler` and
`require_scheduling_administrator` centralize the current authorization rules.

## Representative operation traces

### Effective-dated Faculty Schedule change

1. `forms/physicians.py` binds submitted slots and dates; the view calls
   `services.physicians.correct_faculty_availability_template`.
2. The service is wrapped by `scheduling_mutation` and `transaction.atomic`.
   It normalizes retained schedule-option selections, rejects unusable
   current/future selections, changes the template, and calls `full_clean()`.
3. `FacultyAvailabilityTemplate.clean()` rejects malformed slot maps, reversed
   ranges, and overlapping template ranges. The service writes a
   `PhysicianChange` with old and resulting values in the same transaction.
4. Future reads select the record effective on the requested date through
   `availability` and `slot_projection`; they do not alter assignments or any
   published revision. The published-week snapshot already contains the source
   facts it used.

### Historical correction

1. A Vacation edit reaches `services.vacations.correct_vacation`; the caller
   needs only scheduler authentication, as the design permits scheduler-recorded
   past corrections.
2. The service enters the scheduling serialization boundary, locks the Vacation
   row with `select_for_update()`, validates the inclusive range and model,
   writes the corrected current fact, and creates a `VacationChange` with the
   previous and resulting values, note, actor, and timestamp.
3. `remove_vacation` records a removal change before deleting the current
   Vacation row. `VacationChange.vacation` becomes null on deletion but retains
   `vacation_id_at_recording`, physician, and value snapshots. The same pattern
   is used for removable recurrence and assignment facts.
4. This correction changes the current account of the past; it does not mutate
   `PublishedWeekRevision`. The historical operational views intentionally stop
   recalculating live findings, while retained facts and history remain
   inspectable.

### Precepting assignment and publication

1. `services.preceptors.open_weekly_draft` ensures the exact weekday AM/PM
   structural sessions under the scheduling serialization boundary.
2. `assign_preceptor` authenticates, rejects operational holiday closures,
   locks all session assignments, rejects duplicate positions/physicians and
   ineligible secondary roles, writes the assignment, and records
   `PreceptorAssignmentChange`. Availability is not made a hard write-time
   constraint; the service preserves an invalid but explainable draft where the
   accepted policy permits it.
3. The Publish form carries the digest from
   `publication_fingerprint_for_week`. `publish_week` re-enters the serialized
   boundary, locks the ten sessions and Monday Late Doc observation, recomputes
   the projection and findings, and rejects a changed digest or operational
   hard findings.
4. A locked latest revision makes identical publication idempotent. Otherwise
   `PublishedWeekRevision` stores the self-contained snapshot. Its model blocks
   later `save()` and `delete()` for an existing row.

### Retained solver proposal, edit, reconciliation, and apply

1. `views/solver.py:solver_workspace` validates a selected normalized solve
   range and read-only balance window against the six- and 24-calendar-month
   capacity before `initialize_preceptor_session_range` can create structural
   rows. `preflight_precepting_solve` repeats the validation for every service
   and retained-proposal path, then reads the values into `PreceptingSolverInput`,
   including bounded slot rows, assignments, Late Doc positions, source
   versions, and a digest.
2. `produce_fill_proposal` or `produce_repair_proposal` makes no authoritative
   write. `retain_fill_proposal` stores the input snapshot, selected changes,
   evidence, workload effects, seed, and pending Late Doc reviews in
   `PreceptingSolverProposal` records.
3. `edit_proposed_assignment` and `review_proposed_late_doc` lock the proposal,
   require its optimistic `revision`, reject edits after apply or in the past,
   and increment the revision only after recalculating proposal evidence.
4. A stale proposal cannot apply: `_current_proposal_input` recomputes preflight
   and compares the input fingerprint. `reconcile_precepting_solver_proposal`
   locks the source proposal, compares its starting/current/proposed position
   values, requires a human choice for conflicts, and retains a new proposal and
   reconciliation provenance without changing the draft.
5. `apply_precepting_solver_proposal` runs inside `scheduling_mutation`, locks
   the proposal, revalidates source choices, date authority, hard-finding
   confirmation, and every Late Doc review. It delegates assignment writes and
   history to `assign_preceptor`, `remove_preceptor_assignment`,
   `assign_late_doc`, and `remove_late_doc_assignment`; it only then records the
   proposal as applied. It never publishes.

### Report

1. `views/reports.py` passes GET values to
   `schedule_reports.schedule_report_parameters`, which validates the schedule,
   period, coverage, date range, and organization-local current-date split.
2. The named report service uses range-filtered querysets and returns immutable
   dataclasses. For example, `precepting_schedule_report` delegates its shared
   projection, `academic_event_schedule_report` prefetches participants, and
   `stipend_reports.monthly_stipend_report` joins current assignments/rates.
3. Reports do not write schedule facts, history, publication, or work state.
   They report the current corrected account for their selected coverage; an
   issued published artifact instead reads its retained snapshot.

## Dependency, cohesion, and policy assessment

`availability` is the only single-slot composition owner. `slot_projection`
loads that composition for a bounded slot set and adds only the two accepted
sibling occupancy facts. Precepting, Late Doc, Interviews, publication, Master
Schedule, reports, and the solver consume it while retaining their own rules.
This is an appropriate concrete dependency direction, not a repository or rule
engine abstraction.

The reviewed service graph contains intentional consumer imports rather than a
service-package convenience API: the package root exports no services. The
solver imports Precepting mutation functions only at apply time to avoid a
module cycle and to preserve the ordinary mutation/history owner. Master
Schedule and reports use Precepting findings as consumers. No reviewed path
imports forms or views from a service, and no form/view writes a retained
scheduling fact directly.

`precepting_solver.py` is large, but size alone is not a finding. Its input
projection, deterministic solve, retained proposal, review, reconciliation, and
apply behavior form one human-reviewed proposal boundary. Splitting it solely
by technical phase would add APIs without reducing its dependence on the
concrete Precepting and Late Doc contracts. Its concrete concerns are the
explicit capacity boundary and continued focused stale/reconciliation tests.

There is some intentionally repeated domain knowledge: availability source
ordering is rendered by Precepting; Master Schedule adapts source and finding
facts to presentation; reports use source-specific measures. These consumers
answer different schedule questions and do not establish a competing source of
truth. The work-item dataclasses are shared only for accepted producers, which
keeps completeness policy out of the views.

## Correctness and persistence controls

- **Local date authority:** `operational_attention.organization_today` uses
  `America/Chicago`; tests cover the midnight boundary and the Friday end of a
  Precepting workweek in `tests/test_operational_attention.py`. Organization
  policy and defaults, including physician/category guards, Academic Event form
  defaults, and Schedule Bot workspace defaults, call this owner. Diagnostic or
  display timestamps, account expiry, and deliberately supplied test timestamps
  have distinct semantics and do not need to use it.
- **Schedule Bot capacity:** `validate_precepting_solver_capacity` allows at
  most three calendar months in a solve range and 24 in a balance window.
  `solver_workspace` validates before structural initialization and preflight
  repeats the validation. `PreceptingSolverInputTests.test_capacity_uses_calendar_month_boundaries`
  and `PreceptingSolverWorkspaceTests.test_oversized_solve_is_rejected_before_initialization_or_preflight`
  cover the direct boundary and no-initialization rejection behavior.
- **Effective ranges:** role, Faculty template, monthly availability, Community
  recurrence, and Casual Rounding models validate inclusive ordering and their
  applicable non-overlap rules. The bounded projection queries effective rows
  using the requested first/last dates before composing each slot.
- **History and destructive operations:** current removable facts record a
  change before deletion and history holds the former identifier and value.
  Physician deletion is rejected. Admin exposes retained scheduling records as
  read-only/no-add/no-delete. Published revisions are model-immutable.
- **Serialization and lost updates:** source and assignment mutations use the
  shared scheduling transaction; mutable target rows are commonly read with
  `select_for_update()`. Publication locks sessions, Late Doc observation, and
  revisions before digest comparison/revision allocation. Proposal edits,
  reconciliation, and apply lock the proposal and use its optimistic revision
  or input fingerprint. Nested decorated assignment services remain within the
  outer apply transaction.
- **Stale handling:** publication compares the reviewed digest inside its
  serialized operation. Schedule Bot compares a current preflight fingerprint
  before apply and uses an explicit three-way reconciliation rather than
  silently overwriting an ordinary draft.

## Query and materialization assessment

The evidence is query-shape based and includes project query contracts; the
available demo reset seeds one academic-year horizon (2026-08-01 through
2027-07-31), 13 physicians, source changes, recurrence, two draft months, and
interviews. It is useful for representative UI inspection but is not a
multi-year volume benchmark.

- `slot_projection` performs source-class range queries once, groups them in
  memory, and emits exactly `requested slots * all recorded physicians` rows.
  `test_slot_projection_contract.py` asserts at most 12 queries for two slots
  with mixed sources and at most 11 with Tier 1 occupancy.
- The publication contract projects the workweek once and calls the holiday
  projection once; the same test bounds the observed week and publication paths
  at 25 and 45 queries. This guards fan-out while preserving source-sensitive
  snapshot content.
- `master_schedule_week` requests fourteen slots once and has a 23-query bound
  for both sparse and populated weeks in `test_master_schedule.py`; its direct
  related reads use `select_related`/`prefetch_related`.
- Monthly stipend reports have two- and three-query contracts in
  `test_stipend_reports.py`. Schedule reports range-filter assignments/events,
  prefetch event participants, and deliberately enumerate only their selected
  report period.
- No N+1 pattern was found in these reviewed paths. The known cost is linear
  in the requested slot count times physician count; the Schedule Bot's
  three-calendar-month solve and 24-calendar-month balance limits now bound that
  cost at its input boundary.

## Reconciled source-audit findings

**D1, historical observation — resolved by task 000365.** The 2026-08-26 audit
found that `forms/solver.py` accepted arbitrary custom start/end dates and only
a positive custom balance day count; `views/solver.py:solver_workspace` called
`initialize_preceptor_session_range` before preflight; and preflight expanded
every weekday in both ranges. That evidence remains the reason for the current
boundary. `MAX_SOLVE_CALENDAR_MONTHS = 3` and
`MAX_BALANCE_CALENDAR_MONTHS = 24` now govern normalized input through
`validate_precepting_solver_capacity`. The workspace validates before
initialization and preflight validates again; the direct service and workspace
tests named above protect calendar-month edges and reject excess input without
sessions, proposals, or history.

**D2, historical observation — resolved by task 000366.** The 2026-08-26
inventory identified direct/duplicate reads in physician/category policy and
presentation defaults, including host-local `date.today()` in the Schedule Bot
workspace and Academic Event form. Those policy/default paths now use
`organization_today()`. The owner remains intentionally narrow: diagnostic and
display timestamps, account expiry, and test-supplied timestamps are unrelated
time concepts, not bypasses. `OrganizationTodayTests.test_chicago_midnight_changes_the_operational_date` and
`AcademicEventDefinitionTests.test_form_defaults_follow_the_organization_date_at_chicago_midnight`
directly exercise the owner and a dependent default.

## Unresolved finding

| ID | Consequence | Confidence | Evidence and concern | Plausible behavioral boundary |
| --- | --- | --- | --- | --- |
| D3 | Low: privileged application or datastore access can alter or remove retained `*Change` history and published snapshots, weakening audit evidence. | High | `PublishedWeekRevision.save` and `.delete` reject updates/deletes through a model instance. Current `*Change` mutation paths append prior/recorded values, source removal retains snapshots/source IDs, and `RetainedPhysiciansRecordAdmin` disallows admin writes. These are application-level protections: model-instance protection for published revisions and ordinary mutation-path protection for change rows. They are not datastore-enforced immutability for either record class: `QuerySet.update()`/`.delete()` bypass instance methods, and direct datastore access bypasses Django. Migration `0031_faculty_role_vocabulary` demonstrates the former by bulk-updating published revision snapshots and digests. No current user-facing bypass was found. | Decide whether retained history and published snapshots require technical immutability beyond their application-path protections. If so, define a scoped write authority or datastore enforcement that covers both record classes and an explicit exceptional correction/migration path; do not introduce a generic audit subsystem. |

## Reconciliation with earlier audits and design

The previous [`scheduling-domain-ownership.md`](scheduling-domain-ownership.md)
is **resolved/superseded** for its historical monolith observations: models,
forms, views, and services are now subject-owned packages; the named service
map and `slot_projection` are current. Its still-relevant cautions remain
applicable: protect source-sensitive availability/publication/solver query and
result contracts, keep sibling schedule occupancy narrow, and test shared
presentation effects directly.

[`architecture-roadmap.md`](architecture-roadmap.md) is likewise
**resolved/superseded** for its proposed extraction and absent-Master-Schedule
observations. Its explicit deferrals remain current: this audit does not imply a
generic schedule model, rule engine, repository layer, materialized
availability/cache, plugin system, service split, or autonomous assigner.

The current canonical design is **still applicable**: local Chicago time,
effective-date rules, prior-value history, editable invalid drafts with
findings, immutable weekly publication, and human-reviewed Schedule Bot
proposals all have identifiable implementation owners. D1's accepted capacity
policy and D2's organization-date alignment now implement that direction; D3
remains an enforcement-clarity gap without a new product or compliance
requirement.
