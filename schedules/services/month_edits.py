"""Bounded, ephemeral comparisons; only the last qualifying edit time is retained."""

import calendar
from collections import Counter
from datetime import timedelta

from django.db import transaction

from ..models.late_docs import LateDocAssignment
from ..models.preceptors import PreceptorAssignment, SchedulePeriod
from ..precepting_content import assigned_identity, finding_content
from .finding_acknowledgements import readiness_for_findings
from .holidays import holiday_projections_for_dates
from .operational_attention import organization_today
from .preceptors import focused_draft_findings_for_range, projected_draft_findings_for_weeks
from .work_remaining import InclusiveDateRange, shift_date_months


def month_calendar_content(month):
    """Project actual month dates with the calendar/editor's ordinary finding policy."""
    # The existing Academic Event reader materializes recurrence occurrences. Keep
    # that supported reader's policy, but confine its writes to a rolled-back
    # savepoint: observing a comparison must not initialize source rows or histories.
    # Everything crossing this adapter is immutable content, never model instances.
    with transaction.atomic():
        content = _month_calendar_content(month)
        transaction.set_rollback(True)
    return content


def _month_calendar_content(month):
    end = shift_date_months(month, 1) - timedelta(days=1)
    weeks = tuple(
        dates[0]
        for dates in calendar.Calendar().monthdatescalendar(month.year, month.month)
        if any(month <= day <= end for day in dates[:5])
    )
    assignments = frozenset(
        (
            assignment.session.date,
            assignment.session.period_of_day,
            assignment.responsibility,
            assignment.primary_position,
            assigned_identity(assignment),
        )
        for assignment in PreceptorAssignment.objects.filter(
            session__date__range=(month, end)
        ).select_related("session", "physician")
    )
    late_docs = frozenset(
        (assignment.date, assigned_identity(assignment))
        for assignment in LateDocAssignment.objects.filter(date__range=(month, end)).select_related(
            "physician"
        )
    )
    days = tuple(
        day
        for week in weeks
        for offset in range(5)
        if month <= (day := week + timedelta(days=offset)) <= end
    )
    closures = frozenset(
        (closure.observed_date, period, closure.name, closure.coverage_label)
        for closure in holiday_projections_for_dates(days)
        for period in ("AM", "PM")
        if closure.closes(period)
    )
    if end < organization_today():
        return assignments, late_docs, closures, frozenset()
    findings = focused_draft_findings_for_range(
        projected_draft_findings_for_weeks(weeks, complete_calendar=True),
        InclusiveDateRange(month, end),
    )
    details = Counter(
        (
            (
                entry.finding.session.date,
                entry.finding.session.period_of_day,
                entry.finding.explanation.get("position"),
            )
            if entry.finding.session is not None
            else (entry.finding.week, "week", None),
            finding_content(entry.finding),
            bool(entry.acknowledgement),
        )
        for entry in readiness_for_findings(findings.findings).entries
    )
    return assignments, late_docs, closures, frozenset(details.items())


class MonthEditRecorder:
    def __init__(self, recorded_at):
        self.recorded_at = recorded_at
        self.before = {}
        month = organization_today().replace(day=1)
        while self._has_assignments(month):
            self.capture_month(month)
            month = shift_date_months(month, 1)

    @staticmethod
    def _has_assignments(month):
        end = shift_date_months(month, 1)
        return (
            PreceptorAssignment.objects.filter(
                session__date__gte=month, session__date__lt=end
            ).exists()
            or LateDocAssignment.objects.filter(date__gte=month, date__lt=end).exists()
        )

    def capture_month(self, month):
        if month not in self.before:
            self.before[month] = month_calendar_content(month)

    def capture_range(self, start, end):
        """Direct dated ownership, including both sides of moves and historical corrections."""
        if start is None or end is None:
            return
        month = start.replace(day=1)
        while month <= end:
            self.capture_month(month)
            month = shift_date_months(month, 1)

    def record(self):
        for month, before in self.before.items():
            if before != month_calendar_content(month):
                period, _ = SchedulePeriod.objects.get_or_create(month=month)
                SchedulePeriod.objects.filter(pk=period.pk).update(last_edited_at=self.recorded_at)
