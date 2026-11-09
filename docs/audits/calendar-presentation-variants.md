# Calendar presentation variants audit

**Status:** Completed recommendation. The target described by the original
inventory is implemented; this document records the current owner and retains
the earlier duplication finding as historical context.

## Current architecture

`schedules/calendar_presentation.py` owns the application-local immutable
presentation vocabulary: `CalendarLattice`, weeks, days, lanes, positioned
items, actions, statuses, and compact assignment/finding values.
`calendar_lattice.html` owns the one geometric calendar foundation. It renders
the column geometry, date and weekday headers, selected/adjacent-month state,
lanes, scroll boundary, and status/legend structure. `CalendarWeek` projects
holiday metadata into date headers, covered cells, and explicitly opted-in range
lanes through the `holidays` service; it does not append a closure lane.

The foundation has no generic calendar data model, persistence, or scheduling
policy. Domain views construct their own lattice and supply explicit item
partials:

| Variant | Domain-owned content |
| --- | --- |
| Precepting month | AM/PM assignment positions, Late Doc, findings, and edit actions |
| Vacation | Inclusive range lanes and date-level add actions |
| Academic Events | Event ranges, participant coverage, and resolution state |
| Interviews | AM shift cards and finding state |
| Community Preceptors | Derived AM/PM recurrence commitments |
| Rounding | Normal-week spans, casual occurrences, and source consistency state |
| Schedule Bot review | Proposed/current assignment overlays and solver-specific status |

The Master Schedule is intentionally not a `CalendarLattice` consumer. It is a
weekly, slot-first set of semantic tables with a separate read-only projection;
forcing its positional schedule grid into calendar lanes would hide its row and
coverage semantics.

## Shared workspace and surface ownership

`base.html` owns the application shell. `schedule_workspace_base.html`, local
navigation, period navigation, contextual-action header, surface stack, and
calendar frame are shared workspace primitives. Domain routes supply their own
labels, links, selected periods, summaries, content, and actions.

Domain form templates use `_form_field.html` and `_form_choice_group.html` for
ordinary submitted-field presentation. `data_table.html` owns the standard
semantic, scrollable data-table contract. Domain views create `DataTable` values
through `table_presentation`; individual domains still own columns, rows,
actions, and status meaning. These are presentation contracts, not a shared form
schema, table policy, or rule engine.

## Historical disposition

The prior inventory accurately found copied weekday headers, local calendar
frames, duplicated picker enhancement, and domain-specific border rules in the
pre-unification presentation. Those observations are **historical snapshots**,
not descriptions of current duplication. The completed consolidation chose one
geometric lattice and narrow workspace/surface primitives while retaining the
domain variants listed above. It did not create a third-party calendar,
metadata-driven renderer, or client-side schedule state.

## Remaining risk

Calendar changes still need response-level and browser/accessibility evidence:
the shared lattice affects several domain surfaces, while each domain partial
has distinct links and meaning. Current focused coverage includes
`test_shell`, `test_views`, `test_interview_workspace`, `test_preceptors`, and
the browser rendering tests for Precepting, Master Schedule, lead times, tables,
and work remaining. This is a testing and presentation-change risk, not evidence
of an unimplemented calendar foundation.
