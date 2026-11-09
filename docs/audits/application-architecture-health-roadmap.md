# Application architecture health roadmap

**Status:** Maintained current implementation reconciliation, 2026-08-31. It
preserves the 2026-08-26 source-audit evidence while recording findings
resolved by later accepted implementation. It does not accept a refactor,
change product behavior, or create planning work.

## Reading this record

The [domain and application ownership](domain-application-health.md) and
[Django web and presentation health](django-web-presentation-health.md) audits
retain their dated source evidence. This maintained record reconciles that
evidence to the current implementation; an observation marked resolved below
is historical evidence, not a current finding. The canonical design in
[`../design.md`](../design.md) remains the authority for accepted product
behavior.

- **Observed evidence** names a current source, test, or recorded measurement.
- **Inference** is a conclusion drawn from that evidence, not an accepted rule.
- **Recommendation** is a proposed, independently reviewable next action.
- **Accepted invariant** restates current canonical design or implemented
  authority; it is not newly accepted here.
- **Unresolved human decision** needs explicit product, capacity, or assurance
  direction before implementation.

Earlier [scheduling-domain-ownership](scheduling-domain-ownership.md),
[presentation-workspace-architecture](presentation-workspace-architecture.md),
[calendar-presentation-variants](calendar-presentation-variants.md), and
[architecture-roadmap](architecture-roadmap.md) records are point-in-time
evidence. Their extraction recommendations are completed or superseded; their
explicit deferrals and regression cautions remain relevant where stated.

## Current map

| Need to locate | Current owner | Direction and boundary |
| --- | --- | --- |
| Retained facts and intrinsic validation | `schedules/models/<domain>.py` and source-specific model services | Facts remain source-specific; effective dates and overlaps are validated with the fact. |
| Local time and operational history | `services/operational_attention.py` | `organization_today()` is the injectable Chicago-date authority. Operational projections do not accept a caller cutoff. |
| Effective availability | `services/availability.py` | One physician, one AM/PM slot, with explainable Tier 0 source composition. |
| Shared bounded scheduling read | `services/slot_projection.py` | Caller-selected slots become all-physician Tier 0 rows plus only current Precepting and Interview occupancy. |
| Policy, mutations, and retained history | Named domain services, especially `preceptors`, `late_docs`, `interviews`, and source services | Authenticated services keep authorization, serialization, validation, write, and change recording together. |
| Retained snapshots and proposals | `services/publication.py`; `services/precepting_solver.py` | Snapshot generation has no live consumer; synthetic/reset tooling uses ordinary history. Schedule Bot validates a solve range of at most three calendar months and a balance window of at most 24 before session initialization, then retains a reviewed proposal and delegates Apply to ordinary mutations. |
| Read projections | `master_schedule`, `schedule_reports`, `stipend_reports`, and `work_remaining` services | These consume facts and projections without becoming policy or mutation owners. |
| Submitted values and HTTP orchestration | `forms/<domain>.py`; `views/<domain>.py` | Forms validate representations and trusted choices; views authenticate, select context, invoke owners, and assemble display values. |
| Presentation and enhancement | Calendar/table presentation values and templates, workspace fragments, one CSS asset, focused `data-*` JavaScript | Shared mechanics are presentation-only. Domain templates and views retain content, navigation, and meaningful variants. |

```text
source models and source services
        -> availability (one slot)
        -> slot projection (bounded slots and sibling occupancy)
        -> schedule policy, findings, and read projections
        -> forms/views and presentation

authenticated domain mutations -> history
synthetic/reset tooling -> ordinary domain mutations -> history
Schedule Bot -> retained proposal -> ordinary mutations -> editable draft
```

**Accepted invariant:** The arrows describe concrete dependency and authority,
not a framework. Services do not import forms or views; views and forms do not
write retained scheduling facts directly. Availability, projection, and each
schedule's policy remain separate. See [`../scheduling-services.md`](../scheduling-services.md)
for the maintained owner map.

## Resolved source-audit findings

**C1, resolved — Schedule Bot input capacity.** The 2026-08-26 audit correctly
observed that custom solve and balance input was unbounded, initialization ran
before preflight, and an authenticated request could materialize every
physician for every requested weekday AM/PM slot. The accepted implementation
now sets `MAX_SOLVE_CALENDAR_MONTHS = 3` and
`MAX_BALANCE_CALENDAR_MONTHS = 24` in
`services/precepting_solver.py`. `validate_precepting_solver_capacity()`
enforces both normalized ranges. `views/solver.py:solver_workspace` calls it
before `initialize_preceptor_session_range`, while preflight calls it again for
proposal and retained-proposal revalidation paths. Direct evidence is
`PreceptingSolverInputTests.test_capacity_uses_calendar_month_boundaries` and
`PreceptingSolverWorkspaceTests.test_oversized_solve_is_rejected_before_initialization_or_preflight`;
the latter proves rejection creates no sessions, proposal, or assignment
history.

**H1, resolved — organization-local policy and defaults.** The dated audit's
inventory of bypasses was useful, but current policy/default reads use the
injectable `operational_attention.organization_today()` authority. This covers
physician and availability-category policy, Academic Event form defaults, and
Schedule Bot workspace defaults. `OrganizationTodayTests.test_chicago_midnight_changes_the_operational_date`,
`AcademicEventDefinitionTests.test_form_defaults_follow_the_organization_date_at_chicago_midnight`,
and the Schedule Bot default tests protect that boundary. This owner does not
claim unrelated diagnostic or display timestamps, account-expiry timestamps,
or deliberately supplied test timestamps; those have different meanings and
are not date-policy bypasses.

## Current open assessment and proposed actions

The classifications order consequence and leverage, not code size or aesthetic
uniformity. An item is not approved merely because it appears below.

| Rank | Finding and evidence | Recommendation, preservation strategy, and evidence | Prerequisites |
| --- | --- | --- | --- |
| **High-leverage ownership/reuse** | **H2: The solver comparison command is assembled from a display label.** The review template maps `Late Doc` to hidden protocol fields; the view and `_proposal_edit_key` recognize the corresponding special key. Consequence: changing a label or command shape independently can address the wrong assignment class. | **Recommendation:** when this protocol next changes, make the view/service-owned command representation explicit and pass it to the template. Preserve the current Late Doc empty-period key and ordinary comparison behavior. Verify direct command-key tests and the solver-review response test. | A concrete change to the comparison protocol. This is not justification for a generic presenter or template-command system. |
| **Opportunistic cleanup** | **O1: Primary navigation keeps a literal path-prefix map beside named routes.** `base.html` chooses `aria-current` from request-path slices while `urls.py` owns routes. Consequence: a new route can have an incorrect selected navigation item without affecting routing or authorization. | **Recommendation:** replace only this duplicate selection mapping when a route/navigation change already justifies touching it. Preserve named destinations, selected-state semantics, and all navigation response coverage. Verify affected route response tests. | A route or navigation change; no standalone architecture project is warranted. |
| **Opportunistic cleanup** | **O2: Several views mix handlers with local calendar/table/navigation shaping.** The web audit found this in Precepting, Solve, Rounding, Academic Events, and Physicians, but no repeated stable projection contract. Consequence: shared visible changes require more tracing and can regress a domain surface. | **Recommendation:** extract a local, domain-named projection helper only after a concrete change proves the same projection is reused or independently testable. Preserve service policy ownership and domain-specific template vocabulary. Verify the affected response plus narrow browser/accessibility observation when a shared primitive changes. | Demonstrated repeated contract or a real regression. Do not introduce a generic presenter, page schema, or renderer. |
| **Opportunistic cleanup** | **O3: Calendar partial, status, and CSS-variant values are intentionally open contracts.** The shared calendar renders domain-owned partial names dynamically and turns status/variant values into CSS classes. Consequence: a malformed or changed value fails late while rendering or silently loses its intended style. | **Recommendation:** preserve direct domain partials and add or update focused response evidence whenever a partial, status, or variant is added or changed. This exposes the affected rendering contract without changing the open representation. Do not introduce a registry unless repeated failures or variant scale establish that pressure. | A calendar partial/status/variant change. No standalone refactor is warranted. |
| **Deliberate deferral** | **D1: Application paths append history and model instances protect retained generated snapshots, but neither is datastore-enforced immutable.** No user-facing bypass was found; bulk queryset updates can bypass model-instance methods, and a migration has deliberately changed generated snapshot data. Consequence is limited to privileged application/datastore access, but a stronger audit/compliance claim would not be supported today. | **Recommendation:** make no enforcement change until a human states whether technical immutability is required for history, generated snapshots, or both. If required, define the exceptional correction/migration authority before implementation and prove its permitted and rejected writes. | **Unresolved human decision:** Is application-path protection sufficient for Muriel's retained change rows and generated snapshots, or is a specific technical immutability assurance required? |
| **Deliberate deferral** | **D2: Shared calendar/table primitives have source and focused response evidence, but source inspection cannot prove narrow-viewport or assistive-technology use.** The current audit records this as a confidence gap, not a defect. | **Recommendation:** retain targeted browser and assistive-technology observation as evidence whenever changing shared lattice, table, focus, or scroll behavior. Preserve semantic table/grid and no-JavaScript form behavior. | A shared presentation change. Do not add browser infrastructure or a component registry merely to turn this judgment into a gate. |

## Cohesion and seams

**Observed evidence:** `precepting_solver.py` owns one retained-proposal boundary:
input projection, deterministic Fill/Repair, explanations, proposal editing,
stale detection, reconciliation, and Apply delegation. The solver's explicit
tests cover timing, deterministic choices, review, stale refusal, reconciliation,
history, and ordinary Apply.

**Inference:** It is large but appropriately cohesive. Splitting input,
optimization, persistence, and Apply merely by implementation phase would add
interfaces while leaving each part coupled to the same concrete Precepting,
Late Doc, source-version, and review contracts. Its current size is not a
finding. The supported seam is capacity policy at the solver boundary, before
session initialization, not an internal service split.

`slot_projection` is likewise a cohesive bounded read owner: it loads source
classes once and materializes a deliberate all-physician result for caller-owned
slots. `preceptors` has broad draft behavior but retains structural sessions,
assignments, findings, alternatives, and their history together; no independent
owner seam was demonstrated.

**Evidence-supported seams:** local organization-date policy/default reads and
the solver comparison command are narrow owner seams. View-local presentation
builders are possible domain-local seams only when future work demonstrates a
stable repeated projection. Raw-table variants, flexible calendar partials, and
separate source schedules are meaningful contracts, not missed abstractions.

## Safe future boundaries

These are proposed review units, not created Goals or Tasks. Capacity policy
and organization-date alignment are completed and therefore are not future
architecture boundaries.

1. **Make solver comparison protocol explicit, when changed.** Keep the
   template/view/service command mapping as one narrow protocol change with
   direct request and service evidence. Do not combine it with presentation
   reorganization.
2. **Decide retained-record assurance.** First record the human answer to D1.
   Only an accepted assurance requirement can justify a separate design and
   implementation slice for privileged write authority or datastore controls.

The solver comparison item waits for a protocol change; O1 and O2 wait for
their natural feature work; D1 and D2 remain decisions or evidence practices
rather than refactor backlogs.

## Maintainability checklist

Use this as a change-review aid, not an automated architecture gate.

- Put retained facts, intrinsic validation, mutation authorization, and history
  with the named domain owner; keep forms and views as adapters.
- State whether a change concerns effective time, recorded correction history,
  an operational current date, or a retained generated snapshot. Use the
  Chicago date authority for organization-local policy.
- Name the authoritative transaction/serialization and optimistic-staleness
  boundary before a mutation; do not make a read projection authoritative.
- Reuse `availability` and bounded `slot_projection` for their existing facts,
  then keep schedule-specific findings and policy with their consumer.
- Preserve or add query and materialization evidence when changing
  source-sensitive projections, snapshot generation, reports, Master Schedule, or
  solver ranges.
- Reuse shared calendar, table, form, and workspace contracts only when the
  rendering contract fits. Keep meaningful raw-table and domain-partial variants
  explicit; do not put scheduling policy in templates.
- Choose the narrowest owning test layer: service tests for policy and history,
  response tests for HTTP/template contracts, query contracts for projection
  shape, and real browser/accessibility observation for changed shared surfaces.
