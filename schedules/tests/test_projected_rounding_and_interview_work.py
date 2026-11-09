from datetime import date
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from schedules.models import InterviewShift
from schedules.services.interviews import (
    InterviewFinding,
    InterviewScheduleSummary,
    interview_projected_work_items,
)
from schedules.services.rounding import (
    NormalRounderStatus,
    normal_rounding_projected_work_items,
)
from schedules.services.work_remaining import global_work_items


class ProjectedNormalRoundingWorkTests(TestCase):
    @patch("schedules.services.rounding.is_operational_preceptor_week", return_value=True)
    @patch("schedules.services.rounding.normal_rounder_statuses_for_weeks")
    def test_missing_and_inconsistent_weeks_have_stable_week_items(
        self, status_projection, _operational_week
    ):
        missing_week = date(2026, 7, 6)
        inconsistent_week = date(2026, 7, 13)
        status_projection.return_value = (
            NormalRounderStatus(missing_week, None, None, None, None, None),
            NormalRounderStatus(inconsistent_week, object(), None, False, False, "Not eligible"),
        )

        items = normal_rounding_projected_work_items(
            (inconsistent_week, missing_week, missing_week)
        )

        self.assertEqual(
            [item.key for item in items],
            ["rounding-week-2026-07-06", "rounding-week-2026-07-13"],
        )
        self.assertEqual(items[0].matters_on, date(2026, 2, 6))
        self.assertEqual(items[0].affected_range.end, date(2026, 7, 12))
        self.assertEqual(
            items[0].target_url,
            f"{reverse('normal-rounder-workspace')}?start_date=2026-07-06&end_date=2026-07-06",
        )
        self.assertEqual(
            [(item.reasons[0].classification, item.reasons[0].quantity) for item in items],
            [("required", 1), ("required", 1)],
        )

    @patch("schedules.services.rounding.is_operational_preceptor_week", return_value=True)
    @patch("schedules.services.rounding.normal_rounder_statuses_for_weeks")
    def test_resolved_week_disappears_and_month_arithmetic_obeys_global_boundaries(
        self, status_projection, _operational_week
    ):
        week = date(2023, 7, 31)
        status_projection.return_value = (
            NormalRounderStatus(week, object(), None, True, True, None),
        )
        self.assertEqual(normal_rounding_projected_work_items((week,)), ())

        status_projection.return_value = (NormalRounderStatus(week, None, None, None, None, None),)
        item = normal_rounding_projected_work_items((week,))[0]
        self.assertEqual(item.matters_on, date(2023, 2, 28))
        for today, expected_count in (
            (date(2023, 2, 27), 0),
            (date(2023, 2, 28), 1),
            (date(2023, 8, 6), 1),
            (date(2023, 8, 7), 0),
        ):
            with (
                self.subTest(today=today),
                patch("schedules.services.work_remaining.organization_today", return_value=today),
            ):
                self.assertEqual(len(global_work_items((item,))), expected_count)

    @patch("schedules.services.rounding.normal_rounder_statuses_for_weeks")
    @patch("schedules.services.rounding.is_operational_preceptor_week", return_value=False)
    def test_historical_weeks_do_not_request_status_or_casual_rule_work(
        self, _operational_week, status_projection
    ):
        self.assertEqual(normal_rounding_projected_work_items((date(2026, 7, 6),)), ())
        status_projection.assert_not_called()


class ProjectedInterviewWorkTests(TestCase):
    def summary(self, **overrides):
        values = {
            "academic_year": 2026,
            "shifts": (),
            "attention_active": True,
            "active_count": 20,
            "remaining_to_20": 0,
            "unassigned_count": 0,
            "over_target_count": 0,
            "hard_findings": (),
            "findings": (),
            "complete": True,
        }
        values.update(overrides)
        return InterviewScheduleSummary(**values)

    @patch("schedules.services.interviews.operational_interview_schedule_summary")
    def test_aggregates_one_year_item_with_required_and_advisory_reasons(self, summary_projection):
        hard = InterviewFinding(
            "not_available_assignment",
            "hard",
            InterviewShift.objects.create(date=date(2026, 7, 1)),
            (),
            {},
        )
        advisory = InterviewFinding("backup_only_assignment", "preference", None, (), {})
        for active_count, remaining_to_20, over_target_count, expected_reasons in (
            (
                18,
                2,
                0,
                (("required", 2), ("required", 3), ("required", 1), ("advisory", 1)),
            ),
            (
                22,
                0,
                2,
                (("required", 3), ("required", 2), ("required", 1), ("advisory", 1)),
            ),
        ):
            with self.subTest(active_count=active_count):
                summary_projection.return_value = self.summary(
                    active_count=active_count,
                    remaining_to_20=remaining_to_20,
                    unassigned_count=3,
                    over_target_count=over_target_count,
                    hard_findings=(hard,),
                    findings=(hard, advisory),
                    complete=False,
                )

                items = interview_projected_work_items((2026, 2026))

                self.assertEqual(len(items), 1)
                item = items[0]
                self.assertEqual(item.key, "interview-2026")
                self.assertEqual(item.matters_on, date(2026, 7, 1))
                self.assertEqual(item.affected_range.end, date(2027, 6, 30))
                self.assertEqual(item.target_url, f"{reverse('interview-list')}?academic_year=2026")
                self.assertEqual(
                    tuple((reason.classification, reason.quantity) for reason in item.reasons),
                    expected_reasons,
                )

    @patch("schedules.services.interviews.operational_interview_schedule_summary")
    def test_complete_or_expired_year_produces_no_item(self, summary_projection):
        summary_projection.return_value = self.summary()
        self.assertEqual(interview_projected_work_items((2026,)), ())

        summary_projection.return_value = self.summary(
            attention_active=False,
            active_count=0,
            remaining_to_20=20,
            complete=False,
        )
        self.assertEqual(interview_projected_work_items((2026,)), ())

    def test_unresolved_year_activates_on_july_first_and_expires_after_january(self):
        for today, expected_count in (
            (date(2026, 6, 30), 0),
            (date(2026, 7, 1), 1),
            (date(2027, 1, 31), 1),
            (date(2027, 2, 1), 0),
        ):
            with (
                self.subTest(today=today),
                patch("schedules.services.interviews.organization_today", return_value=today),
            ):
                self.assertEqual(len(interview_projected_work_items((2026,))), expected_count)
