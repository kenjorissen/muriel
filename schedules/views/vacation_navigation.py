"""Shared Vacation workspace navigation."""

from django.urls import reverse


def vacation_workspace_navigation(current, *, month=None, calendar_year=None, can_import=False):
    calendar_url = reverse("vacation-workspace")
    list_url = reverse("vacation-list")
    holiday_url = reverse("holiday-workspace")
    if month is not None:
        calendar_url += f"?month={month:%Y-%m}"
        list_url += f"?month={month:%Y-%m}"
    if calendar_year is not None:
        holiday_url += f"?calendar_year={calendar_year}"
    navigation = [
        {"label": "Calendar", "url": calendar_url, "current": current == "calendar"},
        {"label": "List", "url": list_url, "current": current == "list"},
        {"label": "Holidays", "url": holiday_url, "current": current == "holidays"},
    ]
    if can_import:
        navigation.append(
            {
                "label": "Import",
                "url": reverse("vacation-calendar-import"),
                "current": current == "import",
            }
        )
    return tuple(navigation)
