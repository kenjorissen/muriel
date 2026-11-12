import json
import subprocess
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from pypdf import PdfReader

from schedules.academic_years import academic_year_for_date
from schedules.models import (
    AcademicEvent,
    AcademicEventDefinition,
    AcademicEventMonthlyRule,
    CasualRounderRule,
    CommunityPreceptorRule,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    InterviewShift,
    LateDocAssignment,
    NormalRounder,
    Physician,
    PreceptorAssignment,
    PreceptorSession,
    Vacation,
)
from schedules.pdf import build_schedule_report_pdf
from schedules.services.operational_attention import organization_today
from schedules.services.preceptors import open_monthly_draft
from schedules.services.schedule_reports import (
    COVERAGE_CHOICES,
    PreceptingReportRow,
    PreceptingScheduleReport,
    ReportFindingSummary,
    ScheduleReportParameters,
    academic_event_schedule_report,
    casual_rounding_schedule_report,
    community_precepting_schedule_report,
    interview_schedule_report,
    normal_rounding_schedule_report,
    precepting_schedule_report,
    schedule_report_parameters,
    vacation_schedule_report,
)
from schedules.tests.browser_tools import require_browser_tools
from schedules.tests.test_precepting_calendar_pdf import (
    _grid_cell_at,
    _pdf_artifact,
    _single_location,
)
from schedules.tests.test_table_presentation import _RenderedDataTable
from schedules.views.reports import _schedule_report_year_url

_REPORT_MONDAY = date(2025, 2, 3)


def _workload_row(table, physician_name):
    """Return one rendered report row selected through the Physician field."""
    matches = [
        row
        for row in table.body_rows()
        if table.cell_for_header(row, "Physician").text() == physician_name
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"Expected one rendered Physician row for {physician_name!r}; found {len(matches)}."
        )
    return matches[0]


def _workload_values(content, physician_name, field_labels):
    table = _RenderedDataTable(content, identity="schedule-report-workload")
    row = _workload_row(table, physician_name)
    return tuple(table.cell_for_header(row, field_label).text() for field_label in field_labels)


class ScheduleReportServiceTests(TestCase):
    def physician(self, name, role=None, start=date(2025, 1, 1), end=None):
        physician = Physician.objects.create(display_name=name)
        if role:
            EffectiveRole.objects.create(
                physician=physician, role=role, effective_start=start, effective_end=end
            )
        return physician

    def session(self, on_date, period):
        open_monthly_draft(on_date.replace(day=1))
        return PreceptorSession.objects.get(date=on_date, period_of_day=period)

    def availability(self, physician, state):
        FacultyAvailabilityTemplate.objects.create(
            physician=physician,
            slots={
                slot: FacultyAvailabilityTemplate.base_selection(state)
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
        )

    def academic_event(
        self,
        name,
        start_date,
        end_date,
        period,
        participants=(),
        required=1,
        impact=AcademicEvent.PreceptingImpact.HARD,
    ):
        definition = AcademicEventDefinition.objects.create(
            base_name=name,
            start_academic_year=academic_year_for_date(start_date),
        )
        event = AcademicEvent.objects.create(
            definition=definition,
            academic_year=academic_year_for_date(start_date),
            base_name=name,
            required_participant_count=required,
            resolution=AcademicEvent.Resolution.DATED,
            start_date=start_date,
            end_date=end_date,
            period_of_day=period,
            precepting_impact=impact,
        )
        event.participants.set(participants)
        return event

    def parameters(self, *, today=date(2025, 2, 3), **values):
        return schedule_report_parameters(
            {
                "period": "custom",
                "start_date": "2025-02-03",
                "end_date": "2025-02-03",
                "coverage": "history-and-scheduled",
                **values,
            },
            today=today,
        )

    def test_period_edges_and_coverage_are_inclusive(self):
        academic = schedule_report_parameters(
            {"period": "academic-year", "academic_year": "2024"}, today=date(2025, 2, 3)
        )
        calendar = schedule_report_parameters(
            {"period": "calendar-year", "calendar_year": "2025"}, today=date(2025, 2, 3)
        )
        history = schedule_report_parameters(
            {
                "period": "custom",
                "start_date": "2025-02-02",
                "end_date": "2025-02-03",
                "coverage": "history",
            },
            today=date(2025, 2, 3),
        )

        self.assertEqual(
            (academic.start_date, academic.end_date), (date(2024, 7, 1), date(2025, 6, 30))
        )
        self.assertEqual(
            (calendar.start_date, calendar.end_date), (date(2025, 1, 1), date(2025, 12, 31))
        )
        self.assertEqual(
            (history.effective_start, history.effective_end), (date(2025, 2, 2), date(2025, 2, 2))
        )
        scheduled = schedule_report_parameters(
            {
                "period": "custom",
                "start_date": "2025-02-02",
                "end_date": "2025-02-04",
                "coverage": "scheduled",
            },
            today=date(2025, 2, 3),
        )
        self.assertEqual(
            (scheduled.effective_start, scheduled.effective_end),
            (date(2025, 2, 3), date(2025, 2, 4)),
        )

    def test_coverage_normalizes_to_a_wholly_historical_or_scheduled_range(self):
        historical = schedule_report_parameters(
            {
                "period": "custom",
                "start_date": "2025-01-01",
                "end_date": "2025-02-02",
                "coverage": "scheduled",
            },
            today=date(2025, 2, 3),
        )
        scheduled = schedule_report_parameters(
            {
                "period": "custom",
                "start_date": "2025-02-03",
                "end_date": "2025-03-01",
                "coverage": "history-and-scheduled",
            },
            today=date(2025, 2, 3),
        )
        spanning = schedule_report_parameters(
            {
                "period": "custom",
                "start_date": "2025-02-02",
                "end_date": "2025-02-03",
                "coverage": "history-and-scheduled",
            },
            today=date(2025, 2, 3),
        )

        self.assertEqual(
            (historical.coverage, historical.effective_end), ("history", date(2025, 2, 2))
        )
        self.assertEqual(
            (scheduled.coverage, scheduled.effective_start), ("scheduled", date(2025, 2, 3))
        )
        self.assertEqual(spanning.coverage, "history-and-scheduled")

    def test_coverage_choices_present_combined_between_history_and_scheduled(self):
        self.assertEqual(
            COVERAGE_CHOICES,
            (
                ("history", "History"),
                ("history-and-scheduled", "History + scheduled"),
                ("scheduled", "Scheduled"),
            ),
        )

    def test_navigation_target_uses_combined_coverage_when_it_spans_today(self):
        parameters = schedule_report_parameters(
            {
                "period": "custom",
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
                "coverage": "history",
            },
            today=date(2025, 2, 3),
        )

        url = _schedule_report_year_url(parameters, "calendar-year", 2025)

        self.assertIn("coverage=history-and-scheduled", url)

    def test_annual_periods_use_the_full_calendar_safe_range(self):
        today = date(2025, 2, 3)
        earliest_academic = schedule_report_parameters(
            {"period": "academic-year", "academic_year": "1"}, today=today
        )
        latest_academic = schedule_report_parameters(
            {"period": "academic-year", "academic_year": "9998"}, today=today
        )
        earliest_calendar = schedule_report_parameters(
            {"period": "calendar-year", "calendar_year": "1"}, today=today
        )
        latest_calendar = schedule_report_parameters(
            {"period": "calendar-year", "calendar_year": "9999"}, today=today
        )

        self.assertEqual(
            (earliest_academic.start_date, earliest_academic.end_date),
            (date(1, 7, 1), date(2, 6, 30)),
        )
        self.assertEqual(
            (latest_academic.start_date, latest_academic.end_date),
            (date(9998, 7, 1), date(9999, 6, 30)),
        )
        self.assertEqual(
            (earliest_calendar.start_date, earliest_calendar.end_date),
            (date(1, 1, 1), date(1, 12, 31)),
        )
        self.assertEqual(
            (latest_calendar.start_date, latest_calendar.end_date),
            (date(9999, 1, 1), date(9999, 12, 31)),
        )
        with self.assertRaisesMessage(ValidationError, "Academic year must be between 1 and 9998."):
            schedule_report_parameters(
                {"period": "academic-year", "academic_year": "9999"}, today=today
            )
        with self.assertRaisesMessage(ValidationError, "Calendar year must be between 1 and 9999."):
            schedule_report_parameters(
                {"period": "calendar-year", "calendar_year": "10000"}, today=today
            )

    def test_actual_facts_and_role_eligible_zero_rows_are_retained(self):
        eligible = self.physician("Eligible", EffectiveRole.Role.FACULTY)
        inconsistent = self.physician("Recorded despite role")
        session = self.session(date(2025, 2, 3), "AM")
        PreceptorAssignment.objects.create(
            session=session,
            physician=inconsistent,
            responsibility=PreceptorAssignment.Responsibility.SECONDARY,
        )
        LateDocAssignment.objects.create(date=date(2025, 2, 3), physician=inconsistent)

        report = precepting_schedule_report(self.parameters())

        self.assertEqual([row.physician for row in report.rows], [eligible, inconsistent])
        recorded = report.rows[1]
        self.assertEqual(
            (recorded.am, recorded.pm, recorded.primary, recorded.secondary), (1, 0, 0, 1)
        )
        self.assertEqual((recorded.precepting_total, recorded.late_doc), (1, 1))
        self.assertEqual((report.rows[0].precepting_total, report.rows[0].late_doc), (0, 0))
        self.assertIn("role_ineligible_assignment", [finding.code for finding in report.findings])

    def test_community_commitment_fills_one_open_primary_without_persisting(self):
        faculty = self.physician("Faculty", EffectiveRole.Role.FACULTY)
        community = self.physician("Community", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        session = self.session(date(2025, 2, 3), "AM")
        PreceptorAssignment.objects.create(
            session=session,
            physician=faculty,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )
        CommunityPreceptorRule.objects.create(
            physician=community,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            effective_start=date(2025, 1, 1),
        )

        report = precepting_schedule_report(self.parameters())

        community_row = next(row for row in report.rows if row.physician == community)
        self.assertEqual(
            (community_row.am, community_row.primary, community_row.precepting_total), (1, 1, 1)
        )
        self.assertEqual(PreceptorAssignment.objects.filter(session=session).count(), 1)

    def test_community_report_filters_shared_projection_and_retains_zero_rows(self):
        faculty = self.physician("Faculty", EffectiveRole.Role.FACULTY)
        community = self.physician("Community", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        zero = self.physician("Zero", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        session = self.session(date(2025, 2, 3), "AM")
        PreceptorAssignment.objects.create(
            session=session,
            physician=faculty,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )
        PreceptorAssignment.objects.create(
            session=session,
            physician=community,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=2,
        )
        CommunityPreceptorRule.objects.create(
            physician=community,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            effective_start=date(2025, 1, 1),
        )

        precepting = precepting_schedule_report(self.parameters())
        community_report = community_precepting_schedule_report(
            self.parameters(schedule="community-precepting")
        )

        self.assertEqual([row.physician for row in community_report.rows], [community, zero])
        community_row = community_report.rows[0]
        self.assertEqual(
            (community_row.am, community_row.pm, community_row.precepting_total), (1, 0, 1)
        )
        self.assertEqual(
            community_row.precepting_total,
            next(row for row in precepting.rows if row.physician == community).precepting_total,
        )
        self.assertEqual(PreceptorAssignment.objects.filter(session=session).count(), 2)

    def test_community_report_retains_actual_activity_with_missing_or_expired_roles(self):
        absent = self.physician("Absent", EffectiveRole.Role.FACULTY)
        expired = self.physician("Expired", EffectiveRole.Role.FACULTY)
        EffectiveRole.objects.create(
            physician=expired,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 2, 2),
        )
        session = self.session(date(2025, 2, 3), "AM")
        for physician, position in ((absent, 1), (expired, 2)):
            CommunityPreceptorRule.objects.create(
                physician=physician,
                weekday=0,
                ordinals=[1],
                periods=["AM"],
                effective_start=date(2025, 1, 1),
            )
            PreceptorAssignment.objects.create(
                session=session,
                physician=physician,
                responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                primary_position=position,
            )

        report = community_precepting_schedule_report(
            self.parameters(schedule="community-precepting")
        )

        self.assertEqual([row.physician for row in report.rows], [absent, expired])
        self.assertEqual([row.precepting_total for row in report.rows], [1, 1])
        mismatch = next(
            finding
            for finding in report.findings
            if finding.code == "role_ineligible_community_preceptor"
        )
        self.assertEqual(
            (mismatch.label, mismatch.severity, mismatch.count),
            (
                "Community Preceptor role mismatch",
                "hard",
                2,
            ),
        )

    def test_community_report_includes_open_position_recurrence_fallback(self):
        faculty = self.physician("Faculty", EffectiveRole.Role.FACULTY)
        community = self.physician("Community", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        session = self.session(date(2025, 2, 3), "AM")
        PreceptorAssignment.objects.create(
            session=session,
            physician=faculty,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )
        CommunityPreceptorRule.objects.create(
            physician=community,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            effective_start=date(2025, 1, 1),
        )

        report = community_precepting_schedule_report(
            self.parameters(schedule="community-precepting")
        )

        self.assertEqual([row.physician for row in report.rows], [community])
        self.assertEqual((report.rows[0].am, report.rows[0].precepting_total), (1, 1))
        self.assertEqual(PreceptorAssignment.objects.filter(session=session).count(), 1)

    def test_community_report_deduplicates_recurrence_and_actual_facts(self):
        community = self.physician("Community", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        first_session = self.session(date(2025, 2, 3), "AM")
        second_session = self.session(date(2025, 2, 10), "AM")
        for ordinals in ([1], [1], [2]):
            CommunityPreceptorRule.objects.create(
                physician=community,
                weekday=0,
                ordinals=ordinals,
                periods=["AM"],
                effective_start=date(2025, 1, 1),
            )
        PreceptorAssignment.objects.create(
            session=second_session,
            physician=community,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )

        report = community_precepting_schedule_report(
            self.parameters(schedule="community-precepting", end_date="2025-02-10")
        )

        self.assertEqual([row.physician for row in report.rows], [community])
        self.assertEqual((report.rows[0].am, report.rows[0].precepting_total), (2, 2))
        self.assertEqual(PreceptorAssignment.objects.filter(session=first_session).count(), 0)

    def test_community_report_keeps_recurrence_fallback_and_ambiguity_findings(self):
        faculty = self.physician("Faculty", EffectiveRole.Role.FACULTY)
        first = self.physician("First", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        second = self.physician("Second", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        session = self.session(date(2025, 2, 3), "AM")
        PreceptorAssignment.objects.create(
            session=session,
            physician=faculty,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )
        for physician in (first, second):
            CommunityPreceptorRule.objects.create(
                physician=physician,
                weekday=0,
                ordinals=[1],
                periods=["AM"],
                effective_start=date(2025, 1, 1),
            )

        report = community_precepting_schedule_report(
            self.parameters(schedule="community-precepting")
        )

        self.assertEqual([row.precepting_total for row in report.rows], [0, 0])
        ambiguity = next(
            finding
            for finding in report.findings
            if finding.code == "ambiguous_community_expectation"
        )
        self.assertEqual((ambiguity.severity, ambiguity.count), ("hard", 2))

    def test_ambiguous_community_commitments_are_not_inferred(self):
        faculty = self.physician("Faculty", EffectiveRole.Role.FACULTY)
        first = self.physician("First", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        second = self.physician("Second", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        session = self.session(date(2025, 2, 3), "AM")
        PreceptorAssignment.objects.create(
            session=session,
            physician=faculty,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )
        for physician in (first, second):
            CommunityPreceptorRule.objects.create(
                physician=physician,
                weekday=0,
                ordinals=[1],
                periods=["AM"],
                effective_start=date(2025, 1, 1),
            )

        report = precepting_schedule_report(self.parameters())

        first_row = next(row for row in report.rows if row.physician == first)
        second_row = next(row for row in report.rows if row.physician == second)
        self.assertEqual((first_row.precepting_total, second_row.precepting_total), (0, 0))
        ambiguity = next(
            finding
            for finding in report.findings
            if finding.code == "ambiguous_community_expectation"
        )
        self.assertEqual((ambiguity.severity, ambiguity.count), ("hard", 2))

    def test_scheduled_late_doc_availability_findings_are_summarized(self):
        unavailable = self.physician("Unavailable", EffectiveRole.Role.FACULTY)
        backup_only = self.physician("Backup only", EffectiveRole.Role.FACULTY)
        soft_event = self.physician("Soft Academic Event", EffectiveRole.Role.FACULTY)
        self.availability(unavailable, FacultyAvailabilityTemplate.AvailabilityState.OFF)
        self.availability(backup_only, FacultyAvailabilityTemplate.AvailabilityState.BACKUP_ONLY)
        self.availability(soft_event, FacultyAvailabilityTemplate.AvailabilityState.OPEN)
        for day, physician in (
            (date(2025, 2, 3), unavailable),
            (date(2025, 2, 10), backup_only),
            (date(2025, 2, 17), soft_event),
        ):
            self.session(day, "PM")
            LateDocAssignment.objects.create(date=day, physician=physician)
        self.academic_event(
            "Soft late doc event",
            date(2025, 2, 17),
            date(2025, 2, 17),
            AcademicEvent.PeriodOfDay.PM,
            participants=(soft_event,),
            impact=AcademicEvent.PreceptingImpact.SOFT,
        )

        report = precepting_schedule_report(self.parameters(end_date="2025-02-17"))

        summaries = {finding.code: (finding.severity, finding.count) for finding in report.findings}
        self.assertEqual(summaries["not_available_late_doc"], ("hard", 1))
        self.assertEqual(summaries["backup_only_late_doc"], ("preference", 1))
        self.assertEqual(summaries["soft_academic_event_late_doc"], ("preference", 1))

    def test_unfilled_late_doc_uses_the_shared_finding_label(self):
        self.session(date(2025, 2, 3), "PM")

        report = precepting_schedule_report(self.parameters())

        finding = next(finding for finding in report.findings if finding.code == "missing_late_doc")
        self.assertEqual(
            (finding.label, finding.severity, finding.count), ("Unfilled Late Doc", "hard", 1)
        )

    def test_normal_rounding_uses_monday_anchors_for_coverage_and_findings(self):
        historical = self.physician("Historical", EffectiveRole.Role.FACULTY)
        zero = self.physician("Zero", EffectiveRole.Role.FACULTY)
        community_zero = self.physician(
            "Community zero",
            EffectiveRole.Role.COMMUNITY_ROUNDER,
            start=date(2025, 2, 3),
            end=date(2025, 2, 9),
        )
        self.physician(
            "Partial community rounder",
            EffectiveRole.Role.COMMUNITY_ROUNDER,
            start=date(2025, 2, 4),
            end=date(2025, 2, 9),
        )
        inconsistent = self.physician("Recorded without Faculty role")
        casual_only = self.physician("Casual only", EffectiveRole.Role.CASUAL_ROUNDER)
        NormalRounder.objects.create(week=date(2025, 2, 3), physician=historical)
        NormalRounder.objects.create(week=date(2025, 2, 10), physician=inconsistent)
        CasualRounderRule.objects.create(physician=casual_only, weekday=1, ordinals=[2])

        history = normal_rounding_schedule_report(
            schedule_report_parameters(
                {
                    "schedule": "normal-rounding",
                    "period": "custom",
                    "start_date": "2025-02-03",
                    "end_date": "2025-02-17",
                    "coverage": "history",
                },
                today=date(2025, 2, 4),
            )
        )
        scheduled = normal_rounding_schedule_report(
            schedule_report_parameters(
                {
                    "schedule": "normal-rounding",
                    "period": "custom",
                    "start_date": "2025-02-03",
                    "end_date": "2025-02-17",
                    "coverage": "scheduled",
                },
                today=date(2025, 2, 4),
            )
        )

        self.assertEqual(
            {row.physician: row.weeks for row in history.rows},
            {historical: 1, zero: 0, community_zero: 0},
        )
        self.assertEqual(
            {row.physician: row.weeks for row in scheduled.rows},
            {historical: 0, inconsistent: 1, zero: 0},
        )
        self.assertEqual(history.findings, ())
        self.assertEqual(
            {finding.code: (finding.severity, finding.count) for finding in scheduled.findings},
            {
                "missing_normal_rounder": ("hard", 1),
                "role_ineligible_normal_rounder": ("hard", 1),
            },
        )

    def test_casual_rounding_expands_effective_ordinal_rules_and_deduplicates_days(self):
        first = self.physician("First", EffectiveRole.Role.CASUAL_ROUNDER)
        zero = self.physician("Zero", EffectiveRole.Role.CASUAL_ROUNDER)
        inconsistent = self.physician("Recorded without Casual Rounder role")
        CasualRounderRule.objects.create(
            physician=first,
            weekday=1,
            ordinals=[1],
            effective_start=date(2025, 4, 1),
            effective_end=date(2025, 4, 1),
        )
        for _ in range(2):
            CasualRounderRule.objects.create(
                physician=inconsistent,
                weekday=1,
                ordinals=[5],
                effective_start=date(2025, 4, 29),
                effective_end=date(2025, 4, 29),
            )
        CasualRounderRule.objects.create(
            physician=inconsistent,
            weekday=1,
            ordinals=[1],
            effective_end=date(2025, 3, 31),
        )

        report = casual_rounding_schedule_report(
            schedule_report_parameters(
                {
                    "schedule": "casual-rounding",
                    "period": "custom",
                    "start_date": "2025-04-01",
                    "end_date": "2025-04-30",
                    "coverage": "scheduled",
                },
                today=date(2025, 4, 1),
            )
        )

        self.assertEqual(
            {row.physician: row.days for row in report.rows},
            {first: 1, inconsistent: 1, zero: 0},
        )
        self.assertEqual(
            {finding.code: (finding.severity, finding.count) for finding in report.findings},
            {"role_ineligible_casual_rounder": ("hard", 1)},
        )

    def test_academic_event_report_counts_each_overlapping_occurrence_and_affected_shift(self):
        first = self.physician("First", EffectiveRole.Role.FACULTY)
        second = self.physician("Second", EffectiveRole.Role.FACULTY)
        zero = self.physician("Zero", EffectiveRole.Role.FACULTY)
        inconsistent = self.physician("Recorded without Faculty role")
        self.academic_event(
            "AM event",
            date(2025, 2, 1),
            date(2025, 2, 4),
            AcademicEvent.PeriodOfDay.AM,
            (first, second, inconsistent),
            required=4,
        )
        self.academic_event(
            "Full event",
            date(2025, 2, 3),
            date(2025, 2, 5),
            AcademicEvent.PeriodOfDay.WHOLE_DAY,
            (first,),
        )
        self.academic_event(
            "PM event",
            date(2025, 2, 4),
            date(2025, 2, 4),
            AcademicEvent.PeriodOfDay.PM,
            (first,),
        )
        self.academic_event(
            "Unpopulated event",
            date(2025, 2, 5),
            date(2025, 2, 5),
            AcademicEvent.PeriodOfDay.WHOLE_DAY,
        )

        report = academic_event_schedule_report(
            self.parameters(schedule="academic-events", end_date="2025-02-05")
        )

        rows = {row.physician: row for row in report.rows}
        self.assertEqual(
            (rows[first].events, rows[first].event_days, rows[first].affected_shifts), (3, 6, 9)
        )
        self.assertEqual(
            (rows[second].events, rows[second].event_days, rows[second].affected_shifts), (1, 2, 2)
        )
        self.assertEqual(
            (
                rows[inconsistent].events,
                rows[inconsistent].event_days,
                rows[inconsistent].affected_shifts,
            ),
            (1, 2, 2),
        )
        self.assertEqual((rows[zero].events, rows[zero].event_days), (0, 0))
        findings = {finding.code: (finding.severity, finding.count) for finding in report.findings}
        self.assertEqual(findings["underfilled_academic_event"], ("hard", 1))
        self.assertEqual(findings["unpopulated_academic_event"], ("hard", 1))
        self.assertEqual(findings["role_ineligible_academic_event_participant"], ("hard", 1))

    def test_academic_event_report_sums_disjoint_range_days_once_per_event(self):
        faculty = self.physician("Faculty", EffectiveRole.Role.FACULTY)
        event = self.academic_event(
            "Split event",
            date(2025, 2, 3),
            date(2025, 2, 5),
            AcademicEvent.PeriodOfDay.WHOLE_DAY,
            (faculty,),
        )
        event.ranges.create(start_date=date(2025, 2, 3), end_date=date(2025, 2, 3))
        event.ranges.create(start_date=date(2025, 2, 5), end_date=date(2025, 2, 5))

        report = academic_event_schedule_report(
            self.parameters(schedule="academic-events", end_date="2025-02-05")
        )

        row = next(row for row in report.rows if row.physician == faculty)
        self.assertEqual((row.events, row.event_days, row.affected_shifts), (1, 2, 4))

    def test_collective_monthly_event_report_loads_faculty_roles_once_for_many_occurrences(self):
        faculty = self.physician("Faculty", EffectiveRole.Role.FACULTY, start=date(2025, 1, 1))
        definition = AcademicEventDefinition.objects.create(
            base_name="Monthly faculty meeting",
            scheduling_mode=AcademicEventDefinition.SchedulingMode.MONTHLY_RULES,
            expected_quarter=None,
            all_available_faculty=True,
            start_academic_year=2024,
        )
        AcademicEventMonthlyRule.objects.create(
            definition=definition, weekday=0, ordinals=[1, 2, 3, 4, 5]
        )

        with CaptureQueriesContext(connection) as queries:
            report = academic_event_schedule_report(
                self.parameters(
                    schedule="academic-events", start_date="2025-01-01", end_date="2025-12-31"
                )
            )

        row = next(row for row in report.rows if row.physician == faculty)
        self.assertEqual((row.events, row.event_days, row.affected_shifts), (52, 52, 104))
        self.assertLessEqual(len(queries), 13)

    def test_calendar_only_academic_events_count_days_but_not_affected_shifts(self):
        faculty = self.physician("Faculty", EffectiveRole.Role.FACULTY)
        self.academic_event(
            "Calendar-only event",
            date(2025, 2, 3),
            date(2025, 2, 4),
            AcademicEvent.PeriodOfDay.AM,
            (faculty,),
            impact=AcademicEvent.PreceptingImpact.NONE,
        )

        report = academic_event_schedule_report(
            self.parameters(schedule="academic-events", end_date="2025-02-04")
        )

        row = next(row for row in report.rows if row.physician == faculty)
        self.assertEqual((row.events, row.event_days, row.affected_shifts), (1, 2, 0))

    def test_calendar_only_academic_events_have_no_scheduled_findings(self):
        ineligible = self.physician("Recorded without Faculty role")
        self.academic_event(
            "Unpopulated calendar-only event",
            date(2025, 2, 3),
            date(2025, 2, 3),
            AcademicEvent.PeriodOfDay.AM,
            impact=AcademicEvent.PreceptingImpact.NONE,
        )
        self.academic_event(
            "Underfilled calendar-only event",
            date(2025, 2, 3),
            date(2025, 2, 3),
            AcademicEvent.PeriodOfDay.PM,
            (ineligible,),
            required=2,
            impact=AcademicEvent.PreceptingImpact.NONE,
        )

        report = academic_event_schedule_report(self.parameters(schedule="academic-events"))

        self.assertEqual(report.findings, ())

    def test_interview_report_retains_inconsistent_assignments_and_summarizes_gaps(self):
        assigned = self.physician("Assigned", EffectiveRole.Role.FACULTY)
        zero = self.physician("Zero", EffectiveRole.Role.FACULTY)
        inconsistent = self.physician("Recorded without Faculty role")
        InterviewShift.objects.create(date=date(2025, 2, 3), physician=assigned)
        InterviewShift.objects.create(date=date(2025, 2, 4), physician=inconsistent)
        InterviewShift.objects.create(date=date(2025, 2, 5))

        report = interview_schedule_report(
            self.parameters(schedule="interviews", end_date="2025-02-05")
        )

        rows = {row.physician: row for row in report.rows}
        self.assertEqual(rows[assigned].shifts, 1)
        self.assertEqual(rows[inconsistent].shifts, 1)
        self.assertEqual(rows[zero].shifts, 0)
        findings = {finding.code: (finding.severity, finding.count) for finding in report.findings}
        self.assertEqual(findings["unassigned_interview_shift"], ("hard", 1))
        self.assertEqual(findings["role_ineligible_assignment"], ("hard", 1))

    def test_interview_report_marks_an_unassigned_closed_holiday_as_a_holiday_finding(self):
        today = date(2026, 1, 1)
        holiday = date(2026, 12, 25)
        InterviewShift.objects.create(date=holiday)

        # Holiday findings use the shared operational clock, not just report coverage.
        with patch(
            "schedules.services.operational_attention.organization_today", return_value=today
        ):
            report = interview_schedule_report(
                self.parameters(
                    today=today,
                    schedule="interviews",
                    start_date=holiday.isoformat(),
                    end_date=holiday.isoformat(),
                    coverage="scheduled",
                )
            )

        findings = {finding.code: (finding.severity, finding.count) for finding in report.findings}
        self.assertEqual(findings, {"holiday_closed_interview": ("hard", 1)})

    def test_precepting_report_does_not_require_late_doc_on_a_closed_monday(self):
        holiday = date(2026, 9, 7)

        report = precepting_schedule_report(
            self.parameters(
                schedule="precepting",
                start_date=holiday.isoformat(),
                end_date=holiday.isoformat(),
                coverage="scheduled",
            )
        )

        self.assertNotIn("missing_late_doc", {finding.code for finding in report.findings})

    def test_precepting_report_marks_late_doc_assigned_on_a_closed_monday(self):
        holiday = date(2026, 9, 7)
        physician = self.physician("Faculty", EffectiveRole.Role.FACULTY)
        LateDocAssignment.objects.create(date=holiday, physician=physician)

        report = precepting_schedule_report(
            self.parameters(
                schedule="precepting",
                start_date=holiday.isoformat(),
                end_date=holiday.isoformat(),
                coverage="scheduled",
            )
        )

        findings = {finding.code: (finding.severity, finding.count) for finding in report.findings}
        self.assertEqual(findings, {"holiday_closed_late_doc_assignment": ("hard", 1)})

    def test_event_and_interview_reports_obey_academic_and_calendar_boundaries(self):
        physician = self.physician("Faculty", EffectiveRole.Role.FACULTY, start=date(2024, 1, 1))
        self.academic_event(
            "Academic year start",
            date(2024, 7, 1),
            date(2024, 7, 1),
            AcademicEvent.PeriodOfDay.AM,
            (physician,),
        )
        self.academic_event(
            "Academic year end",
            date(2025, 6, 30),
            date(2025, 6, 30),
            AcademicEvent.PeriodOfDay.PM,
            (physician,),
        )
        InterviewShift.objects.create(date=date(2025, 1, 1), physician=physician)
        InterviewShift.objects.create(date=date(2025, 12, 31), physician=physician)

        academic = academic_event_schedule_report(
            schedule_report_parameters(
                {"schedule": "academic-events", "period": "academic-year", "academic_year": "2024"},
                today=date(2024, 7, 1),
            )
        )
        calendar = interview_schedule_report(
            schedule_report_parameters(
                {"schedule": "interviews", "period": "calendar-year", "calendar_year": "2025"},
                today=date(2025, 1, 1),
            )
        )

        self.assertEqual(academic.rows[0].events, 2)
        self.assertEqual(calendar.rows[0].shifts, 2)

    def test_vacation_report_clips_ranges_and_deduplicates_calendar_days(self):
        eligible = self.physician("Eligible", EffectiveRole.Role.CASUAL_ROUNDER)
        recorded = self.physician("Recorded", EffectiveRole.Role.FACULTY)
        absent = self.physician("Absent")
        expired = self.physician("Expired", EffectiveRole.Role.FACULTY, end=date(2025, 2, 2))
        for start_date, end_date, period in (
            (date(2025, 2, 1), date(2025, 2, 4), Vacation.PeriodOfDay.WHOLE_DAY),
            (date(2025, 2, 4), date(2025, 2, 7), Vacation.PeriodOfDay.AM),
            (date(2025, 2, 6), date(2025, 2, 8), Vacation.PeriodOfDay.PM),
            (date(2025, 2, 8), date(2025, 2, 10), Vacation.PeriodOfDay.WHOLE_DAY),
        ):
            Vacation.objects.create(
                physician=recorded,
                start_date=start_date,
                end_date=end_date,
                period_of_day=period,
            )
        Vacation.objects.create(
            physician=absent, start_date=date(2025, 2, 3), end_date=date(2025, 2, 3)
        )
        Vacation.objects.create(
            physician=expired, start_date=date(2025, 2, 3), end_date=date(2025, 2, 3)
        )

        report = vacation_schedule_report(
            self.parameters(schedule="vacation", end_date="2025-02-09")
        )

        rows = {row.physician: row for row in report.rows}
        self.assertEqual(
            (
                rows[recorded].ranges,
                rows[recorded].weekdays,
                rows[recorded].weekend_days,
                rows[recorded].total_days,
            ),
            (4, 5, 2, 7),
        )
        self.assertEqual((rows[eligible].ranges, rows[eligible].total_days), (0, 0))
        self.assertEqual((rows[absent].ranges, rows[absent].total_days), (1, 1))
        self.assertEqual((rows[expired].ranges, rows[expired].total_days), (1, 1))
        self.assertEqual(
            {finding.code: (finding.severity, finding.count) for finding in report.findings},
            {"role_ineligible_vacation": ("hard", 2)},
        )

    def test_vacation_report_obeys_today_and_annual_range_edges(self):
        physician = self.physician("Physician", EffectiveRole.Role.FACULTY, start=date(2024, 1, 1))
        for on_date in (date(2024, 7, 1), date(2025, 6, 30), date(2025, 1, 1), date(2025, 12, 31)):
            Vacation.objects.create(physician=physician, start_date=on_date, end_date=on_date)

        scheduled = vacation_schedule_report(
            schedule_report_parameters(
                {
                    "schedule": "vacation",
                    "period": "custom",
                    "start_date": "2025-06-29",
                    "end_date": "2025-07-01",
                    "coverage": "scheduled",
                },
                today=date(2025, 6, 30),
            )
        )
        academic = vacation_schedule_report(
            schedule_report_parameters(
                {"schedule": "vacation", "period": "academic-year", "academic_year": "2024"},
                today=date(2024, 7, 1),
            )
        )
        calendar = vacation_schedule_report(
            schedule_report_parameters(
                {"schedule": "vacation", "period": "calendar-year", "calendar_year": "2025"},
                today=date(2025, 1, 1),
            )
        )

        self.assertEqual((scheduled.rows[0].ranges, scheduled.rows[0].total_days), (1, 1))
        self.assertEqual((academic.rows[0].ranges, academic.rows[0].total_days), (3, 3))
        self.assertEqual((calendar.rows[0].ranges, calendar.rows[0].total_days), (3, 3))


class ScheduleReportPresentationTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )

    def test_reports_route_is_an_inert_schedule_selection_landing_page(self):
        self.client.force_login(self.scheduler)

        with patch("schedules.views.reports._schedule_report") as build_report:
            response = self.client.get("/reports/")

        self.assertEqual(reverse("schedule-reports"), "/reports/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/reports/schedule/").status_code, 404)
        self.assertContains(response, "Report Definition")
        self.assertContains(response, '<option value="" disabled selected>Select a report</option>')
        self.assertContains(
            response, '<select id="schedule-report-schedule" name="schedule" required>'
        )
        self.assertNotContains(response, "schedule-report-workload")
        self.assertNotContains(response, "schedule-report-findings")
        self.assertNotContains(response, "Download PDF")
        build_report.assert_not_called()
        self.assertContains(
            response,
            'href="/reports/" aria-current="page">Schedule Reports</a>',
        )
        self.assertContains(
            response,
            'href="/reports/community-preceptor-stipend/">Community Preceptor Stipend</a>',
        )
        content = response.content.decode()
        navigation_start = content.index('<nav class="workspace-local-navigation"')
        navigation = content[navigation_start : content.index("</nav>", navigation_start)]
        self.assertEqual(navigation.count('aria-current="page"'), 1)
        self.assertLess(
            navigation.index("Schedule Reports"), navigation.index("Community Preceptor Stipend")
        )

    def test_authenticated_get_is_stable_and_contains_progressive_controls(self):
        self.client.force_login(self.scheduler)
        today = organization_today()
        response = self.client.get(
            reverse("schedule-reports"),
            {
                "schedule": "precepting",
                "period": "custom",
                "start_date": (today - timedelta(days=1)).isoformat(),
                "end_date": today.isoformat(),
                "coverage": "history-and-scheduled",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'method="get"')
        self.assertContains(response, "data-schedule-report-controls")
        self.assertContains(response, "Schedule Reports")
        self.assertContains(response, "Planning report, not final.")
        self.assertContains(response, "schedule-report-controls.js")
        self.assertContains(response, "form-processing-feedback.js")
        self.assertContains(response, "data-processing-feedback-form")
        self.assertContains(response, 'aria-label="Loading schedule report"')
        self.assertContains(response, "Your schedule report is loading.")
        self.assertContains(
            response,
            '<noscript><div class="form-action-area"><button type="submit">Update report</button></div></noscript>',
        )
        self.assertNotContains(response, ">View report<")
        self.assertEqual(self.client.get(reverse("schedule-reports")).status_code, 200)
        history = self.client.get(
            reverse("schedule-reports"),
            {
                "schedule": "precepting",
                "period": "custom",
                "start_date": "2020-02-01",
                "end_date": "2020-02-28",
                "coverage": "history",
            },
        )
        self.assertNotContains(history, "Planning report, not final.")
        self.assertNotContains(history, "Current findings")
        self.assertEqual(
            self.client.get(
                reverse("schedule-reports"),
                {
                    "period": "custom",
                    "start_date": "2020-02-01",
                    "end_date": "2020-02-28",
                    "coverage": "scheduled",
                },
            ).status_code,
            200,
        )
        self.client.logout()
        self.assertEqual(self.client.get(reverse("schedule-reports")).status_code, 302)

    def test_empty_schedule_is_rejected(self):
        self.client.force_login(self.scheduler)

        response = self.client.get(reverse("schedule-reports"), {"schedule": ""})

        self.assertEqual(response.status_code, 400)

    def test_out_of_range_academic_year_get_is_a_bad_request(self):
        self.client.force_login(self.scheduler)

        response = self.client.get(
            reverse("schedule-reports"),
            {"period": "academic-year", "academic_year": "9999"},
        )

        self.assertEqual(response.status_code, 400)

    def test_controls_group_the_range_and_show_clipped_included_dates(self):
        self.client.force_login(self.scheduler)
        today = organization_today()
        start_date = today - timedelta(days=2)
        end_date = today + timedelta(days=2)

        response = self.client.get(
            reverse("schedule-reports"),
            {
                "period": "custom",
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "coverage": "history",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Report Definition")
        self.assertContains(response, "Date Range")
        self.assertContains(
            response, '<fieldset class="form-choice-group" data-schedule-report-period>'
        )
        self.assertContains(response, "<legend>Range</legend>")
        self.assertContains(response, '<div class="form-choice-group-content">')
        self.assertNotContains(response, "Range type")
        self.assertContains(response, "Effective range")
        self.assertContains(response, "Included dates")
        self.assertContains(
            response,
            f"{start_date:%b} {start_date.day}, {start_date:%Y} through "
            f"{(today - timedelta(days=1)):%b} {(today - timedelta(days=1)).day}, {(today - timedelta(days=1)):%Y}",
        )

    def test_range_hides_custom_dates_until_custom_period_is_selected(self):
        self.client.force_login(self.scheduler)

        academic_year = self.client.get(reverse("schedule-reports"))
        calendar_year = self.client.get(reverse("schedule-reports"), {"period": "calendar-year"})
        custom = self.client.get(
            reverse("schedule-reports"),
            {
                "period": "custom",
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
            },
        )

        custom_range = (
            'class="form-field-grid form-field-grid--effective-range" '
            'data-schedule-report-period-value="custom"'
        )

        self.assertContains(academic_year, 'data-schedule-report-period-value="academic-year">')
        self.assertContains(
            academic_year, 'data-schedule-report-period-value="calendar-year" hidden>'
        )
        self.assertContains(academic_year, f"{custom_range} hidden>")
        self.assertContains(
            calendar_year, 'data-schedule-report-period-value="academic-year" hidden>'
        )
        self.assertContains(calendar_year, 'data-schedule-report-period-value="calendar-year">')
        self.assertContains(calendar_year, f"{custom_range} hidden>")
        self.assertContains(custom, 'data-schedule-report-period-value="academic-year" hidden>')
        self.assertContains(custom, 'data-schedule-report-period-value="calendar-year" hidden>')
        self.assertContains(custom, f"{custom_range}>")
        self.assertContains(custom, '<label for="schedule-report-start-date">Start</label>')
        self.assertContains(custom, '<label for="schedule-report-end-date">End</label>')

    def test_annual_controls_navigate_across_the_full_calendar_safe_range(self):
        self.client.force_login(self.scheduler)
        today = organization_today()
        academic_year = academic_year_for_date(today)

        response = self.client.get(reverse("schedule-reports"), {"schedule": "precepting"})

        self.assertContains(
            response,
            '<nav class="toolbar period-navigation period-navigation-cluster '
            'period-navigation--academic-year" '
            'aria-label="Academic year navigation">',
        )
        self.assertContains(
            response,
            f'<option value="{academic_year}" selected>{academic_year}&ndash;{academic_year + 1}</option>',
        )
        self.assertContains(
            response,
            f"period=academic-year&amp;coverage=history-and-scheduled"
            f"&amp;academic_year={academic_year - 1}&amp;schedule=precepting",
        )
        self.assertContains(response, 'aria-label="Calendar year navigation"')
        self.assertContains(
            response,
            '<nav class="toolbar period-navigation period-navigation-cluster" '
            'aria-label="Calendar year navigation">',
        )
        self.assertContains(
            response, f'<option value="{today.year}" selected>{today.year}</option>'
        )
        content = response.content.decode()
        for navigation_label in ("Academic year navigation", "Calendar year navigation"):
            with self.subTest(navigation_label=navigation_label):
                navigation_start = content.rindex("<nav", 0, content.index(navigation_label))
                navigation = content[navigation_start : content.index("</nav>", navigation_start)]
                self.assertNotIn("<label", navigation)

        academic_lower_boundary = self.client.get(
            reverse("schedule-reports"),
            {"period": "academic-year", "academic_year": "1"},
        )
        self.assertEqual(academic_lower_boundary.status_code, 200)
        self.assertContains(
            academic_lower_boundary,
            '<span role="link" aria-disabled="true" aria-label="Previous academic year">&lsaquo;</span>',
        )
        self.assertContains(academic_lower_boundary, "academic_year=2")
        academic_upper_boundary = self.client.get(
            reverse("schedule-reports"),
            {"period": "academic-year", "academic_year": "9998"},
        )
        self.assertEqual(academic_upper_boundary.status_code, 200)
        self.assertContains(academic_upper_boundary, "academic_year=9997")
        self.assertContains(
            academic_upper_boundary,
            '<span role="link" aria-disabled="true" aria-label="Next academic year">&rsaquo;</span>',
        )
        calendar_lower_boundary = self.client.get(
            reverse("schedule-reports"),
            {"period": "calendar-year", "calendar_year": "1"},
        )
        self.assertEqual(calendar_lower_boundary.status_code, 200)
        self.assertContains(
            calendar_lower_boundary,
            '<span role="link" aria-disabled="true" aria-label="Previous calendar year">&lsaquo;</span>',
        )
        self.assertContains(calendar_lower_boundary, "calendar_year=2")
        calendar_upper_boundary = self.client.get(
            reverse("schedule-reports"),
            {"period": "calendar-year", "calendar_year": "9999"},
        )
        self.assertEqual(calendar_upper_boundary.status_code, 200)
        self.assertContains(calendar_upper_boundary, "calendar_year=9998")
        self.assertContains(
            calendar_upper_boundary,
            '<span role="link" aria-disabled="true" aria-label="Next calendar year">&rsaquo;</span>',
        )

    def test_coverage_controls_fix_the_only_available_coverage_for_one_sided_ranges(self):
        self.client.force_login(self.scheduler)
        today = organization_today()
        historical = self.client.get(
            reverse("schedule-reports"),
            {
                "period": "custom",
                "start_date": (today - timedelta(days=3)).isoformat(),
                "end_date": (today - timedelta(days=1)).isoformat(),
                "coverage": "scheduled",
            },
        )
        scheduled = self.client.get(
            reverse("schedule-reports"),
            {
                "period": "custom",
                "start_date": today.isoformat(),
                "end_date": (today + timedelta(days=3)).isoformat(),
                "coverage": "history",
            },
        )

        self.assertEqual(historical.status_code, 200)
        self.assertContains(
            historical,
            'value="history" aria-describedby="schedule-report-coverage-message" checked',
        )
        self.assertContains(
            historical,
            'value="scheduled" aria-describedby="schedule-report-coverage-message" disabled',
        )
        self.assertContains(
            historical,
            'data-schedule-report-coverage-choice="history-and-scheduled" hidden',
        )
        historical_content = historical.content.decode()
        self.assertLess(
            historical_content.index('data-schedule-report-coverage-choice="history"'),
            historical_content.index(
                'data-schedule-report-coverage-choice="history-and-scheduled"'
            ),
        )
        self.assertLess(
            historical_content.index(
                'data-schedule-report-coverage-choice="history-and-scheduled"'
            ),
            historical_content.index('data-schedule-report-coverage-choice="scheduled"'),
        )
        self.assertNotContains(historical, "Unavailable")
        self.assertContains(historical, "> History</label>", html=False)
        self.assertContains(historical, "> Scheduled</label>", html=False)
        self.assertEqual(scheduled.status_code, 200)
        self.assertContains(
            scheduled,
            'value="history" aria-describedby="schedule-report-coverage-message" disabled',
        )
        self.assertContains(
            scheduled,
            'value="scheduled" aria-describedby="schedule-report-coverage-message" checked',
        )

    def test_community_precepting_get_uses_community_columns(self):
        self.client.force_login(self.scheduler)
        today = _REPORT_MONDAY
        physician = Physician.objects.create(display_name="Community")
        zero = Physician.objects.create(display_name="Community zero")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=today,
        )
        EffectiveRole.objects.create(
            physician=zero,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=today,
        )
        open_monthly_draft(today.replace(day=1))
        PreceptorAssignment.objects.create(
            session=PreceptorSession.objects.get(date=today, period_of_day="AM"),
            physician=physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )

        with (
            patch("schedules.views.reports.organization_today", return_value=today),
            patch("schedules.services.schedule_reports.organization_today", return_value=today),
        ):
            response = self.client.get(
                reverse("schedule-reports"),
                {
                    "schedule": "community-precepting",
                    "period": "custom",
                    "start_date": today.isoformat(),
                    "end_date": today.isoformat(),
                    "coverage": "scheduled",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            _workload_values(response.content.decode(), "Community", ("AM", "PM", "Total")),
            ("1", "0", "1"),
        )
        self.assertEqual(
            _workload_values(response.content.decode(), "Community zero", ("AM", "PM", "Total")),
            ("0", "0", "0"),
        )

    def test_event_and_interview_gets_use_the_shared_surface_and_columns(self):
        self.client.force_login(self.scheduler)
        today = _REPORT_MONDAY
        physician = Physician.objects.create(display_name="Faculty")
        zero = Physician.objects.create(display_name="Event zero")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=today,
        )
        EffectiveRole.objects.create(
            physician=zero,
            role=EffectiveRole.Role.FACULTY,
            effective_start=today,
        )
        definition = AcademicEventDefinition.objects.create(
            base_name="Faculty meeting",
            start_academic_year=academic_year_for_date(today),
        )
        event = AcademicEvent.objects.create(
            definition=definition,
            academic_year=academic_year_for_date(today),
            base_name="Faculty meeting",
            required_participant_count=1,
            resolution=AcademicEvent.Resolution.DATED,
            start_date=today,
            end_date=today + timedelta(days=1),
            period_of_day=AcademicEvent.PeriodOfDay.WHOLE_DAY,
            precepting_impact=AcademicEvent.PreceptingImpact.HARD,
        )
        event.participants.set((physician,))
        InterviewShift.objects.create(date=today, physician=physician)

        with (
            patch("schedules.views.reports.organization_today", return_value=today),
            patch("schedules.services.schedule_reports.organization_today", return_value=today),
        ):
            event_response = self.client.get(
                reverse("schedule-reports"),
                {
                    "schedule": "academic-events",
                    "period": "custom",
                    "start_date": today.isoformat(),
                    "end_date": (today + timedelta(days=1)).isoformat(),
                    "coverage": "scheduled",
                },
            )
            interview_response = self.client.get(
                reverse("schedule-reports"),
                {
                    "schedule": "interviews",
                    "period": "custom",
                    "start_date": today.isoformat(),
                    "end_date": today.isoformat(),
                    "coverage": "scheduled",
                },
            )
        interview_history = self.client.get(
            reverse("schedule-reports"),
            {
                "schedule": "interviews",
                "period": "custom",
                "start_date": "2020-02-01",
                "end_date": "2020-02-28",
                "coverage": "history",
            },
        )

        self.assertEqual(event_response.status_code, 200)
        self.assertEqual(
            _workload_values(
                event_response.content.decode(),
                "Faculty",
                ("Events", "Event days", "Affected shifts"),
            ),
            ("1", "2", "4"),
        )
        self.assertEqual(
            _workload_values(
                event_response.content.decode(),
                "Event zero",
                ("Events", "Event days", "Affected shifts"),
            ),
            ("0", "0", "0"),
        )
        self.assertEqual(interview_response.status_code, 200)
        self.assertEqual(
            _workload_values(interview_response.content.decode(), "Faculty", ("Shifts",)),
            ("1",),
        )
        self.assertEqual(
            _workload_values(interview_response.content.decode(), "Event zero", ("Shifts",)),
            ("0",),
        )
        self.assertNotContains(interview_history, "Current findings")

    def test_rounding_gets_use_independent_columns(self):
        self.client.force_login(self.scheduler)
        today = _REPORT_MONDAY
        normal_physician = Physician.objects.create(display_name="Normal")
        casual_physician = Physician.objects.create(display_name="Casual")
        EffectiveRole.objects.create(
            physician=normal_physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=today,
        )
        EffectiveRole.objects.create(
            physician=casual_physician,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start=today,
        )
        NormalRounder.objects.create(
            physician=normal_physician, week=today - timedelta(days=today.weekday())
        )
        CasualRounderRule.objects.create(
            physician=casual_physician,
            weekday=today.weekday(),
            ordinals=[((today.day - 1) // 7) + 1],
            effective_start=today,
        )

        with (
            patch("schedules.views.reports.organization_today", return_value=today),
            patch("schedules.services.schedule_reports.organization_today", return_value=today),
        ):
            normal = self.client.get(
                reverse("schedule-reports"),
                {
                    "schedule": "normal-rounding",
                    "period": "custom",
                    "start_date": today.isoformat(),
                    "end_date": today.isoformat(),
                    "coverage": "scheduled",
                },
            )
            casual = self.client.get(
                reverse("schedule-reports"),
                {
                    "schedule": "casual-rounding",
                    "period": "custom",
                    "start_date": today.isoformat(),
                    "end_date": today.isoformat(),
                    "coverage": "scheduled",
                },
            )

        self.assertEqual(normal.status_code, 200)
        self.assertEqual(
            _workload_values(normal.content.decode(), "Normal", ("Weeks",)),
            ("1",),
        )
        self.assertEqual(casual.status_code, 200)
        self.assertEqual(
            _workload_values(casual.content.decode(), "Casual", ("Days",)),
            ("1",),
        )

    def test_vacation_get_uses_vacation_columns(self):
        self.client.force_login(self.scheduler)
        today = _REPORT_MONDAY
        physician = Physician.objects.create(display_name="Vacation physician")
        zero = Physician.objects.create(display_name="Vacation zero")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=today,
        )
        EffectiveRole.objects.create(
            physician=zero,
            role=EffectiveRole.Role.FACULTY,
            effective_start=today,
        )
        Vacation.objects.create(
            physician=physician, start_date=today, end_date=today + timedelta(days=6)
        )

        with (
            patch("schedules.views.reports.organization_today", return_value=today),
            patch("schedules.services.schedule_reports.organization_today", return_value=today),
        ):
            response = self.client.get(
                reverse("schedule-reports"),
                {
                    "schedule": "vacation",
                    "period": "custom",
                    "start_date": today.isoformat(),
                    "end_date": (today + timedelta(days=6)).isoformat(),
                    "coverage": "scheduled",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            _workload_values(
                response.content.decode(),
                "Vacation physician",
                ("Ranges", "Weekdays", "Weekend days", "Total days"),
            ),
            ("1", "5", "2", "7"),
        )
        self.assertEqual(
            _workload_values(
                response.content.decode(),
                "Vacation zero",
                ("Ranges", "Weekdays", "Weekend days", "Total days"),
            ),
            ("0", "0", "0", "0"),
        )


_SCHEDULE_REPORT_BROWSER_RUNNER = r"""
const { readFileSync } = require("fs");

const input = JSON.parse(readFileSync(0, "utf8"));
const { chromium } = require(process.env.PRECEPTOR_PLAYWRIGHT_MODULE);

function describesUnavailableRange(text, unavailable, temporalReason) {
  const normalized = text.replace(/\s+/g, " ").trim();
  const availabilityClaim = new RegExp(`\\b${unavailable}\\b\\s+(?:is|remains|becomes|stays)\\s+unavailable\\b`, "i");
  const reversedClaim = new RegExp(`\\b${unavailable}\\b\\s+(?:is\\s+)?not\\s+unavailable\\b`, "i");
  const negatedTemporalReason = /\bnot\s+(?:before|prior\s+to)\s+today\b/i;
  return availabilityClaim.test(normalized) && !reversedClaim.test(normalized) &&
    !negatedTemporalReason.test(normalized) && temporalReason.test(normalized);
}

async function assertUnavailableCoverage(page, url, unavailable, selected, temporalReason) {
  await page.goto(new URL(url, input.baseUrl).href, { waitUntil: "domcontentloaded" });
  const choice = page.locator(`input[name=coverage][value="${unavailable}"]`);
  const selectedChoice = page.locator(`input[name=coverage][value="${selected}"]`);
  if (await choice.count() !== 1 || !(await choice.isDisabled()) || await selectedChoice.isChecked() !== true) {
    throw new Error(`The ${unavailable} coverage control is not disabled while ${selected} is fixed.`);
  }
  const describedBy = await choice.getAttribute("aria-describedby");
  const message = await choice.evaluate((control) => {
    const ids = (control.getAttribute("aria-describedby") || "").trim().split(/\s+/).filter(Boolean);
    if (!ids.length || new Set(ids).size !== ids.length) return null;
    const targets = ids.map((id) => document.getElementById(id));
    return targets.every(Boolean) ? targets.map((target) => target.textContent || "").join(" ") : null;
  });
  if (!message || !describesUnavailableRange(message, unavailable, temporalReason)) {
    throw new Error(`The disabled ${unavailable} coverage control does not describe its actual unavailable range.`);
  }
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: input.browserExecutable });
  try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.route("**/*", (route) => {
    if (route.request().url().endsWith("/static/schedules/application.css")) {
      return route.fulfill({ body: readFileSync(input.stylesheet), contentType: "text/css" });
    }
    return route.request().resourceType() === "document" ? route.continue() : route.abort();
  });
  await page.goto(input.baseUrl + input.reportUrl, { waitUntil: "domcontentloaded" });
  await page.locator('input[name="username"]').fill(input.username);
  await page.locator('input[name="password"]').fill(input.password);
  await page.locator('button[type="submit"]').click();
  await page.waitForLoadState("domcontentloaded");

  const controls = page.locator("form.schedule-report-controls");
  const controlGroups = [];
  for (const [heading, field] of [
    ["Report Definition", 'select[name="schedule"]'],
    ["Date Range", 'input[name="period"]'],
    ["Coverage", 'input[name="coverage"]'],
  ]) {
    const groupHeading = controls.getByRole("heading", { name: heading, exact: true });
    const group = groupHeading.locator("xpath=ancestor::section[1]");
    if (await groupHeading.count() !== 1 || await group.count() !== 1 || await group.locator(field).count() === 0) {
      throw new Error(`The ${heading} controls are not grouped with their own heading and field.`);
    }
    controlGroups.push(group);
  }
  const controlBoxes = await Promise.all(controlGroups.map((group) => group.boundingBox()));
  const formBox = await controls.boundingBox();
  const controlsAreContainedAndNonoverlapping = Boolean(formBox) && controlBoxes.every((group) =>
    group && group.width > 0 && group.height > 0 &&
    formBox.x <= group.x && group.x + group.width <= formBox.x + formBox.width &&
    formBox.y <= group.y && group.y + group.height <= formBox.y + formBox.height
  ) && controlBoxes.every((group, index) =>
    controlBoxes.slice(index + 1).every((later) =>
      group && later && (group.x + group.width <= later.x || later.x + later.width <= group.x ||
        group.y + group.height <= later.y || later.y + later.height <= group.y)
    )
  );
  if (!controlsAreContainedAndNonoverlapping) {
    throw new Error("Schedule Report control surfaces are overlapping or escape their rendered form.");
  }
  const customRange = page.locator('[data-schedule-report-period-value="custom"]');
  if (await customRange.count() !== 1 || !(await customRange.isVisible())) {
    throw new Error("The selected custom date range is not visibly rendered.");
  }
  for (const period of ["academic-year", "calendar-year"]) {
    const hiddenRange = page.locator(`[data-schedule-report-period-value="${period}"]`);
    const hidden = await hiddenRange.evaluate((element) => {
      const bounds = element.getBoundingClientRect();
      return element.hidden && getComputedStyle(element).display === "none" && !bounds.width && !bounds.height;
    });
    if (!hidden) throw new Error(`The unselected ${period} range still occupies rendered space.`);
  }

  const workload = page.locator("#schedule-report-workload");
  const tableIsContained = await workload.evaluate((table) => {
    const frame = table.parentElement;
    const tableBox = table.getBoundingClientRect();
    const frameBox = frame?.getBoundingClientRect();
    return Boolean(frameBox && tableBox.width && tableBox.height &&
      frameBox.left <= tableBox.left && tableBox.right <= frameBox.right &&
      tableBox.right - frameBox.left <= frame.scrollWidth + 1);
  });
  if (!tableIsContained) throw new Error("The rendered workload table is not contained by its scroll frame.");

  const reportSurface = workload.locator("xpath=ancestor::section[1]");
  const header = reportSurface.locator(":scope > header");
  const title = header.getByRole("heading", { name: "Vacation workload", exact: true });
  const range = header.getByText(input.reportRange, { exact: true });
  const action = header.getByRole("link", { name: "Download PDF", exact: true });
  if (await header.count() !== 1 || await title.count() !== 1 ||
      (await range.textContent()).trim() !== input.reportRange || await action.count() !== 1 ||
      await page.getByRole("link", { name: "Download PDF", exact: true }).count() !== 1) {
    throw new Error("The one PDF action is not rendered in the workload report header.");
  }
  const headerOrder = await header.evaluate((element, rangeText) => {
    const readingOrder = [...element.querySelectorAll("h1, h2, h3, h4, h5, h6, p, a")];
    return [
      readingOrder.findIndex((child) => child.textContent.trim() === "Vacation workload"),
      readingOrder.findIndex((child) => child.textContent.trim() === rangeText),
      readingOrder.findIndex((child) => child.textContent.trim() === "Download PDF"),
    ];
  }, input.reportRange);
  if (!(headerOrder[0] < headerOrder[1] && headerOrder[1] < headerOrder[2])) {
    throw new Error("The report header does not keep title, range, and PDF action in reading order.");
  }
  const [headerBox, titleBox, rangeBox, actionBox] = await Promise.all([
    header.boundingBox(), title.boundingBox(), range.boundingBox(), action.boundingBox(),
  ]);
  const headerHasRightAlignedAction = [titleBox, rangeBox, actionBox].every((box) =>
    box && headerBox && headerBox.x <= box.x && box.x + box.width <= headerBox.x + headerBox.width &&
    headerBox.y <= box.y && box.y + box.height <= headerBox.y + headerBox.height
  ) && actionBox && headerBox && actionBox.x + actionBox.width >= headerBox.x + (headerBox.width * 0.9);
  if (!headerHasRightAlignedAction) {
    throw new Error("The PDF action is not visibly right-aligned within the report header.");
  }
  const pdfUrl = new URL(await action.getAttribute("href"), input.baseUrl);
  if (pdfUrl.pathname !== input.pdfPath) {
    throw new Error("The PDF action does not target the supported schedule-report PDF route.");
  }
  for (const [name, value] of Object.entries(input.pdfQuery)) {
    const values = pdfUrl.searchParams.getAll(name);
    if (values.length !== 1 || values[0] !== value) {
      throw new Error(`The PDF action lost its ${name} report parameter.`);
    }
  }

  await page.setViewportSize({ width: 480, height: 900 });
  const [compactHeaderBox, compactTitleBox, compactRangeBox, compactActionBox] = await Promise.all([
    header.boundingBox(), title.boundingBox(), range.boundingBox(), action.boundingBox(),
  ]);
  const compactHeaderIsReadable = [compactTitleBox, compactRangeBox, compactActionBox].every((box) =>
    box && box.width > 0 && box.height > 0 && compactHeaderBox &&
    compactHeaderBox.x <= box.x && box.x + box.width <= compactHeaderBox.x + compactHeaderBox.width &&
    compactHeaderBox.y <= box.y && box.y + box.height <= compactHeaderBox.y + compactHeaderBox.height
  ) && [compactTitleBox, compactRangeBox, compactActionBox].every((box, index, boxes) =>
    boxes.slice(index + 1).every((later) =>
      box && later && (box.x + box.width <= later.x || later.x + later.width <= box.x ||
        box.y + box.height <= later.y || later.y + later.height <= box.y)
    )
  ) && await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
  if (!compactHeaderIsReadable) {
    throw new Error("The compact report header does not keep its title, range, and PDF action readable.");
  }
  const tableFrame = workload.locator("xpath=..");
  const scrollState = await tableFrame.evaluate((frame) => ({
    clientWidth: frame.clientWidth,
    scrollWidth: frame.scrollWidth,
  }));
  if (scrollState.scrollWidth <= scrollState.clientWidth) {
    throw new Error("The narrow report viewport does not preserve horizontal access to the workload table.");
  }
  await tableFrame.evaluate((frame) => { frame.scrollLeft = frame.scrollWidth; });
  const row = workload.getByRole("row").filter({ has: page.getByRole("rowheader", { name: input.reportRow.physician, exact: true }) });
  const rowField = await workload.evaluate((table, reportRow) => {
    const headers = [...table.querySelectorAll("thead th")].map((header) => header.textContent.trim());
    const rows = [...table.querySelectorAll("tbody tr")].filter((candidate) =>
      candidate.querySelector("th[scope=row]")?.textContent.trim() === reportRow.physician
    );
    const totalDays = headers.indexOf("Total days");
    return { matches: rows.length, totalDays, value: rows[0]?.children[totalDays]?.textContent.trim() };
  }, input.reportRow);
  if (rowField.matches !== 1 || rowField.totalDays < 0 || rowField.value !== input.reportRow.totalDays) {
    throw new Error("The rendered vacation row does not bind its Total days value to its Physician field.");
  }
  const totalDaysCell = row.locator("th, td").nth(rowField.totalDays);
  const [frameBox, totalDaysBox] = await Promise.all([tableFrame.boundingBox(), totalDaysCell.boundingBox()]);
  if (!frameBox || !totalDaysBox || totalDaysBox.width <= 0 || totalDaysBox.height <= 0 ||
      totalDaysBox.x < frameBox.x - 1 || totalDaysBox.x + totalDaysBox.width > frameBox.x + frameBox.width + 1) {
    throw new Error("The report row's Total days field is not reachable and readable through the horizontal scroll frame.");
  }

  await assertUnavailableCoverage(page, input.historyOnlyUrl, "scheduled", "history", /(?:ends?|is entirely)\s+.*(?:before|prior to)\s+today/i);
  await assertUnavailableCoverage(page, input.scheduledOnlyUrl, "history", "scheduled", /(?:begins?|starts?|is entirely)\s+.*(?:today|later|on or after)/i);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""


class ScheduleReportPresentationBrowserTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        cls.node, cls.browser_executable = require_browser_tools()
        super().setUpClass()

    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="browser-scheduler", password="browser-password"
        )

    def test_report_controls_and_pdf_action_have_rendered_grouping_and_associations(self):
        today = organization_today()
        report_query = {
            "schedule": "vacation",
            "period": "custom",
            "start_date": (today - timedelta(days=3)).isoformat(),
            "end_date": (today - timedelta(days=1)).isoformat(),
            "coverage": "history",
        }
        physician = Physician.objects.create(display_name="Browser vacation physician")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=today - timedelta(days=3),
        )
        Vacation.objects.create(
            physician=physician,
            start_date=today - timedelta(days=3),
            end_date=today - timedelta(days=1),
        )
        completed = subprocess.run(
            [self.node, "-e", _SCHEDULE_REPORT_BROWSER_RUNNER],
            input=json.dumps(
                {
                    "baseUrl": self.live_server_url,
                    "browserExecutable": self.browser_executable,
                    "stylesheet": str(
                        Path(__file__).parents[1] / "static/schedules/application.css"
                    ),
                    "username": self.scheduler.username,
                    "password": "browser-password",
                    "reportUrl": f"/reports/?{urlencode(report_query)}",
                    "pdfPath": reverse("schedule-report-pdf"),
                    "reportRange": (
                        f"{today - timedelta(days=3):%b} {(today - timedelta(days=3)).day}, "
                        f"{(today - timedelta(days=3)):%Y} through "
                        f"{today - timedelta(days=1):%b} {(today - timedelta(days=1)).day}, "
                        f"{(today - timedelta(days=1)):%Y}"
                    ),
                    "pdfQuery": report_query,
                    "reportRow": {"physician": physician.display_name, "totalDays": "3"},
                    "historyOnlyUrl": (
                        "/reports/?"
                        + urlencode(
                            {
                                **report_query,
                                "coverage": "scheduled",
                            }
                        )
                    ),
                    "scheduledOnlyUrl": (
                        "/reports/?"
                        + urlencode(
                            {
                                **report_query,
                                "start_date": today.isoformat(),
                                "end_date": (today + timedelta(days=3)).isoformat(),
                            }
                        )
                    ),
                }
            ),
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            self.fail(completed.stderr)


class ScheduleReportPdfTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.client.force_login(self.scheduler)

    def test_each_schedule_downloads_the_current_rendered_projection(self):
        today = date(2026, 8, 23)
        query = {
            "period": "custom",
            "start_date": today.isoformat(),
            "end_date": (today + timedelta(days=1)).isoformat(),
            "coverage": "scheduled",
        }
        reports = (
            ("precepting", "Precepting"),
            ("community-precepting", "Community Precepting"),
            ("academic-events", "Academic Events"),
            ("interviews", "Interviews"),
            ("normal-rounding", "Normal Rounding"),
            ("casual-rounding", "Casual Rounding"),
            ("vacation", "Vacation"),
        )
        findings = {
            "precepting": ("Unfilled Late Doc",),
            "community-precepting": ("Unfilled Late Doc",),
            "academic-events": (),
            "interviews": (),
            "normal-rounding": ("Missing Normal Rounder",),
            "casual-rounding": (),
            "vacation": (),
        }

        with patch("schedules.services.schedule_reports.organization_today", return_value=today):
            for schedule, label in reports:
                with self.subTest(schedule=schedule):
                    response = self.client.get(
                        reverse("schedule-reports"), {"schedule": schedule, **query}
                    )
                    pdf = self.client.get(
                        reverse("schedule-report-pdf"), {"schedule": schedule, **query}
                    )
                    text = _pdf_text(pdf.content)

                    self.assertEqual(response.status_code, 200)
                    self.assertContains(
                        response,
                        f"/reports/schedule.pdf?schedule={schedule}&amp;period=custom&amp;coverage=scheduled",
                    )
                    self.assertEqual(pdf.status_code, 200)
                    self.assertEqual(pdf["Content-Type"], "application/pdf")
                    self.assertEqual(
                        pdf["Content-Disposition"],
                        'attachment; filename="'
                        f"muriel-{schedule}-schedule-report-custom-{today.isoformat()}-to-"
                        f'{(today + timedelta(days=1)).isoformat()}-scheduled.pdf"',
                    )
                    title = f"Family Practice Clinic - {label} Schedule Report"
                    self.assertEqual(PdfReader(BytesIO(pdf.content)).metadata.title, title)
                    self.assertIn(title, " ".join(text.split()))
                    for value in (
                        "Custom range",
                        "Effective coverage",
                        "Generated by",
                        "scheduler",
                        "Current corrected records at the time of generation.",
                        "Current findings",
                        "No current facts fall in this coverage.",
                    ):
                        self.assertIn(value, text)
                    if findings[schedule]:
                        self.assertNotIn("No current findings.", text)
                        for finding in findings[schedule]:
                            self.assertIn(finding, text)
                    else:
                        self.assertIn("No current findings.", text)

    def test_history_pdf_omits_planning_and_findings_and_requires_authentication(self):
        query = {
            "schedule": "interviews",
            "period": "custom",
            "start_date": "2020-02-01",
            "end_date": "2020-02-28",
            "coverage": "history",
        }

        response = self.client.get(reverse("schedule-report-pdf"), query)
        text = _pdf_text(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertIn("History: Feb 1, 2020 through Feb 28, 2020", text)
        self.assertNotIn("Planning report", text)
        self.assertNotIn("Current findings", text)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("schedule-report-pdf"), query).status_code, 302)

    def test_pdf_artifact_includes_the_precepting_record_and_findings_table(self):
        name = "Physician with a representative long name that must stay on one printable row"
        second_name = "Second PDF physician"
        parameters = ScheduleReportParameters(
            schedule="precepting",
            period="academic-year",
            coverage="history-and-scheduled",
            start_date=date(2025, 7, 1),
            end_date=date(2026, 6, 30),
            effective_start=date(2025, 7, 1),
            effective_end=date(2026, 6, 30),
            today=date(2026, 1, 1),
        )
        report = PreceptingScheduleReport(
            parameters=parameters,
            rows=(
                PreceptingReportRow(
                    physician=Physician(display_name=name),
                    am=111,
                    pm=222,
                    primary=200,
                    secondary=133,
                    precepting_total=333,
                    late_doc=98,
                ),
                PreceptingReportRow(
                    physician=Physician(display_name=second_name),
                    am=211,
                    pm=322,
                    primary=433,
                    secondary=100,
                    precepting_total=533,
                    late_doc=88,
                ),
            ),
            findings=(
                ReportFindingSummary(
                    code="missing_late_doc", label="Unfilled Late Doc", severity="hard", count=12
                ),
            ),
        )
        content = build_schedule_report_pdf(
            report, generated_at=datetime(2026, 1, 2, 3, 4), generated_by="scheduler"
        )
        reader = PdfReader(BytesIO(content))
        text = "\n".join(page.extract_text() for page in reader.pages)

        # TEXT-CONTRACT: schedule-report-pdf-planning-notice
        # Scheduled/combined PDFs use this exact notice so saved reports are not
        # mistaken for final historical output (docs/design.md section 25.2).
        # Scope is every character, including the em dash. This test protects
        # schedules/pdf.py::build_schedule_report_pdf.
        planning_notice = "Planning report — includes scheduled future work and may change"
        self.assertIn(planning_notice, text)
        for value in (
            "Academic year 2025-2026",
            "History + scheduled: Jul 1, 2025 through Jun 30, 2026",
            name,
            second_name,
            "Unfilled Late Doc",
        ):
            self.assertIn(value, text)

        headers = (
            "Physician",
            "AM",
            "PM",
            "Primary",
            "Secondary",
            "Precepting total",
            "Late Doc",
        )
        record_values = {
            name: ("111", "222", "200", "133", "333", "98"),
            second_name: ("211", "322", "433", "100", "533", "88"),
        }
        _, locations, _, grid_lines = _pdf_artifact(
            content,
            {
                *headers,
                *record_values,
                *(value for values in record_values.values() for value in values),
                "Finding",
                "Severity",
                "Count",
                "Unfilled Late Doc",
                "Hard",
                "12",
                "Generated by",
                "scheduler",
            },
        )

        physician_header = _grid_cell_at(grid_lines, _single_location(locations, "Physician"))
        last_header = _grid_cell_at(grid_lines, _single_location(locations, "Late Doc"))
        basis_label_cell = _grid_cell_at(grid_lines, _single_location(locations, "Generated by"))
        basis_value_cell = _grid_cell_at(grid_lines, _single_location(locations, "scheduler"))
        first_page = reader.pages[0]
        # The PDF requires landscape Letter to fit the complete, non-wrapping Precepting row.
        self.assertEqual((first_page.mediabox.width, first_page.mediabox.height), (792, 612))
        self.assertEqual(
            (basis_label_cell["bottom"], basis_label_cell["top"]),
            (basis_value_cell["bottom"], basis_value_cell["top"]),
        )
        self.assertLessEqual(first_page.mediabox.left, basis_label_cell["left"])
        self.assertLessEqual(basis_value_cell["right"], first_page.mediabox.right)
        self.assertLessEqual(first_page.mediabox.left, physician_header["left"])
        self.assertLessEqual(last_header["right"], first_page.mediabox.right)
        self.assertEqual(
            (physician_header["left"], last_header["right"]),
            (basis_label_cell["left"], basis_value_cell["right"]),
        )

        def assert_record_row(record_name, values):
            name_location = _single_location(locations, record_name)
            name_cell = _grid_cell_at(grid_lines, name_location)
            name_bounds = name_location["bounds"]
            self.assertTrue(
                name_cell["left"] <= name_bounds[1] < name_bounds[3] <= name_cell["right"]
            )
            self.assertEqual(
                (name_cell["left"], name_cell["right"]),
                (physician_header["left"], physician_header["right"]),
            )
            for header, value in zip(headers[1:], values, strict=True):
                header_cell = _grid_cell_at(grid_lines, _single_location(locations, header))
                value_cell = _grid_cell_at(grid_lines, _single_location(locations, value))
                self.assertEqual(
                    (value_cell["left"], value_cell["right"]),
                    (header_cell["left"], header_cell["right"]),
                )
                self.assertEqual(
                    (value_cell["bottom"], value_cell["top"]),
                    (name_cell["bottom"], name_cell["top"]),
                )

        for record_name, values in record_values.items():
            with self.subTest(record_name=record_name):
                assert_record_row(record_name, values)

        finding_headers = ("Finding", "Severity", "Count")
        finding_values = ("Unfilled Late Doc", "Hard", "12")
        finding_cell = _grid_cell_at(grid_lines, _single_location(locations, finding_values[0]))
        for header, value in zip(finding_headers, finding_values, strict=True):
            header_cell = _grid_cell_at(grid_lines, _single_location(locations, header))
            value_cell = _grid_cell_at(grid_lines, _single_location(locations, value))
            self.assertEqual(
                (value_cell["left"], value_cell["right"]),
                (header_cell["left"], header_cell["right"]),
            )
            self.assertEqual(
                (value_cell["bottom"], value_cell["top"]),
                (finding_cell["bottom"], finding_cell["top"]),
            )


def _pdf_text(content):
    return "\n".join(page.extract_text() for page in PdfReader(BytesIO(content)).pages)
