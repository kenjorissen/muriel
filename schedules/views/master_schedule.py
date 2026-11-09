"""Read-only Master Schedule weekly workspace."""

from datetime import date, timedelta
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from ..master_schedule_presentation import (
    MasterScheduleFactPresentation,
    MasterScheduleFindingPresentation,
    MasterScheduleGridCellPresentation,
    MasterScheduleGridDayPresentation,
    MasterScheduleGridGroupPresentation,
    MasterScheduleGridPresentation,
    MasterScheduleGridRowPresentation,
)
from ..pdf import build_master_schedule_pdf, master_schedule_filename
from ..physician_scope import physician_scope_ids
from ..services.master_schedule import MasterScheduleFact, master_schedule_week
from ._shared import _requested_date
from .work_remaining import schedule_navigation


@login_required
def master_schedule_workspace(request):
    selected_date = _requested_date(request.GET.get("date"))
    if selected_date is None:
        return HttpResponseBadRequest("Date must use YYYY-MM-DD.")
    focus_ids = physician_scope_ids(request)
    projection = master_schedule_week(selected_date, physician_focus_ids=focus_ids)
    week_start = projection.week_start
    return render(
        request,
        "schedules/master_schedule_workspace.html",
        {
            "schedule_workspace_page_title": "Master Schedule",
            "schedule_workspace_label": "Schedule",
            "schedule_workspace_navigation": schedule_navigation("master-schedule"),
            "schedule_workspace_selected_date": selected_date,
            "schedule_workspace_previous_period_url": _week_url(week_start - timedelta(days=7)),
            "schedule_workspace_next_period_url": _week_url(week_start + timedelta(days=7)),
            "schedule_workspace_period_picker_action": reverse("master-schedule"),
            "schedule_workspace_period_picker_preserved_query": {},
            "projection": projection,
            "clinic_grid": _clinic_grid(projection),
            "supporting_grid": _supporting_grid(projection),
            "master_schedule_pdf_url": _pdf_url(selected_date, focus_ids),
        },
    )


@login_required
def master_schedule_pdf(request):
    selected_date = _requested_date(request.GET.get("date"))
    if selected_date is None:
        return HttpResponseBadRequest("Date must use YYYY-MM-DD.")
    focus_ids = _requested_focus_ids(request.GET.getlist("physicians"))
    projection = master_schedule_week(selected_date, physician_focus_ids=focus_ids)
    response = HttpResponse(
        build_master_schedule_pdf(
            projection,
            clinic_grid=_clinic_grid(projection),
            supporting_grid=_supporting_grid(projection),
            captured_at=timezone.localtime(timezone.now()),
        ),
        content_type="application/pdf",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="{master_schedule_filename(projection)}"'
    )
    return response


def _requested_focus_ids(values):
    focus_ids = set()
    for value in values:
        try:
            identifier = int(value)
        except ValueError:
            continue
        if str(identifier) == value and identifier > 0:
            focus_ids.add(identifier)
    return focus_ids


def _clinic_grid(projection):
    days = _grid_days(
        (day.on_date for day in projection.clinic_groups[0].positions[0].days), projection
    )
    groups = [
        MasterScheduleGridGroupPresentation(
            group.key,
            group.label,
            tuple(
                MasterScheduleGridRowPresentation(
                    position.key,
                    _position_label(position.label),
                    tuple(
                        _coverage_cell(
                            day.coverage[0],
                            projection,
                            group.key,
                            position.key,
                            day.on_date,
                        )
                        for day in position.days
                    ),
                )
                for position in group.positions
            ),
            _closed_day_indexes(days, "AM" if group.key == "clinic-morning" else "PM"),
        )
        for group in projection.clinic_groups
    ]
    groups.append(
        MasterScheduleGridGroupPresentation(
            "faculty-availability",
            "Faculty availability",
            (
                MasterScheduleGridRowPresentation(
                    "available-faculty",
                    "Available faculty",
                    tuple(
                        _availability_cell(date.fromisoformat(day.iso_date), projection)
                        for day in days
                    ),
                ),
            ),
        )
    )
    return MasterScheduleGridPresentation(
        "Monday through Friday clinic schedule", days, tuple(groups)
    )


def _supporting_grid(projection):
    days = _grid_days(
        (projection.week_start + timedelta(days=offset) for offset in range(-1, 6)), projection
    )
    return MasterScheduleGridPresentation(
        "Sunday through Saturday supporting schedules",
        days,
        tuple(
            MasterScheduleGridGroupPresentation(
                group.key,
                group.label,
                tuple(
                    MasterScheduleGridRowPresentation(
                        row_key,
                        row_label,
                        tuple(
                            _supporting_cell(
                                facts,
                                projection,
                                group.key,
                                row_key,
                                day.iso_date,
                            )
                            for day in days
                        ),
                    )
                    for row_key, row_label, facts in _supporting_rows(group)
                ),
            )
            for group in projection.supporting_groups
        ),
        tuple(index for index, day in enumerate(days) if day.holidays),
    )


def _grid_days(dates, projection):
    holidays_by_date = {}
    for fact in projection.supporting_facts:
        if fact.kind == "holiday_closure":
            holidays_by_date.setdefault(fact.start_date, []).append(fact.record)
    return tuple(
        MasterScheduleGridDayPresentation(
            on_date.isoformat(),
            f"{on_date:%a} {on_date.month}/{on_date.day}",
            tuple(holidays_by_date.get(on_date, ())),
        )
        for on_date in dates
    )


def _closed_day_indexes(days, period):
    return tuple(
        index
        for index, day in enumerate(days)
        if any(holiday.closes(period) for holiday in day.holidays)
    )


def _position_label(label):
    if label.startswith("Precepting primary "):
        return f"Preceptor {label.removeprefix('Precepting primary ')}"
    if label.startswith("Precepting "):
        return label.removeprefix("Precepting ").capitalize()
    return label


def _coverage_cell(coverage, projection, group_key, row_key, on_date):
    return MasterScheduleGridCellPresentation(
        tuple(_fact_presentation(fact, detail="") for fact in coverage.displayed_facts),
        _findings_for(projection, group_key, row_key, on_date, coverage.period_of_day),
    )


def _availability_cell(on_date, projection):
    labels = (
        _availability_label(row, on_date, projection)
        for row in projection.faculty_availability
        if row.physician.is_within_focus
    )
    return MasterScheduleGridCellPresentation(
        tuple(MasterScheduleFactPresentation(label, "") for label in labels if label)
    )


def _availability_label(row, on_date, projection):
    periods = tuple(
        period.period_of_day
        for day in row.days
        if day.on_date == on_date
        for period in day.periods
        if period.availability == "available"
        and not _has_displayed_assignment(
            projection, row.physician.physician, on_date, period.period_of_day
        )
    )
    if not periods:
        return ""
    label = row.physician.physician.display_name
    return label if len(periods) == 2 else f"{label} ({periods[0]})"


def _has_displayed_assignment(projection, physician, on_date, period_of_day):
    return any(
        fact.physician == physician
        for group in projection.clinic_groups
        for position in group.positions
        for day in position.days
        if day.on_date == on_date
        for coverage in day.coverage
        if coverage.period_of_day == period_of_day
        for fact in coverage.displayed_facts
    )


def _supporting_rows(group):
    if group.key == "rounding":
        return (
            (
                "normal-rounding",
                "Normal Rounding",
                tuple(fact for fact in group.facts if fact.kind == "normal_rounder"),
            ),
            (
                "casual-rounding",
                "Casual Rounding",
                tuple(fact for fact in group.facts if fact.kind == "casual_rounder_rule"),
            ),
        )
    return ((group.key, group.label, group.facts),)


def _supporting_cell(facts, projection, group_key, row_key, iso_date):
    on_date = date.fromisoformat(iso_date)
    matching = tuple(
        fact
        for fact in facts
        if fact.start_date is not None
        and fact.start_date <= on_date <= fact.end_date
        and _fact_is_within_focus(fact, projection)
        and not (
            fact.kind == "normal_rounder"
            and fact.start_date == projection.week_start
            and fact.end_date == projection.week_end
            and on_date != projection.week_start
        )
    )
    return MasterScheduleGridCellPresentation(
        _supporting_fact_presentations(matching, group_key),
        _findings_for(projection, group_key, row_key, on_date, None),
    )


def _supporting_fact_presentations(facts, group_key):
    if group_key in {"rounding", "community-precepting"}:
        return _deduplicated_physician_presentations(facts)
    if group_key == "academic-events":
        return _academic_event_presentations(facts)
    return tuple(_supporting_fact_presentation(fact) for fact in facts)


def _deduplicated_physician_presentations(facts):
    by_physician = {}
    for fact in facts:
        key = ("physician", fact.physician.pk) if fact.physician else ("label", fact.label)
        by_physician.setdefault(key, []).append(fact)
    return tuple(
        MasterScheduleFactPresentation(
            facts[0].physician.display_name if facts[0].physician else facts[0].label,
            "",
            _attention(facts),
        )
        for facts in by_physician.values()
    )


def _academic_event_presentations(facts):
    by_event = {}
    for fact in facts:
        by_event.setdefault((fact.source_id, fact.label), []).append(fact)
    return tuple(
        MasterScheduleFactPresentation(
            label,
            "; ".join(
                part
                for part in (
                    ", ".join(
                        physician.display_name
                        for physician in dict.fromkeys(
                            fact.physician for fact in event_facts if fact.physician
                        )
                    ),
                    event_facts[0].explanation
                    if event_facts[0].record.precepting_impact == "none"
                    else "",
                )
                if part
            ),
            _attention(event_facts),
        )
        for (_, label), event_facts in by_event.items()
    )


def _attention(facts):
    return next((fact.attention for fact in facts if fact.attention), "")


def _supporting_fact_presentation(fact):
    if fact.kind == "normal_rounder" and (fact.end_date - fact.start_date).days == 6:
        coverage = "Mon-Sun"
    else:
        coverage = " / ".join(fact.periods)
    label = fact.physician.display_name if fact.physician else fact.label
    if fact.kind == "vacation" and fact.periods == ("AM", "PM"):
        coverage = ""
    elif fact.kind == "holiday_closure" and fact.periods == ("AM", "PM"):
        coverage = ""
    detail_parts = [
        part
        for part in (
            coverage,
            fact.label if fact.physician and fact.kind not in {"vacation"} else "",
            fact.explanation if fact.kind not in {"vacation", "holiday_closure"} else "",
        )
        if part
    ]
    return MasterScheduleFactPresentation(
        label,
        "; ".join(dict.fromkeys(detail_parts)),
        fact.attention,
    )


def _fact_is_within_focus(fact, projection):
    return fact.physician is None or any(
        physician.physician == fact.physician and physician.is_within_focus
        for physician in projection.physicians
    )


def _findings_for(projection, group_key, row_key, on_date, period_of_day):
    return tuple(
        MasterScheduleFindingPresentation(
            finding.message,
            finding.severity,
            tuple(_fact_presentation(source) for source in finding.sources),
        )
        for finding in projection.findings
        if any(
            target.group_key == group_key
            and target.row_key == row_key
            and target.on_date == on_date
            and target.period_of_day == period_of_day
            for target in finding.targets
        )
    )


def _week_url(on_date):
    return f"{reverse('master-schedule')}?date={on_date:%Y-%m-%d}"


def _pdf_url(on_date, focus_ids):
    query = [
        ("date", on_date.isoformat()),
        *(("physicians", identifier) for identifier in sorted(focus_ids)),
    ]
    return f"{reverse('master-schedule-pdf')}?{urlencode(query)}"


def _fact_presentation(fact: MasterScheduleFact, *, detail=None):
    return MasterScheduleFactPresentation(
        fact.physician.display_name if fact.physician else fact.label,
        fact.explanation if detail is None else detail,
        fact.attention,
    )
