# Presentation and workspace architecture audit

**Status:** Completed recommendation with a current ownership record.

## Current ownership

The shared presentation layer is deliberately small and concrete:

| Concern | Current owner | Boundary |
| --- | --- | --- |
| Application shell | `schedules/templates/schedules/base.html` | Global navigation and authenticated application framing |
| Schedule workspace | `schedule_workspace_base.html` and named navigation/period/action fragments | Title, local navigation, selected-period controls, and placement only |
| Calendar geometry | `calendar_presentation.py`, `calendar_lattice.html`, `calendar_frame.html`, and calendar CSS | Shared lattice structure and responsive scroll behavior, not domain calendar meaning |
| Ordinary content and forms | Domain form templates, `_form_field.html`, `_form_choice_group.html`, and shared surface CSS | Consistent form/content framing; domain forms retain fields and validation |
| Long-running form feedback | `processing_feedback.html`, `form-processing-feedback.js`, and shared feedback CSS | Native submissions gain operation-specific pending feedback, busy semantics, and back/forward reset without domain request logic |
| Tables | `table_presentation.py` and `data_table.html` | Semantic table mechanics; domains retain rows, columns, actions, and statuses |
| Domain views | `schedules/views/<domain>.py` | Request context, named service calls, and explicit local presentation values |

The current workspaces include source schedules, Precepting, Schedule Bot,
Master Schedule, Work Remaining, lead times, reports, rates, and account
administration. They may use shared primitives when their rendering contract
matches; no workspace is required to use a generic page or schedule renderer.

## Historical disposition

This audit originally compared Community Preceptors, Vacation, and Rounding to
Academic Events while those surfaces were being consolidated. Its evidence of
separate workspace wrappers and calendar markup is a **historical snapshot**.
The recommendation to use a common workspace frame, surface stack, calendar
frame, period controls, and contextual actions is **completed**. The current
owners are the files listed above.

The original conclusion remains important: recurrence projections, range lanes,
assignment content, actions, histories, and status vocabulary belong to their
domains. The completed presentation work did not transfer those concerns to the
shared workspace layer.

## Still-relevant risk and deferral

Views must continue to pass explicit presentation values rather than allowing
templates to recompute scheduling policy. Changes to a shared primitive should
exercise its affected domain responses and narrow layouts. This is a
still-relevant regression risk.

A generic workspace configuration model, form schema, table policy owner, or
page builder remains deliberately deferred: current reuse is structural and
presentation-only, while route, domain, and policy contracts differ.
