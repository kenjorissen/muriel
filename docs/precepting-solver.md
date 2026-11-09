# Precepting solver operations

## Meeting workflow

`Fill open slots` keeps all current assignments and proposes only open work.
`Repair findings` starts from the current draft and changes only unlocked work in
the chosen range when that is needed to resolve a finding. In either case,
solving is read-only. The proposal is a meeting change set, not a schedule.

Review the before/after findings, workload summaries, and each `Why this?`
explanation before Apply. `Why this?` distinguishes hard exclusions, named
policy advantages and compromises, equally good choices, and the stable final
tie-break. `Compare with...` forces one displayed eligible alternative against
the same inputs; its feasibility, additional moves, workload effect, and
compromises are evidence for the meeting, not a mutation or replacement
proposal.

Hard exclusions are grouped by physician and retain every applicable shared
candidate-assessment reason, including its concise summary, detail, and source
identity. Solve-local exclusions and position boundaries extend that assessment
only for the current solve; they do not change retained scheduling facts. Only
automatic-eligible candidates appear in `Compare with...`.

Every proposed Monday `Late Doc` must be explicitly confirmed or replaced.
Late Doc is independent of Precepting workload balance and has its own
proportional balance policy. Apply writes the current schedule with attributed
history.

The proposal fingerprint protects the solve/review/apply boundary. If relevant
source facts, assignments, locks, or Interview occupancy changed, Apply and
Late Doc review refuse the stale proposal without changing the draft. Reconcile
retains meeting work against a changed draft when needed; it does not merge or
apply changes silently.

## Policy terminology

`Available` is normal eligibility. `Backup only` is an exceptional but eligible
fallback and is not the Secondary assignment role. `Expected Community
Preceptor` is an available Community commitment that the solver prefers for a
primary position; it is not an automatic assignment. `Faculty-equivalent`
counts toward the two-person hard coverage requirement. A Faculty Secondary
requires an actual effective Faculty role. Late Doc also requires one, except
for an `Available as Float` manual assignment; that narrow exception does not
alter the physician's underlying Community Preceptor or faculty-equivalence
facts.

`Available as Float` is a manual-only candidate state. It requires an effective
Float Preceptor role plus the fixed Admin schedule option, after ordinary
availability and all hard conflicts have been composed. It is excluded from
every solver candidate comparison and fill, while the physician remains eligible
wherever ordinary Faculty or Community Preceptor availability applies.

Faculty Precepting balance is proportional to base available Faculty capacity in
the balance window. A Scheduling Administrator may set one current custom
Faculty Precepting capacity for a physician, from 0 through 10 weekly shifts in
tenths. When present, it replaces that physician's computed fairness target:
each covered weekday AM/PM balance slot receives one tenth with fixed-point
integer arithmetic, so partial windows are prorated. This is intentionally not
effective-dated; a new solve applies the current setting to its whole balance
window, and changing it makes an unapplied proposal stale. It does not change
availability, candidate eligibility, Faculty-equivalent coverage, existing
work counts, or separately computed Monday-PM Late Doc capacity. Vacation,
Academic Events, Rounding, and Interviews can constrain an individual solve but
do not redefine computed capacity. Late Doc continues to use Monday PM
availability separately from Precepting shifts.

## Maintaining policy

Named solver policy belongs in `schedules.services.precepting_solver`, with
direct coverage in `schedules.tests.test_precepting_solver`; source availability
rules continue to belong to their named source owner. Add a named
scheduling-language policy only after a real scheduling example establishes its
meaning. Do not add generic weights, per-physician preference records, or solver
configuration for hypothetical needs.

## Scenario evidence

`RealisticPreceptingSolverScenarioTests` builds a July 2026-January 2027
six-month balance fixture. It includes unequal and effective-dated Faculty
capacity, a Community commitment, Vacation, an Academic Event, Interview
occupancy, normal and casual Rounding, backup-only Friday fallback, a locked
Monday PM assignment, thin Monday PM capacity, and an earlier scheduling-pass
assignment. It proves stable Fill, representative explanations and a forced
comparison, Late Doc review, stale refusal, minimal Repair, and history.

## Equal choices

The solver fixes every named policy objective before resolving any exact tie.
It then uses one final whole-range optimization ordered by a generated,
proposal-local seed and durable physician identifiers. Names are never a solver
tie-break. The seed is retained in the proposal's solve settings, so review,
comparison, edits, and reconciliation retain the established ordering. A new
Solve receives a new seed and can choose another equally valid proposal.

`Why this?` calls out when interchangeable policy candidates were chosen by the
recorded seeded tie-break. The listed alternatives remain grouped by eligibility
and scheduling policy; their order within an equal group follows that seed.

## Timing evidence

Run `./scripts/time-precepting-solver` for the fixed September 2026 one- and
three-month ranges. Schedule Bot supports solve ranges of at most three
calendar months; its balance window may extend to twenty-four calendar months.
The timing workflows use the twelve calendar months ending with each solve
range, so retained historical assignments remain part of proportional fairness.
The wrapper creates a temporary SQLite database and the command replaces its
demo scheduling rows with the commissioned synthetic scenario at the fixed
August 1, 2026 anchor. It does not add timing physicians, change availability,
preassign future sessions, or simplify the roster.

Scenario generation and session initialization happen before each measurement.
The report then separates preflight/input construction, model construction, the
exact independent policy prefix, proportional Faculty fairness, the Late Doc
policy, the seeded tie-break, explanations, and end-to-end preflight plus one
Fill or Repair. `--timeout` changes the one whole-operation deadline; its default
is the same eight seconds used by the synchronous web workflow. A timeout names
the active phase, includes completed phase measurements, creates no proposal,
and does not prevent the command from measuring the other mode.

### Historical six-month diagnostics

The following pre-change measurements are retained as historical diagnostic
evidence only. They do not describe a runnable or supported six-month Schedule
Bot workflow or verification of a new public revision. The profile on the
historical development host (machine name omitted), August 28, 2026 used the
untouched scenario and the same twelve-month windows. Model construction was
not the problem: one, three, and six months built 881, 2,625, and 5,302 variables
in 0.008, 0.045, and 0.168 seconds. At six months the first seven lexicographic stages took
2.789 seconds, but the redundant Secondary minimization then used 27.040
seconds and exhausted the 30-second diagnostic limit. With that stage expressed
directly, three-month Late Doc balance still took 11.194 seconds and the
whole-model overlap search remained unresolved after 120 seconds. These
measurements are why the implementation fixes the exact additive session-local
policy prefix without CP-SAT, gives new absolute-deviation variables complete
hints, optimizes Late Doc balance in its independent lane, and searches a
bounded Monday lane before using the explicit overlap upper bound. Native
presolve remained disabled: enabling it on this roster exceeded stage limits
during repeated simplification.

Before the three-month solve cap, the maintained command measured the one-month
range at 0.603 seconds for preflight, 4.409 seconds for Fill, and 4.425 seconds
for Repair. Three months measured 0.565, 4.845, and 4.670 seconds. The retired
six-month diagnostic measured 0.624, 6.817, and 5.877 seconds; its end-to-end
times were 7.441 seconds for Fill and 6.501 seconds for Repair. That diagnostic
six-month Fill returned 511 Precepting assignments, 24 Late Doc assignments
with 24 overlaps, and no unresolved requirements. The accepted three-second
fairness search, at most two workers for fairness and the evidenced overlap
lane, and one-second one-worker seeded tie-break remain intact.
