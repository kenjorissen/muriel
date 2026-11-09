# Scheduling architecture roadmap

**Status:** Reconciled historical roadmap. Completed recommendations are marked
as such; remaining items are risks or deliberately deferred questions, not
accepted future architecture.

## Baseline now implemented

The former roadmap described a Django monolith and proposed extracting named
owners, shared workspace primitives, and a single geometric calendar foundation.
Those recommendations are **completed**:

- Domain models, forms, views, and services are split under their respective
  packages. `docs/scheduling-services.md` is the current ownership reference.
- `CalendarLattice` and `calendar_lattice.html` provide the shared geometric
  calendar foundation; domain partials retain calendar meaning.
- Workspace, ordinary-content/form, and data-table primitives provide shared
  presentation mechanics without a generic renderer or schema.
- Bounded `slot_projection` is the Tier 0/occupancy read seam used by current
  scheduling consumers.
- Master Schedule is an implemented read-only weekly cross-schedule projection.
- Schedule Bot is an implemented retained-proposal workflow with explicit Late
  Doc review, reconciliation, and ordinary mutation/history apply paths.
- Holidays, lead times, global work remaining, schedule reports, stipend rates,
  and stipend reports have concrete current owners.

The original references to `services.py`, broad route-specific calendar
duplication, an absent Master Schedule, and a future solver are historical
snapshot evidence only. They must not be read as current source locations or
missing capabilities.

## Current dependency and authority rules

```text
models and source services -> availability -> slot projection
                                        -> domain policy/read projections
                                        -> views/forms/presentation
                                        -> authenticated mutation/history
```

The arrows describe current imports and authority, not a new framework. Views
and forms call named services but do not own scheduling policy. Models retain
facts and intrinsic validation. Domain services retain scheduling policy,
mutations, and change history. Synthetic/reset tooling uses ordinary source and assignment history.
Schedule Bot writes retained proposals and delegates
accepted changes to the ordinary mutation boundary. Master Schedule and reports
are read-only.

## Still-relevant risks

- Source-sensitive availability, snapshot-generation, and solver reads require focused
  equivalence and query-budget evidence before refactoring their internals.
- Changes to explicit sibling occupancy, Late Doc, or holiday closure behavior
  must preserve each schedule's distinct policy and visible findings.
- Shared calendar/workspace/table primitives require focused response and
  browser/accessibility checks when changed.

## Deliberate deferrals

- A generic schedule model, calendar renderer, form schema, page builder, or
  rule engine is not current architecture.
- A cache, materialized availability table, or service split needs observed
  performance pressure; the current bounded reads are authoritative.
- An autonomous assigner remains future-facing. It would need an accepted
  mutation-range authority; Schedule Bot's reviewed proposals do not establish
  such authority.
- Additional authorization roles remain a product/security decision. Current
  services enforce the existing scheduler and scheduling-administration guards;
  documentation does not infer a new policy owner.

## Historical evidence retained

The earlier roadmap's cautions remain useful: do not merge source schedules
merely because their recurrence shapes resemble one another; do not recompute
retained generated snapshots from current facts; and do not let presentation
reuse absorb domain policy. Those are continuing design constraints, while the
old proposed file layout and implementation sequence are superseded by the
current code.
