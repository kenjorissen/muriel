import calendar
from collections import defaultdict
from datetime import date, timedelta
from threading import Barrier, Lock, Thread

from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse

from schedules.models import (
    Physician,
    PreceptorAssignment,
    PreceptorAssignmentChange,
    PreceptorSession,
    SchedulePeriod,
)
from schedules.services.preceptors import (
    initialize_preceptor_periods,
    open_monthly_draft,
    open_weekly_draft,
)


def _month_start(month):
    return month.replace(day=1)


def _assert_month_sessions(test_case, month, *, edge_dates):
    month = _month_start(month)
    sessions = tuple(
        PreceptorSession.objects.filter(period__month=month).order_by("date", "period_of_day")
    )
    slots = [(session.date, session.period_of_day) for session in sessions]
    periods_by_date = defaultdict(set)
    for session in sessions:
        periods_by_date[session.date].add(session.period_of_day)

    weekday_count = sum(
        date(month.year, month.month, day).weekday() < 5
        for day in range(1, calendar.monthrange(month.year, month.month)[1] + 1)
    )
    expected_periods = {"AM", "PM"}
    test_case.assertEqual(len(slots), len(set(slots)))
    test_case.assertTrue(
        all(
            session.date.year == month.year and session.date.month == month.month
            for session in sessions
        )
    )
    test_case.assertTrue(all(session.date.weekday() < 5 for session in sessions))
    test_case.assertEqual(len(sessions), weekday_count * 2)
    test_case.assertTrue(all(periods == expected_periods for periods in periods_by_date.values()))
    for edge_date in edge_dates:
        test_case.assertEqual(periods_by_date[edge_date], expected_periods)


class LazySessionInitializationServicesTests(TestCase):
    def test_month_creates_exact_weekday_sessions_and_repeated_open_is_idempotent(self):
        month = date(2025, 2, 18)

        period = initialize_preceptor_periods(month)[0]

        self.assertEqual(period.month, date(2025, 2, 1))
        self.assertEqual(
            list(SchedulePeriod.objects.values_list("month", flat=True)), [date(2025, 2, 1)]
        )
        _assert_month_sessions(self, month, edge_dates=(date(2025, 2, 3), date(2025, 2, 28)))
        self.assertFalse(PreceptorAssignment.objects.exists())
        self.assertFalse(PreceptorAssignmentChange.objects.exists())

        repeated = initialize_preceptor_periods(date(2025, 2, 1))[0]

        self.assertEqual(repeated.pk, period.pk)
        _assert_month_sessions(self, month, edge_dates=(date(2025, 2, 3), date(2025, 2, 28)))

    def test_cross_year_workweek_creates_both_full_months_and_returns_ten_sessions(self):
        initialize_preceptor_periods(date(2024, 12, 30), date(2025, 1, 3))
        sessions = tuple(
            PreceptorSession.objects.filter(
                date__range=(date(2024, 12, 30), date(2025, 1, 3))
            ).order_by("date", "period_of_day")
        )
        december = date(2024, 12, 1)
        january = date(2025, 1, 1)

        self.assertEqual(
            list(SchedulePeriod.objects.values_list("month", flat=True)), [december, january]
        )
        _assert_month_sessions(self, december, edge_dates=(date(2024, 12, 2), date(2024, 12, 31)))
        _assert_month_sessions(self, january, edge_dates=(date(2025, 1, 1), date(2025, 1, 31)))
        self.assertEqual(
            [(session.date, session.period_of_day) for session in sessions],
            [
                (day, period)
                for day in (date(2024, 12, 30) + timedelta(days=offset) for offset in range(5))
                for period in ("AM", "PM")
            ],
        )

    def test_historical_initialization_preserves_assignments_and_history(self):
        period = open_monthly_draft(date(2024, 2, 1))
        session = period.sessions.get(date=date(2024, 2, 1), period_of_day="AM")
        physician = Physician.objects.create(display_name="Historical preceptor")
        assignment = PreceptorAssignment.objects.create(
            session=session,
            physician=physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )
        assignment_values = (
            assignment.session_id,
            assignment.physician_id,
            assignment.responsibility,
            assignment.primary_position,
            assignment.is_locked,
        )
        change_count = PreceptorAssignmentChange.objects.count()

        open_monthly_draft(date(2024, 2, 18))
        open_weekly_draft(date(2024, 2, 1))

        assignment.refresh_from_db()
        self.assertEqual(
            (
                assignment.session_id,
                assignment.physician_id,
                assignment.responsibility,
                assignment.primary_position,
                assignment.is_locked,
            ),
            assignment_values,
        )
        self.assertEqual(PreceptorAssignmentChange.objects.count(), change_count)


class LazySessionInitializationConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_simultaneous_month_initialization_is_idempotent(self):
        barrier = Barrier(2)
        errors = []
        lock = Lock()

        def initialize():
            close_old_connections()
            try:
                barrier.wait(timeout=1)
                initialize_preceptor_periods(date(2025, 2, 1))
            except Exception as error:
                with lock:
                    errors.append(error)
            finally:
                close_old_connections()

        first = Thread(target=initialize)
        second = Thread(target=initialize)
        first.start()
        second.start()
        first.join(2)
        second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            list(SchedulePeriod.objects.values_list("month", flat=True)), [date(2025, 2, 1)]
        )
        _assert_month_sessions(
            self, date(2025, 2, 1), edge_dates=(date(2025, 2, 3), date(2025, 2, 28))
        )


class LazySessionInitializationResponseTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )

    def test_anonymous_month_request_does_not_initialize_sessions(self):
        month_response = self.client.get(reverse("monthly-workspace"), {"month": "2026-08"})

        self.assertEqual(month_response.status_code, 302)
        self.assertFalse(SchedulePeriod.objects.exists())
        self.assertFalse(PreceptorSession.objects.exists())

    def test_authenticated_month_response_initializes_selected_and_edge_months(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("monthly-workspace"), {"month": "2026-08"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(SchedulePeriod.objects.values_list("month", flat=True)),
            [date(2026, 8, 1), date(2026, 9, 1)],
        )
        _assert_month_sessions(
            self, date(2026, 8, 1), edge_dates=(date(2026, 8, 3), date(2026, 8, 31))
        )
        _assert_month_sessions(
            self, date(2026, 9, 1), edge_dates=(date(2026, 9, 1), date(2026, 9, 30))
        )

    def test_authenticated_month_response_initializes_both_months_at_year_boundary(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("monthly-workspace"), {"month": "2025-01"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(SchedulePeriod.objects.values_list("month", flat=True)),
            [date(2024, 12, 1), date(2025, 1, 1)],
        )
        _assert_month_sessions(
            self, date(2024, 12, 1), edge_dates=(date(2024, 12, 2), date(2024, 12, 31))
        )
        _assert_month_sessions(
            self, date(2025, 1, 1), edge_dates=(date(2025, 1, 1), date(2025, 1, 31))
        )
