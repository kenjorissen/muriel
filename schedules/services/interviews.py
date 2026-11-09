"""Interview schedule reads, policy, mutations, and retained history."""

from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse

from ..academic_years import academic_year_bounds, academic_year_for_date
from ..models.interviews import InterviewShift, InterviewShiftChange
from ..models.physicians import EffectiveRole, Physician
from . import availability as _availability
from ._mutation import (
    capture_calendar_range,
    normalize_note,
    require_scheduler,
    scheduling_mutation,
)
from .finding_acknowledgements import readiness_for_findings
from .holidays import holiday_closure_for
from .lead_times import lead_time_months
from .operational_attention import is_operational_date, organization_today
from .slot_projection import RequestedSlot, slot_projection
from .work_remaining import (
    InclusiveDateRange,
    ProjectedWorkItem,
    ProjectedWorkReason,
    shift_date_months,
)


@dataclass(frozen=True)
class InterviewFinding:
    code: str
    severity: str
    shift: InterviewShift
    physicians: tuple[Physician, ...]
    explanation: dict
    sources: tuple[_availability.EffectiveAvailabilitySource, ...] = ()

    @property
    def is_blocking(self):
        return self.severity == "hard"

    @property
    def message(self):
        messages = {
            "role_ineligible_assignment": "Assigned physician has no effective Faculty role.",
            "not_available_assignment": "Assigned physician is not available for this Interview shift.",
            "backup_only_assignment": "Assigned physician is available as backup only.",
            "preceptor_assignment_conflict": "Assigned physician is also assigned to the AM Preceptor Schedule.",
            "holiday_closed_interview": "Clinic holiday closes this Interview shift.",
        }
        return messages[self.code]

    @property
    def identity(self):
        from .finding_acknowledgements import finding_identity

        return finding_identity(self)


@dataclass(frozen=True)
class InterviewFindingResult:
    findings: tuple[InterviewFinding, ...]

    @property
    def hard_total(self):
        return sum(finding.severity == "hard" for finding in self.findings)

    @property
    def preference_total(self):
        return sum(finding.severity == "preference" for finding in self.findings)

    @property
    def has_blocking_findings(self):
        return self.hard_total > 0


@dataclass(frozen=True)
class InterviewCandidate:
    physician: Physician
    availability: str | None
    tier: str
    tier_label: str
    availability_reason: str
    requires_override: bool = False


@dataclass(frozen=True)
class InterviewCandidates:
    shift: InterviewShift
    candidates: tuple[InterviewCandidate, ...]


@dataclass(frozen=True)
class InterviewScheduleSummary:
    academic_year: int
    shifts: tuple[InterviewShift, ...]
    attention_active: bool
    active_count: int
    remaining_to_20: int
    unassigned_count: int
    over_target_count: int
    hard_findings: tuple[InterviewFinding, ...]
    findings: tuple[InterviewFinding, ...]
    complete: bool


def interview_shifts_for_academic_year(academic_year, *, include_inactive=False):
    """Return retained Interview shifts whose dates fall in one academic year."""
    start_date, end_date = _academic_year_bounds(academic_year)
    shifts = InterviewShift.objects.filter(date__range=(start_date, end_date))
    if not include_inactive:
        shifts = shifts.filter(is_active=True)
    return shifts.select_related("physician").order_by("date", "pk")


def interview_shift_changes():
    """Return retained Interview history in its existing model ordering."""
    return InterviewShiftChange.objects.select_related("shift", "changed_by")


def interview_assignment_physician_ids_on(on_date):
    """Return physicians assigned to active Interview shifts on one date."""
    return set(
        InterviewShift.objects.filter(
            date=on_date, is_active=True, physician__isnull=False
        ).values_list("physician_id", flat=True)
    )


def interview_assignment_for_physician_on(physician, on_date):
    """Return one active Interview assignment for a physician on one date."""
    return InterviewShift.objects.filter(date=on_date, is_active=True, physician=physician).first()


def interview_assignment_candidates(shift):
    """Return normal, conflict-free Faculty choices without mutating either sibling schedule."""
    if not getattr(shift, "pk", None):
        raise ValidationError("Interview assignment candidates require a persisted shift.")
    shift = InterviewShift.objects.select_related("physician").get(pk=shift.pk)
    if not shift.is_active:
        raise ValidationError("Inactive Interview shifts cannot be assigned.")
    if holiday_closure_for(shift.date, "AM"):
        return InterviewCandidates(shift=shift, candidates=())

    candidates = _interview_assignment_candidates_on(shift.date, exclude_interview_shift=shift)
    return InterviewCandidates(shift=shift, candidates=tuple(candidates))


def interview_assignment_candidates_on(on_date):
    """Return normal Interview choices for a proposed date without creating a shift."""
    if holiday_closure_for(on_date, "AM"):
        return ()
    return tuple(
        candidate
        for candidate in _interview_assignment_picker_candidates_on(on_date)
        if not candidate.requires_override
    )


def interview_assignment_picker_candidates_on(on_date):
    """Return all effective-Faculty Interview choices, including explicit overrides."""
    if holiday_closure_for(on_date, "AM"):
        return ()
    return tuple(_interview_assignment_picker_candidates_on(on_date))


def _interview_assignment_candidates_on(on_date, *, exclude_interview_shift=None):
    return [
        candidate
        for candidate in _interview_assignment_picker_candidates_on(
            on_date, exclude_interview_shift=exclude_interview_shift
        )
        if not candidate.requires_override
    ]


def _interview_assignment_picker_candidates_on(on_date, *, exclude_interview_shift=None):
    candidates = []
    for row in _interview_projection_rows_on(
        on_date, exclude_interview_shift=exclude_interview_shift
    ):
        role = _faculty_role(row)
        if role is None:
            continue
        availability = row
        sibling_assignment = row.preceptor_assignment
        if sibling_assignment and availability.availability not in ("available", "backup_only"):
            tier, label = "override_required", "Override-required Faculty"
            reason = (
                "Tier 0 unavailable and conflicts with an AM Preceptor assignment; "
                "selection records a hard finding."
            )
        elif sibling_assignment:
            tier, label = "override_required", "Override-required Faculty"
            reason = "Conflicts with an AM Preceptor assignment; selection records a hard finding."
        elif availability.availability not in ("available", "backup_only"):
            tier, label = "override_required", "Override-required Faculty"
            reason = "Tier 0 unavailable; selection records a hard finding."
        elif availability.availability == "backup_only":
            tier, label = "backup_only", "Backup-only Faculty"
            reason = _interview_availability_reason(availability)
        elif role.faculty_leadership == EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR:
            tier, label = "program_director", "Available Program Directors"
            reason = _interview_availability_reason(availability)
        else:
            tier, label = "available_faculty", "Other available Faculty"
            reason = _interview_availability_reason(availability)
        candidates.append(
            InterviewCandidate(
                physician=row.physician,
                availability=availability.availability,
                tier=tier,
                tier_label=label,
                availability_reason=reason,
                requires_override=tier == "override_required",
            )
        )
    candidates.sort(
        key=lambda candidate: (
            (
                "program_director",
                "available_faculty",
                "backup_only",
                "override_required",
            ).index(candidate.tier),
            candidate.physician.display_name.casefold(),
            candidate.physician.pk,
        )
    )
    return candidates


def interview_findings_for_shift(shift):
    """Read current, explainable availability and sibling-conflict findings for one shift."""
    if not getattr(shift, "pk", None):
        raise ValidationError("Interview findings require a persisted shift.")
    shift = InterviewShift.objects.select_related("physician").get(pk=shift.pk)
    if not shift.is_active:
        return InterviewFindingResult(())
    if is_operational_date(shift.date) and (closure := holiday_closure_for(shift.date, "AM")):
        return InterviewFindingResult(
            (
                InterviewFinding(
                    code="holiday_closed_interview",
                    severity="hard",
                    shift=shift,
                    physicians=(shift.physician,) if shift.physician_id else (),
                    explanation={"holiday": closure.name, "coverage": closure.coverage_label},
                ),
            )
        )
    if not shift.physician_id:
        return InterviewFindingResult(())
    return InterviewFindingResult(
        _interview_findings_for_shift(shift, _interview_projection_rows_by_physician_on(shift.date))
    )


def _interview_findings_for_shift(shift, rows_by_physician, *, operational=None):
    """Apply Interview finding policy to one assigned shift's projection row."""

    if (is_operational_date(shift.date) if operational is None else operational) and (
        closure := holiday_closure_for(shift.date, "AM")
    ):
        return (
            InterviewFinding(
                code="holiday_closed_interview",
                severity="hard",
                shift=shift,
                physicians=(shift.physician,) if shift.physician_id else (),
                explanation={"holiday": closure.name, "coverage": closure.coverage_label},
            ),
        )
    if not shift.physician_id:
        return ()
    findings = []
    physician = shift.physician
    row = rows_by_physician[physician.pk]
    role = _faculty_role(row)
    if role is None:
        findings.append(
            InterviewFinding(
                code="role_ineligible_assignment",
                severity="hard",
                shift=shift,
                physicians=(physician,),
                explanation={"physician_id": physician.pk, "date": shift.date},
            )
        )
    else:
        availability = row
        if availability.availability in (None, "not_available", "backup_only"):
            findings.append(
                InterviewFinding(
                    code=(
                        "backup_only_assignment"
                        if availability.availability == "backup_only"
                        else "not_available_assignment"
                    ),
                    severity="preference" if availability.availability == "backup_only" else "hard",
                    shift=shift,
                    physicians=(physician,),
                    explanation={
                        "physician_id": physician.pk,
                        "date": shift.date,
                        "availability": availability.availability,
                        "base_availability": availability.base_availability,
                    },
                    sources=_ordered_availability_sources(availability.sources),
                )
            )
    if sibling_assignment := row.preceptor_assignment:
        findings.append(
            InterviewFinding(
                code="preceptor_assignment_conflict",
                severity="hard",
                shift=shift,
                physicians=(physician,),
                explanation={
                    "physician_id": physician.pk,
                    "date": shift.date,
                    "preceptor_assignment_id": sibling_assignment.pk,
                    "preceptor_session_id": sibling_assignment.session_id,
                },
            )
        )
    return tuple(findings)


def interview_findings_for_academic_year(academic_year):
    shifts = tuple(interview_shifts_for_academic_year(academic_year))
    rows_by_slot_and_physician = {
        (row.slot.on_date, row.physician.pk): row
        for row in slot_projection(RequestedSlot(shift.date, "AM") for shift in shifts)
    }
    findings = [
        finding
        for shift in shifts
        for finding in _interview_findings_for_shift(
            shift,
            {
                physician_id: row
                for (on_date, physician_id), row in rows_by_slot_and_physician.items()
                if on_date == shift.date
            },
        )
    ]
    return InterviewFindingResult(
        tuple(
            sorted(
                findings, key=lambda finding: (finding.shift.date, finding.code, finding.shift.pk)
            )
        )
    )


def interview_schedule_summary(academic_year):
    shifts = tuple(interview_shifts_for_academic_year(academic_year))
    finding_result = interview_findings_for_academic_year(academic_year)
    return _interview_schedule_summary(academic_year, shifts, finding_result)


def operational_interview_schedule_summary(academic_year, *, include_future=False):
    """Return actionable Interview work without reevaluating past shifts."""
    shifts = tuple(interview_shifts_for_academic_year(academic_year))
    today = organization_today()
    attention_active = _interview_attention_active(academic_year) or (
        include_future
        and academic_year > academic_year_for_date(today)
        and today >= shift_date_months(date(academic_year, 7, 1), -lead_time_months("interviews"))
    )
    operational_shifts = (
        tuple(shift for shift in shifts if is_operational_date(shift.date))
        if attention_active
        else ()
    )
    rows_by_slot_and_physician = {
        (row.slot.on_date, row.physician.pk): row
        for row in slot_projection(RequestedSlot(shift.date, "AM") for shift in operational_shifts)
    }
    findings = tuple(
        finding
        for shift in operational_shifts
        for finding in _interview_findings_for_shift(
            shift,
            {
                physician_id: row
                for (on_date, physician_id), row in rows_by_slot_and_physician.items()
                if on_date == shift.date
            },
        )
    )
    return _interview_schedule_summary(
        academic_year,
        shifts,
        InterviewFindingResult(findings),
        attention_active=attention_active,
    )


def interview_projected_work_items(academic_years):
    """Project one actionable Interview item for each academic year."""
    items = []
    for academic_year in sorted(set(academic_years)):
        summary = operational_interview_schedule_summary(academic_year, include_future=True)
        if not summary.attention_active:
            continue
        reasons = interview_work_reasons(summary)
        if reasons:
            start_date, end_date = _academic_year_bounds(academic_year)
            items.append(
                ProjectedWorkItem(
                    key=f"interview-{academic_year}",
                    area="interviews",
                    label=f"Interview schedule for {academic_year}",
                    affected_range=InclusiveDateRange(start_date, end_date),
                    matters_on=shift_date_months(start_date, -lead_time_months("interviews")),
                    target_url=f"{reverse('interview-list')}?academic_year={academic_year}",
                    reasons=tuple(reasons),
                )
            )
    return tuple(items)


def interview_work_reasons(summary, *, unassigned_count=None, findings=None):
    """Map Interview quotas and distinct findings to actionable work units."""
    unassigned_count = summary.unassigned_count if unassigned_count is None else unassigned_count
    findings = summary.findings if findings is None else findings
    reasons = []
    if summary.remaining_to_20:
        reasons.append(
            ProjectedWorkReason(
                "required",
                f"{summary.remaining_to_20} shifts remain below the quota",
                summary.remaining_to_20,
            )
        )
    if unassigned_count:
        reasons.append(
            ProjectedWorkReason(
                "required",
                f"{unassigned_count} shifts need Faculty",
                unassigned_count,
            )
        )
    if summary.over_target_count:
        reasons.append(
            ProjectedWorkReason(
                "required",
                f"{summary.over_target_count} shifts exceed the quota",
                summary.over_target_count,
            )
        )
    hard_finding_count = len(readiness_for_findings(findings).unacknowledged_hard_findings)
    if hard_finding_count:
        reasons.append(ProjectedWorkReason("required", "Hard findings remain", hard_finding_count))
    advisory_finding_count = sum(finding.severity == "preference" for finding in findings)
    if advisory_finding_count:
        reasons.append(
            ProjectedWorkReason(
                "advisory",
                "Non-blocking findings remain",
                advisory_finding_count,
            )
        )
    return tuple(reasons)


def _interview_schedule_summary(academic_year, shifts, finding_result, *, attention_active=True):
    hard_findings = tuple(
        finding for finding in finding_result.findings if finding.severity == "hard"
    )
    active_count = len(shifts)
    unassigned_count = sum(shift.physician_id is None for shift in shifts)
    return InterviewScheduleSummary(
        academic_year=academic_year,
        shifts=shifts,
        attention_active=attention_active,
        active_count=active_count,
        remaining_to_20=max(20 - active_count, 0) if attention_active else 0,
        unassigned_count=unassigned_count if attention_active else 0,
        over_target_count=max(active_count - 20, 0) if attention_active else 0,
        hard_findings=hard_findings,
        findings=finding_result.findings,
        complete=(
            attention_active and active_count == 20 and unassigned_count == 0 and not hard_findings
        ),
    )


def _interview_attention_active(academic_year):
    """Interview quota work is actionable only in this year's July-January season."""
    today = organization_today()
    return academic_year_for_date(today) == academic_year and (today.month >= 7 or today.month == 1)


def interview_default_date(academic_year):
    shifts = tuple(interview_shifts_for_academic_year(academic_year))
    suggested = (
        date(academic_year, 10, 1)
        if not shifts
        else max(shift.date for shift in shifts) + timedelta(days=1)
    )
    while suggested.weekday() > 4 or holiday_closure_for(suggested, "AM"):
        suggested += timedelta(days=1)
    return suggested


def interview_workspace_default_month(academic_year, today):
    if academic_year_for_date(today) != academic_year:
        return date(academic_year, 10, 1)
    if today.month in {10, 11, 12, 1}:
        return today.replace(day=1)
    if today.month in {7, 8, 9}:
        first_shift = interview_shifts_for_academic_year(academic_year).first()
        first_month = first_shift.date.replace(day=1) if first_shift else date(academic_year, 10, 1)
        return min(first_month, date(academic_year, 10, 1))
    last_shift = interview_shifts_for_academic_year(academic_year).last()
    last_month = last_shift.date.replace(day=1) if last_shift else date(academic_year + 1, 1, 1)
    return max(last_month, date(academic_year + 1, 1, 1))


@scheduling_mutation
@transaction.atomic
def create_interview_shift(*, academic_year, date, scheduler, note=""):
    capture_calendar_range(date)
    require_scheduler(scheduler)
    _require_in_academic_year(date, academic_year)
    _require_open_interview_date(date)
    existing = InterviewShift.objects.select_for_update().filter(date=date, is_active=True).first()
    if existing:
        return existing
    shift = InterviewShift(date=date, note=normalize_note(note))
    shift.full_clean()
    shift.save()
    _record_interview_shift_change(
        shift=shift,
        action=InterviewShiftChange.Action.CREATED,
        previous_value=None,
        recorded_value=_interview_shift_value(shift),
        scheduler=scheduler,
    )
    return shift


@scheduling_mutation
@transaction.atomic
def correct_interview_shift(*, shift, academic_year, date, scheduler):
    capture_calendar_range(date)
    require_scheduler(scheduler)
    _require_in_academic_year(date, academic_year)
    _require_open_interview_date(date)
    shift = InterviewShift.objects.select_for_update().get(pk=shift.pk)
    capture_calendar_range(shift.date)
    if not shift.is_active:
        raise ValidationError("Only an active Interview shift can be corrected.")
    previous_value = _interview_shift_value(shift)
    if shift.date == date:
        return shift
    shift.date = date
    shift.full_clean()
    shift.save(update_fields=("date",))
    _record_interview_shift_change(
        shift=shift,
        action=InterviewShiftChange.Action.CORRECTED,
        previous_value=previous_value,
        recorded_value=_interview_shift_value(shift),
        scheduler=scheduler,
    )
    return shift


@scheduling_mutation
@transaction.atomic
def change_interview_shift_note(*, shift, note, scheduler):
    require_scheduler(scheduler)
    shift = InterviewShift.objects.select_for_update().get(pk=shift.pk)
    capture_calendar_range(shift.date)
    if not shift.is_active:
        raise ValidationError("Only an active Interview shift can be edited.")
    note = normalize_note(note)
    if shift.note == note:
        return shift
    previous_value = _interview_shift_value(shift)
    shift.note = note
    shift.save(update_fields=("note",))
    _record_interview_shift_change(
        shift=shift,
        action=InterviewShiftChange.Action.NOTE_CHANGED,
        previous_value=previous_value,
        recorded_value=_interview_shift_value(shift),
        scheduler=scheduler,
    )
    return shift


@scheduling_mutation
@transaction.atomic
def assign_interview_shift(*, shift, physician, scheduler, override=False):
    """Assign an Interviewer; ordinary changes require Faculty and override for hard conflicts."""
    require_scheduler(scheduler)
    shift = InterviewShift.objects.select_for_update().get(pk=shift.pk)
    capture_calendar_range(shift.date)
    if not shift.is_active:
        raise ValidationError("Inactive Interview shifts cannot be assigned.")
    _require_open_interview_date(shift.date)
    row = _interview_projection_rows_by_physician_on(shift.date, exclude_interview_shift=shift)[
        physician.pk
    ]
    if not override and _faculty_role(row) is None:
        raise ValidationError(
            {"physician": "This physician has no effective Faculty role on the shift date."}
        )
    if shift.physician_id == physician.pk:
        return shift
    availability = row
    sibling_assignment = row.preceptor_assignment
    if not override and (
        availability.availability not in ("available", "backup_only") or sibling_assignment
    ):
        raise ValidationError(
            {
                "physician": "Use the explicit override to retain an unavailable or conflicting assignment."
            }
        )
    previous_value = _interview_shift_value(shift)
    shift.physician = physician
    shift.save(update_fields=("physician",))
    _record_interview_shift_change(
        shift=shift,
        action=InterviewShiftChange.Action.ASSIGNED,
        previous_value=previous_value,
        recorded_value=_interview_shift_value(shift),
        scheduler=scheduler,
    )
    return shift


@scheduling_mutation
@transaction.atomic
def unassign_interview_shift(*, shift, scheduler):
    require_scheduler(scheduler)
    shift = InterviewShift.objects.select_for_update().get(pk=shift.pk)
    capture_calendar_range(shift.date)
    if not shift.is_active:
        raise ValidationError("Inactive Interview shifts cannot be edited.")
    if shift.physician_id is None:
        return shift
    previous_value = _interview_shift_value(shift)
    shift.physician = None
    shift.save(update_fields=("physician",))
    _record_interview_shift_change(
        shift=shift,
        action=InterviewShiftChange.Action.UNASSIGNED,
        previous_value=previous_value,
        recorded_value=_interview_shift_value(shift),
        scheduler=scheduler,
    )
    return shift


@scheduling_mutation
@transaction.atomic
def remove_interview_shift(*, shift, scheduler):
    require_scheduler(scheduler)
    shift = InterviewShift.objects.select_for_update().get(pk=shift.pk)
    capture_calendar_range(shift.date)
    if not shift.is_active:
        return shift
    previous_value = _interview_shift_value(shift)
    shift.is_active = False
    shift.physician = None
    shift.full_clean()
    shift.save(update_fields=("is_active", "physician"))
    _record_interview_shift_change(
        shift=shift,
        action=InterviewShiftChange.Action.REMOVED,
        previous_value=previous_value,
        recorded_value=_interview_shift_value(shift),
        scheduler=scheduler,
    )
    return shift


def _academic_year_bounds(academic_year):
    try:
        return academic_year_bounds(academic_year)
    except TypeError, ValueError:
        raise ValidationError("Choose a valid academic year.") from None


def _require_in_academic_year(on_date, academic_year):
    if not on_date or academic_year_for_date(on_date) != academic_year:
        raise ValidationError("Interview shift dates must fall within the selected academic year.")


def _require_open_interview_date(on_date):
    if is_operational_date(on_date) and (closure := holiday_closure_for(on_date, "AM")):
        raise ValidationError(f"{closure.name} closes this {closure.coverage_label} clinic shift.")


def _interview_projection_rows_on(on_date, *, exclude_interview_shift=None):
    return slot_projection(
        (RequestedSlot(on_date, "AM"),), exclude_interview_shift=exclude_interview_shift
    )


def _interview_projection_rows_by_physician_on(on_date, *, exclude_interview_shift=None):
    return {
        row.physician.pk: row
        for row in _interview_projection_rows_on(
            on_date, exclude_interview_shift=exclude_interview_shift
        )
    }


def _faculty_role(row):
    return next(
        (role for role in row.effective_roles if role.role == EffectiveRole.Role.FACULTY),
        None,
    )


def _interview_availability_reason(availability):
    if availability.availability == "available":
        return "Available after applying the shared Tier 0 scheduling sources."
    if availability.availability == "backup_only":
        return "Available only as backup after applying the shared Tier 0 scheduling sources."
    return "Not available after applying the shared Tier 0 scheduling sources."


def _ordered_availability_sources(sources):
    source_order = {
        "faculty_template": 0,
        "monthly_availability_rule": 1,
        "community_preceptor_rule": 2,
        "vacation": 3,
        "academic_event": 4,
        "normal_rounder": 5,
        "casual_rounder_rule": 6,
    }
    return tuple(
        sorted(sources, key=lambda source: (source_order[source.source_type], source.source_id))
    )


def _interview_shift_value(shift):
    return {
        "date": shift.date.isoformat(),
        "period_of_day": "AM",
        "physician_id": shift.physician_id,
        "physician_display_name": shift.physician.display_name if shift.physician_id else None,
        "note": shift.note,
        "is_active": shift.is_active,
    }


def _record_interview_shift_change(*, shift, action, previous_value, recorded_value, scheduler):
    return InterviewShiftChange.objects.create(
        shift=shift,
        shift_id_at_recording=shift.pk,
        action=action,
        previous_value=previous_value,
        recorded_value=recorded_value,
        changed_by=scheduler,
    )
