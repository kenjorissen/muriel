"""Organization-wide actionable work remaining workspace."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import reverse

from ..services.work_remaining import global_work_remaining
from ..table_presentation import (
    DataTable,
    DataTableCell,
    DataTableColumn,
    DataTableRow,
)


@login_required
def work_remaining_workspace(request):
    work_remaining = global_work_remaining()
    return render(
        request,
        "schedules/work_remaining_workspace.html",
        {
            "schedule_workspace_page_title": "Work Remaining",
            "schedule_workspace_label": "Schedule",
            "schedule_workspace_navigation": _schedule_navigation("work-remaining"),
            "work_remaining": work_remaining,
            "work_remaining_table": _work_remaining_table(work_remaining.items),
        },
    )


def schedule_navigation(current):
    """Return the local views owned by the global Schedule destination."""
    return _schedule_navigation(current)


def _schedule_navigation(current):
    return (
        {
            "label": "Master Schedule",
            "url": reverse("master-schedule"),
            "current": current == "master-schedule",
        },
        {
            "label": "Work Remaining",
            "url": reverse("work-remaining"),
            "current": current == "work-remaining",
        },
        {
            "label": "Lead Times",
            "url": reverse("lead-time-workspace"),
            "current": current == "lead-times",
        },
    )


def _work_remaining_table(items):
    return DataTable(
        identity="work-remaining",
        label="Organization-wide work remaining",
        width="bounded",
        columns=tuple(
            DataTableColumn(label)
            for label in ("Attention starting", "Area", "Affected period", "Work remaining")
        ),
        rows=tuple(
            DataTableRow(
                (
                    DataTableCell(
                        _date_label(item.matters_on),
                        kind="date",
                        machine_value=item.matters_on.isoformat(),
                    ),
                    DataTableCell(_area_label(item.area), kind="link", url=item.target_url),
                    DataTableCell(_range_label(item.affected_range)),
                    DataTableCell(
                        "; ".join(reason.label for reason in item.reasons),
                        kind="status",
                        tone="danger" if item.required else "attention",
                        row_header=True,
                    ),
                ),
                tone="danger" if item.required else "attention",
            )
            for item in items
        ),
        empty_state="No work remains.",
    )


def _area_label(area):
    return {
        "academic-events": "Academic Events",
        "holidays": "Holidays",
        "interviews": "Interviews",
        "precepting": "Precepting",
        "rounding": "Normal Rounding",
    }[area]


def _date_label(value):
    return f"{value:%b} {value.day}, {value:%Y}"


def _range_label(date_range):
    if date_range.start == date_range.end:
        return _date_label(date_range.start)
    if date_range.start.year == date_range.end.year:
        if date_range.start.month == date_range.end.month:
            return f"{date_range.start:%b} {date_range.start.day}-{date_range.end.day}, {date_range.start:%Y}"
        return (
            f"{date_range.start:%b} {date_range.start.day}-"
            f"{date_range.end:%b} {date_range.end.day}, {date_range.start:%Y}"
        )
    return f"{_date_label(date_range.start)}-{_date_label(date_range.end)}"
