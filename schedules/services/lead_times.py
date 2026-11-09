"""Audited organization-wide lead-time policy."""

from django.core.exceptions import ValidationError

from ..models.lead_times import LeadTimeChange, LeadTimeSetting
from ._mutation import normalize_note, scheduling_administration_mutation

LEAD_TIME_DEFAULTS = {
    LeadTimeSetting.Family.HOLIDAYS: 18,
    LeadTimeSetting.Family.ACADEMIC_EVENTS: 6,
    LeadTimeSetting.Family.PRECEPTING: 5,
    LeadTimeSetting.Family.NORMAL_ROUNDING: 5,
    LeadTimeSetting.Family.INTERVIEWS: 0,
}


def ensure_lead_time_defaults():
    """Create any absent policy defaults for database initialization and reset flows."""
    for family, months in LEAD_TIME_DEFAULTS.items():
        LeadTimeSetting.objects.get_or_create(family=family, defaults={"months": months})


def lead_time_policy():
    """Return the complete current policy without creating or correcting any rows."""
    settings = {
        setting.family: setting.months
        for setting in LeadTimeSetting.objects.filter(family__in=LEAD_TIME_DEFAULTS)
    }
    missing_families = [
        LeadTimeSetting.Family(family).label
        for family in LEAD_TIME_DEFAULTS
        if family not in settings
    ]
    if missing_families:
        raise ValidationError(
            "Lead-time policy is incomplete; missing " + ", ".join(missing_families) + "."
        )
    return {family: settings[family] for family in LEAD_TIME_DEFAULTS}


def lead_time_months(family):
    """Return one validated policy value from the complete policy."""
    _validate_family(family)
    return lead_time_policy()[family]


def lead_time_changes(family=None):
    """Return retained lead-time history, optionally for one family."""
    if family is not None:
        _validate_family(family)
    changes = LeadTimeChange.objects.select_related("setting", "changed_by")
    return changes.filter(family=family) if family is not None else changes


@scheduling_administration_mutation
def set_lead_time(*, family, months, scheduler, note=""):
    """Change one family atomically; setting an unchanged value is a no-op."""
    _validate_family(family)
    _validate_months(months)
    try:
        setting = LeadTimeSetting.objects.select_for_update().get(family=family)
    except LeadTimeSetting.DoesNotExist as error:
        raise ValidationError(
            f"Lead-time policy is incomplete; missing {LeadTimeSetting.Family(family).label}."
        ) from error
    if setting.months == months:
        return setting

    previous_months = setting.months
    setting.months = months
    setting._lead_time_service_mutation = True
    try:
        setting.full_clean()
        setting.save(update_fields=("months",))
    finally:
        del setting._lead_time_service_mutation
    LeadTimeChange.objects.create(
        setting=setting,
        family=family,
        previous_months=previous_months,
        resulting_months=months,
        note=normalize_note(note),
        changed_by=scheduler,
    )
    return setting


def _validate_family(family):
    if family not in LEAD_TIME_DEFAULTS:
        raise ValidationError({"family": "Choose a valid lead-time family."})


def _validate_months(months):
    if isinstance(months, bool) or not isinstance(months, int) or months < 0:
        raise ValidationError(
            {"months": "Lead time must be a non-negative whole number of months."}
        )


__all__ = (
    "LEAD_TIME_DEFAULTS",
    "ensure_lead_time_defaults",
    "lead_time_changes",
    "lead_time_months",
    "lead_time_policy",
    "set_lead_time",
)
