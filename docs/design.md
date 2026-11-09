# Muriel Physician Scheduling Product and Domain Design

**Status:** Current canonical product design. Sections that identify an
initial vertical slice or an earlier proposal are retained as design history;
their deferred lists do not describe the current product.
**Originally recorded:** 2026-08-14
**Current vocabulary:** Muriel, Schedule, Precepting, Schedule Bot, Physicians,
and Accounts are the current user-facing names unless historical context
requires another term.

## Accepted direction: month-calendar Precepting (2026-09-11)

The human has replaced the earlier week-editing and publication requirements.
This section records the accepted replacement contract and takes precedence
over earlier workflow descriptions below. Delivery updates the affected
current documentation as it replaces that behavior. Terminal Task definitions
remain historical evidence and are not modernization targets.

Precepting's month calendar becomes the direct entry point for editing its
assignment slots. The intermediate Precepting Week workspace goes away
entirely. It is not hidden navigation or a retained alternate editing page.
This change does not remove genuine weekly scheduling rules, Rounding's weekly
period, or the weekly Master Schedule.

Remove the Precepting publication lifecycle, actions, status markers, retained
published-revision feature and publication-only artifacts. Current assignments
are the schedule. Keep ordinary assignment/source change history, findings,
explicit exception handling, and Schedule Bot's human review/apply protections.
A solver proposal revision is not a published schedule revision. Ordinary
current-schedule month PDFs and reports remain useful consumers.

September 12 presentation refinement: the Precepting month calendar has no
warning or status badges in the upper-left week date-row gutter. Remove both
aggregate required-work badges and generic week-finding indicators there.
Keep slot/session findings, the month Work remaining summary, genuine weekly
finding details in the existing List and focused editors, and access to the
owning Rounding schedule. This presentation change does not alter finding or
month last-edited policy, or other calendars' indicators.

Record a last-edited time per Precepting month based on changes that actually
affect the month calendar. A scheduling-input save by itself is insufficient.
The human's examples are the acceptance basis:

- Changing an assignment changes the month's time.
- A Vacation change that creates a calendar finding changes the month's time.
- Vacation for a physician not scheduled during that absence does not change
  the month's time when it has no calendar effect.
- A Community Preceptor rule change that creates a calendar finding changes
  the month's time.
- A Community Preceptor rule change whose effect remains masked by Vacation
  or an already-overridden expectation does not change the month's time when
  the calendar result is unchanged.

Source edits may also remove a finding; the same resulting-content criterion
applies. Viewing, filtering, navigating, printing, and the passage of time are
not edits. Ordinary retained history remains independent of this summary time.

The human clarified month ownership and time semantics on 2026-09-11. Only
dates belonging to the month contribute to its last-edited time. A September
30 change does not advance October's time merely because October's calendar
also previews September 30. A source edit may affect multiple months, but each
must have its own qualifying in-month content change. A finding evaluated over
a whole week counts only where it affects the selected month's own dates.

Evaluate the before/after calendar at the time of the edit, with the same
organization-local current date on both sides. Use the calendar's ordinary
historical/operational behavior; do not add a special historical replay or
recalculate hypothetical past findings. For example, a template change that
would trigger findings only on dates already past does not count when those
findings do not change the calendar as displayed at edit time. An explicit
correction to a past assignment still counts if its displayed content changes.

If an edit changes only today's visible content, it counts now, even if the
same edit performed tomorrow would have no visible effect. Keep that recorded
edit time when the day later passes; do not retract, recompute or advance it
because operational findings age out. This is the time a qualifying edit
occurred, not a continuously recalculated verdict about whether it still
matters today.

Changed finding details count even when the compact warning icon stays the
same. This includes the explanation of a finding affecting an in-month slot
or session, rather than only its icon, severity or count. Changes confined to
the list of available replacement physicians in the focused assignment editor
do not count. The human explicitly accepted this distinction on 2026-09-11.
Changing internal source identifiers or other non-displayed provenance alone
still does not qualify.

For propagation into the future, evaluate months sequentially from the current
month and stop at the first month with no actual assignments. Empty initialized
period/session rows and generated candidate proposals are not assignments.
Count retained Precepting and Monday Late Doc assignments shown on this calendar.
The human explicitly accepts the assumption that scheduling has no intervening
empty month; do not search beyond the first empty month, add a gap-recovery
mechanism, or materialize an unbounded recurrence horizon.

This stopping rule bounds future propagation, not the directly edited month.
Adding its first assignment or removing its last assignment must still compare
that month's before/after content, as must an explicit historical correction.
An evaluated month advances its timestamp only for an actual qualifying content
change; merely being in the bounded scan is insufficient.

For an existing or untouched month without a recorded qualifying edit, display
`no edits yet`. Do not invent a historical edit time, reconstruct old calendar
states, or treat migration/initialization/first viewing as an edit. The first
qualifying edit replaces that label with its recorded time. A later empty month
or expired finding does not erase a time that was already recorded. This
initial display was explicitly selected by the human on 2026-09-11.

The month workspace displays this retained value above the calendar, labeled
with the selected month and formatted in `America/Chicago` with the applicable
CST/CDT abbreviation. Physician focus and neighboring dates in the grid do not
change which month's value is shown. The display reads the durable period value;
it does not compute another edit time.

The current-schedule month PDF shows the same selected month's retained value
as "Last edit", separately from its generation time. Recorded times include
`America/Chicago` and the applicable CST/CDT abbreviation; an unedited month
shows exactly `no edits yet`. Printing later, physician filtering, and adjacent
dates in the grid do not change the value or select another month's timestamp.
The PDF continues to show a recorded time when assignments have been removed
or findings have aged out, without reconstructing history or initializing a period.

The recorder stores this organization-wide value in
`SchedulePeriod.last_edited_at`, a nullable timestamp with no historical backfill.
The outer scheduling operation compares ephemeral calendar content under
`SchedulingMutationAuthority` and records differences in the same transaction.
Dated write owners capture their old and new ranges before mutation; physician
name corrections capture months with that physician's retained Precepting or
Monday Late Doc assignments. Other undated source changes use the contiguous
assigned-month horizon. Multi-write forms and imports use `scheduling_operation()`
so intermediate states do not become edits.

`precepting_content.py` owns the assigned identity and detailed finding values
shared with calendar rendering. `services/month_edits.py` excludes spillover,
replacement choices and source identifiers, and uses the ordinary finding policy
at one captured organization-local clock. The Academic Event reader currently
materializes recurrence rows; comparisons contain that reader in a rolled-back
Django savepoint and return only immutable content, so recording does not itself
initialize source records or histories. Browser and PDF timestamp formatting are
separate consumers of the retained value.

### Editing flow and delivery

Use the existing focused slot editor directly from the month and return to the
originating month. Keep ordinary forms and assignment services; no new inline
form or modal is needed. The human authorized queue execution on 2026-09-11 and
requested smaller delivery steps with these boundaries:
retire publication consumers before storage, establish direct month editing
before deleting the Week page, and deliver the authoritative month edit-time
recorder before its calendar and PDF displays.

## 1. Purpose

Build a maintainable scheduling system for maintaining the **Preceptor Schedule**.

The system should replace an error-prone manual scheduling process built around several independently maintained spreadsheets and many human scheduling rules.

The intended architecture is:

**structured scheduling data + explicit rules + deterministic logic + constraint solving where useful + human review and apply**

An LLM is **not** responsible for producing schedules. AI may assist development or eventually help users express/explain rules, but scheduling truth must remain deterministic and reproducible.

The system should favor boring, maintainable technology and remain supportable by a competent Python developer without specialized infrastructure knowledge.

---

## 2. Product scope and original baseline

The primary output is the **Preceptor Schedule**.

Its major upstream inputs are currently:

* Faculty Schedule
* Vacation Schedule
* Academic Event Schedule
* Rounding Schedule
* Community Preceptor Schedule / availability

Today, most or all of these are manually maintained spreadsheets.

The current product maintains these facts through manual entry. Imports and
external integrations remain future work.

Longer term, upstream data may arrive through:

* CSV or spreadsheet import
* clean exports from source systems
* APIs
* change/event streams

The internal scheduling model must not depend on spreadsheet structure.

---

# 3. Product direction

Although the immediate problem is generating the Preceptor Schedule, upstream schedules should be treated as **first-class schedules/data**, not merely as opaque inputs.

For example, Vacation Schedule data entered into this system can produce useful Vacation workload reports and PDFs.

This creates a possible migration path:

1. Existing spreadsheet remains authoritative.
2. Its data is entered/imported into this system for preceptor scheduling.
3. The system becomes pleasant enough for maintaining that data directly.
4. The system can publish the same useful upstream schedule.
5. The spreadsheet becomes unnecessary.

Do not make replacing upstream spreadsheets a prerequisite for initial success.

---

# 4. Schedule lifecycle

Current assignments are the schedule. Source facts feed deterministic availability
and findings; schedulers make explicit edits or review and apply Schedule Bot
proposals. Ordinary assignment and source changes retain their prior values,
actor, and time. Incomplete schedules remain editable with visible findings and
explicit exception handling.

There is no publication lifecycle, published-revision archive, publication status,
or separate publish action. Solver proposals retain their own input fingerprints
and revision checks; these protect human review and Apply and are independent of
the removed publication representation.

`SchedulingMutationAuthority`, defined in `schedules.models.mutation`, remains the
single serialization row for scheduling writes, coherent reads, acknowledgement
retirement, and solver Apply. Its existing table and row survive publication
storage removal. The shared `_mutation` service retains the existing PostgreSQL
transaction/row lock and same-process SQLite development lock conventions.

## Storage retirement

Conventional Django migration `0059_delete_publication_storage` deletes only
`PublishedWeekRevision`. Earlier migrations remain intact so fresh installations
and upgrades follow the same schema history. Physicians, source facts,
assignments, change history, exceptions, accounts, and solver proposals remain.

---

# 5. Schedule consumption

Current-schedule month PDFs, reports, and printed schedules are read-only outputs
of current corrected facts. They identify the selected period and generation time;
they do not create or retain an issued revision. Consumers can regenerate an output
after a correction. Ordinary retained change history supplies the application's
record of who changed a scheduling fact and when.

---

# 6. Provenance

Provenance is a core requirement.

Every scheduling fact should retain sufficient information to answer questions such as:

> Why was this physician unavailable?

> Did this schedule include yesterday's vacation change?

> Which source caused this assignment to become invalid?

> What exact inputs produced a solver proposal?

Examples of provenance:

* manually entered
* imported from Vacation Schedule
* derived from a Rounding Schedule entry
* derived from a recurring faculty rule
* manually overridden
* selected during schedule resolution
* selected by Schedule Bot or a future solver

Each retained solver proposal should preserve the exact input state used to produce it, or an immutable representation sufficient to reproduce and explain the result.

Source and assignment change history retains the previous and resulting account.
Schedule Bot proposals preserve the reviewed inputs and proposed choices, including
the input fingerprint and proposal revision used to reject stale edits or Apply.
These records remain independent of current-schedule PDF generation.

---

# 7. Time model

Precepting occurs:

* Monday through Friday
* two scheduling periods per day:

  * AM
  * PM

Rounding occurs:

* Monday through Sunday

A preceptor session therefore initially means one weekday AM or PM period.

## 7.1 Weekend inspection semantics

Physicians and their effective roles may be inspected on any calendar date. Faculty Schedule base availability applies only to Monday-Friday preceptor sessions. When a physician is inspected on Saturday or Sunday, the application should present base availability as **not applicable — no weekend preceptor session**. This is not an `open`, `backup only`, `off`, or unknown base schedule state, and inspecting a weekend must never create weekend sessions or index into the weekday template.

Rules may need to inspect dates outside the period being modified because constraints such as rounding, workload, or future rules may cross boundaries.

### 7.1.1 Patient Care availability policy

`Patient care` remains a retained built-in Faculty Schedule selection, not a
separate role or custom schedule option.  At the shared Tier 0 availability
boundary, an effective Faculty Patient Care selection is ordinarily `backup
only`.  An effective-dated `External Patient Care` role narrows only that same
selection to `not available` for dates in the role's range.  Before and after
that range, the retained Patient Care selection again composes as `backup
only`.  The composed result preserves both the schedule selection and, when
applicable, the External Patient Care role as explanatory sources; vacations,
hard Academic Events, rounding, occupancy, and custom schedule-option impacts
continue to compose independently.

Keep distinct concepts for:

* **mutation range** — assignments a calculation may change
* **evaluation range** — surrounding data that must be considered to establish validity

## 7.2 Accepted temporal authority and retained history

Scheduling data has at least two distinct temporal meanings:

* **effective time** — the date or period when a fact or assignment applies
* **recorded history** — when the system learned, recorded, or corrected that information

The system must retain history when scheduling facts or assignments change. Every scheduler who may edit schedules may explicitly correct a value whose effective time is in the past when events occurred outside the system or the recorded value was wrong; no separate administrator approval is initially required. The correction must retain the prior value and record who made the correction and when it was made. An explanatory note is always optional. The correction changes the system's current corrected account of the past; it does not alter a previously downloaded artifact.

Historical schedules, reports, and assignments use their retained facts. Changes to current templates or rules do not cause old dates to be recalculated. An explicit human correction changes only the stored historical fact or template being corrected, retains the prior account, and does not trigger recalculation or mutation of other historical records.

An eventual assigner must never change the past. The deterministic assignment boundary must reject any assigner proposal whose mutation range includes a past session. An assigner may read historical facts when evaluating rules or producing a proposal, but it cannot create, replace, correct, or remove historical assignments or source facts.

The organization's authoritative local timezone is `America/Chicago`, including its daylight-saving transitions.

For assignment authority, every session on local calendar date `D` becomes past at the start of date `D + 1` in `America/Chicago`. Until that boundary, the assigner may propose changes for sessions on `D`; at and after that boundary, only a scheduler may record an explicit historical correction. The application must calculate and enforce this boundary. Callers, UI code, and a future assigner must not supply or bypass it.

### Operational attention

The application resolves organization-local today behind one injectable domain
boundary. Operational projections use that date to focus current attention;
they never accept a caller-provided cutoff. A dated Preceptor session,
Interview, or Academic Event remains operational through its inclusive end
date. A Preceptor/Rounding workweek remains operational through Friday, even
though normal rounding records span Monday-Sunday. Once the applicable range
is wholly before local today, workspace projections do not recalculate or show
live findings, warning styling, or warning totals. Retained facts, correction
affordances and alternatives, histories, and report inputs remain available.

### Work remaining projections

Muriel derives unresolved scheduling work from authoritative current facts; it
does not persist a parallel work queue, resolved flag, acknowledgement, or
dismissal state. A projected work item has a stable domain identity, owning
area, concise label, affected inclusive range, direct target, structured
required or advisory reasons with positive actionable-unit quantities, and one
derived `matters_on` date. The user-facing label for that date is `Attention
starting`; the policy interval used to derive it is the area's `Lead time`.
Required reasons take precedence over advisory reasons when both are present.

Scoped and organization-wide work presentations use the same structured
vocabulary for different questions. An explicitly selected workspace range uses
the `focused-range` policy: it includes its current or future unresolved facts
without applying a `matters_on` cutoff, so deliberate far-future navigation can
show work before it enters global attention. It sums the actionable-unit
quantities of every intersecting reason, including a domain-owned actionable date
that falls inside the selected range even when the fact's affected range does
not. Its prose contains only aggregate required/advisory counts, never labels,
an itemized list, or links, and it appears immediately after the range controls
and before Actions or primary content. It is danger/red when required work or hard
findings remain, attention/amber when only advisory or preference work remains,
and clear/green when an operationally evaluated range has no remaining work.
Retained-history projections are separate correction context: historical facts
and correction affordances remain available there with neutral presentation and
never re-enter either focused or global operational counts. A view with no
accepted completeness requirement has no summary rather than a decorative clear
state.

`work_remaining` owns the membership decision and all required/advisory
quantity rollups for those two operational policies. Producing domains own only
their unresolved facts, affected and explicit actionable ranges, reasons,
targets, and lead-time anchor. Workspace and global presentation callers do not
recompute organization-date cutoffs or construct a second summary/action from
raw domain facts. A domain may still suppress a fact that is intrinsically no
longer operational before it reaches this boundary; retained correction data is
not an operational work fact.

The projection owner's `focused_range_has_membership` operation is also the
single current-or-future range test for domain display rows. Precepting,
Normal Rounding, and Interview surfaces delegate their date-range membership to
it rather than reproducing cutoff arithmetic, so their visible unresolved
details remain consistent with their focused summaries.

The global `Schedule > Work Remaining` view answers which work currently
matters across the organization through the `current-attention` policy. An
unresolved item enters when organization-local today reaches its single
`Attention starting` (`matters_on`) date, sorts oldest first with stable domain
and identity tie-breaking, and disappears when resolved or when its affected
operational range becomes historical. Related raw findings collapse
to their natural actionable unit: one Precepting or Normal Rounding week, one
Academic Event planning bucket, one Holiday occurrence, or one Interview
academic year. Unscheduled Yearly Academic Event work groups by Academic Year
and expected quarter; dated Yearly and Monthly-rule occurrence work groups by
the calendar month of its first scheduled date. The initial view is the same
for all authenticated schedulers
and is intended to remain at or near zero; role-specific ownership and
filtering are deferred. Grouping is independent of summary quantity: a weekly
or academic-year row may contribute several individual required or advisory
work units to the summary above it.

Initial Lead time policy is intentionally explicit and small. Manually
scheduled Holiday occurrences receive attention 18 calendar months before
their nominal holiday date. Unscheduled and underfilled Yearly Academic Events
receive attention six months before the start of their expected academic-year
quarter, and a Yearly occurrence's scheduled date does not move that anchor.
Underfilled Monthly-rule occurrences receive attention six calendar months
before their scheduled start date. Precepting and missing or inconsistent Normal
Rounding weeks receive attention five calendar months before Monday. Precepting work
includes required assignment gaps, Late Doc, and findings. A complete week
with no unresolved findings has no remaining work. Interview academic-year quota
and assignment work receives attention on July 1
at the start of that academic year. Vacation entry, Community Precepting
recurrence, Casual Rounding rules, Physician setup, and report or rate
administration initially produce no global work because they have no accepted
completeness schedule.

Lead times are current, audited organization configuration. `Schedule > Lead
Times` exposes independent non-negative whole-month values for Holidays,
Academic Events, Precepting, Normal Rounding, and Interviews. Their defaults
are respectively 18, 6, 5, 5, and 0 months before the already-defined anchor.
Changing a value immediately changes only when unresolved work from that family
enters the global Work Remaining view. It does not change source scheduling
facts, scoped summaries for an explicitly viewed period, history, reports, or
the anchor itself. Values are not effective-dated and cannot be deleted; every
change retains its prior/resulting value, actor, timestamp, and optional note.
All authenticated schedulers may inspect the policy, while Scheduling
Administrator authority controls edits.

## 7.3 Accepted Vacation Schedule granularity

A Vacation Schedule entry is independent of a physician's roles. It has an inclusive start and end date and applies to the whole day, AM only, or PM only. Whole day is the default for manual entry. Roles and vacation facts are separate inputs that current effective-availability logic combines. This preserves the source schedule as first-class data while retaining the AM/PM precision needed by availability and reporting queries.

## 7.4 Accepted Academic Event participation

An Academic Event is one shared event fact with one or more participating physicians. The system should not duplicate the event into an independently maintained record per physician. Event details and participant membership are edited through the shared event, and retained history must make participant additions and removals visible with the rest of the event's changes. An event has one or more nonoverlapping inclusive date ranges sharing Full day, AM only, PM only, or None coverage, with Full day as the manual-entry default. Each definition and occurrence records a Precepting impact: Hard, Soft, or None. Hard and Soft require AM, PM, or Full day coverage. Hard makes participants unavailable during covered periods, Soft preserves eligibility but ranks participants behind otherwise clean candidates and produces a preference finding when assigned, and None remains visible and reportable without an availability or finding effect. None coverage requires None impact.

An occurrence may instead mean **all available Faculty**. This has no individual participants, is complete once dated, and applies its configured Hard, Soft, or None impact to each physician who has an effective Faculty role on each covered date. Faculty-equivalent status without an effective Faculty role does not qualify.

## 7.5 Accepted Rounding Schedule representation

The normal Rounding Schedule is recorded by Monday-starting week. Exactly one
normal rounder owns each Monday-Sunday rounding week. For the initial Preceptor
Schedule, that weekly fact affects the ten Monday-Friday AM/PM preceptor
sessions. A week without a normal rounder is incomplete working state rather
than an invented assignment; it remains editable and produces a deterministic
hard finding until the missing rounder is recorded.

Casual rounding uses separate effective-dated recurrence rules. Each rule
belongs to one physician, names exactly one weekday, and selects any nonempty
combination of first, second, third, fourth, and fifth occurrences of that
weekday in a month. `All` is an input convenience equivalent to selecting all
five ordinals. A missing fifth occurrence produces no date rather than falling
back to the fourth. Casual coverage is all-day and has no AM/PM selection.

Each recurrence rule has optional inclusive start and end dates. Rules for the
same source, physician, and weekday may change over time but their effective ranges
cannot overlap, regardless of the selected ordinals. With multiple rules, only
the earliest may be unbounded into the past and only the latest may be
unbounded into the future; a rule unbounded on both sides must be the only rule
in that scope. Gaps mean that no recurrence rule applies. Different physicians and
different weekdays remain independent.

The normal rounder is ordinarily unavailable to precept throughout the
precepting portion of the rounding week. When a casual rounder is active, the
normal rounder becomes **backup only** for both AM and PM of that entire day;
the weekly rounding ownership does not change and the normal rounder does not
become ordinarily available. The casual-rounding fact does not itself change
the casual rounder's preceptor availability.

Rounding retains the reusable schedule-workspace pattern with four local views:
`Calendar`, `List`, `Normal Rounding`, and `Casual Rules`. Calendar is the
default. Calendar and List are month-based resolved views of both source types;
Normal Rounding and Casual Rules remain the focused maintenance views for the
weekly assignments and recurrence definitions respectively. Rounding history
is contextual on every view rather than a fifth tab. Future consistency polish
may align its workspace presentation with the reference pattern, but does not
authorize a functional rebuild of its projections, recurrence, maintenance,
history, or downstream availability behavior.

The Rounding Calendar is a conventional Sunday-Saturday seven-day month. It
shows each Monday-Sunday normal-rounder week as a continuous labeled span,
split only where the calendar row boundary requires it, and shows each derived
casual-rounder occurrence on its matching day. Missing normal rounders and
recorded normal-rounder inconsistencies are conspicuous. Existing items link to
their authoritative edit forms; a missing week links to a normal-rounder form
prefilled with that Monday. Calendar projection does not persist derived casual
occurrences or invent weekly assignments.

The Rounding List is the selected month's resolved weekly schedule, ordered by
Monday. Every Monday-Sunday rounding week that intersects the selected month
has one row with its normal rounder and missing/inconsistent state. Matching
casual-rounder occurrences are summarized in the same row as comma-separated
`Name (weekday)` entries in chronological order. Casual rounding has no
completeness requirement and does not affect row status. The status is `Filled`
when the required normal rounder is assigned, matching the Academic Events
vocabulary. The List is a compact schedule view; Normal Rounding remains the
source editor.

Calendar and List share current-month defaulting and explicit month navigation.
Their work summary reports missing and inconsistent normal-rounder weeks that
intersect the selected month; casual rules have no completeness requirement.
The Normal Rounding view preserves the existing inclusive date-range inspection
and gap-entry workflow. Its two date controls use the shared small
progressive-enhancement period picker: in supporting browsers, changing either
to a different valid non-empty date submits the ordinary complete range form;
otherwise `Inspect weeks` remains the available submit action. Casual Rules lists
the recurrence sources with its Add action. Forms return to their originating
Rounding view and preserve useful month/range context where practical. This workspace redesign changes no
rounding recurrence, role eligibility, history, or downstream availability
semantics.

## 7.6 Accepted Community Preceptor base commitments

Community Preceptor Schedule rules use the same one-physician, one-weekday,
ordinal-set, and effective-range recurrence shape as casual rounding, but they
remain distinct source facts. A Community Preceptor rule selects AM, PM, or
both, with both periods enabled by default and at least one required. The same
non-overlapping effective-range rule applies per source, physician, and weekday;
different physicians and weekdays remain independent.

A matching selected period is an **expected base commitment**. It makes that
physician base-available and records that they are expected to precept, but it is
not itself an authoritative Preceptor Assignment. An unmatched period is base
`not available`; there is no unknown Community Preceptor base state and no
backup-only recurrence state initially. Source entry remains independent of
the physician's current roles and qualifications.

The expected marker and supporting rule are deterministic candidate-priority
input. Manual scheduling, Schedule Bot, and a future assigner consider an available
expected Community Preceptor before ordinary alternatives, while retaining
human control and the ability to choose another valid assignment. An
unfulfilled available expectation produces a preference finding, not a hard
coverage failure merely by itself.

Vacation is the ordinary current way to override a Community Preceptor
commitment. Base recurrence and vacation remain independent retained facts;
effective-availability composition makes the physician unavailable for a
covered vacation period while preserving both the expectation and blocking
source for explanation. Recording or correcting a recurrence rule does not
create, replace, or remove an assignment or vacation.

Share narrow recurrence calculation and input presentation where useful
without collapsing Community Preceptor and Casual Rounding models, mutation
authority, history, or availability semantics into a generic rule framework.

Community Precepting adopts three local views: `Calendar`, `List`, and `Rules`.
Calendar is the default scanning view; Rules remains the focused source editor
for recurrence definitions. Calendar and List are bounded read projections of
effective Community Preceptor recurrence rules for their selected period. They
show expected base commitments, including the selected AM and/or PM periods and
multiple physicians when rules coincide. They do not persist resolved
occurrences, create or claim Preceptor Assignments, or introduce completeness,
fill, or coverage requirements. A displayed commitment remains an expectation
derived from its supporting rule, not a scheduled assignment or a new status.

## 7.7 Accepted source-fact input posture

Source schedules should be easy to maintain and should record their own facts independently. A source fact is not invalid merely because another current fact makes it irrelevant to scheduling, and harmless redundancy should not force users to correct data that would produce the same scheduling result. For example, a vacation remains valid while the physician has no role.

Input validation should reject facts that are intrinsically malformed or violate a confirmed source-specific rule. Derived availability and validation should decide which independently recorded facts matter for a session. When a cross-source interaction has not been explicitly settled, ask for product direction before adding a consistency rule, warning, or required correction.

## 7.8 Accepted change recording and explanation policy

Every create, edit, correction, removal, assignment change, lock change, and rule override records its prior and new values, authenticated actor, and timestamp automatically. No change requires the scheduler to explain themselves; the UI may offer an optional note.

For a rule override, the system records the overridden rule and resulting finding as structured facts rather than relying on prose. An optional explanation may add useful human context but is never an enforcement mechanism. If an ordinary source or role change is known to make existing assignments inconsistent, the UI may warn and identify the affected assignments, but the warning does not block the change or turn an explanation into a requirement.

A scheduler may deliberately remove a current finding override without deleting its acknowledgement record. The retained history keeps the original reason and acknowledgement provenance, and records the removing scheduler and time separately from an acknowledgement that no longer applies because the finding changed or disappeared.

The destructive scheduling-domain reset is the sole exception: it deletes all finding acknowledgements, including retired and deliberately removed history, with the scheduling facts it replaces. It does not delete login or account records.

---

# 8. Physicians and roles

Every schedulable subject represented by this part of Muriel is a physician,
including Faculty, Community Preceptors, and Casual Rounders.
`Person`/`People` is therefore obsolete domain terminology rather than a useful
broader abstraction. Rename the user-facing workspace, URLs, Django names,
templates, application-owned model and change-history types, relationships,
service vocabulary, and tests consistently to `Physician`/`Physicians`. Use a
data-preserving schema migration for existing records; this terminology change
does not authorize deleting or rebuilding retained physician or scheduling
history.

The system currently knows at least these categories.

## 8.1 Faculty

Faculty may precept.

Faculty and Community Rounder are mutually exclusive physician categories. A
physician's effective Faculty role range must not overlap an effective Community
Rounder role range. Faculty remains mutually exclusive with community preceptor
and casual rounder roles.

All faculty count as **faculty-equivalent** for coverage purposes.

Each effective Faculty role range has one optional, mutually exclusive faculty
leadership designation: `Program Director`, `Associate Program Director`, or
none. An otherwise available, conflict-free Program Director appears in the
preferred Interview-assignment candidate tier ahead of other available Faculty;
the preference is not mandatory coverage. Each effective Program Director is
committed to every active AM Interview shift for Precepting, whether or not an
Interviewer is assigned. The shared bounded slot projection owns this derived
commitment alongside direct Interview occupancy; it is not retained as a
separate scheduling fact and must not be presented as a named Interviewer.
Associate Program Director currently has no Interview scheduling effect.
Multiple physicians may hold either designation;
do not enforce an organization-wide uniqueness rule before one is requested. A
designation change over time is represented by adjacent Faculty role ranges.

Faculty availability begins with a Faculty Schedule and may be modified by:

* vacation
* academic events
* rounding
* other future scheduling rules

## 8.2 Community rounders

Community Rounders may own Normal Rounding weeks. A physician is eligible for a
Normal Rounding week when an effective Faculty or Community Rounder role covers
every date from Monday through Sunday. Community Rounder is independent of
Community Preceptor and Casual Rounder responsibilities; it adds no recurrence
rules, source schedules, attributes, or eligibility for Precepting, Interviews,
Late Doc, or other scheduling work.

## 8.3 Community preceptors

Community preceptors may precept.

They have their own base availability/schedule.

This may already be a resolved schedule rather than being calculated from separate vacation/event schedules.

Each effective Community Preceptor role range records whether that physician is
**faculty-equivalent**. This is an attribute of the role range rather than a
separately maintained qualification interval. The physician form exposes it as
a checkbox only for Community Preceptor roles. A change over time is represented
by adjacent Community Preceptor role ranges with the attribute set differently.
Faculty remains automatically faculty-equivalent, and a Casual Rounder role
alone does not confer the attribute.

A community preceptor may also be a casual rounder. Casual-rounder work does
not change that physician's Community Preceptor Schedule availability.

## 8.4 Casual rounders

A casual rounder may cover one or more rounding periods for the scheduled rounder.

Confirmed policy:

* casual rounders are not faculty
* a casual rounder may also be a community preceptor and may precept in that
  community role
* if a casual rounder replaces a regular rounder, the regular rounder does **not** automatically become normally available to precept
* that displaced rounder may be eligible to serve as an emergency/backup preceptor in exceptional circumstances such as illness

Do not collapse "not scheduled to precept" and "incapable of emergency coverage" into the same state.

## 8.5 Physician-centered maintenance

The Physicians workspace is the identity-centered place to maintain a
physician's roles, qualifications, and applicable source schedules. A
physician detail page may expose that physician's Faculty Schedule, Community
Preceptor Schedule rules, and Casual Rounder rules without collapsing those
distinct source models or their validation and history.

Keep the global Community Precepting destination while adding this
physician-centered access. The global workspace remains the aggregate answer
to what the complete community-preceptor schedule contains; the physician page
answers what is configured for one physician. Reconsider whether both deserve
global navigation only after the revised Physicians workspace can be judged in
use.

The Physicians directory provides server-rendered local views for `All`,
`Faculty`, `Community Rounders`, `Community Preceptors`, `Casual Rounders`,
`Inactive`, and `Schedule Options`. Schedule Options remains its existing server-rendered workspace and
participates in this shared local navigation as its final selected view; its
add and history actions remain contextual. The selected local tab provides its
visible identity, so Schedule Options retains a visually hidden semantic
heading, concise helper, contextual actions, and direct table without repeated
page or section headings.
Role membership is evaluated as of today in the organization's local time. A
physician with multiple compatible current roles may appear in more than one
role-specific view, while `All` presents each physician once. `Inactive` means
no role is effective today, including physicians whose only
roles are past or future. Do not add an `Other` view while the domain has no
other active role category.

The directory itself stays compact. Its local-view navigation supplies the
view identity, so the content does not repeat page, directory, or selected-view
headings. Role-specific views use one tight, single-line row per physician.
The `All` view groups those same rows under `Active` and `Inactive` headings.
An overall Physician history link exposes retained changes across physicians;
the per-physician history remains available from each detail page. In the
directory, the overall-history link shares the current-role/source-schedule
as-of row, right-aligned on ordinary widths and allowed to wrap cleanly below
that context on narrow screens. `Add physician` is right-aligned alongside the
directory's local-view navigation, eliminating a separate action band above
the views. The navigation/action row owns one full-width rule beneath both
areas; the Add button aligns with the navigation text and retains visible
space above that rule rather than sitting flush on it. Both controls remain
ordinary links/buttons with clean narrow wrapping.

Physician detail keeps identity editing first without repeating a generic
`Physician` eyebrow. Date-based scheduling diagnostics use the shared
progressive-enhancement period picker: changing the date submits immediately
when JavaScript is available, while the ordinary submit remains as a no-script
fallback.

Faculty Schedule, Community Preceptor Schedule, and Casual Rounder rule Add
forms default their effective start to the organization's local today. The
recurring-rule forms show the selected physician's existing rules and briefly
explain that only one effective rule per weekday may apply at a time, while
leaving the existing validation to explain actual overlap conflicts. Aggregate
Community Preceptor and Casual Rounder Add forms offer only physicians whose
corresponding role is effective today; future setup remains available through
the physician-centered workspace. A retained future Faculty role may likewise
be set up from that physician's Faculty Schedule entry even before its role is
effective and before any weekly template exists. This setup access does not
make the future role current for directory capacity or assignment eligibility.

Physician role Add and Edit forms likewise show the selected physician's
existing retained roles when any exist. The compact context identifies each
role, its applicable attributes, and effective range; Edit identifies the
current record. This is read-only entry context, not an inline role editor or a
new source of role validation.

---

# 9. Availability, eligibility, and assignment are different concepts

These should be modeled separately.

A physician may be:

* generally eligible to precept
* normally available for a particular session
* unavailable because of another commitment
* available only as emergency backup
* assigned to that session
* eligible but deliberately left unassigned

The casual-rounder case demonstrates why a binary `available` flag is insufficient.

The domain model should be able to explain effective availability and why it has that state.

## 9.1 Effective availability composition

Effective availability is calculated for a physician, weekday date, and AM/PM
session from retained source facts; it is not stored as a mutable day-by-day
schedule. The physician's effective preceptor role selects the base source:
Faculty Schedule for faculty and Community Preceptor Schedule for community
preceptors. Eligibility, faculty-equivalent coverage, availability, expected
community commitment, and assignment remain separate result fields.

Availability states are ordered from most to least restrictive as **not
available**, **backup only**, and **available**. Composition may retain or make
a base state more restrictive, but never makes it more permissive. Vacation
and a covering Academic Event make either faculty or community preceptors not
available. The normal rounder is not available on ordinary precepting days;
when casual rounding covers that day, the rounding contribution is backup
only. Casual coverage therefore changes an otherwise available normal rounder
to backup only, but it does not upgrade Faculty Schedule `off` to
backup only. Vacation or an Academic Event remains not available regardless of
casual coverage.

The result retains all contributing source identities and explanations, not
only the source that supplied the winning state. A blocked expected Community
Preceptor commitment remains identifiable as expected-but-blocked. Casual
rounding does not alter the casual rounder's own Community Preceptor Schedule
availability.

When an available expected Community Preceptor commitment is not represented
among a session's assignments, draft validation reports a visible preference
finding. The finding is non-blocking: it does not by itself make the draft
invalid. Expected commitment remains a prioritization
input rather than an automatically created assignment or hard coverage rule.

There is no validation that a faculty member's Faculty Schedule base state was
Open when the physician was recorded as normal rounder. That combination may
be unhelpful but does not justify another input constraint; composition simply
produces the more restrictive effective state.

---

# 10. Preceptor coverage

Each AM/PM precepting session requires at least:

**2 faculty-equivalent preceptors**

This is a hard coverage requirement reported to derive from nationwide standards. The exact governing citation/version remains to be recorded. Faculty-equivalent status is a qualification of the assigned physician, independent of whether that assignment is primary or secondary. An assigned faculty-equivalent secondary counts toward the required two because that physician remains available to precept when needed.

Currently known rules:

* all Faculty members count as faculty-equivalent
* some community preceptors count as faculty-equivalent
* other community preceptors do not

Example:

If a non-faculty-equivalent community preceptor is present, additional physicians must still provide at least two faculty-equivalent preceptors.

This often results in three total preceptors.

Coverage rules should be represented by capability/qualification rather than special-casing community names.

---

# 11. Primary and secondary assignments

Each complete AM/PM session has:

* two primary assignments
* zero or one secondary assignment

A secondary is available to precept when needed but is not expected to handle everything during the session and may also be doing something else. Primary/secondary responsibility is independent of faculty-equivalent qualification.

A Community Preceptor assignment is always primary. An effective Community
Preceptor role qualifies a physician for either primary position but never for
the secondary position, even when that role carries the faculty-equivalent
attribute. The secondary position requires an effective Faculty role. This is
an intrinsic assignment-role boundary rather than an availability override;
correct the role or responsibility instead of deliberately crossing it.

An availability state of **backup only** is not a secondary assignment role. Backup-only availability means the physician is ordinarily held for exceptional need, such as illness or vacation-driven scarcity when few alternatives remain. A scheduler may assign a backup-only physician as either primary or secondary. Doing so produces a conspicuous, explainable preference finding but is not a hard finding.

An assignment whose effective availability is **not available** produces an
explainable hard finding. An assignment whose
effective availability is **backup only** produces a conspicuous non-blocking
preference finding. An **available** assignment produces no availability
finding. All three states remain recordable in an incomplete or invalid draft;
validation reports consequences rather than preventing the human edit.

Existing spreadsheet/AI output sometimes labels physicians:

* Preceptor 1
* Preceptor 2
* Preceptor 3

Assignment records retain the domain terms primary and secondary rather than infer responsibility from presentation labels. The UI presents the numbered primary positions as Preceptor 1, Preceptor 2, and later Preceptor numbers while retaining their stable display ordering.

## 11.1 Late Doc assignment

Every Monday has one additional **Late Doc** assignment covering 5–6 PM. It is
a separate required slot rather than a third AM/PM responsibility or a property
of Preceptor 1, Preceptor 2, or Secondary. A physician with an effective
Faculty role may fill it; faculty-equivalent status without Faculty does not
qualify. The narrow manual-only exception is an effective Community Preceptor +
Float physician with `Available as Float`; it does not make that physician
Faculty or faculty-equivalent.

Late Doc candidate evaluation uses that Monday's PM effective availability and
the same Tier 0 sources used for Precepting. An existing Monday PM Preceptor
assignment does not conflict with Late Doc, because the same Faculty physician
may cover both. Other applicable availability restrictions retain their normal
available, backup-only, unavailable, explanation, and human-override behavior.

A missing Late Doc is a hard finding for the Monday-Friday week. The current assignment is retained and historically
correctable with the same optional-note and actor history posture as other
schedule facts. Current calendar PDFs include the Late Doc assignment.

In the Precepting month calendar, Late Doc appears as a fourth
row below the optional Secondary row on Monday. The corresponding Tuesday
through Friday cells remain blank because those dates have no Late Doc slot.
This does not create a separate workspace or change ordinary AM/PM session
coverage.

---

# 12. Rounding interaction

Known constraints:

* a physician who is actively rounding cannot precept during that period
* the normal rounder for a Monday-Sunday rounding week is faculty for that week
* rounding assignments can affect the Monday-Friday Preceptor Schedule even though rounding itself runs Monday-Sunday
* casual-rounder substitution does not normally free the displaced rounder for ordinary precepting
* a casual-rounder recurrence does not restrict the casual rounder's separate
  Community Preceptor Schedule availability

The Rounding Schedule therefore contributes to effective preceptor availability.

A Preceptor Schedule week with no recorded normal rounder has a hard draft
finding. The incomplete week remains editable; the finding remains until a
normal rounder is recorded for its Monday-Sunday rounding week.

---

# 13. Rules observed from an earlier manual/AI scheduling attempt

The following examples, with fictional physician names, are useful for identifying rule types, but they are **not yet authoritative requirements**.

Examples included:

* Rowan Finch assigned every Tuesday AM and PM
* Bea Alder apparently limited to one assignment per week
* certain faculty preferably avoided on faculty-meeting Thursdays
* some community preceptors occurring on recurring weekday/month patterns
* workload balancing across faculty
* assignment balancing across weeks
* explicit preference violations when no better schedule is available

These examples indicate the kinds of scheduling rules required, but each must be confirmed with the actual scheduling staff.

---

# 14. Rule semantics

Do not design rules around implementation technique.

A rule should express its domain meaning.

Useful categories will likely include:

## Hard requirements

Examples:

* a rounder cannot precept
* a vacationing faculty member cannot precept
* a session requires at least two faculty-equivalent preceptors

## Explicit/fixed assignments

Examples:

* a physician must cover a particular session
* an already-published assignment is locked

## Strong recurring expectations

Example:

* Rowan Finch normally covers Tuesday AM and PM

The exact semantics might later be established as:

* required
* required unless unavailable
* strongly preferred

Do not assume which until confirmed.

## Limits

Examples:

* maximum assignments per week
* maximum consecutive assignments

## Preferences / avoidance rules

Examples:

* avoid a faculty-meeting period when possible
* prefer one recurring pattern
* avoid an undesirable assignment

## Optimization objectives

Possible future examples:

* balance workload relative to each physician's expected load
* distribute undesirable sessions
* minimize changes to an already-published schedule

Avoid reducing all rule semantics to arbitrary numeric weights in the user-facing domain model.

---

# 15. Deterministic resolution versus optimization

Muriel currently combines authoritative manual scheduling with bounded,
human-reviewed Schedule Bot proposals. `Fill open slots` proposes only missing
work, and `Repair findings` makes only the smallest necessary unlocked changes
inside the selected mutation range. Both use the accepted hard constraints and
named workload policies described below; neither writes nor approves
a schedule without a scheduler's review and explicit Apply action.

Broader solver policy remains future domain discovery: whether observed work
justifies a global-optimization mode, which additional recurring rules and
workload targets are confirmed, and how any new named objectives should rank.
Do not treat that future policy work as a reason to replace the current bounded
Fill/Repair and manual-scheduling baseline.

The UI language should reflect scheduling concepts rather than expose solver implementation.

## 15.1 Original manual assignment boundary

The original usable release was a **human-controlled scheduling tool with
deterministic validation**. It did not automatically select or propose
Precepting assignments. Manual assignment remains available and authoritative;
the subsequently accepted Schedule Bot proposal boundary is defined in section
15.2.

This manual workflow preserves narrow domain surfaces that Schedule Bot uses
without taking authority away from the application. Those surfaces support:

* reading an explicit scheduling input state, including sessions, existing or locked assignments, effective availability, and applicable rules
* querying eligible alternatives and the reasons a physician is available, unavailable, or restricted for a session
* evaluating proposed assignment changes and returning deterministic coverage, conflict, and rule findings
* applying human-approved assignment changes through the same validation and provenance boundary used by the manual workflow

Schedule Bot consumes these surfaces and returns proposed changes. It does not
write authoritative schedule state directly, bypass validation, approve its own
proposal, or apply its own result. Any later assigner must retain those limits and
must not change the past.

Do not build a generic assigner plug-in framework. Keep scheduling semantics out
of the UI and expose the concrete domain operations required by the manual and
proposal workflows.

## 15.2 Accepted initial Precepting solver boundary

### Accepted Solve setup presentation

Solve remains an internal route and a local `Solve` item in the existing
Precepting workspace navigation; it is not a separate Solver workspace. Its
compact setup separates **Mode**, **Range**, **Balance span**, and **Special
rules**. Range uses the same progressive date-range presentation as Schedule
Reports: Month, Quarter, and Custom choices reveal their applicable selector,
show a stable read-only effective range, update immediately when JavaScript is
available, and retain an ordinary complete server-authoritative form submission
when it is not.

Balance span is derived context rather than editable dates. Its end is always
the solve range's effective end. The default is six months; the available spans
are one day, one week, two weeks, four weeks, one month, three months, six
months, one year, and a positive whole-number Custom day count. Day and week
spans are inclusive fixed-day windows. Month and year spans use inclusive
calendar arithmetic ending on the effective end. The server computes this
window for every solve; minimal JavaScript mirrors that result for immediate
review. Existing bounded exclusions and retained-assignment choices remain
solve-local Special rules.

The first Precepting solver is a human-controlled proposal mechanism over the
existing bounded slot projection. A scheduler selects an operational date
range, with Month, Quarter, and Custom conveniences, and requests a solve. The
solver returns an explainable proposal for review; it does not write assignments
while solving or approve or apply its own result. Applying
an accepted proposal must pass through the existing mutation, history, and
validation boundaries and leaves an ordinary editable draft.

The solver has two initial modes:

* **Fill open slots** preserves every existing assignment, whether locked or
  unlocked, and proposes assignments only for open required positions and for
  an optional Secondary when needed or preferred by accepted coverage policy.
* **Repair findings** treats current assignments as a baseline, preserves
  history and locks, and may change otherwise-valid unlocked assignments inside
  the selected solve range when a bounded cascade or swap is needed. After
  satisfying hard constraints, repair minimizes the number and severity of
  changes before applying balance and other accepted system-policy objectives.

The solve range is mutation authority. A separate balance window supplies
read-only workload context and does not permit changes outside the solve range.
Its default length is six months and the scheduler may change it. This supports
the clinic's normal practice of reviewing at least the current month and two
future months, often extending the working schedule through a full six-month
horizon, while allowing later passes to account for assignments retained from
earlier passes. By default, the balance window is the six complete calendar
months ending with the final calendar month touched by the solve range. A solve
through December therefore balances July through December, whether it fills a
three-month October-December range or repairs December alone. Schedule Bot
accepts solve ranges of at most three calendar months. The scheduler may adjust
the balance-window dates for a particular solve, up to its twenty-four-calendar-
month maximum.

Faculty workload balance counts each Faculty Primary assignment and each
Faculty Secondary assignment as one Precepting shift. Existing fixed Faculty
assignments inside the balance window count normally. Community Preceptor
assignments are tracked separately and are not balanced against Faculty; Late
Doc assignments are likewise outside the Precepting balance objective and may
be evaluated by their own policy later.

Both Fill and Repair include every operational Monday Late Doc slot in the
selected solve range. A Faculty physician assigned to Monday PM Precepting may
also be assigned Late Doc; neither assignment causally implies the other. Thin
Monday PM availability will often make the same physician a practical proposal,
but Muriel must not silently turn Monday PM Precepting into a standing Late Doc
commitment. The proposal review gives Late Doc assignments a dedicated,
conspicuous section and requires an explicit human confirmation or replacement
for each proposed Monday before Apply is enabled. Late Doc remains outside the
Faculty Precepting fairness calculation.

As an initial Late Doc soft policy, among otherwise equally suitable, normally
available actual Faculty candidates, prefer a physician already assigned to
that Monday PM Precepting session. This is a convenience preference rather than
eligibility, a standing commitment, or permission to bypass the explicit Late
Doc review.

Late Doc has a separate, explicitly named balance policy rather than inheriting
or disappearing inside general Precepting fairness. Across the same rolling
six-month balance window, derive each actual Faculty physician's Late Doc share
from the Monday PM slots where their effective base schedule resolves to
`open`, with role/template/rule starts and ends prorated normally and
`backup only` contributing no normal capacity. Existing Late Doc assignments in
the window count even when outside the solve range. Keep proposed totals within
one assignment of the best achievable proportional Late Doc balance by default;
within that band, prefer a normally available Faculty physician already
assigned to Monday PM Precepting. The one-assignment band is a named,
organization-tunable Late Doc policy value, not a general solver weight.

Proposal review exposes this calculation separately, including current and
proposed Late Doc totals, overlap with Monday PM Precepting, and any unavoidable
imbalance caused by thin availability. This policy is expected to be tuned from
real schedules while retaining its distinct identity and mandatory human
confirmation.

Proposal review is led by the **Proposed schedule** calendar, followed by a
**Week list** of affected positions and then the **Current schedule** calendar.
The Schedule Bot name replaces Precepting Solve in scheduler-facing workspace
and navigation language. The week list shows the shift, current value, proposed
value, and a concise scheduling-language reason. Fill deltas read as `Filled`
on the proposed calendar while Repair deltas read as `Changed`; color may
support the distinction but must not carry it alone. Findings, manual-edit
provenance, and unchanged assignments retain their own language.

The same review also offers **Proposed schedule** and **Current schedule**
calendar views with identical geometry. Proposed schedule shows the complete
resulting schedule while marking changes and quieting unchanged context;
Current schedule preserves the exact baseline. The change set answers what the
room is being asked to approve, while the calendars answer what the schedule
looks like before and after. Late Doc remains a dedicated confirmation section
rather than disappearing into either general view. Each pending Late Doc on a
current, unapplied proposal offers an inline Confirm action that submits the
proposal revision to the existing reviewed decision service and returns to the
Late Doc review section; Replace remains the separate detailed decision path.
The dedicated review is a compact table of date, proposed physician, decision,
and actions, with controls that remain distinct at narrow widths. This is the
initial presentation to evaluate with users, not a commitment to its final
visual treatment.

Every proposed assignment carries a concise **Why this?** explanation suitable
for discussion in that meeting. It distinguishes hard exclusions, named policy
advantages and disadvantages, equally good alternatives, and the final stable
tie-break; it must not present an optimizer choice as the uniquely correct
human decision. Alternatives remain visible down through eligible backup-only
candidates. The proposal also summarizes its schedule-wide compromises.

For a scheduler who asks a stronger question, **Compare with...** selects a
specific alternative and runs an on-demand counterfactual solve against the
same retained inputs and policy: force that alternative (or exclude the
proposal) and report the resulting hard feasibility, additional moves,
workload effect, and policy compromises. Counterfactuals are not eagerly
generated for every assignment and do not mutate the proposal or schedule.
They are explanatory evidence for the human decision, not an optimizer proof
or a new proposal unless the scheduler explicitly requests a new solve.

The review's Compare with control presents every other physician who passes the
authoritative position-level eligibility and role boundary, grouped exclusively
as equally good, other normally available, or backup only. Hard-ineligible
physicians never appear. Each person is shown with their local scheduling
status and tradeoff, not a claim that their individual eligibility makes a
whole proposal feasible; the forced counterfactual remains that authority.

Faculty fairness targets are proportional to each physician's **base scheduling
capacity** across the balance window rather than equal head count. Resolve the
effective Faculty role, weekly availability templates, and monthly scheduling
rules for every AM/PM slot in the window and use that base capacity as the
physician's share of the total Faculty capacity. The physician's target share
of Faculty Precepting assignments is the same proportion of the total Faculty
assignment load in the window. Effective role and template/rule dates naturally
prorate physicians who start or end during the window, including a physician
who began three months into a six-month balance period.

Do not reduce the fairness target because of Vacation, Academic Events,
Rounding, current Precepting/Interview occupancy, or other transient Tier 0 or
Tier 1 blockers. Those facts constrain the feasible assignments for a solve but
do not silently redefine the physician's underlying workload share. Only a base
schedule slot resolving to `open` contributes to normal computed precepting
capacity. `Backup only`, `Patient care`, `Off`, and a template gap contribute
zero; a custom schedule option contributes according to its configured
scheduling impact. Backup physicians remain eligible for solver-selected
fallback assignments when necessary, but those slots do not increase their
normal expected workload share.

Each physician may instead have one optional current **Faculty Precepting
capacity override**, in weekly-shift units from 0 through 10 in tenths. The
override replaces only that physician's computed Faculty fairness capacity. For
each weekday AM/PM balance slot with an effective Faculty role, the solver uses
one tenth of the weekly value with fixed-point integer arithmetic; partial
windows therefore receive a proportional target. It does not alter Tier 0
availability, candidate eligibility, Faculty-equivalent coverage, work counts,
or the separately derived Monday-PM Late Doc capacity. The setting is current,
attributed retained history rather than effective-dated: a new solve applies it
to its whole balance window. Changing it is a retained solver input and makes
an unapplied proposal stale.

Proportional Faculty workload fairness is a bounded solver policy. A normal
Fill or Repair operation uses no more than two CP-SAT workers and gives this
fairness stage three seconds to find or prove an outcome. Whether optimal or
merely feasible, the achieved fairness value is fixed before the existing
later Late Doc policy and seeded tie-break stages run. If the fairness stage
finds no solution, the operation fails without creating a proposal or changing
the schedule. This ordinary stage-local bound does not claim that the complete
operation finished within three seconds; explicit whole-operation timing limits
remain separate diagnostic checks and retain their exact-completion contract.

After every named policy outcome is fixed, the final proposal-local seeded
tie-break starts from the valid assignment selected by the immediately
preceding policy stage and has a separate one-second CP-SAT budget to improve
only that arbitrary ordering. An optimal or feasible result is retained for
review and Apply because it cannot change the earlier lexicographic outcomes.
A feasible arbitrary tie-break is not a claim of global optimality. If this
final stage finds no solution, the operation fails without creating a proposal
or changing the schedule. Its ordinary stage-local budget remains distinct from
explicit whole-operation diagnostic limits and their exact-completion contract.

Hard scheduling invariants are not solver tuning controls. Past dates, locked
assignments, role boundaries, required coverage, and sibling Tier 1 conflicts
remain authoritative. Findings caused by missing or inconsistent source facts,
such as a missing normal rounder, produce an explainable preflight blocker
rather than allowing the solver to invent upstream data or weaken a hard rule.

Tunable policy is expressed in scheduling language rather than exposed solver
weights. It has three layers:

* non-negotiable domain constraints;
* organization defaults such as the balance window, workload-spread policy,
  weekly limits, and the relative priority of named objectives;
* solve-local adjustments such as temporarily excluding a physician, retaining
  a particular assignment, or changing the balance window for one proposal.

Do not create physician-authored scheduling preference requests or a generic
personal-preference model. In particular, weekday choices such as preferring
Monday or avoiding Wednesday are not clinic scheduling policy; exposing them
would invite an unbounded collection of individual requests. Real system-level
soft policy, including expected Community commitments and avoiding backup-only
assignments, remains distinct from personal wishes. A genuine temporary
circumstance may be entered by the scheduler as an explicit solve-local
adjustment without becoming a retained physician preference.

A proposal retains the policy/default version and solve-local settings that
produced it, exposes before/after findings, changes, workload effects, and
system-policy compromises, and carries an input fingerprint. Apply must fail
without mutation when relevant facts changed after solve. Human edits after an
applied proposal are evidence for policy refinement but do not silently train
or modify solver policy.

### Accepted retained-proposal reconciliation

Schedulers use one scheduling term, **Reconcile**, to preserve work in a
retained proposal after the ordinary draft changes underneath it. The UI does
not expose Git terms or offer another proposal as an input. If schedulers care
about more than one proposal, they reconcile and apply them serially against
the evolving ordinary draft.

Reconciliation compares the ordinary draft when the proposal began, the
current ordinary draft, and the edited proposal. A stale proposal first reports
the number of distinct shifts whose draft assignments changed since it began.
The meeting-facing reconciliation view concentrates on those underlying draft
changes, summarizes how many affected shifts were incorporated automatically,
already matched, or require a decision, and expands only useful affected-shift
detail grouped by week.

For each assignment position, a proposal value survives when the current draft
still matches the starting draft, and a current-draft value survives when the
proposal still matches the starting draft. Equal current and proposal values
are already reconciled, while changes to different positions coexist. Only a
position changed differently in both requires an explicit human choice. A
newly invalid result is conspicuous and uses ordinary assignment reasoning to
explain the current role, availability, occupancy, or source-fact problem.

Reconciliation creates another retained proposal. It never mutates the source
proposal or ordinary draft, never treats proposal history as authoritative
schedule state, and never silently resolves a same-position conflict. The new
proposal records its source and draft provenance and every human conflict choice,
recomputes findings, workload effects, balance, and compromises from current
authoritative facts, and presents those consequences through the ordinary
proposal review before Apply. It does not attempt to preserve the earlier
proposal's balance as an independent truth or silently repair balance while
carrying changes. Every carried Late Doc choice returns to pending explicit
confirmation. Concurrent or newly stale reconciliation must fail without
creating a misleading result.

Every authenticated scheduler may run and apply proposals and make solve-local
adjustments. These actions are fully attributed and retained. Organization
solver defaults are current application policy rather than user-editable
configuration; a future editing surface needs an explicit policy and authority
decision. Optional Physician-account linkage remains identity and presentation
context, not solver authorization. The Scheduling Administrators group retains
its existing additional account, Physician, and role-management authority,
while Django superusers remain the break-glass boundary. Do not add a separate
Physician-scheduler permission without observed misuse or a new explicit
product decision.

Do not begin with a generic constraint language, arbitrary user-facing numeric
weights, automatic policy learning, background job system, or solver plug-in
framework. Add named policies from observed scheduling examples and keep the
solver adapter subordinate to the authoritative domain model.

---

# 16. Gaps, conflicts, and exceptions

Regardless of resolution strategy, the system should make exceptions first-class.

Possible findings include:

* uncovered session
* insufficient faculty-equivalent coverage
* conflicting assignments
* no eligible physician
* unresolved choice between candidates
* soft-rule violation
* stale source data

Schedulers should spend their attention on these cases rather than manually reviewing every ordinary session.

---

# 17. Manual scheduling

Manual changes are normal behavior, not an exceptional escape hatch.

A scheduler should be able to:

* add an assignment
* replace an assignment
* remove an assignment
* lock an assignment
* inspect eligible alternatives
* see why someone is unavailable
* see what rule or source produced an existing assignment

Manual changes require provenance.

### Initial assignment alternatives

For a specific session and assignment position, the domain creates one immutable
candidate assessment for each physician. It separately records automatic
eligibility and manual selectability, plus an ordered set of structured reasons
with stable code, effect/severity, concise summary, detail, and retained source
identity where applicable. The assessment combines effective role and position
boundaries, Tier 0 availability sources, sibling Interview commitments, and
same-session Precepting occupancy; simultaneous facts are retained rather than
replaced by a priority shortcut.

Manual alternatives include every role-valid physician who is not already
assigned in another position in that session. Effective unavailability and an
Interview conflict do not hide or disable a physician; humans may intentionally
create invalid working state and must be able to see every reason and resulting
finding. A missing required role or another Precepting position occupied in the
same session remains structurally unselectable. The retained current assignment
remains visible even when later facts make it inconsistent.

Alternatives use four ordinary deterministic presentation tiers:

1. available expected Community Preceptors
2. other available physicians
3. backup-only physicians
4. hard-conflict or not-available physicians

An effective Float Preceptor has an additional, manual-only path.  The fixed
`Admin` schedule option is identified by its immutable system identity, not its
display text.  When no ordinary Faculty availability or Community Preceptor
recurrence is available, an effective Admin selection makes an effective Float
Preceptor `Available as Float`.  That tier sits immediately before hard-conflict
or not-available choices.  It permits a deliberate Primary, Secondary, or Late
Doc assignment without a Float-specific finding, including a Community
Preceptor + Float physician in the otherwise Faculty-only Late Doc position,
but it does not make the physician faculty-equivalent or change their
underlying role facts.  Vacation,
hard Academic Events, Rounding, Interview commitments, duplicate occupancy, and
the other ordinary hard conflicts still override it.  Community Float inputs are
additive: their Off schedule selection cannot suppress a Community recurrence.
Schedule Bot excludes the Float-only path while continuing to consider the same
physician wherever ordinary Faculty or Community availability applies.

Within a tier, physicians are ordered alphabetically with a stable identity
tiebreaker. Each alternative exposes concise status, category,
faculty-equivalent status, effective availability, expected commitment, and an
expanded presentation of every assessment reason. Backup-only, soft-event, and
hard-conflict selections explicitly state their resulting finding. This is
candidate presentation, not an automated proposal, workload score, or inferred
assignment decision.

### Accepted initial lock behavior

A lock is stored on and shown with an assignment, and a scheduler may toggle it so the workflow can be exercised before automation exists. Initially it does not prevent an explicit scheduler edit, replacement, or removal. Its authority is over future automated behavior: an assigner proposal must preserve a locked assignment unless a scheduler first changes or unlocks it. This behavior may be refined when real automated-assignment pressure exists.

The system should validate manual changes against hard constraints and clearly identify any permitted preference violations.

A draft is working state and may be saved while incomplete or invalid, including with coverage gaps, conflicts, or other hard-rule findings. Validation findings should remain visible and explain what prevents completion. Unresolved findings remain visible until resolved or explicitly acknowledged; invalidity must not be hidden or converted into apparent success.

### Initial deterministic draft findings

The first validation slice reports these hard findings:

* either of the two required primary positions is empty
* fewer than two assigned physicians are faculty-equivalent for the session date
* an assigned physician is no longer ordinarily role-eligible to precept
* an assigned physician's effective availability is `not available`
* the affected Monday-Sunday rounding week has no normal rounder

The optional secondary position creates no finding when empty. An assigned
`backup only` physician and an available expected Community Preceptor who is not
assigned create non-blocking preference findings. Expected community physicians
who are themselves effectively blocked do not create an unassigned-expectation
finding. Findings explain the applicable physicians, source facts, and rules, while
semantic invalidity never prevents a scheduler from saving another draft edit.

---

# 18. Schedule repair

A current schedule may need modification because of events such as:

* illness
* unexpected absence
* changed vacation
* changed rounding assignment

The desired behavior is **minimal perturbation**.

Example:

Bob becomes unavailable for two weeks after assignments have already been recorded.

Desired behavior:

1. Start from the current assignments.
2. Define the affected date range.
3. Make Bob unavailable during that range.
4. Preserve all unaffected assignments whenever possible.
5. Reassign only what is necessary.
6. Evaluate relevant constraints around the requested range.
7. Show exactly what changed.
8. Review the proposed repair.
9. Apply the reviewed proposal with ordinary change history.

Optimization priority for a repair will likely resemble:

1. satisfy all hard constraints
2. minimize number/severity of changes to the current schedule
3. satisfy normal preferences and fairness objectives

Do not regenerate the entire schedule merely because one input changed.

---

# 19. Temporary rules and solve-local overrides

A calculation may need rules that apply only to:

* one computation
* one date range
* one scenario

Example:

> For this repair only, do not assign Nell Vale.

These should not silently modify the canonical policy.

A solve/scenario should preserve:

* baseline scheduling state
* temporary input changes
* temporary rule changes
* date/mutation range
* result
* provenance

---

# 20. Scenario / what-if analysis

The system should eventually support questions such as:

> What happens from October 12-23 if Bea Alder is allowed two assignments per week instead of one?

A scenario should:

1. start from a known baseline
2. apply explicit rule/data deltas
3. resolve the requested period
4. compare the result with the baseline

Useful comparison output includes:

* assignments added
* assignments removed
* assignments changed
* coverage gaps introduced/eliminated
* hard-constraint effects
* preference violations
* workload changes
* total blast radius
* whether anything outside the requested mutation range changed

A scenario does not become authoritative unless deliberately reviewed and applied.

### Accepted synthetic scenario baseline

The reusable synthetic scenario owner accepts an explicit authenticated
Scheduling Administrator actor and deterministic seed. It starts only from an
empty scheduling domain; account, authentication, group, permission, and other
login-owned records are not scenario inputs and remain untouched. All scenario
writes use the ordinary application services so source and identity history
records identify that actor.

Its durable baseline starts July 1, 2020. It imports the checked-in demonstration
Faculty roster, including all 16 visibly fictional identities and their varied
ten-slot weekly templates (including Rounds selections), through the existing
roster-import semantics rather than a copied database. This roster is
demonstration input, not current clinic data or newly defined scheduling policy.
It also creates exactly three visibly generated Community Preceptors
with stable `Fake Community Doc N` names, distinct valid role profiles, and
recurring Community Precepting commitments.

Every generated choice is derived from the seed plus stable domain, physician,
and period keys, never generator iteration or mutable global random state.
Extending a later scenario end date therefore preserves already generated
facts. This scenario owner is separate from the intentionally small local
`reset-demo` fixture.

Precepting generation completes actual assignments, including Late Doc, through
the organization-local current month, validates ordinary findings, and leaves
the later attention-visible horizon open. It retains source and assignment
change history. There is no publication prerequisite or storage inventory.

---

# 21. Input schedules as first-class data

Initial upstream schedules should include at least:

## Faculty Schedule

Defines ordinary faculty availability or expected working pattern.

### Accepted initial representation

The first manual Faculty Schedule slice records effective-dated weekly templates. Each template belongs to one faculty physician and contains ten weekday slots: Monday through Friday, AM and PM. Every slot has exactly one base state: **Open**, **Backup only**, or **Off**.

A template has optional inclusive start and end dates. A blank start is unbounded into the past, a blank end is unbounded into the future, and an end date may fall in the middle of a week. A physician may have multiple non-overlapping templates. When no template covers a date, the physician's base state is **Off**; there is no unknown base-schedule state.

**Backup only** is a base-availability restriction saying the physician is ordinarily held for exceptional need, such as illness or vacation-driven scarcity. It is not the same concept as a secondary assignment: primary/secondary describes responsibility within a session, while Open/Backup only/Off describes whether and under what circumstances the physician should be considered.

An effective faculty role remains distinct from the weekly template.
Date-specific domain queries resolve the template's weekday/period state and
report role eligibility separately so current validation and a future assigner
can consume a stable surface without treating availability as an assignment.
Role changes and template corrections retain their own provenance and history.

This base template is an upstream input; Vacation, Academic Events, Rounding,
and current monthly scheduling rules may modify effective availability without
rewriting it. Historical schedules and reports use retained facts and are not
recomputed when a template changes.

This initial representation does not claim to reproduce the full semantics of the real external Faculty Schedule. Any later exception mechanism, activity-code mapping, or import adapter remains subject to domain discovery. Synthetic examples may use the approved safe names, but invented availability is test/demo data rather than scheduling policy.

## Vacation Schedule

Blocks preceptor availability independently of the physician's role.

Treat Vacation Schedule as a standalone source schedule rather than merely a
Preceptor Schedule input. Its primary calendar workspace uses a conventional
seven-day month so recorded ranges and continuity across weekends remain
visible even when a downstream schedule consumes only weekdays. It may reuse
the application's accepted month heading, navigation, and visual language, but
must not inherit the Preceptor Schedule calendar's Monday-Friday boundary.
Use the conventional US Sunday-Saturday column order and show real
adjacent-month days in the first and last week rows so ranges remain legible at
month boundaries.

Render each vacation range as a horizontal all-day-style bar spanning its
covered dates within a calendar week. A range crossing a week boundary
continues as another segment in the next row, with the physician's name repeated
on each weekly segment. Mark AM-only and PM-only ranges with explicit `AM` or
`PM` text; whole-day ranges need no extra label. Stack overlaps in stable
physician-name order and initially let the week row grow to expose every entry
rather than hiding records behind an aggregate “more” control.

Clicking unused background space in a calendar day begins a new vacation with
both inclusive dates defaulted to that day, no physician preselected, and whole
day as the coverage default. Clicking an existing vacation bar opens that
specific retained record for editing or removal. Keep a real add link in
each date heading as the keyboard and no-script path, plus a persistent `Record
vacation` action for entry without a calendar starting point. A small
application-owned enhancement may make the unused day background invoke the
same add link while ignoring vacation-bar interactions.

Initially reuse the existing server-rendered Vacation form rather than adding
a modal or a second editing implementation. Save, edit, and removal
return to the month from which the user entered the form. Existing authenticated
mutation and retained-history behavior remains authoritative.

Each Vacation owns one current optional note. Add and edit forms expose that
note, and editing replaces it rather than accumulating comments on the live
record. Retained Vacation change snapshots include the prior and resulting note
values so history shows its evolution. Removal asks for no separate note or
explanation; its snapshot retains the Vacation's final note as the prior value.

This is a pre-release direct replacement of the earlier per-change Vacation
reason representation. No current product data requires preservation, so do
not backfill or compatibility-read old reason values and do not maintain a
legacy reason presentation.

Vacation adopts `Calendar` and `List` local views. Calendar remains the default
seven-day scanning surface, replacing the current visible arbitrary-range form
and range table on that page. List shows the selected month's retained current
Vacation ranges that overlap that month, not derived daily occurrences. Order
the list by start date, then end date, physician display name, and stable record
identity so overlapping ranges remain predictable. Retain the underlying
ability to query vacations over an arbitrary bounded date range for reporting.
Retained change history remains contextual to both views rather than becoming a
third local view.

Initially show every physician's vacation on the calendar without a physician
filter. The primary scanning question is who is out on a given date, so a
filter must not hide that context by default. Add optional filtering later only
if observed calendar density creates a need for it.

Use one consistent vacation-bar palette rather than assigning persistent
colors to individual physicians. The displayed physician name identifies the record,
and explicit `AM` or `PM` text communicates partial-day coverage; color is not
the sole carrier of either meaning.

## Academic Event Schedule

Represents academic/off-site commitments that block each participant's
preceptor availability for the covered periods.

Academic Events are planned by academic year. An academic year runs from July
1 through June 30 and is always presented with both years: `2026-2027` means
July 1, 2026 through June 30, 2027. The current academic year, derived from the
organization's local date, is the default; January 2027 therefore defaults to
`2026-2027` rather than `2027-2028`.

Maintain a separate catalog of Academic Event definitions. Each definition has
a stable base name, an inclusive start academic year, and an optional inclusive
end academic year. New definitions default to the current academic year with
an unbounded end, while both bounds remain editable. It also has a positive
whole-number default required participant count, initially one, and one
expected academic-year quarter from Q1 through Q4. Each definition also owns a
default positive whole-day duration, initially one day, default coverage of
Full day, AM only, PM only, or None, and default Precepting impact of Hard, Soft,
or None; Full day and Hard are the initial defaults. A
definition may also retain optional plain-text `Typically Scheduled` source
metadata. It is displayed in the catalog and definition form/history and is
used only to order catalog, list, and scheduling choices within an expected
quarter. It never changes occurrence defaults, quarter interpretation,
attention, work remaining, availability, Schedule Bot inputs, or findings. A
definition is expected exactly once in every academic year in its effective
range.

Definitions have either `Yearly` scheduling, retaining the one annual
resolution workflow, or `Monthly rules` scheduling. A Monthly-rules definition
has no expected quarter and never produces annual unscheduled, not-occurring,
or participant-fill work. It owns retained effective-dated rules: each selects
one weekday and a nonempty set of first through fifth monthly occurrences.
Rules for one definition and weekday cannot overlap, and a missing fifth
weekday produces no event. Reading a bounded date range materializes each
matching occurrence as one retained, dated Academic Event owned by its rule.
Calendar, list, availability, master-schedule, and report reads use those
durable instances once rather than a parallel recurrence projection. A Monthly
occurrence has one rule-computed, read-only date; schedulers cannot date,
remove, or otherwise manually schedule it. Rule corrections reconcile the
materialized future horizon and retain historical instances and changes.

A Monthly definition owns assignment defaults: a positive participant minimum,
either individual default participants or collective all-available-Faculty
scope. New instances copy those defaults and are marked as default assignments.
Later definition-default changes reconcile only future default instances;
retained per-occurrence assignment edits remain Muriel-owned and unchanged.
A collective occurrence has no individual participants and applies its
configured coverage and impact to every available Faculty physician. History,
administration, and retention surfaces identify both Monthly-rule origin and
assignment-default versus scheduler-maintained assignment changes.

Active definitions may share a normalized base name only when their expected
quarters differ. The active name-and-quarter combination may not overlap in
effective academic years. Ordinary displays keep the base name alone; a quarter
is appended only where an otherwise ambiguous definition must be selected or
identified.

An annual definition is resolved by one dated occurrence or an explicit `Not
occurring this year` decision. A dated occurrence owns one or more
nonoverlapping inclusive date ranges, each of which must stay within that
academic year and shares the occurrence's whole-day/AM/PM coverage. Manual
entry begins with one range and may add more. An ordinary occurrence may contain
zero or more participants, or may be marked all available Faculty with no
individual participants. It snapshots the definition's required participant count and may
override that positive minimum for the selected year. It also snapshots the
definition's expected quarter. Zero participants is a valid `Scheduled,
unpopulated` state; a positive count below the minimum is `Scheduled, needs
participants`; meeting or exceeding the minimum, or a dated all-available-Faculty
occurrence, is complete. The minimum is
not a maximum. A not-occurring decision is also complete for the year's
outstanding-work calculation and is reversible with history retained.

Each occurrence snapshots its definition's base name and may add an optional
year-specific suffix. Display the result as `Base name - Suffix`, omitting the
separator when the suffix is blank. Definition renames affect later choices
and future occurrences but never silently rename an already scheduled or
historical occurrence. Definition count changes likewise affect future
defaults, not existing occurrence snapshots. Definition quarter changes affect
future ordering/defaults and do not rewrite existing snapshots.

Definition duration, coverage, and Precepting impact are scheduling defaults rather than permanent
constraints. Starting a dated occurrence copies all three values from the selected
definition. The scheduler may then edit the occurrence's inclusive dates, coverage, and
impact for that academic year. Later definition edits affect later scheduling
defaults only and never rewrite current, inactive, or historical occurrences.

Quarter is expected-ordering metadata, not a constraint on exact dates. Order
definitions, scheduling choices, and undated annual status groups by Q1 through
Q4, then populated `Typically Scheduled` text, then base name; empty text sorts
after populated descriptions. Continue ordering dated occurrences by actual start
date. Starting an occurrence initially sets its start date to its quarter's
first day—July 1, October 1, January 1, or April 1 within the selected academic
year. Its initial inclusive end date is that start plus the definition's
default duration minus one day. Dates elsewhere in that academic year and a
different valid occurrence duration remain valid without warning.

Keep definition year selectors compact and relative to the current academic
year. Start choices span current minus five through current plus ten and default
to current. End choices span current through current plus ten, add `Unbounded`,
and default to `Unbounded`. Always label choices with both years. These are form
input choices rather than domain/history limits; an edit form includes any
existing out-of-window stored value so old definitions remain editable as time
advances.

Only effective definitions without a live annual resolution appear in that
year's scheduling dropdown. The primary workspace provides a seven-day month
calendar and an optional academic-year List view whose dated occurrences are
ordered by start date. It separately alerts for unscheduled definitions and
scheduled occurrences below their participant minimum, including how many
physicians remain needed; not-occurring resolutions remain visible in the list but
do not remain outstanding.

Event definitions have their own management page. User-facing removal of an
unused definition deactivates and hides it rather than physically deleting it.
Once a definition has current or retained annual use—including an occurrence
later removed or a not-occurring decision later reversed—it cannot be
deactivated, and its effective range cannot exclude that used year. This
conservative rule may be revisited if it creates real friction.

Scheduling Administrators may upload the clinic's `Academic Year Calendar`
XLSX file from definition management to preview its reusable catalog rows. A
blocker-free preview carries only its normalized parsed input in a compressed,
signed, ten-minute browser token bound to the authenticated Scheduling
Administrator and the Definition-import workflow; Muriel neither retains the
raw workbook nor creates a durable preview record. Apply reconstructs that
input, rebuilds the current plan, and rejects a missing, expired, altered, or
wrong-user/workflow token without changing data, directing the administrator to
preview again. The importer selects exactly one worksheet with a supported Event column
and one consistent consecutive academic-year label, such as `2026-2027`; its
title may be the academic year, and unrelated worksheets are ignored. The
synchronous preview names the workbook academic year and separates exact
existing definitions, different existing definitions with field-level
reconciliation details, new definitions, conflicts, and reported-but-unsupported
rows before any write. A workbook initializes only an absent conditional
identity: name for a unique workbook name, or name plus quarter for a repeated
name. A new definition starts in the workbook academic year with an unbounded
end, one required participant, full-day coverage, its source quarter and
optional `Typically Scheduled` text, and a dated source row's inclusive duration
(or one day when undated). A `Faculty Meetings` row is the one explicitly
recognized all-year exception: it creates a Monthly-rules definition collectively
applying to all available Faculty, with None impact, Full-day coverage, its complete
source wording, and an unbounded Thursday rule effective July 1 of the workbook
year for the first, third, fourth, and fifth occurrences. Existing definitions and
rules remain Muriel-owned, including their quarter, effective years, scheduling
defaults, and future scheduling settings; neither exact nor different rows are
mutated. Applying a conflict-free plan may create only a missing definition and its
initial Faculty Meetings rule in one transaction. Definitions missing from the
workbook remain untouched, and free-form `Typically Scheduled` text never derives a
recurrence. `Residency Interviews` remains in the Interviews workspace. Re-uploading
an unchanged workbook creates neither definitions nor history.

After that definition plan has no pending creates or conflicts, a separate
Scheduling Administrator annual-schedule import reuses its exact row identity.
Its blocker-free preview uses the same short-lived signed browser-token boundary,
scoped instead to the Annual Schedule-import workflow. Apply rebuilds current
definition, physician, and occurrence matching before the retained transaction,
so current blockers prevent mutation and an unchanged replay remains a no-op.
It normalizes July-December dates to the starting calendar year and January-June
dates to the ending year, ignoring an embedded spreadsheet year. Explicit date
lists become one occurrence with only their contiguous inclusive ranges; gaps are
never filled. Named Faculty are matched by a punctuation-insensitive, unique
current Physician identity; ambiguous or unmatched names block application,
`All available faculty` creates no individual assignments, and resident-only
attendance is reported but ignored. A dated supported row creates or corrects
only its definition and academic-year occurrence, including ranges, coverage,
detail, note, collective Faculty state, and individual participants. Blank or
unsupported rows leave annual occurrences untouched. Applying rechecks the
preview before using ordinary retained occurrence history in one transaction;
an unchanged reimport is a no-op.

Removing an annual occurrence and reversing a not-occurring decision likewise
retain an inactive record and its full history while removing its live schedule
and availability effects. The definition then becomes schedulable again for
that year when otherwise effective. No ordinary Academic Event workflow
hard-deletes definitions, occurrences, annual decisions, or their change
records. Catalog changes never rewrite or recompute annual or historical facts.

Academic Event edits remain immediately effective retained scheduling facts,
with no draft or publish lifecycle for now. The pre-release free-form Event
representation may be replaced directly; there is no current product data that
requires compatibility handling.

## Interview Schedule

Interview scheduling is an implemented academic-year calendar and list with
a target of 20 AM shifts, immediately effective retained edits, and explicit
remaining/unpopulated status rather than numbered `Interview slot 1` catalog
choices. Only physicians with the actual Faculty role qualify; faculty-equivalent
status alone does not. More than 20 shifts are permitted, but the workspace
must visibly flag that the academic-year target has been exceeded.

An Interview shift may be scheduled before its Faculty assignment is known,
just as an Academic Event occurrence may be scheduled before its participant
requirement is filled. Reuse the shared scheduled-but-unassigned status and
workspace presentation rather than copying Academic-Event-specific markup and
styling. Keep the domain records separate: Academic Events retain definitions,
date ranges, coverage, and multi-physician participation, while an Interview
shift owns one date and zero or one assigned Faculty physician.
It has no separate title or detail field: its unique date identifies it, and
its only additional editable content is the optional Faculty assignment and
optional note.

When an external Interview date is cancelled, the scheduler removes the active
shift. Removal deactivates rather than hard-deletes it, retains its full change
history, removes it from active calendar/list and target counts, and releases
its Faculty assignment. Interviews need no `not occurring` resolution because
there is no predefined annual catalog.

The Interview workspace reports schedule-count work separately from assignment
work. Below the target it states how many shifts remain to be scheduled; for
scheduled unassigned shifts it states how many still need Faculty assignments;
above the target it states by how many shifts the target is exceeded. It shows
the completed state only when exactly 20 shifts exist, all 20 have an assigned
Faculty physician, and no shift has a hard finding.

Interview dates are supplied by an external scheduling process rather than
generated by Muriel. At most one AM Interview shift may be recorded for a
calendar date; Muriel does not add recurrence or automatic date-generation
rules for this schedule.

Any date within the selected academic year is valid without a seasonal warning;
October through January is customary, not a constraint. A date outside the
selected academic year is rejected. When that academic year has no Interview
shifts, the Schedule form defaults the date to October 1 of the academic year's
starting calendar year. After shifts exist, it defaults forward to the first
Monday-Friday weekday after the chronologically latest scheduled shift in that
academic year, regardless of record entry order, so consecutive entry is quick.
The proposed default never creates a record until the scheduler submits the
form, and the date remains editable.

When the Interview Calendar opens without an explicit month for the clinic's
current academic year, choose a useful seasonal starting month. During July
through September, open the earlier of the first active Interview month or
October, using October when no shifts exist. During October through January,
open the current clinic-local month regardless of scheduled dates. During
February through June, open the later of the last active Interview month or
January, using January when no shifts exist. An explicit valid month remains
authoritative. A non-current academic year continues to default to October;
automatic movement into a following academic year is deferred.

Interview and Preceptor assignments conflict symmetrically for the same AM.
Interview candidates use the shared availability inputs plus existing
Preceptor assignments, and Preceptor candidates exclude physicians assigned to an
Interview shift. Implement this as a shared base-availability calculation with
schedule-specific assignment conflicts rather than recursive availability
calls. Each Interview shift requires exactly one assigned Faculty physician.
Candidate ordering has one initial soft preference: a Program Director who is
otherwise available for the Interview shift and has no conflicting assignment
appears ahead of ordinary Faculty candidates. If multiple Program Directors
qualify, they share that preferred tier. This does not make Program Director
coverage mandatory. Associate Program Director has no Interview scheduling
effect for now. Faculty whose Tier 0 effective availability is `backup only`
remain assignable, appear after every `available` Faculty candidate, and
produce the same conspicuous non-blocking preference finding as a backup-only
Preceptor assignment. No additional Interview scheduling rules are accepted
today; a future rule requires a concrete scheduling example and explicit policy
decision rather than reopening the implemented academic-year, Faculty-only,
availability, and sibling-occupancy baseline.

Normal Interview assignment candidates include only `available` and `backup
only` physicians with an effective Faculty role. The human form uses one
Faculty picker ordered by scheduling desirability: preferred available Program
Directors, other available Faculty, backup-only Faculty, then clearly labeled
override choices for actual Faculty who are Tier 0 unavailable or have a
same-period sibling Preceptor assignment. Selecting a choice from the explicit
override group is the scheduler's deliberate acknowledgement; do not require a
second picker or explanation. The retained Interview shift then exposes an
explainable hard finding and is not complete, while the future assigner-facing
surface must reject that choice. The picker never includes a physician without
an effective Faculty role.

More generally, the independent driving schedules form Tier 0 availability.
Preceptor Schedule and Interviews are sibling Tier 1 schedules. Only
one Tier 1 schedule is computed or edited at a time; the sibling's existing
assignments are fixed candidate-exclusion input for the same date and period.
Muriel does not jointly solve, rebalance, or recursively calculate the two Tier
1 schedules. “First scheduled wins” describes this current-state exclusion, not
permanent priority: changing or removing the occupying assignment makes the
physician available to the sibling schedule again when Tier 0 permits it.

A bounded slot projection enumerates every recorded physician and
retain the metadata needed to explain that physician's effective role, Tier 0
availability and sources, and current Tier 1 occupancy for each requested slot.
That complete read result does not make every physician assignable. Each
schedule applies its accepted role boundary before constructing candidate and
override choices: Interviews require an effective actual Faculty role, while
Preceptor assignments require the role applicable to their responsibility:
Primary accepts an effective Faculty or Community Preceptor role, while
Secondary requires an effective Faculty role. Availability and sibling
conflicts may be explicit override conditions; absence of the schedule's
required role is not an override. Correct the role fact rather than crossing a
qualification boundary.

## Master Schedule

The Master Schedule is Muriel's read-only operational front door. Its initial
view is one Monday-through-Sunday week; a monthly master view remains future
work. Global navigation labels the destination `Schedule`, places it before
Precepting, and ordinary successful sign-in leads there. The workspace itself
uses the explicit title `Master Schedule`. Existing specialist workspaces remain
the places where source facts are changed.

The weekly presentation is schedule-centered: the clinic grid uses
its Monday-through-Friday working dates, while Supporting Schedules use the
conventional Sunday-through-Saturday column order. Compact rows represent
operational slots or closely related slot families. Physicians appear as the
contents of the relevant cells rather than receiving permanent rows of their
own. AM and PM content stays aligned across the week, and closely related
positions may share a compact cell when their identity remains obvious.
Both grid headings use the compact `Mon 8/24` date form.
Assigned work is primary; effective availability and blocking information are
concise supporting contents for the relevant slot, not a physician directory
embedded in the calendar.

The view composes Precepting and Late Doc assignments, Interviews, normal and
casual Rounding, Community Precepting expectations, Vacation, Academic Events,
and observed Holiday closure. Week-level and all-day facts must remain
understandable without being misrepresented as AM-only data. The ordinary
global physician focus filters or emphasizes applicable names and facts without
changing the stable slot grid; `Everyone` remains the default.

The Master Schedule composes existing authoritative read services. It does not
persist a parallel schedule, claim editing or publication authority, or invent
new conflict semantics. It is a read-only, print-first output rather than a
navigation surface, so schedule contents do not link to owning workspaces or
editors. Existing current/future findings and incomplete required work remain
visible in compact form. The selected Master Schedule week remains operational
as a whole until the following Monday begins in the organization timezone, so
its complete clinic Monday-Friday schedule retains findings even after
individual weekdays pass. Older weeks are displayed without newly computed
operational warnings while retaining their source facts.

Its compact title is contained in the shared section-header treatment, with the
PDF action aligned alongside it; the PDF uses the equivalent non-interactive
contained single-row title treatment, including its Monday-Friday selected
schedule period and calendar year inline. Its PDF action exports the selected
current week and physician focus from this same projection as a read-only posted
schedule. It is not a published revision or an independent snapshot authority.
It prefers portrait paper when both weekly grids retain one-inch day columns;
otherwise it uses landscape. The
layout retains the Monday-Friday clinic and Sunday-Saturday supporting grids,
including their counterclockwise Morning and Afternoon row-group labels,
compact names, conditional rows, and findings without editor or navigation
controls. A stable week-based filename and one organization-local capture
timestamp at the bottom are its only provenance.

The application-owned calendar lattice, period controls, surfaces, and state
presentation are reused where their semantics fit; any added variant is narrow
and reusable rather than tied to the Master Schedule route.

Its weekly read projection has two presentation-neutral grids. The first is a
Monday-Friday clinic grid with one concise `Clinic` column header spanning its
two stubs and accessible clinic-schedule labeling. Morning and Afternoon row-group
headers are rendered counterclockwise in a narrow content-sized leftmost stub, with tight horizontal
padding and labels centered across their full row spans and within the stub,
with their first letters at the bottom. Each spans its applicable position rows,
followed by the adjacent horizontal position stub:
Preceptor 1, Preceptor 2, Secondary, Interviews in the applicable Morning period,
and Late Doc in the applicable Monday Afternoon period. Interviews appears only
when a displayed weekday falls in October through January; for a January week,
it disappears after the academic year's final scheduled Interview shift, but
remains for an academic year with no scheduled shifts. It has no redundant
clinic-and-Faculty summary heading and also carries a separate Faculty
availability group with one compact daily list of physicians
who retain an authoritative `available` period not occupied by a displayed
assignment. A name carries `AM` or `PM` only when that detail is needed to show
partial-day availability; availability explanations and source navigation are
omitted. The second is a Sunday-Saturday supporting-schedules grid with a
concise heading that does not repeat the date span, and separate Rounding,
Community Precepting, Vacation, Academic Events, and Holiday groups, in that
order. Normal and casual Rounding and Community Precepting show
deduplicated physician names. Academic Events show a short event title followed
by deduplicated participant names. Vacation shows a physician name alone for a
full-day entry and adds `AM` or `PM` only for a partial-day entry; Holiday text
is equally concise. These compact fact forms retain their incomplete, missing,
or conflict flags. Effective availability may therefore cite Vacation or
another source while that same owned source fact remains visible in the
supporting grid; this is source visibility, not a second availability
calculation. The projection additionally maps each live finding to the
applicable group, row, date, and period so a presentation may flag it without
changing source ownership or focus semantics.

## Holiday Schedule

The Vacation workspace includes a compact `Holidays` list for one calendar
year. Each retained yearly occurrence belongs to the fixed clinic catalog:
New Year's Day, Memorial Day, Independence Day, Labor Day, Thanksgiving Day,
Christmas Eve, and Christmas Day. Deterministic observances are computed when
the year is first inspected. New Year's Day and Christmas Day use ordinary
weekday observance; Memorial Day, Labor Day, and Thanksgiving are derived from
their calendar rules; and weekday Christmas Eve closes PM only. A weekend
Christmas Eve is explicitly `not observed`.

Weekend Independence Day remains explicitly `unscheduled` until a scheduler
records its observed date. New Year's Day, Independence Day, and Christmas Day
may be scheduled or historically corrected with an optional note; their
identity and coverage remain fixed. Computed-only holidays have no edit action.
The selected-year list calls out unscheduled observed holidays as work
remaining, while `not observed` is complete. The list has calendar-year arrow
navigation and a filtered year picker, and Holiday history retains
materialization and scheduler changes. This is the only holiday-management
surface; a holiday calendar and a new top-level workspace are out of scope.

Every unified calendar tints an observed holiday's affected date header and the
covered day or AM/PM period with the same pale-purple informational treatment.
Range calendars omit duplicate header text and place the compact holiday label
first in their ordinary item stack rather than a dedicated closure lane.
Full-day range items show only the holiday name; the Christmas Eve PM closure
reads `Christmas Eve, PM`. Slot calendars retain the compact header label at
the same prominent scale as `Closed`. Accessible names retain the holiday and
full coverage context. Holiday calendar content is informational; Holiday
management remains available through the Vacation workspace navigation. Slot
calendars have no closure lane: the projection is informational for schedules such as Academic
Events, Rounding, and Vacation, but matching Precepting, Community Precepting,
Late Doc, and Interview coverage has one compact `Closed` cell with an
accessible holiday and coverage name. Closed coverage has no assignment gap,
expected Community occurrence, candidate, or solver requirement. New
Precepting, Late Doc, and Interview mutations reject it with a holiday-specific
error. Existing current assignments and Interview shifts are retained and
produce hard holiday findings until a scheduler removes or reschedules them;
historical records are retained without operational recomputation. Holiday
occurrences are included in solver fingerprints so a changed closure makes an
old proposal review stale.

Precepting month and Schedule Bot Current/Proposed overview calendars project
position-targeted hard and non-blocking findings only through the occupied
slot's ordinary conflict or preference styling. Visible slot content remains
the physician name; accessible labels may name the state. Positionless
session/week findings retain their existing handling and never color an
arbitrary physician. Focused Precepting editors, Schedule Bot proposed-week, list,
and unresolved-finding presentations retain their detailed
finding language.
Retained holiday findings use this same rule. Schedule Bot applies the
appropriate Before or After findings to Current or Proposed respectively, while
an empty or stale proposed closed position remains `Closed` rather than a gap
or a holiday finding.

## Rounding Schedule

Defines rounding assignments and casual-rounder substitutions.

## Community Preceptor Schedule

Defines community-preceptor availability/recurring participation.

Although initially entered for the Preceptor Schedule, these schedules should be representable and publishable in their own right where practical.

Avoid a schema in which they exist only as "preceptor input fields."

---

# 22. Source normalization

Future integration should follow:

**external source → adapter/import → normalized scheduling facts**

Do not make spreadsheet rows the permanent domain representation.

Normalized records should capture fields appropriate to the event, likely including:

* physician
* source schedule
* type
* date/time or AM/PM period
* effective interval
* recurrence if appropriate
* source identity/reference
* notes
* provenance
* revision/change information

Exact schema is not yet fixed.

---

# 23. User experience principles

The system will primarily be used by administrative staff and physicians.

It should not expose optimization machinery unless necessary.

## Application shell and navigation

Every authenticated page uses a consistent shared application shell rather
than page-specific routes back through the monthly workspace. Its global
navigation exposes these destinations in this order:

1. Schedule
2. Precepting
3. Rounding
4. Vacation
5. Academic Events
6. Community Precepting
7. Interviews
8. Physicians
9. Reports

The current destination is visibly identified. Account identity and Sign out
are utility controls, separate from the primary destinations. Month/week/date
movement is local Preceptor Schedule navigation; section history and
add/correct/remove controls remain contextual to the page they affect.

`Schedule` opens the read-only Master Schedule. Its local destinations, `Work
Remaining` and `Lead Times`, respectively project organization-wide actionable
work and maintain the timing policy for its work families. `Accounts` is a
superuser utility control rather than a primary navigation destination.

`Academic Events` is one global destination. Inside it, local navigation uses
`Calendar`, `List`, and `Definitions` in that order, with Calendar as the
default. Definitions is not repeated in the global navigation. These are
ordinary server-rendered Django routes and templates, not client-side tabs or
an SPA; history and mutation controls remain contextual to the active view.

Academic Events and Interviews use the same bounded `Previous Academic year`,
`Academic Year` picker, `Next Academic year` cluster on Calendar and List
views. Its arrows use exactly the picker option range, preserve the active
workspace view and applicable calendar month, and become visibly and
accessibly unavailable at either bound. The native GET form remains available
without JavaScript; the shared period-picker enhancement submits a changed
valid selection immediately.

`Interviews` is a global destination immediately before `Physicians`. Although
Interviews and Preceptor are sibling Tier 1 schedules, Interviews are seasonal
and do not need to sit beside Preceptor Schedule in the global navigation.
Its local navigation contains `Calendar` and `List` only. Interview dates are
the schedule records themselves rather than occurrences of a Muriel-maintained
definition catalog, so Interviews has no Definitions or other empty supporting
view. Retained Interview history is a contextual link available from both
views, not a local-navigation tab.

Academic Events is the reference pattern for schedule workspaces. Interviews
should reproduce its established calendar-first workspace language as
closely as the domain permits. Community Precepting uses `Calendar`, `List`,
and `Rules`; Vacation uses `Calendar` and `List`; and Rounding retains
`Calendar`, `List`, `Normal Rounding`, and `Casual Rules`. These are local
workspace variants, not a requirement that every source schedule have the same
views or behavior.

Ordinary HTML data tables use one application-owned structured presentation
contract and Django renderer. View presentation code supplies typed columns,
rows, cells, semantic row/cell tones, simple GET links/actions, optional footer
rows, an empty state, and a bounded width variant. The renderer owns semantic
table sections, header scopes, the scroll frame, density, borders, and tone
application; it does not infer domain meaning from labels or accept arbitrary
trusted HTML. Domain-specific projections continue to decide what data appears
and how states such as danger or attention are classified.

This standard renderer is for ordinary history, detail, list, rate, report, and
summary tables. Tables with inline POST forms or irregular mixed rows, and
schedule/layout grids such as Master Schedule and Precepting schedule tables,
retain specialized templates while consuming the same low-level CSS primitives
where applicable. A specialized table is an explicit fit decision, not a
second generic renderer or permission to duplicate ordinary table structure.

Its initial cell vocabulary is deliberately small: plain text, dates and
times, muted metadata, retained code-like values, status text, and ordinary GET
links or action links. It does not accept rendered or trusted HTML, template
callbacks, inline POST forms, row or column spans, schedule grids, or domain
calculations. A table that needs one of those remains specialized until a
separate accepted presentation contract proves a reusable need.

Strengthen that pattern as a small reusable, application-owned schedule
workspace shell. Reuse the workspace frame, period-control placement and
progressive enhancement, stacked surfaces, contextual action/history placement,
and structural calendar/list primitives where behavior matches. All calendar
workspaces use one application-owned geometric renderer that supports five- or
seven-day columns, named lanes, multi-day ranges, whole-week items, single-day
cards, optional actions, and extensible status decoration. Keep projections,
source controls, table columns, calendar entries, actions, and status meaning
local to each schedule rather than expressing them as generic renderer policy.
This is server-rendered template/CSS reuse with minimal progressive enhancement,
not a third-party calendar, metadata-driven page builder, rendering framework,
SPA, or client-side domain model. Its internal boundary should remain clean
enough for possible later extraction without prematurely designing a public
library API.

Precepting's Calendar is the default month view and opens assignment slots
directly in focused editors. List provides the chronological reading alternative;
its actions open session editors, or the calendar for a closed empty session.
Schedule Bot owns proposal workflows. The Precepting Week workspace and route
are removed. Direct editors default to their assignment date's month; links
from a calendar preserve its originating month across edits, validation, and
history. Calendar remains selected while editing. `Schedule > Work Remaining`
provides the organization-wide actionable list; Precepting entries open the
relevant month at the affected week row. Genuine weekly scheduling rules and
Schedule Bot's proposal review remain unchanged.

The shared header uses an original Random Health logo with a rounded
green, blue, and purple emblem and sans-serif lettering. It identifies the
facility as `Family Practice Clinic` when space permits; the facility label may take a less prominent or
collapsed treatment on narrow screens rather than crowding the working
navigation. The header presents fictional organization branding.

The product identity is `Muriel`, with `Physician Scheduling` as its visible
descriptor. `Muriel` remains identifiable at narrow widths even if the facility
identity becomes more compact. Browser document titles use the form
`<page name> | Muriel`; the shared base template supplies the product suffix so
individual pages own only their meaningful page name. The visual palette uses
the logo's familiar green, blue, and purple as controlled accents over quiet
neutral surfaces, while preserving accessible contrast and keeping ordinary
schedule content visually calm.

Shared presentation distinguishes three semantic surface tones while retaining
the established palette values: `subtle` (`#f6f8fa`) for generic container
chrome and row-label or role stubs, `calendar` (`#e2e6ea`) for date and weekday
coordinate headers, and `schedule group` (`#e6f0ef`) for semantic schedule
groupings. Calendar adjacent-month treatment remains a calendar-specific
modifier rather than a fourth surface role. Master Schedule day/date labels use
the shared calendar date-header typography; Clinic and Schedule coordinate
stubs retain their own heading treatment. Shared CSS custom properties and the
PDF palette own these roles so a future palette change does not require
consumer-by-consumer replacement.

In the initial Django implementation, a shared `base.html` template owns this
shell, global navigation, utility controls, and common styling. Individual
pages supply their title, local navigation/actions, and content through
template inheritance rather than duplicating complete HTML documents.

The unauthenticated sign-in page uses the same branded header, but its panel
does not repeat the schedule module as a competing product title. The panel
identifies the immediate sign-in action and gives brief context about working
on schedules rather than only drafts. Its labels, controls, errors, and action
use a conventional stacked form relationship that stays clear at both normal
and narrow widths. Exact spacing and visual weight are chosen through browser
review rather than treated as fixed design constants.

Keep this shell structurally simple. Use Django's existing template and static
file facilities, semantic HTML, and a small application-owned CSS surface.
Prefer responsive CSS that keeps the primary destinations visible over a scripted
menu. Do not add a CSS framework, JavaScript framework, component library,
asset bundler, external font/CDN dependency, or navigation package without a
concrete requirement that the existing stack cannot meet.

Server-rendered semantic HTML is the functional baseline. Use minimal
JavaScript only for concrete, local usability improvements that the native
HTML/CSS experience cannot provide consistently. Such enhancements remain
small and application-owned, use capability detection, preserve native or
server-rendered fallback behavior where practical, and do not create duplicate
client-side domain state. Do not introduce a JavaScript framework, package, or
build step merely to organize a small enhancement.

## Workspace physician presentation scope

An authenticated scheduler may set a top-level presentation scope to zero or
more existing Physicians. No selection means `Everyone`; a nonempty selection
means `Selected physicians only`. It is a browser-session convenience for
scanning schedule workspaces, never an authorization, organization, saved
preference, report parameter, or domain-query scope.

The scope is applied after each workspace obtains its normal full-domain read
projection. It may narrow only the explicit display values below. Completeness,
validation, findings, candidate eligibility, history, mutation,
and retained schedule/source facts always use the complete domain data. An
active scope must say that it is a presentation focus and must not make a
partial display look like a complete schedule.

### Per-workspace presentation contract

* **Precepting month and focused editors:** Selected physicians' assignment names and
  focused assignment marks may be shown; nonselected assignment names may be
  collapsed or omitted. Every weekday AM/PM session, each primary/secondary
  position's occupied-versus-open state, coverage/error
  mark, and non-person-specific week context remains visible. All session and
  week findings, their full affected-physician context, full assignment and
  gap totals remain complete. Physician focus may
  not narrow focused-editor alternatives or hide an existing assignment
  position, including one occupied by a nonselected physician.
* **Rounding Calendar and List:** Casual-rounder occurrences may narrow to
  selected physicians. Every Normal Rounding week row in the inspected range
  remains present: show selected normal-rounder names, and represent a
  nonselected recorded normal rounder as occupied or assigned outside the
  focus so the week never looks unassigned. Missing and inconsistent
  normal-rounder marks, the complete work summary, and full week status remain
  visible. The **Normal Rounding** range retains every week row and does not
  narrow its assignments; only the **Casual Rules** physician roster and rules
  may narrow. Their date/range context and any missing-normal-rounder rows
  remain complete.
* **Vacation Calendar and List:** Vacation bars and retained-range rows owned
  by selected physicians may narrow. Calendar dates, range boundaries, and the
  active-scope statement remain visible; an omitted out-of-scope vacation must
  not be rendered as an available or clear day. Vacation has no filtered
  completeness total or availability conclusion. Directly opening an
  out-of-scope vacation still shows its full record and permits its normal
  authenticated workflow.
* **Academic Events Calendar and List:** A dated event with one or more
  selected participants may appear with focused participant names. Every
  scheduled event that is unassigned or underfilled remains visible even when
  it has no selected participant, because it is outstanding work. A fully
  filled event with no selected participant may narrow out. Every retained
  incomplete event preserves its title, period, full assigned/required counts,
  status, and an indication that nonselected participants are outside the
  focus, so it never looks empty or less populated than it is. Definitions,
  unscheduled definitions, not-occurring decisions, underfilled-event alerts,
  physician-needed totals, and their status meanings remain complete because
  they are annual planning authority rather than a physician list.
* **Interviews Calendar and List:** Every scheduled shift row/card remains
  visible because each is a counted work unit. Show selected assignee names;
  represent nonselected assignees as occupied outside the focus, and preserve
  unassigned shifts as unassigned. The annual target, complete/remaining/
  exceeded totals, hard findings, and all schedule-count and assignment-work
  summaries remain full-data values.
* **Community Precepting Calendar, List, and Rules:** Expected-commitment
  occurrences and physician-owned recurrence-rule rows may narrow to selected
  physicians. A retained selected occurrence continues to show its AM/PM
  period and supporting-rule link. This workspace has no fill or coverage
  semantics, so no completeness state is inferred from an empty scoped result.
  Direct links to any rule remain available and show the full rule.
* **Physicians directory and detail:** The directory, every local directory
  view, Schedule Options, and overall Physician history are never filtered.
  A direct physician detail page remains an explicit identity context and is
  never suppressed or altered because the physician is outside the active
  scope. Its role, availability, source-rule, and retained-history context is
  complete for that physician.

### Histories and mutation forms

All global and per-record history pages remain complete and unfiltered. Create,
edit, correction, removal, assignment, role, availability, and recurrence-rule
forms retain their normal full choices and candidate ordering. A scope may not
remove a physician from a picker, preselect an unrelated physician, suppress
an override candidate, or change validation or mutation authority. A direct
form for an out-of-scope record remains usable and returns to its normal
contextual workspace. The header may continue to indicate the active scope on
these pages, but the scope applies no narrowing there.

### Session and interaction rules

* The authenticated Django session is the sole persistence boundary. It stores
  only a validated, de-duplicated set of existing Physician identities; it is
  cleared by the ordinary session/logout lifecycle and creates no database
  preference or cross-user state.
* Submitting the multi-select replaces the current selection. `Everyone` is a
  one-step explicit reset that removes the session value. Unknown, malformed,
  and deleted submitted identities are discarded; duplicate identities are
  de-duplicated. Inactive existing physicians remain valid selections and can
  yield an empty scoped display; deleted identities are discarded whenever the
  scope is read. If nothing valid remains, the scope is `Everyone`.
* Browser tabs share the same server session. A successful scope submission in
  one tab is used by later requests from every tab; pages already rendered do
  not need client-side synchronization. The last successful submission wins.
* The control is an ordinary CSRF-protected server-rendered form with a native
  multi-select/checkbox fallback and an explicit submit/reset action. JavaScript
  may improve selection scanning but cannot be required to set, clear, or
  retain the scope.
* Every authenticated page exposes a persistent, programmatically labelled
  scope control and a visible active-scope summary naming the selected
  physicians. The summary states `Everyone` when clear and, when active,
  identifies the page as a selected-physician presentation rather than a full
  schedule. Its state change is announced accessibly without relying on color
  or an icon.
* On success, the scope form uses a post-redirect-get return to the current
  page and retains only a validated same-site path and query. It must reject
  absolute, cross-origin, malformed, or otherwise unsafe return targets and
  fall back to the ordinary workspace destination. Existing local period and
  view query context is preserved only through that validated return path.

### Reports

Reports are deliberately outside this scope contract and currently use complete
domain data, not the session presentation scope. Whether a future report should
add a physician parameter or its own report-local control remains a separate
reporting decision. The workspace scope must not be inferred as report behavior.

## Reusable scheduler form presentation

Scheduler-facing forms use a compact application-owned presentation vocabulary
rather than Django's unarranged default output, repeated page-specific styling,
or a third-party form/UI framework. This vocabulary is ordinary semantic HTML,
small template fragments where they remove real repetition, and shared CSS for
page introductions, subtly bounded form sections, section headers and bodies,
field grids and groups, read-only values, derived previews, validation messages,
compact choice groups, and action areas. It is not a form schema, rendering
engine, layout DSL, component library, or promise to automatically restyle
every form.

The first adopter is the Academic Event add/edit workflow. Its accepted visual
direction is a quiet page introduction followed by four compact subtle sections:
Event, Schedule, Physicians, and Notes and actions. Preserve the established Muriel
shell, typography, and color tokens rather than reproducing a prototype's shell.
The semantic `h1` is the form page title and uses a shared restrained `1.35rem`
treatment with `600` weight and a compact line height. It remains clearly above
the `1.1rem` section-heading tier without scaling up with viewport width or
becoming oversized. Visible page-level `h1` elements in forms, workspace
surfaces, solver views, and history views share this tier;
purpose-specific calendar/table headings and visually hidden semantic headings
retain their own treatment. An
eyebrow or kicker is optional and is omitted when it would merely repeat the
page title's action, as on Schedule Academic Event and Edit Academic Event.
Event year and selected definition are read-only context, while the optional
event detail remains editable and drives a lightly purple-accented `Displayed
as` preview. Dates and the required participant minimum form a compact grid;
whole-day/AM-only/PM-only coverage is one native radio group. Notes and the
primary/return actions share a restrained final section. Add and edit retain
their distinct headings and actions while using the same presentation.

Physician multiple-choice input may use one specific progressively enhanced
picker pattern where directory size creates real scanning pressure. The server
renders authoritative native choices and selections in a bounded usable
fallback. Application-owned JavaScript may filter those rendered choices by
displayed name and mirror their checked state into removable selected-physician
tokens and a count. Tokens are a convenience over the native controls, not a
second state store: removing one changes the underlying submitted choice, and
JavaScript absence leaves the form complete. Do not generalize this into a
frontend component framework or add remote search before scale requires it.

Selected-physician tokens use a compact name-chip treatment. The selected area
provides one quiet instruction, and each chip has a small conventional remove
affordance with the accessible name `Remove <physician name>` rather than visibly
repeating `Remove` after every physician. Activating a chip continues to update
the authoritative native checkbox and existing live count.

The accepted form vocabulary applies across Muriel-owned create, edit,
correction, removal, and focused action forms outside the calendar workspaces.
Adoption includes Physicians and effective-fact maintenance; Vacation, Rounding,
Community Preceptor, and Academic Event definition maintenance; focused
assignment work; and the Faculty Availability template. Use shared primitives
at the highest practical level while allowing workflow-specific grouping.
Small inline actions should remain compact rather than imitating a full form
page.

Faculty Availability needs a purpose-built presentation within that shared
vocabulary. Organize its ten Monday-Friday AM/PM fields as one compact,
easy-to-scan week and offer Open, Backup only, Patient care, and Off as direct
keyboard-operable choices. Preserve ordinary Django submission and a complete
no-JavaScript experience. Effective start and end remain independently
optional, and no new interaction rule should reject otherwise harmless source
facts.

This adoption does not restyle the sign-in page, Django administration,
calendar grids, calendar navigation/pickers, or calendar interaction behavior.
It does not add a renderer, form schema, frontend framework, third-party form
package, or new scheduling semantics.

## Academic Events cleanup presentation

The Academic Events seven-day calendar uses the same visible date/content-cell
boundaries and selected-versus-adjacent-month treatment as the Vacation
calendar, while retaining its own event bars and read-only interaction.

Academic Event definition entry uses `Add Academic Event definition` with a
brief purpose statement. The definition form labels its stored base name as
`Name` and its default period as `Affected coverage`. Definition removal uses
the ordinary form-section header/body geometry with a restrained destructive
variant; it does not change deactivation, retained-use, or history behavior.

The Academic Events List presents a collapsed `Monthly-rule events` disclosure
first, followed by visible `Scheduled yearly events`, `Unscheduled yearly
events`, and `Yearly events not occurring` surfaces. The monthly disclosure
contains only serialized projected occurrences; the three yearly surfaces
contain only annual resolutions or unresolved yearly definitions. It labels the
assignment count `Assigned` and presents the existing domain status `Complete`
as `Filled` in this view only. Scheduling controls use the standard
label-over-picker field with its action aligned beside the control.
Annual-resolution navigation and POST actions remain semantically distinct but
are presented as one compact action group. Lower resolution tables are denser
than the primary schedule table while retaining readable targets and focus
states.

Definitions are visibly split into `Monthly-rule definitions` first and `Yearly
definitions` second, each using the ordinary definition table without a
redundant Type column. A Monthly-rules definition shows `Not applicable` for
Expected quarter. Projected occurrences have `Projected` status and a `Manage
rules` action to their owning definition rather than an editable occurrence
action. Yearly occurrence rows retain their recorded status and `Edit` action.

## Academic Events assignment-state presentation

Academic Event workspaces use assignment language rather than repeating that a
visible occurrence is scheduled. Calendar event bars always show their
assigned/required participant count. Incomplete bars also say `Assignment
incomplete`; filled bars rely on their completed color and omit status text.
The List Status column says `Assignment incomplete (N needed)` or `Filled`.
These are view labels only; retained domain status values do not change.

Unresolved annual events use the action label `Schedule`. The Unscheduled
events and Not occurring this year tables size to useful content with a
sensible maximum and wrap long content instead of stretching sparse columns
across the workspace.

The occurrence form describes its Event section as the full name for the
scheduled event, calls period-of-day `Affected Coverage`, and calls its physicians
section `Assigned physicians`, with `Filter physicians`, `Physicians`, and `Click on a name
to remove it.` for the picker. Event removal and definition removal share the
same destructive form-section structure and presentation while preserving
their distinct service behavior and retained history.

## Schedule first

The primary working view should resemble a familiar schedule/calendar/grid.

The month shows weekday AM/PM sessions and their assigned preceptors. Select an
assigned name, unfilled required position, optional Secondary slot or Monday
Late Doc slot to open its existing focused editor. Save, removal and cancel
return to the originating month, including when the slot's actual date belongs
to an adjacent month. Validation, history and exception actions preserve this
origin. Physician focus survives navigation without restricting mutation choices.
The Week workspace is removed. Direct editor URLs without origin context return
to the assignment date's month. Session findings and retained history remain
available in the focused editor. Week finding details remain available through
the existing List and focused editors and the owning Rounding schedule.

The month retains one compact calendar matrix with a single Monday-Friday
column-header row. Each week is a row group with a prominent date row and lighter
AM/PM subrows. The narrow left gutter holds period labels; week date-row gutter
cells contain no warning or status badges.
There is no whole-week click action. Slot links retain date, AM/PM and position
context for assistive technology and work with the keyboard.

Leading and trailing weekdays outside the selected month keep their actual
Monday-Friday positions and edit their actual session. AM and PM sessions line
up across weekdays, independent of individual cells' content height.

A week appears in a selected month only when at least one of that week's
Monday-Friday dates belongs to the month. A month touching a calendar week only
on Saturday or Sunday does not show that workweek. For example, August 2026
starts with the August 3-7 workweek, while its August 31-September 4 workweek is
shown because Monday August 31 belongs to August.

The month and Schedule Bot proposal review share assignment positions and status
meanings through common presentation structures and template fragments. Do not repeat `Preceptor 1`, `Preceptor 2`, and `Secondary` on
every session or add a separate position-order explanation: position conveys
the three slots visually, while accessible names retain the role meaning. The
month uses compact gap/status signals rather than verbose finding explanations;
the slot editor provides detailed findings, assignment changes, and contextual
history.

The month does not repeat exception summaries or warning indicators for each
week in the gutter. Session-level status remains visible in the grid, and the
month Work remaining summary retains its counts. Consequential week-level
findings remain available in the List and focused editors and through the
owning Rounding schedule; removing gutter badges does not suppress findings.

Above the calendar, the month page uses the selected month as its sole page
heading. It does not repeat the Preceptor Schedule section name or apply one
month-wide `draft` label. A compact month-navigation
toolbar places an explicit previous-month action, then a native month picker
then an explicit next-month action. The picker retains an accessible name
without displaying redundant `Month` text and remains wide enough for its full
value alongside the browser-supplied icon. Directional chevrons reinforce the
previous and next labels. A small feature-detected progressive enhancement may
call the standard native picker capability when the user clicks anywhere in
the month input. Once initialized, it hides the separate `View` action and
submits the ordinary GET form when the user commits a different valid month.
Opening or canceling the picker, reselecting the current month, or leaving the
value invalid/empty does not navigate. The server-rendered form retains its
submit control as a fallback if the enhancement is disabled or unavailable.
Viewing a month continues to use the accepted underlying draft/session
lifecycle without presenting submission as a distinct Open Draft operation.
That lifecycle is lazy structural initialization: opening a Preceptor month
or submitting an authenticated valid Solve range, may idempotently ensure
the owning monthly `SchedulePeriod` and weekday AM/PM `PreceptorSession` rows
exist. Solve initializes every calendar month intersecting its normalized range
before ordinary preflight, so an untouched future month has its normal retained
draft structure while missing rounders and other real blockers remain visible.
These empty structural rows are not a human assignment decision and do not
require assignment-change history or an explanatory note. Month and
Solve entry paths use the same explicit, concurrency-safe initialization
boundary and require the ordinary authenticated workspace authority.
Initialization never changes assignments or
alters the meaning of retained historical facts; opening a historical period may
create only its previously absent empty structure.

The month picker shares the progressive enhancement used by other schedule
period controls, including the weekly Master Schedule. Keep ordinary GET submit
controls when scripting or native picker capabilities are unavailable. The
browser owns its native popup; the application does not recreate it.

The month picker identifies the selected month/year, so the calendar retains a
visually hidden semantic heading rather than repeating a visible month heading.
The month omits page-level assignment-progress and aggregate hard/preference
totals; compact slot findings and Work Remaining communicate current work,
while focused editors provide detailed findings and history.

Within each month week group, the date row uses a visible neutral gray
and a restrained heavier top divider so it reads as the header for that whole
workweek. An adjacent-month date heading uses a clearly darker neutral gray
plus muted text so the boundary is apparent. AM/PM row-heading cells use a
lighter neutral approximately halfway between the white assignment cells and
the week-header gray, so they read as sub-group labels rather than peers of the
week header. This hierarchy does not extend gray into assignment cells, and the
single Monday-Friday column-header row keeps its existing higher-level
treatment.

Calendar views begin with the same small semantic presentation primitives: a
scroll frame and surface, weekday and date headers, selected- and
adjacent-month hierarchy, week boundaries, and shared typography, spacing,
borders, and responsive baseline. One application-owned geometric renderer
owns those primitives for every calendar and accommodates five- or seven-day
columns, named lanes such as AM/PM, multi-day ranges, whole-week spans,
single-day cards, optional cell/item actions, and visible status decoration.
Each domain supplies explicit presentation values and narrow content partials
for its actual records and actions; those are not separate renderer bases.
Calendar display projections, persistence, and use-case policy remain separate.

Adjacent-month weekdays in an edge week show their real assignments and status
in a visually muted treatment rather than appearing as empty placeholders.
Each slot opens its focused editor for the actual assignment date and returns
to the originating month. The same dates may therefore appear, muted, in one
neighboring month and as ordinary dates in their own month.

Within each AM/PM session, the top two positions are required Preceptors and the
bottom position is the optional Secondary. Calendar visual tone follows the
severity of the finding rather than the structural state of the slot: any hard
finding uses the established danger treatment, while preference-only findings
use the attention treatment. A missing required Preceptor is therefore shown
as danger with visible `Unfilled` text and a hard-finding icon or equivalent
accessible cue. An empty optional Secondary uses a quiet neutral marker and
does not look like a coverage failure. The same hard-before-preference
precedence applies to finding-bearing calendar cards, spans, compact status
decorations, and calendar-adjacent status surfaces. Color never carries the
meaning alone; structural states such as unfilled, assigned, optional-empty,
and closed remain available for wording and behavior without overriding the
finding severity's tone.

List and maintenance-table emphasis remains deliberately selective rather
than inheriting a blanket rule from calendar severity. An incomplete Normal
Rounding row in both the Normal Rounding source view and the consolidated
Rounding List, and an unscheduled Holiday row, use the danger treatment because
each is a compact required-work item. In the Interviews List, a row uses danger
when it carries the specific hard finding that an assigned physician is
unavailable, but other incomplete Interview rows are not automatically red.
These selected cases share one cell-owned danger-row treatment with the same
hard-finding intensity; ordinary cell backgrounds must not hide it. Academic
Events List retains its established presentation until scheduler feedback
supports a change.

Compact findings inside ordinary data tables use a distinct table
presentation rather than the padded standalone `.finding` block. The table
cell owns the hard or preference background through its borders, with hard
taking precedence when severities are mixed, while individual messages form a
tight unboxed stack inside the cell. Required-position statuses such as
`Unfilled` use the same cell-owned tone without adding an inner card. Detailed forms,
solver explanations, reports, and other standalone finding content retain the
roomier finding component, and a cell finding does not imply that its entire
table row should be emphasized.

In the month calendar, assignment controls remain per position. An
assigned physician's name is the entry point to inspect, replace, lock, or remove
that assignment through the existing focused editor. An empty required
Preceptor opens the same focused editor; the empty optional position remains a
quieter action.
Do not repeat separate `Change` links beside names, and do not introduce a
multi-assignment day/session form until real editing pressure justifies its
additional atomicity and validation behavior.

## Exception driven

Normal sessions should be visually quiet.

Missing coverage, conflicts, and decisions needing attention should be obvious.

## Explainability

Selecting a session/physician should answer:

* why assigned?
* why unavailable?
* what source says that?
* what rule applies?
* is this fixed, derived, manually chosen, or solver-selected?

## Progressive disclosure

Common scheduling policy should look like scheduling policy, not a generic programming/constraint language.

Example:

> Rowan Finch normally covers Tuesday AM and PM.

The user should not need to understand CP-SAT variables or objective weights.

## Easy manual editing

Replacing a preceptor should show meaningful candidates, not merely an alphabetical dropdown.

Potential candidates should expose relevant information such as:

* eligibility
* conflicts
* availability
* workload effect
* preference issues

---

# 24. Operational attention

The current operational front door is **Schedule**. The Master Schedule shows
the selected week's current facts and incomplete work in schedule context;
**Schedule > Work Remaining** answers the organization-wide question, “What
needs attention?” Work projection and lead-time behavior are specified in
section 7.2. A separate dashboard is not currently accepted.

---

# 25. Metrics and reporting

Upstream schedules may need metrics/reporting even when they require no solver.

Current schedule reports use explicit academic-year, calendar-year, or custom
date ranges and identify their range, generation time, and current-corrected
record basis.
Additional report families require a concrete scheduling question.

Retained history must be sufficient to distinguish the current corrected account
of a historical period from the values recorded earlier. Schedule Reports already
expose History coverage; which additional
historical views are needed remains to be decided, and the underlying history
must not be discarded.

Potential examples:

* vacation usage/counts
* faculty assignment totals
* assignment totals by week
* preceptor workload distribution
* coverage counts
* preference violations
* unresolved sessions

Do not build a generic analytics platform initially.

Add metrics based on real scheduling questions.

## 25.1 Accepted monthly stipend reports

The first reporting workspace produces two separate calendar-month stipend
reports from Muriel's **current corrected scheduling facts**, including future
assignments already scheduled within the selected month.

The **Community Preceptor Stipend** report includes only physicians with at
least one qualifying current Community Preceptor assignment in the selected
month. Every valid Community Preceptor assignment is Primary and counts as one
four-hour shift. The **Late Doc Stipend** report includes only physicians with
at least one retained Late Doc assignment in the month; each Monday 5–6 PM slot
counts as one one-hour shift. A missing Monday Late Doc is visible as a warning
and reporting work item but does not block report output. Holiday closures are
modeled and do not create Late Doc coverage requirements.

Each report lists qualifying shifts by physician, date, and period/type, with
hours, the applicable hourly rate, and US-dollar amount. It gives
per-physician shift/hour/amount subtotals and a report total. Physicians with
zero qualifying hours are omitted.

Community Preceptor and Late Doc rates are two separate organization-wide
effective-dated hourly values, entered to the nearest cent. A rate timeline may
be initially unconfigured; once configured it is either one fully unbounded
value or a gapless, non-overlapping sequence of adjacent values with unbounded
outer ends. The shift date selects its rate, so a mid-month change is
calculated per detail row. Rate corrections retain prior values, actor, time,
and optional note, and current reports immediately use the corrected timeline.

Every authenticated scheduler may view the reports and generate their PDFs.
Only Scheduling Administrators may manage stipend rates. A report with a
qualifying shift lacking an applicable rate remains visible with a clear work
item and without a misleading amount, but PDF generation is blocked until all
qualifying shifts have rates. A missing Late Doc warning does not block PDF
generation.

Reports are generated on demand rather than retained as issued report
revisions. Each PDF's visible heading and title metadata identify the shell's
fictional facility, `Family Practice Clinic`, and the stipend report type.
Each PDF identifies the calendar month, generation
time, generating account, current-record basis, applied rate periods, detail,
subtotals, warnings, and total. The downloaded PDF is the submitted artifact;
assignment and rate histories remain the system's correction audit.

The top-level **Reports** workspace initially has local views for `Community
Preceptor Stipend`, `Late Doc Stipend`, and administrator-only `Stipend Rates`.
These fixed-month semantics do not establish a generic reporting engine or the
history-versus-scheduled controls of later report families.

## 25.2 Accepted date-range schedule reports

The Reports workspace also provides one **Schedule Reports** view for comparing
physician workload. It uses a native Schedule selector rather than adding a
local navigation item for every schedule. The initial choices are Precepting,
Community Precepting, Interviews, Academic Events, Normal Rounding, Casual
Rounding, and Vacation.

The report period is an Academic year, Calendar year, or inclusive Custom date
range. Academic years run July 1 through June 30 and are always labeled with
both years; calendar years run January 1 through December 31. Coverage is
History, Scheduled, or History + scheduled. In the organization timezone,
History ends yesterday and Scheduled begins today, and the selected period is
intersected with that coverage before eligibility, facts, or findings are
calculated. The bare Schedule Reports workspace does not generate a report.
Its Schedule selector begins with a required `Select a report` placeholder
while the current academic year and History + scheduled remain the initial
definition defaults. Selecting a real report is the generation boundary and
shows the shared processing overlay until navigation completes. Direct URLs
with a valid Schedule retain their requested report definition. Minimal
JavaScript may reveal relevant controls and disable
impossible combinations, but stable GET parameters and server validation are
authoritative and the form remains usable without JavaScript. When a selected
period lies entirely before or after today, a stale explicit coverage choice is
normalized to the sole meaningful coverage; controls fix that choice, disable
the impossible alternative with an explanation, and omit combined coverage.
When a changed period begins to span both sides of today, coverage defaults to
History + scheduled; the user may then explicitly narrow it to either side.
Annual selection and navigation span every calendar-valid year: academic-year
starts 1 through 9998 and calendar years 1 through 9999.

Schedule-report controls keep the visible definition and results synchronized.
With JavaScript, every valid Schedule, Range, annual picker, Custom date, and
Coverage change immediately submits the complete GET definition and replaces
the page; Previous and Next use the same immediate navigation policy. Dependent
normalization, including Coverage changes caused by a new range, is settled
before one request is made. Incomplete or invalid Custom ranges do not submit.
The shared period-control presentation remains reusable by immediate and
non-immediate consumers without duplicating its markup. Without JavaScript, a
conventional Update report submit remains available.

Every report uses **current corrected facts**. Ordinary change history retains
the prior account, actor, and time. A row
is shown for every physician eligible during any part of the effective report
window, including zero-count rows, plus anyone with counted activity even when
their current role facts are inconsistent. Recorded activity is never hidden
because of a role mismatch. Tables use one non-wrapping row per physician on
ordinary desktop widths and scroll horizontally on narrow screens.

The accepted per-physician measures are:

* **Precepting:** AM, PM, Primary, Secondary, Precepting total, and Late Doc.
  AM plus PM and Primary plus Secondary each equal Precepting total; Late Doc is
  separate.
* **Community Precepting:** AM, PM, and Total.
* **Interviews:** Shifts.
* **Academic Events:** Events, Event days, and Affected shifts. An assigned
   occurrence counts once in any effective window it overlaps, including a calendar-only
   occurrence. Event days count included calendar dates; AM or PM contributes one
   affected shift per date and Full day contributes two only for Hard or Soft impact.
   Separate occurrences are additive.
* **Normal Rounding:** Weeks, classified by the Monday week-start anchor so a
  week is never split or double-counted.
* **Casual Rounding:** distinct recurrence Days.
* **Vacation:** overlapping Ranges, distinct Weekdays, distinct Weekend days,
  and distinct Total days. Partial-day and overlapping Vacation records do not
  multiply a calendar day.

Community Preceptor recurrence remains an expected base commitment rather than
an assignment everywhere else. For reporting only, the Precepting projection
first counts actual assignments and may then use unmatched Community recurrence
commitments to fill open Primary positions. Exact physician/date/period facts
are deduplicated. Vacation and Academic Event availability do not block this
fallback because those upstream schedules are not managed for Community
Preceptors. If expected Community physicians exceed the open Primary positions,
the report does not choose arbitrarily: it counts no ambiguous fallback and
reports the condition as a hard finding. Community Precepting is a filtered
presentation of this same projection, not a second reporting truth.

Scheduled and combined reports always carry a prominent notice that they are
planning reports and may change. They summarize relevant current warnings,
conflicts, role mismatches, and ambiguous Community expectations by type,
severity, and count without reproducing a detailed finding register. Findings
do not block output. History-only reports omit both the planning notice and the
findings section because historical reporting records what actually happened.

Every authenticated scheduler may view these reports. PDFs are generated on
demand only after the screen projections and presentation are settled, and use
the exact same projection, parameters, ordering, zero rows, and finding summary.
Their visible heading and title metadata identify the shell's fictional facility,
`Family Practice Clinic`, and the schedule report type. Download filenames use a
neutral `muriel-` prefix while retaining the schedule, selected period/range, and
coverage. PDFs identify the effective range, coverage, generation time and account,
and the current-corrected-record basis. Scheduled
or combined PDFs retain the planning notice and findings so a saved report is
not mistaken for final historical output. PDFs are not retained as revisions.

This is a narrow shared parameter and presentation shell around explicit
schedule-specific projections. It is not a generic report builder, analytics
store, category pivot, spreadsheet export, publication-based reporting mode,
master calendar, or solver, and it adds no new source-of-truth records.

## 25.3 Accepted Faculty Admin Time report

The Reports workspace provides a calendar-month **Admin Time** view only to a
logged-in account linked to a Physician with a Program Director role effective
today. Django staff, superuser, Scheduling Administrator, and unrelated Faculty
status do not independently grant access. The report uses the standard History,
Scheduled, and History + scheduled coverage choices and current corrected facts.

This is a coarse four-hour-per-AM/PM planning measure for effective Faculty,
not payroll, timekeeping, or an FTE source of truth. For each included weekday
period, the shared schedule-input precedence resolves one explicit built-in or
custom schedule option. Every option except literal `Off` creates one
four-hour gross-capacity key retaining that option's identity. Independent
current scheduling facts then remove blocked physician/date/period keys through
the shared availability projection. Weekly templates, monthly rules, annual
windows, and custom-option impacts describe the gross option and do not block
themselves: built-in `Admin`, for example, remains scheduled work despite its
ordinary Precepting impact. Vacation, Academic Event, holiday, Rounding,
Interview, and any other independently blocking source therefore share one
classification rather than a report-specific allowlist. The remaining worked
set is gross capacity minus the union of blocked keys, so overlapping blockers
never subtract the same four hours twice. Every surviving option contributes
four hours both to its own visible option bucket and to **Total worked**.

`Open` and built-in `Admin` are treated identically as potential Admin
Time. A current Precepting assignment in either type of period remains part of
Total worked but is subtracted once from computed Admin Time. A Precepting
assignment from another option does not manufacture or subtract Admin Time.
Thus computed Admin Time is net surviving Open plus Admin after Precepting
consumption. Rounding and Interview duties are independent blockers when their
shared current source facts make a physician-period unavailable.

The ACGME goal is the smaller of ten percent of Total worked and the existing
four-hour-per-week cap prorated to included weekday coverage. The UMN goal is
twenty percent of Total worked without a cap. Their Met/Below outcomes are
independent.

The report table visibly exposes every represented built-in and custom schedule
option, excluded-time detail, explicit Off time, Total worked,
Precepting drawn from Open/Admin, computed Admin Time, and both target
calculations. Blocked-time detail shows both the deduplicated removed-period
total and its source reasons without implying overlapping reason counts are
additive. It remains one row per Faculty where practical and may scroll
horizontally rather than wrapping numeric cells or hiding details. Only the
ACGME and UMN comparison cells carry positive/danger tone.

---

# 26. Technology direction

Prioritize maintainability and low operational complexity.

Current stack:

* Python
* Django
* server-rendered HTML
* minimal JavaScript
* SQLite for local development
* PostgreSQL for the commissioned production runtime
* OR-Tools CP-SAT for Schedule Bot proposals

Avoid initially:

* Rust
* SPA frameworks unless the UI genuinely requires one
* microservices
* Kubernetes
* event sourcing
* custom constraint solvers
* elaborate asynchronous infrastructure
* AI runtime dependencies

Prefer:

**one application + one database + conventional backup/restore**

### Commissioned production deployment direction

The commissioned Muriel installation runs on the existing single Ubuntu host
and will be publicly reachable at its configured Tailscale Funnel hostname.
Funnel terminates public HTTPS and forwards only to a loopback Nginx edge;
Nginx serves collected static assets, applies bounded public-authentication
abuse controls, and proxies the single Gunicorn/Django application to local
PostgreSQL. Gunicorn, Nginx, PostgreSQL, backups, and timers use conventional
systemd/Ubuntu operation. Django Admin remains off the public listener and is a
break-glass surface through a tailnet-only Tailscale Serve route to a loopback
Nginx listener; ordinary account administration remains in Muriel.

This is deliberately a small conventional stack rather than a container or
orchestration platform. Repository-owned bootstrap, deployment, backup,
restore, verification, and operator documentation must make the installation
reproducible. Production configuration fails closed around a host-local secret,
`DEBUG = False`, exact host/origin trust, secure proxy/cookie handling, and
PostgreSQL. A reviewed exact commit is deployed; development SQLite and demo
reset data never become production data implicitly. Backups require an
integrity-checked off-host copy and a successful scratch restore drill before
the service is considered operational.

Funnel makes the sign-in surface reachable from the public internet and does
not authenticate visitors as tailnet members. The public boundary therefore
requires application authentication on all domain surfaces, source-preserving
edge request limits for authentication/recovery/invitation flows, safe errors
and logs, and an explicit incident shutdown path. This first deployment does
not claim high availability, a custom public domain, regulatory compliance, or
general support for arbitrary hosting topologies.

## 26.1 Accepted organization boundary

The initial product serves one organization. Do not add tenant models, tenant-scoped data access, tenant routing, or a generalized organization abstraction without observed multi-organization pressure and a new explicit product decision.

Organization-wide policy such as the authoritative `America/Chicago` timezone may remain direct application configuration while the product has this single-organization boundary.

### Open-source residency direction

Muriel is intended to broaden into an open-source scheduling application that
other residency programs can understand, operate, and adapt. The initial
distribution model remains **one organization per installation**. Open-source
distribution does not itself justify tenant models, a hosted multi-tenant
service, a plug-in framework, or a generic scheduling/rule engine.

The intended first public shape is an **opinionated reference implementation**:
a well-fleshed-out, coherent demonstration of what Muriel can do for a real
residency, with code and tests clean enough for others to understand and modify.
It is not a promise that every workflow is selectable through configuration.
When other residencies demonstrate concrete policy differences, evaluate the
smallest modification, configuration seam, or extension point supported by
those examples rather than designing pluggability in advance.

MIT is the accepted license for Muriel's first-party source, as recorded in
[root LICENSE](../LICENSE). Bundled fonts remain separately licensed under the
[SIL Open Font License 1.1](../schedules/fonts/LICENSE-OFL.txt), with existing
[provenance records](../schedules/fonts/README.md); third-party dependencies
retain their own terms. This choice does not settle remaining attribution
review, clean-export readiness, support or compatibility commitments, or
public-release approval.

The public repository will start with fresh Git history from a cleaned export.
Keep the old repository private; do not export its old Cassidy planning or
Attempt evidence. Initialize new planning in the public repository rather than
carrying private planning history forward. This direction does not authorize
publishing the current tree before the remaining readiness work is complete.

The accepted deployment-source transition retains conventional single-host
OpenSSH/systemd tools while moving installation identity into explicit operator
configuration, without a hidden original-installation fallback. Exact-target
and public/Admin route safeguards remain required; this is not a deployment
profile framework or a change to the commissioned server topology.

Production `ops/status TARGET` and `ops/logs TARGET` require `MURIEL_SSH_TARGET`
to name one supported ASCII SSH alias and require the sole argument to match
exactly; missing, invalid, or mismatched selection refuses before connecting.
Normal private OpenSSH configuration owns the username, network hostname,
credentials, and connection options. Both commands remain read-only and
noninteractive, without sudo or a TTY. Status is limited to the existing
`muriel.service` status operation; logs reads only the last 100 lines of the
`muriel` journal with the pager disabled and no live following.
The [deployment runbook](deployment.md#production-service-status) defines alias
syntax and operator setup. Existing operators must configure their SSH identity
and explicit alias before using these commands; no automatic host rename or
remote migration occurs.

Production `ops/restart TARGET` uses the same required `MURIEL_SSH_TARGET` alias
and exact sole-argument guard. Unlike status/logs it retains an interactive SSH
TTY for the operator's remote sudo prompt. It restarts only `muriel`, checking
that service is active only after restart succeeds; restart, active-check, and
SSH failures propagate without automatic recovery.

Staging `ops/status-staging TARGET` uses the same required `MURIEL_SSH_TARGET`
alias and exact sole-argument guard, refusing missing, invalid, or mismatched
selection before SSH. It remains read-only and noninteractive without sudo or a
TTY, running only `systemctl status --no-pager muriel-staging.service` and
preserving output and remote/SSH failures. One installation may host both
services: the wrapper's fixed operation, not alias spelling, selects staging
rather than production. The [staging status runbook](deployment.md#staging-service-status)
defines explicit local setup and the operator transition without an
original-installation fallback or remote migration.

Staging `ops/logs-staging TARGET` uses the same required alias and exact
sole-argument guard, with noninteractive SSH and no sudo or TTY. It reads only
`journalctl --no-pager -u muriel-staging -n 100`, preserving output and
journal/SSH failures, without live following or service mutation. The fixed
wrapper operation selects the staging journal independently of alias spelling.
The [staging log runbook](deployment.md#staging-service-logs) defines the local
operator transition with no fallback or live changes.

Operator `ops/create-staging-superuser TARGET` uses the same required alias and
exact sole-argument guard, retaining an interactive SSH TTY and running only
`sudo /srv/muriel-staging/create-superuser`. Output and remote/SSH failures
propagate. Alias spelling selects the physical host; the fixed command and
validated staging release, environment, database, role, and execution identity
select staging. The installed helper remains standalone: sudo with no arguments
(or `--root DIRECTORY` for isolated fixtures) needs neither SSH configuration nor
the repository target guard. The [account runbook](deployment.md#staging-account-administration)
defines explicit local setup and the existing-operator transition.

Direct `ops/deploy TARGET` uses the same required `MURIEL_SSH_TARGET` alias and
exact sole-argument guard, rejecting invalid selection before archiving,
allocating transfer artifacts, or connecting. Normal private OpenSSH
configuration owns connection identity, without a personal login or private
fallback in this transport path. The validated alias is used for temporary
allocation, SCP, privileged activation, and cleanup. Deployment still requires
a clean checkout, archives exact `HEAD` with the existing exclusions, and sends
a private archive to the installed production release helper. Activation retains
one interactive SSH TTY/sudo invocation with remote archive stdin redirection;
owned local/validated remote archives are cleaned on success and failure,
without masking primary failures or acting on unsafe remote paths. The
[direct-deployment runbook](deployment.md#direct-release-deployment) describes
required local configuration for existing operators. No live migration occurs;
fixed service/path topology and commissioned server domains are unchanged.
Routine `ops/update-production TARGET` also requires the configured alias and
exact sole-argument match before archive creation, transfer-state allocation, or
connection. The same validated alias selects every allocation, transfer,
verification, privileged invocation, and failure cleanup destination. It retains
the clean exact-HEAD archive, exclusions and private mode, validated remote
archive/helper paths, and helper extracted from that same revision's archive.
One interactive privileged session runs the verified helper with the exact
commit/archive, retaining bootstrap reconciliation, deployment/readiness,
backup/service, public-edge, and exact Tailscale route/type checks. Owned cleanup
and honest residue reporting preserve failures without automatic retries.
Success reports the exact deployed commit and configured browser URLs, not
domains inferred from the SSH alias. Existing operators must configure private OpenSSH
identity and the explicit local alias as described in the
[routine-update runbook](deployment.md#routine-production-update). The alias can
address the current installation without changing server identity or making
arbitrary domains deployable. This transition makes no live changes.
Staging `ops/update-staging TARGET [--candidate] [--reset-and-load]` also requires
that explicit alias and exactly one matching target before archive/transfer
allocation or connection. Flags may precede or follow the target; duplicate or
unknown flags/targets and Candidate plus reset are refused. The validated alias
survives Candidate-ref inspection and selects every allocation, transfer,
helper verification, interactive sudo session and cleanup. Clean exact-HEAD
updates, exact unaccepted Candidate identity, explicit synthetic-reset authority,
staging-only isolation, owned cleanup and failure reporting remain unchanged.
The [staging runbook](deployment.md#synthetic-staging) describes required local
setup without a personal login or private fallback. Every update mode also
requires explicit nonsecret `PRECEPTOR_STAGING_ALLOWED_HOST` and
`PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN`: one exact lowercase ASCII DNS hostname
without a trailing dot and exactly `https://<host>:10000`. Invalid, missing or
inconsistent input refuses before Git, archive/temporary allocation or SSH.
Capture the pair once and pass only those two nonsecret values as literal extra
arguments to the same verified helper and sole sudo session. The helper validates
argument count and identity before host mutation and supplies the captured pair
explicitly to every bootstrap check/apply/recheck, without assuming inherited
sudo environment or inferring identity from aliases or observed routes.

Both staging bootstrap entry points require that same process pair for check
and apply. First install creates the service identity and protected directory
before generating its private staging-only secret and selected pair with fixed
`muriel_staging` database/role. The protected environment remains the application's
authority. Preserve preflight refusal of existing divergent values, unsafe
metadata or symlink paths before prerequisite mutation; existing files must match
the intended pair and are never rewritten, repaired, adopted as a default or
sourced as root. No production configuration is required. Installed standalone
release/account helpers retain their protected-environment lifetime, without a
local operator-input or repository dependency.

The same bootstrap-confirmed pair selects staging route comparisons, bounded
`<origin>/health/` request and actionable diagnostic, and the labeled staging
browser URL `<origin>/` after successful remote work, retaining the exact commit.
Existing operators supply the actual existing identity: this is not automatic
host rename/configuration migration. The physical host must actually have the
selected identity; mismatched observed routes fail. Production Nginx/certificate
bootstrap uses the separate explicit production pairs described below, without
an arbitrary-topology claim.

Staging route convergence owns only TCP `10000` and the selected staging host's
Web/AllowFunnel `:10000` entries. A complete post-state requires exactly
`{"HTTPS": true}` for that TCP listener and the sole `/` Web proxy to
`http://127.0.0.1:8082`, without extra fields. Staging Funnel permission must be
absent or boolean false before and after mutation. All remaining observed state
is preserved with exact nested keys, values and types, not Python bool/int
coercion or a compiled global production baseline. Invalid preflight JSON or
structure prevents mutation. After the single background Serve mutation, a
separate persistent-state acquisition and comparison must succeed before health
verification or deployment success; failures retain cleanup without retries,
Funnel mutation, unrelated route repair or rollback. Production's separate
whole-state verifier remains stricter about its complete commissioned state.

The opt-in staging performance probe requires the explicit nonsecret local pair
`PRECEPTOR_STAGING_ALLOWED_HOST` and `PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN`, with
one exact hostname and its exact HTTPS origin on port 10000. It validates and
captures that pair before password prompting or networking, with no compiled
installation fallback, SSH alias discovery, or arbitrary URL override. All
requests, POST Origin headers, and redirect decisions retain that captured
origin. Marker-before-credentials, cookie/CSRF safeguards, bounded measurements,
and marked unapplied-proposal cleanup remain required. The
[performance-probe runbook](staging.md#performance-probes) describes local setup
without importing server settings or copying protected server environment files.
The manual-only probe and archived-release wrapper do not migrate server runtime
identity; staging bootstrap/update uses the same explicit pair. Production Nginx
and certificate bootstrap use separate explicit production selection.

The local `ops/check-public-edge` dual-route verifier requires explicit nonsecret
production primary and secondary host/origin pairs:
`PRECEPTOR_PRODUCTION_ALLOWED_HOST` / `PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN`
and `PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST` /
`PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN`. It captures both distinct
exact hostnames and their exact HTTPS origins before networking, without a
compiled-domain fallback or importing Django settings. Primary and secondary
Host headers select Funnel and Cloudflare respectively; socket destinations
remain fixed loopback ports 8080 and 8083 with the existing PROXY-v2/raw HTTP
adapter, shared synthetic source, bounded requests and edge safety checks.
Routine production update validates the protected fixed `/etc/muriel/environment`
file (regular, non-symlink, readable, `root:muriel` mode `0640`) and sources it
only in the service-user shell for that consumer. Missing or invalid configuration
fails verification rather than skipping either route; Django's separately
optional secondary pair is unchanged. The [public-edge runbook](public-edge.md)
describes standalone configuration and service-user invocation. Nginx and
bootstrap certificates are not reconfigured by this local check. Bootstrap
separately renders both Nginx identities and selects the primary certificate
hostname from explicit input.

Routine production route verification also consumes the protected primary
host/origin pair, reusing local-edge exact identity validation without invoking
its sockets or importing Django settings. It sources the fixed environment only
as `muriel`, clearing inherited identity settings. After local-edge success,
the privileged helper captures read-only `tailscale serve status --json` in its
owned private staging directory and opens that capture for the unprivileged
comparator over stdin, without relaxing directory permissions. Complete exact
key/value/type comparison requires the configured primary identity for public
TLS on 443, private Admin Web on 8443 and the sole Funnel permission on 443.
Optional private staging on 10000 uses that same identity; partial staging,
foreign hosts, additional state and incorrectly typed equivalents fail. Route
acquisition, malformed JSON and comparison failures prevent update success and
retain cleanup. There is no compiled-host, observed-state or SSH-alias fallback.
After complete route comparison succeeds, that same service-user invocation
reports the captured validated secondary hostname as the preferred public
Cloudflare URL (`https://<secondary-host>/`), the primary hostname as the
temporary public Funnel fallback (`https://<primary-host>/`), and private Admin
(`https://<primary-host>:8443/admin/`). Existing SSH stdout carries these labeled
browser destinations; there is no display-only settings reload or local
configuration input. The local deployed-commit success message still requires
successful remote completion. Verification failures suppress the destinations
and deployment success; ordinary verified-URL output need not wait for EXIT
cleanup to finish.
This is read-only verification, not route creation or a topology change.
Production Nginx rendering and bootstrap certificates use explicit production
selection, while staging environment generation and browser output use the
separate staging pair described above. This does not generalize the topology
or authorize a live identity migration.

Production bootstrap preflights an existing fixed `/etc/muriel/environment`
before any apply mutation, including package installation: it must be readable,
regular, non-symlink and `root:muriel` mode `0640`, with the existing managed-path
parent-chain guard. Unsafe occupants are refused without changing bytes, metadata
or symlink targets. Bootstrap now loads safe existing shell configuration only
as `muriel`, clearing all four inherited identity values and suppressing configuration
output, to require exact agreement with both selected nonsecret pairs.
It never sources the file as root, prints secrets, imports Django for comparison,
or rewrites the file. Absence at a safe path is permitted in both apply and read-only check/dry-run,
including a converged check without an environment; the operator creates it after
bootstrap establishes the service identity and directory. The operator must
create matching protected application configuration before deployment. Safe
matching existing files remain untouched during reconciliation. Release/readiness and later update
probe/route checks still require and independently validate their environment;
bootstrap is not deployability certification or an all-host transaction. Staging
keeps its separate configuration/preflight. This source change authorizes no live
configuration access or host changes.

Production bootstrap check/apply and local routine update require explicit
`PRECEPTOR_PRODUCTION_ALLOWED_HOST` / `PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN`
and `PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST` /
`PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN`. Each is one lowercase ASCII
DNS hostname without a trailing dot and exactly `https://<host>`, without padding,
wildcards, ports or shell/HTTP syntax; primary and secondary must be distinct.
There is no original-domain, alias or route inference. Update validates before
Git, archive allocation or transport, captures all four values once and carries
only these four additional literal arguments through the exact verified remote
helper and sole sudo session. The standalone helper validates with the guard
from its verified archive and explicitly supplies both pairs to every bootstrap
check/apply/recheck, without a release/venv dependency or sudo environment-
preservation assumption. First manual bootstrap needs neither protected
configuration nor an existing service identity/directory; later operator-created
application configuration must match. Staging keeps its separate inputs;
installed standalone helpers keep their existing interfaces.

Bootstrap renders named markers in the site and Admin proxy source templates
through its existing fixed managed-file owner: the primary hostname selects
both Funnel and private Admin server names and the fixed `<primary-host>:8443`
Admin proxy Host. The independent secondary selects the Cloudflare server name.
Check compares intended rendered bytes read-only; apply preserves atomic
regular-file replacement, owner/mode, path safety and stock-link rules. Rendering
failure or unresolved identity fails closed; private invocation-owned scratch
is cleaned without disarming package, file or certificate cleanup. Nginx runtime
variables, public loopback ports 8080/8083, private loopback TLS 8081, default
rejection, PROXY-v2 trust, replaced headers/markers, public limits, static handling
and Admin blocking remain unchanged. Nginx syntax validation remains; bootstrap
does not reload or activate. Protected server pairs still own local edge/route
verification and browser output, never display-only local values. Local selections
must agree, not override them. Cloudflare control-plane provisioning remains an
explicit operator action. This completes production bootstrap identity selection,
not live migration, DNS changes, export approval or public-release certification.

Production bootstrap verifies the private Admin certificate/key pair with
standard OpenSSL: parseability, current validity, intended hostname coverage,
and a usable noninteractive matching private key, independent of key algorithm.
This is offline local pair/identity validation, not external CA trust,
revocation or reachability verification. OpenSSL is an explicit bootstrap OS
package. The same selected primary hostname owns both first issuance and
existing-pair validation; changing selection never silently rotates a pair.
The human explicitly chose refusal with operator renewal guidance for an
existing wrong-host or otherwise unusable pair, never automatic replacement on
`--apply`. Partial pairs also require operator repair. `--check` is read-only;
refusal preserves existing bytes and metadata. First installation may obtain a
pair only when both files are absent, using invocation-owned private scratch
and validating the result before installation. Fixed root-owned 0600 files in
the 0700 directory, path safety, valid-pair reuse, package-start suppression and
nonactivation remain required. Bootstrap does not reload Nginx, activate Muriel,
or renew certificates automatically. See the [host bootstrap](host-bootstrap.md)
and [explicit Admin renewal procedure](public-edge.md#admin-certificate-renewal).
This source direction authorizes no live certificate inspection, issuance,
replacement, deployment or repository cutover.

The Precepting workbook importer no longer ships clinic-specific Physician-name
aliases. Keep its alias lookup branch with an empty mapping and defer
generalization until a demonstrated need; do not add configuration or fuzzy
matching for this removal. Ordinary matching uses normalized full names
(including Last, First input), then unique-surname fallback only for an
otherwise unmatched one-word input. Missing or ambiguous identities block
import. This narrow change does not alter the coordinate-specific workbook
correction, parsing, preview/Apply safeguards, or existing database records.

Open-source readiness is a long-lived direction delivered incrementally, not a
single near-term release project. Architecture cleanliness, explicit ownership,
tests, and conventional operation may advance alongside ordinary product work.
Branding extraction, public packaging, compatibility commitments, and other
release-facing work can remain late slices until a real public-release boundary
is selected. The umbrella Goal stays open while independently accepted Tasks are
added and completed over time.

This direction raises the design bar for explicit ownership and conventional
operation. Scheduling policy must remain outside presentation handlers;
retained facts, read projections, policy findings, mutations/history, and HTTP
presentation should have understandable boundaries. Shared presentation code
should remain a small base-plus-explicit-variant system. Residency or clinic
identity, local timezone, and other truly installation-wide values must be
isolatable from reusable scheduling behavior before a public release, while
policy differences should become configuration only after real cross-residency
variation is observed.

Before an open-source release, the project will need an explicit supported
runtime/database and upgrade path, production configuration and secret
handling, backup/restore guidance, safe demonstration data, security and
authorization documentation, remaining third-party attribution review,
installation and contribution documentation, and a release compatibility
decision. This is future release
direction, not a claim that the current development application is ready for
public deployment or that it satisfies any regulatory compliance regime.

## 26.2 Accepted local demonstration workflow

The main worktree provides `scripts/start-dev` for the current Django
application and `scripts/reset-demo` for rebuilding local demonstration data.
Starting the application never resets or seeds data. The reset is
development-only, requires explicit confirmation, and refuses every database
other than the expected main-worktree SQLite database.

The deterministic demonstration horizon is August 1, 2026 through July 31,
2027, with Precepting draft assignments populated only through September 30,
2026. It contains 10 Faculty, 3 Community Preceptors, and a 15-shift Interview
season from October 2026 through January 2027. The local demo Community
identities are explicitly fictional replacements: Fictional Cedar has zero
retained Community Preceptor rules, Fictional Harbor has one, and Fictional
Summit has two. Their exact labels are not a product contract, and these are
separate fixtures from the synthetic history scenario's generated Community
doctors. One Faculty role is Program Director and two are Associate Program
Director; Community and Faculty roles remain exclusive. Community rules
demonstrate weekday, ordinal, AM/PM, and effective-range presentation without
creating assignment or completeness semantics.

As first-class source workspaces are added, their demonstration examples may
remain isolated in the destructive demo seeder rather than shaping production
models. The initial Academic Event examples span August and September 2026 and
show multiple participants, range shapes, and every whole-day/AM/PM coverage
type. Normal-rounder examples cover every Monday-starting week from August 3
through September 28, matching the seeded assignment horizon, while a few
casual-rounder rules exercise weekday/ordinal recurrence including a real
fifth occurrence. Fictional Cedar covers all Mondays, Fictional Harbor covers
first/third/fifth Wednesdays, and Fictional Summit covers second/fourth Fridays.
All three Casual Rounder rules and roles start August 1, 2026; Cedar and Summit
end September 30, 2026, while Harbor's rule and role are unbounded into the future.
August has full primary coverage and September retains zero, one, and two-primary
draft gap shapes. Each of those months includes one
intentional unavailable assignment and one backup-only assignment; all other
filled positions are ordinarily available and have no sibling Interview
conflict. Each Interview month likewise includes one intentional unavailable
assignment and one backup-only assignment, with all other Interview
assignments ordinarily available. These deterministic examples use the normal
authenticated mutation and history boundaries and are synthetic UI/query
cases, not policy.

## 26.3 Python development quality

Use Ruff as the single Python formatter, import sorter, and lint engine. Keep
it in pinned development requirements rather than the application's runtime
dependencies. The initial lint policy should emphasize ordinary correctness
and consistent source presentation without requiring annotations, docstrings,
or framework-specific style. Project checks should enforce the accepted Ruff
baseline before Django checks and tests, while one simple repository command
applies safe fixes and formatting.

Do not add pre-commit, a second formatter/linter, or a broader quality-service
stack without observed workflow pressure. Python tooling must work in ordinary
development and Cassidy worktrees through the main worktree virtual
environment, and must not require network access during the deterministic
project check.

---

# 27. Architectural boundaries

Scheduling semantics should not be embedded directly into Django views/templates.

The implementation is maintained through AI-assisted development, so its
structure should be **agent-legible by construction and human-auditable**.
Functional correctness remains first. An agent should be able to locate the
owner of a behavior, load a bounded context, make one scoped change, and run
direct evidence without reconstructing a giant mixed module. A human must still
be able to trace that behavior and retains all product and disposition
authority; optimizing code for agent comprehension does not grant an agent
authority over scheduling facts or accepted design.

Prefer cohesive owner-named modules, direct imports that reveal ownership,
one-way dependencies, explicit immutable read values, simple control flow,
searchable domain terminology, focused tests near the owning boundary, and a
short maintained architecture map. Avoid giant functional buckets, catch-all
utility modules, dynamic registries, reflection-heavy dispatch, deep
inheritance, hidden conventions, generic metadata frameworks, and clever DRY
abstractions that require broad context to understand. Do not fragment code
into tiny modules merely to reduce line counts; each module should own a real
domain or application responsibility.

The current scheduling service layer is an ordinary Python package inside the
existing Django application. Its modules own physician/role facts,
Vacation and Academic Event sources, Rounding and Community sources, effective
availability, bounded slot projections, Interview policy, Preceptor policy,
and solver proposal review/apply. Callers import the owning module directly.
The package root is
not a giant re-export facade, and temporary movement during the refactor does
not establish a compatibility API. This is code organization only: models,
forms, views, routes, database facts, validation, history, and presentation do
not change as a consequence.

Keep a conceptual separation between:

## Domain

* physicians
* schedule facts
* effective availability
* rules
* coverage
* solver proposals
* scenarios
* provenance

## Application/web layer

* persistence
* forms
* authentication/authorization
* HTML
* user workflows
* administration

## Solver

Schedule Bot consumes domain state and produces candidate domain results.

The solver must not become the authoritative data model.

---

# 28. Original vertical slice (implemented baseline)

The first useful implementation was deliberately small. This retained list is
the implemented baseline, not a limit on the current product.

It supported:

1. Physicians

   * faculty
   * community preceptor
   * faculty-equivalent status
   * active/inactive dates if needed

2. Manual maintenance of:

   * Faculty Schedule
   * Vacation Schedule
   * Academic Event Schedule
   * Rounding Schedule
   * Community Preceptor Schedule

3. Effective availability

   * normal availability
   * unavailable
   * emergency/backup-only where relevant
   * reason/provenance

4. Preceptor Schedule period

   * create a month
   * create Monday-Friday AM/PM sessions
   * edit the same sessions through month and focused Monday-Friday week views

5. Initial validated rules:

   * rounding conflicts
   * vacation/event unavailability
   * faculty-equivalent coverage
   * any confirmed explicit recurring assignments

6. Schedule grid

7. Gap/conflict detection

8. Human-controlled assignment/replacement

9. Draft revision

10. Publish an immutable Monday-Friday weekly revision

11. Simple weekly PDF with provenance/version information

12. Retained prior values and provenance when scheduling facts or assignments are corrected, including explicit human correction of historical data

---

# 29. Original vertical-slice deferrals and current direction

The first vertical slice deferred global optimization, assignment proposals,
workload fairness and repair solving, what-if scenarios, and date-range reports.
Bounded, human-controlled Schedule Bot proposals now provide assignment
proposals and solving, and Reports provide date-range reports. What-if scenarios
remain deferred. These current surfaces do not establish automatic assignment,
unbounded optimization, or a generic analytics system. Spreadsheet import,
external APIs, change streams, a generic rule editor, and AI runtime integration
remain deferred. The model must not preclude them when concrete demand appears.

---

# 30. Remaining domain discovery

The current product baselines are authoritative: effective-dated Faculty
templates; weekly normal and recurrence-based casual Rounding; recurrence-based
Community commitments and per-role faculty-equivalent qualification; two
Primary plus optional Faculty Secondary Precepting positions; bounded
Fill/Repair Schedule Bot proposals with a six-month default balance window;
current-corrected schedule reports; optional Physician-account links; and
authenticated scheduler and
Scheduling Administrator authority. The following narrower questions remain;
do not invent answers to them.

* If external Faculty Schedule imports are introduced, which source semantics and mappings should feed the current effective-dated weekly-template model?
* What exact governing citation and version supports the current two-faculty-equivalent coverage requirement?
* Which additional availability or Academic Event interaction rules are confirmed beyond the current source composition and blocking behavior?
* Does real emergency use require a separately scheduled emergency-backup representation beyond the current `backup only` availability and visible alternatives?
* Do any additional qualifications need to supplement the current Faculty and per-role Community faculty-equivalent coverage model?
* Which observed recurring assignment patterns, if any, should become named system policy rather than remain the historical, non-authoritative examples in section 13?
* Do observed schedules justify tuning the current proportional, capacity-based balance policy, its six-month default window, or named objective priorities?
* Which additional rules, once confirmed, are hard requirements, preferences, or authorized and auditable exceptions?
* Which additional historical report views or PDF formats answer a concrete consumer need beyond the current corrected reports and on-demand PDFs?
* Does a future physician-facing workflow or authorization role need an explicit product and security decision beyond the current scheduler, Scheduling Administrator, and Django-superuser boundaries?

When implementation encounters one of these, preserve the ambiguity rather than
silently choosing a policy. The historical examples in section 13 remain
non-authoritative evidence, not a source of inferred rules.

---

# 31. Design principle

The application should make the real scheduling policy explicit enough that software can enforce what is deterministic while leaving genuine judgment visible to the humans responsible for it.

The goal is not:

> Have AI produce a plausible schedule.

The goal is:

> Maintain authoritative scheduling facts and rules, derive what can be derived, identify what needs human judgment, solve constrained choices when useful, preserve provenance, and maintain explainable schedules.

---

# 32. Form and ordinary workspace surface spacing ownership

## 32.1 Accepted ownership contract

Muriel-owned form surfaces use the following ownership model. Ordinary
non-form workspace surfaces use the same surface-boundary rule and the
explicit `.surface-content-stack` extension below. This applies to application
scheduling and administration presentation, not Django Admin or the separate
registration and login presentation.

* A surface container owns its boundary: `.surface-frame` owns its border and
  `.surface-body` owns its `var(--surface-padding)` inset. A child must not
  recreate that inset with exterior padding or a compensating margin.
* An explicit layout parent owns spacing between its direct children through
  `gap`. The standard vertical parent is `.form-content-stack`; grids and
  action rows are layout parents for their own direct children.
* `.surface-content-stack` is the corresponding vertical layout parent for an
  ordinary non-form `.surface-body`. It uses the existing
  `--form-stack-gap: .85rem`, resets only its direct children's block margins,
  and is applied only where direct prose, lists, status content, or actions
  are siblings. It is not an implicit mode for every surface body.
* A `.contextual-action-row` owns the block margins of its direct children as
  well as its wrapping flex gap. An explanatory paragraph therefore cannot
  enlarge the action row outside that row's own layout.
* A direct child of `.form-content-stack` has no exterior block margin. A
  nested stack supplies its own gap once, so a component cannot double its
  parent's spacing.
* Reusable components own only their internal spacing: a `.form-field-group`
  owns label-to-control, helper/error-to-control separation through its grid
  `gap`, and its direct controls contribute no exterior block margin.
  Choice-group spacing, preview padding, and action-row button spacing are
  likewise component-internal. Components do not add a context-dependent
  exterior block margin.
* A hidden direct child must use `hidden` or an equivalent `display: none`
  state. Grid excludes such a child, so it creates no gap; a hidden child must
  not reserve space with visibility, height, or margin.
* Native HTML remains authoritative. A related set of choices is a
  `fieldset` with a `legend`; a section heading is not a substitute for a
  legend merely to achieve a visual treatment.

The reusable structures are deliberately small:

* `.surface-frame`, optional `.surface-header`, and `.surface-body` form a
  bounded form section.
* `.form-content-stack` is the vertical layout owner; `.form-field-grid` is
  the compact multi-column layout owner.
* `.form-field-group` contains one label, control, validation, and optional
  helper. `.form-read-only-value` is the corresponding labeled display value.
* `.form-choice-group` is a `fieldset` containing a `legend` and its choices.
  It owns only the legend/choice and intra-choice gaps.
* `.form-validation-message`, `.form-derived-preview`, and
  `.form-action-area` are semantic components. The action area remains a
  wrapping internal layout, but receives its vertical placement from the
  surrounding stack.
* `physician_picker.html` remains the one shared composite multi-select
  include. Its native fieldset and legend are retained; it is not replaced by
  an ARIA imitation.

Use the existing tokens rather than introducing a second scale:

* `--surface-padding` is the surface-boundary inset.
* `--surface-stack-gap` is the gap between peer form surfaces.
* `--form-stack-gap: .85rem` is the vertical gap for
  `.form-content-stack`; the next migration introduces the token in place of
  the raw value.
* `--section-action-gap` is the compact gap inside an action row.
* The existing `.35rem`, `.45rem`, `.65rem`, and `.75rem` component/grid
  values remain internal component or layout values, not page-level margins.
* The generic control `margin-top: .25rem` is a legacy fallback, not a
  component spacing token: it must not apply to a control that is a direct
  child of `.form-field-group`.

## 32.2 Current inventory

The audit covers `schedules/templates/schedules/` and
`schedules/static/schedules/application.css`. It excludes templates under
`registration/`, Django Admin, and authentication login/password surfaces.

### Surface and layout declarations

| Current declaration | Current behavior | Intended owner and disposition |
| --- | --- | --- |
| `.surface-body { padding: var(--surface-padding) }` | Provides the inset inside every bordered form section. | Correct container ownership; retain. |
| `.form-presentation { display: grid; gap: var(--surface-stack-gap) }` | Spaces top-level validation and surface sections. | Correct form-surface layout ownership; retain. |
| `.form-content-stack { display: grid; gap: .85rem }` | Spaces direct children in the eight consumers listed below. | Correct vertical layout mechanism; retain and use for ordinary stacked surface bodies. |
| `.form-field-grid { display: grid; gap: .75rem }` and its column variants | Spaces peer field groups in compact multi-column areas. It does not neutralize the generic margin on a nested control. | Correct grid ownership; retain. The field-group migration, not this peer grid, removes direct control margins. |
| `.form-action-area { display: flex; ... gap: var(--section-action-gap); margin-top: var(--section-action-gap) }` | Owns button/link wrapping but also pushes itself from preceding content. | Internal flex gap is correct; exterior top margin is legacy self-spacing and must be replaced by a parent stack gap. |
| `.form-choice-group { display: grid; gap: .45rem; margin: .85rem 0 0 }` | Owns choice internals but also adds an exterior top margin. | Internal gap is correct; exterior margin is legacy self-spacing and must be removed after all consumers use a stack. |
| `.form-field { display: grid; gap: .45rem; margin: 0 0 1rem }` and its direct-control margin reset | An older paragraph field wrapper used by the Academic Event removal form. Unlike `.form-field-group`, it already clears its direct controls' generic top margin. | Its internal gap is reusable; its exterior bottom margin is legacy and must be replaced by a stack. |
| `.form-content-stack > * { margin-block: 0 }` | Resets direct-child margins, including the choice-group margin. | Transitional cascade protection; the final contract is no component exterior margins, not a selector-specific reset. |
| `.form-field-group[data-role-attribute]` and `.surface-frame--removal .form-field-group { margin-top: .85rem }` | Add page/context-specific vertical separation outside a layout parent. | Legacy consumer-specific spacing; migrate those bodies to a stack and remove. |
| `.interview-form-context { ... gap: .25rem 1.5rem; margin-bottom: .85rem }` | Its flex gap is internal; its bottom margin separates it from the next grid. | Preserve the internal gap; replace the exterior margin with a parent stack gap. |
| `.form-page-intro`, `.form-return-link`, `.form-page-helper`, and the separate removal surface margin | Space page chrome and peer surfaces. | Page/surface layout ownership, outside the reusable field toolkit; preserve deliberately. |

### Component and semantic-default declarations

| Current declaration | Current behavior | Intended owner and disposition |
| --- | --- | --- |
| `input, select, textarea { margin-top: .25rem; padding: .45rem }` | The padding creates a control's touch/content inset. The top margin duplicates label-to-control spacing when the control is a `.form-field-group` grid item: grid item margins do not collapse with the group's `.35rem` gap, so the ordinary field receives both `.25rem` and `.35rem`. | The generic margin is legacy fallback spacing for unwrapped controls, not control-internal spacing. During migration, clear it for direct controls in `.form-field-group`; retain it only for named legacy/unwrapped consumers until their map entry migrates, then remove the generic rule or narrow it to an explicit remaining legacy selector. |
| `.form-field-group { display: grid; gap: .35rem }` | Separates a label, control, errors, and helper, but currently combines with the generic control top margin. | Correct sole owner of a shared field group's internal label/control/help separation. Retain the gap and make its direct `input`, `select`, and `textarea` exterior-block-margin-free. |
| `.form-read-only-value { padding: .45rem 0 }` | Gives a labeled read-only value vertical breathing room. | Component internal padding; retain. Its last-span wrapping rule is content handling, not exterior spacing. |
| `.form-derived-preview { display: grid; gap: .25rem; ... padding: .55rem .7rem }` | Provides a compact preview card. | Component internal layout/inset; retain. |
| `.form-validation-message { ... padding: .65rem .8rem }` and `.errorlist { margin: .25rem 0; padding-left: 1.2rem }` | Validation has component-local inset; field errors retain list spacing. | Internal semantic component spacing; retain. The validation message's placement belongs to the enclosing presentation/stack. |
| `.form-choice-group > div` and descendants | Flex-wrap and space the rendered Django radio/checkbox choices; labels have their own inset. | Correct choice-group internal layout; retain. |
| `.form-choice-group input`, `.form-checkbox-control`, and availability-choice rules | Remove generic control margin where controls sit inline with their labels. | Correct consumer/component override, not an exterior-spacing mechanism; retain. |
| `h2 { margin-top: 1.5rem }`, ordinary paragraph/list margins, and browser fieldset/legend defaults | Native defaults can leak into a surface body when a component does not explicitly neutralize them. | A stack owns sibling spacing. Migration must use semantic components or explicitly zero direct-child exterior margins; do not add a global reset. |
| `.form-choice-group h3 { margin: 0 }`, `.form-choice-group legend { padding: 0 }`, and fieldset border/padding resets in choice/availability/picker controls | Normalizes individual semantic children without changing their layout role. | Retain the native semantic elements; use the appropriate component structure rather than broad semantic resets. |

### Hidden state, first/last child, and cascade findings

* `.form-field-group[hidden]`,
  `[data-schedule-report-period-value][hidden]`,
  `.physician-picker-choice[hidden]`, and the period-picker submit button use
  `display: none`. They do not occupy a grid track and therefore create no
  parent `gap`. This is the required hidden-child behavior.
* The Schedule Report additionally renders `hidden` on an included-dates
  read-only group and on unavailable coverage choices. Those children also
  leave no phantom gap.
* The relevant first/last-child rules are `.alternative:first-child` and
  `.form-read-only-value > span:last-child`; neither owns form-sibling spacing.
  No form stack relies on a first- or last-child margin exception. Calendar,
  table, and navigation first/last-child rules are outside this form audit.
* The current choice-group margin is cancelled only when it is a direct child
  of `.form-content-stack`, because the later
  `.form-content-stack > * { margin-block: 0 }` rule has equal class
  specificity and wins by source order. This is a fragile cascade override,
  not the desired long-term ownership boundary.
* The generic control margin is overridden by `.form-field`, inline checkbox
  controls, period navigation, report choice controls, and choice labels, but
  not by `.form-field-group` or `.form-field-grid`. Thus every ordinary
  labeled control in a field group currently has duplicated `.25rem` margin
  plus `.35rem` grid-gap separation. The field-group reset is required; these
  other context rules do not make sibling spacing control-owned.

### Formatting-context measurement

Declared grid gap alone is not reliable for native fieldset content. In
headless Google Chrome, using the current `.form-choice-group` declarations,
the measured vertical distance from a heading to its choices was `7.19px`
(`.45rem`), while the measured distance from a fieldset `legend` to its
choices was `0.00px`. The legend participates in the fieldset's special
rendering rather than as an ordinary grid item, so the declared grid `gap`
does not create the intended legend-to-choice separation.

Consequently, the migration must provide an explicit internal legend-to-choice
layout wrapper in the shared choice component when a fieldset needs that
separation. It must not change a semantic choice group into a heading-based
`section` to make grid gap appear. This browser formatting-context behavior is
a component-normalization requirement, not an accepted end-state exception.

## 32.3 Form and structural inventory

The primary form templates already share `form-layout`, `form-presentation`,
surface frames, field groups, read-only values, validation messages, and action
areas to varying degrees:

| Surface family | Current equivalent structures and differences | Migration target |
| --- | --- | --- |
| Physician, effective fact, account, and invitation | Labeled controls and checkboxes use field groups; physician/effective-fact conditionally reveal role attributes. Account invite lacks the section headings used by account edit. | One field-group/checkbox structure and stacked surface bodies; section headings remain content-specific. |
| Vacation, interview, academic event, and event definition | All use labeled fields, read-only context, sections, and action groups. Academic Event alone has a derived preview and physician picker; Interview uses a flex context with a bottom margin. | Preserve specialized preview/picker semantics; put every vertically stacked body child in a stack. |
| Rounding, Community Preceptor, and Monthly Availability rules | All repeat identity/weekday, recurrence choices, effective range, retained-rule context, notes/actions, and removal forms. They duplicate the same fieldset/legend markup and direct-body spacing. | Shared field, choice, effective-range, recurrence-context, and action structures; one fieldset choice implementation. |
| Faculty availability | Uses nested day/period fieldsets and a five-column availability grid. | Intentional structural variant: preserve nested native fieldsets and its grid, while applying the outer surface/stack contract to range, matrix, and actions. |
| Assignment | Uses fields/action sections plus read-only current-assignment and alternative fieldsets that are display content, not form choice components. | Preserve alternative tiers as display fieldsets; use the common field/action/removal structure for mutations. |
| Schedule Report controls | Uses the only heading-based `.form-choice-group` for Range, plus a native fieldset for Include and hidden conditional controls. | Convert Range to the common native choice fieldset and apply the explicit legend-content wrapper. Preserve hidden controls. |
| Stipend rate and custom schedule option | Loop over fields or duplicate individual field groups in several independent forms. | Use the common field/action structure; preserve their compact single-surface presentation. |
| Late Doc | Uses `form.as_p`, bare headings/sections, and bare buttons. | Legacy form structure to migrate fully to the common field, validation, surface, action, and removal structures. |

Current page-specific markup duplicates shared concepts in particular:

* The three recurrence-rule forms each repeat fieldsets for ordinals and
  periods, effective-range grids, notes/actions, and removal-reason forms.
* Primary form templates repeatedly spell out label/control/errors/helper
  field groups and read-only labeled values.
* Removal forms in Academic Event, Assignment, Vacation, Rounding, Community
  Preceptor, Monthly Availability, Interview, and Event Definition each use
  their own compact markup instead of one shared removal surface structure.
* `physician_picker.html` is already a shared include and is the model to use
  for a bounded reusable semantic include. No generalized component system is
  justified.

All current `.form-choice-group` uses are:

| Template | Current element | Required migration |
| --- | --- | --- |
| `schedule_report.html` Range | `section` with `h3` | Replace with native `fieldset`/`legend` plus the explicit legend-content wrapper. |
| `schedule_report.html` Include | `fieldset`/`legend` | Add the wrapper so legend-to-choice spacing is explicit. |
| `academic_event_form.html` period of day | `fieldset`/`legend` | Add the wrapper. |
| `rounding_form.html` ordinals | `fieldset`/`legend` | Add the wrapper. |
| `community_preceptor_form.html` ordinals | `fieldset`/`legend` | Add the wrapper. |
| `community_preceptor_form.html` periods | `fieldset`/`legend` | Add the wrapper. |
| `monthly_availability_rule_form.html` ordinals | `fieldset`/`legend` | Add the wrapper. |
| `monthly_availability_rule_form.html` periods | `fieldset`/`legend` | Add the wrapper. |

All current `.form-content-stack` consumers are:

| Template and surface | Direct children | Current concern and migration |
| --- | --- | --- |
| `schedule_report.html` Report Definition | field group | Correct stack use; retain. |
| `schedule_report.html` Date Range | heading-based choice group, conditional picker, read-only value | Convert the choice group; hidden conditional content already creates no gap. |
| `schedule_report.html` Coverage | native choice group, meta message, conditionally hidden read-only value | Add the legend-content wrapper; retain hidden behavior. |
| `academic_event_form.html` Event | hidden input and field grid | Hidden input does not render; retain the stack. |
| `academic_event_form.html` Schedule | field grid and native choice group | Add the legend-content wrapper. |
| `rounding_form.html` Casual rounder rule | field grid and native choice group | Add the legend-content wrapper. |
| `community_preceptor_form.html` Recurrence and coverage | two native choice groups | Add the legend-content wrapper. |
| `monthly_availability_rule_form.html` Recurrence and coverage | two native choice groups | Add the legend-content wrapper. |

## 32.4 Migration map and intentional structural variants

The next form-structure task owns the shared semantic includes/classes. The
following map covers every affected Muriel form surface without changing domain
behavior in this audit task.

For every map entry that adopts `.form-field-group`, its direct `input`,
`select`, and `textarea` children must have no exterior block margin. The
group's `.35rem` gap is then the only label/control/error/helper separator;
`.form-field-grid` continues to own only peer-field separation. The generic
`.25rem` control margin remains only as a temporary fallback for
`late_doc_form.html`'s `form.as_p`. The named compact, inline, table-row, and
picker variants below must use their own explicit layout-context reset rather
than that fallback. When the mapped legacy consumer has migrated, delete the
generic margin rule or narrow it to any deliberately retained, named legacy
selector.

| Surface | Migration action |
| --- | --- |
| `academic_event_form.html` | Stack every multi-child body, use shared field/read-only/preview/choice/action structures, and normalize its removal form. |
| `academic_event_definition_form.html` | Stack field and action bodies and normalize its removal form. |
| `account_form.html` and `account_invite_form.html` | Normalize section heading and field/action structures; preserve account authority semantics. |
| `assignment_form.html` | Normalize save and removal bodies only; do not reinterpret current assignment or alternative-tier display fieldsets. |
| `community_preceptor_form.html`, `monthly_availability_rule_form.html`, and `rounding_form.html` | Consolidate repeated recurrence, range, notes/actions, retained-context, and removal structures. |
| `custom_availability_category_form.html` | Use the common compact surface/field/action structure across creation, definition, assignment, and deactivation forms. |
| `effective_fact_form.html`, `physician_form.html`, and editable forms in `physician_detail.html` | Replace conditional self-margins with stack ownership while preserving conditional role fields and history context. |
| `faculty_availability_form.html` | Apply common outer field/action structure only; preserve the availability-week matrix and nested day/period fieldsets. |
| `interview_form.html` | Replace the context block's exterior bottom margin with stack placement and normalize removal. |
| `late_doc_form.html` | Migrate from `form.as_p` and bare markup to the common semantic form surface. |
| `schedule_report.html` | Standardize the Range and Include choice groups, preserve hidden controls and progressive enhancement, and retain its three stacks. |
| `stipend_rate_form.html` and `stipend_rate_remove.html` | Replace loop-only/direct-body spacing with the common compact field/action stack. |
| `vacation_form.html` | Stack multi-child bodies and normalize removal while preserving read-only physician context. |
| `physician_picker.html` | Retain as the shared picker include; give it only the common fieldset choice spacing needed by the native legend measurement. |
| `_recurrence_rule_context.html` and `_existing_role_context.html` | Retain as read-only context includes; their surface/table layout is not a field component. |

The following are intentional structural variants, not silently omitted form
consumers. Each remains subject to the shared spacing tokens, explicit layout
ownership, semantic HTML, and consistency rules. They differ from a stacked
editing form only because their real interaction or layout structure differs;
they are not exemptions from the contract or approval to retain inconsistent
spacing.

* Compact navigation, period picker, report picker, scope chooser, and filter
  forms are inline/compact controls. Their flex layout explicitly owns their
  spacing rather than a form-content stack.
* Inline table-row actions, account resend/revoke forms, event resolution
  forms and calendar action forms are action controls
  embedded in tables or contextual action rows rather than surface forms.
* The physician-scope popup, faculty availability matrix, assignment
  alternative tiers, and physician picker are specialized native semantic
  controls. Their structural boundaries remain semantic and explicit rather
  than being flattened into generic field groups.

Separate registration, sign-in, password, invitation-acceptance, and Django
Admin presentation remain outside this migration and audit scope.

No visual rewrite, functional form change, global reset, `!important`, CSS
framework, or generalized component system is authorized by this contract.

## 32.5 Ordinary workspace content-flow inventory

The non-form audit covers authenticated workspace templates with direct prose
or action siblings. The following consumers use `.surface-content-stack`:

* `precepting_solver_week.html` for proposed-week status,
  `precepting_solver_reconcile.html` for the reconciliation summary, and
  `precepting_solver_review.html` for its proposal, unresolved-work,
  manual-history, and apply-action bodies. In that review template, the
  fallback assignment-change body remains a table/disclosure layout, workload
  effects remain one disclosure body, Late Doc review remains a record-list
  body, and the optional comparison remains a report-table body; those explicit
  layouts do not receive the ordinary-content stack. The reconciliation form
  retains its explicit form layout.
* `account_workspace.html` and `stipend_report.html` for flash-message status.
* `physician_detail.html` for its account summary and
  `account_invitation_delivery.html` for manual invitation delivery content.
* `rounding_scheduling_actions.html` for explanatory text and its action row.
* `custom_availability_category_workspace.html` and
  `academic_event_definition_workspace.html` for their explanatory text and
  contextual action rows.
* `work_remaining_summary.html` is the complete authenticated scoped
  work-summary partial. Each consumer supplies its domain projection through
  the shared workspace shell position; detail rows and lists remain below it.
* `stipend_report.html` for missing-rate and Late Doc findings,
  `stipend_rate_workspace.html` for its timeline description, and
  `account_read_only.html` for the administrator account notice.

One-child stack bodies have only their `.surface-body` boundary inset. Hidden
direct children use `display: none`, so grid creates no track or gap for them.
Calendar frames, table wrappers, forms, pickers, physician selection, and
other named composite layouts retain their own explicit layout owners rather
than receiving this stack.
