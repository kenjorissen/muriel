"""Preceptor draft, assignment, alternatives, findings, and history services."""

from dataclasses import dataclass, field
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse

from ..models.availability_categories import CustomAvailabilityCategory
from ..models.late_docs import LateDocAssignment
from ..models.physicians import EffectiveRole, FacultyAvailabilityTemplate, Physician
from ..models.preceptors import (
    PreceptorAssignment,
    PreceptorAssignmentChange,
    PreceptorSession,
    SchedulePeriod,
)
from . import availability as _availability
from . import physicians as _physicians
from . import rounding as _rounding
from ._mutation import (
    _serialized_scheduling_mutation,
    capture_calendar_range,
    normalize_note,
    require_scheduler,
    scheduling_mutation,
)
from .finding_acknowledgements import readiness_for_findings
from .holidays import holiday_closure_for, holiday_projections_for_dates
from .lead_times import lead_time_months
from .operational_attention import is_operational_date, is_operational_preceptor_week
from .slot_projection import RequestedSlot, slot_projection
from .work_remaining import (
    InclusiveDateRange,
    ProjectedWorkItem,
    ProjectedWorkReason,
    WorkSummaryType,
    focused_range_has_membership,
    shift_date_months,
)


def initialize_preceptor_periods(*months: date) -> tuple[SchedulePeriod, ...]:
    """Idempotently create the requested monthly periods and weekday sessions."""
    session_dates = []
    for month_start in dict.fromkeys(month.replace(day=1) for month in months):
        day = month_start
        while day.month == month_start.month:
            if day.weekday() < 5:
                session_dates.append(day)
            day += timedelta(days=1)
    return _ensure_preceptor_sessions(session_dates)


def initialize_preceptor_session_range(
    start_date: date, end_date: date
) -> tuple[SchedulePeriod, ...]:
    """Idempotently create weekday sessions only in the requested inclusive range."""
    if end_date < start_date:
        raise ValidationError("Preceptor session range end date cannot be before its start date.")
    return _ensure_preceptor_sessions(
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
        if (start_date + timedelta(days=offset)).weekday() < 5
    )


def ensure_precepting_horizon_sessions(week_starts) -> tuple[SchedulePeriod, ...]:
    """Idempotently create the weekday sessions for complete Precepting workweeks."""
    session_dates = [
        week_start + timedelta(days=offset)
        for week_start in dict.fromkeys(work_week_start(week_start) for week_start in week_starts)
        for offset in range(5)
    ]
    return _ensure_preceptor_sessions(session_dates)


def _ensure_preceptor_sessions(session_dates) -> tuple[SchedulePeriod, ...]:
    """Create structural periods and AM/PM sessions for the specified weekdays."""
    dates = tuple(dict.fromkeys(session_dates))
    month_starts = tuple(dict.fromkeys(day.replace(day=1) for day in dates))
    periods = []
    with _serialized_scheduling_mutation():
        for month_start in month_starts:
            period, _ = SchedulePeriod.objects.get_or_create(month=month_start)
            periods.append(period)
        periods_by_month = {period.month: period for period in periods}
        for day in dates:
            for period_of_day in PreceptorSession.PeriodOfDay.values:
                PreceptorSession.objects.get_or_create(
                    period=periods_by_month[day.replace(day=1)],
                    date=day,
                    period_of_day=period_of_day,
                )
    return tuple(periods)


def open_monthly_draft(month: date) -> SchedulePeriod:
    """Return a month's draft after ensuring every weekday AM/PM session exists."""
    return initialize_preceptor_periods(month)[0]


def work_week_start(on_date: date) -> date:
    """Normalize a date to the Monday of its authoritative work week."""
    return on_date - timedelta(days=on_date.weekday())


def open_weekly_draft(on_date: date) -> tuple[PreceptorSession, ...]:
    """Ensure and return the ten weekday sessions for the week containing a date."""
    week_start = work_week_start(on_date)
    week_end = week_start + timedelta(days=4)
    initialize_preceptor_periods(week_start, week_end)
    return tuple(
        PreceptorSession.objects.filter(date__range=(week_start, week_end)).order_by(
            "date", "period_of_day"
        )
    )


@dataclass(frozen=True)
class SessionAssignmentStatus:
    assignment: PreceptorAssignment
    physician: Physician
    responsibility: str
    primary_position: int | None
    is_locked: bool
    ordinarily_eligible_to_precept: bool
    preceptor_reason: str | None
    faculty_equivalent: bool
    faculty_equivalent_reason: str | None


@dataclass(frozen=True)
class _ProjectedAssignment:
    """A read-only assignment overlay used when evaluating a proposed draft."""

    pk: int | None
    physician: Physician
    responsibility: str
    primary_position: int | None
    is_locked: bool

    @property
    def physician_id(self):
        return self.physician.pk


@dataclass(frozen=True)
class _ProjectedLateDocAssignment:
    pk: None
    physician: Physician

    @property
    def physician_id(self):
        return self.physician.pk


ASSIGNMENT_ALTERNATIVE_TIER_LABELS = {
    "available_expected_community": "Available expected Community Preceptors",
    "available_other": "Other available physicians",
    "available_soft_academic_event": "Available with a soft Academic Event",
    "backup_only": "Backup-only physicians",
    "available_as_float": "Available as Float",
    "not_available": "Hard-conflict or not-available physicians",
}
ASSIGNMENT_ALTERNATIVE_TIER_ORDER = tuple(ASSIGNMENT_ALTERNATIVE_TIER_LABELS)


@dataclass(frozen=True)
class CandidateAssessmentReason:
    """One stable, display-ready fact about a Precepting candidate."""

    code: str
    effect: str
    severity: str | None
    summary: str
    detail: str
    source_type: str | None = None
    source_id: int | str | None = None

    @property
    def source_identity(self):
        if self.source_type is None:
            return None
        return (self.source_type, self.source_id)


@dataclass(frozen=True)
class CandidateAssessment:
    """Immutable position-aware eligibility and explanation for one physician."""

    physician: Physician
    session: PreceptorSession
    responsibility: str
    primary_position: int | None
    automatic_eligible: bool
    manually_selectable: bool
    reasons: tuple[CandidateAssessmentReason, ...]
    effective_category: str | None
    ordinarily_eligible_to_precept: bool
    preceptor_reason: str | None
    faculty_equivalent: bool
    faculty_equivalent_reason: str | None
    is_applicable: bool
    availability: str | None
    base_availability: str | None
    is_expected_community_commitment: bool
    sources: tuple[_availability.EffectiveAvailabilitySource, ...]
    has_soft_academic_event: bool
    manual_float_eligible: bool

    @property
    def has_hard_reason(self):
        return any(reason.severity == "hard" for reason in self.reasons)


@dataclass(frozen=True)
class AssignmentAlternative:
    assessment: CandidateAssessment
    physician: Physician
    effective_category: str | None
    ordinarily_eligible_to_precept: bool
    preceptor_reason: str | None
    faculty_equivalent: bool
    faculty_equivalent_reason: str | None
    is_applicable: bool
    availability: str | None
    base_availability: str | None
    is_expected_community_commitment: bool
    sources: tuple[_availability.EffectiveAvailabilitySource, ...]
    tier: str
    tier_label: str
    availability_reason: str
    selection_consequence: str | None = None
    assignment: PreceptorAssignment | None = None
    responsibility: str | None = None
    primary_position: int | None = None
    is_locked: bool | None = None

    @property
    def reasons(self):
        return self.assessment.reasons

    @property
    def automatic_eligible(self):
        return self.assessment.automatic_eligible

    @property
    def manually_selectable(self):
        return self.assessment.manually_selectable

    @property
    def manual_float_eligible(self):
        return self.assessment.manual_float_eligible

    @property
    def status_text(self):
        return self.availability_reason

    @property
    def effective_availability(self):
        return self.availability

    @property
    def state(self):
        return self.availability

    @property
    def base_state(self):
        return self.base_availability

    @property
    def role_eligible(self):
        return self.ordinarily_eligible_to_precept

    @property
    def role_eligibility_reason(self):
        return self.preceptor_reason

    @property
    def category(self):
        return self.effective_category

    @property
    def presentation_tier(self):
        return self.tier

    @property
    def effective_category_display(self):
        if self.effective_category is None:
            return "No effective preceptor category"
        return EffectiveRole.Role(self.effective_category).label

    @property
    def availability_display(self):
        if not self.ordinarily_eligible_to_precept:
            return "Not ordinarily eligible to precept"
        if not self.is_applicable:
            return "Not applicable"
        if self.availability is None:
            return "No effective availability state"
        if self.manual_float_eligible:
            return "Available as Float"
        return {
            "available": "Available",
            "backup_only": "Backup only",
            "not_available": "Not available",
        }[self.availability]

    @property
    def base_availability_display(self):
        if not self.is_applicable:
            return "Not applicable"
        if self.base_availability is None:
            return "No preceptor base schedule applies"
        if self.base_availability in FacultyAvailabilityTemplate.AvailabilityState.values:
            return FacultyAvailabilityTemplate.AvailabilityState(self.base_availability).label
        return CustomAvailabilityCategory.Impact(self.base_availability).label

    @property
    def faculty_equivalent_display(self):
        return "Yes" if self.faculty_equivalent else "No"

    @property
    def expected_display(self):
        return (
            "Expected Community Preceptor"
            if self.is_expected_community_commitment
            else "No expected commitment"
        )

    @property
    def expected_community(self):
        return self.is_expected_community_commitment

    @property
    def expected_community_commitment(self):
        return self.is_expected_community_commitment

    @property
    def expected_but_blocked(self):
        return self.is_expected_community_commitment and self.availability == "not_available"

    @property
    def availability_sources(self):
        return self.sources

    @property
    def contributing_sources(self):
        return self.sources

    @property
    def source_explanations(self):
        return tuple(
            {
                "source_type": source.source_type,
                "source_id": source.source_id,
                "label": source.label,
                "contribution": source.contribution,
                "reason": source.reason,
            }
            for source in self.sources
        )

    @property
    def explanation(self):
        return {
            "eligibility": {
                "eligible": self.ordinarily_eligible_to_precept,
                "reason": self.preceptor_reason,
            },
            "faculty_equivalent": {
                "qualified": self.faculty_equivalent,
                "reason": self.faculty_equivalent_reason,
            },
            "availability": {
                "state": self.availability,
                "base_state": self.base_availability,
                "reason": self.availability_reason,
                "sources": self.source_explanations,
            },
            "expected_community": self.is_expected_community_commitment,
            "tier": self.tier,
        }


@dataclass(frozen=True)
class AssignmentAlternativeTier:
    key: str
    label: str
    alternatives: tuple[AssignmentAlternative, ...]


@dataclass(frozen=True)
class AssignmentAlternatives:
    session: PreceptorSession
    responsibility: str
    primary_position: int | None
    current: AssignmentAlternative | None
    alternatives: tuple[AssignmentAlternative, ...]
    tiers: tuple[AssignmentAlternativeTier, ...]

    @property
    def current_state(self):
        return self.current

    @property
    def current_assignment(self):
        return self.current

    @property
    def candidates(self):
        return self.alternatives

    @property
    def alternatives_by_tier(self):
        return {tier.key: tier.alternatives for tier in self.tiers}

    @property
    def tier_groups(self):
        return self.tiers

    def __iter__(self):
        return iter(self.alternatives)

    def __len__(self):
        return len(self.alternatives)

    def __getitem__(self, index):
        return self.alternatives[index]


AssignmentAlternativesResult = AssignmentAlternatives
AssignmentCandidate = AssignmentAlternative


@dataclass(frozen=True)
class DraftFinding:
    code: str
    severity: str
    session: PreceptorSession | None
    week: date | None
    physicians: tuple[Physician, ...]
    explanation: dict
    sources: tuple[_availability.EffectiveAvailabilitySource, ...] = ()
    identity_values: dict = field(default_factory=dict)

    @property
    def is_blocking(self):
        return self.severity == "hard"

    @property
    def message(self):
        return draft_finding_message(self.code, self.explanation)

    @property
    def label(self):
        return draft_finding_label(self.code)

    @property
    def identity(self):
        from .finding_acknowledgements import finding_identity

        return finding_identity(self)


_DRAFT_FINDING_DEFINITIONS = {
    "missing_primary_position": (
        "Unfilled",
        "Required Preceptor position is unfilled.",
    ),
    "missing_primary_coverage": (
        "Unfilled",
        "One or both required Preceptor positions are unfilled.",
    ),
    "insufficient_faculty_equivalent_coverage": (
        "Too few faculty-equivalents",
        "Fewer than two assigned physicians are faculty-equivalent.",
    ),
    "role_ineligible_assignment": (
        "Ineligible role",
        "Assigned physician has no effective preceptor role on this date.",
    ),
    "role_responsibility_mismatch": (
        "Wrong position",
        "Community Preceptor assignments must be Primary.",
    ),
    "not_available_assignment": (
        "Unavailable",
        "Assigned physician is not available for this session.",
    ),
    "backup_only_assignment": (
        "Backup only",
        "Assigned physician is available as backup only for this session.",
    ),
    "soft_academic_event_assignment": (
        "Soft Academic Event",
        "Assigned physician has a soft Academic Event during this session.",
    ),
    "missing_normal_rounder": (
        "No normal rounder",
        "No normal rounder is recorded for the corresponding rounding week.",
    ),
    "unassigned_community_commitment": (
        "Unassigned community",
        "Expected available Community Preceptor is not assigned.",
    ),
    "interview_assignment_conflict": (
        "Interview conflict",
        "Assigned physician is also assigned to the AM Interview Schedule.",
    ),
    "program_director_interview_commitment": (
        "Program Director Interview",
        "Program Director is committed to the AM Interview Schedule.",
    ),
    "missing_late_doc": ("Unfilled Late Doc", "Monday Late Doc assignment is unfilled."),
    "role_ineligible_late_doc": (
        "Ineligible role",
        "Late Doc physician has no effective Faculty role on Monday.",
    ),
    "not_available_late_doc": (
        "Unavailable",
        "Late Doc physician is not available Monday PM.",
    ),
    "backup_only_late_doc": (
        "Backup only",
        "Late Doc physician is available as backup only Monday PM.",
    ),
    "soft_academic_event_late_doc": (
        "Soft Academic Event",
        "Late Doc physician has a soft Academic Event Monday PM.",
    ),
    "holiday_closed_assignment": (
        "Closed holiday",
        "Clinic holiday closes this Precepting session while an assignment remains.",
    ),
    "holiday_closed_late_doc_assignment": (
        "Closed holiday",
        "Clinic holiday closes Monday Late Doc while an assignment remains.",
    ),
}


def draft_finding_label(code):
    """Return the compact label owned by the scheduling finding definition."""
    return _DRAFT_FINDING_DEFINITIONS[code][0]


def draft_finding_message(code, explanation=None):
    """Use one scheduling-language message for live and retained findings."""
    if code == "missing_primary_position":
        position = (explanation or {}).get("position", "")
        if position.startswith("primary-"):
            return f"Preceptor {position.removeprefix('primary-')} is unfilled."
    return _DRAFT_FINDING_DEFINITIONS[code][1]


_PRECEPTING_SUMMARY_TYPES = {
    "missing_primary_position": WorkSummaryType(
        key="unfilled-position",
        singular_label="unfilled position",
        plural_label="unfilled positions",
    ),
    "missing_late_doc": WorkSummaryType(
        key="unfilled-late-doc",
        singular_label="unfilled Late Doc",
        plural_label="unfilled Late Docs",
    ),
}

_PRECEPTING_HARD_FINDING_ORDER = (
    "missing_primary_position",
    "missing_late_doc",
    *(code for code in _DRAFT_FINDING_DEFINITIONS if code not in _PRECEPTING_SUMMARY_TYPES),
)


def _precepting_hard_finding_summary_type(code):
    """Give every hard finding a stable aggregate identity outside templates."""
    if code in _PRECEPTING_SUMMARY_TYPES:
        return _PRECEPTING_SUMMARY_TYPES[code]
    label = draft_finding_label(code).lower()
    return WorkSummaryType(
        key=f"precepting-finding-{code}",
        singular_label=f"{label} finding",
        plural_label=f"{label} findings",
    )


PRECEPTING_FINDING_WORK_SUMMARY_TYPE_KEYS = frozenset(
    _precepting_hard_finding_summary_type(code).key for code in _DRAFT_FINDING_DEFINITIONS
)


@dataclass(frozen=True)
class DraftFindingResult:
    findings: tuple[DraftFinding, ...]

    @property
    def hard_total(self):
        return sum(finding.severity == "hard" for finding in self.findings)

    @property
    def preference_total(self):
        return sum(finding.severity == "preference" for finding in self.findings)

    @property
    def hard_count(self):
        return self.hard_total

    @property
    def preference_count(self):
        return self.preference_total

    @property
    def has_blocking_findings(self):
        return self.hard_total > 0

    @property
    def blocking(self):
        return self.has_blocking_findings


def session_assignments(session):
    assignments = list(
        session.assignments.select_related("physician").order_by(
            "responsibility", "primary_position", "pk"
        )
    )
    primary = [assignment for assignment in assignments if assignment.responsibility == "primary"]
    secondary = [
        assignment for assignment in assignments if assignment.responsibility == "secondary"
    ]
    ordered = sorted(primary, key=lambda assignment: assignment.primary_position) + secondary
    return tuple(
        SessionAssignmentStatus(
            assignment=assignment,
            physician=assignment.physician,
            responsibility=assignment.responsibility,
            primary_position=assignment.primary_position,
            is_locked=assignment.is_locked,
            ordinarily_eligible_to_precept=(
                status := _physicians.physicians_status_on(assignment.physician, session.date)
            ).ordinarily_eligible_to_precept,
            preceptor_reason=status.preceptor_reason,
            faculty_equivalent=status.faculty_equivalent,
            faculty_equivalent_reason=status.faculty_equivalent_reason,
        )
        for assignment in ordered
    )


def assignment_alternatives_for_session(
    session,
    responsibility=None,
    primary_position=None,
    *,
    position=None,
    assignment_overrides=None,
    excluded_physician_ids=(),
):
    if not getattr(session, "pk", None):
        raise ValidationError("Assignment alternatives require a persisted session.")
    if position is not None:
        if responsibility is not None or primary_position is not None:
            raise ValidationError("Supply either position or responsibility and primary_position.")
        responsibility, primary_position = _assignment_slot_from_position(position)
    elif primary_position is None and responsibility in {"primary-1", "primary-2", "secondary"}:
        responsibility, primary_position = _assignment_slot_from_position(responsibility)
    _validate_assignment_slot(responsibility, primary_position)
    assignments = tuple(
        PreceptorAssignment.objects.filter(session=session)
        .select_related("physician")
        .order_by("responsibility", "primary_position", "pk")
    )
    if is_operational_date(session.date) and holiday_closure_for(
        session.date, session.period_of_day
    ):
        current = next(
            (
                assignment
                for assignment in assignments
                if assignment.responsibility == responsibility
                and assignment.primary_position == primary_position
            ),
            None,
        )
        if current is None:
            return AssignmentAlternatives(session, responsibility, primary_position, None, (), ())
        row = _projection_rows_by_physician(session)[current.physician_id]
        return AssignmentAlternatives(
            session,
            responsibility,
            primary_position,
            _assignment_alternative_from_assessment(
                candidate_assessment_from_projection_row(
                    row,
                    session,
                    responsibility,
                    primary_position,
                    current_assignment=current,
                ),
                tier="current",
                tier_label="Current assignment",
                assignment=current,
                responsibility=responsibility,
                primary_position=primary_position,
            ),
            (),
            (),
        )
    if assignment_overrides is not None:
        assignments_by_position = {
            (assignment.responsibility, assignment.primary_position): assignment
            for assignment in assignments
        }
        for (override_responsibility, override_position), physician in assignment_overrides.items():
            current_assignment = assignments_by_position.get(
                (override_responsibility, override_position)
            )
            if physician is None:
                assignments_by_position.pop((override_responsibility, override_position), None)
            else:
                assignments_by_position[(override_responsibility, override_position)] = (
                    _ProjectedAssignment(
                        pk=current_assignment.pk if current_assignment else None,
                        physician=physician,
                        responsibility=override_responsibility,
                        primary_position=override_position,
                        is_locked=current_assignment.is_locked if current_assignment else False,
                    )
                )
        assignments = tuple(assignments_by_position.values())
    current = next(
        (
            assignment
            for assignment in assignments
            if assignment.responsibility == responsibility
            and assignment.primary_position == primary_position
        ),
        None,
    )
    assigned_elsewhere = {
        assignment.physician_id for assignment in assignments if assignment is not current
    }
    rows_by_physician = _projection_rows_by_physician(session)
    current_alternative = None
    if current is not None:
        current_alternative = _assignment_alternative_from_assessment(
            candidate_assessment_from_projection_row(
                rows_by_physician[current.physician_id],
                session,
                responsibility,
                primary_position,
                current_assignment=current,
            ),
            tier="current",
            tier_label="Current assignment",
            assignment=current,
            responsibility=responsibility,
            primary_position=primary_position,
        )
    excluded_physician_ids = set(excluded_physician_ids)
    alternatives = []
    for physician_id, row in rows_by_physician.items():
        if (
            physician_id in assigned_elsewhere
            or physician_id in excluded_physician_ids
            or (current is not None and physician_id == current.physician_id)
        ):
            continue
        assessment = candidate_assessment_from_projection_row(
            row, session, responsibility, primary_position, assigned_elsewhere=assigned_elsewhere
        )
        if not assessment.manually_selectable:
            continue
        tier = _assignment_alternative_tier(assessment)
        alternatives.append(
            _assignment_alternative_from_assessment(
                assessment,
                tier=tier,
                tier_label=ASSIGNMENT_ALTERNATIVE_TIER_LABELS[tier],
                responsibility=responsibility,
                primary_position=primary_position,
            )
        )
    alternatives.sort(
        key=lambda alternative: (
            ASSIGNMENT_ALTERNATIVE_TIER_ORDER.index(alternative.tier),
            alternative.physician.display_name.casefold(),
            alternative.physician.pk,
        )
    )
    alternatives = tuple(alternatives)
    tiers = tuple(
        AssignmentAlternativeTier(
            key=tier,
            label=ASSIGNMENT_ALTERNATIVE_TIER_LABELS[tier],
            alternatives=tuple(
                alternative for alternative in alternatives if alternative.tier == tier
            ),
        )
        for tier in ASSIGNMENT_ALTERNATIVE_TIER_ORDER
        if tier not in {"available_soft_academic_event", "available_as_float"}
        or any(alternative.tier == tier for alternative in alternatives)
    )
    return AssignmentAlternatives(
        session=session,
        responsibility=responsibility,
        primary_position=primary_position,
        current=current_alternative,
        alternatives=alternatives,
        tiers=tiers,
    )


def assignment_alternatives_for_position(session, position):
    return assignment_alternatives_for_session(session, position=position)


def precepting_candidate_assessment(session, physician, responsibility, primary_position):
    """Assess one physician for one persisted Precepting position without mutation."""
    if not getattr(session, "pk", None):
        raise ValidationError("Candidate assessments require a persisted session.")
    _validate_assignment_slot(responsibility, primary_position)
    rows_by_physician = _projection_rows_by_physician(session)
    try:
        row = rows_by_physician[physician.pk]
    except KeyError:
        raise ValidationError("Candidate assessment physician must be recorded.") from None
    assignments = tuple(
        PreceptorAssignment.objects.filter(session=session).select_related("physician")
    )
    current = next(
        (
            assignment
            for assignment in assignments
            if assignment.responsibility == responsibility
            and assignment.primary_position == primary_position
        ),
        None,
    )
    assigned_elsewhere = {
        assignment.physician_id for assignment in assignments if assignment is not current
    }
    return candidate_assessment_from_projection_row(
        row,
        session,
        responsibility,
        primary_position,
        current_assignment=current,
        assigned_elsewhere=assigned_elsewhere,
    )


def candidate_assessment_from_projection_row(
    row,
    session,
    responsibility,
    primary_position,
    *,
    current_assignment=None,
    assigned_elsewhere=(),
):
    """Combine structural boundaries, Tier 0, and sibling facts without priority shortcuts."""
    availability = row.tier_zero
    reasons = []
    role_valid = availability.ordinarily_eligible_to_precept
    position_valid = _position_is_valid_for_availability(responsibility, availability)
    if not role_valid:
        reasons.append(
            CandidateAssessmentReason(
                "role_ineligible_assignment",
                "structural",
                "hard",
                "Ineligible role",
                availability.preceptor_reason
                or "No effective Faculty or Community Preceptor role applies on this date.",
            )
        )
    if not position_valid:
        reasons.append(
            CandidateAssessmentReason(
                "role_responsibility_mismatch",
                "structural",
                "hard",
                "Wrong position",
                "Secondary assignments require an effective Faculty role or Float-only "
                "availability on the session date.",
            )
        )
    if row.physician.pk in assigned_elsewhere:
        reasons.append(
            CandidateAssessmentReason(
                "same_session_occupancy",
                "structural",
                "hard",
                "Already assigned",
                "This physician already occupies another position in this session.",
            )
        )
    if availability.availability == "not_available":
        reasons.append(
            CandidateAssessmentReason(
                "not_available_assignment",
                "hard",
                "hard",
                "Not available",
                "Tier 0 availability is not available for this session.",
            )
        )
    elif availability.availability == "backup_only":
        reasons.append(
            CandidateAssessmentReason(
                "backup_only_assignment",
                "advisory",
                "preference",
                "Backup only",
                "Selecting this physician creates a non-blocking preference finding.",
            )
        )
    if manual_float_reason := manual_float_availability_reason(availability):
        reasons.append(manual_float_reason)
    if availability.has_soft_academic_event:
        reasons.append(
            CandidateAssessmentReason(
                "soft_academic_event_assignment",
                "advisory",
                "preference",
                "Soft Academic Event",
                "Selecting this physician creates a non-blocking preference finding.",
            )
        )
    reasons.extend(
        CandidateAssessmentReason(
            "availability_source",
            "hard" if availability.availability == "not_available" else "advisory",
            "hard" if availability.availability == "not_available" else "preference",
            source.label,
            source.reason,
            source.source_type,
            source.source_id,
        )
        for source in _ordered_availability_sources(availability.sources)
    )
    reasons.extend(
        CandidateAssessmentReason(
            conflict.code,
            "hard",
            "hard",
            conflict.label,
            conflict.reason,
            conflict.source_type,
            conflict.source_id,
        )
        for conflict in row.precepting_conflicts
    )
    manually_selectable = (
        is_manually_eligible_for_position(responsibility, availability)
        and row.physician.pk not in assigned_elsewhere
    )
    automatic_eligible = (
        manually_selectable
        and not availability.is_float_only
        and not any(reason.severity == "hard" for reason in reasons)
    )
    return CandidateAssessment(
        physician=row.physician,
        session=session,
        responsibility=responsibility,
        primary_position=primary_position,
        automatic_eligible=automatic_eligible,
        manually_selectable=manually_selectable,
        reasons=tuple(reasons),
        effective_category=availability.effective_category,
        ordinarily_eligible_to_precept=availability.ordinarily_eligible_to_precept,
        preceptor_reason=availability.preceptor_reason,
        faculty_equivalent=availability.faculty_equivalent,
        faculty_equivalent_reason=availability.faculty_equivalent_reason,
        is_applicable=availability.is_applicable,
        availability=availability.availability,
        base_availability=availability.base_availability,
        is_expected_community_commitment=availability.is_expected_community_commitment,
        sources=_ordered_availability_sources(availability.sources),
        has_soft_academic_event=availability.has_soft_academic_event,
        manual_float_eligible=availability.is_float_only,
    )


def manual_float_availability_reason(availability):
    """Return the shared Admin-source explanation for Float-only availability."""
    if not availability.is_float_only:
        return None
    sources = _ordered_availability_sources(availability.sources)
    admin_source = next(
        (source for source in sources if source.reason == "manual_float_availability"),
        None,
    )
    return CandidateAssessmentReason(
        "manual_float_availability",
        "manual_only",
        None,
        "Available as Float",
        (
            f"{admin_source.label} records the Admin period for this effective Float "
            "Preceptor; manual assignment is allowed but Schedule Bot will not select it."
        )
        if admin_source
        else "An Admin period makes this effective Float Preceptor manually available.",
        admin_source.source_type if admin_source else None,
        admin_source.source_id if admin_source else None,
    )


def _position_is_valid_for_availability(responsibility, availability):
    """Return whether a position accepts this physician's effective availability."""
    return not (
        responsibility == PreceptorAssignment.Responsibility.SECONDARY
        and availability.effective_category != EffectiveRole.Role.FACULTY
        and not availability.is_float_only
    )


def is_manually_eligible_for_position(position, availability):
    """Whether a Tier 0 role may be deliberately assigned to this position.

    Float-only availability is an explicit manual exception for positions that
    otherwise require Faculty. It leaves the underlying Community and
    faculty-equivalence facts unchanged.
    """
    if not availability.ordinarily_eligible_to_precept:
        return False
    if position in (PreceptorAssignment.Responsibility.SECONDARY, "late_doc"):
        return (
            availability.effective_category == EffectiveRole.Role.FACULTY
            or availability.is_float_only
        )
    return True


def _assignment_alternative_tier(assessment):
    if getattr(assessment, "has_hard_reason", False):
        return "not_available"
    if assessment.availability == "available" and assessment.has_soft_academic_event:
        return "available_soft_academic_event"
    if (
        assessment.availability == "available"
        and assessment.effective_category == EffectiveRole.Role.COMMUNITY_PRECEPTOR
        and assessment.is_expected_community_commitment
    ):
        return "available_expected_community"
    if assessment.availability == "available":
        return "available_other"
    if assessment.availability == "backup_only":
        return "backup_only"
    if (
        getattr(assessment, "manual_float_eligible", False)
        or assessment.availability == "float_only"
    ):
        return "available_as_float"
    return "not_available"


def _assignment_alternative_from_assessment(
    assessment, *, tier, tier_label, assignment=None, responsibility=None, primary_position=None
):
    consequence = None
    hard_reasons = tuple(reason for reason in assessment.reasons if reason.severity == "hard")
    if hard_reasons:
        consequence = (
            "Hard conflict or not available: selecting this physician creates a hard availability "
            "finding or sibling-conflict finding."
        )
    elif tier == "backup_only":
        consequence = (
            "Backup only: selecting this physician creates a non-blocking preference finding."
        )
    elif tier == "available_soft_academic_event":
        consequence = "Soft Academic Event: selecting this physician creates a non-blocking preference finding."
    return AssignmentAlternative(
        assessment=assessment,
        physician=assessment.physician,
        effective_category=assessment.effective_category,
        ordinarily_eligible_to_precept=assessment.ordinarily_eligible_to_precept,
        preceptor_reason=assessment.preceptor_reason,
        faculty_equivalent=assessment.faculty_equivalent,
        faculty_equivalent_reason=assessment.faculty_equivalent_reason,
        is_applicable=assessment.is_applicable,
        availability=assessment.availability,
        base_availability=assessment.base_availability,
        is_expected_community_commitment=assessment.is_expected_community_commitment,
        sources=assessment.sources,
        tier=tier,
        tier_label=tier_label,
        availability_reason=_assignment_availability_reason(assessment, tier),
        selection_consequence=consequence,
        assignment=assignment,
        responsibility=responsibility,
        primary_position=primary_position,
        is_locked=assignment.is_locked if assignment is not None else None,
    )


def _projection_rows_by_physician(session):
    return {
        row.physician.pk: row
        for row in slot_projection((RequestedSlot(session.date, session.period_of_day),))
    }


def _assignment_availability_reason(assessment, tier):
    availability = assessment
    concise = isinstance(assessment, CandidateAssessment)
    if not availability.ordinarily_eligible_to_precept:
        return (
            availability.preceptor_reason
            or "No effective faculty or Community Preceptor role applies on this date."
        )
    if not availability.is_applicable:
        return "No weekday preceptor session applies on this date."
    if availability.availability == "available":
        if availability.has_soft_academic_event:
            if concise:
                return "Available with a soft Academic Event; see details."
            return (
                "Available, but has a soft Academic Event: "
                f"{', '.join(source.label for source in _ordered_availability_sources(availability.sources))}."
            )
        if availability.is_expected_community_commitment:
            return "Expected Community Preceptor commitment is available."
        return "Available after applying the recorded scheduling sources."
    if getattr(availability, "manual_float_eligible", availability.availability == "float_only"):
        if concise:
            return "Available as Float through Admin; manual assignment only; see details."
        source_labels = tuple(
            source.label for source in _ordered_availability_sources(availability.sources)
        )
        if source_labels:
            return f"Available as Float through {', '.join(source_labels)}; manual assignment only."
        return "Available as Float; manual assignment only."
    if availability.availability == "backup_only":
        if concise:
            return "Available only as backup; see details."
        source_labels = tuple(
            source.label for source in _ordered_availability_sources(availability.sources)
        )
        if source_labels:
            return f"Available only as backup because of {', '.join(source_labels)}."
        return "Available only as backup."
    if availability.is_expected_community_commitment:
        if concise:
            return "Expected Community Preceptor commitment is blocked; see details."
        source_labels = tuple(
            source.label for source in _ordered_availability_sources(availability.sources)
        )
        if source_labels:
            return (
                f"Expected Community Preceptor commitment is blocked by {', '.join(source_labels)}."
            )
        return "Expected Community Preceptor commitment is blocked."
    if concise:
        return "Not available; see details."
    source_labels = tuple(
        source.label for source in _ordered_availability_sources(availability.sources)
    )
    if source_labels:
        return f"Not available because of {', '.join(source_labels)}."
    return "No matching available preceptor schedule applies on this date."


def draft_findings_for_session(session):
    return _draft_findings_for_session(session, _projection_rows_by_physician(session))


def operational_draft_findings_for_session(session):
    """Return live draft findings only while the session remains operational."""
    if not is_operational_date(session.date):
        return DraftFindingResult(())
    return draft_findings_for_session(session)


def _draft_findings_for_session(
    session,
    rows_by_physician,
    *,
    assignment_overrides=None,
    holiday_closures=None,
    operational=None,
):
    assignments = _session_assignments_from_projection(
        session, rows_by_physician, assignment_overrides=assignment_overrides
    )
    closure = (
        holiday_closures.get((session.date, session.period_of_day))
        if holiday_closures is not None
        else holiday_closure_for(session.date, session.period_of_day)
    )
    if (is_operational_date(session.date) if operational is None else operational) and closure:
        return _draft_finding_result(
            [
                DraftFinding(
                    "holiday_closed_assignment",
                    "hard",
                    session,
                    None,
                    (status.physician,),
                    {
                        "holiday": closure.name,
                        "coverage": closure.coverage_label,
                        "assignment_id": status.assignment.pk,
                        "responsibility": status.responsibility,
                        "primary_position": status.primary_position,
                        "position": _assignment_position_value(status),
                    },
                )
                for status in assignments
            ]
        )
    findings = []
    assigned_primary_positions = {
        status.primary_position
        for status in assignments
        if status.responsibility == PreceptorAssignment.Responsibility.PRIMARY
    }
    missing_positions = tuple(
        position for position in (1, 2) if position not in assigned_primary_positions
    )
    findings.extend(
        DraftFinding(
            "missing_primary_position",
            "hard",
            session,
            None,
            (),
            {"position": f"primary-{position}", "primary_position": position},
        )
        for position in missing_positions
    )
    faculty_equivalent = tuple(status for status in assignments if status.faculty_equivalent)
    if not missing_positions and len(faculty_equivalent) < 2:
        findings.append(
            DraftFinding(
                "insufficient_faculty_equivalent_coverage",
                "hard",
                session,
                None,
                tuple(status.physician for status in faculty_equivalent),
                {
                    "required_count": 2,
                    "assigned_count": len(assignments),
                    "faculty_equivalent_count": len(faculty_equivalent),
                },
                identity_values={
                    "assigned_physician_ids": [status.physician.pk for status in assignments]
                },
            )
        )
    assigned_physician_ids = {status.physician.pk for status in assignments}
    for status in assignments:
        position = _assignment_position_value(status)
        if not status.ordinarily_eligible_to_precept:
            findings.append(
                DraftFinding(
                    "role_ineligible_assignment",
                    "hard",
                    session,
                    None,
                    (status.physician,),
                    {
                        "assignment_id": status.assignment.pk,
                        "responsibility": status.responsibility,
                        "primary_position": status.primary_position,
                        "preceptor_reason": status.preceptor_reason,
                        "position": position,
                    },
                )
            )
        availability = rows_by_physician[status.physician.pk]
        if (
            status.ordinarily_eligible_to_precept
            and status.responsibility == PreceptorAssignment.Responsibility.SECONDARY
            and not _position_is_valid_for_availability(
                status.responsibility, availability.tier_zero
            )
        ):
            findings.append(
                DraftFinding(
                    "role_responsibility_mismatch",
                    "hard",
                    session,
                    None,
                    (status.physician,),
                    {
                        "assignment_id": status.assignment.pk,
                        "responsibility": status.responsibility,
                        "primary_position": status.primary_position,
                        "position": position,
                        "effective_category": availability.effective_category,
                    },
                )
            )
        if availability.availability in ("not_available", "backup_only"):
            findings.append(
                DraftFinding(
                    "not_available_assignment"
                    if availability.availability == "not_available"
                    else "backup_only_assignment",
                    "hard" if availability.availability == "not_available" else "preference",
                    session,
                    None,
                    (status.physician,),
                    {
                        "assignment_id": status.assignment.pk,
                        "responsibility": status.responsibility,
                        "primary_position": status.primary_position,
                        "position": position,
                        "availability": availability.availability,
                        "base_availability": availability.base_availability,
                    },
                    _ordered_availability_sources(availability.sources),
                )
            )
        elif availability.has_soft_academic_event:
            findings.append(
                DraftFinding(
                    "soft_academic_event_assignment",
                    "preference",
                    session,
                    None,
                    (status.physician,),
                    {"assignment_id": status.assignment.pk, "position": position},
                    _ordered_availability_sources(availability.sources),
                )
            )
        for conflict in availability.precepting_conflicts:
            findings.append(
                DraftFinding(
                    conflict.code,
                    "hard",
                    session,
                    None,
                    (status.physician,),
                    {
                        "assignment_id": status.assignment.pk,
                        "position": position,
                        **conflict.finding_explanation,
                    },
                )
            )
    for availability in rows_by_physician.values():
        physician = availability.physician
        if (
            availability.ordinarily_eligible_to_precept
            and availability.effective_category == EffectiveRole.Role.COMMUNITY_PRECEPTOR
            and availability.is_expected_community_commitment
            and availability.availability == "available"
            and physician.pk not in assigned_physician_ids
        ):
            findings.append(
                DraftFinding(
                    "unassigned_community_commitment",
                    "preference",
                    session,
                    None,
                    (physician,),
                    {
                        "community_preceptor_rule_id": availability.tier_zero.community_preceptor_rule.pk,
                        "availability": availability.availability,
                    },
                    _ordered_availability_sources(availability.sources),
                )
            )
    return _draft_finding_result(findings)


def _session_assignments_from_projection(session, rows_by_physician, *, assignment_overrides=None):
    if "assignments" in getattr(session, "_prefetched_objects_cache", {}):
        assignments = sorted(
            session.assignments.all(),
            key=lambda assignment: (
                assignment.responsibility,
                assignment.primary_position or 0,
                assignment.pk,
            ),
        )
    else:
        assignments = list(
            session.assignments.select_related("physician").order_by(
                "responsibility", "primary_position", "pk"
            )
        )
    if assignment_overrides is not None:
        assignments_by_position = {
            (assignment.responsibility, assignment.primary_position): assignment
            for assignment in assignments
        }
        for (responsibility, primary_position), physician in assignment_overrides.items():
            current = assignments_by_position.get((responsibility, primary_position))
            if physician is None:
                assignments_by_position.pop((responsibility, primary_position), None)
            else:
                assignments_by_position[(responsibility, primary_position)] = _ProjectedAssignment(
                    pk=current.pk if current else None,
                    physician=physician,
                    responsibility=responsibility,
                    primary_position=primary_position,
                    is_locked=current.is_locked if current else False,
                )
        assignments = list(assignments_by_position.values())
    primary = [assignment for assignment in assignments if assignment.responsibility == "primary"]
    secondary = [
        assignment for assignment in assignments if assignment.responsibility == "secondary"
    ]
    ordered = sorted(primary, key=lambda assignment: assignment.primary_position) + secondary
    return tuple(
        SessionAssignmentStatus(
            assignment=assignment,
            physician=assignment.physician,
            responsibility=assignment.responsibility,
            primary_position=assignment.primary_position,
            is_locked=assignment.is_locked,
            ordinarily_eligible_to_precept=(
                row := rows_by_physician[assignment.physician_id]
            ).ordinarily_eligible_to_precept,
            preceptor_reason=row.preceptor_reason,
            faculty_equivalent=row.faculty_equivalent,
            faculty_equivalent_reason=row.faculty_equivalent_reason,
        )
        for assignment in ordered
    )


def draft_findings_for_week(on_date):
    week = work_week_start(on_date)
    return projected_draft_findings_for_weeks((week,))


def projected_draft_findings_for_weeks(
    weeks, *, assignment_overrides=None, late_doc_overrides=None, complete_calendar=False
):
    """Evaluate the ordinary finding policy against read-only assignment overlays."""
    weeks = tuple(sorted(dict.fromkeys(weeks)))
    if not weeks:
        return DraftFindingResult(())
    sessions = PreceptorSession.objects.filter(
        date__range=(min(weeks), max(weeks) + timedelta(days=4))
    ).order_by("date", "period_of_day", "pk")
    if complete_calendar:
        sessions_by_slot = {
            (session.date, session.period_of_day): session
            for session in sessions.prefetch_related("assignments__physician")
        }
        for week in weeks:
            for offset in range(5):
                on_date = week + timedelta(days=offset)
                for period in PreceptorSession.PeriodOfDay.values:
                    if (on_date, period) not in sessions_by_slot:
                        # A nonpersisted calendar slot has no assignments or retained
                        # acknowledgement identity. Negative keys cannot collide with rows.
                        session = PreceptorSession(
                            pk=-(on_date.toordinal() * 2 + (period == "PM")),
                            date=on_date,
                            period_of_day=period,
                        )
                        session._prefetched_objects_cache = {"assignments": ()}
                        sessions_by_slot[on_date, period] = session
        sessions = tuple(sessions_by_slot.values())
    return _draft_findings_for_sessions(
        sessions,
        weeks=weeks,
        assignment_overrides=assignment_overrides,
        late_doc_overrides=late_doc_overrides,
    )


def preceptor_date_has_focused_membership(on_date, selected_range):
    """Whether a displayed Precepting date remains actionable in its selected range."""
    return focused_range_has_membership(InclusiveDateRange(on_date, on_date), selected_range)


def preceptor_week_has_focused_membership(week_start, selected_range):
    """Whether any weekday in a displayed Precepting week remains actionable."""
    return focused_range_has_membership(
        InclusiveDateRange(week_start, week_start + timedelta(days=4)), selected_range
    )


def focused_draft_findings_for_range(findings, selected_range):
    """Keep only current-or-future findings within a workspace's selected range."""
    return DraftFindingResult(
        tuple(
            finding
            for finding in findings.findings
            if (
                preceptor_date_has_focused_membership(finding.session.date, selected_range)
                if finding.session is not None
                else preceptor_week_has_focused_membership(finding.week, selected_range)
            )
        )
    )


def operational_draft_findings_for_week(on_date):
    """Project current workweek findings without recalculating historical sessions."""
    week = work_week_start(on_date)
    if not is_operational_preceptor_week(week):
        return DraftFindingResult(())
    sessions = PreceptorSession.objects.filter(
        date__range=(max(week, _operational_today()), week + timedelta(days=4))
    ).order_by("date", "period_of_day", "pk")
    return _draft_findings_for_sessions(sessions, weeks=(week,))


def precepting_projected_work_items(week_starts):
    """Project each operational Precepting week with unresolved work once."""
    weeks = tuple(sorted({work_week_start(week_start) for week_start in week_starts}))
    weeks = tuple(week_start for week_start in weeks if is_operational_preceptor_week(week_start))
    if not weeks:
        return ()

    items = []
    for week_start in weeks:
        findings = operational_draft_findings_for_week(week_start)
        readiness = readiness_for_findings(findings.findings)
        hard_finding_counts = {
            code: sum(finding.code == code for finding in readiness.unacknowledged_hard_findings)
            for code in _DRAFT_FINDING_DEFINITIONS
            if any(finding.code == code for finding in readiness.unacknowledged_hard_findings)
        }
        reasons = precepting_work_reasons(
            hard_finding_counts=hard_finding_counts,
            advisory_finding_count=findings.preference_total,
        )
        if reasons:
            # Open a month that owns remaining session work, including after a
            # cross-month week's Monday has elapsed.
            target_date = min(
                (
                    finding.session.date
                    for finding in findings.findings
                    if finding.session is not None
                ),
                default=week_start,
            )
            items.append(
                ProjectedWorkItem(
                    key=f"precepting-week-{week_start:%Y-%m-%d}",
                    area="precepting",
                    label=f"Week of {week_start:%b} {week_start.day}",
                    affected_range=InclusiveDateRange(week_start, week_start + timedelta(days=4)),
                    matters_on=shift_date_months(week_start, -lead_time_months("precepting")),
                    target_url=(
                        f"{reverse('monthly-workspace')}?month={target_date:%Y-%m}"
                        f"#precepting-week-{week_start:%Y-%m-%d}"
                    ),
                    reasons=tuple(reasons),
                )
            )
    return tuple(items)


def precepting_work_reasons(
    *,
    hard_finding_counts,
    advisory_finding_count,
):
    """Map weekly findings to distinct actionable work units."""
    reasons = []
    for code in _PRECEPTING_HARD_FINDING_ORDER:
        count = hard_finding_counts.get(code, 0)
        if not count:
            continue
        summary_type = _precepting_hard_finding_summary_type(code)
        if code == "missing_primary_position":
            label = f"{count} required position{' is' if count == 1 else 's are'} unfilled"
        elif code == "missing_late_doc":
            label = "Late Doc is unfilled"
        else:
            label = f"{count} hard finding{' remains' if count == 1 else 's remain'}"
        reasons.append(
            ProjectedWorkReason(
                "required",
                label,
                count,
                summary_type=summary_type,
            )
        )
    if advisory_finding_count:
        reasons.append(
            ProjectedWorkReason(
                "advisory",
                f"{advisory_finding_count} non-blocking finding"
                f"{' remains' if advisory_finding_count == 1 else 's remain'}",
                advisory_finding_count,
            )
        )
    return tuple(reasons)


def draft_findings_for_month(month):
    month_start = month.replace(day=1)
    sessions = PreceptorSession.objects.filter(period__month=month_start).order_by(
        "date", "period_of_day", "pk"
    )
    return _draft_findings_for_sessions(sessions)


def operational_draft_findings_for_month(month):
    """Project current month findings without recalculating historical sessions."""
    sessions = (
        PreceptorSession.objects.filter(period__month=month.replace(day=1))
        .filter(date__gte=_operational_today())
        .order_by("date", "period_of_day", "pk")
    )
    weeks = tuple(
        sorted(
            {
                work_week_start(session.date)
                for session in sessions
                if is_operational_preceptor_week(work_week_start(session.date))
            }
        )
    )
    return _draft_findings_for_sessions(sessions, weeks=weeks)


def _operational_today():
    # Keep the clock behind the shared boundary while avoiding a caller cutoff.
    from .operational_attention import organization_today

    return organization_today()


def _draft_findings_for_sessions(
    sessions,
    *,
    weeks=None,
    rows_by_slot_and_physician=None,
    assignment_overrides=None,
    late_doc_overrides=None,
    holidays=None,
    operational=None,
):
    sessions = tuple(sessions)
    if weeks is None:
        weeks = tuple(sorted({work_week_start(session.date) for session in sessions}))
    holidays = tuple(
        holiday_projections_for_dates((*[session.date for session in sessions], *weeks))
        if holidays is None
        else holidays
    )
    holiday_closures = _holiday_closures_by_slot(holidays)
    if rows_by_slot_and_physician is None:
        rows_by_slot_and_physician = {
            (row.slot.on_date, row.slot.period_of_day, row.physician.pk): row
            for row in slot_projection(
                RequestedSlot(session.date, session.period_of_day) for session in sessions
            )
        }
    findings = []
    for session in sessions:
        rows_by_physician = {
            row.physician.pk: row
            for (on_date, period_of_day, _), row in rows_by_slot_and_physician.items()
            if (on_date, period_of_day) == (session.date, session.period_of_day)
        }
        findings.extend(
            _draft_findings_for_session(
                session,
                rows_by_physician,
                assignment_overrides={
                    (responsibility, primary_position): physician
                    for (session_id, responsibility, primary_position), physician in (
                        assignment_overrides or {}
                    ).items()
                    if session_id == session.pk
                },
                holiday_closures=holiday_closures,
                operational=operational,
            ).findings
        )
    for week in weeks:
        if _rounding.normal_rounder_for_week(week) is None:
            findings.append(
                DraftFinding(
                    "missing_normal_rounder",
                    "hard",
                    None,
                    week,
                    (),
                    {"week_start": week, "week_end": week + timedelta(days=6)},
                )
            )
    findings.extend(
        _late_doc_findings(
            weeks,
            rows_by_slot_and_physician,
            sessions,
            late_doc_overrides=late_doc_overrides,
            holiday_closures=holiday_closures,
            operational=operational,
        )
    )
    return _draft_finding_result(findings)


def _late_doc_findings(
    weeks,
    rows_by_slot_and_physician,
    sessions,
    *,
    late_doc_overrides=None,
    holiday_closures=None,
    operational=None,
):
    """Evaluate the separate Monday Late Doc slot beside, not inside, AM/PM coverage."""
    weeks = tuple(dict.fromkeys(weeks))
    assignments = {
        assignment.date: assignment
        for assignment in LateDocAssignment.objects.filter(date__in=weeks).select_related(
            "physician"
        )
    }
    if late_doc_overrides is not None:
        for on_date, physician in late_doc_overrides.items():
            if physician is None:
                assignments.pop(on_date, None)
            else:
                assignments[on_date] = _ProjectedLateDocAssignment(pk=None, physician=physician)
    pm_sessions = {
        session.date: session
        for session in sessions
        if session.period_of_day == PreceptorSession.PeriodOfDay.PM
    }
    findings = []
    for week in weeks:
        session = pm_sessions.get(week)
        if session is None:
            continue
        closure = (
            holiday_closures.get((week, PreceptorSession.PeriodOfDay.PM))
            if holiday_closures is not None
            else holiday_closure_for(week, PreceptorSession.PeriodOfDay.PM)
        )
        if (is_operational_date(week) if operational is None else operational) and closure:
            assignment = assignments.get(week)
            if assignment:
                findings.append(
                    DraftFinding(
                        "holiday_closed_late_doc_assignment",
                        "hard",
                        session,
                        None,
                        (assignment.physician,),
                        {"holiday": closure.name, "position": "late-doc"},
                    )
                )
            continue
        rows_by_physician = {
            physician_id: row
            for (on_date, period_of_day, physician_id), row in rows_by_slot_and_physician.items()
            if (on_date, period_of_day) == (week, PreceptorSession.PeriodOfDay.PM)
        }
        if not rows_by_physician:
            rows_by_physician = {
                row.physician.pk: row
                for row in slot_projection((RequestedSlot(week, PreceptorSession.PeriodOfDay.PM),))
            }
        assignment = assignments.get(week)
        if assignment is None:
            if is_operational_date(week) if operational is None else operational:
                findings.append(
                    DraftFinding(
                        "missing_late_doc",
                        "hard",
                        session,
                        None,
                        (),
                        {"position": "late-doc"},
                    )
                )
            continue
        row = rows_by_physician[assignment.physician_id]
        if not is_manually_eligible_for_position("late_doc", row.tier_zero):
            findings.append(
                DraftFinding(
                    "role_ineligible_late_doc",
                    "hard",
                    session,
                    None,
                    (assignment.physician,),
                    {"position": "late-doc", "assignment_id": assignment.pk},
                )
            )
        if row.availability in ("not_available", "backup_only"):
            findings.append(
                DraftFinding(
                    "not_available_late_doc"
                    if row.availability == "not_available"
                    else "backup_only_late_doc",
                    "hard" if row.availability == "not_available" else "preference",
                    session,
                    None,
                    (assignment.physician,),
                    {
                        "position": "late-doc",
                        "assignment_id": assignment.pk,
                        "availability": row.availability,
                        "base_availability": row.base_availability,
                    },
                    _ordered_availability_sources(row.sources),
                )
            )
        elif row.has_soft_academic_event:
            findings.append(
                DraftFinding(
                    "soft_academic_event_late_doc",
                    "preference",
                    session,
                    None,
                    (assignment.physician,),
                    {"position": "late-doc", "assignment_id": assignment.pk},
                    _ordered_availability_sources(row.sources),
                )
            )
    return findings


def _holiday_closures_by_slot(holidays):
    closures = {}
    for holiday in holidays:
        for period_of_day in PreceptorSession.PeriodOfDay.values:
            if holiday.closes(period_of_day):
                closures.setdefault((holiday.observed_date, period_of_day), holiday)
    return closures


def _draft_finding_result(findings):
    return DraftFindingResult(tuple(sorted(findings, key=_draft_finding_sort_key)))


def _draft_finding_sort_key(finding):
    code_order = {
        "missing_normal_rounder": 0,
        "missing_primary_position": 1,
        "insufficient_faculty_equivalent_coverage": 2,
        "role_ineligible_assignment": 3,
        "role_responsibility_mismatch": 4,
        "not_available_assignment": 5,
        "backup_only_assignment": 6,
        "soft_academic_event_assignment": 7,
        "interview_assignment_conflict": 8,
        "program_director_interview_commitment": 9,
        "unassigned_community_commitment": 10,
        "missing_late_doc": 11,
        "role_ineligible_late_doc": 12,
        "not_available_late_doc": 13,
        "backup_only_late_doc": 14,
        "soft_academic_event_late_doc": 15,
        "holiday_closed_assignment": 16,
        "holiday_closed_late_doc_assignment": 17,
    }
    responsibility_order = {"primary": 0, "secondary": 1}
    session = finding.session
    position = finding.explanation.get("primary_position")
    return (
        finding.week or session.date,
        0 if finding.week else 1,
        PreceptorSession.PeriodOfDay.values.index(session.period_of_day) if session else -1,
        code_order[finding.code],
        responsibility_order.get(finding.explanation.get("responsibility"), -1),
        position if position is not None else -1,
        tuple((physician.display_name, physician.pk) for physician in finding.physicians),
    )


def _assignment_position_value(status):
    if status.responsibility == PreceptorAssignment.Responsibility.SECONDARY:
        return "secondary"
    return f"primary-{status.primary_position}"


def _ordered_availability_sources(sources):
    source_order = {
        "faculty_template": 0,
        "monthly_availability_rule": 1,
        "external_patient_care": 2,
        "community_preceptor_rule": 3,
        "vacation": 4,
        "academic_event": 5,
        "normal_rounder": 6,
        "casual_rounder_rule": 7,
    }
    return tuple(
        sorted(sources, key=lambda source: (source_order[source.source_type], source.source_id))
    )


@scheduling_mutation
@transaction.atomic
def assign_preceptor(
    *, session, physician, responsibility, primary_position, scheduler, reason="", override=False
):
    require_scheduler(scheduler)
    capture_calendar_range(session.date)
    if is_operational_date(session.date) and (
        closure := holiday_closure_for(session.date, session.period_of_day)
    ):
        raise ValidationError(f"{closure.name} closes this {closure.coverage_label} clinic shift.")
    _validate_assignment_slot(responsibility, primary_position)
    assignments = list(
        PreceptorAssignment.objects.select_for_update()
        .filter(session=session)
        .select_related("physician")
    )
    current = next(
        (
            assignment
            for assignment in assignments
            if assignment.responsibility == responsibility
            and assignment.primary_position == primary_position
        ),
        None,
    )
    if any(
        assignment.physician_id == physician.pk
        and assignment.pk != (current.pk if current else None)
        for assignment in assignments
    ):
        raise ValidationError(
            {"physician": "A physician can occupy only one position in a session."}
        )
    if current and current.physician_id == physician.pk:
        return current
    if (
        not override
        and not _physicians.physicians_status_on(
            physician, session.date
        ).ordinarily_eligible_to_precept
    ):
        raise ValidationError(
            {"physician": "This physician is not eligible to precept on the session date."}
        )
    if (
        not override
        and responsibility == PreceptorAssignment.Responsibility.SECONDARY
        and not _position_is_valid_for_availability(
            responsibility,
            _availability.effective_preceptor_availability_on(
                physician, session.date, session.period_of_day
            ),
        )
    ):
        raise ValidationError(
            {
                "physician": (
                    "Secondary assignments require an effective Faculty role or Float-only "
                    "availability on the session date."
                )
            }
        )
    if current:
        previous_value = _assignment_value(current)
        current.physician = physician
        current.full_clean()
        current.save(update_fields=("physician",))
        _record_assignment_change(
            session=session,
            assignment_id=current.pk,
            action=PreceptorAssignmentChange.Action.ASSIGNED,
            previous_value=previous_value,
            recorded_value=_assignment_value(current),
            scheduler=scheduler,
            reason=reason,
        )
        return current
    assignment = PreceptorAssignment(
        session=session,
        physician=physician,
        responsibility=responsibility,
        primary_position=primary_position,
    )
    assignment.full_clean()
    assignment.save()
    _record_assignment_change(
        session=session,
        assignment_id=assignment.pk,
        action=PreceptorAssignmentChange.Action.ASSIGNED,
        previous_value=None,
        recorded_value=_assignment_value(assignment),
        scheduler=scheduler,
        reason=reason,
    )
    return assignment


@scheduling_mutation
@transaction.atomic
def remove_preceptor_assignment(*, assignment, scheduler, reason=""):
    require_scheduler(scheduler)
    assignment = PreceptorAssignment.objects.select_for_update().get(pk=assignment.pk)
    capture_calendar_range(assignment.session.date)
    _record_assignment_change(
        session=assignment.session,
        assignment_id=assignment.pk,
        action=PreceptorAssignmentChange.Action.REMOVED,
        previous_value=_assignment_value(assignment),
        recorded_value=None,
        scheduler=scheduler,
        reason=reason,
    )
    assignment.delete()


@scheduling_mutation
@transaction.atomic
def set_preceptor_assignment_lock(*, assignment, is_locked, scheduler, reason=""):
    require_scheduler(scheduler)
    assignment = PreceptorAssignment.objects.select_for_update().get(pk=assignment.pk)
    capture_calendar_range(assignment.session.date)
    if assignment.is_locked == is_locked:
        return assignment
    previous_value = _assignment_value(assignment)
    assignment.is_locked = is_locked
    assignment.save(update_fields=("is_locked",))
    _record_assignment_change(
        session=assignment.session,
        assignment_id=assignment.pk,
        action=PreceptorAssignmentChange.Action.LOCK_CHANGED,
        previous_value=previous_value,
        recorded_value=_assignment_value(assignment),
        scheduler=scheduler,
        reason=reason,
    )
    return assignment


def _validate_assignment_slot(responsibility, primary_position):
    if responsibility == PreceptorAssignment.Responsibility.PRIMARY and primary_position in (1, 2):
        return
    if responsibility == PreceptorAssignment.Responsibility.SECONDARY and primary_position is None:
        return
    raise ValidationError(
        "Assignments must use one of two primary positions or the secondary position."
    )


def _assignment_slot_from_position(position):
    positions = {
        "primary-1": (PreceptorAssignment.Responsibility.PRIMARY, 1),
        "primary-2": (PreceptorAssignment.Responsibility.PRIMARY, 2),
        "secondary": (PreceptorAssignment.Responsibility.SECONDARY, None),
    }
    try:
        return positions[position]
    except KeyError:
        raise ValidationError(
            "Assignments must use one of two primary positions or the secondary position."
        ) from None


def _assignment_value(assignment):
    return {
        "physician_id": assignment.physician_id,
        "physician_display_name": assignment.physician.display_name,
        "responsibility": assignment.responsibility,
        "primary_position": assignment.primary_position,
        "is_locked": assignment.is_locked,
    }


def _record_assignment_change(**kwargs):
    kwargs["reason"] = normalize_note(kwargs["reason"])
    kwargs["changed_by"] = kwargs.pop("scheduler")
    return PreceptorAssignmentChange.objects.create(**kwargs)
