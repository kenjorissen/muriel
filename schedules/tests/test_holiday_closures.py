from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from schedules.calendar_presentation import CalendarDay, CalendarItem, CalendarLane, CalendarWeek
from schedules.models import (
    ClinicHoliday,
    EffectiveRole,
    InterviewShift,
    InterviewShiftChange,
    Physician,
    PreceptorAssignment,
    PreceptorSession,
)
from schedules.services.holidays import (
    holiday_closure_for,
    holiday_occurrences_for,
    schedule_holiday_occurrence,
)
from schedules.services.interviews import create_interview_shift
from schedules.services.precepting_solver import SolveDateRange, preflight_precepting_solve
from schedules.services.preceptors import (
    assign_preceptor,
    assignment_alternatives_for_session,
    draft_findings_for_session,
    open_monthly_draft,
)


class HolidayClosureTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(username="scheduler")

    def test_projection_respects_pm_only_and_weekend_observance(self):
        self.assertIsNone(holiday_closure_for(date(2026, 12, 24), "AM"))
        closure = holiday_closure_for(date(2026, 12, 24), "PM")
        self.assertEqual((closure.name, closure.coverage_label), ("Christmas Eve", "PM only"))
        self.assertEqual(
            holiday_closure_for(date(2021, 12, 24), "AM").name,
            "Christmas Day",
        )

    def test_calendar_projects_holidays_into_date_headers_and_range_items(self):
        week = CalendarWeek(
            label="week",
            days=(CalendarDay("2026-12-24", "Dec 24", True),),
            lanes=(
                CalendarLane(
                    "Vacation entries",
                    (CalendarItem(1, 1, "schedules/calendar_vacation_item.html", ()),),
                    holiday_items=True,
                ),
            ),
        )

        self.assertEqual(week.calendar_days[0].holidays[0].name, "Christmas Eve")
        self.assertEqual(week.calendar_days[0].holidays[0].coverage_label, "PM only")
        lane = week.calendar_lanes[0]
        self.assertEqual(lane.holiday_columns, (1,))
        self.assertEqual([item.status for item in lane.items], ["holiday", ""])
        self.assertEqual(lane.items[0].content[0].name, "Christmas Eve")
        self.assertEqual(lane.items[0].content[0].coverage_label, "PM only")

    def test_closed_precepting_session_has_no_gap_finding(self):
        open_monthly_draft(date(2026, 12, 1))
        session = PreceptorSession.objects.get(date=date(2026, 12, 24), period_of_day="PM")

        self.assertEqual(draft_findings_for_session(session).findings, ())

    def test_existing_assignment_is_retained_as_a_holiday_finding_and_new_one_is_rejected(self):
        open_monthly_draft(date(2026, 12, 1))
        session = PreceptorSession.objects.get(date=date(2026, 12, 24), period_of_day="PM")
        open_session = PreceptorSession.objects.get(date=date(2026, 12, 24), period_of_day="AM")
        physician = Physician.objects.create(display_name="Existing assignment")
        PreceptorAssignment.objects.create(
            session=session,
            physician=physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )

        self.assertEqual(
            [finding.code for finding in draft_findings_for_session(session).findings],
            ["holiday_closed_assignment"],
        )
        retained = assignment_alternatives_for_session(
            session, PreceptorAssignment.Responsibility.PRIMARY, 1
        )
        empty = assignment_alternatives_for_session(
            session, PreceptorAssignment.Responsibility.PRIMARY, 2
        )
        self.assertEqual(retained.current.assignment.physician, physician)
        self.assertEqual(tuple(retained), ())
        self.assertIsNone(empty.current)
        self.assertEqual(tuple(empty), ())

        proposed_physician = Physician.objects.create(display_name="Eligible new assignment")
        EffectiveRole.objects.create(
            physician=proposed_physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2026, 1, 1),
        )
        assigned_on_open_coverage = assign_preceptor(
            session=open_session,
            physician=proposed_physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        self.assertEqual(assigned_on_open_coverage.physician, proposed_physician)

        assignment_before_rejection = list(
            PreceptorAssignment.objects.filter(session=session).values(
                "pk", "physician_id", "responsibility", "primary_position", "is_locked"
            )
        )
        history_before_rejection = list(
            session.assignment_changes.order_by("pk").values(
                "assignment_id",
                "action",
                "previous_value",
                "recorded_value",
                "reason",
                "changed_by_id",
            )
        )
        with self.assertRaises(ValidationError) as rejected:
            assign_preceptor(
                session=session,
                physician=proposed_physician,
                responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                primary_position=2,
                scheduler=self.scheduler,
            )
        self.assertIn("Christmas Eve", str(rejected.exception))
        self.assertIn("PM only", str(rejected.exception))
        self.assertEqual(
            list(
                PreceptorAssignment.objects.filter(session=session).values(
                    "pk", "physician_id", "responsibility", "primary_position", "is_locked"
                )
            ),
            assignment_before_rejection,
        )
        self.assertEqual(
            list(
                session.assignment_changes.order_by("pk").values(
                    "assignment_id",
                    "action",
                    "previous_value",
                    "recorded_value",
                    "reason",
                    "changed_by_id",
                )
            ),
            history_before_rejection,
        )

    def test_interviews_reject_all_day_but_not_christmas_eve_pm_closure(self):
        shift = create_interview_shift(
            academic_year=2026,
            date=date(2026, 12, 24),
            scheduler=self.scheduler,
        )
        self.assertEqual(shift.date, date(2026, 12, 24))
        shift_before_rejection = {
            "date": shift.date,
            "physician_id": shift.physician_id,
            "note": shift.note,
            "is_active": shift.is_active,
        }
        history_before_rejection = list(
            shift.changes.order_by("pk").values(
                "action", "previous_value", "recorded_value", "changed_by_id"
            )
        )
        all_history_count_before_rejection = InterviewShiftChange.objects.count()
        with self.assertRaises(ValidationError) as rejected:
            create_interview_shift(
                academic_year=2026,
                date=date(2026, 12, 25),
                scheduler=self.scheduler,
            )
        self.assertIn("Christmas Day", str(rejected.exception))
        self.assertIn("AM and PM", str(rejected.exception))
        shift.refresh_from_db()
        self.assertEqual(
            {
                "date": shift.date,
                "physician_id": shift.physician_id,
                "note": shift.note,
                "is_active": shift.is_active,
            },
            shift_before_rejection,
        )
        self.assertEqual(
            list(
                shift.changes.order_by("pk").values(
                    "action", "previous_value", "recorded_value", "changed_by_id"
                )
            ),
            history_before_rejection,
        )
        self.assertEqual(InterviewShiftChange.objects.count(), all_history_count_before_rejection)
        self.assertFalse(InterviewShift.objects.filter(date=date(2026, 12, 25)).exists())

    def test_solver_omits_closed_coverage(self):
        open_monthly_draft(date(2026, 12, 1))

        solver_input = preflight_precepting_solve(
            SolveDateRange(date(2026, 12, 24), date(2026, 12, 24)),
            balance_window=SolveDateRange(date(2026, 12, 24), date(2026, 12, 24)),
        ).solver_input

        self.assertEqual(
            [(session.date, session.period_of_day) for session in solver_input.sessions],
            [(date(2026, 12, 24), "AM")],
        )

    def test_retained_manual_holiday_changes_the_closure_projection(self):
        occurrence = holiday_occurrences_for(2027).get(holiday=ClinicHoliday.INDEPENDENCE_DAY)
        self.assertIsNone(holiday_closure_for(date(2027, 7, 5), "AM"))
        schedule_holiday_occurrence(
            occurrence=occurrence,
            observed_date=date(2027, 7, 5),
            scheduler=self.scheduler,
        )

        self.assertEqual(holiday_closure_for(date(2027, 7, 5), "AM").name, "Independence Day")

    def test_holiday_change_makes_a_solver_input_fingerprint_stale(self):
        solve_range = SolveDateRange(date(2027, 7, 5), date(2027, 7, 5))
        before = preflight_precepting_solve(solve_range, balance_window=solve_range).solver_input
        occurrence = holiday_occurrences_for(2027).get(holiday=ClinicHoliday.INDEPENDENCE_DAY)

        schedule_holiday_occurrence(
            occurrence=occurrence,
            observed_date=date(2027, 7, 5),
            scheduler=self.scheduler,
        )
        after = preflight_precepting_solve(solve_range, balance_window=solve_range).solver_input

        self.assertNotEqual(before.fingerprint, after.fingerprint)
        self.assertEqual(after.sessions, ())
