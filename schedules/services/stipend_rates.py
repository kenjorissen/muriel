"""Organization-wide stipend-rate timelines and their retained corrections."""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db.models import F
from django.utils import timezone

from ..models.stipend_rates import StipendRate, StipendRateChange
from ._mutation import normalize_note, scheduling_administration_mutation

TIMELINE_ORDERING = (
    F("effective_start").asc(nulls_first=True),
    F("effective_end").asc(nulls_last=True),
    "pk",
)


def stipend_rates(kind=None):
    rates = StipendRate.objects.filter(retired_at__isnull=True)
    if kind:
        rates = rates.filter(kind=kind)
    return rates.order_by("kind", *TIMELINE_ORDERING)


def stipend_rate_changes(kind=None):
    changes = StipendRateChange.objects.select_related("rate", "changed_by")
    if kind:
        changes = changes.filter(rate__kind=kind)
    return changes


def stipend_rate_on(*, kind, on_date):
    """Return the current corrected hourly rate effective on a calendar date."""
    return (
        stipend_rates(kind)
        .filter(effective_start__isnull=True)
        .filter(effective_end__isnull=True)
        .first()
        or stipend_rates(kind)
        .filter(effective_start__lte=on_date, effective_end__gte=on_date)
        .first()
        or stipend_rates(kind)
        .filter(effective_start__isnull=True, effective_end__gte=on_date)
        .first()
        or stipend_rates(kind)
        .filter(effective_start__lte=on_date, effective_end__isnull=True)
        .first()
    )


@scheduling_administration_mutation
def set_initial_stipend_rate(*, kind, hourly_rate, scheduler, note=""):
    """Configure an empty stipend type with its one unbounded rate."""
    rates = _locked_rates(kind)
    if rates:
        raise ValidationError("This stipend timeline already has a rate.")
    rate = StipendRate(kind=kind, hourly_rate=hourly_rate)
    rate.full_clean()
    rate.save()
    _record_change(rate, StipendRateChange.Action.CREATED, None, scheduler, note)
    return rate


@scheduling_administration_mutation
def append_stipend_rate(*, kind, hourly_rate, transition_date, direction, scheduler, note=""):
    """Extend one end of a configured timeline without introducing a gap."""
    rates = _locked_rates(kind)
    if not rates:
        raise ValidationError("Set an initial stipend rate before adding another rate.")
    if direction == "later":
        existing = rates[-1]
        if existing.effective_start and transition_date <= existing.effective_start:
            raise ValidationError(
                {"transition_date": "Choose a date inside the final rate period."}
            )
        previous_value = _rate_value(existing)
        existing.effective_end = transition_date - timedelta(days=1)
        rate = StipendRate(
            kind=kind,
            hourly_rate=hourly_rate,
            effective_start=transition_date,
        )
        changed_fields = ("effective_end",)
    elif direction == "earlier":
        existing = rates[0]
        if existing.effective_end and transition_date >= existing.effective_end:
            raise ValidationError(
                {"transition_date": "Choose a date inside the first rate period."}
            )
        previous_value = _rate_value(existing)
        existing.effective_start = transition_date + timedelta(days=1)
        rate = StipendRate(
            kind=kind,
            hourly_rate=hourly_rate,
            effective_end=transition_date,
        )
        changed_fields = ("effective_start",)
    else:
        raise ValidationError("Choose whether to add the rate earlier or later.")

    _full_clean_timeline([*rates, rate], [existing, rate])
    existing.save(update_fields=changed_fields)
    rate.save()
    _record_change(existing, StipendRateChange.Action.CORRECTED, previous_value, scheduler, note)
    _record_change(rate, StipendRateChange.Action.CREATED, None, scheduler, note)
    return rate


@scheduling_administration_mutation
def correct_stipend_rate(*, rate, hourly_rate, effective_start, effective_end, scheduler, note=""):
    rates = _locked_rates(rate.kind)
    rate = next((candidate for candidate in rates if candidate.pk == rate.pk), None)
    if rate is None:
        raise ValidationError("This stipend rate no longer exists.")
    index = rates.index(rate)
    previous = rates[index - 1] if index else None
    following = rates[index + 1] if index + 1 < len(rates) else None
    if (previous is None and effective_start is not None) or (
        following is None and effective_end is not None
    ):
        raise ValidationError("The outer timeline boundaries must remain unbounded.")
    if previous and effective_start is None:
        raise ValidationError({"effective_start": "An internal transition date is required."})
    if following and effective_end is None:
        raise ValidationError({"effective_end": "An internal transition date is required."})

    changed = [rate]
    previous_values = {rate.pk: _rate_value(rate)}
    old_start = rate.effective_start
    old_end = rate.effective_end
    rate.hourly_rate = hourly_rate
    rate.effective_start = effective_start
    rate.effective_end = effective_end
    if previous and effective_start != old_start:
        previous_values[previous.pk] = _rate_value(previous)
        previous.effective_end = effective_start - timedelta(days=1)
        changed.append(previous)
    if following and effective_end != old_end:
        previous_values[following.pk] = _rate_value(following)
        following.effective_start = effective_end + timedelta(days=1)
        changed.append(following)
    if all(_rate_value(candidate) == previous_values[candidate.pk] for candidate in changed):
        return rate
    _full_clean_timeline(rates, changed)
    for candidate in changed:
        candidate.save()
        _record_change(
            candidate,
            StipendRateChange.Action.CORRECTED,
            previous_values[candidate.pk],
            scheduler,
            note,
        )
    return rate


@scheduling_administration_mutation
def remove_stipend_rate(*, rate, absorber=None, scheduler, note=""):
    """Retire one rate while its adjacent active rate absorbs its full period."""
    rates = _locked_rates(rate.kind)
    rate = next((candidate for candidate in rates if candidate.pk == rate.pk), None)
    if rate is None:
        raise ValidationError("This stipend rate no longer exists.")
    if len(rates) == 1:
        raise ValidationError("At least one stipend rate must remain in a configured timeline.")

    index = rates.index(rate)
    previous = rates[index - 1] if index else None
    following = rates[index + 1] if index + 1 < len(rates) else None
    if previous and following:
        absorbing = next(
            (
                candidate
                for candidate in (previous, following)
                if str(candidate.pk) == str(absorber)
            ),
            None,
        )
        if absorbing is None:
            raise ValidationError(
                {"absorber": "Choose one of the displayed neighboring rates to absorb this period."}
            )
    else:
        if absorber is not None:
            raise ValidationError({"absorber": "This rate has only one neighboring period."})
        absorbing = previous or following

    previous_value = _rate_value(absorbing)
    removed_value = _rate_value(rate)
    if absorbing is previous:
        absorbing.effective_end = rate.effective_end
    else:
        absorbing.effective_start = rate.effective_start
    active_rates = [candidate for candidate in rates if candidate != rate]
    _full_clean_timeline(active_rates, [absorbing])

    rate.retired_at = timezone.now()
    rate.save(update_fields=("retired_at",))
    absorbing.save(update_fields=("effective_end" if absorbing is previous else "effective_start",))
    _record_change(
        rate,
        StipendRateChange.Action.RETIRED,
        removed_value,
        scheduler,
        note,
        recorded_value=None,
    )
    _record_change(
        absorbing,
        StipendRateChange.Action.CORRECTED,
        previous_value,
        scheduler,
        note,
    )
    return rate


def _locked_rates(kind):
    return list(
        StipendRate.objects.select_for_update()
        .filter(kind=kind, retired_at__isnull=True)
        .order_by(*TIMELINE_ORDERING)
    )


def _full_clean_timeline(timeline_rates, changed_rates):
    """Validate pending splits against their complete, not-yet-persisted timeline."""
    for rate in changed_rates:
        rate._validation_timeline_rates = timeline_rates
    try:
        for rate in changed_rates:
            rate.full_clean()
    finally:
        for rate in changed_rates:
            del rate._validation_timeline_rates


def _rate_value(rate):
    return {
        "kind": rate.kind,
        "hourly_rate": str(rate.hourly_rate),
        "effective_start": rate.effective_start.isoformat() if rate.effective_start else None,
        "effective_end": rate.effective_end.isoformat() if rate.effective_end else None,
    }


def _record_change(rate, action, previous_value, scheduler, note, recorded_value=None):
    return StipendRateChange.objects.create(
        rate=rate,
        action=action,
        previous_value=previous_value,
        recorded_value=_rate_value(rate)
        if recorded_value is None and action != StipendRateChange.Action.RETIRED
        else recorded_value,
        note=normalize_note(note),
        changed_by=scheduler,
    )
