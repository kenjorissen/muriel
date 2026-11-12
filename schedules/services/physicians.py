"""Physician identity, effective-role, and Faculty availability sources."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from ..models.availability_categories import (
    CustomAvailabilityCategory,
    CustomAvailabilityCategoryAssignment,
)
from ..models.late_docs import LateDocAssignment
from ..models.physicians import (
    EffectiveRole,
    FacultyAvailabilityTemplate,
    MonthlyPhysicianAvailabilityRule,
    Physician,
    PhysicianChange,
    has_automatic_admin_option_overlap,
    has_schedule_input_role_overlap,
)
from ..models.preceptors import PreceptorAssignment, PreceptorSession
from ._mutation import (
    capture_calendar_range,
    normalize_note,
    require_scheduler,
    scheduling_administration_mutation,
    scheduling_mutation,
)
from ._recurrence import normalize_ordinals
from .operational_attention import organization_today


@dataclass(frozen=True)
class PhysicianSchedulingStatus:
    effective_roles: tuple[str, ...]
    ordinarily_eligible_to_precept: bool
    preceptor_reason: str | None
    faculty_equivalent: bool
    faculty_equivalent_reason: str | None


@dataclass(frozen=True)
class FacultyAvailabilityStatus:
    base_availability: str | None
    availability_selection: dict | None
    is_applicable: bool
    source: str | None
    supporting_template: FacultyAvailabilityTemplate | None
    faculty_eligible: bool
    faculty_reason: str | None

    @property
    def base_availability_display(self):
        if not self.is_applicable:
            return "Not applicable — no weekend preceptor session"
        if self.availability_selection:
            return FacultyAvailabilityTemplate.selection_label(self.availability_selection)
        return FacultyAvailabilityTemplate.AvailabilityState(self.base_availability).label

    @property
    def backup_only(self):
        """This is a restriction on base availability, never a backup assignment."""
        return self.base_availability == FacultyAvailabilityTemplate.AvailabilityState.BACKUP_ONLY


@dataclass(frozen=True)
class _FacultyAvailabilitySources:
    """The bounded source facts needed to resolve one week's base capacity."""

    roles: tuple[EffectiveRole, ...]
    templates: tuple[FacultyAvailabilityTemplate, ...]
    monthly_rules: tuple[MonthlyPhysicianAvailabilityRule, ...]
    categories_by_id: dict[int, CustomAvailabilityCategory]


@scheduling_administration_mutation
@transaction.atomic
def create_physician(
    *,
    display_name,
    scheduler,
    reason="",
    roles=None,
    role=None,
    effective_start=None,
    effective_end=None,
):
    role_specs = _normalize_initial_role_specs(
        roles=roles,
        role=role,
        effective_start=effective_start,
        effective_end=effective_end,
    )
    physician = Physician(display_name=display_name)
    physician.full_clean()
    physician.save()
    _record_change(
        physician=physician,
        subject=PhysicianChange.Subject.IDENTITY,
        subject_id=physician.pk,
        previous_value=None,
        recorded_value=_physician_value(physician),
        scheduler=scheduler,
        reason=reason,
    )
    for role_spec in role_specs:
        add_role(physician=physician, scheduler=scheduler, reason=reason, **role_spec)
    return physician


@scheduling_administration_mutation
@transaction.atomic
def correct_physician_identity(*, physician, display_name, scheduler, reason=""):
    # Retained assignments display the live name, including historical months
    # outside the contiguous future propagation horizon.
    for month in PreceptorAssignment.objects.filter(physician=physician).dates(
        "session__date", "month"
    ):
        capture_calendar_range(month)
    for month in LateDocAssignment.objects.filter(physician=physician).dates("date", "month"):
        capture_calendar_range(month)
    previous_value = _physician_value(physician)
    physician.display_name = display_name
    physician.full_clean()
    physician.save(update_fields=("display_name",))
    _record_change(
        physician=physician,
        subject=PhysicianChange.Subject.IDENTITY,
        subject_id=physician.pk,
        previous_value=previous_value,
        recorded_value=_physician_value(physician),
        scheduler=scheduler,
        reason=reason,
    )
    return physician


@scheduling_administration_mutation
@transaction.atomic
def set_faculty_precepting_capacity_override(*, physician, capacity_override, scheduler, reason=""):
    """Set or clear the physician's current Faculty workload target."""
    physician = Physician.objects.select_for_update().get(pk=physician.pk)
    capacity_override = Decimal(capacity_override) if capacity_override is not None else None
    previous_value = _faculty_precepting_capacity_override_value(physician)
    if physician.faculty_precepting_capacity_override == capacity_override:
        return physician
    physician.faculty_precepting_capacity_override = capacity_override
    physician.full_clean()
    physician.save(update_fields=("faculty_precepting_capacity_override",))
    _record_change(
        physician=physician,
        subject=PhysicianChange.Subject.FACULTY_PRECEPTING_CAPACITY_OVERRIDE,
        subject_id=physician.pk,
        previous_value=previous_value,
        recorded_value=_faculty_precepting_capacity_override_value(physician),
        scheduler=scheduler,
        reason=reason,
    )
    return physician


def faculty_precepting_capacity_for_week(physician, on_date, *, sources=None):
    """Return the selected week's schedule-derived Faculty Precepting capacity."""
    monday = on_date - timedelta(days=on_date.weekday())
    capacity = 0
    for weekday_offset in range(5):
        weekday = monday + timedelta(days=weekday_offset)
        for period_of_day in PreceptorSession.PeriodOfDay.values:
            availability = faculty_availability_on(
                physician, weekday, period_of_day, sources=sources
            )
            if availability.faculty_eligible and availability.base_availability == "open":
                capacity += 1
    return Decimal(capacity)


def faculty_precepting_capacities_for_week(physicians, on_date):
    """Return weekly schedule-derived capacities without a query per directory row."""
    physicians = tuple(physicians)
    physician_ids = tuple(physician.pk for physician in physicians)
    if not physician_ids:
        return {}

    monday = on_date - timedelta(days=on_date.weekday())
    friday = monday + timedelta(days=4)
    roles = EffectiveRole.objects.filter(
        physician_id__in=physician_ids,
        role=EffectiveRole.Role.FACULTY,
        effective_start__lte=friday,
    ).filter(Q(effective_end__isnull=True) | Q(effective_end__gte=monday))
    templates = (
        FacultyAvailabilityTemplate.objects.filter(physician_id__in=physician_ids)
        .filter(Q(effective_start__isnull=True) | Q(effective_start__lte=friday))
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=monday))
    )
    monthly_rules = (
        MonthlyPhysicianAvailabilityRule.objects.filter(
            physician_id__in=physician_ids,
            is_active=True,
        )
        .filter(Q(effective_start__isnull=True) | Q(effective_start__lte=friday))
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=monday))
    )
    roles_by_physician = defaultdict(list)
    templates_by_physician = defaultdict(list)
    rules_by_physician = defaultdict(list)
    for role in roles:
        roles_by_physician[role.physician_id].append(role)
    for template in templates:
        templates_by_physician[template.physician_id].append(template)
    for rule in monthly_rules:
        rules_by_physician[rule.physician_id].append(rule)
    category_ids = {
        FacultyAvailabilityTemplate.selection_category_id(selection)
        for selection in (
            *(
                selection
                for physician_templates in templates_by_physician.values()
                for template in physician_templates
                for selection in template.slots.values()
            ),
            *(
                rule.availability_selection
                for rules in rules_by_physician.values()
                for rule in rules
            ),
        )
        if FacultyAvailabilityTemplate.selection_category_id(selection)
    }
    categories_by_id = CustomAvailabilityCategory.objects.in_bulk(category_ids)
    return {
        physician.pk: faculty_precepting_capacity_for_week(
            physician,
            on_date,
            sources=_FacultyAvailabilitySources(
                roles=tuple(roles_by_physician[physician.pk]),
                templates=tuple(templates_by_physician[physician.pk]),
                monthly_rules=tuple(rules_by_physician[physician.pk]),
                categories_by_id=categories_by_id,
            ),
        )
        for physician in physicians
    }


def physician_for_user(user):
    """Return the physician explicitly represented by a user, if any."""
    if not user or not getattr(user, "pk", None):
        return None
    return Physician.objects.filter(user=user).first()


@scheduling_administration_mutation
@transaction.atomic
def link_physician_user(*, physician, user, scheduler, reason=""):
    """Link or replace the account that represents a physician."""
    physician = Physician.objects.select_for_update().get(pk=physician.pk)
    user = get_user_model().objects.select_for_update().get(pk=user.pk)
    _validate_linkable_user(user)
    linked_elsewhere = (
        Physician.objects.select_for_update().filter(user=user).exclude(pk=physician.pk).first()
    )
    if linked_elsewhere:
        raise ValidationError({"user": "This account already represents another physician."})
    previous_value = _account_identity_value(physician.user)
    recorded_value = _account_identity_value(user)
    if previous_value == recorded_value:
        return physician
    physician.user = user
    physician.save(update_fields=("user",))
    _record_change(
        physician=physician,
        subject=PhysicianChange.Subject.ACCOUNT_IDENTITY,
        subject_id=user.pk,
        previous_value=previous_value,
        recorded_value=recorded_value,
        scheduler=scheduler,
        reason=reason,
    )
    return physician


@scheduling_administration_mutation
@transaction.atomic
def unlink_physician_user(*, physician, scheduler, reason=""):
    """Remove a physician's explicit account relationship without deleting either record."""
    # PostgreSQL cannot lock the nullable side of an outer join. Keep both
    # existing rows locked, but acquire them separately in this transaction.
    physician = Physician.objects.select_for_update().get(pk=physician.pk)
    if physician.user_id is None:
        return physician
    user = get_user_model().objects.select_for_update().get(pk=physician.user_id)
    previous_value = _account_identity_value(user)
    physician.user = None
    physician.save(update_fields=("user",))
    _record_change(
        physician=physician,
        subject=PhysicianChange.Subject.ACCOUNT_IDENTITY,
        subject_id=None,
        previous_value=previous_value,
        recorded_value=None,
        scheduler=scheduler,
        reason=reason,
    )
    return physician


@scheduling_administration_mutation
@transaction.atomic
def add_role(
    *,
    physician,
    role,
    effective_start,
    effective_end,
    faculty_equivalent=False,
    faculty_leadership="",
    scheduler,
    reason="",
):
    effective_role = EffectiveRole(
        physician=physician,
        role=role,
        effective_start=effective_start,
        effective_end=effective_end,
        faculty_equivalent=(
            faculty_equivalent if role == EffectiveRole.Role.COMMUNITY_PRECEPTOR else False
        ),
        faculty_leadership=(faculty_leadership if role == EffectiveRole.Role.FACULTY else ""),
    )
    effective_role.full_clean()
    effective_role.save()
    _record_change(
        physician=physician,
        subject=PhysicianChange.Subject.ROLE,
        subject_id=effective_role.pk,
        previous_value=None,
        recorded_value=_role_value(effective_role),
        scheduler=scheduler,
        reason=reason,
    )
    return effective_role


@scheduling_administration_mutation
@transaction.atomic
def correct_role(
    *,
    effective_role,
    effective_start,
    effective_end,
    scheduler,
    reason="",
    role=None,
    faculty_equivalent=None,
    faculty_leadership=None,
):
    stored_role = EffectiveRole.objects.select_for_update().get(pk=effective_role.pk)
    previous_value = _role_value(stored_role)
    stored_role.role = role if role is not None else stored_role.role
    stored_role.effective_start = effective_start
    stored_role.effective_end = effective_end
    if stored_role.role == EffectiveRole.Role.COMMUNITY_PRECEPTOR:
        stored_role.faculty_equivalent = (
            stored_role.faculty_equivalent if faculty_equivalent is None else faculty_equivalent
        )
    else:
        stored_role.faculty_equivalent = False
    if stored_role.role == EffectiveRole.Role.FACULTY:
        stored_role.faculty_leadership = (
            stored_role.faculty_leadership if faculty_leadership is None else faculty_leadership
        )
    else:
        stored_role.faculty_leadership = ""
    stored_role.full_clean()
    stored_role.save(
        update_fields=(
            "role",
            "effective_start",
            "effective_end",
            "faculty_equivalent",
            "faculty_leadership",
        )
    )
    effective_role.role = stored_role.role
    effective_role.effective_start = stored_role.effective_start
    effective_role.effective_end = stored_role.effective_end
    effective_role.faculty_equivalent = stored_role.faculty_equivalent
    effective_role.faculty_leadership = stored_role.faculty_leadership
    _record_change(
        physician=stored_role.physician,
        subject=PhysicianChange.Subject.ROLE,
        subject_id=stored_role.pk,
        previous_value=previous_value,
        recorded_value=_role_value(stored_role),
        scheduler=scheduler,
        reason=reason,
    )
    return effective_role


def end_role(*, effective_role, effective_end, scheduler, reason=""):
    return correct_role(
        effective_role=effective_role,
        effective_start=effective_role.effective_start,
        effective_end=effective_end,
        faculty_equivalent=effective_role.faculty_equivalent,
        faculty_leadership=effective_role.faculty_leadership,
        scheduler=scheduler,
        reason=reason,
    )


@scheduling_mutation
@transaction.atomic
def create_faculty_availability_template(
    *, physician, effective_start, effective_end, slots, scheduler, reason=""
):
    require_scheduler(scheduler)
    context_date = effective_start or organization_today()
    _require_schedule_input_role(
        physician, effective_start, effective_end, "Faculty Schedule templates"
    )

    slots = _normalized_faculty_slots(
        physician,
        slots,
        effective_start=effective_start,
        effective_end=effective_end,
    )
    _reject_unassigned_current_or_future_selections(
        physician, slots.values(), effective_end, context_date
    )
    template = FacultyAvailabilityTemplate(
        physician=physician,
        effective_start=effective_start,
        effective_end=effective_end,
        slots=slots,
    )
    matching_template = FacultyAvailabilityTemplate.objects.filter(
        physician=physician, effective_start=effective_start, effective_end=effective_end
    ).first()
    if matching_template and matching_template.slots == slots:
        return matching_template
    template.full_clean()
    template.save()
    _record_change(
        physician=physician,
        subject=PhysicianChange.Subject.FACULTY_AVAILABILITY_TEMPLATE,
        subject_id=template.pk,
        previous_value=None,
        recorded_value=_faculty_availability_value(template),
        scheduler=scheduler,
        reason=reason,
    )
    return template


@scheduling_mutation
@transaction.atomic
def correct_faculty_availability_template(
    *, template, effective_start, effective_end, slots, scheduler, reason=""
):
    require_scheduler(scheduler)
    context_date = effective_start or organization_today()
    previous_value = _faculty_availability_value(template)
    slots = _normalized_faculty_slots(
        physician=template.physician,
        slots=slots,
        previous=template.slots,
        effective_start=effective_start,
        effective_end=effective_end,
    )
    _reject_unassigned_current_or_future_selections(
        template.physician, slots.values(), effective_end, context_date
    )
    recorded_value = {
        "effective_start": effective_start.isoformat() if effective_start else None,
        "effective_end": effective_end.isoformat() if effective_end else None,
        "slots": _ordered_slots(slots),
    }
    if previous_value == recorded_value:
        return template
    template.effective_start = effective_start
    template.effective_end = effective_end
    template.slots = slots
    template.full_clean()
    template.save(update_fields=("effective_start", "effective_end", "slots"))
    _record_change(
        physician=template.physician,
        subject=PhysicianChange.Subject.FACULTY_AVAILABILITY_TEMPLATE,
        subject_id=template.pk,
        previous_value=previous_value,
        recorded_value=_faculty_availability_value(template),
        scheduler=scheduler,
        reason=reason,
    )
    return template


@scheduling_mutation
@transaction.atomic
def start_later_faculty_availability_template(
    *, template, effective_start, slots, scheduler, reason=""
):
    """Split the final weekly-template period at one later calendar date."""
    require_scheduler(scheduler)
    templates = list(
        FacultyAvailabilityTemplate.objects.select_for_update()
        .filter(physician=template.physician)
        .order_by("effective_start", "pk")
    )
    preceding = next((candidate for candidate in templates if candidate.pk == template.pk), None)
    if preceding is None:
        raise ValidationError("This weekly template no longer exists.")
    if preceding != templates[-1]:
        raise ValidationError("Start a later template only from the latest weekly template.")
    if effective_start is None:
        raise ValidationError({"effective_start": "Choose a start date."})
    if preceding.effective_start and effective_start <= preceding.effective_start:
        raise ValidationError(
            {"effective_start": "Choose a date later than the preceding template's start."}
        )

    correct_faculty_availability_template(
        template=preceding,
        effective_start=preceding.effective_start,
        effective_end=effective_start - timedelta(days=1),
        slots=preceding.slots,
        scheduler=scheduler,
        reason=reason,
    )
    return create_faculty_availability_template(
        physician=preceding.physician,
        effective_start=effective_start,
        effective_end=None,
        slots=slots,
        scheduler=scheduler,
        reason=reason,
    )


@scheduling_mutation
@transaction.atomic
def create_monthly_physician_availability_rule(
    *,
    physician,
    weekday,
    ordinals,
    periods,
    availability_selection,
    effective_start=None,
    effective_end=None,
    annual_start_month=None,
    annual_end_month=None,
    scheduler,
    reason="",
):
    require_scheduler(scheduler)
    context_date = effective_start or organization_today()
    _require_schedule_input_role(
        physician, effective_start, effective_end, "Monthly scheduling rules"
    )
    ordinals = normalize_ordinals(ordinals)
    periods = _normalize_monthly_periods(periods)
    annual_start_month, annual_end_month = _normalize_annual_window(
        annual_start_month, annual_end_month
    )
    availability_selection = _normalized_monthly_availability_selection(
        physician,
        availability_selection,
        effective_start=effective_start,
        effective_end=effective_end,
    )
    _reject_unassigned_current_or_future_selections(
        physician, (availability_selection,), effective_end, context_date
    )
    existing = MonthlyPhysicianAvailabilityRule.objects.filter(
        physician=physician,
        weekday=weekday,
        ordinals=ordinals,
        periods=periods,
        availability_selection=availability_selection,
        effective_start=effective_start,
        effective_end=effective_end,
        annual_start_month=annual_start_month,
        annual_end_month=annual_end_month,
        is_active=True,
    ).first()
    if existing:
        return existing
    rule = MonthlyPhysicianAvailabilityRule(
        physician=physician,
        weekday=weekday,
        ordinals=ordinals,
        periods=periods,
        availability_selection=availability_selection,
        effective_start=effective_start,
        effective_end=effective_end,
        annual_start_month=annual_start_month,
        annual_end_month=annual_end_month,
    )
    rule.full_clean()
    rule.save()
    _record_change(
        physician=physician,
        subject=PhysicianChange.Subject.MONTHLY_AVAILABILITY_RULE,
        subject_id=rule.pk,
        previous_value=None,
        recorded_value=_monthly_availability_rule_value(rule),
        scheduler=scheduler,
        reason=reason,
    )
    return rule


@scheduling_mutation
@transaction.atomic
def correct_monthly_physician_availability_rule(
    *,
    rule,
    weekday,
    ordinals,
    periods,
    availability_selection,
    effective_start=None,
    effective_end=None,
    annual_start_month=None,
    annual_end_month=None,
    scheduler,
    reason="",
):
    require_scheduler(scheduler)
    rule = (
        MonthlyPhysicianAvailabilityRule.objects.select_for_update()
        .select_related("physician")
        .get(pk=rule.pk)
    )
    context_date = effective_start or organization_today()
    _require_schedule_input_role(
        rule.physician, effective_start, effective_end, "Monthly scheduling rules"
    )
    ordinals = normalize_ordinals(ordinals)
    periods = _normalize_monthly_periods(periods)
    annual_start_month, annual_end_month = _normalize_annual_window(
        annual_start_month, annual_end_month
    )
    availability_selection = _normalized_monthly_availability_selection(
        rule.physician,
        availability_selection,
        previous=rule.availability_selection,
        effective_start=effective_start,
        effective_end=effective_end,
    )
    if rule.is_active:
        _reject_unassigned_current_or_future_selections(
            rule.physician, (availability_selection,), effective_end, context_date
        )
    previous_value = _monthly_availability_rule_value(rule)
    recorded_value = _monthly_availability_rule_value_values(
        rule.physician,
        weekday,
        ordinals,
        periods,
        availability_selection,
        effective_start,
        effective_end,
        annual_start_month,
        annual_end_month,
        rule.is_active,
    )
    if previous_value == recorded_value:
        return rule
    rule.weekday = weekday
    rule.ordinals = ordinals
    rule.periods = periods
    rule.availability_selection = availability_selection
    rule.effective_start = effective_start
    rule.effective_end = effective_end
    rule.annual_start_month = annual_start_month
    rule.annual_end_month = annual_end_month
    rule.full_clean()
    rule.save(
        update_fields=(
            "weekday",
            "ordinals",
            "periods",
            "availability_selection",
            "effective_start",
            "effective_end",
            "annual_start_month",
            "annual_end_month",
        )
    )
    _record_change(
        physician=rule.physician,
        subject=PhysicianChange.Subject.MONTHLY_AVAILABILITY_RULE,
        subject_id=rule.pk,
        previous_value=previous_value,
        recorded_value=_monthly_availability_rule_value(rule),
        scheduler=scheduler,
        reason=reason,
    )
    return rule


@scheduling_mutation
@transaction.atomic
def deactivate_monthly_physician_availability_rule(*, rule, scheduler, reason=""):
    require_scheduler(scheduler)
    rule = (
        MonthlyPhysicianAvailabilityRule.objects.select_for_update()
        .select_related("physician")
        .get(pk=rule.pk)
    )
    if not rule.is_active:
        return rule
    previous_value = _monthly_availability_rule_value(rule)
    rule.is_active = False
    rule.full_clean()
    rule.save(update_fields=("is_active",))
    _record_change(
        physician=rule.physician,
        subject=PhysicianChange.Subject.MONTHLY_AVAILABILITY_RULE,
        subject_id=rule.pk,
        previous_value=previous_value,
        recorded_value=_monthly_availability_rule_value(rule),
        scheduler=scheduler,
        reason=reason,
    )
    return rule


def faculty_availability_on(physician, on_date, period_of_day, *, sources=None):
    """Resolve a base template or matching monthly rule and faculty-role eligibility."""
    if period_of_day not in PreceptorSession.PeriodOfDay.values:
        raise ValidationError({"period_of_day": "Period must be AM or PM."})
    if sources is None:
        role_is_effective = (
            EffectiveRole.objects.filter(
                physician=physician,
                role=EffectiveRole.Role.FACULTY,
                effective_start__lte=on_date,
            )
            .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=on_date))
            .exists()
        )
    else:
        role_is_effective = any(
            role.effective_start <= on_date
            and (role.effective_end is None or role.effective_end >= on_date)
            for role in sources.roles
        )
    if on_date.weekday() >= 5:
        return FacultyAvailabilityStatus(
            base_availability=None,
            availability_selection=None,
            is_applicable=False,
            source=None,
            supporting_template=None,
            faculty_eligible=role_is_effective,
            faculty_reason="Effective Faculty role" if role_is_effective else None,
        )
    monthly_rules = (
        MonthlyPhysicianAvailabilityRule.objects.filter(
            physician=physician, weekday=on_date.weekday(), is_active=True
        )
        .filter(Q(effective_start__isnull=True) | Q(effective_start__lte=on_date))
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=on_date))
        if sources is None
        else (rule for rule in sources.monthly_rules if rule.weekday == on_date.weekday())
    )
    monthly_rule = next(
        (rule for rule in monthly_rules if rule.matches(on_date, period_of_day)),
        None,
    )
    if monthly_rule:
        availability_selection = monthly_rule.availability_selection
        return FacultyAvailabilityStatus(
            base_availability=_availability_selection_base_state(
                availability_selection,
                categories_by_id=sources.categories_by_id if sources is not None else None,
            ),
            availability_selection=availability_selection,
            is_applicable=True,
            source="monthly_rule",
            supporting_template=None,
            faculty_eligible=role_is_effective,
            faculty_reason="Effective Faculty role" if role_is_effective else None,
        )
    template = (
        FacultyAvailabilityTemplate.objects.filter(physician=physician)
        .filter(Q(effective_start__isnull=True) | Q(effective_start__lte=on_date))
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=on_date))
        .first()
        if sources is None
        else next(
            (
                candidate
                for candidate in sources.templates
                if (candidate.effective_start is None or candidate.effective_start <= on_date)
                and (candidate.effective_end is None or candidate.effective_end >= on_date)
            ),
            None,
        )
    )
    if not template:
        return FacultyAvailabilityStatus(
            base_availability=FacultyAvailabilityTemplate.AvailabilityState.OFF,
            availability_selection=None,
            is_applicable=True,
            source=None,
            supporting_template=None,
            faculty_eligible=role_is_effective,
            faculty_reason="Effective Faculty role" if role_is_effective else None,
        )
    slot = (
        f"{template.SLOT_CHOICES[on_date.weekday() * 2][0].split('_')[0]}_{period_of_day.lower()}"
    )
    availability_selection = template.slots[slot]
    base_availability = _availability_selection_base_state(
        availability_selection,
        categories_by_id=sources.categories_by_id if sources is not None else None,
    )
    return FacultyAvailabilityStatus(
        base_availability=base_availability,
        availability_selection=availability_selection,
        is_applicable=True,
        source="manual",
        supporting_template=template,
        faculty_eligible=role_is_effective,
        faculty_reason="Effective Faculty role" if role_is_effective else None,
    )


def _availability_selection_base_state(selection, *, categories_by_id=None):
    category_id = FacultyAvailabilityTemplate.selection_category_id(selection)
    if category_id is None:
        return selection["base_state"]
    category = (
        categories_by_id[category_id]
        if categories_by_id is not None
        else CustomAvailabilityCategory.objects.get(pk=category_id)
    )
    return category.impact


def faculty_role_on(physician, on_date):
    """Return the effective actual-Faculty role used by Interview scheduling."""
    return (
        EffectiveRole.objects.filter(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start__lte=on_date,
        )
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=on_date))
        .order_by("pk")
        .first()
    )


def physicians_status_on(physician, on_date):
    roles = tuple(
        EffectiveRole.objects.filter(physician=physician, effective_start__lte=on_date)
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=on_date))
        .values_list("role", flat=True)
    )
    faculty = EffectiveRole.Role.FACULTY in roles
    community = EffectiveRole.Role.COMMUNITY_PRECEPTOR in roles
    qualified = (
        EffectiveRole.objects.filter(
            physician=physician,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            faculty_equivalent=True,
            effective_start__lte=on_date,
        )
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=on_date))
        .exists()
    )
    faculty_reason = (
        "Effective Faculty role"
        if faculty
        else "Effective faculty-equivalent Community Preceptor role"
        if community and qualified
        else None
    )
    preceptor_reason = (
        "Effective Faculty role"
        if faculty
        else "Effective community-preceptor role"
        if community
        else None
    )
    return PhysicianSchedulingStatus(
        effective_roles=roles,
        ordinarily_eligible_to_precept=faculty or community,
        preceptor_reason=preceptor_reason,
        faculty_equivalent=faculty or (community and qualified),
        faculty_equivalent_reason=faculty_reason,
    )


def _record_change(**kwargs):
    kwargs["reason"] = normalize_note(kwargs["reason"])
    kwargs["changed_by"] = kwargs.pop("scheduler")
    return PhysicianChange.objects.create(**kwargs)


def _physician_value(physician):
    return {"display_name": physician.display_name}


def _faculty_precepting_capacity_override_value(physician):
    return {
        "faculty_precepting_capacity_override": (
            str(physician.faculty_precepting_capacity_override)
            if physician.faculty_precepting_capacity_override is not None
            else None
        )
    }


def _validate_linkable_user(user):
    email = user.email.strip()
    if not email:
        raise ValidationError(
            {"user": "An account needs a configured email before it can be linked."}
        )
    matching_users = get_user_model().objects.filter(email__iexact=email)
    if matching_users.count() != 1:
        raise ValidationError(
            {"user": "This account's email is ambiguous and cannot be linked until it is unique."}
        )


def _account_identity_value(user):
    if user is None:
        return None
    return {
        "user_id": user.pk,
        "email": user.email,
        "username": user.get_username(),
        "is_active": user.is_active,
    }


def _normalize_initial_role_specs(*, roles, role, effective_start, effective_end):
    if roles is None:
        if not role:
            return []
        roles = [
            {
                "role": role,
                "effective_start": effective_start,
                "effective_end": effective_end,
                "faculty_equivalent": False,
                "faculty_leadership": "",
            }
        ]
    elif role:
        raise ValidationError("Supply either roles or one initial role, not both.")
    elif isinstance(roles, dict) or isinstance(roles, EffectiveRole):
        roles = [roles]
    elif isinstance(roles, str):
        roles = [
            {"role": roles, "effective_start": effective_start, "effective_end": effective_end}
        ]

    normalized = []
    for spec in roles:
        if isinstance(spec, EffectiveRole):
            values = {
                "role": spec.role,
                "effective_start": spec.effective_start,
                "effective_end": spec.effective_end,
                "faculty_equivalent": spec.faculty_equivalent,
                "faculty_leadership": spec.faculty_leadership,
            }
        elif isinstance(spec, dict):
            values = {
                "role": spec.get("role"),
                "effective_start": spec.get("effective_start", effective_start),
                "effective_end": spec.get("effective_end", effective_end),
                "faculty_equivalent": spec.get("faculty_equivalent", False),
                "faculty_leadership": spec.get("faculty_leadership", ""),
            }
        elif isinstance(spec, str):
            values = {
                "role": spec,
                "effective_start": effective_start,
                "effective_end": effective_end,
                "faculty_equivalent": False,
                "faculty_leadership": "",
            }
        elif isinstance(spec, (tuple, list)) and len(spec) == 3:
            values = {"role": spec[0], "effective_start": spec[1], "effective_end": spec[2]}
        else:
            raise ValidationError(
                "Each initial role must include a role, effective start, and optional effective end."
            )
        if not values["role"]:
            raise ValidationError({"role": "Choose an initial role."})
        if not values["effective_start"]:
            raise ValidationError(
                {"effective_start": "An initial role needs an effective start date."}
            )
        normalized.append(values)
    return normalized


def _role_value(role):
    return {
        "role": role.role,
        "effective_start": role.effective_start.isoformat(),
        "effective_end": role.effective_end.isoformat() if role.effective_end else None,
        "faculty_equivalent": role.faculty_equivalent,
        "faculty_leadership": role.faculty_leadership,
    }


def _faculty_availability_value(template):
    return {
        "effective_start": template.effective_start.isoformat()
        if template.effective_start
        else None,
        "effective_end": template.effective_end.isoformat() if template.effective_end else None,
        "slots": _ordered_slots(template.slots),
    }


def _ordered_slots(slots):
    return {key: slots[key] for key, _ in FacultyAvailabilityTemplate.SLOT_CHOICES}


def _normalized_faculty_slots(
    physician, slots, previous=None, effective_start=None, effective_end=None
):
    """Capture labels for newly selected categories without rewriting retained selections."""
    previous = previous or {}
    category_ids = {
        FacultyAvailabilityTemplate.selection_category_id(selection)
        for selection in slots.values()
        if FacultyAvailabilityTemplate.selection_category_id(selection)
    }
    categories = CustomAvailabilityCategory.objects.in_bulk(category_ids)
    normalized = {}
    new_category_ids = set()
    for slot, selection in slots.items():
        if isinstance(selection, str):
            state = selection
            if state in FacultyAvailabilityTemplate.AvailabilityState.values:
                normalized[slot] = FacultyAvailabilityTemplate.base_selection(state)
                continue
        if isinstance(selection, dict) and set(selection) == {"base_state"}:
            state = selection["base_state"]
            if state in FacultyAvailabilityTemplate.AvailabilityState.values:
                normalized[slot] = FacultyAvailabilityTemplate.base_selection(state)
                continue
        category_id = FacultyAvailabilityTemplate.selection_category_id(selection)
        if category_id is None:
            normalized[slot] = selection
            continue
        previous_selection = previous.get(slot)
        if FacultyAvailabilityTemplate.selection_category_id(previous_selection) == category_id:
            normalized[slot] = previous_selection
            continue
        new_category_ids.add(category_id)
        category = categories.get(category_id)
        if category is None:
            normalized[slot] = selection
            continue
        normalized[slot] = FacultyAvailabilityTemplate.category_selection(category)
    if new_category_ids:
        assigned_category_ids = set(
            CustomAvailabilityCategoryAssignment.objects.filter(
                physician=physician,
                category_id__in=new_category_ids,
                is_active=True,
                category__is_active=True,
            ).values_list("category_id", flat=True)
        )
        automatic_admin_ids = _automatic_admin_category_ids(
            physician, new_category_ids, effective_start, effective_end
        )
        if assigned_category_ids | automatic_admin_ids != new_category_ids:
            raise ValidationError(
                {"slots": "Choose only active schedule options assigned to this physician."}
            )
    return normalized


def _reject_unassigned_current_or_future_selections(
    physician, selections, effective_end, context_date
):
    if effective_end is not None and effective_end < organization_today():
        return
    category_ids = {
        FacultyAvailabilityTemplate.selection_category_id(selection)
        for selection in selections
        if FacultyAvailabilityTemplate.selection_category_id(selection)
    }
    if not category_ids:
        return
    assigned_ids = set(
        CustomAvailabilityCategoryAssignment.objects.filter(
            physician=physician, category_id__in=category_ids, is_active=True
        ).values_list("category_id", flat=True)
    )
    automatic_admin_ids = _automatic_admin_category_ids(
        physician, category_ids, context_date, effective_end
    )
    if assigned_ids | automatic_admin_ids != category_ids:
        raise ValidationError(
            "Current and future schedule facts may use only schedule options assigned to this physician."
        )


def _automatic_admin_category_ids(
    physician, category_ids, effective_start=None, effective_end=None
):
    if not has_automatic_admin_option_overlap(physician, effective_start, effective_end):
        return set()
    return set(
        CustomAvailabilityCategory.objects.filter(
            pk__in=category_ids,
            system_key=CustomAvailabilityCategory.SystemKey.ADMIN,
            is_active=True,
        ).values_list("pk", flat=True)
    )


def _require_schedule_input_role(physician, effective_start, effective_end, fact_label):
    if not has_schedule_input_role_overlap(physician, effective_start, effective_end):
        raise ValidationError(
            {
                "physician": (
                    f"{fact_label} require a recorded Faculty role or "
                    "Community Preceptor + Float Preceptor roles."
                )
            }
        )


def _normalize_monthly_periods(periods):
    if isinstance(periods, str):
        periods = [periods]
    try:
        values = set(periods)
    except TypeError, ValueError:
        raise ValidationError({"periods": "Select AM, PM, or both periods."}) from None
    ordered = [
        period for period in MonthlyPhysicianAvailabilityRule.PeriodOfDay.values if period in values
    ]
    if not ordered or values - set(MonthlyPhysicianAvailabilityRule.PeriodOfDay.values):
        raise ValidationError({"periods": "Select AM, PM, or both periods."})
    return ordered


def _normalize_annual_window(start_month, end_month):
    if (start_month is None) != (end_month is None):
        raise ValidationError("Choose both annual window months or leave both blank.")
    if start_month is None:
        return None, None
    if (
        isinstance(start_month, bool)
        or isinstance(end_month, bool)
        or not isinstance(start_month, int)
        or not isinstance(end_month, int)
        or start_month not in range(1, 13)
        or end_month not in range(1, 13)
    ):
        raise ValidationError("Choose valid calendar months for the annual window.")
    return start_month, end_month


def _normalized_monthly_availability_selection(
    physician, selection, previous=None, effective_start=None, effective_end=None
):
    previous_category_id = FacultyAvailabilityTemplate.selection_category_id(previous)
    category_id = FacultyAvailabilityTemplate.selection_category_id(selection)
    if category_id is None:
        if (
            isinstance(selection, str)
            and selection in FacultyAvailabilityTemplate.AvailabilityState.values
        ):
            return FacultyAvailabilityTemplate.base_selection(selection)
        if isinstance(selection, dict) and set(selection) == {"base_state"}:
            state = selection["base_state"]
            if state in FacultyAvailabilityTemplate.AvailabilityState.values:
                return FacultyAvailabilityTemplate.base_selection(state)
        return selection
    if category_id == previous_category_id:
        return previous
    category = CustomAvailabilityCategory.objects.filter(pk=category_id).first()
    if not category or (
        not CustomAvailabilityCategoryAssignment.objects.filter(
            physician=physician, category=category, is_active=True, category__is_active=True
        ).exists()
        and category.pk
        not in _automatic_admin_category_ids(
            physician, {category.pk}, effective_start, effective_end
        )
    ):
        raise ValidationError(
            {
                "availability_selection": "Choose only active schedule options assigned to this physician."
            }
        )
    return FacultyAvailabilityTemplate.category_selection(category)


def _monthly_availability_rule_value(rule):
    return _monthly_availability_rule_value_values(
        rule.physician,
        rule.weekday,
        rule.ordinals,
        rule.periods,
        rule.availability_selection,
        rule.effective_start,
        rule.effective_end,
        rule.annual_start_month,
        rule.annual_end_month,
        rule.is_active,
    )


def _monthly_availability_rule_value_values(
    physician,
    weekday,
    ordinals,
    periods,
    availability_selection,
    effective_start,
    effective_end,
    annual_start_month,
    annual_end_month,
    is_active,
):
    return {
        "physician_id": physician.pk,
        "physician_display_name": physician.display_name,
        "weekday": weekday,
        "ordinals": sorted(ordinals),
        "periods": list(periods),
        "availability_selection": availability_selection,
        "effective_start": effective_start.isoformat() if effective_start else None,
        "effective_end": effective_end.isoformat() if effective_end else None,
        "annual_start_month": annual_start_month,
        "annual_end_month": annual_end_month,
        "is_active": is_active,
    }
