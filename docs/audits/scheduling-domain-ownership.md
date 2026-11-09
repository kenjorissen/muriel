# Scheduling domain ownership audit

**Status:** Historical audit reconciled to current implementation. Its
pre-extraction findings are retained as history; the owner map below is the
current record.

## Historical snapshot and disposition

The original audit was written when scheduling facts, forms, views, and services
were concentrated in monolithic `schedules/models.py`, `forms.py`, `views.py`,
and `services.py`. Its line-number citations, observations about unextracted
owners, and recommendations to split that monolith are **historical evidence**,
not current source references.

That extraction recommendation is **completed**. Domain modules now own models,
forms, views, and services by subject, and the owner map in
[`../scheduling-services.md`](../scheduling-services.md) is the maintained
current reference. The original evidence remains useful for why these seams
exist: source facts are independent, availability is a derived read, draft
findings do not prohibit human edits, changes retain history, and retained
snapshot generation does not recalculate current facts.

## Current dependency direction

```text
domain facts and intrinsic validation (models/<domain>.py)
                         |
source reads and mutations (services/<domain>.py)
                         |
availability -> bounded slot projection -> schedule-specific findings/candidates
                         |                         |
       calendars, reports, Master Schedule, work remaining, Schedule Bot
                                                   |
                       authenticated mutations, history, and proposals
```

`slot_projection` is the current bounded Tier 0 and occupancy seam. It reads
roles, templates, monthly rules, availability categories, Vacation, Academic
Events, Rounding, Community commitments, and existing Preceptor/Interview
occupancy. It is a concrete scheduling projection, not a generic shared rule
engine. Preceptor, Late Doc, Interview, Master Schedule, reports, and Schedule
Bot each retain their own policy and presentation responsibilities. Synthetic/reset
tooling uses ordinary source and assignment history without publications.

`holidays` supplies a closure projection to those consumers. `lead_times` and
`work_remaining` form a separate operational-attention boundary: lead times own
the editable policy and work remaining owns the shared projected-item contract
and accepted producer aggregation. `master_schedule` and `schedule_reports` are
read-only consumers. Schedule Bot alone owns retained proposals and applies
accepted changes through ordinary mutation services.

## Current authority record

Models hold retained facts and intrinsic constraints. Domain mutation services
keep authorization, transaction/serialization where applicable, validation,
write, and history together. Forms validate submitted representations; views
authenticate, select request context, invoke named owners, and create
presentation values. Services do not import forms or views.

Draft initialization remains intentionally distinct from assignment edits:
`preceptors.initialize_preceptor_periods` idempotently creates weekday sessions
under the scheduling serialization boundary. It does not claim assignment
history. This is an implemented structural initialization behavior, not the old
unserialized monthly-GET condition described by the historical audit.

## Still-relevant risks

- Tier 0 fan-out and source-sensitive snapshot/solver projections need their
  existing result and query-contract tests before internal movement.
- Preceptor/Interview sibling occupancy and Late Doc policy have distinct
  schedule contracts; reuse must remain narrow and tested rather than becoming a
  joint schedule owner.
- Shared presentation primitives can affect several routes; response and browser
  coverage remains necessary for visible changes.

## Deliberate deferrals

No generic schedule model, generic rule engine, cache/materialized availability
table, microservice split, or compatibility layer is implied by the current
packages. A future automated assigner remains a separate product/authority
question; Schedule Bot is a human-reviewed proposal mechanism today.
