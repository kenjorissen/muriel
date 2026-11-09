"""Custom Faculty availability category catalog mutations."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from ..models.availability_categories import (
    CustomAvailabilityCategory,
    CustomAvailabilityCategoryAssignment,
    CustomAvailabilityCategoryChange,
)
from ..models.physicians import (
    EffectiveRole,
    FacultyAvailabilityTemplate,
    MonthlyPhysicianAvailabilityRule,
)
from ..monthly_rule_presentation import monthly_rule_summary
from ._mutation import normalize_note, require_scheduler, scheduling_mutation
from .operational_attention import organization_today


def ensure_system_schedule_options():
    category, _ = CustomAvailabilityCategory.objects.get_or_create(
        system_key=CustomAvailabilityCategory.SystemKey.ADMIN,
        defaults={
            "name": "Admin",
            "impact": CustomAvailabilityCategory.Impact.NOT_AVAILABLE,
            "is_active": True,
        },
    )
    return category


@scheduling_mutation
@transaction.atomic
def create_custom_availability_category(*, name, impact, scheduler, reason=""):
    require_scheduler(scheduler)
    category = CustomAvailabilityCategory(name=name.strip(), impact=impact)
    category.full_clean()
    category.save()
    _record_change(
        category=category,
        action=CustomAvailabilityCategoryChange.Action.CREATED,
        previous_value=None,
        recorded_value=_category_value(category),
        scheduler=scheduler,
        reason=reason,
    )
    return category


@scheduling_mutation
@transaction.atomic
def correct_custom_availability_category(*, category, name, impact, scheduler, reason=""):
    require_scheduler(scheduler)
    category = CustomAvailabilityCategory.objects.select_for_update().get(pk=category.pk)
    if category.system_key:
        raise ValidationError("Built-in schedule options cannot be corrected.")
    previous_value = _category_value(category)
    category.name = name.strip()
    category.impact = impact
    category.full_clean()
    recorded_value = _category_value(category)
    if previous_value == recorded_value:
        return category
    category.save(update_fields=("name", "impact"))
    _record_change(
        category=category,
        action=CustomAvailabilityCategoryChange.Action.CORRECTED,
        previous_value=previous_value,
        recorded_value=recorded_value,
        scheduler=scheduler,
        reason=reason,
    )
    return category


@scheduling_mutation
@transaction.atomic
def deactivate_custom_availability_category(*, category, scheduler, reason=""):
    require_scheduler(scheduler)
    category = CustomAvailabilityCategory.objects.select_for_update().get(pk=category.pk)
    if category.system_key:
        raise ValidationError("Built-in schedule options cannot be deactivated.")
    if not category.is_active:
        return category
    previous_value = _category_value(category)
    category.is_active = False
    category.save(update_fields=("is_active",))
    _record_change(
        category=category,
        action=CustomAvailabilityCategoryChange.Action.DEACTIVATED,
        previous_value=previous_value,
        recorded_value=_category_value(category),
        scheduler=scheduler,
        reason=reason,
    )
    return category


@scheduling_mutation
@transaction.atomic
def set_custom_availability_category_physicians(*, category, physicians, scheduler, reason=""):
    """Retain every applicability change while applying one compact selection."""
    require_scheduler(scheduler)
    category = CustomAvailabilityCategory.objects.select_for_update().get(pk=category.pk)
    if not category.is_active:
        raise ValidationError("Inactive categories cannot be assigned to physicians.")
    physicians = tuple(physicians)
    physician_ids = {physician.pk for physician in physicians}
    eligible_ids = set(
        EffectiveRole.objects.filter(
            physician_id__in=physician_ids, role=EffectiveRole.Role.FACULTY
        ).values_list("physician_id", flat=True)
    )
    if eligible_ids != physician_ids:
        raise ValidationError("Categories can be assigned only to Faculty physicians.")

    assignments = {
        assignment.physician_id: assignment
        for assignment in CustomAvailabilityCategoryAssignment.objects.select_for_update().filter(
            category=category
        )
    }
    active_ids = {
        physician_id for physician_id, assignment in assignments.items() if assignment.is_active
    }
    removed_ids = active_ids - physician_ids
    _reject_current_or_future_category_references(category, removed_ids)
    physicians_by_id = {physician.pk: physician for physician in physicians}
    for physician_id in sorted(physician_ids - active_ids):
        assignment = assignments.get(physician_id)
        previous_value = _assignment_value(assignment) if assignment else None
        if assignment is None:
            assignment = CustomAvailabilityCategoryAssignment(
                category=category, physician=physicians_by_id[physician_id], is_active=True
            )
            assignment.full_clean()
            assignment.save()
        else:
            assignment.is_active = True
            assignment.full_clean()
            assignment.save(update_fields=("is_active",))
        _record_change(
            category=category,
            physician=assignment.physician,
            action=CustomAvailabilityCategoryChange.Action.ASSIGNED,
            previous_value=previous_value,
            recorded_value=_assignment_value(assignment),
            scheduler=scheduler,
            reason=reason,
        )
    for physician_id in sorted(removed_ids):
        assignment = assignments[physician_id]
        previous_value = _assignment_value(assignment)
        assignment.is_active = False
        assignment.save(update_fields=("is_active",))
        _record_change(
            category=category,
            physician=assignment.physician,
            action=CustomAvailabilityCategoryChange.Action.UNASSIGNED,
            previous_value=previous_value,
            recorded_value=_assignment_value(assignment),
            scheduler=scheduler,
            reason=reason,
        )


def _reject_current_or_future_category_references(category, physician_ids):
    if not physician_ids:
        return
    today = organization_today()
    templates = (
        FacultyAvailabilityTemplate.objects.filter(physician_id__in=physician_ids)
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=today))
        .select_related("physician")
    )
    rules = (
        MonthlyPhysicianAvailabilityRule.objects.filter(
            physician_id__in=physician_ids, is_active=True
        )
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=today))
        .select_related("physician")
    )
    blockers = []
    for template in templates:
        if any(
            FacultyAvailabilityTemplate.selection_category_id(selection) == category.pk
            for selection in template.slots.values()
        ):
            blockers.append(
                f"Faculty weekly template for {template.physician.display_name} "
                f"({_effective_range(template.effective_start, template.effective_end)})"
            )
    for rule in rules:
        if (
            FacultyAvailabilityTemplate.selection_category_id(rule.availability_selection)
            == category.pk
        ):
            blockers.append(
                f"Monthly scheduling rule for {rule.physician.display_name} "
                f"({monthly_rule_summary(rule)})"
            )
    if blockers:
        raise ValidationError(
            "Cannot unassign this schedule option while it is used by " + "; ".join(blockers) + "."
        )


def _effective_range(start, end):
    return (
        f"effective {start.isoformat() if start else 'unbounded'} through "
        f"{end.isoformat() if end else 'ongoing'}"
    )


def _category_value(category):
    return {"name": category.name, "impact": category.impact, "is_active": category.is_active}


def _assignment_value(assignment):
    return {
        "physician_id": assignment.physician_id,
        "physician_name": assignment.physician.display_name,
        "is_active": assignment.is_active,
    }


def _record_change(
    *, category, action, previous_value, recorded_value, scheduler, reason, physician=None
):
    return CustomAvailabilityCategoryChange.objects.create(
        category=category,
        physician=physician,
        action=action,
        previous_value=previous_value,
        recorded_value=recorded_value,
        reason=normalize_note(reason),
        changed_by=scheduler,
    )
