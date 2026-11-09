"""Readable retained monthly scheduling-rule presentation."""

from datetime import date

from django.utils.formats import date_format

from .models.physicians import FacultyAvailabilityTemplate, MonthlyPhysicianAvailabilityRule

_ORDINALS = ("first", "second", "third", "fourth", "fifth")


def monthly_rule_summary(rule):
    """Describe one retained rule without exposing its stored fields."""
    return monthly_rule_summary_values(
        weekday=rule.get_weekday_display(),
        ordinals=rule.ordinals,
        periods=rule.periods,
        availability_selection=rule.availability_selection,
        effective_start=rule.effective_start,
        effective_end=rule.effective_end,
        annual_start_month=rule.annual_start_month,
        annual_end_month=rule.annual_end_month,
    )


def monthly_rule_summary_values(
    *,
    weekday,
    ordinals,
    periods,
    availability_selection,
    effective_start,
    effective_end,
    annual_start_month,
    annual_end_month,
):
    """Describe a persisted or historical retained-rule value."""
    prefix = _effective_range_prefix(effective_start, effective_end)
    annual_window = _annual_window_display(annual_start_month, annual_end_month)
    recurrence = _recurrence_display(weekday, ordinals, periods)
    selection = FacultyAvailabilityTemplate.selection_label(availability_selection)
    summary = f"{prefix}{selection} applies {annual_window} {recurrence}."
    if annual_start_month is not None and annual_end_month is not None:
        summary += " Outside the annual window, the weekly template applies."
    return summary


def _effective_range_prefix(start, end):
    if start and end:
        return f"From {_date_display(start)} through {_date_display(end)}, "
    if start:
        return f"Beginning {_date_display(start)}, "
    if end:
        return f"Through {_date_display(end)}, "
    return ""


def _annual_window_display(start_month, end_month):
    if start_month is None or end_month is None:
        return "every year"
    end = (
        f"the end of {date(2000, end_month, 1):%B}"
        if end_month == 2
        else (f"{date(2000, end_month, 1):%B} {_last_day_of_month(end_month)}")
    )
    return f"every year from {date(2000, start_month, 1):%B} 1 through {end}"


def _recurrence_display(weekday, ordinals, periods):
    ordinal_clause = _ordinal_display(ordinals)
    period_clause = _period_display(periods)
    return f"on {ordinal_clause} {weekday}{period_clause}"


def _ordinal_display(ordinals):
    ordinals = tuple(sorted(ordinals))
    if ordinals == (1, 2, 3, 4, 5):
        return "every"
    labels = tuple(_ORDINALS[ordinal - 1] for ordinal in ordinals)
    return _join_words(labels)


def _period_display(periods):
    periods = tuple(
        period
        for period in MonthlyPhysicianAvailabilityRule.PeriodOfDay.values
        if period in periods
    )
    return f" {_join_words(periods)}"


def _join_words(words):
    if len(words) == 1:
        return words[0]
    if len(words) == 2:
        return " and ".join(words)
    return f"{', '.join(words[:-1])}, and {words[-1]}"


def _date_display(value):
    return date_format(value, "F j, Y")


def _last_day_of_month(month):
    return (date(2000 if month < 12 else 2001, month % 12 + 1, 1) - date.resolution).day
