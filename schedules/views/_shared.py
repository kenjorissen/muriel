"""Small cross-domain date parsing and calendar navigation helpers."""

from datetime import date, datetime

from ..academic_years import academic_year_for_date
from ..services.operational_attention import organization_today


def bounded_academic_years(current_academic_year):
    return range(current_academic_year - 5, current_academic_year + 6)


def requested_bounded_academic_year(request, current_academic_year):
    value = request.GET.get("academic_year")
    if not value:
        return current_academic_year
    try:
        academic_year = int(value)
    except TypeError, ValueError:
        return None
    return (
        academic_year
        if value == str(academic_year)
        and academic_year in bounded_academic_years(current_academic_year)
        else None
    )


def academic_year_navigation_context(academic_year, choices, url_for_year):
    choices = tuple(choices)
    position = choices.index(academic_year)
    return {
        "schedule_workspace_previous_academic_year_url": (
            url_for_year(choices[position - 1]) if position else None
        ),
        "schedule_workspace_next_academic_year_url": (
            url_for_year(choices[position + 1]) if position + 1 < len(choices) else None
        ),
    }


def academic_year_month_navigation_context(choices, month, url_for_month):
    """Build bounded month-arrow URLs for an academic-year calendar."""
    previous_month = _shift_month(month, -1)
    next_month = _shift_month(month, 1)
    return {
        "schedule_workspace_previous_month_url": (
            url_for_month(previous_month)
            if academic_year_for_date(previous_month) in choices
            else None
        ),
        "schedule_workspace_next_month_url": (
            url_for_month(next_month) if academic_year_for_date(next_month) in choices else None
        ),
    }


def academic_year_month(academic_year, month):
    return month.replace(year=academic_year if month.month >= 7 else academic_year + 1)


def _requested_month(value):
    if not value:
        return organization_today().replace(day=1)
    try:
        return datetime.strptime(value, "%Y-%m").date()
    except ValueError:
        return None


def _requested_date(value):
    if not value:
        return organization_today()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _shift_month(month, offset):
    month_number = month.month + offset
    year = month.year
    if month_number == 0:
        return date(year - 1, 12, 1)
    if month_number == 13:
        return date(year + 1, 1, 1)
    return date(year, month_number, 1)
