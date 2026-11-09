import re
from datetime import UTC, date, datetime
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from schedules.services.preceptors import (
    DraftFinding,
    DraftFindingResult,
    precepting_projected_work_items,
)
from schedules.services.work_remaining import (
    InclusiveDateRange,
    global_work_items,
    scoped_work_remaining,
)


class ProjectedPreceptingWorkTests(TestCase):
    week = date(2026, 7, 6)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2026, 9, 1, 18, tzinfo=UTC),
    )
    def test_cross_month_work_opens_the_month_owning_remaining_sessions(self, _today):
        from django.contrib.auth import get_user_model

        from schedules.services.preceptors import open_monthly_draft

        open_monthly_draft(date(2026, 9, 1))
        (item,) = precepting_projected_work_items((date(2026, 8, 31),))
        self.assertEqual(item.target_url, "/?month=2026-09#precepting-week-2026-08-31")
        self.client.force_login(get_user_model().objects.create_user(username="remaining-work"))
        response = self.client.get(item.target_url)
        self.assertEqual(response.context["selected_month"], date(2026, 9, 1))
        self.assertContains(response, 'id="precepting-week-2026-08-31"')
        first_week = response.context["weeks"][0]
        self.assertTrue(first_week["days"][1]["sessions"][0].findings)

    def findings(self, *findings):
        return DraftFindingResult(findings)

    def finding(self, code, severity, *, position=None):
        return DraftFinding(
            code,
            severity,
            None,
            self.week,
            (),
            {"position": position} if position else {},
        )

    @patch("schedules.services.preceptors.is_operational_preceptor_week", return_value=True)
    @patch("schedules.services.preceptors.operational_draft_findings_for_week")
    def test_aggregates_weekly_counts_and_mixed_reason_classifications(
        self, finding_projection, _operational_week
    ):
        finding_projection.return_value = self.findings(
            self.finding("missing_primary_position", "hard", position="primary-1"),
            self.finding("missing_primary_position", "hard", position="primary-2"),
            self.finding("missing_late_doc", "hard"),
            self.finding("not_available_assignment", "hard"),
            self.finding("backup_only_assignment", "preference"),
        )
        items = precepting_projected_work_items((self.week, self.week + date.resolution))

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.key, "precepting-week-2026-07-06")
        self.assertEqual(item.matters_on, date(2026, 2, 6))
        self.assertEqual(item.affected_range.start, self.week)
        self.assertEqual(item.affected_range.end, date(2026, 7, 10))
        self.assertEqual(
            item.target_url,
            f"{reverse('monthly-workspace')}?month=2026-07#precepting-week-2026-07-06",
        )
        self.assertTrue(item.required)
        self.assertEqual(
            [
                (
                    reason.classification,
                    reason.quantity,
                    reason.summary_type.key if reason.summary_type else None,
                )
                for reason in item.reasons
            ],
            [
                ("required", 2, "unfilled-position"),
                ("required", 1, "unfilled-late-doc"),
                ("required", 1, "precepting-finding-not_available_assignment"),
                ("advisory", 1, None),
            ],
        )
        summary = scoped_work_remaining(
            (item,), InclusiveDateRange(self.week, self.week + date.resolution * 4), evaluated=True
        )
        self.assertEqual(
            [
                (summary_item.key, summary_item.count, summary_item.label)
                for summary_item in summary.required_summary_items
            ],
            [
                ("unfilled-position", 2, "unfilled positions"),
                ("unfilled-late-doc", 1, "unfilled Late Doc"),
                ("precepting-finding-not_available_assignment", 1, "unavailable finding"),
            ],
        )
        self.assertEqual(
            sum(summary_item.count for summary_item in summary.required_summary_items),
            summary.required_item_count,
        )
        # TEXT-CONTRACT: work-remaining-count-grammar
        self.assertRegex(summary.prose, r"\b4 required actions remain\b")
        typed_phrases = (
            "2 unfilled positions",
            "1 unfilled Late Doc",
            "1 unavailable finding",
        )
        for phrase in typed_phrases:
            self.assertRegex(summary.prose, rf"\b{re.escape(phrase)}\b")
        self.assertEqual(
            [summary.prose.index(phrase) for phrase in typed_phrases],
            sorted(summary.prose.index(phrase) for phrase in typed_phrases),
        )
        self.assertIn(" and ", summary.prose)
        # TEXT-CONTRACT: work-remaining-count-grammar
        self.assertRegex(summary.prose, r"\b1 advisory action remains\b")

    @patch("schedules.services.preceptors.is_operational_preceptor_week", return_value=True)
    @patch("schedules.services.preceptors.operational_draft_findings_for_week")
    def test_holiday_closed_positions_do_not_create_gap_or_missing_late_doc_reasons(
        self, finding_projection, _operational_week
    ):
        finding_projection.return_value = self.findings(
            self.finding("holiday_closed_assignment", "hard"),
            self.finding("holiday_closed_late_doc_assignment", "hard"),
        )
        item = precepting_projected_work_items((self.week,))[0]

        self.assertEqual(
            [
                (
                    reason.classification,
                    reason.quantity,
                    reason.summary_type.key if reason.summary_type else None,
                )
                for reason in item.reasons
            ],
            [
                ("required", 1, "precepting-finding-holiday_closed_assignment"),
                ("required", 1, "precepting-finding-holiday_closed_late_doc_assignment"),
            ],
        )

    @patch("schedules.services.preceptors.operational_draft_findings_for_week")
    @patch("schedules.services.preceptors.is_operational_preceptor_week", return_value=True)
    def test_complete_week_has_no_work(self, _preceptor_operational, finding_projection):
        finding_projection.return_value = self.findings()

        self.assertEqual(precepting_projected_work_items((self.week,)), ())

    @patch("schedules.services.preceptors.is_operational_preceptor_week", return_value=True)
    @patch("schedules.services.preceptors.operational_draft_findings_for_week")
    def test_month_end_timing_and_global_expiry(self, finding_projection, _operational_week):
        week = date(2023, 7, 31)
        finding_projection.return_value = DraftFindingResult(
            (DraftFinding("missing_late_doc", "hard", None, week, (), {}),)
        )
        item = precepting_projected_work_items((week,))[0]

        self.assertEqual(item.matters_on, date(2023, 2, 28))
        for today, expected_count in (
            (date(2023, 2, 27), 0),
            (date(2023, 8, 4), 1),
            (date(2023, 8, 5), 0),
        ):
            with (
                self.subTest(today=today),
                patch("schedules.services.work_remaining.organization_today", return_value=today),
            ):
                self.assertEqual(len(global_work_items((item,))), expected_count)

    @patch("schedules.services.preceptors.is_operational_preceptor_week", return_value=False)
    def test_historical_weeks_produce_no_global_correction_item(self, _operational_week):
        self.assertEqual(precepting_projected_work_items((self.week,)), ())
