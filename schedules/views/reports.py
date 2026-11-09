"""Read-only monthly stipend report views."""

from collections import Counter
from datetime import date
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from ..academic_years import academic_year_bounds, academic_year_for_date
from ..pdf import (
    build_monthly_stipend_pdf,
    build_schedule_report_pdf,
    monthly_stipend_filename,
    schedule_report_filename,
)
from ..services._mutation import can_manage_scheduling_administration
from ..services.faculty_admin_time import (
    COVERAGE_CHOICES as ADMIN_TIME_COVERAGE_CHOICES,
)
from ..services.faculty_admin_time import (
    HOURS_PER_PERIOD,
    can_view_faculty_admin_time_report,
    monthly_faculty_admin_time_report,
)
from ..services.operational_attention import organization_today
from ..services.schedule_reports import (
    COVERAGE_CHOICES,
    EARLIEST_REPORT_YEAR,
    LATEST_ACADEMIC_YEAR,
    LATEST_CALENDAR_YEAR,
    PERIOD_CHOICES,
    SCHEDULE_CHOICES,
    academic_event_schedule_report,
    casual_rounding_schedule_report,
    community_precepting_schedule_report,
    interview_schedule_report,
    normal_rounding_schedule_report,
    precepting_schedule_report,
    schedule_report_parameters,
    vacation_schedule_report,
)
from ..services.stipend_reports import (
    community_preceptor_stipend_report as community_preceptor_report_for_month,
)
from ..services.stipend_reports import (
    late_doc_stipend_report as late_doc_report_for_month,
)
from ..table_presentation import DataTable, DataTableCell, DataTableColumn, DataTableRow
from ._shared import _requested_month, _shift_month


@login_required
def schedule_reports(request):
    selected_schedule = request.GET.get("schedule")
    try:
        parameters = schedule_report_parameters(request.GET)
    except ValidationError as error:
        return HttpResponseBadRequest(error.messages[0])
    report = _schedule_report(parameters) if selected_schedule is not None else None
    return render(
        request,
        "schedules/schedule_report.html",
        {
            **reports_workspace_context(request, view="schedule_reports"),
            "report": report,
            "schedule_report_parameters": parameters,
            "schedule_report_table": _schedule_report_table(report) if report else None,
            "schedule_report_findings_table": _schedule_report_findings_table(report)
            if report
            else None,
            "schedule_choices": SCHEDULE_CHOICES,
            "period_choices": PERIOD_CHOICES,
            "schedule_report_coverage_choices": _schedule_report_coverage_choices(parameters),
            "schedule_report_academic_year": _schedule_report_year(parameters, "academic-year"),
            "schedule_report_calendar_year": _schedule_report_year(parameters, "calendar-year"),
            "schedule_report_academic_year_choices": _schedule_report_year_choices(
                parameters, "academic-year"
            ),
            "schedule_report_calendar_year_choices": _schedule_report_year_choices(
                parameters, "calendar-year"
            ),
            "schedule_report_academic_year_previous_url": _schedule_report_year_url(
                parameters,
                "academic-year",
                _schedule_report_year(parameters, "academic-year") - 1,
                schedule=selected_schedule,
            ),
            "schedule_report_academic_year_next_url": _schedule_report_year_url(
                parameters,
                "academic-year",
                _schedule_report_year(parameters, "academic-year") + 1,
                schedule=selected_schedule,
            ),
            "schedule_report_calendar_year_previous_url": _schedule_report_year_url(
                parameters,
                "calendar-year",
                _schedule_report_year(parameters, "calendar-year") - 1,
                schedule=selected_schedule,
            ),
            "schedule_report_calendar_year_next_url": _schedule_report_year_url(
                parameters,
                "calendar-year",
                _schedule_report_year(parameters, "calendar-year") + 1,
                schedule=selected_schedule,
            ),
            "schedule_report_url": reverse("schedule-reports"),
            "schedule_report_pdf_url": _schedule_report_pdf_url(parameters) if report else None,
        },
    )


@login_required
def schedule_report_pdf(request):
    try:
        parameters = schedule_report_parameters(request.GET)
        report = _schedule_report(parameters)
    except ValidationError as error:
        return HttpResponseBadRequest(error.messages[0])
    account = request.user.get_full_name() or request.user.get_username()
    response = HttpResponse(
        build_schedule_report_pdf(
            report,
            generated_at=timezone.localtime(timezone.now()),
            generated_by=account,
        ),
        content_type="application/pdf",
    )
    response["Content-Disposition"] = f'attachment; filename="{schedule_report_filename(report)}"'
    return response


def _schedule_report(parameters):
    return {
        "precepting": precepting_schedule_report,
        "community-precepting": community_precepting_schedule_report,
        "academic-events": academic_event_schedule_report,
        "interviews": interview_schedule_report,
        "normal-rounding": normal_rounding_schedule_report,
        "casual-rounding": casual_rounding_schedule_report,
        "vacation": vacation_schedule_report,
    }[parameters.schedule](parameters)


def _schedule_report_pdf_url(parameters):
    query = {
        "schedule": parameters.schedule,
        "period": parameters.period,
        "coverage": parameters.coverage,
    }
    if parameters.period == "academic-year":
        query["academic_year"] = parameters.start_date.year
    elif parameters.period == "calendar-year":
        query["calendar_year"] = parameters.start_date.year
    else:
        query["start_date"] = parameters.start_date.isoformat()
        query["end_date"] = parameters.end_date.isoformat()
    return f"{reverse('schedule-report-pdf')}?{urlencode(query)}"


def _schedule_report_table(report):
    schedule = report.parameters.schedule
    columns, values = {
        "academic-events": (
            ("Events", "Event days", "Affected shifts"),
            ("events", "event_days", "affected_shifts"),
        ),
        "interviews": (("Shifts",), ("shifts",)),
        "normal-rounding": (("Weeks",), ("weeks",)),
        "casual-rounding": (("Days",), ("days",)),
        "vacation": (
            ("Ranges", "Weekdays", "Weekend days", "Total days"),
            ("ranges", "weekdays", "weekend_days", "total_days"),
        ),
        "community-precepting": (("AM", "PM", "Total"), ("am", "pm", "precepting_total")),
        "precepting": (
            ("AM", "PM", "Primary", "Secondary", "Precepting total", "Late Doc"),
            ("am", "pm", "primary", "secondary", "precepting_total", "late_doc"),
        ),
    }[schedule]
    return DataTable(
        identity="schedule-report-workload",
        label="Schedule report workload",
        width="wide",
        columns=tuple(DataTableColumn(label) for label in ("Physician", *columns)),
        rows=tuple(
            DataTableRow(
                cells=(
                    DataTableCell(row.physician.display_name, row_header=True),
                    *(DataTableCell(str(getattr(row, value))) for value in values),
                )
            )
            for row in report.rows
        ),
        empty_state="No current facts fall in this coverage.",
    )


def _schedule_report_findings_table(report):
    return DataTable(
        identity="schedule-report-findings",
        label="Current findings",
        width="compact",
        columns=tuple(DataTableColumn(label) for label in ("Finding", "Severity", "Count")),
        rows=tuple(
            DataTableRow(
                cells=(
                    DataTableCell(finding.label, row_header=True),
                    DataTableCell(
                        finding.severity.capitalize(),
                        kind="status",
                        tone="danger" if finding.severity == "hard" else "attention",
                    ),
                    DataTableCell(str(finding.count)),
                )
            )
            for finding in report.findings
        ),
        empty_state="No current findings.",
    )


def _schedule_report_coverage_choices(parameters):
    return tuple(
        {
            "value": value,
            "label": label,
            "available": (
                parameters.has_history
                if value == "history"
                else parameters.has_scheduled
                if value == "scheduled"
                else parameters.has_history and parameters.has_scheduled
            ),
        }
        for value, label in COVERAGE_CHOICES
    )


def _schedule_report_year(parameters, period):
    if parameters.period == period:
        return parameters.start_date.year
    if period == "academic-year":
        return academic_year_for_date(parameters.today)
    return parameters.today.year


def _schedule_report_year_url(parameters, period, year, *, schedule=None):
    maximum_year = LATEST_ACADEMIC_YEAR if period == "academic-year" else LATEST_CALENDAR_YEAR
    if not EARLIEST_REPORT_YEAR <= year <= maximum_year:
        return None
    start_date, end_date = (
        academic_year_bounds(year)
        if period == "academic-year"
        else (date(year, 1, 1), date(year, 12, 31))
    )
    query = {
        "period": period,
        "coverage": (
            "history-and-scheduled"
            if start_date < parameters.today <= end_date
            else parameters.coverage
        ),
        "academic_year" if period == "academic-year" else "calendar_year": year,
    }
    if schedule is not None:
        query["schedule"] = schedule
    return f"{reverse('schedule-reports')}?{urlencode(query)}"


def _schedule_report_year_choices(parameters, period):
    selected_year = _schedule_report_year(parameters, period)
    maximum_year = LATEST_ACADEMIC_YEAR if period == "academic-year" else LATEST_CALENDAR_YEAR
    return range(
        max(EARLIEST_REPORT_YEAR, selected_year - 5),
        min(maximum_year, selected_year + 5) + 1,
    )


@login_required
def community_preceptor_stipend_report(request):
    month = _requested_month(request.GET.get("month"))
    if month is None:
        return HttpResponseBadRequest("Month must use YYYY-MM format.")
    report = community_preceptor_report_for_month(month)
    return render(
        request,
        "schedules/stipend_report.html",
        {
            **reports_workspace_context(request, view="community_preceptor", month=month),
            "report": report,
            "report_title": "Community Preceptor Stipend",
            "stipend_detail_table": _stipend_detail_table(report),
            "stipend_totals_table": _stipend_totals_table(report),
        },
    )


@login_required
def late_doc_stipend_report(request):
    month = _requested_month(request.GET.get("month"))
    if month is None:
        return HttpResponseBadRequest("Month must use YYYY-MM format.")
    report = late_doc_report_for_month(month)
    return render(
        request,
        "schedules/stipend_report.html",
        {
            **reports_workspace_context(request, view="late_doc", month=month),
            "report": report,
            "report_title": "Late Doc Stipend",
            "stipend_detail_table": _stipend_detail_table(report),
            "stipend_totals_table": _stipend_totals_table(report),
        },
    )


@login_required
def faculty_admin_time_report(request):
    today = organization_today()
    if not can_view_faculty_admin_time_report(request.user, today=today):
        raise PermissionDenied
    month = _requested_month(request.GET.get("month"))
    if month is None:
        return HttpResponseBadRequest("Month must use YYYY-MM format.")
    coverage = request.GET.get("coverage", "history-and-scheduled")
    try:
        report = monthly_faculty_admin_time_report(month, coverage, today)
    except ValidationError as error:
        return HttpResponseBadRequest(error.messages[0])
    return render(
        request,
        "schedules/faculty_admin_time_report.html",
        {
            **reports_workspace_context(
                request, view="faculty_admin_time", month=month, coverage=coverage
            ),
            "report": report,
            "admin_time_coverage_choices": ADMIN_TIME_COVERAGE_CHOICES,
            "admin_time_table": _faculty_admin_time_table(report),
        },
    )


@login_required
def community_preceptor_stipend_pdf(request):
    return _monthly_stipend_pdf_response(
        request,
        report_for_month=community_preceptor_report_for_month,
        report_url_name="community-preceptor-stipend-report",
    )


@login_required
def late_doc_stipend_pdf(request):
    return _monthly_stipend_pdf_response(
        request,
        report_for_month=late_doc_report_for_month,
        report_url_name="late-doc-stipend-report",
    )


def _monthly_stipend_pdf_response(request, *, report_for_month, report_url_name):
    month = _requested_month(request.GET.get("month"))
    if month is None:
        return HttpResponseBadRequest("Month must use YYYY-MM format.")
    report = report_for_month(month)
    if report.is_incomplete:
        dates = ", ".join(f"{day.strftime('%b')} {day.day}" for day in report.missing_rate_dates)
        messages.error(
            request,
            f"PDF was not generated. Configure an hourly rate for qualifying shift dates: {dates}.",
        )
        return redirect(f"{reverse(report_url_name)}?month={month:%Y-%m}")
    account = request.user.get_full_name() or request.user.get_username()
    response = HttpResponse(
        build_monthly_stipend_pdf(
            report,
            generated_at=timezone.localtime(timezone.now()),
            generated_by=account,
        ),
        content_type="application/pdf",
    )
    response["Content-Disposition"] = f'attachment; filename="{monthly_stipend_filename(report)}"'
    return response


def _stipend_detail_table(report):
    return DataTable(
        identity="stipend-itemized-shifts",
        label="Itemized stipend shifts",
        columns=tuple(
            DataTableColumn(label)
            for label in ("Date", "Period", "Physician", "Hours", "Hourly rate", "Amount")
        ),
        rows=tuple(
            DataTableRow(
                cells=(
                    DataTableCell(
                        detail.date.strftime("%a, %b ") + str(detail.date.day),
                        kind="date",
                        machine_value=detail.date.isoformat(),
                    ),
                    DataTableCell(detail.period),
                    DataTableCell(detail.physician.display_name, row_header=True),
                    DataTableCell(str(detail.hours)),
                    _stipend_amount_cell(detail.hourly_rate, "Missing rate"),
                    _stipend_amount_cell(detail.amount, "Incomplete"),
                )
            )
            for detail in report.details
        ),
        empty_state=f"No qualifying shifts in {report.month:%B %Y}.",
    )


def _stipend_totals_table(report):
    return DataTable(
        identity="stipend-totals",
        label="Stipend totals",
        width="compact",
        columns=tuple(
            DataTableColumn(label) for label in ("Physician", "Shifts", "Hours", "Stipend amount")
        ),
        rows=tuple(
            DataTableRow(
                cells=(
                    DataTableCell(row.physician.display_name, row_header=True),
                    DataTableCell(f"{row.shift_total} shift{'s' if row.shift_total != 1 else ''}"),
                    DataTableCell(str(row.hour_total)),
                    _stipend_amount_cell(row.amount_total, "Incomplete"),
                )
            )
            for row in report.physicians
        ),
        footer_rows=(
            DataTableRow(
                cells=(
                    DataTableCell("Overall total", row_header=True),
                    DataTableCell(
                        f"{report.shift_total} shift{'s' if report.shift_total != 1 else ''}"
                    ),
                    DataTableCell(str(report.hour_total)),
                    _stipend_amount_cell(report.amount_total, "Incomplete"),
                )
            ),
        ),
        empty_state="No physician totals are available for this month.",
    )


def _stipend_amount_cell(amount, unavailable_label):
    return (
        DataTableCell(f"${amount}")
        if amount is not None
        else DataTableCell(unavailable_label, kind="status", tone="attention")
    )


def _faculty_admin_time_table(report):
    options = report.options
    return DataTable(
        identity="faculty-admin-time",
        label="Faculty administrative time",
        width="wide",
        columns=tuple(
            DataTableColumn(label)
            for label in (
                "Physician",
                *(option.label for option in options),
                "Blocked time",
                "Total worked",
                "Precepting (Available/Admin)",
                "Computed Admin Time",
                "ACGME",
                "UMN",
            )
        ),
        rows=tuple(
            DataTableRow(
                cells=(
                    DataTableCell(row.physician.display_name, row_header=True),
                    *(
                        DataTableCell(
                            _hours_with_periods(row.not_available_hours)
                            if option.key == "base:off"
                            else _hours_with_periods(row.hours_for(option.key))
                        )
                        for option in options
                    ),
                    DataTableCell(_excluded_time_summary(row)),
                    DataTableCell(_hours_with_periods(row.total_worked_hours)),
                    DataTableCell(_hours_with_periods(row.precepting_from_admin_hours)),
                    DataTableCell(_hours_with_periods(row.computed_admin_hours)),
                    DataTableCell(
                        " · ".join(
                            (
                                f"{_hours(row.acgme_goal)} target",
                                f"{_hours(row.acgme_ten_percent)} (10% Total worked)",
                                f"{_hours(row.acgme_prorated_cap)} prorated cap",
                                "Met" if row.acgme_met else "Below",
                            )
                        ),
                        kind="status",
                        tone="positive" if row.acgme_met else "danger",
                    ),
                    DataTableCell(
                        f"{_hours(row.umn_goal)} target · {'Met' if row.umn_met else 'Below'}",
                        kind="status",
                        tone="positive" if row.umn_met else "danger",
                    ),
                )
            )
            for row in report.rows
        ),
        empty_state=f"No Faculty physicians have an effective role in {report.month:%B %Y}.",
    )


def _excluded_time_summary(row):
    if not row.exclusions:
        return _hours_with_periods(row.excluded_hours)
    source_periods = Counter(
        label for exclusion in row.exclusions for label in exclusion.exclusions
    )
    sources = ", ".join(
        f"{label} ({periods} period{'s' if periods != 1 else ''})"
        for label, periods in source_periods.items()
    )
    return (
        f"{_hours_with_periods(row.excluded_hours)} removed · {sources}; source counts may overlap"
    )


def _hours_with_periods(hours):
    periods = hours / HOURS_PER_PERIOD
    return f"{_hours(hours)} ({_number(periods)} period{'s' if periods != 1 else ''})"


def _hours(hours):
    return f"{_number(hours)} hours"


def _number(value):
    return format(value.normalize(), "f")


def reports_workspace_context(request, *, view, month=None, coverage=None):
    can_manage_rates = can_manage_scheduling_administration(request.user)
    navigation = [
        {
            "label": "Schedule Reports",
            "url": reverse("schedule-reports"),
            "current": view == "schedule_reports",
        },
    ]
    if can_view_faculty_admin_time_report(request.user):
        navigation.append(
            {
                "label": "Admin Time",
                "url": _report_url("faculty_admin_time", month, coverage),
                "current": view == "faculty_admin_time",
            }
        )
    navigation.extend(
        (
            {
                "label": "Community Preceptor Stipend",
                "url": _report_url("community_preceptor", month),
                "current": view == "community_preceptor",
            },
            {
                "label": "Late Doc Stipend",
                "url": _report_url("late_doc", month),
                "current": view == "late_doc",
            },
        )
    )
    if can_manage_rates:
        navigation.append(
            {
                "label": "Stipend Rates",
                "url": reverse("stipend-rate-workspace"),
                "current": view == "rates",
            }
        )
    context = {
        "schedule_workspace_page_title": "Reports",
        "schedule_workspace_label": "Reports",
        "schedule_workspace_navigation": tuple(navigation),
        "can_manage_stipend_rates": can_manage_rates,
        "messages": messages.get_messages(request),
    }
    if month is not None:
        context.update(
            {
                "schedule_workspace_selected_month": month,
                "schedule_workspace_previous_month_url": _report_url(
                    view, _shift_month(month, -1), coverage
                ),
                "schedule_workspace_next_month_url": _report_url(
                    view, _shift_month(month, 1), coverage
                ),
                "schedule_workspace_month_picker_action": reverse(_report_view_name(view)),
                "schedule_workspace_month_picker_preserved_query": (
                    {"coverage": coverage} if coverage else {}
                ),
            }
        )
    return context


def _report_url(view, month, coverage=None):
    url = reverse(_report_view_name(view))
    query = {"month": month.strftime("%Y-%m")} if month else {}
    if coverage:
        query["coverage"] = coverage
    return f"{url}?{urlencode(query)}" if query else url


def _report_view_name(view):
    return {
        "community_preceptor": "community-preceptor-stipend-report",
        "late_doc": "late-doc-stipend-report",
        "faculty_admin_time": "faculty-admin-time-report",
    }[view]
