"""Bounded, all-physician Tier 0 slot projection reads."""

from dataclasses import dataclass
from datetime import date

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, Value

from ..models.academic_events import AcademicEvent
from ..models.availability_categories import CustomAvailabilityCategory
from ..models.community_preceptors import CommunityPreceptorRule
from ..models.interviews import InterviewShift
from ..models.physicians import (
    EffectiveRole,
    FacultyAvailabilityTemplate,
    MonthlyPhysicianAvailabilityRule,
    Physician,
)
from ..models.preceptors import PreceptorAssignment, PreceptorSession
from ..models.rounding import CasualRounderRule, NormalRounder
from ..models.vacations import Vacation
from . import academic_events, availability, rounding
from .holidays import HolidayProjection, holiday_projections_for_dates


@dataclass(frozen=True, order=True)
class RequestedSlot:
    """One caller-selected AM or PM slot for a bounded scheduling read."""

    on_date: date
    period_of_day: str

    def __post_init__(self):
        if self.period_of_day not in PreceptorSession.PeriodOfDay.values:
            raise ValidationError({"period_of_day": "Period must be AM or PM."})


@dataclass(frozen=True)
class PreceptingConflict:
    """A sibling-schedule commitment that prevents Precepting in this slot."""

    code: str
    source_type: str
    source_id: int | str | None
    record: object
    finding_explanation: dict
    label: str
    reason: str


@dataclass(frozen=True)
class IndependentSlotBlocker:
    """One current fact that independently removes an otherwise scheduled slot."""

    source_type: str
    source_id: int | str | None
    record: object
    label: str


@dataclass(frozen=True)
class SlotProjectionRow:
    """One physician's role, Tier 0 facts, and current occupancy for a slot."""

    slot: RequestedSlot
    physician: Physician
    effective_roles: tuple[EffectiveRole, ...]
    tier_zero: availability.EffectivePreceptorAvailability
    preceptor_assignment: PreceptorAssignment | None
    interview_shift: InterviewShift | None
    precepting_conflicts: tuple[PreceptingConflict, ...]
    holiday_closures: tuple[HolidayProjection, ...]

    @property
    def ordinarily_eligible_to_precept(self):
        return self.tier_zero.ordinarily_eligible_to_precept

    @property
    def preceptor_reason(self):
        return self.tier_zero.preceptor_reason

    @property
    def faculty_equivalent(self):
        return self.tier_zero.faculty_equivalent

    @property
    def faculty_equivalent_reason(self):
        return self.tier_zero.faculty_equivalent_reason

    @property
    def effective_category(self):
        return self.tier_zero.effective_category

    @property
    def base_availability(self):
        return self.tier_zero.base_availability

    @property
    def availability(self):
        return self.tier_zero.availability

    @property
    def is_expected_community_commitment(self):
        return self.tier_zero.is_expected_community_commitment

    @property
    def sources(self):
        return self.tier_zero.sources

    @property
    def has_soft_academic_event(self):
        return self.tier_zero.has_soft_academic_event

    @property
    def independent_blockers(self):
        """Shared work blockers, excluding schedule selection and Precepting."""
        blockers = [
            IndependentSlotBlocker("holiday", None, holiday, f"Holiday closure: {holiday.name}")
            for holiday in self.holiday_closures
        ]
        blockers.extend(
            IndependentSlotBlocker(
                source.source_type, source.source_id, source.record, source.label
            )
            for source in self.tier_zero.independent_blocking_sources
        )
        if self.interview_shift is not None:
            blockers.append(
                IndependentSlotBlocker(
                    "interview_shift",
                    self.interview_shift.pk,
                    self.interview_shift,
                    f"Interview: {self.interview_shift.date.isoformat()}",
                )
            )
        return tuple(blockers)


@dataclass(frozen=True)
class _SlotProjectionSourceFacts:
    roles_by_physician: dict[int, tuple[EffectiveRole, ...]]
    templates_by_physician: dict[int, tuple[FacultyAvailabilityTemplate, ...]]
    monthly_rules_by_physician: dict[int, tuple[MonthlyPhysicianAvailabilityRule, ...]]
    categories_by_id: dict[int, CustomAvailabilityCategory]
    vacations_by_physician: dict[int, tuple[Vacation, ...]]
    events_by_physician: dict[int, tuple[AcademicEvent, ...]]
    normal_rounders_by_week: dict[date, NormalRounder]
    casual_rules_by_weekday: dict[int, tuple[CasualRounderRule, ...]]
    community_rules_by_weekday: dict[int, tuple[CommunityPreceptorRule, ...]]
    preceptor_assignments_by_slot_and_physician: dict[tuple[date, str, int], PreceptorAssignment]
    interview_shifts_by_date: dict[date, InterviewShift]
    holiday_closures_by_slot: dict[tuple[date, str], tuple[HolidayProjection, ...]]


def _slot_projection_source_facts(
    slots,
    physicians,
    *,
    exclude_preceptor_assignment=None,
    exclude_interview_shift=None,
    include_holiday_closures=False,
):
    """Load each Tier 0 source class and Tier 1 occupancy once for bounded slots."""
    if not slots or not physicians:
        return _SlotProjectionSourceFacts({}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {})
    first_date = min(slot.on_date for slot in slots)
    last_date = max(slot.on_date for slot in slots)
    physician_ids = tuple(physician.pk for physician in physicians)
    physicians_by_id = {physician.pk: physician for physician in physicians}
    weekdays = {slot.on_date.weekday() for slot in slots}
    weeks = {rounding.rounding_week_start(slot.on_date) for slot in slots}
    holiday_closures_by_slot = {}
    if include_holiday_closures:
        for holiday in holiday_projections_for_dates({slot.on_date for slot in slots}):
            for period_of_day in ("AM", "PM"):
                if holiday.closes(period_of_day):
                    holiday_closures_by_slot.setdefault(
                        (holiday.observed_date, period_of_day), []
                    ).append(holiday)

    def grouped(records, key):
        result = {}
        for record in records:
            result.setdefault(key(record), []).append(record)
        return {group: tuple(values) for group, values in result.items()}

    roles = EffectiveRole.objects.filter(
        physician_id__in=physician_ids, effective_start__lte=last_date
    ).filter(Q(effective_end__isnull=True) | Q(effective_end__gte=first_date))
    templates = (
        FacultyAvailabilityTemplate.objects.filter(physician_id__in=physician_ids)
        .filter(Q(effective_start__isnull=True) | Q(effective_start__lte=last_date))
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=first_date))
    )
    monthly_rules = (
        MonthlyPhysicianAvailabilityRule.objects.filter(
            physician_id__in=physician_ids, weekday__in=weekdays, is_active=True
        )
        .filter(Q(effective_start__isnull=True) | Q(effective_start__lte=last_date))
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=first_date))
    )
    category_ids = {
        FacultyAvailabilityTemplate.selection_category_id(selection)
        for selection in (
            *(selection for template in templates for selection in template.slots.values()),
            *(rule.availability_selection for rule in monthly_rules),
        )
        if FacultyAvailabilityTemplate.selection_category_id(selection)
    }
    vacations = Vacation.objects.overlapping(first_date, last_date).filter(
        physician_id__in=physician_ids
    )
    events = academic_events.academic_events_overlapping(
        first_date, last_date, participant_ids=physician_ids
    )
    normal_rounders = (
        NormalRounder.objects.filter(week__in=weeks)
        .order_by()
        .annotate(
            source_type=Value("normal", output_field=models.CharField()),
            weekday=Value(None, output_field=models.PositiveSmallIntegerField()),
            ordinals=Value([], output_field=models.JSONField()),
            effective_start=Value(None, output_field=models.DateField()),
            effective_end=Value(None, output_field=models.DateField()),
        )
        .values(
            "source_type",
            "pk",
            "physician_id",
            "week",
            "weekday",
            "ordinals",
            "effective_start",
            "effective_end",
        )
    )
    casual_rules = (
        CasualRounderRule.objects.filter(weekday__in=weekdays)
        .filter(Q(effective_start__isnull=True) | Q(effective_start__lte=last_date))
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=first_date))
        .order_by()
        .annotate(
            source_type=Value("casual", output_field=models.CharField()),
            week=Value(None, output_field=models.DateField()),
        )
        .values(
            "source_type",
            "pk",
            "physician_id",
            "week",
            "weekday",
            "ordinals",
            "effective_start",
            "effective_end",
        )
    )
    community_rules = (
        CommunityPreceptorRule.objects.filter(physician_id__in=physician_ids, weekday__in=weekdays)
        .filter(Q(effective_start__isnull=True) | Q(effective_start__lte=last_date))
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=first_date))
        .select_related("physician")
    )
    requested_session_slots = Q()
    for slot in slots:
        requested_session_slots |= Q(
            session__date=slot.on_date, session__period_of_day=slot.period_of_day
        )
    preceptor_assignments = (
        PreceptorAssignment.objects.filter(requested_session_slots, physician_id__in=physician_ids)
        .exclude(pk=getattr(exclude_preceptor_assignment, "pk", exclude_preceptor_assignment))
        .select_related("session")
    )
    interview_shifts = InterviewShift.objects.none()
    interview_dates = {
        slot.on_date for slot in slots if slot.period_of_day == PreceptorSession.PeriodOfDay.AM
    }
    if interview_dates:
        interview_shifts = InterviewShift.objects.filter(
            date__in=interview_dates, is_active=True
        ).exclude(pk=getattr(exclude_interview_shift, "pk", exclude_interview_shift))
    events_by_physician = {physician_id: [] for physician_id in physician_ids}
    for event in events:
        if event.all_available_faculty:
            for physician_id in events_by_physician:
                events_by_physician[physician_id].append(event)
            continue
        if event._participant_id in events_by_physician:
            event._prefetched_objects_cache["participants"] = (
                physicians_by_id[event._participant_id],
            )
            events_by_physician[event._participant_id].append(event)
    normal_rounders_by_week = {}
    casual_rules_by_weekday = {}
    for source in normal_rounders.union(casual_rules, all=True):
        if source["source_type"] == "normal":
            normal_rounders_by_week[source["week"]] = NormalRounder(
                pk=source["pk"], week=source["week"], physician_id=source["physician_id"]
            )
            continue
        casual_rules_by_weekday.setdefault(source["weekday"], []).append(
            CasualRounderRule(
                pk=source["pk"],
                physician_id=source["physician_id"],
                weekday=source["weekday"],
                ordinals=source["ordinals"],
                effective_start=source["effective_start"],
                effective_end=source["effective_end"],
            )
        )

    return _SlotProjectionSourceFacts(
        roles_by_physician=grouped(roles, lambda role: role.physician_id),
        templates_by_physician=grouped(templates, lambda template: template.physician_id),
        monthly_rules_by_physician=grouped(monthly_rules, lambda rule: rule.physician_id),
        categories_by_id=CustomAvailabilityCategory.objects.in_bulk(category_ids),
        vacations_by_physician=grouped(vacations, lambda vacation: vacation.physician_id),
        events_by_physician={key: tuple(value) for key, value in events_by_physician.items()},
        normal_rounders_by_week=normal_rounders_by_week,
        casual_rules_by_weekday={
            weekday: tuple(rules) for weekday, rules in casual_rules_by_weekday.items()
        },
        community_rules_by_weekday=grouped(community_rules, lambda rule: rule.weekday),
        preceptor_assignments_by_slot_and_physician={
            (
                assignment.session.date,
                assignment.session.period_of_day,
                assignment.physician_id,
            ): assignment
            for assignment in preceptor_assignments
        },
        interview_shifts_by_date={shift.date: shift for shift in interview_shifts},
        holiday_closures_by_slot={
            slot: tuple(closures) for slot, closures in holiday_closures_by_slot.items()
        },
    )


def slot_projection(
    requested_slots,
    *,
    exclude_preceptor_assignment=None,
    exclude_interview_shift=None,
    include_holiday_closures=False,
    physicians=None,
):
    """Return every recorded physician's Tier 0 facts and current Tier 1 occupancy."""
    slots = tuple(sorted(set(requested_slots)))
    if not slots:
        return ()
    physicians = (
        tuple(Physician.objects.order_by("display_name", "pk"))
        if physicians is None
        else tuple(physicians)
    )
    facts = _slot_projection_source_facts(
        slots,
        physicians,
        exclude_preceptor_assignment=exclude_preceptor_assignment,
        exclude_interview_shift=exclude_interview_shift,
        include_holiday_closures=include_holiday_closures,
    )
    rows = []
    for slot in slots:
        for physician in physicians:
            effective_roles = tuple(
                role
                for role in facts.roles_by_physician.get(physician.pk, ())
                if role.effective_start <= slot.on_date
                and (role.effective_end is None or role.effective_end >= slot.on_date)
            )
            interview_shift = (
                facts.interview_shifts_by_date.get(slot.on_date)
                if slot.period_of_day == PreceptorSession.PeriodOfDay.AM
                else None
            )
            direct_interviewer = (
                interview_shift
                if interview_shift and interview_shift.physician_id == physician.pk
                else None
            )
            precepting_conflicts = ()
            if interview_shift:
                if direct_interviewer:
                    precepting_conflicts = (
                        PreceptingConflict(
                            "interview_assignment_conflict",
                            "interview_shift",
                            interview_shift.pk,
                            interview_shift,
                            {
                                "interview_shift_id": interview_shift.pk,
                                "interview_date": interview_shift.date,
                            },
                            "Interview conflict",
                            "Assigned to this AM Interview shift.",
                        ),
                    )
                elif any(
                    role.role == EffectiveRole.Role.FACULTY
                    and role.faculty_leadership == EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR
                    for role in effective_roles
                ):
                    precepting_conflicts = (
                        PreceptingConflict(
                            "program_director_interview_commitment",
                            "interview_shift",
                            interview_shift.pk,
                            interview_shift,
                            {
                                "interview_shift_id": interview_shift.pk,
                                "interview_date": interview_shift.date,
                            },
                            "Program Director Interview",
                            "Committed to this AM Interview shift as Program Director.",
                        ),
                    )
            rows.append(
                SlotProjectionRow(
                    slot=slot,
                    physician=physician,
                    effective_roles=effective_roles,
                    tier_zero=availability._compose_effective_preceptor_availability(
                        physician,
                        slot.on_date,
                        slot.period_of_day,
                        effective_roles=effective_roles,
                        templates=facts.templates_by_physician.get(physician.pk, ()),
                        vacations=facts.vacations_by_physician.get(physician.pk, ()),
                        events=facts.events_by_physician.get(physician.pk, ()),
                        normal_rounder=facts.normal_rounders_by_week.get(
                            rounding.rounding_week_start(slot.on_date)
                        ),
                        casual_rules=facts.casual_rules_by_weekday.get(slot.on_date.weekday(), ()),
                        community_rules=facts.community_rules_by_weekday.get(
                            slot.on_date.weekday(), ()
                        ),
                        monthly_rules=facts.monthly_rules_by_physician.get(physician.pk, ()),
                        categories_by_id=facts.categories_by_id,
                    ),
                    preceptor_assignment=facts.preceptor_assignments_by_slot_and_physician.get(
                        (slot.on_date, slot.period_of_day, physician.pk)
                    ),
                    interview_shift=direct_interviewer,
                    precepting_conflicts=precepting_conflicts,
                    holiday_closures=facts.holiday_closures_by_slot.get(
                        (slot.on_date, slot.period_of_day), ()
                    ),
                )
            )
    return tuple(rows)
