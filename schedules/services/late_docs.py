"""Late Doc assignment policy, candidate reads, mutations, and retained history."""

from dataclasses import dataclass
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction

from ..models.late_docs import LateDocAssignment, LateDocAssignmentChange
from ..models.physicians import Physician
from . import availability as _availability
from ._mutation import (
    capture_calendar_range,
    normalize_note,
    require_scheduler,
    scheduling_mutation,
)
from .holidays import holiday_closure_for
from .operational_attention import is_operational_date
from .preceptors import (
    _assignment_alternative_tier,
    _assignment_availability_reason,
    _ordered_availability_sources,
    is_manually_eligible_for_position,
)
from .slot_projection import RequestedSlot, slot_projection

LATE_DOC_CANDIDATE_TIER_LABELS = {
    "available_other": "Available Faculty",
    "available_soft_academic_event": "Available with a soft Academic Event",
    "backup_only": "Backup-only Faculty",
    "available_as_float": "Available as Float",
    "not_available": "Not-available Faculty",
}
LATE_DOC_CANDIDATE_TIER_ORDER = tuple(LATE_DOC_CANDIDATE_TIER_LABELS)


@dataclass(frozen=True)
class LateDocAssignmentCandidate:
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
    assignment: LateDocAssignment | None = None

    @property
    def requires_override(self):
        return self.tier == "not_available"

    @property
    def availability_sources(self):
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
class LateDocAssignmentCandidates:
    date: date
    current: LateDocAssignmentCandidate | None
    candidates: tuple[LateDocAssignmentCandidate, ...]

    def __iter__(self):
        return iter(self.candidates)

    def __len__(self):
        return len(self.candidates)

    def __getitem__(self, index):
        return self.candidates[index]


def late_doc_assignment_for_date(on_date):
    """Return the current assignment for a Monday, if the incomplete draft has one."""
    _require_monday(on_date)
    return LateDocAssignment.objects.filter(date=on_date).select_related("physician").first()


def late_doc_assignment_changes():
    """Return retained Late Doc assignment history in its existing model ordering."""
    return LateDocAssignmentChange.objects.select_related("assignment", "changed_by")


def late_doc_assignment_candidates_on(on_date):
    """Return all Faculty choices using the Monday PM availability projection."""
    _require_monday(on_date)
    current = late_doc_assignment_for_date(on_date)
    if is_operational_date(on_date) and holiday_closure_for(on_date, "PM"):
        current_candidate = (
            _candidate_from_row(
                _late_doc_projection_rows_by_physician_on(on_date)[current.physician_id],
                assignment=current,
            )
            if current
            else None
        )
        return LateDocAssignmentCandidates(on_date, current_candidate, ())
    rows_by_physician = {
        row.physician.pk: row for row in slot_projection((RequestedSlot(on_date, "PM"),))
    }
    current_candidate = (
        _candidate_from_row(rows_by_physician[current.physician_id], assignment=current)
        if current
        else None
    )
    candidates = [
        _candidate_from_row(row)
        for row in rows_by_physician.values()
        if is_manually_eligible_for_position("late_doc", row.tier_zero)
        and (current is None or row.physician.pk != current.physician_id)
    ]
    candidates.sort(
        key=lambda candidate: (
            LATE_DOC_CANDIDATE_TIER_ORDER.index(candidate.tier),
            candidate.physician.display_name.casefold(),
            candidate.physician.pk,
        )
    )
    return LateDocAssignmentCandidates(on_date, current_candidate, tuple(candidates))


@scheduling_mutation
@transaction.atomic
def assign_late_doc(*, on_date, physician, scheduler, note="", override=False):
    """Create or replace the retained Late Doc assignment for one Monday."""
    require_scheduler(scheduler)
    _require_monday(on_date)
    capture_calendar_range(on_date)
    if is_operational_date(on_date) and (closure := holiday_closure_for(on_date, "PM")):
        raise ValidationError(f"{closure.name} closes this {closure.coverage_label} clinic shift.")
    assignment = (
        LateDocAssignment.objects.select_for_update()
        .filter(date=on_date)
        .select_related("physician")
        .first()
    )
    row = _late_doc_projection_rows_by_physician_on(on_date)[physician.pk]
    if not override and not is_manually_eligible_for_position("late_doc", row.tier_zero):
        raise ValidationError(
            {
                "physician": (
                    "Late Doc assignments require an effective Faculty role or "
                    "Float-only availability on the Monday."
                )
            }
        )
    if not override and row.availability not in ("available", "backup_only", "float_only"):
        raise ValidationError(
            {"physician": "Use the explicit override to retain an unavailable Late Doc assignment."}
        )
    if assignment and assignment.physician_id == physician.pk:
        return assignment
    previous_value = _late_doc_assignment_value(assignment) if assignment else None
    if assignment:
        assignment.physician = physician
        assignment.full_clean()
        assignment.save(update_fields=("physician",))
    else:
        assignment = LateDocAssignment(date=on_date, physician=physician)
        assignment.full_clean()
        assignment.save()
    _record_late_doc_assignment_change(
        assignment=assignment,
        action=LateDocAssignmentChange.Action.ASSIGNED,
        previous_value=previous_value,
        recorded_value=_late_doc_assignment_value(assignment),
        scheduler=scheduler,
        note=note,
    )
    return assignment


@scheduling_mutation
@transaction.atomic
def remove_late_doc_assignment(*, assignment, scheduler, note=""):
    """Remove the current assignment while retaining a complete history record."""
    require_scheduler(scheduler)
    assignment = (
        LateDocAssignment.objects.select_for_update()
        .select_related("physician")
        .get(pk=assignment.pk)
    )
    capture_calendar_range(assignment.date)
    _record_late_doc_assignment_change(
        assignment=assignment,
        action=LateDocAssignmentChange.Action.REMOVED,
        previous_value=_late_doc_assignment_value(assignment),
        recorded_value=None,
        scheduler=scheduler,
        note=note,
    )
    assignment.delete()


def _require_monday(on_date):
    if not isinstance(on_date, date) or on_date.weekday() != 0:
        raise ValidationError({"date": "Late Doc assignments must be on a Monday."})


def _late_doc_projection_rows_by_physician_on(on_date):
    return {row.physician.pk: row for row in slot_projection((RequestedSlot(on_date, "PM"),))}


def _candidate_from_row(row, *, assignment=None):
    tier = "current" if assignment else _assignment_alternative_tier(row.tier_zero)
    consequence = None
    if tier == "backup_only":
        consequence = (
            "Backup only: selecting this physician creates a non-blocking preference finding."
        )
    elif tier == "available_soft_academic_event":
        consequence = "Soft Academic Event: selecting this physician creates a non-blocking preference finding."
    elif tier == "not_available":
        consequence = "Not available: selecting this physician creates a hard availability finding."
    return LateDocAssignmentCandidate(
        physician=row.physician,
        effective_category=row.effective_category,
        ordinarily_eligible_to_precept=row.ordinarily_eligible_to_precept,
        preceptor_reason=row.preceptor_reason,
        faculty_equivalent=row.faculty_equivalent,
        faculty_equivalent_reason=row.faculty_equivalent_reason,
        is_applicable=row.tier_zero.is_applicable,
        availability=row.availability,
        base_availability=row.base_availability,
        is_expected_community_commitment=row.is_expected_community_commitment,
        sources=_ordered_availability_sources(row.sources),
        tier=tier,
        tier_label=("Current assignment" if assignment else LATE_DOC_CANDIDATE_TIER_LABELS[tier]),
        availability_reason=_assignment_availability_reason(row.tier_zero, tier),
        selection_consequence=consequence,
        assignment=assignment,
    )


def _late_doc_assignment_value(assignment):
    return {
        "date": assignment.date.isoformat(),
        "physician_id": assignment.physician_id,
        "physician_display_name": assignment.physician.display_name,
    }


def _record_late_doc_assignment_change(**kwargs):
    kwargs["assignment_id_at_recording"] = kwargs["assignment"].pk
    kwargs["date"] = kwargs["assignment"].date
    kwargs["note"] = normalize_note(kwargs["note"])
    kwargs["changed_by"] = kwargs.pop("scheduler")
    return LateDocAssignmentChange.objects.create(**kwargs)
