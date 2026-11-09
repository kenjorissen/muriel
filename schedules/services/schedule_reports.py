"""Read-only date-range projections for the Schedule Reports workspace."""

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db.models import Q

from ..academic_years import academic_year_bounds, academic_year_for_date, academic_year_label
from ..models import (
    AcademicEvent,
    CasualRounderRule,
    EffectiveRole,
    InterviewShift,
    LateDocAssignment,
    NormalRounder,
    Physician,
    PreceptorAssignment,
    PreceptorSession,
    Vacation,
)
from .academic_events import academic_events_overlapping
from .community import community_preceptor_occurrences
from .holidays import holiday_closure_for
from .interviews import interview_findings_for_shift
from .operational_attention import organization_today
from .preceptors import (
    draft_finding_label,
    draft_findings_for_session,
    draft_findings_for_week,
    is_manually_eligible_for_position,
)
from .rounding import normal_rounder_statuses_for_weeks, normal_rounders_for_weeks
from .slot_projection import RequestedSlot, slot_projection

SCHEDULE_CHOICES = (
    ("precepting", "Precepting"),
    ("community-precepting", "Community Precepting"),
    ("interviews", "Interviews"),
    ("academic-events", "Academic Events"),
    ("normal-rounding", "Normal Rounding"),
    ("casual-rounding", "Casual Rounding"),
    ("vacation", "Vacation"),
)
PERIOD_CHOICES = (
    ("academic-year", "Academic year"),
    ("calendar-year", "Calendar year"),
    ("custom", "Custom"),
)
COVERAGE_CHOICES = (
    ("history", "History"),
    ("history-and-scheduled", "History + scheduled"),
    ("scheduled", "Scheduled"),
)
EARLIEST_REPORT_YEAR = 1
LATEST_ACADEMIC_YEAR = 9998
LATEST_CALENDAR_YEAR = 9999


@dataclass(frozen=True)
class ScheduleReportParameters:
    schedule: str
    period: str
    coverage: str
    start_date: date
    end_date: date
    effective_start: date
    effective_end: date
    today: date

    @property
    def period_label(self):
        if self.period == "academic-year":
            return academic_year_label(self.start_date.year)
        if self.period == "calendar-year":
            return str(self.start_date.year)
        return f"{self.start_date:%b %-d, %Y} through {self.end_date:%b %-d, %Y}"

    @property
    def includes_scheduled(self):
        return self.coverage in {"scheduled", "history-and-scheduled"}

    @property
    def has_history(self):
        return self.start_date < self.today

    @property
    def has_scheduled(self):
        return self.end_date >= self.today


@dataclass(frozen=True)
class PreceptingReportRow:
    physician: Physician
    am: int
    pm: int
    primary: int
    secondary: int
    precepting_total: int
    late_doc: int


@dataclass(frozen=True)
class ReportFindingSummary:
    code: str
    label: str
    severity: str
    count: int


@dataclass(frozen=True)
class PreceptingScheduleReport:
    parameters: ScheduleReportParameters
    rows: tuple[PreceptingReportRow, ...]
    findings: tuple[ReportFindingSummary, ...]


@dataclass(frozen=True)
class AcademicEventReportRow:
    physician: Physician
    events: int
    event_days: int
    affected_shifts: int


@dataclass(frozen=True)
class AcademicEventScheduleReport:
    parameters: ScheduleReportParameters
    rows: tuple[AcademicEventReportRow, ...]
    findings: tuple[ReportFindingSummary, ...]


@dataclass(frozen=True)
class InterviewReportRow:
    physician: Physician
    shifts: int


@dataclass(frozen=True)
class InterviewScheduleReport:
    parameters: ScheduleReportParameters
    rows: tuple[InterviewReportRow, ...]
    findings: tuple[ReportFindingSummary, ...]


@dataclass(frozen=True)
class NormalRoundingReportRow:
    physician: Physician
    weeks: int


@dataclass(frozen=True)
class NormalRoundingScheduleReport:
    parameters: ScheduleReportParameters
    rows: tuple[NormalRoundingReportRow, ...]
    findings: tuple[ReportFindingSummary, ...]


@dataclass(frozen=True)
class CasualRoundingReportRow:
    physician: Physician
    days: int


@dataclass(frozen=True)
class CasualRoundingScheduleReport:
    parameters: ScheduleReportParameters
    rows: tuple[CasualRoundingReportRow, ...]
    findings: tuple[ReportFindingSummary, ...]


@dataclass(frozen=True)
class VacationReportRow:
    physician: Physician
    ranges: int
    weekdays: int
    weekend_days: int
    total_days: int


@dataclass(frozen=True)
class VacationScheduleReport:
    parameters: ScheduleReportParameters
    rows: tuple[VacationReportRow, ...]
    findings: tuple[ReportFindingSummary, ...]


def schedule_report_parameters(values, *, today=None):
    """Parse a stable GET surface and intersect the selected period with coverage."""
    today = today or organization_today()
    schedule = values.get("schedule", "precepting")
    period = values.get("period", "academic-year")
    coverage = values.get("coverage", "history-and-scheduled")
    if schedule not in dict(SCHEDULE_CHOICES):
        raise ValidationError("Choose a supported schedule report.")
    if period not in dict(PERIOD_CHOICES):
        raise ValidationError("Choose an Academic year, Calendar year, or Custom period.")
    if coverage not in dict(COVERAGE_CHOICES):
        raise ValidationError("Choose History, Scheduled, or History + scheduled coverage.")

    if period == "academic-year":
        current_year = academic_year_for_date(today)
        year = _year(
            values.get("academic_year"),
            "Academic year",
            current_year,
            minimum=EARLIEST_REPORT_YEAR,
            maximum=LATEST_ACADEMIC_YEAR,
        )
        start_date, end_date = academic_year_bounds(year)
    elif period == "calendar-year":
        year = _year(
            values.get("calendar_year"),
            "Calendar year",
            today.year,
            minimum=EARLIEST_REPORT_YEAR,
            maximum=LATEST_CALENDAR_YEAR,
        )
        start_date, end_date = date(year, 1, 1), date(year, 12, 31)
    else:
        start_date = _date(values.get("start_date"), "Custom start")
        end_date = _date(values.get("end_date"), "Custom end")
        if end_date < start_date:
            raise ValidationError("Custom end date cannot be before its start date.")

    coverage = _available_coverage(coverage, start_date, end_date, today)
    history_end = today - timedelta(days=1)
    if coverage == "history":
        effective_start, effective_end = start_date, min(end_date, history_end)
    elif coverage == "scheduled":
        effective_start, effective_end = max(start_date, today), end_date
    else:
        effective_start, effective_end = start_date, end_date
    if effective_end < effective_start:
        raise ValidationError("The selected coverage does not overlap this period.")
    return ScheduleReportParameters(
        schedule=schedule,
        period=period,
        coverage=coverage,
        start_date=start_date,
        end_date=end_date,
        effective_start=effective_start,
        effective_end=effective_end,
        today=today,
    )


def precepting_schedule_report(parameters):
    """Aggregate current assignments and non-persisted Community fallback facts."""
    if parameters.schedule != "precepting":
        raise ValidationError("This schedule report is not available yet.")
    return _precepting_schedule_projection(parameters)[0]


def community_precepting_schedule_report(parameters):
    """Present the Community subset of the shared Precepting projection."""
    if parameters.schedule != "community-precepting":
        raise ValidationError("This schedule report is not available yet.")
    report, community_activity_ids = _precepting_schedule_projection(parameters)
    eligible_ids = _role_eligible_ids(
        EffectiveRole.Role.COMMUNITY_PRECEPTOR,
        parameters.effective_start,
        parameters.effective_end,
    )
    return PreceptingScheduleReport(
        parameters=parameters,
        rows=tuple(
            row for row in report.rows if row.physician.pk in eligible_ids | community_activity_ids
        ),
        findings=report.findings,
    )


def academic_event_schedule_report(parameters):
    """Aggregate assigned, overlapping Academic Event occurrences by physician."""
    if parameters.schedule != "academic-events":
        raise ValidationError("This schedule report is not available yet.")
    events = academic_events_overlapping(parameters.effective_start, parameters.effective_end)
    faculty_roles = tuple(
        EffectiveRole.objects.filter(
            role=EffectiveRole.Role.FACULTY,
            effective_start__lte=parameters.effective_end,
        )
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=parameters.effective_start))
        .select_related("physician")
    )
    rows = {}

    def counts_for(physician):
        return rows.setdefault(
            physician.pk,
            {"physician": physician, "events": 0, "event_days": 0, "affected_shifts": 0},
        )

    for event in events:
        included_days = _academic_event_days_in(
            event, parameters.effective_start, parameters.effective_end
        )
        affected_shifts = (
            included_days * (2 if event.period_of_day == AcademicEvent.PeriodOfDay.WHOLE_DAY else 1)
            if event.precepting_impact != AcademicEvent.PreceptingImpact.NONE
            else 0
        )
        if event.all_available_faculty:
            for physician, faculty_days in _faculty_event_days(
                event, parameters.effective_start, parameters.effective_end, faculty_roles
            ).items():
                counts = counts_for(physician)
                counts["events"] += 1
                counts["event_days"] += faculty_days
                counts["affected_shifts"] += (
                    faculty_days
                    * (2 if event.period_of_day == AcademicEvent.PeriodOfDay.WHOLE_DAY else 1)
                    if event.precepting_impact != AcademicEvent.PreceptingImpact.NONE
                    else 0
                )
            continue
        participants = event.participants.all()
        for physician in participants:
            counts = counts_for(physician)
            counts["events"] += 1
            counts["event_days"] += included_days
            counts["affected_shifts"] += affected_shifts

    for role in faculty_roles:
        counts_for(role.physician)

    findings = ()
    if parameters.includes_scheduled:
        scheduled_start = max(parameters.effective_start, parameters.today)
        scheduled_events = tuple(
            event
            for event in events
            if any(
                end >= scheduled_start and start <= parameters.effective_end
                for start, end in event.date_ranges
            )
        )
        findings = _academic_event_finding_summaries(
            scheduled_events, scheduled_start, parameters.effective_end
        )
    return AcademicEventScheduleReport(
        parameters=parameters,
        rows=tuple(AcademicEventReportRow(**value) for value in _sorted_report_rows(rows.values())),
        findings=findings,
    )


def _academic_event_days_in(event, start_date, end_date):
    return sum(
        (min(end, end_date) - max(start, start_date)).days + 1
        for start, end in event.date_ranges
        if start <= end_date and end >= start_date
    )


def _faculty_event_days(event, start_date, end_date, faculty_roles):
    days_by_physician = {}
    for range_start, range_end in event.date_ranges:
        effective_start = max(range_start, start_date)
        effective_end = min(range_end, end_date)
        if effective_start > effective_end:
            continue
        for role in faculty_roles:
            role_start = max(role.effective_start, effective_start)
            role_end = min(role.effective_end or effective_end, effective_end)
            if role_start > role_end:
                continue
            days_by_physician[role.physician] = (
                days_by_physician.get(role.physician, 0) + (role_end - role_start).days + 1
            )
    return days_by_physician


def interview_schedule_report(parameters):
    """Aggregate current Interview assignments and scheduled-window findings."""
    if parameters.schedule != "interviews":
        raise ValidationError("This schedule report is not available yet.")
    shifts = tuple(
        InterviewShift.objects.filter(
            is_active=True,
            date__range=(parameters.effective_start, parameters.effective_end),
        )
        .select_related("physician")
        .order_by("date", "pk")
    )
    rows = {}

    def counts_for(physician):
        return rows.setdefault(physician.pk, {"physician": physician, "shifts": 0})

    for shift in shifts:
        if shift.physician_id:
            counts_for(shift.physician)["shifts"] += 1
    for physician in _faculty_physicians_for(parameters):
        counts_for(physician)

    findings = ()
    if parameters.includes_scheduled:
        scheduled_shifts = tuple(shift for shift in shifts if shift.date >= parameters.today)
        findings = _interview_finding_summaries(scheduled_shifts)
    return InterviewScheduleReport(
        parameters=parameters,
        rows=tuple(InterviewReportRow(**value) for value in _sorted_report_rows(rows.values())),
        findings=findings,
    )


def normal_rounding_schedule_report(parameters):
    """Count Normal Rounder assignments by their authoritative Monday anchors."""
    if parameters.schedule != "normal-rounding":
        raise ValidationError("This schedule report is not available yet.")
    assignments = tuple(
        NormalRounder.objects.filter(
            week__range=(parameters.effective_start, parameters.effective_end)
        )
        .select_related("physician")
        .order_by("week", "pk")
    )
    rows = {}

    def counts_for(physician):
        return rows.setdefault(physician.pk, {"physician": physician, "weeks": 0})

    for assignment in assignments:
        counts_for(assignment.physician)["weeks"] += 1
    weeks = _mondays_between(parameters.effective_start, parameters.effective_end)
    for physician in normal_rounders_for_weeks(weeks):
        counts_for(physician)

    findings = ()
    if parameters.includes_scheduled:
        scheduled_start = max(parameters.effective_start, parameters.today)
        findings = _normal_rounding_finding_summaries(
            _mondays_between(scheduled_start, parameters.effective_end)
        )
    return NormalRoundingScheduleReport(
        parameters=parameters,
        rows=tuple(
            NormalRoundingReportRow(**value) for value in _sorted_report_rows(rows.values())
        ),
        findings=findings,
    )


def casual_rounding_schedule_report(parameters):
    """Expand current Casual Rounder rules without materializing their occurrences."""
    if parameters.schedule != "casual-rounding":
        raise ValidationError("This schedule report is not available yet.")
    rules = tuple(
        CasualRounderRule.objects.filter(
            Q(effective_start__isnull=True) | Q(effective_start__lte=parameters.effective_end)
        )
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=parameters.effective_start))
        .select_related("physician")
    )
    rows = {}

    def counts_for(physician):
        return rows.setdefault(physician.pk, {"physician": physician, "days": 0})

    occurrences = set()
    on_date = parameters.effective_start
    while on_date <= parameters.effective_end:
        for rule in rules:
            if rule.matches(on_date):
                occurrences.add((rule.physician_id, on_date))
        on_date += timedelta(days=1)
    physicians = Physician.objects.in_bulk(physician_id for physician_id, _ in occurrences)
    for physician_id, _ in occurrences:
        counts_for(physicians[physician_id])["days"] += 1
    eligible_ids = _role_eligible_ids(
        EffectiveRole.Role.CASUAL_ROUNDER,
        parameters.effective_start,
        parameters.effective_end,
    )
    for physician in Physician.objects.filter(pk__in=eligible_ids).order_by("display_name", "pk"):
        counts_for(physician)
    findings = ()
    if parameters.includes_scheduled:
        findings = _casual_rounding_finding_summaries(
            occurrences,
            max(parameters.effective_start, parameters.today),
            parameters.effective_end,
        )
    return CasualRoundingScheduleReport(
        parameters=parameters,
        rows=tuple(
            CasualRoundingReportRow(**value) for value in _sorted_report_rows(rows.values())
        ),
        findings=findings,
    )


def vacation_schedule_report(parameters):
    """Count current Vacation ranges and their distinct overlapping calendar days."""
    if parameters.schedule != "vacation":
        raise ValidationError("This schedule report is not available yet.")
    vacations = tuple(
        Vacation.objects.overlapping(parameters.effective_start, parameters.effective_end)
        .select_related("physician")
        .order_by("start_date", "end_date", "pk")
    )
    rows = {}

    def counts_for(physician):
        return rows.setdefault(physician.pk, {"physician": physician, "ranges": 0, "days": set()})

    for vacation in vacations:
        counts = counts_for(vacation.physician)
        counts["ranges"] += 1
        first_day = max(vacation.start_date, parameters.effective_start)
        last_day = min(vacation.end_date, parameters.effective_end)
        while first_day <= last_day:
            counts["days"].add(first_day)
            first_day += timedelta(days=1)

    eligible_ids = set(
        EffectiveRole.objects.filter(effective_start__lte=parameters.effective_end)
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=parameters.effective_start))
        .values_list("physician_id", flat=True)
    )
    for physician in Physician.objects.filter(pk__in=eligible_ids).order_by("display_name", "pk"):
        counts_for(physician)

    findings = ()
    if parameters.includes_scheduled:
        scheduled_start = max(parameters.effective_start, parameters.today)
        findings = _vacation_finding_summaries(vacations, scheduled_start, parameters.effective_end)

    report_rows = []
    for value in _sorted_report_rows(rows.values()):
        weekdays = sum(day.weekday() < 5 for day in value["days"])
        total_days = len(value["days"])
        report_rows.append(
            VacationReportRow(
                physician=value["physician"],
                ranges=value["ranges"],
                weekdays=weekdays,
                weekend_days=total_days - weekdays,
                total_days=total_days,
            )
        )
    return VacationScheduleReport(
        parameters=parameters,
        rows=tuple(report_rows),
        findings=findings,
    )


def _precepting_schedule_projection(parameters):
    assignments = tuple(
        PreceptorAssignment.objects.filter(
            session__date__range=(parameters.effective_start, parameters.effective_end)
        )
        .select_related("physician", "session")
        .order_by("session__date", "session__period_of_day", "pk")
    )
    late_docs = tuple(
        LateDocAssignment.objects.filter(
            date__range=(parameters.effective_start, parameters.effective_end)
        ).select_related("physician")
    )
    rows = {}

    def counts_for(physician):
        return rows.setdefault(
            physician.pk,
            {"physician": physician, "am": 0, "pm": 0, "primary": 0, "secondary": 0, "late_doc": 0},
        )

    for assignment in assignments:
        counts = counts_for(assignment.physician)
        counts[assignment.session.period_of_day.lower()] += 1
        counts[assignment.responsibility] += 1
    for assignment in late_docs:
        counts_for(assignment.physician)["late_doc"] += 1

    sessions = tuple(
        PreceptorSession.objects.filter(
            date__range=(parameters.effective_start, parameters.effective_end)
        ).order_by("date", "period_of_day", "pk")
    )
    inferred, ambiguous, community_assignments = _community_fallback(sessions, assignments)
    for physician, period in inferred:
        counts = counts_for(physician)
        counts[period.lower()] += 1
        counts["primary"] += 1

    eligible_ids = _role_eligible_ids(
        (EffectiveRole.Role.FACULTY, EffectiveRole.Role.COMMUNITY_PRECEPTOR),
        parameters.effective_start,
        parameters.effective_end,
    )
    for physician in Physician.objects.filter(pk__in=eligible_ids).order_by("display_name", "pk"):
        counts_for(physician)

    report_rows = tuple(
        PreceptingReportRow(
            physician=value["physician"],
            am=value["am"],
            pm=value["pm"],
            primary=value["primary"],
            secondary=value["secondary"],
            precepting_total=value["primary"] + value["secondary"],
            late_doc=value["late_doc"],
        )
        for value in sorted(
            rows.values(),
            key=lambda value: (value["physician"].display_name.casefold(), value["physician"].pk),
        )
    )
    findings = ()
    if parameters.includes_scheduled:
        findings = _finding_summaries(
            tuple(session for session in sessions if session.date >= parameters.today),
            tuple(assignment for assignment in late_docs if assignment.date >= parameters.today),
            tuple(occurrence for occurrence in ambiguous if occurrence.date >= parameters.today),
            _community_role_mismatch_count(
                tuple(
                    assignment
                    for assignment in community_assignments
                    if assignment.session.date >= parameters.today
                )
            ),
            max(parameters.effective_start, parameters.today),
            parameters.effective_end,
        )
    return (
        PreceptingScheduleReport(parameters=parameters, rows=report_rows, findings=findings),
        _community_activity_physician_ids(community_assignments, inferred),
    )


def _role_eligible_ids(roles, start_date, end_date):
    if isinstance(roles, str):
        roles = (roles,)
    return set(
        EffectiveRole.objects.filter(
            role__in=roles,
            effective_start__lte=end_date,
        )
        .filter(Q(effective_end__isnull=True) | Q(effective_end__gte=start_date))
        .values_list("physician_id", flat=True)
    )


def _faculty_physicians_for(parameters):
    eligible_ids = _role_eligible_ids(
        EffectiveRole.Role.FACULTY,
        parameters.effective_start,
        parameters.effective_end,
    )
    return Physician.objects.filter(pk__in=eligible_ids).order_by("display_name", "pk")


def _sorted_report_rows(rows):
    return sorted(
        rows,
        key=lambda value: (value["physician"].display_name.casefold(), value["physician"].pk),
    )


def _academic_event_finding_summaries(events, start_date, end_date):
    findings = []
    physician_ids = {
        participant.pk for event in events for participant in _academic_event_participants(event)
    }
    roles_by_physician = {}
    for role in EffectiveRole.objects.filter(
        physician_id__in=physician_ids,
        role=EffectiveRole.Role.FACULTY,
    ):
        roles_by_physician.setdefault(role.physician_id, []).append(role)
    for event in events:
        if event.precepting_impact == AcademicEvent.PreceptingImpact.NONE:
            continue
        if event.all_available_faculty:
            continue
        participants = _academic_event_participants(event)
        if not participants:
            findings.append(("unpopulated_academic_event", "hard"))
        elif len(participants) < event.required_participant_count:
            findings.append(("underfilled_academic_event", "hard"))
        event_start = min(
            max(start, start_date) for start, end in event.date_ranges if end >= start_date
        )
        event_end = max(min(end, end_date) for start, end in event.date_ranges if start <= end_date)
        for physician in participants:
            if not any(
                role.effective_start <= event_end
                and (role.effective_end is None or role.effective_end >= event_start)
                for role in roles_by_physician.get(physician.pk, ())
            ):
                findings.append(("role_ineligible_academic_event_participant", "hard"))
    return _report_finding_summaries(
        findings,
        {
            "unpopulated_academic_event": "Unpopulated Academic Event",
            "underfilled_academic_event": "Underfilled Academic Event",
            "role_ineligible_academic_event_participant": "Academic Event role mismatch",
        },
    )


def _academic_event_participants(event):
    return tuple(event.participants.all())


def _interview_finding_summaries(shifts):
    findings = []
    for shift in shifts:
        shift_findings = interview_findings_for_shift(shift).findings
        if shift_findings:
            findings.extend((finding.code, finding.severity) for finding in shift_findings)
        elif not shift.physician_id:
            findings.append(("unassigned_interview_shift", "hard"))
    return _report_finding_summaries(
        findings,
        {
            "unassigned_interview_shift": "Interview assignment gap",
            "role_ineligible_assignment": "Interview role mismatch",
            "not_available_assignment": "Interview unavailable",
            "backup_only_assignment": "Interview backup only",
            "preceptor_assignment_conflict": "Interview Preceptor conflict",
            "holiday_closed_interview": "Interview scheduled during clinic holiday closure",
        },
    )


def _normal_rounding_finding_summaries(weeks):
    findings = []
    for status in normal_rounder_statuses_for_weeks(weeks):
        if status.normal_rounder is None:
            findings.append(("missing_normal_rounder", "hard"))
        elif status.inconsistent:
            findings.append(("role_ineligible_normal_rounder", "hard"))
    return _report_finding_summaries(
        findings,
        {
            "missing_normal_rounder": "Missing Normal Rounder",
            "role_ineligible_normal_rounder": "Normal Rounder eligibility mismatch",
        },
    )


def _casual_rounding_finding_summaries(occurrences, start_date, end_date):
    scheduled_occurrences = {
        (physician_id, on_date)
        for physician_id, on_date in occurrences
        if start_date <= on_date <= end_date
    }
    physician_ids = {physician_id for physician_id, _ in scheduled_occurrences}
    roles_by_physician = {physician_id: [] for physician_id in physician_ids}
    for role in EffectiveRole.objects.filter(
        physician_id__in=physician_ids,
        role=EffectiveRole.Role.CASUAL_ROUNDER,
        effective_start__lte=end_date,
    ).filter(Q(effective_end__isnull=True) | Q(effective_end__gte=start_date)):
        roles_by_physician[role.physician_id].append(role)
    findings = [
        ("role_ineligible_casual_rounder", "hard")
        for physician_id, on_date in scheduled_occurrences
        if not any(
            role.effective_start <= on_date
            and (role.effective_end is None or role.effective_end >= on_date)
            for role in roles_by_physician[physician_id]
        )
    ]
    return _report_finding_summaries(
        findings,
        {"role_ineligible_casual_rounder": "Casual Rounder role mismatch"},
    )


def _vacation_finding_summaries(vacations, start_date, end_date):
    scheduled_vacations = tuple(
        vacation
        for vacation in vacations
        if vacation.end_date >= start_date and vacation.start_date <= end_date
    )
    physician_ids = {vacation.physician_id for vacation in scheduled_vacations}
    roles_by_physician = {}
    for role in EffectiveRole.objects.filter(physician_id__in=physician_ids):
        roles_by_physician.setdefault(role.physician_id, []).append(role)
    findings = [
        ("role_ineligible_vacation", "hard")
        for vacation in scheduled_vacations
        if not any(
            role.effective_start <= min(vacation.end_date, end_date)
            and (
                role.effective_end is None
                or role.effective_end >= max(vacation.start_date, start_date)
            )
            for role in roles_by_physician.get(vacation.physician_id, ())
        )
    ]
    return _report_finding_summaries(
        findings,
        {"role_ineligible_vacation": "Vacation role mismatch"},
    )


def _report_finding_summaries(findings, labels):
    totals = Counter(findings)
    return tuple(
        ReportFindingSummary(
            code, labels.get(code, code.replace("_", " ").capitalize()), severity, count
        )
        for (code, severity), count in sorted(totals.items())
    )


def _community_activity_physician_ids(community_assignments, inferred):
    activity_ids = {assignment.physician_id for assignment in community_assignments}
    activity_ids.update(physician.pk for physician, _ in inferred)
    return activity_ids


def _community_role_mismatch_count(community_assignments):
    physician_ids = {assignment.physician_id for assignment in community_assignments}
    roles_by_physician = {}
    for role in EffectiveRole.objects.filter(
        physician_id__in=physician_ids,
        role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
    ):
        roles_by_physician.setdefault(role.physician_id, []).append(role)
    return sum(
        not any(
            role.effective_start <= assignment.session.date
            and (role.effective_end is None or assignment.session.date <= role.effective_end)
            for role in roles_by_physician.get(assignment.physician_id, ())
        )
        for assignment in community_assignments
    )


def _community_fallback(sessions, assignments):
    assignments_by_slot = {}
    for assignment in assignments:
        assignments_by_slot.setdefault(
            (assignment.session.date, assignment.session.period_of_day), []
        ).append(assignment)
    sessions_by_slot = {(session.date, session.period_of_day): session for session in sessions}
    inferred = []
    ambiguous = []
    expected = (
        community_preceptor_occurrences(
            min((session.date for session in sessions), default=None),
            max((session.date for session in sessions), default=None),
        )
        if sessions
        else ()
    )
    expected_by_slot = {}
    for occurrence in expected:
        if (occurrence.date, occurrence.period) in sessions_by_slot:
            expected_by_slot.setdefault((occurrence.date, occurrence.period), []).append(occurrence)
    community_assignment_ids = set()
    for slot, occurrences in expected_by_slot.items():
        actual = assignments_by_slot.get(slot, ())
        expected_physician_ids = {occurrence.physician_id for occurrence in occurrences}
        community_assignment_ids.update(
            assignment.pk
            for assignment in actual
            if assignment.physician_id in expected_physician_ids
        )
        actual_physician_ids = {assignment.physician_id for assignment in actual}
        unmatched = []
        seen = set()
        for occurrence in occurrences:
            key = (occurrence.physician_id, occurrence.date, occurrence.period)
            if occurrence.physician_id not in actual_physician_ids and key not in seen:
                unmatched.append(occurrence)
                seen.add(key)
        open_positions = {
            1,
            2,
        } - {
            assignment.primary_position
            for assignment in actual
            if assignment.responsibility == PreceptorAssignment.Responsibility.PRIMARY
        }
        if len(unmatched) > len(open_positions):
            ambiguous.extend(unmatched)
            continue
        for occurrence in unmatched:
            inferred.append((occurrence, occurrence.period))
    physicians = Physician.objects.in_bulk({occurrence.physician_id for occurrence, _ in inferred})
    return (
        tuple((physicians[occurrence.physician_id], period) for occurrence, period in inferred),
        tuple(ambiguous),
        tuple(
            assignment for assignment in assignments if assignment.pk in community_assignment_ids
        ),
    )


def _finding_summaries(
    sessions, late_docs, ambiguous, community_role_mismatch_count, start_date, end_date
):
    findings = []
    for session in sessions:
        findings.extend(
            finding
            for finding in draft_findings_for_session(session).findings
            if finding.code != "missing_normal_rounder"
        )
    late_docs_by_date = {assignment.date: assignment for assignment in late_docs}
    late_doc_rows = {
        (row.slot.on_date, row.physician.pk): row
        for row in slot_projection(RequestedSlot(on_date, "PM") for on_date in late_docs_by_date)
    }
    late_doc_session_dates = {
        session.date
        for session in sessions
        if session.period_of_day == PreceptorSession.PeriodOfDay.PM
    }
    for day in _mondays_between(start_date, end_date):
        if holiday_closure_for(day, "PM"):
            if late_docs_by_date.get(day) is not None:
                findings.append(("holiday_closed_late_doc_assignment", "hard"))
            continue
        assignment = late_docs_by_date.get(day)
        if assignment is None:
            findings.append(("missing_late_doc", "hard"))
        elif not is_manually_eligible_for_position(
            "late_doc", late_doc_rows[(day, assignment.physician_id)].tier_zero
        ):
            findings.append(("role_ineligible_late_doc", "hard"))
        if day in late_doc_session_dates:
            findings.extend(
                finding
                for finding in draft_findings_for_week(day).findings
                if finding.code
                in {
                    "not_available_late_doc",
                    "backup_only_late_doc",
                    "soft_academic_event_late_doc",
                }
            )
    findings.extend(("ambiguous_community_expectation", "hard") for _ in ambiguous)
    findings.extend(
        ("role_ineligible_community_preceptor", "hard")
        for _ in range(community_role_mismatch_count)
    )
    totals = Counter(
        (item[0], item[1]) if isinstance(item, tuple) else (item.code, item.severity)
        for item in findings
    )
    labels = {
        "ambiguous_community_expectation": "Ambiguous Community expectation",
        "role_ineligible_community_preceptor": "Community Preceptor role mismatch",
    }
    return tuple(
        ReportFindingSummary(
            code,
            labels[code] if code in labels else draft_finding_label(code),
            severity,
            count,
        )
        for (code, severity), count in sorted(totals.items())
    )


def _mondays_between(start_date, end_date):
    if not start_date or not end_date:
        return ()
    first = start_date + timedelta(days=(7 - start_date.weekday()) % 7)
    return tuple(
        first + timedelta(days=7 * index) for index in range((end_date - first).days // 7 + 1)
    )


def _year(value, label, default, *, minimum, maximum):
    if value in (None, ""):
        return default
    try:
        year = int(value)
    except TypeError, ValueError:
        raise ValidationError(f"{label} must be a four-digit year.") from None
    if not minimum <= year <= maximum:
        raise ValidationError(f"{label} must be between {minimum} and {maximum}.")
    return year


def _available_coverage(coverage, start_date, end_date, today):
    """Keep stale coverage links within the range's only possible coverage."""
    if end_date < today:
        return "history"
    if start_date >= today:
        return "scheduled"
    return coverage


def _date(value, label):
    try:
        return date.fromisoformat(value)
    except TypeError, ValueError:
        raise ValidationError(f"{label} must use YYYY-MM-DD.") from None


__all__ = (
    "COVERAGE_CHOICES",
    "EARLIEST_REPORT_YEAR",
    "LATEST_ACADEMIC_YEAR",
    "LATEST_CALENDAR_YEAR",
    "PERIOD_CHOICES",
    "SCHEDULE_CHOICES",
    "AcademicEventReportRow",
    "AcademicEventScheduleReport",
    "CasualRoundingReportRow",
    "CasualRoundingScheduleReport",
    "InterviewReportRow",
    "InterviewScheduleReport",
    "NormalRoundingReportRow",
    "NormalRoundingScheduleReport",
    "PreceptingReportRow",
    "PreceptingScheduleReport",
    "ReportFindingSummary",
    "ScheduleReportParameters",
    "VacationReportRow",
    "VacationScheduleReport",
    "academic_event_schedule_report",
    "casual_rounding_schedule_report",
    "community_precepting_schedule_report",
    "interview_schedule_report",
    "normal_rounding_schedule_report",
    "precepting_schedule_report",
    "schedule_report_parameters",
    "vacation_schedule_report",
)
