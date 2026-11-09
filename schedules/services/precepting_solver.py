"""Read-only input projection and preflight for bounded Precepting solves."""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import secrets
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from itertools import product
from time import perf_counter

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, IntegerField, When
from django.utils import timezone
from ortools.graph.python import min_cost_flow
from ortools.sat.python import cp_model

from ..academic_years import expected_quarter_start
from ..models.late_docs import LateDocAssignment
from ..models.physicians import EffectiveRole, Physician
from ..models.preceptors import PreceptorSession
from ..models.solver import (
    PreceptingSolverLateDocReview,
    PreceptingSolverProposal,
    PreceptingSolverProposalComparison,
    PreceptingSolverProposalEdit,
    PreceptingSolverProposalReconciliation,
)
from ._mutation import normalize_note, require_scheduler, scheduling_mutation
from .holidays import holiday_closure_for, holiday_projections_for_dates
from .operational_attention import organization_today
from .preceptors import (
    CandidateAssessmentReason,
    assignment_alternatives_for_session,
    candidate_assessment_from_projection_row,
    is_manually_eligible_for_position,
    manual_float_availability_reason,
)
from .rounding import normal_rounder_statuses_for_weeks, rounding_weeks
from .slot_projection import RequestedSlot, slot_projection

MAX_SOLVE_CALENDAR_MONTHS = 3
MAX_BALANCE_CALENDAR_MONTHS = 24

logger = logging.getLogger("muriel")


@dataclass(frozen=True)
class SolveDateRange:
    start_date: date
    end_date: date

    def __post_init__(self):
        if not isinstance(self.start_date, date) or not isinstance(self.end_date, date):
            raise ValidationError("Solve ranges require start and end dates.")
        if self.end_date < self.start_date:
            raise ValidationError("Solve range end date cannot be before its start date.")


@dataclass(frozen=True)
class SolverAssignment:
    id: int
    physician_id: int
    physician_name: str
    responsibility: str
    primary_position: int | None
    is_locked: bool | None
    is_faculty: bool = False


@dataclass(frozen=True)
class SolverCandidate:
    physician_id: int
    physician_name: str
    effective_roles: tuple[str, ...]
    availability: str | None
    base_availability: str | None
    ordinarily_eligible_to_precept: bool
    faculty_equivalent: bool
    expected_community_commitment: bool
    preceptor_assignment_id: int | None
    interview_shift_id: int | None
    source_ids: tuple[tuple[str, int], ...]
    precepting_conflict_codes: tuple[str, ...] = ()
    has_soft_academic_event: bool = False
    manually_selectable: bool = True
    manual_float_eligible: bool = False
    manually_eligible_for_late_doc: bool = False
    reasons: tuple[CandidateAssessmentReason, ...] = ()

    @property
    def is_faculty(self):
        return EffectiveRole.Role.FACULTY in self.effective_roles

    @property
    def automatic_eligible(self):
        return not self.manual_float_eligible and not any(
            reason.severity == "hard" for reason in self.reasons
        )


@dataclass(frozen=True)
class SolverPosition:
    key: str
    required: bool
    assignment: SolverAssignment | None
    candidates: tuple[SolverCandidate, ...] = ()


@dataclass(frozen=True)
class SolverSession:
    date: date
    period_of_day: str
    session_id: int | None
    positions: tuple[SolverPosition, ...]
    candidates: tuple[SolverCandidate, ...]


@dataclass(frozen=True)
class SolverLateDocPosition:
    date: date
    assignment: SolverAssignment | None
    candidates: tuple[SolverCandidate, ...]
    all_candidates: tuple[SolverCandidate, ...]


@dataclass(frozen=True)
class CommunityCommitment:
    date: date
    period_of_day: str
    physician_id: int
    physician_name: str
    available: bool


@dataclass(frozen=True)
class FacultyCapacity:
    physician_id: int
    physician_name: str
    precepting_capacity: Decimal
    precepting_share: float
    late_doc_capacity: int
    late_doc_share: float
    precepting_capacity_units: int | None = None
    custom_precepting_capacity_override: Decimal | None = None


@dataclass(frozen=True)
class SourceVersion:
    kind: str
    key: str
    digest: str


@dataclass(frozen=True)
class PreflightBlocker:
    code: str
    message: str
    dates: tuple[date, ...] = ()


@dataclass(frozen=True)
class PreceptingSolverInput:
    solve_range: SolveDateRange
    balance_window: SolveDateRange
    sessions: tuple[SolverSession, ...]
    late_doc_positions: tuple[SolverLateDocPosition, ...]
    community_commitments: tuple[CommunityCommitment, ...]
    faculty_capacities: tuple[FacultyCapacity, ...]
    balance_assignments: tuple[SolverAssignment, ...]
    balance_late_doc_assignments: tuple[SolverAssignment, ...]
    source_versions: tuple[SourceVersion, ...]
    fingerprint: str
    excluded_physician_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class SolverPreflightResult:
    solver_input: PreceptingSolverInput
    blockers: tuple[PreflightBlocker, ...]

    @property
    def ready(self):
        return not self.blockers


class HardFindingConfirmationRequired(ValidationError):
    """Carries the current hard findings that require an explicit Apply decision."""

    def __init__(self, findings, finding_keys):
        self.findings = tuple(findings)
        self.finding_keys = tuple(finding_keys)
        super().__init__(
            "This proposal has hard findings. Review them and explicitly confirm Apply anyway."
        )


@dataclass(frozen=True)
class FillProposalAssignment:
    date: date
    period_of_day: str
    session_id: int
    position: str
    physician_id: int
    physician_name: str
    explanation: FillAssignmentExplanation


@dataclass(frozen=True)
class FillProposalLateDocAssignment:
    date: date
    physician_id: int
    physician_name: str
    explanation: FillAssignmentExplanation


@dataclass(frozen=True)
class FillAssignmentAlternative:
    physician_id: int
    physician_name: str
    reason: str


@dataclass(frozen=True)
class FillAssignmentExclusion:
    physician_id: int
    physician_name: str
    reasons: tuple[CandidateAssessmentReason, ...]


@dataclass(frozen=True)
class FillAssignmentExplanation:
    """Scheduling-language evidence retained with one selected proposal choice."""

    hard_exclusions: tuple[FillAssignmentExclusion, ...]
    policy_advantages: tuple[str, ...]
    policy_disadvantages: tuple[str, ...]
    equally_good_alternatives: tuple[FillAssignmentAlternative, ...]
    other_normally_available_alternatives: tuple[FillAssignmentAlternative, ...]
    backup_only_alternatives: tuple[FillAssignmentAlternative, ...]
    stable_tie_break: str | None
    repair_reason: str | None = None


@dataclass(frozen=True)
class FillProposalUnresolvedRequirement:
    date: date
    period_of_day: str | None
    position: str
    code: str
    message: str
    hard_causes: tuple[str, ...]


@dataclass(frozen=True)
class FillProposalRemoval:
    date: date
    period_of_day: str | None
    session_id: int | None
    position: str
    explanation: FillAssignmentExplanation


@dataclass(frozen=True)
class FacultyWorkloadTotal:
    physician_id: int
    physician_name: str
    current_total: int
    proposed_total: int
    target_share: float
    target_total: float


@dataclass(frozen=True)
class LateDocWorkloadTotal:
    physician_id: int
    physician_name: str
    current_total: int
    proposed_total: int
    target_share: float
    target_total: float


@dataclass(frozen=True)
class FillProposalCompromiseSummary:
    faculty_workloads: tuple[FacultyWorkloadTotal, ...]
    late_doc_workloads: tuple[LateDocWorkloadTotal, ...]
    backup_assignments: tuple[str, ...]
    missed_community_commitments: tuple[str, ...]
    unavoidable_imbalance: tuple[str, ...]
    late_doc_pm_precepting_overlap: tuple[str, ...]


@dataclass(frozen=True)
class FillProposal:
    solver_input_fingerprint: str
    assignments: tuple[FillProposalAssignment, ...]
    late_doc_assignments: tuple[FillProposalLateDocAssignment, ...]
    unresolved_requirements: tuple[FillProposalUnresolvedRequirement, ...]
    compromises: FillProposalCompromiseSummary
    precepting_removals: tuple[FillProposalRemoval, ...] = field(default_factory=tuple)
    late_doc_removals: tuple[FillProposalRemoval, ...] = field(default_factory=tuple)
    solve_seed: str = ""


@dataclass(frozen=True)
class FillProposalComparison:
    """Read-only evidence from forcing or excluding one proposed choice."""

    feasible: bool
    forced_or_excluded: str
    additional_changes: tuple[str, ...]
    workload_effect: FillProposalCompromiseSummary
    compromises: tuple[str, ...]
    proposal: FillProposal


@dataclass(frozen=True)
class _FillRequirement:
    date: date
    period_of_day: str | None
    session_id: int | None
    position: str
    candidates: tuple[SolverCandidate, ...]
    all_candidates: tuple[SolverCandidate, ...]
    excluded_physician_ids: tuple[int, ...] = ()

    @property
    def is_primary(self):
        return self.position.startswith("primary-")

    @property
    def is_late_doc(self):
        return self.position == "late-doc"


def month_solve_range(month):
    """Normalize a selected month to its explicit first and last calendar dates."""
    return SolveDateRange(
        month.replace(day=1), month.replace(day=calendar.monthrange(month.year, month.month)[1])
    )


def quarter_solve_range(academic_year, quarter):
    """Normalize an academic quarter to its explicit first and last calendar dates."""
    if quarter not in (1, 2, 3, 4):
        raise ValidationError("Quarter must be between 1 and 4.")
    start_date = expected_quarter_start(academic_year, f"Q{quarter}")
    end_month = start_date.month + 2
    end_year = start_date.year + (end_month > 12)
    end_month = end_month - 12 if end_month > 12 else end_month
    return SolveDateRange(
        start_date,
        date(end_year, end_month, calendar.monthrange(end_year, end_month)[1]),
    )


def custom_solve_range(start_date, end_date):
    """Normalize a custom selection after validating its explicit date bounds."""
    return SolveDateRange(start_date, end_date)


def clip_solve_range_at_today(solve_range):
    """Return the editable portion of a selected range, if it has one."""
    if not isinstance(solve_range, SolveDateRange):
        raise ValidationError("Supply a normalized SolveDateRange.")
    today = organization_today()
    if solve_range.end_date < today:
        return None
    return SolveDateRange(max(solve_range.start_date, today), solve_range.end_date)


def default_balance_window(solve_range):
    """Return six complete calendar months ending in the solve range's final month."""
    end_month = solve_range.end_date.replace(day=1)
    start_month = _add_months(end_month, -5)
    solve_start_month = solve_range.start_date.replace(day=1)
    return SolveDateRange(min(start_month, solve_start_month), _last_day_of_month(end_month))


def balance_window_for_span(solve_range, span, *, custom_days=None):
    """Derive the inclusive workload window ending with the solve's effective end."""
    if not isinstance(solve_range, SolveDateRange):
        raise ValidationError("Supply a normalized SolveDateRange.")
    fixed_days = {
        "1-day": 1,
        "1-week": 7,
        "2-weeks": 14,
        "4-weeks": 28,
    }
    calendar_months = {"1-month": 1, "3-months": 3, "6-months": 6, "1-year": 12}
    if span in fixed_days:
        start_date = solve_range.end_date - timedelta(days=fixed_days[span] - 1)
    elif span in calendar_months:
        start_date = _shift_date_months(solve_range.end_date, -calendar_months[span]) + timedelta(
            days=1
        )
    elif span == "custom" and isinstance(custom_days, int) and custom_days > 0:
        start_date = solve_range.end_date - timedelta(days=custom_days - 1)
    else:
        raise ValidationError("Choose a valid balance span.")
    return SolveDateRange(start_date, solve_range.end_date)


def validate_precepting_solver_capacity(solve_range, *, balance_window):
    """Reject ranges that exceed Schedule Bot's fixed calendar-month capacity."""
    if not isinstance(solve_range, SolveDateRange):
        raise ValidationError("Supply a normalized SolveDateRange.")
    if not isinstance(balance_window, SolveDateRange):
        raise ValidationError("Balance window must be a normalized SolveDateRange.")

    errors = []
    if _exceeds_calendar_months(solve_range, MAX_SOLVE_CALENDAR_MONTHS):
        errors.append(
            f"Schedule Bot accepts solve ranges of at most {MAX_SOLVE_CALENDAR_MONTHS} "
            "calendar months."
        )
    if _exceeds_calendar_months(balance_window, MAX_BALANCE_CALENDAR_MONTHS):
        errors.append(
            f"Schedule Bot accepts balance windows of at most {MAX_BALANCE_CALENDAR_MONTHS} "
            "calendar months."
        )
    if errors:
        raise ValidationError(errors)


def preflight_precepting_solve(solve_range, *, balance_window=None):
    """Project immutable solver facts and name blockers before optimization begins."""
    if not isinstance(solve_range, SolveDateRange):
        raise ValidationError("Supply a normalized SolveDateRange.")
    balance_window = balance_window or default_balance_window(solve_range)
    if not isinstance(balance_window, SolveDateRange):
        raise ValidationError("Balance window must be a normalized SolveDateRange.")
    validate_precepting_solver_capacity(solve_range, balance_window=balance_window)

    solve_dates = _weekdays_in(solve_range)
    balance_dates = _weekdays_in(balance_window)
    solve_slots = tuple(
        (on_date, period)
        for on_date in solve_dates
        for period in PreceptorSession.PeriodOfDay.values
        if not holiday_closure_for(on_date, period)
    )
    balance_slots = tuple(
        (on_date, period)
        for on_date in balance_dates
        for period in PreceptorSession.PeriodOfDay.values
        if not holiday_closure_for(on_date, period)
    )
    requested_slots = tuple(
        RequestedSlot(on_date, period)
        for on_date, period in sorted(set(solve_slots).union(balance_slots))
    )
    rows = slot_projection(requested_slots)
    rows_by_slot = {}
    for row in rows:
        rows_by_slot.setdefault((row.slot.on_date, row.slot.period_of_day), []).append(row)

    sessions_by_slot = {}
    for session in PreceptorSession.objects.filter(
        date__range=(solve_range.start_date, solve_range.end_date)
    ).order_by("date", "period_of_day", "pk"):
        sessions_by_slot.setdefault((session.date, session.period_of_day), []).append(session)
    solve_sessions = tuple(
        _solver_session(
            on_date,
            period,
            _single_session(sessions_by_slot.get((on_date, period), ())),
            rows_by_slot.get((on_date, period), ()),
        )
        for on_date, period in solve_slots
    )
    late_doc_assignments = {
        assignment.date: assignment
        for assignment in LateDocAssignment.objects.filter(
            date__range=(
                min(solve_range.start_date, balance_window.start_date),
                max(solve_range.end_date, balance_window.end_date),
            )
        ).select_related("physician")
    }
    late_doc_positions = tuple(
        SolverLateDocPosition(
            date=on_date,
            assignment=_late_doc_assignment(late_doc_assignments.get(on_date)),
            candidates=tuple(
                _candidate(
                    row,
                    manual_float_reason=manual_float_availability_reason(row.tier_zero),
                )
                for row in rows_by_slot.get((on_date, "PM"), ())
                if _is_faculty(row)
            ),
            all_candidates=tuple(
                _candidate(
                    row,
                    manual_float_reason=manual_float_availability_reason(row.tier_zero),
                )
                for row in rows_by_slot.get((on_date, "PM"), ())
            ),
        )
        for on_date in solve_dates
        if on_date.weekday() == 0 and not holiday_closure_for(on_date, "PM")
    )
    commitments = tuple(
        CommunityCommitment(
            date=on_date,
            period_of_day=period,
            physician_id=row.physician.pk,
            physician_name=row.physician.display_name,
            available=row.availability == "available",
        )
        for on_date, period in solve_slots
        for row in rows_by_slot.get((on_date, period), ())
        if row.is_expected_community_commitment
    )
    capacities = _faculty_capacities(balance_slots, rows_by_slot)
    balance_assignments = tuple(
        _assignment(row.preceptor_assignment, is_faculty=_is_faculty(row))
        for on_date, period in balance_slots
        for row in rows_by_slot.get((on_date, period), ())
        if row.preceptor_assignment is not None
    )
    balance_late_doc_assignments = tuple(
        _late_doc_assignment(late_doc_assignments.get(on_date))
        for on_date in balance_dates
        if (
            on_date.weekday() == 0
            and not holiday_closure_for(on_date, "PM")
            and late_doc_assignments.get(on_date) is not None
        )
    )
    source_versions = _source_versions(
        rows,
        solve_sessions,
        late_doc_assignments,
        solve_range,
        balance_window,
        holiday_projections_for_dates((*solve_dates, *balance_dates)),
        capacities,
    )
    solver_input = PreceptingSolverInput(
        solve_range=solve_range,
        balance_window=balance_window,
        sessions=solve_sessions,
        late_doc_positions=late_doc_positions,
        community_commitments=commitments,
        faculty_capacities=capacities,
        balance_assignments=balance_assignments,
        balance_late_doc_assignments=balance_late_doc_assignments,
        source_versions=source_versions,
        fingerprint=_digest(
            tuple((version.kind, version.key, version.digest) for version in source_versions)
        ),
    )
    return SolverPreflightResult(solver_input, _preflight_blockers(solver_input, sessions_by_slot))


def new_solve_seed():
    """Return a proposal-local seed; retained proposals never silently reroll it."""
    return secrets.token_hex(16)


class PreceptingSolverTimeout(RuntimeError):
    """The configured synchronous solver operation exhausted its one deadline."""


FACULTY_WORKLOAD_FAIRNESS_TIME_SECONDS = 3
FACULTY_WORKLOAD_FAIRNESS_WORKERS = 2
FINAL_SEEDED_TIE_BREAK_TIME_SECONDS = 1
SYNCHRONOUS_SOLVER_TIME_SECONDS = 8


def produce_fill_proposal(
    solver_input,
    *,
    solve_seed=None,
    max_time_seconds=SYNCHRONOUS_SOLVER_TIME_SECONDS,
    phase_timings=None,
):
    """Return the best feasible Fill proposal without changing schedules."""
    if not isinstance(solver_input, PreceptingSolverInput):
        raise ValidationError("Supply a PreceptingSolverInput from preflight.")
    validate_precepting_solver_capacity(
        solver_input.solve_range, balance_window=solver_input.balance_window
    )
    return _CpSatFillAdapter(
        solver_input,
        solve_seed=solve_seed or "0",
        max_time_seconds=max_time_seconds,
        phase_timings=phase_timings,
    ).solve()


def produce_repair_proposal(
    solver_input,
    *,
    solve_seed=None,
    max_time_seconds=SYNCHRONOUS_SOLVER_TIME_SECONDS,
    phase_timings=None,
):
    """Return minimal deltas that resolve current future findings where feasible."""
    if not isinstance(solver_input, PreceptingSolverInput):
        raise ValidationError("Supply a PreceptingSolverInput from preflight.")
    validate_precepting_solver_capacity(
        solver_input.solve_range, balance_window=solver_input.balance_window
    )
    return _CpSatFillAdapter(
        solver_input,
        repair=True,
        solve_seed=solve_seed or "0",
        max_time_seconds=max_time_seconds,
        phase_timings=phase_timings,
    ).solve()


def compare_fill_proposal(
    solver_input, proposal, assignment, *, alternative_physician_id=None, exclude=False
):
    """Compare a forced eligible alternative or exclusion without changing a proposal."""
    if not isinstance(solver_input, PreceptingSolverInput):
        raise ValidationError("Supply a PreceptingSolverInput from preflight.")
    validate_precepting_solver_capacity(
        solver_input.solve_range, balance_window=solver_input.balance_window
    )
    if not isinstance(proposal, FillProposal):
        raise ValidationError("Supply the FillProposal being compared.")
    if proposal.solver_input_fingerprint != solver_input.fingerprint:
        raise ValidationError(
            "The FillProposal was produced from a different PreceptingSolverInput."
        )
    if not isinstance(assignment, (FillProposalAssignment, FillProposalLateDocAssignment)):
        raise ValidationError("Supply one assignment from the FillProposal.")
    if assignment not in proposal.assignments + proposal.late_doc_assignments:
        raise ValidationError("The assignment is not selected by the FillProposal.")
    if (alternative_physician_id is None) == (not exclude):
        raise ValidationError("Choose one eligible alternative or exclude the proposed physician.")

    period = assignment.period_of_day if isinstance(assignment, FillProposalAssignment) else None
    position = assignment.position if isinstance(assignment, FillProposalAssignment) else "late-doc"
    choice = (assignment.date, period, position, assignment.physician_id)
    if exclude:
        adapter = _CpSatFillAdapter(
            solver_input,
            excluded_choices=(choice,),
            solve_seed=proposal.solve_seed or "0",
            max_time_seconds=SYNCHRONOUS_SOLVER_TIME_SECONDS,
        )
        action = f"Excluded {assignment.physician_name} from {_position_label(position)}."
    else:
        adapter = _CpSatFillAdapter(
            solver_input,
            forced_choices=((assignment.date, period, position, alternative_physician_id),),
            solve_seed=proposal.solve_seed or "0",
            max_time_seconds=SYNCHRONOUS_SOLVER_TIME_SECONDS,
        )
        action = f"Forced the selected alternative for {_position_label(position)}."
    compared = adapter.solve()
    original = _proposal_choice_labels(proposal)
    replacement = _proposal_choice_labels(compared)
    unresolved = _proposal_unresolved_labels(compared)
    additional_changes = tuple(
        replacement.get(
            key,
            unresolved.get(key, _removed_proposal_choice_label(key)),
        )
        for key in sorted(
            set(original).union(replacement),
            key=lambda value: (value[0], {"AM": 0, "PM": 1, None: 2}[value[1]], value[2]),
        )
        if key != (assignment.date, period, position) and original.get(key) != replacement.get(key)
    )
    compromises = tuple(
        dict.fromkeys(
            compared.compromises.backup_assignments
            + compared.compromises.missed_community_commitments
            + compared.compromises.unavoidable_imbalance
        )
    )
    return FillProposalComparison(
        feasible=not compared.unresolved_requirements,
        forced_or_excluded=action,
        additional_changes=additional_changes,
        workload_effect=compared.compromises,
        compromises=compromises,
        proposal=compared,
    )


class _CpSatFillAdapter:
    """Keep the OR-Tools boundary limited to the immutable solver projection."""

    def __init__(
        self,
        solver_input,
        *,
        forced_choices=(),
        excluded_choices=(),
        repair=False,
        solve_seed="0",
        max_time_seconds=None,
        phase_timings=None,
    ):
        if max_time_seconds is not None and max_time_seconds <= 0:
            raise ValidationError("CP-SAT operation time limits must be positive.")
        self.repair = repair
        self.solve_seed = str(solve_seed)
        self.max_time_seconds = max_time_seconds
        self.solve_deadline = None
        self.phase_timings = phase_timings if phase_timings is not None else {}
        self.current_phase = None
        self.phase_started = None
        self.baseline = {}
        self.movable_assignment_ids = frozenset()
        if repair:
            solver_input = self._repair_input(solver_input)
        self.solver_input = solver_input
        self.requirements = self._requirements()
        self.model = cp_model.CpModel()
        self.variables = {}
        self.coverage_variables = {}
        self.forced_choices = tuple(forced_choices)
        self.excluded_choices = tuple(excluded_choices)
        self.late_doc_band_totals = {}
        self.retained_solution_hint = ()
        self.overlap_sources = {}
        self.primary_one_target = 0

    def _repair_input(self, solver_input):
        """Open only unlocked positions, retaining their current choice as the repair baseline."""
        movable_ids = set()
        sessions = []
        for session in solver_input.sessions:
            positions = []
            for position in session.positions:
                assignment = position.assignment
                key = (session.date, session.period_of_day, position.key)
                if assignment and not assignment.is_locked:
                    movable_ids.add(assignment.id)
                    self.baseline[key] = assignment
                    positions.append(replace(position, assignment=None))
                else:
                    positions.append(position)
            sessions.append(replace(session, positions=tuple(positions)))
        late_doc_positions = []
        for position in solver_input.late_doc_positions:
            key = (position.date, None, "late-doc")
            if position.assignment:
                self.baseline[key] = position.assignment
                late_doc_positions.append(replace(position, assignment=None))
            else:
                late_doc_positions.append(position)
        self.movable_assignment_ids = frozenset(movable_ids)
        return replace(
            solver_input,
            sessions=tuple(sessions),
            late_doc_positions=tuple(late_doc_positions),
            balance_assignments=tuple(
                assignment
                for assignment in solver_input.balance_assignments
                if assignment.id not in self.movable_assignment_ids
            ),
            balance_late_doc_assignments=tuple(
                assignment
                for assignment in solver_input.balance_late_doc_assignments
                if (
                    position := next(
                        (
                            item
                            for item in solver_input.late_doc_positions
                            if item.assignment and item.assignment.id == assignment.id
                        ),
                        None,
                    )
                )
                is None
            ),
        )

    def solve(self):
        self.solve_deadline = (
            perf_counter() + self.max_time_seconds if self.max_time_seconds is not None else None
        )
        try:
            self._measure_phase("model construction", self._add_variables_and_session_constraints)
            self._measure_phase("forced choices", self._apply_comparison_constraints)
            self._measure_phase("independent policy prefix", self._fix_independent_policy_outcomes)
            self._apply_feasibility_priorities()
            solver = self._measure_phase("seeded tie-break", self._resolve_seeded_ties)
            selected = self._solve_selected(solver)
            self._check_deadline()
            self.current_phase = "proposal explanations"
            self.phase_started = perf_counter()
            policy_objectives = _policy_objectives(self.solver_input, selected)
            unresolved = self._unresolved(selected)
            compromises = _compromise_summary(self.solver_input, selected, unresolved)
            proposal = FillProposal(
                solver_input_fingerprint=self.solver_input.fingerprint,
                assignments=tuple(
                    FillProposalAssignment(
                        date=requirement.date,
                        period_of_day=requirement.period_of_day,
                        session_id=requirement.session_id,
                        position=requirement.position,
                        physician_id=candidate.physician_id,
                        physician_name=candidate.physician_name,
                        explanation=(
                            self._repair_explanation()
                            if self.repair
                            else _explain_choice(
                                requirement,
                                candidate,
                                selected,
                                self.solver_input,
                                policy_objectives,
                                self.late_doc_band_totals,
                                self.solve_seed,
                            )
                        ),
                    )
                    for requirement, candidate in selected.items()
                    if not requirement.is_late_doc
                ),
                late_doc_assignments=tuple(
                    FillProposalLateDocAssignment(
                        date=requirement.date,
                        physician_id=candidate.physician_id,
                        physician_name=candidate.physician_name,
                        explanation=(
                            self._repair_explanation()
                            if self.repair
                            else _explain_choice(
                                requirement,
                                candidate,
                                selected,
                                self.solver_input,
                                policy_objectives,
                                self.late_doc_band_totals,
                                self.solve_seed,
                            )
                        ),
                    )
                    for requirement, candidate in selected.items()
                    if requirement.is_late_doc
                ),
                unresolved_requirements=unresolved,
                compromises=compromises,
                solve_seed=self.solve_seed,
            )
            self.phase_timings["proposal explanations"] = perf_counter() - self.phase_started
            self._check_deadline()
            return self._repair_deltas(proposal, selected) if self.repair else proposal
        finally:
            self.solve_deadline = None
            self.current_phase = None
            self.phase_started = None

    def _measure_phase(self, name, operation):
        self.current_phase = name
        self.phase_started = perf_counter()
        try:
            return operation()
        finally:
            self.phase_timings[name] = perf_counter() - self.phase_started
            self.current_phase = None
            self.phase_started = None

    def _check_deadline(self):
        if self.solve_deadline is not None and perf_counter() >= self.solve_deadline:
            self._raise_timeout()

    def _raise_timeout(self):
        timings = dict(self.phase_timings)
        if self.current_phase is not None and self.phase_started is not None:
            timings[self.current_phase] = perf_counter() - self.phase_started
        evidence = "; ".join(f"{phase} {seconds:.3f}s" for phase, seconds in timings.items())
        limit = f"{self.max_time_seconds:.3f}s"
        phase = self.current_phase or "the solver operation"
        raise PreceptingSolverTimeout(
            f"Schedule Bot stopped at its {limit} synchronous limit during {phase}. "
            f"Measured phases: {evidence or 'no phase completed'}. No proposal was created."
        )

    @staticmethod
    def _repair_explanation():
        return FillAssignmentExplanation(
            (),
            ("Required to resolve current hard findings with the fewest assignment changes.",),
            (),
            (),
            (),
            (),
            None,
            repair_reason="Needed to resolve a hard finding while retaining other assignments.",
        )

    def _repair_deltas(self, proposal, selected):
        assignments = []
        late_doc_assignments = []
        precepting_removals = []
        late_doc_removals = []
        for requirement in self.requirements:
            key = (requirement.date, requirement.period_of_day, requirement.position)
            baseline = self.baseline.get(key)
            candidate = selected.get(requirement)
            if baseline and candidate and baseline.physician_id == candidate.physician_id:
                continue
            if baseline is None and candidate is None:
                continue
            if candidate is None:
                if baseline and not requirement.is_primary and not requirement.is_late_doc:
                    precepting_removals.append(
                        FillProposalRemoval(
                            requirement.date,
                            requirement.period_of_day,
                            requirement.session_id,
                            requirement.position,
                            self._repair_explanation(),
                        )
                    )
                # Required coverage cannot be removed, so unresolved work retains its baseline.
                continue
            explanation = self._repair_explanation()
            if requirement.is_late_doc:
                late_doc_assignments.append(
                    FillProposalLateDocAssignment(
                        requirement.date,
                        candidate.physician_id,
                        candidate.physician_name,
                        explanation,
                    )
                )
            else:
                assignments.append(
                    FillProposalAssignment(
                        requirement.date,
                        requirement.period_of_day,
                        requirement.session_id,
                        requirement.position,
                        candidate.physician_id,
                        candidate.physician_name,
                        explanation,
                    )
                )
        return replace(
            proposal,
            assignments=tuple(assignments),
            late_doc_assignments=tuple(late_doc_assignments),
            precepting_removals=tuple(precepting_removals),
            late_doc_removals=tuple(late_doc_removals),
        )

    def _solve_selected(self, solver=None):
        if solver is None:
            solver = self._solver()
            status = self._solve_model(solver)
        else:
            status = solver.ResponseProto().status
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            raise RuntimeError("The Fill model has no feasible empty proposal.")
        selected = {
            requirement: candidate
            for (requirement, candidate), variable in self.variables.items()
            if solver.Value(variable)
        }
        return selected

    def _apply_feasibility_priorities(self):
        """Fix named scheduling policy stages before stable names break ties."""
        self._measure_phase("Faculty fairness", self._minimize_faculty_workload_imbalance)
        self._measure_phase("Late Doc policy", self._apply_late_doc_balance_and_overlap_policy)
        self._measure_phase("first-primary preference", self._fix_first_primary_preference)

    def _fix_independent_policy_outcomes(self):
        """Fix the exact session-local prefix without repeatedly solving the whole range.

        Before proportional workload balance, every policy objective is additive
        across independent sessions (and independent Late Doc positions).  Solving
        the complete interchangeable assignment model once per objective made the
        otherwise tiny Secondary proof accidentally combinatorial.  Enumerating at
        most three positions in one session produces the same lexicographic prefix
        and also supplies a complete feasible hint for the first global stage.
        """
        requirements_by_session = {}
        late_requirements = []
        for requirement in self.requirements:
            if requirement.is_late_doc:
                late_requirements.append(requirement)
            else:
                requirements_by_session.setdefault(requirement.session_id, []).append(requirement)

        selected_hint = {}
        coverage_hint = {}
        sessions_by_id = {
            session.session_id: session
            for session in self.solver_input.sessions
            if session.session_id is not None
        }
        for session_id, session in sessions_by_id.items():
            requirements = tuple(requirements_by_session.get(session_id, ()))
            best_metrics, best_choices = self._best_component_choices(requirements, session=session)
            self._fix_component_metrics(
                requirements, best_metrics, coverage=self.coverage_variables[session_id]
            )
            selected_hint.update(best_choices)
            coverage_hint[session_id] = best_metrics[1]
            self.primary_one_target += best_metrics[12]

        for requirement in late_requirements:
            best_metrics, best_choices = self._best_component_choices((requirement,))
            self._fix_component_metrics((requirement,), best_metrics)
            selected_hint.update(best_choices)

        self.retained_solution_hint = tuple(
            (variable, int(selected_hint.get(requirement) == candidate))
            for (requirement, candidate), variable in self.variables.items()
        ) + tuple(
            (variable, coverage_hint.get(session_id, 0))
            for session_id, variable in self.coverage_variables.items()
        )

    def _best_component_choices(self, requirements, *, session=None):
        choices_by_requirement = []
        for requirement in requirements:
            forced_ids = {
                physician_id
                for on_date, period, position, physician_id in self.forced_choices
                if (on_date, period, position)
                == (requirement.date, requirement.period_of_day, requirement.position)
            }
            excluded_ids = {
                physician_id
                for on_date, period, position, physician_id in self.excluded_choices
                if (on_date, period, position)
                == (requirement.date, requirement.period_of_day, requirement.position)
            }
            candidates = tuple(
                sorted(
                    (
                        candidate
                        for candidate in requirement.candidates
                        if candidate.physician_id not in excluded_ids
                        and (not forced_ids or candidate.physician_id in forced_ids)
                    ),
                    key=lambda candidate: _seeded_choice_key(
                        self.solve_seed, requirement, candidate
                    ),
                )
            )
            choices_by_requirement.append(candidates if forced_ids else (None, *candidates))

        existing_faculty_equivalent = 0
        if session is not None:
            candidates_by_id = {
                candidate.physician_id: candidate for candidate in session.candidates
            }
            existing_faculty_equivalent = sum(
                candidates_by_id[position.assignment.physician_id].faculty_equivalent
                for position in session.positions
                if position.assignment is not None
                and position.assignment.physician_id in candidates_by_id
            )

        best = None
        best_choices = None
        for component_choices in product(*choices_by_requirement):
            self._check_deadline()
            selected = {
                requirement: candidate
                for requirement, candidate in zip(requirements, component_choices, strict=True)
                if candidate is not None
            }
            physician_ids = [candidate.physician_id for candidate in selected.values()]
            if len(physician_ids) != len(set(physician_ids)):
                continue
            metrics = self._component_metrics(
                requirements,
                selected,
                existing_faculty_equivalent=existing_faculty_equivalent,
            )
            if best is None or metrics > best:
                best = metrics
                best_choices = selected
        if best is None:
            raise ValidationError("The forced solver choices cannot satisfy one session.")
        return best, best_choices

    def _component_metrics(self, requirements, selected, *, existing_faculty_equivalent=0):
        primary_count = sum(requirement.is_primary for requirement in selected)
        coverage_count = int(
            existing_faculty_equivalent
            + sum(candidate.faculty_equivalent for candidate in selected.values())
            >= 2
        )
        late_doc_count = sum(requirement.is_late_doc for requirement in selected)
        changes = 0
        severity = 0
        for requirement in requirements:
            baseline = self.baseline.get(
                (requirement.date, requirement.period_of_day, requirement.position)
            )
            candidate = selected.get(requirement)
            if baseline is not None:
                changed = candidate is None or candidate.physician_id != baseline.physician_id
            else:
                changed = candidate is not None
            changes += changed
            severity += baseline is not None and candidate is None
            severity += (
                candidate is not None
                and candidate.availability == "backup_only"
                and (baseline is None or candidate.physician_id != baseline.physician_id)
            )
        backup_count = sum(
            candidate.availability == "backup_only" for candidate in selected.values()
        )
        soft_academic_event_count = sum(
            candidate.has_soft_academic_event for candidate in selected.values()
        )
        community_count = sum(
            requirement.is_primary and selected[requirement].expected_community_commitment
            for requirement in selected
        )
        primary_faculty_count = sum(
            requirement.is_primary and selected[requirement].faculty_equivalent
            for requirement in selected
        )
        secondary_count = sum(requirement.position == "secondary" for requirement in selected)
        community_first_count = sum(
            requirement.position == "primary-1"
            and selected[requirement].expected_community_commitment
            for requirement in selected
        )
        community_first_sort_score = sum(
            _community_first_sort_value(requirement, selected[requirement])
            for requirement in selected
        )
        primary_one_count = sum(requirement.position == "primary-1" for requirement in selected)
        return (
            primary_count,
            coverage_count,
            late_doc_count,
            -changes if self.repair else 0,
            -severity if self.repair else 0,
            -backup_count,
            -soft_academic_event_count,
            community_count,
            primary_faculty_count,
            -secondary_count,
            community_first_count,
            community_first_sort_score,
            primary_one_count,
        )

    def _fix_component_metrics(self, requirements, metrics, *, coverage=None):
        variables = {
            requirement: [
                (candidate, variable)
                for (item, candidate), variable in self.variables.items()
                if item == requirement
            ]
            for requirement in requirements
        }

        def selected(predicate):
            return [
                variable
                for requirement, choices in variables.items()
                for candidate, variable in choices
                if predicate(requirement, candidate)
            ]

        self.model.Add(sum(selected(lambda requirement, _: requirement.is_primary)) == metrics[0])
        if coverage is not None:
            self.model.Add(coverage == metrics[1])
        self.model.Add(sum(selected(lambda requirement, _: requirement.is_late_doc)) == metrics[2])
        if self.repair:
            changes = []
            severity = []
            for requirement, choices in variables.items():
                baseline = self.baseline.get(
                    (requirement.date, requirement.period_of_day, requirement.position)
                )
                if baseline is not None:
                    retained = next(
                        (
                            variable
                            for candidate, variable in choices
                            if candidate.physician_id == baseline.physician_id
                        ),
                        None,
                    )
                    changes.append(1 - retained if retained is not None else 1)
                    severity.append(1 - sum(variable for _, variable in choices))
                else:
                    changes.extend(variable for _, variable in choices)
                severity.extend(
                    variable
                    for candidate, variable in choices
                    if candidate.availability == "backup_only"
                    and (baseline is None or candidate.physician_id != baseline.physician_id)
                )
            self.model.Add(sum(changes) == -metrics[3])
            self.model.Add(sum(severity) == -metrics[4])
        self.model.Add(
            sum(selected(lambda _, candidate: candidate.availability == "backup_only"))
            == -metrics[5]
        )
        self.model.Add(
            sum(selected(lambda _, candidate: candidate.has_soft_academic_event)) == -metrics[6]
        )
        self.model.Add(
            sum(
                selected(
                    lambda requirement, candidate: (
                        requirement.is_primary and candidate.expected_community_commitment
                    )
                )
            )
            == metrics[7]
        )
        self.model.Add(
            sum(
                selected(
                    lambda requirement, candidate: (
                        requirement.is_primary and candidate.faculty_equivalent
                    )
                )
            )
            == metrics[8]
        )
        self.model.Add(
            sum(selected(lambda requirement, _: requirement.position == "secondary")) == -metrics[9]
        )
        self.model.Add(
            sum(
                selected(
                    lambda requirement, candidate: (
                        requirement.position == "primary-1"
                        and candidate.expected_community_commitment
                    )
                )
            )
            == metrics[10]
        )
        self.model.Add(
            sum(
                _community_first_sort_value(requirement, candidate) * variable
                for (requirement, candidate), variable in self.variables.items()
                if requirement in requirements
            )
            == metrics[11]
        )

    def _fix_first_primary_preference(self):
        self.model.Add(
            sum(
                variable
                for (requirement, _), variable in self.variables.items()
                if requirement.position == "primary-1"
            )
            == self.primary_one_target
        )

    def has_policy_outcome(self, policy_objectives, late_doc_totals):
        """Check one forced choice against the already-selected policy outcome."""
        self._add_variables_and_session_constraints()
        self._apply_comparison_constraints()
        (
            primary_count,
            coverage_count,
            late_doc_count,
            backup_count,
            soft_academic_event_count,
            community_count,
            primary_faculty_count,
            secondary_count,
            community_first_count,
            community_first_sort_score,
            faculty_imbalance,
            late_imbalance,
            late_overlap,
            primary_one_count,
        ) = policy_objectives
        self.model.Add(
            sum(
                variable
                for (requirement, _), variable in self.variables.items()
                if requirement.is_primary
            )
            == primary_count
        )
        self.model.Add(sum(self.coverage_variables.values()) == coverage_count)
        self.model.Add(
            sum(
                variable
                for (requirement, _), variable in self.variables.items()
                if requirement.is_late_doc
            )
            == late_doc_count
        )
        self.model.Add(
            sum(
                variable
                for (_, candidate), variable in self.variables.items()
                if candidate.availability == "backup_only"
            )
            == backup_count
        )
        self.model.Add(
            sum(
                variable
                for (_, candidate), variable in self.variables.items()
                if candidate.has_soft_academic_event
            )
            == soft_academic_event_count
        )
        self.model.Add(
            sum(
                variable
                for (requirement, candidate), variable in self.variables.items()
                if requirement.is_primary and candidate.expected_community_commitment
            )
            == community_count
        )
        self.model.Add(
            sum(
                variable
                for (requirement, candidate), variable in self.variables.items()
                if requirement.is_primary and candidate.faculty_equivalent
            )
            == primary_faculty_count
        )
        self.model.Add(
            sum(
                variable
                for (requirement, _), variable in self.variables.items()
                if requirement.position == "secondary"
            )
            == secondary_count
        )
        self.model.Add(
            sum(
                variable
                for (requirement, candidate), variable in self.variables.items()
                if requirement.position == "primary-1" and candidate.expected_community_commitment
            )
            == community_first_count
        )
        self.model.Add(
            sum(
                _community_first_sort_value(requirement, candidate) * variable
                for (requirement, candidate), variable in self.variables.items()
            )
            == community_first_sort_score
        )
        self._constrain_faculty_workload_imbalance(faculty_imbalance)
        self._constrain_late_doc_balance(late_imbalance, late_doc_totals)
        self.model.Add(sum(self._late_doc_overlap_variables()) == late_overlap)
        self.model.Add(
            sum(
                variable
                for (requirement, _), variable in self.variables.items()
                if requirement.position == "primary-1"
            )
            == primary_one_count
        )
        return self._solve_model(self._solver()) in (cp_model.OPTIMAL, cp_model.FEASIBLE)

    def _apply_comparison_constraints(self):
        choices = {
            (
                requirement.date,
                requirement.period_of_day,
                requirement.position,
                candidate.physician_id,
            ): variable
            for (requirement, candidate), variable in self.variables.items()
        }
        for choice in self.forced_choices:
            variable = choices.get(choice)
            if variable is None:
                raise ValidationError(
                    "The forced physician is not an eligible alternative for this position."
                )
            self.model.Add(variable == 1)
        for choice in self.excluded_choices:
            variable = choices.get(choice)
            if variable is None:
                raise ValidationError(
                    "The excluded physician is not an eligible alternative for this position."
                )
            self.model.Add(variable == 0)

    def _minimize_faculty_workload_imbalance(self):
        capacities = self.solver_input.faculty_capacities
        capacity_total = sum(_precepting_capacity_units(capacity) for capacity in capacities)
        if not capacity_total:
            return
        current = _assignment_counts(self.solver_input.balance_assignments, capacities)
        existing_total = sum(current.values())
        proposed = [
            variable
            for (requirement, candidate), variable in self.variables.items()
            if not requirement.is_late_doc and candidate.is_faculty
        ]
        total_assignments = existing_total + sum(proposed)
        maximum_deviation = capacity_total * (existing_total + len(proposed))
        retained_values = {
            variable.Index(): value for variable, value in self.retained_solution_hint
        }
        hinted_proposed_total = existing_total + sum(
            retained_values.get(variable.Index(), 0) for variable in proposed
        )
        deviations = []
        deviation_hints = []
        for capacity in capacities:
            physician_variables = [
                variable
                for (requirement, candidate), variable in self.variables.items()
                if not requirement.is_late_doc
                and candidate.is_faculty
                and candidate.physician_id == capacity.physician_id
            ]
            physician_total = current.get(capacity.physician_id, 0) + sum(physician_variables)
            deviation = self.model.NewIntVar(
                0, maximum_deviation, f"faculty_workload_{capacity.physician_id}"
            )
            self.model.AddAbsEquality(
                deviation,
                physician_total * capacity_total
                - _precepting_capacity_units(capacity) * total_assignments,
            )
            deviations.append(deviation)
            hinted_physician_total = current.get(capacity.physician_id, 0) + sum(
                retained_values.get(variable.Index(), 0) for variable in physician_variables
            )
            deviation_hints.append(
                (
                    deviation,
                    abs(
                        hinted_physician_total * capacity_total
                        - _precepting_capacity_units(capacity) * hinted_proposed_total
                    ),
                )
            )
        self.retained_solution_hint += tuple(deviation_hints)
        self._minimize_then_fix(deviations, fairness_stage=True)

    def _constrain_faculty_workload_imbalance(self, target_imbalance):
        capacities = self.solver_input.faculty_capacities
        capacity_total = sum(_precepting_capacity_units(capacity) for capacity in capacities)
        if not capacity_total:
            return
        current = _assignment_counts(self.solver_input.balance_assignments, capacities)
        existing_total = sum(current.values())
        proposed = [
            variable
            for (requirement, candidate), variable in self.variables.items()
            if not requirement.is_late_doc and candidate.is_faculty
        ]
        total_assignments = existing_total + sum(proposed)
        maximum_deviation = capacity_total * (existing_total + len(proposed))
        deviations = []
        for capacity in capacities:
            physician_total = current.get(capacity.physician_id, 0) + sum(
                variable
                for (requirement, candidate), variable in self.variables.items()
                if not requirement.is_late_doc
                and candidate.is_faculty
                and candidate.physician_id == capacity.physician_id
            )
            deviation = self.model.NewIntVar(
                0, maximum_deviation, f"comparison_faculty_workload_{capacity.physician_id}"
            )
            self.model.AddAbsEquality(
                deviation,
                physician_total * capacity_total
                - _precepting_capacity_units(capacity) * total_assignments,
            )
            deviations.append(deviation)
        self.model.Add(sum(deviations) == target_imbalance)

    def _apply_late_doc_balance_and_overlap_policy(self):
        capacities = self.solver_input.faculty_capacities
        capacity_total = sum(capacity.late_doc_capacity for capacity in capacities)
        late_variables = [
            variable
            for (requirement, _), variable in self.variables.items()
            if requirement.is_late_doc
        ]
        if capacity_total and late_variables:
            current = _assignment_counts(self.solver_input.balance_late_doc_assignments, capacities)
            existing_total = sum(current.values())
            total_assignments = existing_total + sum(late_variables)
            maximum_deviation = capacity_total * (existing_total + len(late_variables))
            retained_values = {
                variable.Index(): value for variable, value in self.retained_solution_hint
            }
            hinted_assignment_total = existing_total + sum(
                retained_values.get(variable.Index(), 0) for variable in late_variables
            )
            deviations = []
            deviation_hints = []
            totals = {}
            for capacity in capacities:
                physician_variables = [
                    variable
                    for (requirement, candidate), variable in self.variables.items()
                    if requirement.is_late_doc and candidate.physician_id == capacity.physician_id
                ]
                physician_total = current.get(capacity.physician_id, 0) + sum(physician_variables)
                totals[capacity.physician_id] = physician_total
                deviation = self.model.NewIntVar(
                    0, maximum_deviation, f"late_doc_workload_{capacity.physician_id}"
                )
                self.model.AddAbsEquality(
                    deviation,
                    physician_total * capacity_total
                    - capacity.late_doc_capacity * total_assignments,
                )
                deviations.append(deviation)
                hinted_physician_total = current.get(capacity.physician_id, 0) + sum(
                    retained_values.get(variable.Index(), 0) for variable in physician_variables
                )
                deviation_hints.append(
                    (
                        deviation,
                        abs(
                            hinted_physician_total * capacity_total
                            - capacity.late_doc_capacity * hinted_assignment_total
                        ),
                    )
                )
            self.retained_solution_hint += tuple(deviation_hints)
            best_totals = self._minimize_late_doc_balance(deviations, late_variables)
            for physician_id, total in totals.items():
                best_total = best_totals[physician_id]
                self.late_doc_band_totals[physician_id] = best_total
                # The named one-assignment band permits convenient PM overlap without hiding balance.
                self.model.Add(total >= max(0, best_total - 1))
                self.model.Add(total <= best_total + 1)
            self._maximize_late_doc_overlap_with_retained_pm(
                late_variables, deviations, best_totals
            )
        overlaps = self._late_doc_overlap_variables()
        if overlaps:
            self.model.Add(sum(overlaps) == self._maximize_reduced_late_doc_overlap(overlaps))

    def _minimize_late_doc_balance(self, deviations, late_variables):
        """Optimize the independent Late Doc lane without a whole-model proof.

        Earlier policy has already fixed the number of selected Late Docs.  At
        this point the remaining decision is a capacitated assignment from each
        Monday to a Faculty member with the same already-fixed independent
        policy outcome; its proportional absolute deviations are separable by
        physician.  A min-cost flow therefore gives the exact same balance
        optimum without asking CP-SAT to prove an optimum through the
        unrelated, highly interchangeable Precepting model.
        """
        retained_values = {
            variable.Index(): value for variable, value in self.retained_solution_hint
        }
        capacities = self.solver_input.faculty_capacities
        capacity_total = sum(capacity.late_doc_capacity for capacity in capacities)
        current = _assignment_counts(self.solver_input.balance_late_doc_assignments, capacities)
        existing_total = sum(current.values())
        selected_count = sum(
            retained_values.get(variable.Index(), 0) for variable in late_variables
        )
        requirements = tuple(
            requirement for requirement in self.requirements if requirement.is_late_doc
        )

        flow = min_cost_flow.SimpleMinCostFlow()
        source = 0
        sink = 1
        requirement_nodes = {
            requirement: index for index, requirement in enumerate(requirements, start=2)
        }
        capacity_nodes = {
            capacity.physician_id: index
            for index, capacity in enumerate(capacities, start=2 + len(requirements))
        }
        selected_arcs = {}
        for requirement, node in requirement_nodes.items():
            flow.add_arc_with_capacity_and_unit_cost(source, node, 1, 0)
            allowed_candidates = self._late_doc_candidates_with_fixed_policy_outcome(
                requirement, retained_values
            )
            for (item, candidate), variable in self.variables.items():
                if item == requirement and candidate in allowed_candidates:
                    selected_arcs[
                        flow.add_arc_with_capacity_and_unit_cost(
                            node, capacity_nodes[candidate.physician_id], 1, 0
                        )
                    ] = (variable, candidate)
        for capacity in capacities:
            previous_deviation = abs(
                current.get(capacity.physician_id, 0) * capacity_total
                - capacity.late_doc_capacity * (existing_total + selected_count)
            )
            capacity_node = capacity_nodes[capacity.physician_id]
            for assigned_count in range(1, selected_count + 1):
                deviation = abs(
                    (current.get(capacity.physician_id, 0) + assigned_count) * capacity_total
                    - capacity.late_doc_capacity * (existing_total + selected_count)
                )
                flow.add_arc_with_capacity_and_unit_cost(
                    capacity_node, sink, 1, deviation - previous_deviation
                )
                previous_deviation = deviation
        flow.set_node_supply(source, selected_count)
        flow.set_node_supply(sink, -selected_count)
        if flow.solve() != flow.OPTIMAL:
            raise ValidationError(
                "The Late Doc balance policy cannot satisfy the earlier scheduling policies."
            )

        selected_choices = {
            variable.Index(): candidate
            for arc, (variable, candidate) in selected_arcs.items()
            if flow.flow(arc)
        }
        totals = dict(current)
        for candidate in selected_choices.values():
            totals[candidate.physician_id] = totals.get(candidate.physician_id, 0) + 1
        retained_values.update(
            {
                variable.Index(): int(variable.Index() in selected_choices)
                for variable in late_variables
            }
        )
        retained_values.update(
            {
                deviation.Index(): abs(
                    totals.get(capacity.physician_id, 0) * capacity_total
                    - capacity.late_doc_capacity * (existing_total + selected_count)
                )
                for capacity, deviation in zip(capacities, deviations, strict=True)
            }
        )
        self.retained_solution_hint = tuple(
            (variable, retained_values.get(variable.Index(), 0))
            for index in range(len(self.model.Proto().variables))
            for variable in (self.model.GetIntVarFromProtoIndex(index),)
        )
        return totals

    def _late_doc_candidates_with_fixed_policy_outcome(self, requirement, retained_values):
        """Return choices that preserve this Late Doc's independent prefix.

        Late Docs are independent before the balance stage, so the retained
        prefix supplies one valid local outcome for each Monday.  The flow
        must not exchange that outcome for a better balance by using a
        backup-only or soft-event candidate, or by replacing a retained Repair
        assignment.  Comparing the component metrics keeps every policy-equal
        candidate available to the flow while preserving forced and excluded
        comparison choices.
        """
        forced_ids = {
            physician_id
            for on_date, period, position, physician_id in self.forced_choices
            if (on_date, period, position)
            == (requirement.date, requirement.period_of_day, requirement.position)
        }
        excluded_ids = {
            physician_id
            for on_date, period, position, physician_id in self.excluded_choices
            if (on_date, period, position)
            == (requirement.date, requirement.period_of_day, requirement.position)
        }
        choices = tuple(
            candidate
            for (item, candidate), _ in self.variables.items()
            if item == requirement
            and candidate.physician_id not in excluded_ids
            and (not forced_ids or candidate.physician_id in forced_ids)
        )
        retained = next(
            (
                candidate
                for (item, candidate), variable in self.variables.items()
                if item == requirement and retained_values.get(variable.Index(), 0)
            ),
            None,
        )
        if retained is None:
            return ()
        target_metrics = self._component_metrics((requirement,), {requirement: retained})
        return tuple(
            candidate
            for candidate in choices
            if self._component_metrics((requirement,), {requirement: candidate}) == target_metrics
        )

    def _maximize_late_doc_overlap_with_retained_pm(self, late_variables, deviations, best_totals):
        """Choose the most PM-aligned Late Docs within the accepted balance band."""
        retained_values = {
            variable.Index(): value for variable, value in self.retained_solution_hint
        }
        capacities = self.solver_input.faculty_capacities
        current = _assignment_counts(self.solver_input.balance_late_doc_assignments, capacities)
        requirements = tuple(
            requirement for requirement in self.requirements if requirement.is_late_doc
        )
        selected_count = sum(
            retained_values.get(variable.Index(), 0) for variable in late_variables
        )
        flow = min_cost_flow.SimpleMinCostFlow()
        source = 0
        sink = 1
        requirement_nodes = {
            requirement: index for index, requirement in enumerate(requirements, start=2)
        }
        capacity_nodes = {
            capacity.physician_id: index
            for index, capacity in enumerate(capacities, start=2 + len(requirements))
        }
        selected_arcs = {}
        for requirement, node in requirement_nodes.items():
            flow.add_arc_with_capacity_and_unit_cost(source, node, 1, 0)
            allowed_candidates = self._late_doc_candidates_with_fixed_policy_outcome(
                requirement, retained_values
            )
            for (item, candidate), variable in self.variables.items():
                if item != requirement or candidate not in allowed_candidates:
                    continue
                selected_arcs[
                    flow.add_arc_with_capacity_and_unit_cost(
                        node,
                        capacity_nodes[candidate.physician_id],
                        1,
                        -int(
                            self._late_doc_matches_retained_pm(
                                requirement, candidate, retained_values
                            )
                        ),
                    )
                ] = (variable, candidate)
        lower_reward = selected_count + 1
        for capacity in capacities:
            lower = max(
                0, best_totals[capacity.physician_id] - 1 - current.get(capacity.physician_id, 0)
            )
            upper = max(
                0, best_totals[capacity.physician_id] + 1 - current.get(capacity.physician_id, 0)
            )
            for proposed_count in range(1, upper + 1):
                flow.add_arc_with_capacity_and_unit_cost(
                    capacity_nodes[capacity.physician_id],
                    sink,
                    1,
                    -lower_reward if proposed_count <= lower else 0,
                )
        flow.set_node_supply(source, selected_count)
        flow.set_node_supply(sink, -selected_count)
        if flow.solve() != flow.OPTIMAL:
            raise ValidationError(
                "The Late Doc overlap policy cannot satisfy the accepted balance band."
            )

        selected_choices = {
            variable.Index(): candidate
            for arc, (variable, candidate) in selected_arcs.items()
            if flow.flow(arc)
        }
        totals = dict(current)
        for candidate in selected_choices.values():
            totals[candidate.physician_id] = totals.get(candidate.physician_id, 0) + 1
        capacity_total = sum(capacity.late_doc_capacity for capacity in capacities)
        retained_values.update(
            {
                variable.Index(): int(variable.Index() in selected_choices)
                for variable in late_variables
            }
        )
        retained_values.update(
            {
                deviation.Index(): abs(
                    totals.get(capacity.physician_id, 0) * capacity_total
                    - capacity.late_doc_capacity * (sum(current.values()) + selected_count)
                )
                for capacity, deviation in zip(capacities, deviations, strict=True)
            }
        )
        self.retained_solution_hint = tuple(
            (variable, retained_values.get(variable.Index(), 0))
            for index in range(len(self.model.Proto().variables))
            for variable in (self.model.GetIntVarFromProtoIndex(index),)
        )

    def _late_doc_matches_retained_pm(self, late_requirement, candidate, retained_values):
        pm_session = next(
            (
                session
                for session in self.solver_input.sessions
                if session.date == late_requirement.date and session.period_of_day == "PM"
            ),
            None,
        )
        if pm_session is None:
            return False
        return any(
            position.assignment and position.assignment.physician_id == candidate.physician_id
            for position in pm_session.positions
        ) or any(
            requirement.session_id == pm_session.session_id
            and pm_candidate.physician_id == candidate.physician_id
            and retained_values.get(variable.Index(), 0)
            for (requirement, pm_candidate), variable in self.variables.items()
        )

    def _maximize_reduced_late_doc_overlap(self, overlaps):
        """Prove the Late Doc/Monday-PM overlap optimum without reopening Fill.

        Faculty fairness and the Late Doc balance band can jointly make the
        per-Monday overlap ceiling unattainable.  The retained hint is useful
        as a feasible starting point, but it cannot prove that lower outcome.
        Build the proof from only the decisions this policy is allowed to
        exchange: Monday PM and Late Doc choices.  All other selected Fill
        choices remain constants through the prior policy outcomes.
        """
        retained_values = {
            variable.Index(): value for variable, value in self.retained_solution_hint
        }
        movable_requirements = frozenset(
            requirement
            for requirement in self.requirements
            if requirement.is_late_doc
            or (requirement.period_of_day == "PM" and requirement.date.weekday() == 0)
        )
        reduced_model = cp_model.CpModel()
        reduced_variables = {
            (requirement, candidate): reduced_model.NewBoolVar(f"reduced_{variable.Index()}")
            for (requirement, candidate), variable in self.variables.items()
            if requirement in movable_requirements
        }
        self._add_reduced_choice_constraints(
            reduced_model, movable_requirements, reduced_variables, retained_values
        )
        self._add_reduced_faculty_fairness_constraint(
            reduced_model, reduced_variables, movable_requirements, retained_values
        )
        self._add_reduced_late_doc_band_constraints(reduced_model, reduced_variables)
        reduced_overlaps = self._reduced_late_doc_overlap_variables(
            reduced_model, reduced_variables
        )
        reduced_model.Maximize(sum(reduced_overlaps))
        solver = self._solver()
        solver.parameters.cp_model_presolve = True
        status = solver.Solve(reduced_model)
        if status != cp_model.OPTIMAL:
            if self.solve_deadline is not None and status in (cp_model.FEASIBLE, cp_model.UNKNOWN):
                self._raise_timeout()
            raise ValidationError(
                "The reduced Late Doc overlap policy could not prove its optimal outcome."
            )
        self._check_deadline()
        retained_values.update(
            {
                self.variables[requirement, candidate].Index(): solver.Value(variable)
                for (requirement, candidate), variable in reduced_variables.items()
            }
        )
        retained_values.update(
            {
                overlap.Index(): int(
                    retained_values.get(late_variable.Index(), 0)
                    and retained_values.get(precepting_variable.Index(), 0)
                )
                for overlap, (late_variable, precepting_variable) in self.overlap_sources.items()
            }
        )
        self.retained_solution_hint = tuple(
            (variable, retained_values.get(variable.Index(), 0))
            for index in range(len(self.model.Proto().variables))
            for variable in (self.model.GetIntVarFromProtoIndex(index),)
        )
        return sum(retained_values.get(variable.Index(), 0) for variable in overlaps)

    def _add_reduced_choice_constraints(self, model, requirements, variables, retained_values):
        requirements_by_session = {}
        for requirement in requirements:
            choices = [variable for (item, _), variable in variables.items() if item == requirement]
            model.Add(sum(choices) <= 1)
            if requirement.session_id is not None:
                requirements_by_session.setdefault(requirement.session_id, []).append(requirement)

        for session_id, session_requirements in requirements_by_session.items():
            for physician_id in {
                candidate.physician_id
                for requirement, candidate in variables
                if requirement.session_id == session_id
            }:
                model.Add(
                    sum(
                        variable
                        for (requirement, candidate), variable in variables.items()
                        if requirement.session_id == session_id
                        and candidate.physician_id == physician_id
                    )
                    <= 1
                )
            session = next(
                item for item in self.solver_input.sessions if item.session_id == session_id
            )
            existing_faculty_equivalent = self._session_existing_faculty_equivalent(session)
            selected = self._retained_component_selection(session_requirements, retained_values)
            metrics = self._component_metrics(
                session_requirements,
                selected,
                existing_faculty_equivalent=existing_faculty_equivalent,
            )
            self._add_reduced_component_metrics(
                model,
                session_requirements,
                variables,
                metrics,
                existing_faculty_equivalent=existing_faculty_equivalent,
            )

        for requirement in requirements:
            if not requirement.is_late_doc:
                continue
            self._add_reduced_component_metrics(
                model,
                (requirement,),
                variables,
                self._component_metrics(
                    (requirement,),
                    self._retained_component_selection((requirement,), retained_values),
                ),
            )

        forced = set(self.forced_choices)
        excluded = set(self.excluded_choices)
        for (requirement, candidate), variable in variables.items():
            choice = (
                requirement.date,
                requirement.period_of_day,
                requirement.position,
                candidate.physician_id,
            )
            if choice in forced:
                model.Add(variable == 1)
            elif choice in excluded:
                model.Add(variable == 0)

    def _retained_component_selection(self, requirements, retained_values):
        return {
            requirement: candidate
            for requirement in requirements
            for (item, candidate), variable in self.variables.items()
            if item == requirement and retained_values.get(variable.Index(), 0)
        }

    @staticmethod
    def _session_existing_faculty_equivalent(session):
        candidates_by_id = {candidate.physician_id: candidate for candidate in session.candidates}
        return sum(
            candidates_by_id[position.assignment.physician_id].faculty_equivalent
            for position in session.positions
            if position.assignment is not None
            and position.assignment.physician_id in candidates_by_id
        )

    def _add_reduced_component_metrics(
        self, model, requirements, variables, metrics, *, existing_faculty_equivalent=0
    ):
        def selected(predicate):
            return [
                variable
                for (requirement, candidate), variable in variables.items()
                if requirement in requirements and predicate(requirement, candidate)
            ]

        model.Add(sum(selected(lambda requirement, _: requirement.is_primary)) == metrics[0])
        faculty_equivalent = existing_faculty_equivalent + sum(
            selected(lambda _, candidate: candidate.faculty_equivalent)
        )
        if metrics[1]:
            model.Add(faculty_equivalent >= 2)
        else:
            model.Add(faculty_equivalent <= 1)
        model.Add(sum(selected(lambda requirement, _: requirement.is_late_doc)) == metrics[2])
        if self.repair:
            changes = []
            severity = []
            for requirement in requirements:
                choices = [
                    (candidate, variable)
                    for (item, candidate), variable in variables.items()
                    if item == requirement
                ]
                baseline = self.baseline.get(
                    (requirement.date, requirement.period_of_day, requirement.position)
                )
                if baseline is not None:
                    retained = next(
                        (
                            variable
                            for candidate, variable in choices
                            if candidate.physician_id == baseline.physician_id
                        ),
                        None,
                    )
                    changes.append(1 - retained if retained is not None else 1)
                    severity.append(1 - sum(variable for _, variable in choices))
                else:
                    changes.extend(variable for _, variable in choices)
                severity.extend(
                    variable
                    for candidate, variable in choices
                    if candidate.availability == "backup_only"
                    and (baseline is None or candidate.physician_id != baseline.physician_id)
                )
            model.Add(sum(changes) == -metrics[3])
            model.Add(sum(severity) == -metrics[4])
        model.Add(
            sum(selected(lambda _, candidate: candidate.availability == "backup_only"))
            == -metrics[5]
        )
        model.Add(
            sum(selected(lambda _, candidate: candidate.has_soft_academic_event)) == -metrics[6]
        )
        model.Add(
            sum(
                selected(
                    lambda requirement, candidate: (
                        requirement.is_primary and candidate.expected_community_commitment
                    )
                )
            )
            == metrics[7]
        )
        model.Add(
            sum(
                selected(
                    lambda requirement, candidate: (
                        requirement.is_primary and candidate.faculty_equivalent
                    )
                )
            )
            == metrics[8]
        )
        model.Add(
            sum(selected(lambda requirement, _: requirement.position == "secondary")) == -metrics[9]
        )
        model.Add(
            sum(
                selected(
                    lambda requirement, candidate: (
                        requirement.position == "primary-1"
                        and candidate.expected_community_commitment
                    )
                )
            )
            == metrics[10]
        )
        model.Add(
            sum(
                _community_first_sort_value(requirement, candidate) * variable
                for (requirement, candidate), variable in variables.items()
                if requirement in requirements
            )
            == metrics[11]
        )

    def _add_reduced_faculty_fairness_constraint(
        self, model, variables, movable_requirements, retained_values
    ):
        capacities = self.solver_input.faculty_capacities
        capacity_total = sum(_precepting_capacity_units(capacity) for capacity in capacities)
        if not capacity_total:
            return
        current = _assignment_counts(self.solver_input.balance_assignments, capacities)
        fixed = [
            (requirement, candidate)
            for (requirement, candidate), variable in self.variables.items()
            if requirement not in movable_requirements
            and not requirement.is_late_doc
            and candidate.is_faculty
            and retained_values.get(variable.Index(), 0)
        ]
        total_assignments = (
            sum(current.values())
            + len(fixed)
            + sum(
                variable
                for (requirement, candidate), variable in variables.items()
                if not requirement.is_late_doc and candidate.is_faculty
            )
        )
        maximum_deviation = capacity_total * (sum(current.values()) + len(fixed) + len(variables))
        deviations = []
        for capacity in capacities:
            physician_total = (
                current.get(capacity.physician_id, 0)
                + sum(candidate.physician_id == capacity.physician_id for _, candidate in fixed)
                + sum(
                    variable
                    for (requirement, candidate), variable in variables.items()
                    if not requirement.is_late_doc
                    and candidate.is_faculty
                    and candidate.physician_id == capacity.physician_id
                )
            )
            deviation = model.NewIntVar(
                0, maximum_deviation, f"reduced_faculty_workload_{capacity.physician_id}"
            )
            model.AddAbsEquality(
                deviation,
                physician_total * capacity_total
                - _precepting_capacity_units(capacity) * total_assignments,
            )
            deviations.append(deviation)
        target = sum(
            value
            for variable, value in self.retained_solution_hint
            if variable.Name().startswith("faculty_workload_")
        )
        model.Add(sum(deviations) == target)

    def _add_reduced_late_doc_band_constraints(self, model, variables):
        if not self.late_doc_band_totals:
            return
        current = _assignment_counts(
            self.solver_input.balance_late_doc_assignments, self.solver_input.faculty_capacities
        )
        for physician_id, best_total in self.late_doc_band_totals.items():
            total = current.get(physician_id, 0) + sum(
                variable
                for (requirement, candidate), variable in variables.items()
                if requirement.is_late_doc and candidate.physician_id == physician_id
            )
            model.Add(total >= max(0, best_total - 1))
            model.Add(total <= best_total + 1)

    def _reduced_late_doc_overlap_variables(self, model, variables):
        overlaps = []
        pm_sessions = {
            (session.date, session.period_of_day): session
            for session in self.solver_input.sessions
            if session.period_of_day == "PM" and session.date.weekday() == 0
        }
        for (late_requirement, candidate), late_variable in variables.items():
            if not late_requirement.is_late_doc:
                continue
            pm_session = pm_sessions.get((late_requirement.date, "PM"))
            if pm_session is None:
                continue
            if any(
                position.assignment and position.assignment.physician_id == candidate.physician_id
                for position in pm_session.positions
            ):
                overlaps.append(late_variable)
                continue
            for (requirement, pm_candidate), precepting_variable in variables.items():
                if (
                    requirement.session_id == pm_session.session_id
                    and pm_candidate.physician_id == candidate.physician_id
                ):
                    overlap = model.NewBoolVar(
                        f"reduced_late_doc_overlap_{late_requirement.date.isoformat()}_"
                        f"{candidate.physician_id}"
                    )
                    model.AddBoolAnd((late_variable, precepting_variable)).OnlyEnforceIf(overlap)
                    model.AddBoolOr((late_variable.Not(), precepting_variable.Not())).OnlyEnforceIf(
                        overlap.Not()
                    )
                    overlaps.append(overlap)
        return overlaps

    def _late_doc_overlap_upper_bound(self, overlaps):
        """Return the independent per-Monday upper bound for Late Doc overlap.

        Several candidate-pair variables and Late Doc candidate variables can
        describe one Monday, but the Late Doc requirement permits only one of
        them.  Counting the distinct Late Doc requirements, rather than the
        candidate variables, is therefore a valid model-wide bound, including
        when a Monday has no possible PM counterpart at all.
        """
        late_variable_indexes = {
            self.overlap_sources.get(overlap, (overlap,))[0].Index() for overlap in overlaps
        }
        return len(
            {
                requirement
                for (requirement, _), variable in self.variables.items()
                if requirement.is_late_doc and variable.Index() in late_variable_indexes
            }
        )

    def _constrain_late_doc_balance(self, target_imbalance, baseline_totals):
        capacities = self.solver_input.faculty_capacities
        capacity_total = sum(capacity.late_doc_capacity for capacity in capacities)
        late_variables = [
            variable
            for (requirement, _), variable in self.variables.items()
            if requirement.is_late_doc
        ]
        if not capacity_total or not late_variables:
            return
        current = _assignment_counts(self.solver_input.balance_late_doc_assignments, capacities)
        existing_total = sum(current.values())
        total_assignments = existing_total + sum(late_variables)
        maximum_deviation = capacity_total * (existing_total + len(late_variables))
        deviations = []
        for capacity in capacities:
            physician_total = current.get(capacity.physician_id, 0) + sum(
                variable
                for (requirement, candidate), variable in self.variables.items()
                if requirement.is_late_doc and candidate.physician_id == capacity.physician_id
            )
            deviation = self.model.NewIntVar(
                0, maximum_deviation, f"comparison_late_doc_workload_{capacity.physician_id}"
            )
            self.model.AddAbsEquality(
                deviation,
                physician_total * capacity_total - capacity.late_doc_capacity * total_assignments,
            )
            self.model.Add(physician_total >= max(0, baseline_totals[capacity.physician_id] - 1))
            self.model.Add(physician_total <= baseline_totals[capacity.physician_id] + 1)
            deviations.append(deviation)
        self.model.Add(sum(deviations) == target_imbalance)

    def _late_doc_overlap_variables(self):
        overlaps = []
        for (late_requirement, candidate), late_variable in self.variables.items():
            if not late_requirement.is_late_doc:
                continue
            pm_session = next(
                (
                    session
                    for session in self.solver_input.sessions
                    if session.date == late_requirement.date and session.period_of_day == "PM"
                ),
                None,
            )
            if pm_session is None:
                continue
            if any(
                position.assignment and position.assignment.physician_id == candidate.physician_id
                for position in pm_session.positions
            ):
                overlaps.append(late_variable)
                continue
            for (requirement, pm_candidate), precepting_variable in self.variables.items():
                if (
                    requirement.session_id == pm_session.session_id
                    and pm_candidate.physician_id == candidate.physician_id
                ):
                    overlap = self.model.NewBoolVar(
                        f"late_doc_overlap_{late_requirement.date.isoformat()}_{candidate.physician_id}"
                    )
                    self.model.AddBoolAnd((late_variable, precepting_variable)).OnlyEnforceIf(
                        overlap
                    )
                    self.model.AddBoolOr(
                        (late_variable.Not(), precepting_variable.Not())
                    ).OnlyEnforceIf(overlap.Not())
                    self.overlap_sources[overlap] = (late_variable, precepting_variable)
                    overlaps.append(overlap)
        return overlaps

    def _requirements(self):
        requirements = []
        for session in self.solver_input.sessions:
            if session.session_id is None:
                continue
            for position in session.positions:
                if position.assignment is not None:
                    continue
                if not position.required and position.key != "secondary":
                    continue
                source_candidates = position.candidates or session.candidates
                candidates = tuple(
                    sorted(
                        (
                            candidate
                            for candidate in source_candidates
                            if _eligible_for_preceptor_position(
                                candidate,
                                position,
                                movable_assignment_ids=self.movable_assignment_ids,
                                excluded_physician_ids=self.solver_input.excluded_physician_ids,
                            )
                        ),
                        key=lambda candidate: candidate.physician_id,
                    )
                )
                requirements.append(
                    _FillRequirement(
                        session.date,
                        session.period_of_day,
                        session.session_id,
                        position.key,
                        candidates,
                        source_candidates,
                        self.solver_input.excluded_physician_ids,
                    )
                )
        for position in self.solver_input.late_doc_positions:
            if position.assignment is not None:
                continue
            requirements.append(
                _FillRequirement(
                    position.date,
                    None,
                    None,
                    "late-doc",
                    tuple(
                        sorted(
                            (
                                candidate
                                for candidate in position.candidates
                                if _eligible_for_late_doc(
                                    candidate,
                                    excluded_physician_ids=self.solver_input.excluded_physician_ids,
                                )
                            ),
                            key=lambda candidate: candidate.physician_id,
                        )
                    ),
                    position.all_candidates,
                    self.solver_input.excluded_physician_ids,
                )
            )
        return tuple(sorted(requirements, key=_fill_requirement_sort_key))

    def _add_variables_and_session_constraints(self):
        variables_by_session_and_physician = {}
        for requirement in self.requirements:
            for candidate in requirement.candidates:
                variable = self.model.NewBoolVar(
                    f"{requirement.date.isoformat()}_{requirement.period_of_day}_{requirement.position}_{candidate.physician_id}"
                )
                self.variables[requirement, candidate] = variable
                if not requirement.is_late_doc:
                    variables_by_session_and_physician.setdefault(
                        (requirement.session_id, candidate.physician_id), []
                    ).append(variable)
            requirement_variables = [
                variable for (item, _), variable in self.variables.items() if item == requirement
            ]
            if requirement_variables:
                self.model.Add(sum(requirement_variables) <= 1)
        for variables in variables_by_session_and_physician.values():
            self.model.Add(sum(variables) <= 1)
        for session in self.solver_input.sessions:
            if session.session_id is None:
                continue
            candidates_by_id = {
                candidate.physician_id: candidate for candidate in session.candidates
            }
            existing_faculty_equivalent_count = sum(
                candidates_by_id.get(position.assignment.physician_id).faculty_equivalent
                for position in session.positions
                if position.assignment is not None
                and position.assignment.physician_id in candidates_by_id
            )
            proposed_faculty_equivalent = [
                variable
                for (requirement, candidate), variable in self.variables.items()
                if requirement.session_id == session.session_id and candidate.faculty_equivalent
            ]
            coverage = self.model.NewBoolVar(f"{session.session_id}_faculty_equivalent_coverage")
            faculty_equivalent_count = (
                sum(proposed_faculty_equivalent) + existing_faculty_equivalent_count
            )
            self.model.Add(faculty_equivalent_count >= 2).OnlyEnforceIf(coverage)
            self.model.Add(faculty_equivalent_count <= 1).OnlyEnforceIf(coverage.Not())
            self.coverage_variables[session.session_id] = coverage

    def _maximize_then_fix(self, variables, *, hint_search=False):
        variables = tuple(variables)
        if not variables:
            return
        self.model.Maximize(sum(variables))
        self._apply_retained_solution_hint()
        solver = self._solver(hint_search=hint_search)
        status = self._solve_model(solver)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return
        self._retain_seeded_tie_break_hint(solver)
        self.model.Add(sum(variables) == sum(solver.Value(variable) for variable in variables))

    def _minimize_then_fix(
        self,
        variables,
        *,
        retain_solution=False,
        fix=True,
        fairness_stage=False,
        seeded_tie_break_stage=False,
    ):
        variables = tuple(variables)
        if not variables:
            return None
        self.model.Minimize(sum(variables))
        self._apply_retained_solution_hint()
        solver = self._solver(
            fairness_stage=fairness_stage, seeded_tie_break_stage=seeded_tie_break_stage
        )
        status = self._solve_model(
            solver,
            accept_feasible=fairness_stage or seeded_tie_break_stage,
        )
        stage = (
            ("faculty_fairness", "proportional Faculty workload fairness")
            if fairness_stage
            else ("seeded_tie_break", "final seeded tie-break")
            if seeded_tie_break_stage
            else None
        )
        if stage:
            outcome, message = _solver_stage_outcome(stage[1], status)
            logger.info(
                "schedule_bot_solver_stage stage=%s outcome=%s detail=%s",
                stage[0],
                outcome,
                message,
            )
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            if stage:
                raise ValidationError(message)
            return None
        if not seeded_tie_break_stage:
            self._retain_seeded_tie_break_hint(solver)
        if fix:
            self.model.Add(sum(variables) == sum(solver.Value(variable) for variable in variables))
        return solver if retain_solution else None

    def _retain_seeded_tie_break_hint(self, solver):
        self.retained_solution_hint = tuple(
            (variable, solver.Value(variable))
            for index in range(len(self.model.Proto().variables))
            for variable in (self.model.GetIntVarFromProtoIndex(index),)
        )

    def _apply_retained_solution_hint(self):
        """Start each policy stage from the preceding stage's complete valid solution."""
        self.model.ClearHints()
        for variable, value in self.retained_solution_hint:
            self.model.AddHint(variable, value)

    def _resolve_seeded_ties(self):
        """Break only already-fixed policy ties in one final whole-range optimization."""
        ranked = sorted(
            self.variables.items(),
            key=lambda item: _seeded_choice_key(self.solve_seed, *item[0]),
        )
        return self._minimize_then_fix(
            ((index + 1) * variable for index, (_, variable) in enumerate(ranked)),
            retain_solution=True,
            fix=False,
            seeded_tie_break_stage=True,
        )

    def _unresolved(self, selected):
        unresolved = []
        requirements_by_session = {}
        for requirement in self.requirements:
            if requirement.session_id is not None:
                requirements_by_session.setdefault(requirement.session_id, []).append(requirement)
            if requirement.is_primary and requirement not in selected:
                unresolved.append(_unresolved_primary(requirement))
            elif requirement.is_late_doc and requirement not in selected:
                unresolved.append(_unresolved_late_doc(requirement))
        for session in self.solver_input.sessions:
            if session.session_id is None:
                continue
            required_positions_are_filled = all(
                position.assignment is not None
                or any(
                    requirement.position == position.key and requirement in selected
                    for requirement in requirements_by_session.get(session.session_id, ())
                )
                for position in session.positions
                if position.required
            )
            if not required_positions_are_filled:
                continue
            assignments = [
                position.assignment for position in session.positions if position.assignment
            ]
            candidates_by_id = {
                candidate.physician_id: candidate for candidate in session.candidates
            }
            faculty_equivalent_count = sum(
                candidates_by_id.get(assignment.physician_id).faculty_equivalent
                for assignment in assignments
                if assignment.physician_id in candidates_by_id
            ) + sum(
                candidate.faculty_equivalent
                for requirement in requirements_by_session.get(session.session_id, ())
                for candidate in (selected.get(requirement),)
                if candidate is not None
            )
            if faculty_equivalent_count < 2:
                unresolved.append(
                    FillProposalUnresolvedRequirement(
                        session.date,
                        session.period_of_day,
                        "faculty-equivalent-coverage",
                        "insufficient_faculty_equivalent_coverage",
                        "Fewer than two assigned physicians are faculty-equivalent.",
                        ("faculty_equivalent_coverage",),
                    )
                )
        return tuple(sorted(unresolved, key=_unresolved_sort_key))

    def _solver(self, *, fairness_stage=False, seeded_tie_break_stage=False, hint_search=False):
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = (
            FACULTY_WORKLOAD_FAIRNESS_WORKERS if fairness_stage or hint_search else 1
        )
        # The model already contains the complete scheduling constraints and is
        # solved repeatedly while lexicographic outcomes are fixed. Native
        # presolve repeats costly whole-range simplification at every stage and
        # scales poorly on interchangeable assignment lanes.
        solver.parameters.cp_model_presolve = False
        if fairness_stage or seeded_tie_break_stage:
            solver.parameters.search_branching = cp_model.HINT_SEARCH
        solver.parameters.random_seed = _seeded_integer(self.solve_seed, "ortools")
        time_limit = (
            FACULTY_WORKLOAD_FAIRNESS_TIME_SECONDS
            if fairness_stage
            else FINAL_SEEDED_TIE_BREAK_TIME_SECONDS
            if seeded_tie_break_stage
            else None
        )
        if self.solve_deadline is not None:
            remaining_seconds = self.solve_deadline - perf_counter()
            if remaining_seconds <= 0:
                self._raise_timeout()
            time_limit = (
                min(time_limit, remaining_seconds) if time_limit is not None else remaining_seconds
            )
        if time_limit is not None:
            solver.parameters.max_time_in_seconds = time_limit
        return solver

    def _solve_model(self, solver, *, accept_feasible=False):
        status = solver.Solve(self.model)
        if (
            self.solve_deadline is not None
            and status in (cp_model.FEASIBLE, cp_model.UNKNOWN)
            and not (accept_feasible and status == cp_model.FEASIBLE)
        ):
            self._raise_timeout()
        return status


def _solver_stage_outcome(stage, status):
    """Describe a native solve status in scheduler-facing policy language."""
    if status == cp_model.OPTIMAL:
        return "best", f"Schedule Bot proved the best {stage} outcome."
    if status == cp_model.FEASIBLE:
        return "valid", f"Schedule Bot found a valid {stage} outcome within its search time."
    if status == cp_model.INFEASIBLE:
        return (
            "infeasible",
            f"Schedule Bot proved that no {stage} outcome satisfies the earlier scheduling policies.",
        )
    if status == cp_model.UNKNOWN:
        return (
            "time_limit",
            f"Schedule Bot reached the {stage} search limit before finding a valid outcome.",
        )
    return (
        "invalid",
        f"Schedule Bot could not evaluate the {stage} outcome because its scheduling inputs "
        "produced an invalid model.",
    )


def _eligible_for_preceptor_position(
    candidate, position, *, movable_assignment_ids=(), excluded_physician_ids=()
):
    if candidate.physician_id in excluded_physician_ids:
        return False
    if candidate.manual_float_eligible:
        return False
    if candidate.reasons:
        return not any(
            reason.severity == "hard"
            and not (
                reason.code == "same_session_occupancy"
                and candidate.preceptor_assignment_id in movable_assignment_ids
            )
            for reason in candidate.reasons
        )
    return (
        candidate.ordinarily_eligible_to_precept
        and candidate.availability in ("available", "backup_only")
        and not candidate.precepting_conflict_codes
        and (
            candidate.preceptor_assignment_id is None
            or candidate.preceptor_assignment_id in movable_assignment_ids
        )
        and (position.key != "secondary" or candidate.is_faculty)
    )


def _eligible_for_late_doc(candidate, *, excluded_physician_ids=()):
    return (
        candidate.physician_id not in excluded_physician_ids
        and candidate.is_faculty
        and not candidate.manual_float_eligible
        and candidate.availability in ("available", "backup_only")
    )


def _seeded_integer(seed, value):
    return (
        int.from_bytes(hashlib.sha256(f"{seed}:{value}".encode()).digest()[:4], "big")
        % 2_147_483_647
    )


def _community_first_sort_value(requirement, candidate):
    """Prefer the alphabetically first expected Community Preceptor in primary 1."""
    if requirement.position != "primary-1" or not candidate.expected_community_commitment:
        return 0
    ordered = sorted(
        (item for item in requirement.candidates if item.expected_community_commitment),
        key=lambda item: (item.physician_name.casefold(), item.physician_name, item.physician_id),
    )
    return len(ordered) - ordered.index(candidate)


def _seeded_choice_key(seed, requirement, candidate):
    return (
        _seeded_integer(
            seed,
            f"{requirement.date.isoformat()}:{requirement.period_of_day}:{requirement.position}:"
            f"{candidate.physician_id}",
        ),
        requirement.date,
        requirement.period_of_day or "",
        requirement.position,
        candidate.physician_id,
    )


def _candidate_sort_key(candidate, solve_seed):
    return (
        candidate.availability != "available",
        candidate.has_soft_academic_event,
        _seeded_integer(solve_seed, candidate.physician_id),
    )


def _assignment_counts(assignments, capacities):
    faculty_ids = {capacity.physician_id for capacity in capacities}
    counts = {physician_id: 0 for physician_id in faculty_ids}
    for assignment in assignments:
        if assignment.is_faculty and assignment.physician_id in faculty_ids:
            counts[assignment.physician_id] += 1
    return counts


def _precepting_capacity_units(capacity):
    """Return a fixed-point workload coefficient; legacy test inputs have no unit field."""
    if capacity.precepting_capacity_units is not None:
        return capacity.precepting_capacity_units
    return int(Decimal(str(capacity.precepting_capacity)) * 100)


def _explain_choice(
    requirement,
    candidate,
    selected,
    solver_input,
    policy_objectives,
    late_doc_band_totals,
    solve_seed,
):
    exclusions = tuple(
        FillAssignmentExclusion(
            other.physician_id,
            other.physician_name,
            _ineligibility_reasons(other, requirement),
        )
        for other in sorted(
            requirement.all_candidates,
            key=lambda item: (item.physician_name.casefold(), item.physician_id),
        )
        if other != candidate and _ineligibility_reasons(other, requirement)
    )
    eligible_others = tuple(
        sorted(
            (
                other
                for other in requirement.candidates
                if other != candidate and other.availability == "available"
            ),
            key=lambda item: _candidate_sort_key(item, solve_seed),
        )
    )
    advantages = []
    disadvantages = []
    if requirement.is_primary and candidate.expected_community_commitment:
        advantages.append("Fulfills the expected Community Preceptor commitment.")
        if requirement.position == "primary-1":
            advantages.append("Places a Community Preceptor in the first primary slot.")
    workload_effect = _faculty_workload_policy_effect(
        requirement, candidate, selected, solver_input
    )
    if workload_effect is not None:
        effect, message = workload_effect
        if effect == "disadvantage":
            disadvantages.append(message)
        else:
            advantages.append(message)
    if candidate.availability == "backup_only":
        disadvantages.append("Uses a backup-only Faculty fallback.")
    if candidate.has_soft_academic_event:
        disadvantages.append("Uses a Faculty member with a soft Academic Event.")
    if requirement.is_late_doc:
        if _has_monday_pm_precepting_overlap(requirement, candidate, selected, solver_input):
            advantages.append("Keeps Late Doc with Monday PM Precepting.")
        else:
            disadvantages.append("Does not overlap Monday PM Precepting.")
        advantages.append("Follows the separate Late Doc balance policy.")
    equal = tuple(
        FillAssignmentAlternative(
            other.physician_id,
            other.physician_name,
            "Produces the same complete named policy outcome.",
        )
        for other in eligible_others
        if _same_policy_profile(candidate, other, solver_input)
    )
    equal_ids = {alternative.physician_id for alternative in equal}
    other_normally_available = tuple(
        FillAssignmentAlternative(
            other.physician_id,
            other.physician_name,
            "Normally available; the whole proposal may have different policy tradeoffs.",
        )
        for other in eligible_others
        if other.physician_id not in equal_ids
    )
    backup = tuple(
        FillAssignmentAlternative(
            other.physician_id,
            other.physician_name,
            "Eligible only as a backup-only fallback.",
        )
        for other in sorted(
            requirement.candidates, key=lambda item: _candidate_sort_key(item, solve_seed)
        )
        if other != candidate and other.availability == "backup_only"
    )
    tie_break = None
    if equal:
        tie_break = (
            "Equal-policy candidates were ordered by this proposal's recorded seeded tie-break."
        )
    return FillAssignmentExplanation(
        hard_exclusions=exclusions,
        policy_advantages=tuple(advantages),
        policy_disadvantages=tuple(disadvantages),
        equally_good_alternatives=equal,
        other_normally_available_alternatives=other_normally_available,
        backup_only_alternatives=backup,
        stable_tie_break=tie_break,
    )


def _faculty_workload_policy_effect(requirement, candidate, selected, solver_input):
    if requirement.is_late_doc or not candidate.is_faculty:
        return None
    capacities = solver_input.faculty_capacities
    capacity_by_id = {capacity.physician_id: capacity for capacity in capacities}
    capacity = capacity_by_id.get(candidate.physician_id)
    capacity_total = sum(_precepting_capacity_units(item) for item in capacities)
    if capacity is None or not capacity_total:
        return None
    current = _assignment_counts(solver_input.balance_assignments, capacities)
    additions = {item.physician_id: 0 for item in capacities}
    for other_requirement, other_candidate in selected.items():
        if not other_requirement.is_late_doc and other_candidate.is_faculty:
            additions[other_candidate.physician_id] = (
                additions.get(other_candidate.physician_id, 0) + 1
            )
    proposed_total = sum(current.values()) + sum(additions.values())
    imbalance_with_choice = _faculty_workload_imbalance(
        capacities, current, additions, proposed_total, capacity_total
    )
    additions[candidate.physician_id] -= 1
    imbalance_without_choice = _faculty_workload_imbalance(
        capacities, current, additions, proposed_total - 1, capacity_total
    )
    final_total = current.get(candidate.physician_id, 0) + additions[candidate.physician_id] + 1
    target_total = proposed_total * capacity.precepting_share
    if imbalance_with_choice > imbalance_without_choice:
        return (
            "disadvantage",
            f"Adds a Faculty Precepting shift for {candidate.physician_name} despite "
            f"{final_total} shifts against a {target_total:.1f} proportional target.",
        )
    if imbalance_with_choice < imbalance_without_choice:
        return "advantage", "Contributes to Faculty workload balance."
    return "neutral", "Maintains the proposal's Faculty workload balance."


def _faculty_workload_imbalance(capacities, current, additions, total, capacity_total):
    return sum(
        abs(
            (current.get(capacity.physician_id, 0) + additions.get(capacity.physician_id, 0))
            * capacity_total
            - _precepting_capacity_units(capacity) * total
        )
        for capacity in capacities
    )


def _ineligibility_reasons(candidate, requirement):
    reasons = [reason for reason in candidate.reasons if reason.severity == "hard"]
    if candidate.manual_float_eligible:
        manual_float_reason = next(
            (reason for reason in candidate.reasons if reason.code == "manual_float_availability"),
            None,
        )
        if manual_float_reason is not None:
            reasons.append(manual_float_reason)
        else:
            reasons.append(
                CandidateAssessmentReason(
                    "manual_float_availability",
                    "manual_only",
                    None,
                    "Available as Float",
                    "Admin makes this effective Float Preceptor manually available; "
                    "Schedule Bot does not select it.",
                )
            )
    if candidate.physician_id in requirement.excluded_physician_ids:
        reasons.append(
            CandidateAssessmentReason(
                "solve_local_exclusion",
                "solve_local",
                "hard",
                "Excluded from this Fill proposal",
                "This physician was excluded for this proposal only.",
            )
        )
    if requirement.is_late_doc and not candidate.manually_eligible_for_late_doc:
        reasons.append(
            CandidateAssessmentReason(
                "late_doc_role_boundary",
                "structural",
                "hard",
                "Ineligible role",
                "Late Doc requires an actual Faculty role.",
            )
        )
    if (
        requirement.is_late_doc
        and not candidate.manual_float_eligible
        and candidate.availability not in ("available", "backup_only")
    ):
        reasons.append(
            CandidateAssessmentReason(
                "not_available_late_doc",
                "hard",
                "hard",
                "Not available",
                "Tier 0 availability is not available for Monday Late Doc.",
            )
        )
    return tuple(reasons)


def _same_policy_profile(candidate, alternative, solver_input):
    """Recognize interchangeable candidates without re-solving each displayed alternative."""
    return _candidate_policy_profile(candidate, solver_input) == _candidate_policy_profile(
        alternative, solver_input
    )


def _candidate_policy_profile(candidate, solver_input):
    capacities = {item.physician_id: item for item in solver_input.faculty_capacities}
    faculty_current = _assignment_counts(solver_input.balance_assignments, capacities.values())
    late_current = _assignment_counts(
        solver_input.balance_late_doc_assignments, capacities.values()
    )

    def value(item):
        if item is None:
            return None
        return (
            item.effective_roles,
            item.availability,
            item.base_availability,
            item.ordinarily_eligible_to_precept,
            item.faculty_equivalent,
            item.expected_community_commitment,
            item.preceptor_assignment_id is not None,
            item.precepting_conflict_codes,
            item.has_soft_academic_event,
        )

    session_profile = tuple(
        (
            session.date,
            session.period_of_day,
            value(
                next(
                    (
                        item
                        for item in session.candidates
                        if item.physician_id == candidate.physician_id
                    ),
                    None,
                )
            ),
        )
        for session in solver_input.sessions
    )
    late_doc_profile = tuple(
        (
            position.date,
            value(
                next(
                    (
                        item
                        for item in position.candidates
                        if item.physician_id == candidate.physician_id
                    ),
                    None,
                )
            ),
        )
        for position in solver_input.late_doc_positions
    )
    capacity = capacities.get(candidate.physician_id)
    return (
        session_profile,
        late_doc_profile,
        (
            _precepting_capacity_units(capacity),
            capacity.late_doc_capacity,
            faculty_current.get(candidate.physician_id, 0),
            late_current.get(candidate.physician_id, 0),
        )
        if capacity
        else None,
    )


def _has_equal_policy_outcome(
    requirement,
    alternative,
    solver_input,
    policy_objectives,
    late_doc_band_totals,
):
    adapter = _CpSatFillAdapter(
        solver_input,
        forced_choices=(
            (
                requirement.date,
                requirement.period_of_day,
                requirement.position,
                alternative.physician_id,
            ),
        ),
    )
    return adapter.has_policy_outcome(
        policy_objectives,
        late_doc_band_totals,
    )


def _has_monday_pm_precepting_overlap(requirement, candidate, selected, solver_input):
    for session in solver_input.sessions:
        if session.date != requirement.date or session.period_of_day != "PM":
            continue
        if any(
            position.assignment and position.assignment.physician_id == candidate.physician_id
            for position in session.positions
        ):
            return True
        return any(
            other.date == requirement.date
            and other.period_of_day == "PM"
            and selected_candidate.physician_id == candidate.physician_id
            for other, selected_candidate in selected.items()
            if not other.is_late_doc
        )
    return False


def _policy_objectives(solver_input, selected):
    """Return the ordered policy values before the seeded exact-tie objective."""
    primary_count = sum(requirement.is_primary for requirement in selected)
    coverage_count = 0
    for session in solver_input.sessions:
        if session.session_id is None:
            continue
        candidates_by_id = {candidate.physician_id: candidate for candidate in session.candidates}
        faculty_equivalent_count = sum(
            candidates_by_id[assignment.physician_id].faculty_equivalent
            for position in session.positions
            for assignment in (position.assignment,)
            if assignment is not None and assignment.physician_id in candidates_by_id
        ) + sum(
            candidate.faculty_equivalent
            for requirement, candidate in selected.items()
            if requirement.session_id == session.session_id
        )
        coverage_count += faculty_equivalent_count >= 2
    late_doc_count = sum(requirement.is_late_doc for requirement in selected)
    backup_count = sum(candidate.availability == "backup_only" for candidate in selected.values())
    soft_academic_event_count = sum(
        candidate.has_soft_academic_event for candidate in selected.values()
    )
    community_count = sum(
        requirement.is_primary and candidate.expected_community_commitment
        for requirement, candidate in selected.items()
    )
    primary_faculty_count = sum(
        requirement.is_primary and candidate.faculty_equivalent
        for requirement, candidate in selected.items()
    )
    secondary_count = sum(requirement.position == "secondary" for requirement in selected)
    community_first_count = sum(
        requirement.position == "primary-1" and candidate.expected_community_commitment
        for requirement, candidate in selected.items()
    )
    community_first_sort_score = sum(
        _community_first_sort_value(requirement, candidate)
        for requirement, candidate in selected.items()
    )
    capacities = solver_input.faculty_capacities
    faculty_current = _assignment_counts(solver_input.balance_assignments, capacities)
    late_current = _assignment_counts(solver_input.balance_late_doc_assignments, capacities)
    faculty_additions = {capacity.physician_id: 0 for capacity in capacities}
    late_additions = {capacity.physician_id: 0 for capacity in capacities}
    for requirement, candidate in selected.items():
        if requirement.is_late_doc:
            late_additions[candidate.physician_id] = (
                late_additions.get(candidate.physician_id, 0) + 1
            )
        elif candidate.is_faculty:
            faculty_additions[candidate.physician_id] = (
                faculty_additions.get(candidate.physician_id, 0) + 1
            )
    faculty_capacity_total = sum(_precepting_capacity_units(capacity) for capacity in capacities)
    faculty_total = sum(faculty_current.values()) + sum(faculty_additions.values())
    faculty_imbalance = sum(
        abs(
            (
                faculty_current.get(capacity.physician_id, 0)
                + faculty_additions.get(capacity.physician_id, 0)
            )
            * faculty_capacity_total
            - _precepting_capacity_units(capacity) * faculty_total
        )
        for capacity in capacities
    )
    late_capacity_total = sum(capacity.late_doc_capacity for capacity in capacities)
    late_total = sum(late_current.values()) + sum(late_additions.values())
    late_imbalance = sum(
        abs(
            (
                late_current.get(capacity.physician_id, 0)
                + late_additions.get(capacity.physician_id, 0)
            )
            * late_capacity_total
            - capacity.late_doc_capacity * late_total
        )
        for capacity in capacities
    )
    late_overlap = sum(
        _has_monday_pm_precepting_overlap(requirement, candidate, selected, solver_input)
        for requirement, candidate in selected.items()
        if requirement.is_late_doc
    )
    primary_one_count = sum(requirement.position == "primary-1" for requirement in selected)
    return (
        primary_count,
        coverage_count,
        late_doc_count,
        backup_count,
        soft_academic_event_count,
        community_count,
        primary_faculty_count,
        secondary_count,
        community_first_count,
        community_first_sort_score,
        faculty_imbalance,
        late_imbalance,
        late_overlap,
        primary_one_count,
    )


def _compromise_summary(solver_input, selected, unresolved):
    faculty_capacities = solver_input.faculty_capacities
    faculty_current = _assignment_counts(solver_input.balance_assignments, faculty_capacities)
    late_current = _assignment_counts(solver_input.balance_late_doc_assignments, faculty_capacities)
    faculty_additions = {capacity.physician_id: 0 for capacity in faculty_capacities}
    late_additions = {capacity.physician_id: 0 for capacity in faculty_capacities}
    backup = []
    overlap = []
    selected_community = {
        (session.date, session.period_of_day, position.assignment.physician_id)
        for session in solver_input.sessions
        for position in session.positions
        if position.assignment is not None
    }
    for requirement, candidate in selected.items():
        if candidate.availability == "backup_only":
            backup.append(
                f"{candidate.physician_name} on {requirement.date.isoformat()} {requirement.position}"
            )
        if requirement.is_late_doc:
            late_additions[candidate.physician_id] = (
                late_additions.get(candidate.physician_id, 0) + 1
            )
            if _has_monday_pm_precepting_overlap(requirement, candidate, selected, solver_input):
                overlap.append(f"{requirement.date.isoformat()}: {candidate.physician_name}")
        elif candidate.is_faculty:
            faculty_additions[candidate.physician_id] = (
                faculty_additions.get(candidate.physician_id, 0) + 1
            )
        if requirement.is_primary and candidate.expected_community_commitment:
            selected_community.add(
                (requirement.date, requirement.period_of_day, candidate.physician_id)
            )
    faculty_total = sum(faculty_current.values()) + sum(faculty_additions.values())
    late_total = sum(late_current.values()) + sum(late_additions.values())
    faculty_workloads = tuple(
        FacultyWorkloadTotal(
            capacity.physician_id,
            capacity.physician_name,
            faculty_current.get(capacity.physician_id, 0),
            faculty_current.get(capacity.physician_id, 0)
            + faculty_additions.get(capacity.physician_id, 0),
            capacity.precepting_share,
            faculty_total * capacity.precepting_share,
        )
        for capacity in faculty_capacities
    )
    late_workloads = tuple(
        LateDocWorkloadTotal(
            capacity.physician_id,
            capacity.physician_name,
            late_current.get(capacity.physician_id, 0),
            late_current.get(capacity.physician_id, 0)
            + late_additions.get(capacity.physician_id, 0),
            capacity.late_doc_share,
            late_total * capacity.late_doc_share,
        )
        for capacity in faculty_capacities
    )
    missed_community = tuple(
        f"{commitment.date.isoformat()} {commitment.period_of_day}: {commitment.physician_name}"
        for commitment in solver_input.community_commitments
        if commitment.available
        and (commitment.date, commitment.period_of_day, commitment.physician_id)
        not in selected_community
    )
    imbalance = [
        f"{item.physician_name} has {item.proposed_total} Faculty Precepting shifts against a "
        f"{item.target_total:.1f} proportional target."
        for item in faculty_workloads
        if abs(item.proposed_total - item.target_total) > 0.5
    ]
    imbalance.extend(
        f"{item.physician_name} has {item.proposed_total} Late Doc shifts against a "
        f"{item.target_total:.1f} proportional target."
        for item in late_workloads
        if abs(item.proposed_total - item.target_total) > 1
    )
    if unresolved:
        imbalance.append("Some required work remains unresolved after applying hard constraints.")
    return FillProposalCompromiseSummary(
        faculty_workloads=faculty_workloads,
        late_doc_workloads=late_workloads,
        backup_assignments=tuple(backup),
        missed_community_commitments=missed_community,
        unavoidable_imbalance=tuple(imbalance),
        late_doc_pm_precepting_overlap=tuple(overlap),
    )


def _proposal_choice_labels(proposal):
    labels = {}
    for assignment in proposal.assignments:
        labels[(assignment.date, assignment.period_of_day, assignment.position)] = (
            f"{assignment.date.isoformat()} {assignment.period_of_day} "
            f"{_position_label(assignment.position)}: "
            f"{assignment.physician_name}"
        )
    for assignment in proposal.late_doc_assignments:
        labels[(assignment.date, None, "late-doc")] = (
            f"{assignment.date.isoformat()} late-doc: {assignment.physician_name}"
        )
    return labels


def _proposal_unresolved_labels(proposal):
    return {
        (requirement.date, requirement.period_of_day, requirement.position): (
            f"{requirement.date.isoformat()} "
            f"{requirement.period_of_day + ' ' if requirement.period_of_day else ''}"
            f"{_position_label(requirement.position)}: unresolved"
        )
        for requirement in proposal.unresolved_requirements
    }


def _removed_proposal_choice_label(key):
    on_date, period, position = key
    return f"{on_date.isoformat()} {period + ' ' if period else ''}{_position_label(position)}: removed"


def _fill_requirement_sort_key(requirement):
    position_order = {"primary-1": 0, "primary-2": 1, "secondary": 2, "late-doc": 3}
    period_order = {"AM": 0, "PM": 1, None: 2}
    return (
        requirement.date,
        period_order[requirement.period_of_day],
        position_order[requirement.position],
    )


def _unresolved_primary(requirement):
    cause = "candidate_scarcity" if requirement.candidates else "no_eligible_candidate"
    return FillProposalUnresolvedRequirement(
        requirement.date,
        requirement.period_of_day,
        requirement.position,
        "missing_primary_coverage",
        f"No feasible proposal fills {_position_label(requirement.position)}.",
        (cause,),
    )


def _unresolved_late_doc(requirement):
    cause = "candidate_scarcity" if requirement.candidates else "no_eligible_faculty_candidate"
    return FillProposalUnresolvedRequirement(
        requirement.date,
        None,
        "late-doc",
        "missing_late_doc",
        "No feasible actual-Faculty proposal fills Monday Late Doc.",
        (cause,),
    )


def _position_label(position):
    if position.startswith("primary-"):
        return f"Preceptor {position.removeprefix('primary-')}"
    return position.replace("-", " ").title()


def _unresolved_sort_key(requirement):
    code_order = {
        "missing_primary_coverage": 0,
        "insufficient_faculty_equivalent_coverage": 1,
        "missing_late_doc": 2,
    }
    return (
        requirement.date,
        {"AM": 0, "PM": 1, None: 2}[requirement.period_of_day],
        code_order[requirement.code],
        requirement.position,
    )


def _solver_session(on_date, period, session, rows):
    source_assignments = tuple(
        row.preceptor_assignment for row in rows if row.preceptor_assignment is not None
    )
    assignments = {
        (
            assignment.responsibility,
            assignment.primary_position,
        ): _assignment(
            assignment,
            is_faculty=_is_faculty(
                next(row for row in rows if row.physician.pk == assignment.physician_id)
            ),
        )
        for assignment in source_assignments
    }

    def position(key, required, responsibility, primary_position):
        assignment = assignments.get((responsibility, primary_position))
        current_assignment = next(
            (
                item
                for item in source_assignments
                if item.responsibility == responsibility
                and item.primary_position == primary_position
            ),
            None,
        )
        assigned_elsewhere = {
            item.physician_id for item in source_assignments if item is not current_assignment
        }
        return SolverPosition(
            key,
            required,
            assignment,
            tuple(
                _candidate(
                    row,
                    candidate_assessment_from_projection_row(
                        row,
                        session,
                        responsibility,
                        primary_position,
                        current_assignment=current_assignment,
                        assigned_elsewhere=assigned_elsewhere,
                    ),
                )
                for row in rows
            ),
        )

    return SolverSession(
        date=on_date,
        period_of_day=period,
        session_id=session.pk if session else None,
        positions=(
            position("primary-1", True, "primary", 1),
            position("primary-2", True, "primary", 2),
            position("secondary", False, "secondary", None),
        ),
        candidates=tuple(_candidate(row) for row in rows),
    )


def _candidate(row, assessment=None, *, manual_float_reason=None):
    return SolverCandidate(
        physician_id=row.physician.pk,
        physician_name=row.physician.display_name,
        effective_roles=tuple(role.role for role in row.effective_roles),
        availability=row.availability,
        base_availability=row.base_availability,
        ordinarily_eligible_to_precept=row.ordinarily_eligible_to_precept,
        faculty_equivalent=row.faculty_equivalent,
        expected_community_commitment=row.is_expected_community_commitment,
        preceptor_assignment_id=row.preceptor_assignment.pk if row.preceptor_assignment else None,
        interview_shift_id=row.interview_shift.pk if row.interview_shift else None,
        source_ids=tuple((source.source_type, source.source_id) for source in row.sources),
        precepting_conflict_codes=tuple(conflict.code for conflict in row.precepting_conflicts),
        has_soft_academic_event=row.has_soft_academic_event,
        manually_selectable=assessment.manually_selectable if assessment else True,
        manual_float_eligible=row.tier_zero.is_float_only,
        manually_eligible_for_late_doc=is_manually_eligible_for_position("late_doc", row.tier_zero),
        reasons=(
            assessment.reasons
            if assessment
            else ((manual_float_reason,) if manual_float_reason is not None else ())
        ),
    )


def _assignment(assignment, *, is_faculty=False):
    return SolverAssignment(
        id=assignment.pk,
        physician_id=assignment.physician_id,
        physician_name=assignment.physician.display_name,
        responsibility=assignment.responsibility,
        primary_position=assignment.primary_position,
        is_locked=assignment.is_locked,
        is_faculty=is_faculty,
    )


def _late_doc_assignment(assignment):
    if assignment is None:
        return None
    return SolverAssignment(
        id=assignment.pk,
        physician_id=assignment.physician_id,
        physician_name=assignment.physician.display_name,
        responsibility="late_doc",
        primary_position=None,
        is_locked=None,
        is_faculty=True,
    )


def _faculty_capacities(balance_slots, rows_by_slot):
    counts = {}
    names = {}
    late_doc_counts = {}
    override_units = {}
    overrides = {}
    for on_date, period in balance_slots:
        for row in rows_by_slot.get((on_date, period), ()):
            if not _is_faculty(row):
                continue
            names[row.physician.pk] = row.physician.display_name
            override = row.physician.faculty_precepting_capacity_override
            if override is not None:
                overrides[row.physician.pk] = override
                # A one-decimal weekly setting is represented in hundredths of a shift,
                # so every covered weekday AM/PM slot receives an exact tenth.
                override_units[row.physician.pk] = override_units.get(row.physician.pk, 0) + int(
                    override * 10
                )
            if row.base_availability == "open":
                counts[row.physician.pk] = counts.get(row.physician.pk, 0) + 1
                if on_date.weekday() == 0 and period == "PM":
                    late_doc_counts[row.physician.pk] = late_doc_counts.get(row.physician.pk, 0) + 1
    precepting_units = {
        physician_id: override_units.get(physician_id, counts.get(physician_id, 0) * 100)
        for physician_id in names
    }
    total = sum(precepting_units.values())
    late_doc_total = sum(late_doc_counts.values())
    return tuple(
        FacultyCapacity(
            physician_id=physician_id,
            physician_name=names[physician_id],
            precepting_capacity=Decimal(precepting_units[physician_id]) / 100,
            precepting_share=precepting_units[physician_id] / total if total else 0,
            late_doc_capacity=late_doc_counts.get(physician_id, 0),
            late_doc_share=late_doc_counts.get(physician_id, 0) / late_doc_total
            if late_doc_total
            else 0,
            precepting_capacity_units=precepting_units[physician_id],
            custom_precepting_capacity_override=overrides.get(physician_id),
        )
        for physician_id in sorted(names, key=lambda key: (names[key].casefold(), key))
    )


def _source_versions(
    rows, sessions, late_doc_assignments, solve_range, balance_window, holidays=(), capacities=()
):
    values = []
    for row in rows:
        values.append(
            (
                "slot_projection",
                f"{row.slot.on_date.isoformat()}:{row.slot.period_of_day}:{row.physician.pk}",
                {
                    "roles": [
                        (
                            role.pk,
                            role.role,
                            role.effective_start,
                            role.effective_end,
                            role.faculty_equivalent,
                        )
                        for role in row.effective_roles
                    ],
                    "availability": row.availability,
                    "base_availability": row.base_availability,
                    "expected_community": row.is_expected_community_commitment,
                    "sources": [
                        (
                            source.source_type,
                            source.source_id,
                            source.contribution,
                            source.reason,
                            source.selection,
                        )
                        for source in row.sources
                    ],
                    "preceptor_assignment": _assignment_value(row.preceptor_assignment),
                    "interview_shift": (
                        (
                            row.interview_shift.pk,
                            row.interview_shift.physician_id,
                            row.interview_shift.is_active,
                        )
                        if row.interview_shift
                        else None
                    ),
                    "precepting_conflicts": [
                        (conflict.code, conflict.source_type, conflict.source_id)
                        for conflict in row.precepting_conflicts
                    ],
                },
            )
        )
    for session in sessions:
        values.append(
            (
                "preceptor_session",
                f"{session.date.isoformat()}:{session.period_of_day}",
                (session.session_id, session.date, session.period_of_day),
            )
        )
    for on_date, assignment in late_doc_assignments.items():
        values.append(
            ("late_doc_assignment", on_date.isoformat(), _late_doc_assignment_value(assignment))
        )
    values.extend(
        ("holiday", holiday.observed_date.isoformat(), (holiday.name, holiday.coverage))
        for holiday in holidays
    )
    values.extend(
        (
            "faculty_precepting_capacity",
            str(capacity.physician_id),
            (
                str(capacity.custom_precepting_capacity_override)
                if capacity.custom_precepting_capacity_override is not None
                else None,
                str(capacity.precepting_capacity),
                capacity.precepting_capacity_units,
            ),
        )
        for capacity in capacities
    )
    values.extend(
        (
            ("solve_range", "selected", (solve_range.start_date, solve_range.end_date)),
            ("balance_window", "selected", (balance_window.start_date, balance_window.end_date)),
        )
    )
    return tuple(
        SourceVersion(kind, key, _digest(value))
        for kind, key, value in sorted(values, key=lambda value: (value[0], value[1]))
    )


def _preflight_blockers(solver_input, sessions_by_slot):
    blockers = []
    if solver_input.solve_range.start_date < organization_today():
        blockers.append(
            PreflightBlocker(
                "past_mutation_range",
                "The solve range includes dates before today and cannot be changed.",
                tuple(
                    on_date
                    for on_date in _weekdays_in(solver_input.solve_range)
                    if on_date < organization_today()
                ),
            )
        )
    missing_sessions = tuple(
        session.date
        for session in solver_input.sessions
        if not sessions_by_slot.get((session.date, session.period_of_day))
    )
    if missing_sessions:
        blockers.append(
            PreflightBlocker(
                "missing_preceptor_sessions",
                "The solve range is missing required Preceptor draft sessions.",
                tuple(dict.fromkeys(missing_sessions)),
            )
        )
    inconsistent_sessions = tuple(
        (on_date, period)
        for (on_date, period), sessions in sessions_by_slot.items()
        if len(sessions) != 1
    )
    if inconsistent_sessions:
        blockers.append(
            PreflightBlocker(
                "inconsistent_preceptor_sessions",
                "More than one Preceptor draft session exists for a required date and period.",
                tuple(on_date for on_date, _ in inconsistent_sessions),
            )
        )
    for status in normal_rounder_statuses_for_weeks(
        rounding_weeks(solver_input.solve_range.start_date, solver_input.solve_range.end_date)
    ):
        if status.normal_rounder is None:
            blockers.append(
                PreflightBlocker(
                    "missing_normal_rounder",
                    "No normal rounder is recorded for a solve week.",
                    (status.week,),
                )
            )
        elif status.inconsistent:
            blockers.append(
                PreflightBlocker(
                    "inconsistent_normal_rounder",
                    status.reason,
                    (status.week,),
                )
            )
    return tuple(blockers)


def _assignment_value(assignment):
    if assignment is None:
        return None
    return (
        assignment.pk,
        assignment.physician_id,
        assignment.responsibility,
        assignment.primary_position,
        assignment.is_locked,
    )


def _late_doc_assignment_value(assignment):
    return (
        assignment.pk,
        assignment.physician_id,
        "late_doc",
        None,
        None,
    )


def _single_session(sessions):
    return sessions[0] if len(sessions) == 1 else None


def _is_faculty(row):
    return any(role.role == EffectiveRole.Role.FACULTY for role in row.effective_roles)


def _weekdays_in(date_range):
    return tuple(
        date_range.start_date + timedelta(days=offset)
        for offset in range((date_range.end_date - date_range.start_date).days + 1)
        if (date_range.start_date + timedelta(days=offset)).weekday() < 5
    )


def _add_months(month, offset):
    month_number = month.month - 1 + offset
    return date(month.year + month_number // 12, month_number % 12 + 1, 1)


def _shift_date_months(value, offset):
    month = _add_months(value.replace(day=1), offset)
    return month.replace(day=min(value.day, calendar.monthrange(month.year, month.month)[1]))


def _exceeds_calendar_months(date_range, maximum_months):
    return date_range.end_date >= _shift_date_months(date_range.start_date, maximum_months)


def _last_day_of_month(month):
    return month.replace(day=calendar.monthrange(month.year, month.month)[1])


def _digest(value):
    def default(item):
        if isinstance(item, date):
            return item.isoformat()
        raise TypeError(f"Cannot serialize {item!r}")

    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), default=default)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def retain_fill_proposal(
    *,
    solver_input,
    fill_proposal,
    scheduler,
    mode="fill",
    policy_version="initial",
    solve_settings=None,
):
    """Persist a reviewed solver artifact without changing the authoritative draft."""
    require_scheduler(scheduler)
    if not isinstance(solver_input, PreceptingSolverInput):
        raise ValidationError("Supply a PreceptingSolverInput from preflight.")
    validate_precepting_solver_capacity(
        solver_input.solve_range, balance_window=solver_input.balance_window
    )
    if not isinstance(fill_proposal, FillProposal):
        raise ValidationError("Supply the FillProposal to retain.")
    if fill_proposal.solver_input_fingerprint != solver_input.fingerprint:
        raise ValidationError(
            "The FillProposal was produced from a different PreceptingSolverInput."
        )
    if mode not in PreceptingSolverProposal.Mode.values:
        raise ValidationError("Solver proposal mode must be fill or repair.")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise ValidationError("Solver proposals require a policy version.")
    if solve_settings is not None and not isinstance(solve_settings, dict):
        raise ValidationError("Solve-local settings must be a dictionary.")

    settings = dict(solve_settings or {})
    solve_seed = fill_proposal.solve_seed or new_solve_seed()
    if settings.get("solve_seed") not in (None, solve_seed):
        raise ValidationError("Solve settings must retain the FillProposal seed.")
    settings["solve_seed"] = solve_seed
    before_findings, after_findings = _proposal_finding_snapshots(solver_input, fill_proposal)
    proposal = PreceptingSolverProposal.objects.create(
        mode=mode,
        solve_start_date=solver_input.solve_range.start_date,
        solve_end_date=solver_input.solve_range.end_date,
        balance_start_date=solver_input.balance_window.start_date,
        balance_end_date=solver_input.balance_window.end_date,
        policy_version=policy_version.strip(),
        solve_settings=settings,
        input_fingerprint=solver_input.fingerprint,
        input_snapshot=_json_value(solver_input),
        proposed_assignments={
            "precepting": _json_value(
                (*fill_proposal.assignments, *fill_proposal.precepting_removals)
            ),
            "late_doc": _json_value(
                (*fill_proposal.late_doc_assignments, *fill_proposal.late_doc_removals)
            ),
        },
        before_findings=before_findings,
        after_findings=after_findings,
        workload_effects={
            "faculty": _json_value(fill_proposal.compromises.faculty_workloads),
            "late_doc": _json_value(fill_proposal.compromises.late_doc_workloads),
        },
        compromises=_json_value(fill_proposal.compromises),
        created_by=scheduler,
        last_edited_by=scheduler,
    )
    late_doc_review_values = [
        (assignment.date, assignment.physician_id)
        for assignment in fill_proposal.late_doc_assignments
    ]
    late_doc_review_values.extend(
        (
            removal.date,
            next(
                position.assignment.physician_id
                for position in solver_input.late_doc_positions
                if position.date == removal.date and position.assignment is not None
            ),
        )
        for removal in fill_proposal.late_doc_removals
    )
    PreceptingSolverLateDocReview.objects.bulk_create(
        PreceptingSolverLateDocReview(
            proposal=proposal,
            date=on_date,
            proposed_physician_id=physician_id,
        )
        for on_date, physician_id in late_doc_review_values
    )
    return proposal


@transaction.atomic
def review_proposed_late_doc(
    *, proposal, on_date, decision, scheduler, replacement_physician=None, expected_revision=None
):
    """Record the scheduler's required confirmation or replacement for one Monday."""
    require_scheduler(scheduler)
    proposal = PreceptingSolverProposal.objects.select_for_update().get(pk=proposal.pk)
    _require_proposal_revision(proposal, expected_revision)
    if proposal.applied_at is not None:
        raise ValidationError("An applied solver proposal cannot be reviewed again.")
    solver_input = _current_proposal_input(proposal)
    review = PreceptingSolverLateDocReview.objects.select_for_update().get(
        proposal=proposal, date=on_date
    )
    if decision == PreceptingSolverLateDocReview.Decision.CONFIRMED:
        if replacement_physician is not None:
            raise ValidationError("A confirmed Late Doc decision cannot include a replacement.")
        replacement_physician = None
    elif decision == PreceptingSolverLateDocReview.Decision.REPLACED:
        if replacement_physician is None:
            raise ValidationError("A replacement Late Doc decision requires a physician.")
        position = next(
            (item for item in solver_input.late_doc_positions if item.date == on_date), None
        )
        candidate = (
            next(
                (
                    item
                    for item in position.candidates
                    if item.physician_id == replacement_physician.pk
                ),
                None,
            )
            if position
            else None
        )
        if candidate is None or not _eligible_for_late_doc(
            candidate, excluded_physician_ids=solver_input.excluded_physician_ids
        ):
            raise ValidationError("This physician is no longer eligible for Late Doc.")
    else:
        raise ValidationError("Late Doc decisions must be confirmed or replaced.")
    review.decision = decision
    review.replacement_physician = replacement_physician
    review.reviewed_by = scheduler
    review.reviewed_at = timezone.now()
    review.save(update_fields=("decision", "replacement_physician", "reviewed_by", "reviewed_at"))
    _refresh_effective_proposal(
        proposal,
        scheduler,
        solver_input=solver_input,
    )
    return review


def retain_proposal_comparison(*, proposal, comparison, scheduler):
    """Keep an on-demand comparison as evidence without changing the proposal choice set."""
    require_scheduler(scheduler)
    if not isinstance(comparison, FillProposalComparison):
        raise ValidationError("Supply a FillProposalComparison to retain.")
    if comparison.proposal.solver_input_fingerprint != proposal.input_fingerprint:
        raise ValidationError("The comparison was produced from a different solver proposal input.")
    return PreceptingSolverProposalComparison.objects.create(
        proposal=proposal,
        result=_json_value(comparison),
        created_by=scheduler,
        proposal_revision=proposal.revision,
    )


@dataclass(frozen=True)
class ProposalReconciliationPreview:
    current_input_fingerprint: str
    changed_shift_count: int
    incorporated_count: int
    already_matched_count: int
    conflict_shift_count: int
    conflicts: tuple[dict, ...]


def preview_proposal_reconciliation(proposal):
    """Compare the starting draft, current draft, and effective proposal positions."""
    current_input = _reconciliation_current_input(proposal)
    conflicts = []
    changed_shifts = set()
    outcomes_by_shift = {}
    for key in _reconciliation_keys(proposal, current_input):
        starting = _baseline_assignment(proposal, key)
        current = _input_assignment_value(current_input, key)
        proposed = _reconciliation_proposal_value(proposal, key)
        if _same_assignment(starting, current):
            continue
        shift = (key[1], key[2])
        changed_shifts.add(shift)
        if _same_assignment(proposed, starting):
            outcomes_by_shift.setdefault(shift, set()).add("incorporated")
            continue
        if _same_assignment(current, proposed):
            outcomes_by_shift.setdefault(shift, set()).add("already_matched")
        elif _same_assignment(current, starting):
            outcomes_by_shift.setdefault(shift, set()).add("incorporated")
        else:
            outcomes_by_shift.setdefault(shift, set()).add("conflict")
            conflicts.append(
                {
                    "key": key,
                    "assignment_type": key[0],
                    "date": key[1],
                    "period_of_day": key[2],
                    "position": key[3],
                    "current": current,
                    "proposed": proposed,
                }
            )
    return ProposalReconciliationPreview(
        current_input_fingerprint=current_input.fingerprint,
        changed_shift_count=len(changed_shifts),
        incorporated_count=sum(
            "conflict" not in outcomes and "incorporated" in outcomes
            for outcomes in outcomes_by_shift.values()
        ),
        already_matched_count=sum(
            outcomes == {"already_matched"} for outcomes in outcomes_by_shift.values()
        ),
        conflict_shift_count=sum("conflict" in outcomes for outcomes in outcomes_by_shift.values()),
        conflicts=tuple(conflicts),
    )


@scheduling_mutation
@transaction.atomic
def reconcile_precepting_solver_proposal(
    *, proposal, scheduler, expected_revision, expected_current_input_fingerprint, choices
):
    """Create a new proposal from current draft facts without mutating either source."""
    require_scheduler(scheduler)
    source = PreceptingSolverProposal.objects.select_for_update().get(pk=proposal.pk)
    _require_proposal_revision(source, expected_revision)
    if source.applied_at is not None:
        raise ValidationError("An applied solver proposal cannot be reconciled.")
    current_input = _reconciliation_current_input(source)
    if current_input.fingerprint != expected_current_input_fingerprint:
        raise ValidationError(
            "The ordinary draft changed while you were reconciling it. Start again."
        )
    if PreceptingSolverProposalReconciliation.objects.filter(
        source_proposal=source,
        current_input_fingerprint=current_input.fingerprint,
    ).exists():
        raise ValidationError("This proposal was already reconciled with the current draft.")
    preview = preview_proposal_reconciliation(source)
    choices = choices or {}
    conflict_choices = {}
    for conflict in preview.conflicts:
        key = conflict["key"]
        choice = choices.get(_reconciliation_choice_key(key))
        if choice not in {"current", "proposal"}:
            raise ValidationError(
                "Choose the current draft or proposal for every conflicting assignment."
            )
        conflict_choices[key] = choice

    changes = {"precepting": [], "late_doc": []}
    removals = []
    recorded_choices = []
    for key in _reconciliation_keys(source, current_input):
        starting = _baseline_assignment(source, key)
        current = _input_assignment_value(current_input, key)
        proposed = _reconciliation_proposal_value(source, key)
        if _same_assignment(current, starting) or _same_assignment(current, proposed):
            chosen = proposed
        elif _same_assignment(proposed, starting):
            chosen = current
        else:
            chosen = proposed if conflict_choices[key] == "proposal" else current
            recorded_choices.append(
                {
                    "position": _reconciliation_choice_key(key),
                    "choice": conflict_choices[key],
                    "starting_physician_id": (starting or {}).get("physician_id"),
                    "current_physician_id": (current or {}).get("physician_id"),
                    "proposal_physician_id": (proposed or {}).get("physician_id"),
                }
            )
        if not _same_assignment(chosen, current):
            if chosen is None:
                removals.append((key, current))
            else:
                changes[key[0]].append(_reconciliation_assignment(current_input, key, chosen))

    from .preceptors import projected_draft_findings_for_weeks

    before_findings = _scoped_finding_snapshots(
        current_input,
        projected_draft_findings_for_weeks(
            rounding_weeks(current_input.solve_range.start_date, current_input.solve_range.end_date)
        ).findings,
    )
    reconciled = PreceptingSolverProposal.objects.create(
        mode=source.mode,
        solve_start_date=current_input.solve_range.start_date,
        solve_end_date=current_input.solve_range.end_date,
        balance_start_date=current_input.balance_window.start_date,
        balance_end_date=current_input.balance_window.end_date,
        policy_version=source.policy_version,
        solve_settings=source.solve_settings,
        input_fingerprint=current_input.fingerprint,
        input_snapshot=_json_value(current_input),
        proposed_assignments=changes,
        before_findings=before_findings,
        after_findings=[],
        workload_effects={},
        compromises={},
        created_by=scheduler,
        last_edited_by=scheduler,
    )
    PreceptingSolverProposalEdit.objects.bulk_create(
        PreceptingSolverProposalEdit(
            proposal=reconciled,
            assignment_type=key[0],
            date=date.fromisoformat(key[1]),
            period_of_day=key[2],
            position=key[3],
            previous_value=current,
            resulting_value=None,
            note=f"Reconciled from proposal {source.pk}.",
            edited_by=scheduler,
        )
        for key, current in removals
    )
    late_doc_review_values = [
        (assignment["date"], assignment["physician_id"])
        for assignment in changes["late_doc"]
        if assignment
    ]
    late_doc_review_values.extend(
        (key[1], current["physician_id"])
        for key, current in removals
        if key[0] == "late_doc" and current
    )
    PreceptingSolverLateDocReview.objects.bulk_create(
        PreceptingSolverLateDocReview(
            proposal=reconciled,
            date=date.fromisoformat(on_date),
            proposed_physician_id=physician_id,
        )
        for on_date, physician_id in late_doc_review_values
    )
    _recalculate_effective_proposal(reconciled, current_input)
    reconciled.save(update_fields=("after_findings", "workload_effects", "compromises"))
    PreceptingSolverProposalReconciliation.objects.create(
        proposal=reconciled,
        source_proposal=source,
        starting_input_fingerprint=source.input_fingerprint,
        current_input_fingerprint=current_input.fingerprint,
        choices=recorded_choices,
        reconciled_by=scheduler,
    )
    return reconciled


def _reconciliation_current_input(proposal):
    result = preflight_precepting_solve(
        SolveDateRange(proposal.solve_start_date, proposal.solve_end_date),
        balance_window=SolveDateRange(proposal.balance_start_date, proposal.balance_end_date),
    )
    if result.blockers:
        raise ValidationError("The current draft is not ready to reconcile.")
    return without_excluded_physicians(
        result.solver_input, proposal.solve_settings.get("excluded_physician_ids", ())
    )


def _reconciliation_keys(proposal, current_input):
    keys = {
        ("late_doc", position.date.isoformat(), "", "late-doc")
        for position in current_input.late_doc_positions
    }
    keys.update(
        {
            ("precepting", session.date.isoformat(), session.period_of_day, position.key)
            for session in current_input.sessions
            for position in session.positions
        }
    )
    return tuple(sorted(keys))


def _input_assignment_value(solver_input, key):
    source = _source_position(solver_input, key)
    return _json_value(source.assignment) if source and source.assignment else None


def _reconciliation_choice_key(key):
    return ":".join(key)


def _reconciliation_assignment(solver_input, key, value):
    if value is None:
        return None
    assignment = _assignment_for_edit(
        solver_input, key, Physician.objects.get(pk=value["physician_id"])
    )
    assignment["explanation"] = {"reconciled": True}
    return assignment


def effective_proposal_assignments(proposal):
    """Return the editable overlay as draft deltas, leaving solver JSON untouched."""
    changes = _effective_proposal_changes(proposal)
    return (
        tuple(value for key, value in changes.items() if key[0] == "precepting" and value),
        tuple(value for key, value in changes.items() if key[0] == "late_doc" and value),
    )


def proposal_assignment_context(*, proposal, assignment_type, on_date, period_of_day, position):
    """Expose one position through the ordinary assignment-choice projection."""
    solver_input = _current_proposal_input(proposal)
    key = _proposal_edit_key(assignment_type, on_date, period_of_day, position)
    source = _source_position(solver_input, key)
    if source is None:
        raise ValidationError("This position is not part of the proposal range.")
    if assignment_type == "precepting" and source.assignment and source.assignment.is_locked:
        raise ValidationError("A locked Precepting assignment cannot be edited in a proposal.")
    context = {"value": _effective_assignment_value(proposal, key)}
    if assignment_type == "precepting":
        context["alternatives"] = _proposal_preceptor_alternatives(proposal, key, source)
    else:
        candidates = [
            candidate
            for candidate in _source_candidates(solver_input, key)
            if _eligible_for_late_doc(
                candidate, excluded_physician_ids=solver_input.excluded_physician_ids
            )
        ]
        context["candidates"] = _seeded_late_doc_physician_queryset(
            candidates, proposal.solve_settings["solve_seed"]
        )
    return context


def _seeded_physician_queryset(physician_ids, solve_seed):
    ordered_ids = sorted(set(physician_ids), key=lambda item: _seeded_integer(solve_seed, item))
    return _ordered_physician_queryset(ordered_ids)


def _seeded_late_doc_physician_queryset(candidates, solve_seed):
    return _ordered_physician_queryset(
        candidate.physician_id
        for candidate in sorted(candidates, key=lambda item: _candidate_sort_key(item, solve_seed))
    )


def _ordered_physician_queryset(physician_ids):
    ordered_ids = tuple(dict.fromkeys(physician_ids))
    if not ordered_ids:
        return Physician.objects.none()
    return Physician.objects.filter(pk__in=ordered_ids).order_by(
        Case(
            *(When(pk=physician_id, then=index) for index, physician_id in enumerate(ordered_ids)),
            output_field=IntegerField(),
        )
    )


@transaction.atomic
def edit_proposed_assignment(
    *,
    proposal,
    assignment_type,
    on_date,
    period_of_day,
    position,
    physician=None,
    note="",
    scheduler,
    expected_revision,
):
    """Retain one scheduler choice while preserving the solver artifact as baseline evidence."""
    require_scheduler(scheduler)
    proposal = PreceptingSolverProposal.objects.select_for_update().get(pk=proposal.pk)
    _require_proposal_revision(proposal, expected_revision)
    if proposal.applied_at is not None:
        raise ValidationError("An applied solver proposal cannot be edited.")
    if on_date < organization_today():
        raise ValidationError("Past proposed positions cannot be edited.")
    solver_input = _current_proposal_input(proposal)
    key = _proposal_edit_key(assignment_type, on_date, period_of_day, position)
    source = _source_position(solver_input, key)
    if source is None:
        raise ValidationError("This position is not part of the proposal range.")
    if assignment_type == "precepting" and source.assignment and source.assignment.is_locked:
        raise ValidationError("A locked Precepting assignment cannot be edited in a proposal.")
    if physician is not None:
        if assignment_type == "precepting":
            alternatives = _proposal_preceptor_alternatives(proposal, key, source)
            alternative = next(
                (
                    item
                    for item in (alternatives.current, *alternatives.alternatives)
                    if item is not None and item.physician.pk == physician.pk
                ),
                None,
            )
            allowed = (
                physician.pk not in solver_input.excluded_physician_ids
                and alternative is not None
                and alternative.ordinarily_eligible_to_precept
                and (
                    position != "secondary"
                    or alternative.effective_category == EffectiveRole.Role.FACULTY
                )
            )
        else:
            candidate = next(
                (
                    candidate
                    for candidate in _source_candidates(solver_input, key)
                    if candidate.physician_id == physician.pk
                ),
                None,
            )
            allowed = candidate is not None and _eligible_for_late_doc(
                candidate, excluded_physician_ids=solver_input.excluded_physician_ids
            )
        if not allowed:
            raise ValidationError("This physician is no longer eligible for the proposed position.")
        if physician.pk in _effective_session_physician_ids(proposal, key):
            raise ValidationError(
                "This physician already occupies another proposed position in this session."
            )
    previous_value = _effective_assignment_value(proposal, key)
    resulting_value = _assignment_for_edit(solver_input, key, physician) if physician else None
    if _same_assignment(previous_value, resulting_value):
        return proposal
    PreceptingSolverProposalEdit.objects.create(
        proposal=proposal,
        assignment_type=assignment_type,
        date=on_date,
        period_of_day=period_of_day or "",
        position=position,
        previous_value=previous_value,
        resulting_value=resulting_value,
        note=note.strip(),
        edited_by=scheduler,
    )
    _reset_late_doc_review(proposal, key, resulting_value)
    _refresh_effective_proposal(proposal, scheduler, solver_input=solver_input)
    return proposal


def _proposal_preceptor_alternatives(proposal, key, _source):
    """Overlay proposal choices on the persisted session without mutating the draft."""
    session = PreceptorSession.objects.get(date=date.fromisoformat(key[1]), period_of_day=key[2])
    physician_ids = {
        value["physician_id"]
        for position in ("primary-1", "primary-2", "secondary")
        if (
            value := _effective_assignment_value(
                proposal,
                ("precepting", key[1], key[2], position),
            )
        )
    }
    physicians = Physician.objects.in_bulk(physician_ids)
    assignment_overrides = {
        ("secondary", None) if position == "secondary" else ("primary", int(position[-1])): (
            physicians.get(value["physician_id"]) if value else None
        )
        for position in ("primary-1", "primary-2", "secondary")
        for value in (
            _effective_assignment_value(
                proposal,
                ("precepting", key[1], key[2], position),
            ),
        )
    }
    responsibility, primary_position = (
        ("secondary", None) if key[3] == "secondary" else ("primary", int(key[3][-1]))
    )
    alternatives = assignment_alternatives_for_session(
        session,
        responsibility,
        primary_position,
        assignment_overrides=assignment_overrides,
        excluded_physician_ids=proposal.solve_settings.get("excluded_physician_ids", ()),
    )
    solve_seed = proposal.solve_settings["solve_seed"]
    tier_order = {tier.key: index for index, tier in enumerate(alternatives.tiers)}
    ordered = tuple(
        sorted(
            alternatives.alternatives,
            key=lambda item: (
                tier_order[item.tier],
                _seeded_integer(solve_seed, item.physician.pk),
            ),
        )
    )
    return replace(
        alternatives,
        alternatives=ordered,
        tiers=tuple(
            replace(
                tier,
                alternatives=tuple(item for item in ordered if item.tier == tier.key),
            )
            for tier in alternatives.tiers
        ),
    )


def _proposal_edit_key(assignment_type, on_date, period_of_day, position):
    if assignment_type not in {"precepting", "late_doc"}:
        raise ValidationError("Proposal edits must target Precepting or Late Doc work.")
    if not isinstance(on_date, date):
        raise ValidationError("Proposal edits require a valid date.")
    if assignment_type == "late_doc":
        return ("late_doc", on_date.isoformat(), "", "late-doc")
    if period_of_day not in {"AM", "PM"} or position not in {"primary-1", "primary-2", "secondary"}:
        raise ValidationError("Choose a valid Precepting position.")
    return ("precepting", on_date.isoformat(), period_of_day, position)


def _source_position(solver_input, key):
    assignment_type, on_date, period_of_day, position = key
    if assignment_type == "late_doc":
        return next(
            (item for item in solver_input.late_doc_positions if item.date.isoformat() == on_date),
            None,
        )
    session = next(
        (
            item
            for item in solver_input.sessions
            if item.date.isoformat() == on_date and item.period_of_day == period_of_day
        ),
        None,
    )
    return (
        next((item for item in session.positions if item.key == position), None)
        if session
        else None
    )


def _source_candidates(solver_input, key):
    assignment_type, on_date, period_of_day, _ = key
    if assignment_type == "late_doc":
        source = _source_position(solver_input, key)
        return source.candidates if source else ()
    session = next(
        (
            item
            for item in solver_input.sessions
            if item.date.isoformat() == on_date and item.period_of_day == period_of_day
        ),
        None,
    )
    return session.candidates if session else ()


def _baseline_assignment(proposal, key):
    assignment_type, on_date, period_of_day, position = key
    if assignment_type == "late_doc":
        for item in proposal.input_snapshot["late_doc_positions"]:
            if item["date"] == on_date:
                return item["assignment"]
        return None
    for session in proposal.input_snapshot["sessions"]:
        if session["date"] == on_date and session["period_of_day"] == period_of_day:
            return next(
                (item["assignment"] for item in session["positions"] if item["key"] == position),
                None,
            )
    return None


def _original_proposal_assignment(proposal, key):
    assignment_type, on_date, period_of_day, position = key
    for assignment in proposal.proposed_assignments.get(assignment_type, ()):
        if (
            assignment["date"] == on_date
            and assignment.get("period_of_day", "") == period_of_day
            and assignment.get("position", "late-doc") == position
        ):
            return assignment
    return None


def _effective_assignment_value(proposal, key):
    value = _original_proposal_assignment(proposal, key) or _baseline_assignment(proposal, key)
    for edit in proposal.edits.all():
        if (
            edit.assignment_type,
            edit.date.isoformat(),
            edit.period_of_day,
            edit.position,
        ) == key:
            value = edit.resulting_value
    return value


def _reconciliation_proposal_value(proposal, key):
    """Include a reviewed Late Doc replacement without changing its source proposal."""
    value = _effective_assignment_value(proposal, key)
    if key[0] != "late_doc":
        return value
    review = proposal.late_doc_reviews.filter(date=date.fromisoformat(key[1])).first()
    if review and review.decision == PreceptingSolverLateDocReview.Decision.REPLACED:
        return {"physician_id": review.replacement_physician_id}
    return value


def _effective_proposal_changes(proposal):
    changes = {}
    for assignment_type in ("precepting", "late_doc"):
        for assignment in proposal.proposed_assignments.get(assignment_type, ()):
            key = (
                assignment_type,
                assignment["date"],
                assignment.get("period_of_day", ""),
                assignment.get("position", "late-doc"),
            )
            changes[key] = assignment if assignment.get("physician_id") is not None else None
    for edit in proposal.edits.all():
        key = (edit.assignment_type, edit.date.isoformat(), edit.period_of_day, edit.position)
        value = edit.resulting_value
        if _same_assignment(value, _baseline_assignment(proposal, key)):
            changes.pop(key, None)
        else:
            changes[key] = value
    return changes


def _proposal_input_with_changes_removed(solver_input, proposal):
    """Project every proposal delta as an open position for recalculated evidence."""
    changes = set(_effective_proposal_changes(proposal))
    if not changes:
        return solver_input

    removed_precepting_assignment_ids = set()
    removed_late_doc_assignment_ids = set()
    sessions = []
    for session in solver_input.sessions:
        positions = []
        for position in session.positions:
            key = ("precepting", session.date.isoformat(), session.period_of_day, position.key)
            if key in changes:
                if position.assignment:
                    removed_precepting_assignment_ids.add(position.assignment.id)
                positions.append(replace(position, assignment=None))
            else:
                positions.append(position)
        sessions.append(replace(session, positions=tuple(positions)))

    late_doc_positions = []
    for position in solver_input.late_doc_positions:
        key = ("late_doc", position.date.isoformat(), "", "late-doc")
        if key in changes:
            if position.assignment:
                removed_late_doc_assignment_ids.add(position.assignment.id)
            late_doc_positions.append(replace(position, assignment=None))
        else:
            late_doc_positions.append(position)
    return replace(
        solver_input,
        sessions=tuple(sessions),
        late_doc_positions=tuple(late_doc_positions),
        balance_assignments=tuple(
            assignment
            for assignment in solver_input.balance_assignments
            if assignment.id not in removed_precepting_assignment_ids
        ),
        balance_late_doc_assignments=tuple(
            assignment
            for assignment in solver_input.balance_late_doc_assignments
            if assignment.id not in removed_late_doc_assignment_ids
        ),
    )


def _assignment_for_edit(solver_input, key, physician):
    assignment_type, on_date, period_of_day, position = key
    if assignment_type == "late_doc":
        return {
            "date": on_date,
            "position": "late-doc",
            "physician_id": physician.pk,
            "physician_name": physician.display_name,
            "explanation": {"manual": True},
        }
    session = next(
        item
        for item in solver_input.sessions
        if item.date.isoformat() == on_date and item.period_of_day == period_of_day
    )
    return {
        "session_id": session.session_id,
        "date": on_date,
        "period_of_day": period_of_day,
        "position": position,
        "physician_id": physician.pk,
        "physician_name": physician.display_name,
        "explanation": {"manual": True},
    }


def _effective_session_physician_ids(proposal, key):
    if key[0] != "precepting":
        return set()
    return {
        value["physician_id"]
        for position in ("primary-1", "primary-2", "secondary")
        if position != key[3]
        and (
            value := _effective_assignment_value(proposal, ("precepting", key[1], key[2], position))
        )
    }


def _same_assignment(left, right):
    return (left or {}).get("physician_id") == (right or {}).get("physician_id")


def _reset_late_doc_review(proposal, key, resulting_value):
    if key[0] != "late_doc":
        return
    review = proposal.late_doc_reviews.filter(date=date.fromisoformat(key[1])).first()
    baseline = _baseline_assignment(proposal, key)
    baseline_physician_id = baseline.get("physician_id") if baseline else None
    if _same_assignment(resulting_value, baseline):
        if review:
            review.delete()
        return
    if review is None:
        review = PreceptingSolverLateDocReview(
            proposal=proposal,
            date=date.fromisoformat(key[1]),
            proposed_physician_id=baseline_physician_id,
        )
    if resulting_value is not None:
        review.proposed_physician_id = resulting_value["physician_id"]
    review.decision = PreceptingSolverLateDocReview.Decision.PENDING
    review.replacement_physician = None
    review.reviewed_by = None
    review.reviewed_at = None
    review.save()


def _require_proposal_revision(proposal, expected_revision):
    if expected_revision is None:
        raise ValidationError("A proposal revision is required.")
    if expected_revision != proposal.revision:
        raise ValidationError(
            "This proposal changed while you were editing it. Your work was not saved; review the refreshed proposal."
        )


def _refresh_effective_proposal(proposal, scheduler, *, solver_input=None):
    solver_input = solver_input or _current_proposal_input(proposal)
    _recalculate_effective_proposal(proposal, solver_input)
    proposal.revision += 1
    proposal.last_edited_by = scheduler
    proposal.save(
        update_fields=(
            "after_findings",
            "workload_effects",
            "compromises",
            "revision",
            "last_edited_by",
            "updated_at",
        )
    )


def _recalculate_effective_proposal(proposal, solver_input):
    proposal.after_findings = _proposal_after_findings(proposal, solver_input=solver_input)
    effective_solver_input = _proposal_input_with_changes_removed(solver_input, proposal)
    adapter = _CpSatFillAdapter(effective_solver_input)
    candidates = {
        (
            requirement.date.isoformat(),
            requirement.period_of_day or "",
            requirement.position,
        ): requirement
        for requirement in adapter.requirements
    }
    selected = {}
    precepting_assignments, late_doc_assignments = effective_proposal_assignments(proposal)
    for assignment in (*precepting_assignments, *late_doc_assignments):
        key = (
            assignment["date"],
            assignment.get("period_of_day", ""),
            assignment.get("position", "late-doc"),
        )
        requirement = candidates.get(key)
        if requirement:
            candidate = next(
                (
                    item
                    for item in _source_candidates(
                        solver_input,
                        (
                            "late_doc" if key[1] == "" else "precepting",
                            key[0],
                            key[1],
                            key[2],
                        ),
                    )
                    if item.physician_id == assignment["physician_id"]
                ),
                None,
            )
            if candidate:
                selected[requirement] = candidate
    summary = _compromise_summary(effective_solver_input, selected, adapter._unresolved(selected))
    proposal.workload_effects = {
        "faculty": _json_value(summary.faculty_workloads),
        "late_doc": _json_value(summary.late_doc_workloads),
    }
    proposal.compromises = _json_value(summary)


@scheduling_mutation
def apply_precepting_solver_proposal(
    *,
    proposal,
    scheduler,
    confirm_hard_findings=False,
    expected_revision=None,
    hard_finding_reason="",
    selected_hard_finding_keys=(),
):
    """Atomically apply a fully reviewed current proposal through normal draft services."""
    require_scheduler(scheduler)
    proposal = PreceptingSolverProposal.objects.select_for_update().get(pk=proposal.pk)
    if proposal.applied_at is not None:
        return proposal

    if confirm_hard_findings:
        _require_proposal_revision(proposal, expected_revision)

    solver_input = _current_proposal_input(proposal)
    _validate_current_proposal(proposal, solver_input)
    precepting_assignments, late_doc_assignments = effective_proposal_assignments(proposal)
    effective_changes = _effective_proposal_changes(proposal)
    late_doc_physicians = _reviewed_late_doc_physicians(proposal, effective_changes)
    current_choices = _current_solver_choices(solver_input)
    _validate_stored_choices(precepting_assignments, late_doc_assignments, current_choices)
    _validate_review_replacements(late_doc_physicians, current_choices)
    _validate_effective_changes(solver_input, effective_changes)
    projected_findings = _proposal_projected_findings(proposal, solver_input=solver_input)
    hard_finding_objects = tuple(
        finding for finding in projected_findings if finding.severity == "hard"
    )
    hard_findings = tuple(_finding_snapshot(finding) for finding in hard_finding_objects)
    from .finding_acknowledgements import acknowledge_finding

    hard_finding_keys = tuple(_finding_selection_key(finding) for finding in hard_finding_objects)
    if hard_findings and not confirm_hard_findings:
        raise HardFindingConfirmationRequired(hard_findings, hard_finding_keys)
    selected_hard_finding_keys = tuple(selected_hard_finding_keys)
    if hard_findings:
        hard_finding_reason = normalize_note(hard_finding_reason)
        if not hard_finding_reason:
            raise ValidationError(
                {"reason": "A short reason is required to apply with hard findings."}
            )
        if len(hard_finding_reason) > 500:
            raise ValidationError({"reason": "Keep the reason to 500 characters or fewer."})
        if len(set(selected_hard_finding_keys)) != len(selected_hard_finding_keys) or not set(
            selected_hard_finding_keys
        ).issubset(hard_finding_keys):
            raise ValidationError(
                "The selected hard findings changed. Review the refreshed proposal before applying."
            )
    elif selected_hard_finding_keys:
        raise ValidationError("This proposal has no hard findings to acknowledge.")
    else:
        hard_finding_reason = ""

    from .late_docs import assign_late_doc, remove_late_doc_assignment
    from .preceptors import (
        _assignment_slot_from_position,
        assign_preceptor,
        remove_preceptor_assignment,
    )

    reason = f"Applied Precepting solver proposal {proposal.pk}."
    for key, assignment in effective_changes.items():
        assignment_type, on_date, period_of_day, position = key
        if assignment_type == "precepting":
            session = next(
                session
                for session in solver_input.sessions
                if session.date.isoformat() == on_date and session.period_of_day == period_of_day
            )
            responsibility, primary_position = _assignment_slot_from_position(position)
            current = next(
                (
                    item.assignment
                    for item in session.positions
                    if item.key == position and item.assignment is not None
                ),
                None,
            )
            if assignment:
                assign_preceptor(
                    session=PreceptorSession.objects.get(pk=session.session_id),
                    physician=Physician.objects.get(pk=assignment["physician_id"]),
                    responsibility=responsibility,
                    primary_position=primary_position,
                    scheduler=scheduler,
                    reason=reason,
                )
            elif current:
                remove_preceptor_assignment(
                    assignment=PreceptorSession.objects.get(pk=session.session_id).assignments.get(
                        responsibility=responsibility, primary_position=primary_position
                    ),
                    scheduler=scheduler,
                    reason=reason,
                )
        else:
            current = LateDocAssignment.objects.filter(date=date.fromisoformat(on_date)).first()
            physician_id = late_doc_physicians[on_date]
            if physician_id is not None:
                assign_late_doc(
                    on_date=date.fromisoformat(on_date),
                    physician=Physician.objects.get(pk=physician_id),
                    scheduler=scheduler,
                    note=reason,
                )
            elif current:
                remove_late_doc_assignment(assignment=current, scheduler=scheduler, note=reason)
    proposal.applied_by = scheduler
    proposal.applied_at = timezone.now()
    proposal.applied_reason = hard_finding_reason
    proposal.save(update_fields=("applied_by", "applied_at", "applied_reason"))

    actual_findings = _current_proposal_findings(proposal)
    actual_by_key = {_finding_selection_key(finding): finding for finding in actual_findings}
    if set(actual_by_key) != set(hard_finding_keys):
        raise ValidationError(
            "The applied draft findings changed. Review the refreshed proposal before applying."
        )
    for finding_key in selected_hard_finding_keys:
        acknowledge_finding(
            finding=actual_by_key[finding_key], scheduler=scheduler, reason=hard_finding_reason
        )
    return proposal


def _current_proposal_input(proposal):
    result = preflight_precepting_solve(
        SolveDateRange(proposal.solve_start_date, proposal.solve_end_date),
        balance_window=SolveDateRange(proposal.balance_start_date, proposal.balance_end_date),
    )
    if result.blockers:
        raise ValidationError("The solver proposal is no longer valid: preflight is blocked.")
    if result.solver_input.fingerprint != proposal.input_fingerprint:
        raise ValidationError("The solver proposal is stale and cannot be applied.")
    return without_excluded_physicians(
        result.solver_input, proposal.solve_settings.get("excluded_physician_ids", ())
    )


def without_excluded_physicians(solver_input, physician_ids):
    """Record exclusions without removing fixed-assignment metadata from the projection."""
    return replace(solver_input, excluded_physician_ids=tuple(sorted(set(physician_ids))))


def _validate_current_proposal(proposal, solver_input):
    if proposal.mode not in PreceptingSolverProposal.Mode.values:
        raise ValidationError("This solver proposal mode cannot be applied.")
    if solver_input.solve_range.start_date < organization_today():
        raise ValidationError("The solver proposal includes past dates and cannot be applied.")


def _reviewed_late_doc_physicians(proposal, effective_changes):
    reviews = {
        review.date.isoformat(): review for review in proposal.late_doc_reviews.select_for_update()
    }
    late_doc_changes = {
        key[1]: value for key, value in effective_changes.items() if key[0] == "late_doc"
    }
    stored_dates = set(late_doc_changes)
    if set(reviews) != stored_dates:
        raise ValidationError("The solver proposal's Late Doc reviews are incomplete.")
    physicians = {}
    for on_date, review in reviews.items():
        if review.decision == PreceptingSolverLateDocReview.Decision.CONFIRMED:
            assignment = late_doc_changes[on_date]
            physicians[on_date] = assignment["physician_id"] if assignment else None
        elif review.decision == PreceptingSolverLateDocReview.Decision.REPLACED:
            physicians[on_date] = review.replacement_physician_id
        else:
            raise ValidationError(
                "Every proposed Late Doc assignment must be confirmed or replaced."
            )
    return physicians


def _current_solver_choices(solver_input):
    choices = {
        (requirement.date.isoformat(), requirement.period_of_day, requirement.position): {
            candidate.physician_id for candidate in requirement.candidates
        }
        for requirement in _CpSatFillAdapter(solver_input).requirements
    }
    for session in solver_input.sessions:
        for position in session.positions:
            if position.assignment and position.assignment.is_locked:
                continue
            choices[(session.date.isoformat(), session.period_of_day, position.key)] = {
                candidate.physician_id
                for candidate in position.candidates or session.candidates
                if _eligible_for_manual_preceptor_position(
                    candidate,
                    position,
                    excluded_physician_ids=solver_input.excluded_physician_ids,
                )
                or (
                    position.assignment
                    and candidate.physician_id == position.assignment.physician_id
                    and candidate.physician_id not in solver_input.excluded_physician_ids
                )
            }
    for position in solver_input.late_doc_positions:
        choices[(position.date.isoformat(), None, "late-doc")] = {
            candidate.physician_id
            for candidate in position.candidates
            if _eligible_for_late_doc(
                candidate, excluded_physician_ids=solver_input.excluded_physician_ids
            )
        }
    return choices


def _eligible_for_manual_preceptor_position(candidate, position, *, excluded_physician_ids=()):
    """Manual proposal choices retain ordinary hard findings instead of excluding them."""
    if (
        candidate.physician_id in excluded_physician_ids
        or not candidate.ordinarily_eligible_to_precept
        or candidate.precepting_conflict_codes
    ):
        return False
    return position.key != "secondary" or candidate.is_faculty


def _validate_stored_choices(precepting_assignments, late_doc_assignments, current_choices):
    for assignment in precepting_assignments:
        choice = (assignment["date"], assignment["period_of_day"], assignment["position"])
        if assignment["physician_id"] not in current_choices.get(choice, set()):
            raise ValidationError("A proposed Precepting assignment is no longer valid.")
    for assignment in late_doc_assignments:
        choice = (assignment["date"], None, "late-doc")
        if assignment["physician_id"] not in current_choices.get(choice, set()):
            raise ValidationError("A proposed Late Doc assignment is no longer valid.")


def _validate_review_replacements(physicians, current_choices):
    for on_date, physician_id in physicians.items():
        if physician_id is None:
            continue
        choice = (on_date, None, "late-doc")
        if physician_id not in current_choices.get(choice, set()):
            raise ValidationError("A replacement Late Doc assignment is no longer valid.")


def _validate_effective_changes(solver_input, effective_changes):
    """Defend the Apply boundary even if retained proposal data was forged or stale."""
    for key in effective_changes:
        assignment_type, on_date, _period_of_day, position = key
        if date.fromisoformat(on_date) < organization_today():
            raise ValidationError("Past proposed positions cannot be applied.")
        if assignment_type != "precepting":
            continue
        source = _source_position(solver_input, key)
        if source is None:
            raise ValidationError("A proposed Precepting position is no longer valid.")
        if source.assignment and source.assignment.is_locked:
            raise ValidationError(
                "A locked Precepting assignment cannot be applied from a proposal."
            )
    for session in solver_input.sessions:
        physician_ids = set()
        for position in session.positions:
            key = ("precepting", session.date.isoformat(), session.period_of_day, position.key)
            assignment = effective_changes.get(key)
            if assignment is None and key not in effective_changes and position.assignment:
                assignment = {"physician_id": position.assignment.physician_id}
            if assignment is None:
                continue
            if assignment["physician_id"] in physician_ids:
                raise ValidationError(
                    "A physician can occupy only one proposed position in a session."
                )
            physician_ids.add(assignment["physician_id"])


def _finding_snapshot(finding):
    return {
        "code": finding.code,
        "severity": finding.severity,
        "date": (finding.week or finding.session.date).isoformat(),
        "period_of_day": finding.session.period_of_day if finding.session else None,
        "physician_ids": [physician.pk for physician in finding.physicians],
        "explanation": _json_value(finding.explanation),
    }


def _finding_selection_key(finding):
    """Identify a projected finding across the assignment rows created by Apply."""
    snapshot = _finding_snapshot(finding)
    explanation = {
        key: value
        for key, value in snapshot["explanation"].items()
        if key not in {"assignment_id", "preceptor_assignment_id"}
    }
    return json.dumps(
        {
            **snapshot,
            "explanation": explanation,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _proposal_finding_snapshots(solver_input, fill_proposal):
    """Read the ordinary finding policy before and after a proposed draft overlay."""
    weeks = rounding_weeks(solver_input.solve_range.start_date, solver_input.solve_range.end_date)
    from .preceptors import projected_draft_findings_for_weeks

    before = projected_draft_findings_for_weeks(weeks)
    after = _projected_finding_snapshots(
        solver_input,
        precepting_assignments=fill_proposal.assignments,
        late_doc_assignments=fill_proposal.late_doc_assignments,
        precepting_removals=tuple(
            ("precepting", item.date.isoformat(), item.period_of_day, item.position)
            for item in fill_proposal.precepting_removals
        ),
        late_doc_removals=tuple(
            ("late_doc", item.date.isoformat(), "", item.position)
            for item in fill_proposal.late_doc_removals
        ),
    )
    return (
        _scoped_finding_snapshots(solver_input, before.findings),
        after,
    )


def _proposal_after_findings(proposal, *, solver_input=None):
    return [
        _finding_snapshot(finding)
        for finding in _proposal_projected_findings(proposal, solver_input=solver_input)
    ]


def _proposal_projected_findings(proposal, *, solver_input=None):
    solver_input = (
        solver_input
        or preflight_precepting_solve(
            SolveDateRange(proposal.solve_start_date, proposal.solve_end_date),
            balance_window=SolveDateRange(proposal.balance_start_date, proposal.balance_end_date),
        ).solver_input
    )
    reviews = {review.date.isoformat(): review for review in proposal.late_doc_reviews.all()}
    changes = _effective_proposal_changes(proposal)
    precepting_assignments, _ = effective_proposal_assignments(proposal)
    late_doc_assignments = []
    late_doc_removals = []
    for key, assignment in changes.items():
        if key[0] != "late_doc":
            continue
        review = reviews.get(key[1])
        physician_id = (
            review.replacement_physician_id
            if review and review.decision == PreceptingSolverLateDocReview.Decision.REPLACED
            else assignment["physician_id"]
            if assignment
            else None
        )
        if physician_id is None:
            late_doc_removals.append(key)
        else:
            late_doc_assignments.append({"date": key[1], "physician_id": physician_id})
    return _projected_draft_findings(
        solver_input,
        precepting_assignments=precepting_assignments,
        late_doc_assignments=late_doc_assignments,
        precepting_removals=tuple(
            key for key, value in changes.items() if key[0] == "precepting" and value is None
        ),
        late_doc_removals=tuple(late_doc_removals),
    )


def _projected_finding_snapshots(
    solver_input,
    *,
    precepting_assignments,
    late_doc_assignments,
    precepting_removals=(),
    late_doc_removals=(),
):
    return [
        _finding_snapshot(finding)
        for finding in _projected_draft_findings(
            solver_input,
            precepting_assignments=precepting_assignments,
            late_doc_assignments=late_doc_assignments,
            precepting_removals=precepting_removals,
            late_doc_removals=late_doc_removals,
        )
    ]


def _projected_draft_findings(
    solver_input,
    *,
    precepting_assignments,
    late_doc_assignments,
    precepting_removals=(),
    late_doc_removals=(),
):
    from .preceptors import _assignment_slot_from_position, projected_draft_findings_for_weeks

    def value(assignment, name):
        if hasattr(assignment, name):
            return getattr(assignment, name)
        return assignment[name]

    precepting_assignments = tuple(precepting_assignments)
    late_doc_assignments = tuple(late_doc_assignments)
    physician_ids = {
        value(assignment, "physician_id")
        for assignment in precepting_assignments + late_doc_assignments
    }
    physicians = Physician.objects.in_bulk(physician_ids)
    assignment_overrides = {}
    for _, on_date, period_of_day, position in precepting_removals:
        session = next(
            session
            for session in solver_input.sessions
            if session.date.isoformat() == on_date and session.period_of_day == period_of_day
        )
        responsibility, primary_position = _assignment_slot_from_position(position)
        assignment_overrides[(session.session_id, responsibility, primary_position)] = None
    for assignment in precepting_assignments:
        responsibility, primary_position = _assignment_slot_from_position(
            value(assignment, "position")
        )
        assignment_overrides[
            (value(assignment, "session_id"), responsibility, primary_position)
        ] = physicians[value(assignment, "physician_id")]
    late_doc_overrides = {
        date.fromisoformat(on_date): None for _, on_date, _, _ in late_doc_removals
    }
    for assignment in late_doc_assignments:
        on_date = value(assignment, "date")
        late_doc_overrides[date.fromisoformat(on_date) if isinstance(on_date, str) else on_date] = (
            physicians[value(assignment, "physician_id")]
        )
    findings = projected_draft_findings_for_weeks(
        rounding_weeks(solver_input.solve_range.start_date, solver_input.solve_range.end_date),
        assignment_overrides=assignment_overrides,
        late_doc_overrides=late_doc_overrides,
    )
    return _scoped_findings(solver_input, findings.findings)


def _current_proposal_findings(proposal):
    from .preceptors import projected_draft_findings_for_weeks

    findings = projected_draft_findings_for_weeks(
        rounding_weeks(proposal.solve_start_date, proposal.solve_end_date)
    )
    return tuple(
        finding
        for finding in _scoped_findings_for_dates(proposal, findings.findings)
        if finding.severity == "hard"
    )


def _scoped_finding_snapshots(solver_input, findings):
    return [_finding_snapshot(finding) for finding in _scoped_findings(solver_input, findings)]


def _scoped_findings(solver_input, findings):
    return tuple(
        finding
        for finding in findings
        if finding.week is not None
        or solver_input.solve_range.start_date
        <= finding.session.date
        <= solver_input.solve_range.end_date
    )


def _scoped_findings_for_dates(proposal, findings):
    return tuple(
        finding
        for finding in findings
        if finding.week is not None
        or proposal.solve_start_date <= finding.session.date <= proposal.solve_end_date
    )


def _json_value(value):
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value
