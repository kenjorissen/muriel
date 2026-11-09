"""Tier 0 preceptor availability composed from scheduling source facts."""

from dataclasses import dataclass
from datetime import date

from django.core.exceptions import ValidationError
from django.db.models import Q

from ..models.academic_events import AcademicEvent
from ..models.availability_categories import CustomAvailabilityCategory
from ..models.community_preceptors import CommunityPreceptorRule
from ..models.physicians import (
    EffectiveRole,
    FacultyAvailabilityTemplate,
    MonthlyPhysicianAvailabilityRule,
    Physician,
)
from ..models.preceptors import PreceptorSession
from ..models.rounding import CasualRounderRule, NormalRounder
from ..models.vacations import Vacation
from . import academic_events as _academic_events
from . import community as _community
from . import rounding as _rounding
from . import vacations as _vacations


@dataclass(frozen=True)
class EffectiveAvailabilitySource:
    """One retained fact that contributes to an effective availability result."""

    source_type: str
    source_id: int
    contribution: str
    reason: str
    record: object
    selection: dict | None = None

    @property
    def label(self):
        if self.source_type == "faculty_template":
            label = f"Faculty Schedule template {self.source_id}"
            if self.selection and "category_name" in self.selection:
                return f"{label}: {self.selection['category_name']}"
            return label
        if self.source_type == "monthly_availability_rule":
            label = f"Monthly scheduling rule {self.source_id}"
            if self.selection and "category_name" in self.selection:
                return f"{label}: {self.selection['category_name']}"
            return label
        if self.source_type == "community_preceptor_rule":
            return f"Community Preceptor rule {self.source_id}"
        if self.source_type == "vacation":
            return f"Vacation {self.source_id}"
        if self.source_type == "academic_event":
            return f"Academic Event: {self.record.title}"
        if self.source_type == "external_patient_care":
            return f"External Patient Care role {self.source_id}"
        if self.source_type == "normal_rounder":
            return f"Normal rounder week {self.record.week.isoformat()}"
        return f"Casual-rounder rule {self.source_id}"

    @property
    def independently_blocks_period(self):
        """Whether this source removes a scheduled work period on its own."""
        return self.contribution == "not_available" and self.source_type not in {
            "faculty_template",
            "monthly_availability_rule",
        }


@dataclass(frozen=True)
class EffectivePreceptorAvailability:
    """Read-only composition of current preceptor source facts for one AM/PM session."""

    physician: Physician
    on_date: date
    period_of_day: str
    is_applicable: bool
    availability: str | None
    ordinarily_eligible_to_precept: bool
    preceptor_reason: str | None
    faculty_equivalent: bool
    faculty_equivalent_reason: str | None
    effective_category: str | None
    base_availability: str | None
    availability_selection: dict | None
    is_expected_community_commitment: bool
    sources: tuple[EffectiveAvailabilitySource, ...]
    faculty_template: FacultyAvailabilityTemplate | None
    monthly_availability_rule: MonthlyPhysicianAvailabilityRule | None
    community_preceptor_rule: CommunityPreceptorRule | None
    vacations: tuple[Vacation, ...]
    academic_events: tuple[AcademicEvent, ...]
    normal_rounder: NormalRounder | None
    casual_rounder_rules: tuple[CasualRounderRule, ...]

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
    def expected_community_commitment(self):
        return self.is_expected_community_commitment

    @property
    def contributing_sources(self):
        return self.sources

    @property
    def expected_but_blocked(self):
        return self.is_expected_community_commitment and self.availability == "not_available"

    @property
    def is_float_only(self):
        """Whether Admin + Float makes this a manual-only availability period."""
        return self.availability == "float_only"

    @property
    def has_soft_academic_event(self):
        return any(
            source.source_type == "academic_event" and source.contribution == "soft"
            for source in self.sources
        )

    @property
    def independent_blocking_sources(self):
        """Current non-schedule facts that make this period unavailable."""
        return tuple(source for source in self.sources if source.independently_blocks_period)

    @property
    def availability_display(self):
        if not self.is_applicable:
            return "Not applicable — no weekend preceptor session"
        if self.availability is None:
            return "Not eligible to precept"
        if self.is_float_only:
            return "Available as Float"
        return {
            "available": "Available",
            "backup_only": "Backup only",
            "not_available": "Not available",
        }[self.availability]

    @property
    def effective_category_display(self):
        if self.effective_category is None:
            return "None"
        return EffectiveRole.Role(self.effective_category).label

    @property
    def base_availability_display(self):
        if not self.is_applicable:
            return "Not applicable — no weekend preceptor session"
        if self.availability_selection:
            return FacultyAvailabilityTemplate.selection_label(self.availability_selection)
        if self.base_availability is None:
            return "No preceptor base schedule applies"
        if self.base_availability in FacultyAvailabilityTemplate.AvailabilityState.values:
            return FacultyAvailabilityTemplate.AvailabilityState(self.base_availability).label
        return CustomAvailabilityCategory.Impact(self.base_availability).label

    @property
    def availability_selection_display(self):
        return self.base_availability_display


def _compose_effective_preceptor_availability(
    physician,
    on_date,
    period_of_day,
    *,
    effective_roles,
    templates,
    vacations,
    events,
    normal_rounder,
    casual_rules,
    community_rules,
    monthly_rules=(),
    categories_by_id=None,
):
    effective_roles = tuple(
        role
        for role in effective_roles
        if role.effective_start <= on_date
        and (role.effective_end is None or role.effective_end >= on_date)
    )
    roles = {role.role for role in effective_roles}
    faculty = EffectiveRole.Role.FACULTY in roles
    community = EffectiveRole.Role.COMMUNITY_PRECEPTOR in roles
    floating = EffectiveRole.Role.FLOAT_PRECEPTOR in roles
    external_patient_care = next(
        (role for role in effective_roles if role.role == EffectiveRole.Role.EXTERNAL_PATIENT_CARE),
        None,
    )
    qualified = any(
        role.role == EffectiveRole.Role.COMMUNITY_PRECEPTOR and role.faculty_equivalent
        for role in effective_roles
    )
    category = (
        EffectiveRole.Role.FACULTY
        if faculty
        else (EffectiveRole.Role.COMMUNITY_PRECEPTOR if community else None)
    )
    preceptor_reason = (
        "Effective Faculty role"
        if faculty
        else "Effective community-preceptor role"
        if community
        else None
    )
    faculty_equivalent_reason = (
        "Effective Faculty role"
        if faculty
        else "Effective faculty-equivalent Community Preceptor role"
        if community and qualified
        else None
    )
    common = {
        "physician": physician,
        "on_date": on_date,
        "period_of_day": period_of_day,
        "ordinarily_eligible_to_precept": faculty or community,
        "preceptor_reason": preceptor_reason,
        "faculty_equivalent": faculty or (community and qualified),
        "faculty_equivalent_reason": faculty_equivalent_reason,
        "effective_category": category,
    }
    if on_date.weekday() >= 5:
        return EffectivePreceptorAvailability(
            **common,
            is_applicable=False,
            availability=None,
            base_availability=None,
            availability_selection=None,
            is_expected_community_commitment=False,
            sources=(),
            faculty_template=None,
            monthly_availability_rule=None,
            community_preceptor_rule=None,
            vacations=(),
            academic_events=(),
            normal_rounder=None,
            casual_rounder_rules=(),
        )

    vacations = tuple(
        vacation
        for vacation in vacations
        if _vacations.vacation_covers(vacation, on_date, period_of_day)
    )
    events = tuple(
        event
        for event in events
        if event.precepting_impact != AcademicEvent.PreceptingImpact.NONE
        and _academic_events.academic_event_covers(
            event,
            physician,
            on_date,
            period_of_day,
            effective_roles=effective_roles,
        )
    )
    sources = []
    faculty_template = None
    monthly_availability_rule = None
    community_rule = None
    expected_community_commitment = False
    base_availability = None
    availability_selection = None
    states = []
    schedule_template = next(
        (
            template
            for template in templates
            if (template.effective_start is None or template.effective_start <= on_date)
            and (template.effective_end is None or template.effective_end >= on_date)
        ),
        None,
    )
    schedule_monthly_rule = next(
        (
            rule
            for rule in monthly_rules
            if rule.physician_id == physician.pk and rule.matches(on_date, period_of_day)
        ),
        None,
    )
    schedule_selection = None
    schedule_source = None
    if schedule_monthly_rule:
        schedule_selection = schedule_monthly_rule.availability_selection
        schedule_source = ("monthly_availability_rule", schedule_monthly_rule)
    elif schedule_template:
        slot = (
            f"{schedule_template.SLOT_CHOICES[on_date.weekday() * 2][0].split('_')[0]}_"
            f"{period_of_day.lower()}"
        )
        schedule_selection = schedule_template.slots[slot]
        schedule_source = ("faculty_template", schedule_template)

    if category == EffectiveRole.Role.FACULTY:
        faculty_template = schedule_template
        monthly_availability_rule = schedule_monthly_rule
        if monthly_availability_rule:
            availability_selection = schedule_selection
            base_availability = _selection_base_state(availability_selection, categories_by_id)
        elif faculty_template:
            availability_selection = schedule_selection
            base_availability = _selection_base_state(availability_selection, categories_by_id)
        else:
            base_availability = FacultyAvailabilityTemplate.AvailabilityState.OFF
        base_impact = (
            _selection_impact(availability_selection, categories_by_id)
            if availability_selection
            else FacultyAvailabilityTemplate.base_state_impact(base_availability)
        )
        patient_care = (
            base_availability == FacultyAvailabilityTemplate.AvailabilityState.PATIENT_CARE
        )
        states.append(
            "not_available"
            if patient_care and external_patient_care
            else "backup_only"
            if patient_care
            else base_impact
        )
        if monthly_availability_rule:
            sources.append(
                EffectiveAvailabilitySource(
                    "monthly_availability_rule",
                    monthly_availability_rule.pk,
                    base_impact,
                    (
                        "custom_availability_category"
                        if "category_id" in availability_selection
                        else "base_availability"
                    ),
                    monthly_availability_rule,
                    availability_selection,
                )
            )
        elif faculty_template:
            sources.append(
                EffectiveAvailabilitySource(
                    "faculty_template",
                    faculty_template.pk,
                    base_impact,
                    (
                        "custom_availability_category"
                        if "category_id" in availability_selection
                        else "base_availability"
                    ),
                    faculty_template,
                    availability_selection,
                )
            )
        if patient_care and external_patient_care:
            sources.append(
                EffectiveAvailabilitySource(
                    "external_patient_care",
                    external_patient_care.pk,
                    "not_available",
                    "external_patient_care",
                    external_patient_care,
                )
            )
    elif category == EffectiveRole.Role.COMMUNITY_PRECEPTOR:
        community_rule = next(
            (
                rule
                for rule in community_rules
                if rule.physician_id == physician.pk and rule.matches(on_date, period_of_day)
            ),
            None,
        )
        base_availability = "available" if community_rule else "not_available"
        expected_community_commitment = community_rule is not None
        states.append(base_availability)
        if community_rule:
            sources.append(
                EffectiveAvailabilitySource(
                    "community_preceptor_rule",
                    community_rule.pk,
                    base_availability,
                    "expected_community_commitment",
                    community_rule,
                )
            )
        if floating and _is_admin_selection(schedule_selection, categories_by_id):
            availability_selection = schedule_selection
            if schedule_source[0] == "monthly_availability_rule":
                monthly_availability_rule = schedule_source[1]
            else:
                faculty_template = schedule_source[1]

    float_source = floating and _is_admin_selection(availability_selection, categories_by_id)
    if float_source and category == EffectiveRole.Role.FACULTY:
        # Admin normally has a not-available impact.  It becomes a separate
        # manual-only path only for an effective Float Preceptor.
        states = []
    if float_source and category == EffectiveRole.Role.COMMUNITY_PRECEPTOR:
        # Community recurrence remains the ordinary source.  Admin adds Float
        # availability only when that recurrence is absent.
        states = [] if not community_rule else states
    for vacation in vacations:
        sources.append(
            EffectiveAvailabilitySource(
                "vacation", vacation.pk, "not_available", "vacation", vacation
            )
        )
        if category:
            states.append("not_available")
    for event in events:
        contribution = (
            "not_available"
            if event.precepting_impact == AcademicEvent.PreceptingImpact.HARD
            else "soft"
        )
        sources.append(
            EffectiveAvailabilitySource(
                "academic_event", event.pk, contribution, "academic_event", event
            )
        )
        if category and event.precepting_impact == AcademicEvent.PreceptingImpact.HARD:
            states.append("not_available")
    if not (normal_rounder and normal_rounder.physician_id == physician.pk):
        normal_rounder = None
    matching_casual_rules = ()
    if normal_rounder:
        matching_casual_rules = tuple(rule for rule in casual_rules if rule.matches(on_date))
        rounding_availability = "backup_only" if matching_casual_rules else "not_available"
        sources.append(
            EffectiveAvailabilitySource(
                "normal_rounder",
                normal_rounder.pk,
                rounding_availability,
                "normal_rounding",
                normal_rounder,
            )
        )
        states.append(rounding_availability)
        for rule in matching_casual_rules:
            sources.append(
                EffectiveAvailabilitySource(
                    "casual_rounder_rule", rule.pk, "backup_only", "casual_rounding_coverage", rule
                )
            )
    if float_source:
        source_type, record = schedule_source
        sources.append(
            EffectiveAvailabilitySource(
                source_type,
                record.pk,
                "float_only",
                "manual_float_availability",
                record,
                availability_selection,
            )
        )
        if not states:
            states.append("float_only")
    availability = (
        min(
            states,
            key={"not_available": 0, "backup_only": 1, "float_only": 2, "available": 3}.get,
        )
        if category
        else None
    )
    return EffectivePreceptorAvailability(
        **common,
        is_applicable=True,
        availability=availability,
        base_availability=base_availability,
        availability_selection=availability_selection,
        is_expected_community_commitment=expected_community_commitment,
        sources=tuple(sources),
        faculty_template=faculty_template,
        monthly_availability_rule=monthly_availability_rule,
        community_preceptor_rule=community_rule,
        vacations=vacations,
        academic_events=events,
        normal_rounder=normal_rounder,
        casual_rounder_rules=matching_casual_rules,
    )


def effective_preceptor_availability_on(physician, on_date, period_of_day):
    """Resolve one weekday preceptor session without creating or changing records."""
    if period_of_day not in PreceptorSession.PeriodOfDay.values:
        raise ValidationError({"period_of_day": "Period must be AM or PM."})
    effective_roles = EffectiveRole.objects.filter(
        physician=physician, effective_start__lte=on_date
    ).filter(Q(effective_end__isnull=True) | Q(effective_end__gte=on_date))
    templates = (
        FacultyAvailabilityTemplate.objects.filter(physician=physician)
        .filter(Q(effective_start__isnull=True) | Q(effective_start__lte=on_date))
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=on_date))
    )
    monthly_rules = (
        MonthlyPhysicianAvailabilityRule.objects.filter(
            physician=physician, weekday=on_date.weekday(), is_active=True
        )
        .filter(Q(effective_start__isnull=True) | Q(effective_start__lte=on_date))
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=on_date))
    )
    categories_by_id = _categories_for_templates(templates, monthly_rules)
    return _compose_effective_preceptor_availability(
        physician,
        on_date,
        period_of_day,
        effective_roles=effective_roles,
        templates=templates,
        vacations=_vacations.vacations_overlapping(on_date, on_date).filter(physician=physician),
        events=_academic_events.academic_events_affecting_physician(physician, on_date, on_date),
        normal_rounder=_rounding.normal_rounder_for_date(on_date),
        casual_rules=_rounding.casual_rounder_rules_on(on_date),
        community_rules=_community.community_preceptor_rules_on(on_date),
        monthly_rules=monthly_rules,
        categories_by_id=categories_by_id,
    )


def effective_preceptor_availability_for_physicians(physicians, on_date, period_of_day):
    """Resolve a bounded caller-selected set without granting mutation authority."""
    physicians = tuple(physicians)
    if not physicians:
        return ()
    from .slot_projection import RequestedSlot, slot_projection

    rows_by_physician = {
        row.physician.pk: row
        for row in slot_projection((RequestedSlot(on_date, period_of_day),), physicians=physicians)
    }
    return tuple(rows_by_physician[physician.pk].tier_zero for physician in physicians)


def effective_preceptor_availability_for_session(session, physicians):
    return effective_preceptor_availability_for_physicians(
        physicians, session.date, session.period_of_day
    )


effective_availability_on = effective_preceptor_availability_on
effective_availability_for_physicians = effective_preceptor_availability_for_physicians


def _categories_for_templates(templates, monthly_rules=()):
    templates = tuple(templates)
    monthly_rules = tuple(monthly_rules)
    category_ids = {
        FacultyAvailabilityTemplate.selection_category_id(selection)
        for selection in (
            *(selection for template in templates for selection in template.slots.values()),
            *(rule.availability_selection for rule in monthly_rules),
        )
        if FacultyAvailabilityTemplate.selection_category_id(selection)
    }
    return CustomAvailabilityCategory.objects.in_bulk(category_ids)


def _selection_impact(selection, categories_by_id):
    if "base_state" in selection:
        return FacultyAvailabilityTemplate.base_state_impact(selection["base_state"])
    category_id = selection["category_id"]
    category = (
        categories_by_id.get(category_id)
        if categories_by_id is not None
        else CustomAvailabilityCategory.objects.get(pk=category_id)
    )
    return category.impact


def _selection_base_state(selection, categories_by_id):
    if "base_state" in selection:
        return selection["base_state"]
    return _selection_impact(selection, categories_by_id)


def _is_admin_selection(selection, categories_by_id):
    if not selection or "category_id" not in selection:
        return False
    category = (
        categories_by_id.get(selection["category_id"])
        if categories_by_id is not None
        else CustomAvailabilityCategory.objects.filter(pk=selection["category_id"]).first()
    )
    return bool(category and category.system_key == CustomAvailabilityCategory.SystemKey.ADMIN)
