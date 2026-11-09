"""Presentation-only PDF exports for current schedule projections."""

import re
from datetime import datetime, timedelta
from html import escape
from io import BytesIO
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    KeepInFrame,
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    TableStyle,
)

from .services.operational_attention import ORGANIZATION_TIME_ZONE

_FONT_NAME = "PreceptorScheduleNotoSansSC"
_ARABIC_FONT_NAME = "PreceptorScheduleNotoNaskhArabic"
_FONT_DIRECTORY = Path(__file__).with_name("fonts")
_ARABIC_RUN = re.compile(
    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]+"
    r"(?:[ \t]+[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]+)*"
)
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_STIPEND_MARGIN = 0.55 * inch
_STIPEND_PRINTABLE_WIDTH = letter[0] - (2 * _STIPEND_MARGIN)
_STIPEND_AMOUNT_MINIMUM_WIDTH_INCHES = 0.5
_SCHEDULE_MARGIN = 0.45 * inch
_PRECEPTING_CALENDAR_MARGIN = 0.35 * inch
_PRECEPTING_CALENDAR_CELL_HORIZONTAL_PADDING = 3
_PRECEPTING_CALENDAR_LANES = (("AM", "am"), ("PM", "pm"), ("Late Doc", "late_doc"))
_MASTER_SCHEDULE_MINIMUM_DAY_WIDTH = 1 * inch
_SURFACE_TONES = {
    "subtle": colors.HexColor("#F6F8FA"),
    "calendar": colors.HexColor("#E2E6EA"),
    "holiday": colors.HexColor("#F3EFFA"),
    "schedule_group": colors.HexColor("#E6F0EF"),
}

# Fictional facility identity shared by the report families, matching the application shell.
_REPORT_FACILITY_NAME = "Family Practice Clinic"

_SCHEDULE_REPORT_LABELS = {
    "precepting": "Precepting",
    "community-precepting": "Community Precepting",
    "academic-events": "Academic Events",
    "interviews": "Interviews",
    "normal-rounding": "Normal Rounding",
    "casual-rounding": "Casual Rounding",
    "vacation": "Vacation",
}


def master_schedule_filename(projection):
    """Return the stable current-view export filename for a Monday-starting week."""
    return f"master-schedule-{projection.week_start.isoformat()}.pdf"


def precepting_calendar_filename(month):
    """Return the stable filename for a current-view month export."""
    return f"precepting-calendar-{month:%Y-%m}.pdf"


def build_precepting_calendar_pdf(month, weeks, *, captured_at, last_edited_at=None):
    """Render a current-state, five-weekday precepting calendar projection."""
    _register_font()
    buffer = BytesIO()
    pagesize = landscape(letter)
    document = SimpleDocTemplate(
        buffer,
        pagesize=pagesize,
        leftMargin=_PRECEPTING_CALENDAR_MARGIN,
        rightMargin=_PRECEPTING_CALENDAR_MARGIN,
        topMargin=_PRECEPTING_CALENDAR_MARGIN,
        bottomMargin=_PRECEPTING_CALENDAR_MARGIN,
        # TEXT-CONTRACT: precepting-calendar-month-title
        # The month export identifies its selected month with this exact paired
        # metadata and visible-heading format: "Precepting - " (space after "-"), month
        # name, ", " (space after the comma), and a four-digit year. Protecting method:
        # test_uses_concise_month_title_and_subtitle_without_a_repeated_month in
        # schedules/tests/test_precepting_calendar_pdf.py (PreceptingCalendarPdfTests).
        title=f"Precepting - {month:%B, %Y}",
    )
    styles = _styles()
    if last_edited_at is None:
        # TEXT-CONTRACT: precepting-month-no-edits
        # docs/design.md, Accepted direction: month-calendar Precepting, requires
        # the human-selected lowercase label, internal spaces and no punctuation;
        # surrounding presentation is not fixed.
        # Protected by test_unedited_month_pdf_uses_the_initial_label_without_recording_an_edit
        # in schedules/tests/test_precepting_calendar_pdf.py.
        edit_label = "no edits yet"
    else:
        edit_label = (
            f"{_timestamp_label(last_edited_at.astimezone(ORGANIZATION_TIME_ZONE))} "
            f"({ORGANIZATION_TIME_ZONE})"
        )
    story = [
        # TEXT-CONTRACT: precepting-calendar-month-title
        # The month export identifies its selected month with this exact
        # visible-heading and paired metadata format: "Precepting - " (space after "-"),
        # month name, ", " (space after the comma), and a four-digit year. Protecting method:
        # test_uses_concise_month_title_and_subtitle_without_a_repeated_month in
        # schedules/tests/test_precepting_calendar_pdf.py (PreceptingCalendarPdfTests).
        Paragraph(f"Precepting - {month:%B, %Y}", styles["pdf_title"]),
        Paragraph(
            f"Current schedule state | Generated {_text(_timestamp_label(captured_at))}",
            styles["pdf_subtitle"],
        ),
        Paragraph(f"Last edit: {_text(edit_label)}", styles["pdf_subtitle"]),
        Spacer(1, 7),
    ]
    story.append(_precepting_calendar_month_table(month, weeks, pagesize, styles))
    document.build(story)
    return buffer.getvalue()


def _precepting_calendar_month_table(month, weeks, pagesize, styles):
    """Render the whole month as one weekday grid with indivisible week groups."""
    rows = [
        [""]
        + [
            Paragraph(weekday, styles["pdf_precepting_calendar_weekday"])
            for weekday in _WEEKDAYS[:5]
        ]
    ]
    week_starts = []
    for week in weeks:
        week_starts.append(len(rows))
        rows.append(
            [""]
            + [
                Paragraph(
                    _precepting_calendar_date_header(day),
                    styles[
                        "pdf_precepting_calendar_adjacent_date"
                        if day["date"].month != month.month
                        else "pdf_precepting_calendar_date"
                    ],
                )
                for day in week["days"]
            ]
        )
        for label, key in _PRECEPTING_CALENDAR_LANES:
            rows.append(
                [Paragraph(label, styles["pdf_precepting_calendar_lane"])]
                + [
                    Paragraph(
                        _precepting_calendar_day_content(day[key]),
                        styles["pdf_precepting_calendar"],
                    )
                    if day[key]
                    else ""
                    for day in week["days"]
                ]
            )
    lane_width = _precepting_calendar_lane_width(styles)
    content_width = (pagesize[0] - (2 * _PRECEPTING_CALENDAR_MARGIN) - lane_width) / 5
    table = _table(rows, (lane_width, *(content_width for _ in range(5))))
    commands = [
        ("BACKGROUND", (0, 1), (0, -1), _SURFACE_TONES["subtle"]),
        ("BACKGROUND", (1, 1), (-1, -1), colors.white),
        ("LEFTPADDING", (0, 0), (-1, -1), _PRECEPTING_CALENDAR_CELL_HORIZONTAL_PADDING),
        ("RIGHTPADDING", (0, 0), (-1, -1), _PRECEPTING_CALENDAR_CELL_HORIZONTAL_PADDING),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]
    for week_start, week in zip(week_starts, weeks, strict=True):
        commands.extend(
            [
                ("BACKGROUND", (0, week_start), (-1, week_start), _SURFACE_TONES["calendar"]),
                ("LINEABOVE", (0, week_start), (-1, week_start), 0.9, colors.HexColor("#52667A")),
                ("NOSPLIT", (0, week_start), (-1, week_start + len(_PRECEPTING_CALENDAR_LANES))),
            ]
        )
        for day_index, day in enumerate(week["days"], start=1):
            if day["date"].month != month.month:
                commands.append(
                    (
                        "BACKGROUND",
                        (day_index, week_start),
                        (day_index, week_start + len(_PRECEPTING_CALENDAR_LANES)),
                        _SURFACE_TONES["subtle"],
                    )
                )
            if day.get("has_holiday_closure"):
                commands.append(
                    (
                        "BACKGROUND",
                        (day_index, week_start + 1),
                        (day_index, week_start + len(_PRECEPTING_CALENDAR_LANES)),
                        _SURFACE_TONES["holiday"],
                    )
                )
    table.setStyle(TableStyle(commands))
    return table


def _precepting_calendar_lane_width(styles):
    """Fit the visible lane labels in their rendered PDF style and table padding."""
    lane_style = styles["pdf_precepting_calendar_lane"]
    label_width = max(
        pdfmetrics.stringWidth(label, lane_style.fontName, lane_style.fontSize)
        for label, _ in _PRECEPTING_CALENDAR_LANES
    )
    return label_width + (2 * _PRECEPTING_CALENDAR_CELL_HORIZONTAL_PADDING)


def _precepting_calendar_date_header(day):
    return f"{day['date']:%b} {day['date'].day}"


def _precepting_calendar_day_content(assignments):
    return "<br/>".join(_text(value) for _, value in assignments)


def build_master_schedule_pdf(projection, *, clinic_grid, supporting_grid, captured_at):
    """Render the displayed, current Master Schedule projection without creating an artifact."""
    _register_font()
    buffer = BytesIO()
    styles = _styles()
    pagesize = _master_schedule_pagesize(clinic_grid, supporting_grid, styles)
    document = SimpleDocTemplate(
        buffer,
        pagesize=pagesize,
        leftMargin=_SCHEDULE_MARGIN,
        rightMargin=_SCHEDULE_MARGIN,
        topMargin=_SCHEDULE_MARGIN,
        bottomMargin=_SCHEDULE_MARGIN,
        title="Master Schedule",
    )
    story = [
        _master_schedule_header(projection, pagesize, styles),
        Spacer(1, 8),
        _master_schedule_grid_table(clinic_grid, styles, pagesize=pagesize, clinic=True),
        Spacer(1, 8),
        Paragraph("Supporting schedules", styles["pdf_heading"]),
        _master_schedule_grid_table(supporting_grid, styles, pagesize=pagesize, clinic=False),
        Spacer(1, 8),
        Paragraph(f"Captured {_timestamp_label(captured_at)}", styles["pdf_body"]),
    ]
    document.build(story)
    return buffer.getvalue()


def monthly_stipend_filename(report):
    return f"{report.kind.replace('_', '-')}-stipend-{report.month:%Y-%m}.pdf"


def build_monthly_stipend_pdf(report, *, generated_at, generated_by):
    """Render an ephemeral stipend report from its shared monthly projection."""
    _register_font()
    buffer = BytesIO()
    # TEXT-CONTRACT: stipend-pdf-title-prefix
    # docs/design.md section 25.1 requires the shell's fictional facility
    # in the visible and PDF-metadata title: "Family Practice Clinic - ". Scope:
    # capitalization, hyphen-minus and surrounding spaces. Protected by
    # schedules/tests/test_stipend_reports.py::StipendReportPdfTests.test_community_download_extracts_shared_projection_with_provenance_and_totals.
    title = f"{_REPORT_FACILITY_NAME} - {_stipend_kind_label(report.kind)} Stipend Report"
    document = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=_STIPEND_MARGIN,
        rightMargin=_STIPEND_MARGIN,
        topMargin=_STIPEND_MARGIN,
        bottomMargin=_STIPEND_MARGIN,
        title=title,
        author=str(generated_by),
    )
    styles = _styles()
    story = [
        Paragraph(title, styles["pdf_title"]),
        Paragraph(
            f"<b>{report.month.strftime('%B %Y')}</b> | Current-record basis",
            styles["pdf_subtitle"],
        ),
        Spacer(1, 8),
        Paragraph("Report basis", styles["pdf_heading"]),
        Paragraph(
            "Current corrected and future scheduled assignment records at the time of generation. "
            "This report is not stored as an artifact or revision.",
            styles["pdf_body"],
        ),
        _stipend_basis_table(generated_at, generated_by, styles),
        Spacer(1, 8),
    ]
    if report.missing_late_doc_dates:
        story.extend(
            [
                Paragraph("Warnings", styles["pdf_heading"]),
                *_stipend_warning_paragraphs(report, styles),
                Spacer(1, 8),
            ]
        )
    story.extend(
        [
            Paragraph("Itemized shifts", styles["pdf_heading"]),
            _stipend_detail_table(report, styles),
            Spacer(1, 8),
            Paragraph("Totals", styles["pdf_heading"]),
            _stipend_totals_table(report, styles),
        ]
    )
    document.build(story)
    return buffer.getvalue()


def schedule_report_filename(report):
    parameters = report.parameters
    if parameters.period == "academic-year":
        period = f"academic-year-{parameters.start_date.year}-{parameters.end_date.year}"
    elif parameters.period == "calendar-year":
        period = f"calendar-year-{parameters.start_date.year}"
    else:
        period = f"custom-{parameters.start_date.isoformat()}-to-{parameters.end_date.isoformat()}"
    return f"muriel-{parameters.schedule}-schedule-report-{period}-{parameters.coverage}.pdf"


def build_schedule_report_pdf(report, *, generated_at, generated_by):
    """Render an ephemeral schedule report from its already-calculated screen projection."""
    _register_font()
    parameters = report.parameters
    report_label = _SCHEDULE_REPORT_LABELS[parameters.schedule]
    pagesize = landscape(letter) if parameters.schedule == "precepting" else letter
    printable_width = pagesize[0] - (2 * _SCHEDULE_MARGIN)
    title = f"{_REPORT_FACILITY_NAME} - {report_label} Schedule Report"
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=pagesize,
        leftMargin=_SCHEDULE_MARGIN,
        rightMargin=_SCHEDULE_MARGIN,
        topMargin=_SCHEDULE_MARGIN,
        bottomMargin=_SCHEDULE_MARGIN,
        title=title,
        author=str(generated_by),
    )
    styles = _styles()
    story = [
        Paragraph(title, styles["pdf_title"]),
        Paragraph(
            f"<b>{_schedule_period_label(parameters)}</b> | {_schedule_coverage_label(parameters)}",
            styles["pdf_subtitle"],
        ),
        Spacer(1, 8),
        Paragraph("Report basis", styles["pdf_heading"]),
        Paragraph(
            "Current corrected records at the time of generation. "
            "This report is not retained as an issued artifact.",
            styles["pdf_body"],
        ),
        _schedule_basis_table(parameters, generated_at, generated_by, printable_width, styles),
        Spacer(1, 8),
    ]
    if parameters.includes_scheduled:
        story.extend(
            [
                # TEXT-CONTRACT: schedule-report-pdf-planning-notice
                # Scheduled/combined PDFs use this exact notice so saved reports are not
                # mistaken for final historical output (docs/design.md section 25.2).
                # Scope is every character, including the em dash. Protecting test:
                # schedules/tests/test_schedule_reports.py::ScheduleReportPdfTests.test_pdf_artifact_includes_the_precepting_record_and_findings_table.
                Paragraph(
                    "Planning report — includes scheduled future work and may change",
                    styles["pdf_planning"],
                ),
                Spacer(1, 8),
            ]
        )
    story.extend(
        [
            Paragraph(f"{report_label} workload", styles["pdf_heading"]),
            _schedule_report_table(report, printable_width, styles),
        ]
    )
    if parameters.includes_scheduled:
        story.extend(
            [
                Spacer(1, 8),
                Paragraph("Current findings", styles["pdf_heading"]),
                _schedule_findings_table(report.findings, printable_width, styles),
            ]
        )
    document.build(story)
    return buffer.getvalue()


def _register_font():
    if _FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(_FONT_NAME, str(_FONT_DIRECTORY / "NotoSansSC-VF.ttf")))
    if _ARABIC_FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(
            TTFont(_ARABIC_FONT_NAME, str(_FONT_DIRECTORY / "NotoNaskhArabic-Regular.ttf"))
        )


def _styles():
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="pdf_title",
            parent=styles["Title"],
            fontName=_FONT_NAME,
            fontSize=18,
            leading=22,
            spaceAfter=2,
        )
    )
    styles.add(
        ParagraphStyle(
            name="pdf_subtitle",
            parent=styles["Normal"],
            fontName=_FONT_NAME,
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#374151"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="pdf_master_schedule_title",
            parent=styles["pdf_title"],
            fontSize=13,
            leading=16,
        )
    )
    styles.add(
        ParagraphStyle(
            name="pdf_heading",
            parent=styles["Heading2"],
            fontName=_FONT_NAME,
            fontSize=11,
            leading=14,
            spaceBefore=2,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="pdf_body",
            parent=styles["BodyText"],
            fontName=_FONT_NAME,
            fontSize=8,
            leading=10,
            spaceAfter=3,
        )
    )
    styles.add(
        ParagraphStyle(
            name="pdf_planning",
            parent=styles["pdf_body"],
            fontName=_FONT_NAME,
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#7C2D12"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="pdf_table",
            parent=styles["BodyText"],
            fontName=_FONT_NAME,
            fontSize=7.5,
            leading=9,
        )
    )
    styles.add(
        ParagraphStyle(
            name="pdf_table_header",
            parent=styles["pdf_table"],
            textColor=colors.white,
            alignment=TA_CENTER,
        )
    )
    styles.add(
        ParagraphStyle(
            name="pdf_precepting_calendar",
            parent=styles["pdf_table"],
            fontSize=6.5,
            leading=7.6,
        )
    )
    styles.add(
        ParagraphStyle(
            name="pdf_precepting_calendar_weekday",
            parent=styles["pdf_precepting_calendar"],
            textColor=colors.white,
            alignment=TA_CENTER,
        )
    )
    styles.add(
        ParagraphStyle(
            name="pdf_precepting_calendar_date",
            parent=styles["pdf_precepting_calendar"],
            alignment=TA_CENTER,
        )
    )
    styles.add(
        ParagraphStyle(
            name="pdf_precepting_calendar_adjacent_date",
            parent=styles["pdf_precepting_calendar_date"],
            textColor=colors.HexColor("#6B7280"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="pdf_precepting_calendar_lane",
            parent=styles["pdf_precepting_calendar"],
        )
    )
    styles.add(
        ParagraphStyle(
            name="pdf_master_schedule_coordinate_header",
            parent=styles["pdf_table"],
            alignment=TA_CENTER,
        )
    )
    styles.add(
        ParagraphStyle(
            name="pdf_master_schedule_holiday",
            parent=styles["pdf_table"],
            alignment=TA_CENTER,
            textColor=colors.HexColor("#493967"),
        )
    )
    return styles


def _master_schedule_header(projection, pagesize, styles):
    """Match the browser's compact, contained title treatment without an action."""
    header = _table(
        [
            [
                Paragraph(
                    f"Master Schedule \N{EM DASH} {_master_schedule_period_label(projection)}",
                    styles["pdf_master_schedule_title"],
                )
            ],
        ],
        (pagesize[0] - (2 * _SCHEDULE_MARGIN),),
        has_header=False,
    )
    header.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0, colors.white),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#9CA3AF")),
                ("BACKGROUND", (0, 0), (-1, -1), _SURFACE_TONES["subtle"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return header


def _master_schedule_period_label(projection):
    start = projection.week_start
    end = start + timedelta(days=4)
    if start.month == end.month:
        return f"{start:%b} {start.day}\N{EN DASH}{end.day}, {start.year}"
    if start.year == end.year:
        return f"{start:%b} {start.day}\N{EN DASH}{end:%b} {end.day}, {start.year}"
    return f"{start:%b} {start.day}, {start.year}\N{EN DASH}{end:%b} {end.day}, {end.year}"


def _stipend_basis_table(generated_at, generated_by, styles):
    return _table(
        [
            [
                Paragraph("Generated by", styles["pdf_table"]),
                Paragraph(_text(generated_by), styles["pdf_table"]),
            ],
            [
                Paragraph("Generated at", styles["pdf_table"]),
                Paragraph(_text(_timestamp_label(generated_at)), styles["pdf_table"]),
            ],
        ],
        _stipend_table_widths((1.35, None)),
        has_header=False,
    )


def _schedule_period_label(parameters):
    if parameters.period == "academic-year":
        return f"Academic year {parameters.start_date.year}-{parameters.end_date.year}"
    if parameters.period == "calendar-year":
        return f"Calendar year {parameters.start_date.year}"
    return (
        f"Custom range {parameters.start_date:%b %-d, %Y} through {parameters.end_date:%b %-d, %Y}"
    )


def _schedule_coverage_label(parameters):
    coverage = dict(
        history="History",
        scheduled="Scheduled",
        history_and_scheduled="History + scheduled",
    )[parameters.coverage.replace("-", "_")]
    return (
        f"{coverage}: {parameters.effective_start:%b %-d, %Y} through "
        f"{parameters.effective_end:%b %-d, %Y}"
    )


def _schedule_basis_table(parameters, generated_at, generated_by, printable_width, styles):
    rows = [
        [
            Paragraph("Selected period", styles["pdf_table"]),
            Paragraph(_text(_schedule_period_label(parameters)), styles["pdf_table"]),
        ],
        [
            Paragraph("Effective coverage", styles["pdf_table"]),
            Paragraph(_text(_schedule_coverage_label(parameters)), styles["pdf_table"]),
        ],
        [
            Paragraph("Generated by", styles["pdf_table"]),
            Paragraph(_text(generated_by), styles["pdf_table"]),
        ],
        [
            Paragraph("Generated at", styles["pdf_table"]),
            Paragraph(_text(_timestamp_label(generated_at)), styles["pdf_table"]),
        ],
    ]
    return _table(rows, (1.25 * inch, printable_width - (1.25 * inch)), has_header=False)


def _schedule_report_table(report, printable_width, styles):
    headers, values = _schedule_table_values(report)
    if not values:
        return Paragraph("No current facts fall in this coverage.", styles["pdf_body"])
    widths = _schedule_table_widths(headers, values, printable_width)
    rows = [[Paragraph(header, styles["pdf_table_header"]) for header in headers]]
    for row in values:
        physician = KeepInFrame(
            widths[0] - 8,
            styles["pdf_table"].leading,
            [Paragraph(_text(row[0]), styles["pdf_table"])],
            mode="shrink",
        )
        rows.append(
            [physician, *(Paragraph(_text(value), styles["pdf_table"]) for value in row[1:])]
        )
    return _table(rows, widths)


def _schedule_table_values(report):
    schedule = report.parameters.schedule
    if schedule == "academic-events":
        return (
            ("Physician", "Events", "Event days", "Affected shifts"),
            tuple(
                (
                    row.physician.display_name,
                    str(row.events),
                    str(row.event_days),
                    str(row.affected_shifts),
                )
                for row in report.rows
            ),
        )
    if schedule == "interviews":
        return ("Physician", "Shifts"), tuple(
            (row.physician.display_name, str(row.shifts)) for row in report.rows
        )
    if schedule == "normal-rounding":
        return ("Physician", "Weeks"), tuple(
            (row.physician.display_name, str(row.weeks)) for row in report.rows
        )
    if schedule == "casual-rounding":
        return ("Physician", "Days"), tuple(
            (row.physician.display_name, str(row.days)) for row in report.rows
        )
    if schedule == "vacation":
        return (
            ("Physician", "Ranges", "Weekdays", "Weekend days", "Total days"),
            tuple(
                (
                    row.physician.display_name,
                    str(row.ranges),
                    str(row.weekdays),
                    str(row.weekend_days),
                    str(row.total_days),
                )
                for row in report.rows
            ),
        )
    if schedule == "community-precepting":
        return (
            ("Physician", "AM", "PM", "Total"),
            tuple(
                (row.physician.display_name, str(row.am), str(row.pm), str(row.precepting_total))
                for row in report.rows
            ),
        )
    return (
        ("Physician", "AM", "PM", "Primary", "Secondary", "Precepting total", "Late Doc"),
        tuple(
            (
                row.physician.display_name,
                str(row.am),
                str(row.pm),
                str(row.primary),
                str(row.secondary),
                str(row.precepting_total),
                str(row.late_doc),
            )
            for row in report.rows
        ),
    )


def _schedule_table_widths(headers, values, printable_width):
    numeric_widths = tuple(
        max(
            0.45 * inch,
            max(pdfmetrics.stringWidth(value, _FONT_NAME, 7.5) for value in column) + 8,
        )
        for column in zip(headers[1:], *(row[1:] for row in values), strict=True)
    )
    return (printable_width - sum(numeric_widths), *numeric_widths)


def _schedule_findings_table(findings, printable_width, styles):
    if not findings:
        return Paragraph("No current findings.", styles["pdf_body"])
    widths = (printable_width - (1.15 * inch) - (0.55 * inch), 1.15 * inch, 0.55 * inch)
    rows = [
        [
            Paragraph("Finding", styles["pdf_table_header"]),
            Paragraph("Severity", styles["pdf_table_header"]),
            Paragraph("Count", styles["pdf_table_header"]),
        ]
    ]
    rows.extend(
        [
            Paragraph(_text(finding.label), styles["pdf_table"]),
            Paragraph(_text(finding.severity.capitalize()), styles["pdf_table"]),
            Paragraph(str(finding.count), styles["pdf_table"]),
        ]
        for finding in findings
    )
    return _table(rows, widths)


def _stipend_warning_paragraphs(report, styles):
    if report.missing_late_doc_dates:
        dates = ", ".join(_date_label(day.isoformat()) for day in report.missing_late_doc_dates)
        return [
            Paragraph(
                f"<b>Late Doc work item:</b> No assignment is recorded for: {_text(dates)}.",
                styles["pdf_body"],
            )
        ]
    return []


def _stipend_detail_table(report, styles):
    if not report.details:
        return Paragraph(
            f"No qualifying shifts in {report.month.strftime('%B %Y')}.", styles["pdf_body"]
        )
    rows = [
        [
            Paragraph("Date", styles["pdf_table_header"]),
            Paragraph("Period", styles["pdf_table_header"]),
            Paragraph("Physician", styles["pdf_table_header"]),
            Paragraph("Hours", styles["pdf_table_header"]),
            Paragraph("Hourly rate", styles["pdf_table_header"]),
            Paragraph("Amount", styles["pdf_table_header"]),
        ]
    ]
    for detail in report.details:
        rows.append(
            [
                Paragraph(_date_label(detail.date.isoformat()), styles["pdf_table"]),
                Paragraph(_text(detail.period), styles["pdf_table"]),
                Paragraph(_text(detail.physician.display_name), styles["pdf_table"]),
                Paragraph(str(detail.hours), styles["pdf_table"]),
                Paragraph(_dollars(detail.hourly_rate), styles["pdf_table"]),
                Paragraph(_dollars(detail.amount), styles["pdf_table"]),
            ]
        )
    return _table(
        rows,
        _stipend_table_widths(
            (
                None,
                _stipend_content_width(
                    ["Period", *(detail.period for detail in report.details)], 0.75
                ),
                _stipend_content_width(
                    [
                        "Physician",
                        *(detail.physician.display_name for detail in report.details),
                    ],
                    1.5,
                ),
                _stipend_content_width(
                    ["Hours", *(str(detail.hours) for detail in report.details)], 0.7
                ),
                _stipend_content_width(
                    ["Hourly rate", *(_dollars(detail.hourly_rate) for detail in report.details)],
                    0.9,
                ),
                max(
                    _STIPEND_AMOUNT_MINIMUM_WIDTH_INCHES,
                    _stipend_content_width(
                        ["Amount", *(_dollars(detail.amount) for detail in report.details)], 0.9
                    ),
                ),
            )
        ),
    )


def _stipend_totals_table(report, styles):
    rows = [
        [
            Paragraph("Physician", styles["pdf_table_header"]),
            Paragraph("Shifts", styles["pdf_table_header"]),
            Paragraph("Hours", styles["pdf_table_header"]),
            Paragraph("Stipend amount", styles["pdf_table_header"]),
        ]
    ]
    rows.extend(
        [
            Paragraph(_text(physician.physician.display_name), styles["pdf_table"]),
            Paragraph(str(physician.shift_total), styles["pdf_table"]),
            Paragraph(str(physician.hour_total), styles["pdf_table"]),
            Paragraph(_dollars(physician.amount_total), styles["pdf_table"]),
        ]
        for physician in report.physicians
    )
    rows.append(
        [
            Paragraph("Overall total", styles["pdf_table"]),
            Paragraph(str(report.shift_total), styles["pdf_table"]),
            Paragraph(str(report.hour_total), styles["pdf_table"]),
            Paragraph(_dollars(report.amount_total), styles["pdf_table"]),
        ]
    )
    return _table(
        rows,
        _stipend_table_widths(
            (
                None,
                _stipend_content_width(
                    ["Shifts", *(str(physician.shift_total) for physician in report.physicians)],
                    0.7,
                ),
                _stipend_content_width(
                    ["Hours", *(str(physician.hour_total) for physician in report.physicians)], 0.7
                ),
                _stipend_content_width(
                    [
                        "Stipend amount",
                        *(_dollars(physician.amount_total) for physician in report.physicians),
                        _dollars(report.amount_total),
                    ],
                    1.2,
                ),
            )
        ),
    )


def _stipend_kind_label(kind):
    return {
        "community_preceptor": "Community Preceptor",
        "late_doc": "Late Doc",
    }[kind]


def _timestamp_label(value):
    return value.strftime("%Y-%m-%d %H:%M %Z")


def _dollars(value):
    return f"${value:.2f}" if value is not None else "Incomplete"


class _CounterclockwiseParagraph(Flowable):
    """Keep a row-group label readable in the same counterclockwise direction as the grid."""

    def __init__(self, text, style):
        super().__init__()
        self.paragraph = Paragraph(_text(text), style)
        self.paragraph_width = pdfmetrics.stringWidth(text, style.fontName, style.fontSize) + 1

    def wrap(self, available_width, available_height):
        _, paragraph_height = self.paragraph.wrap(self.paragraph_width, available_height)
        self.width = paragraph_height
        self.height = self.paragraph_width
        return self.width, self.height

    def draw(self):
        self.canv.translate(self.width, 0)
        self.canv.rotate(90)
        self.paragraph.drawOn(self.canv, 0, 0)


def _master_schedule_pagesize(clinic_grid, supporting_grid, styles):
    portrait_width = letter[0] - (2 * _SCHEDULE_MARGIN)
    stub_widths = (
        _master_schedule_stub_width(clinic_grid, styles, clinic=True),
        _master_schedule_stub_width(supporting_grid, styles, clinic=False),
    )
    if all(
        (portrait_width - stub_width) / len(grid.days) >= _MASTER_SCHEDULE_MINIMUM_DAY_WIDTH
        for grid, stub_width in zip((clinic_grid, supporting_grid), stub_widths, strict=True)
    ):
        return letter
    return landscape(letter)


def _master_schedule_stub_width(grid, styles, *, clinic):
    row_label_width = max(
        pdfmetrics.stringWidth(
            row.label, styles["pdf_table"].fontName, styles["pdf_table"].fontSize
        )
        + 8
        for group in grid.groups
        for row in group.rows
    )
    return row_label_width + (0.35 * inch if clinic else 0)


def _master_schedule_grid_table(grid, styles, *, pagesize, clinic):
    date_headers = [_master_schedule_date_header(day, styles) for day in grid.days]
    holiday_cell_backgrounds = []
    if clinic:
        rows = [
            [
                Paragraph("Clinic", styles["pdf_master_schedule_coordinate_header"]),
                Paragraph("", styles["pdf_master_schedule_coordinate_header"]),
                *date_headers,
            ]
        ]
        spans = []
        row_group_alignment = []
        row_group_backgrounds = []
        for group in grid.groups:
            if group.key not in {"clinic-morning", "clinic-afternoon"}:
                group_row = len(rows)
                rows.append(
                    [Paragraph(_text(group.label), styles["pdf_table"]), *("" for _ in grid.days)]
                )
                spans.append(("SPAN", (0, group_row), (-1, group_row)))
                for row in group.rows:
                    rows.append(
                        [
                            "",
                            Paragraph(_text(row.label), styles["pdf_table"]),
                            *[_master_schedule_cell(cell, styles) for cell in row.cells],
                        ]
                    )
                continue
            for index, row in enumerate(group.rows):
                rows.append(
                    [
                        _CounterclockwiseParagraph(group.label, styles["pdf_table"])
                        if index == 0
                        else "",
                        Paragraph(_text(row.label), styles["pdf_table"]),
                        *[
                            _master_schedule_closed_cell(styles)
                            if day_index in group.closed_day_indexes
                            else _master_schedule_cell(cell, styles)
                            for day_index, cell in enumerate(row.cells)
                        ],
                    ]
                )
            if group.rows:
                end = len(rows) - 1
                start = end - len(group.rows) + 1
                if end > start:
                    spans.append(("SPAN", (0, start), (0, end)))
                    row_group_alignment.append(("VALIGN", (0, start), (0, end), "MIDDLE"))
                row_group_backgrounds.append(
                    ("BACKGROUND", (0, start), (0, end), _SURFACE_TONES["schedule_group"])
                )
                for day_index in group.closed_day_indexes:
                    column = day_index + 2
                    if end > start:
                        spans.append(("SPAN", (column, start), (column, end)))
                    row_group_backgrounds.append(
                        ("BACKGROUND", (column, start), (column, end), _SURFACE_TONES["holiday"])
                    )
        position_width = _master_schedule_stub_width(grid, styles, clinic=False)
        group_width = 0.35 * inch
        spans.append(("SPAN", (0, 0), (1, 0)))
        day_width = (pagesize[0] - (2 * _SCHEDULE_MARGIN) - group_width - position_width) / len(
            grid.days
        )
        widths = (group_width, position_width, *(day_width,) * len(grid.days))
    else:
        rows = [
            [
                Paragraph("Schedule", styles["pdf_master_schedule_coordinate_header"]),
                *date_headers,
            ]
        ]
        spans = []
        row_group_alignment = []
        row_group_backgrounds = []
        for group in grid.groups:
            group_row = len(rows)
            rows.append(
                [Paragraph(_text(group.label), styles["pdf_table"]), *("" for _ in grid.days)]
            )
            spans.append(("SPAN", (0, group_row), (-1, group_row)))
            for row in group.rows:
                row_index = len(rows)
                rows.append(
                    [
                        Paragraph(_text(row.label), styles["pdf_table"]),
                        *[_master_schedule_cell(cell, styles) for cell in row.cells],
                    ]
                )
                holiday_cell_backgrounds.extend(
                    (
                        "BACKGROUND",
                        (day_index + 1, row_index),
                        (day_index + 1, row_index),
                        _SURFACE_TONES["holiday"],
                    )
                    for day_index, day in enumerate(grid.days)
                    if day.holidays
                )
        stub_width = _master_schedule_stub_width(grid, styles, clinic=False)
        day_width = (pagesize[0] - (2 * _SCHEDULE_MARGIN) - stub_width) / len(grid.days)
        widths = (stub_width, *(day_width,) * len(grid.days))
    table = _table(rows, widths)
    table.setStyle(
        TableStyle(
            [
                *spans,
                *row_group_alignment,
                ("BACKGROUND", (0, 1), (-1, -1), colors.white),
                ("BACKGROUND", (0, 0), (-1, 0), _SURFACE_TONES["calendar"]),
                (
                    "BACKGROUND",
                    (1, 1) if clinic else (0, 1),
                    (1, -1) if clinic else (0, -1),
                    _SURFACE_TONES["subtle"],
                ),
                *row_group_backgrounds,
                *holiday_cell_backgrounds,
            ]
        )
    )
    for command in spans:
        if command[2][0] == -1:
            _, (_, row), _ = command
            table.setStyle(
                TableStyle([("BACKGROUND", (0, row), (-1, row), _SURFACE_TONES["schedule_group"])])
            )
    return table


def _master_schedule_cell(cell, styles):
    parts = []
    for fact in cell.facts:
        detail = f"; {fact.detail}" if fact.detail else ""
        attention = f" {fact.attention[:1].upper()}{fact.attention[1:]}" if fact.attention else ""
        parts.append(f"{_text(fact.label)}{_text(detail)}{_text(attention)}")
    for finding in cell.findings:
        sources = ", ".join(source.label for source in finding.sources)
        source_text = f"<br/>Sources: {_text(sources)}" if sources else ""
        severity = f"{finding.severity[:1].upper()}{finding.severity[1:]}"
        parts.append(f"<b>{_text(severity)} finding:</b> {_text(finding.message)}{source_text}")
    return Paragraph("<br/>".join(parts), styles["pdf_table"]) if parts else ""


def _master_schedule_date_header(day, styles):
    holiday_labels = "<br/>".join(
        _text(f"{holiday.name}, PM" if holiday.name == "Christmas Eve" else holiday.name)
        for holiday in day.holidays
    )
    text = f"{_text(day.label)}<br/>{holiday_labels}" if holiday_labels else _text(day.label)
    return Paragraph(text, styles["pdf_master_schedule_coordinate_header"])


def _master_schedule_closed_cell(styles):
    return Paragraph("Closed", styles["pdf_master_schedule_holiday"])


def _table(rows, widths, has_header=True):
    table = LongTable(rows, colWidths=widths, repeatRows=1 if has_header else 0, hAlign="LEFT")
    commands = [
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#9CA3AF")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    if has_header:
        commands.insert(0, ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E79")))
    table.setStyle(TableStyle(commands))
    return table


def _stipend_table_widths(widths):
    """Give one flexible stipend column the page width left by content columns."""
    if widths.count(None) != 1:
        raise ValueError("Stipend tables require exactly one flexible column.")
    remaining = _STIPEND_PRINTABLE_WIDTH - sum(
        width * inch for width in widths if width is not None
    )
    return tuple(remaining if width is None else width * inch for width in widths)


def _stipend_content_width(values, maximum):
    padding = 8
    return min(
        maximum,
        max(pdfmetrics.stringWidth(value, _FONT_NAME, 7.5) + padding for value in values) / inch,
    )


def _date_label(value):
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    return f"{_WEEKDAYS[parsed.weekday()]}, {parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def _text(value):
    text = str(value)
    output = []
    position = 0
    for match in _ARABIC_RUN.finditer(text):
        output.append(escape(text[position : match.start()]))
        shaped = get_display(arabic_reshaper.reshape(match.group()))
        output.append(f'<font name="{_ARABIC_FONT_NAME}">{escape(shaped)}</font>')
        position = match.end()
    output.append(escape(text[position:]))
    return "".join(output)
