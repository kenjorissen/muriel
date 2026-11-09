from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from schedules.models import AcademicEvent, InterviewShift, Physician
from schedules.services.academic_events import (
    academic_event_attention_start,
    academic_event_projected_work_items,
    academic_event_work_remaining,
    academic_events_overlapping,
    actionable_academic_event_projected_work_items,
    correct_academic_event_occurrence,
    create_academic_event_definition,
    create_academic_event_monthly_rule,
    create_academic_event_occurrence,
    record_academic_event_not_occurring,
    reverse_academic_event_not_occurring,
)
from schedules.services.holidays import holiday_closure_for
from schedules.services.interviews import (
    create_interview_shift,
    operational_interview_schedule_summary,
)
from schedules.services.operational_attention import (
    is_operational_date_range,
    is_operational_preceptor_week,
    organization_today,
)
from schedules.services.preceptors import (
    draft_findings_for_week,
    open_weekly_draft,
    operational_draft_findings_for_week,
)
from schedules.services.rounding import operational_rounding_month_projection
from schedules.services.work_remaining import global_work_remaining
from schedules.tests.test_views import rendered_work_remaining_summary_text


class OrganizationTodayTests(SimpleTestCase):
    def test_chicago_midnight_changes_the_operational_date(self):
        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2026, 2, 3, 5, 59, tzinfo=UTC),
        ):
            self.assertEqual(organization_today(), date(2026, 2, 2))
        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2026, 2, 3, 6, 0, tzinfo=UTC),
        ):
            self.assertEqual(organization_today(), date(2026, 2, 3))

    def test_ranges_and_workweeks_remain_operational_through_their_end(self):
        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2026, 2, 4, 18, tzinfo=UTC),
        ):
            self.assertTrue(is_operational_date_range(date(2026, 2, 2), date(2026, 2, 4)))
            self.assertFalse(is_operational_date_range(date(2026, 2, 1), date(2026, 2, 3)))
            self.assertTrue(is_operational_preceptor_week(date(2026, 2, 2)))

    def test_preceptor_week_becomes_historical_at_the_start_of_the_following_day(self):
        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2026, 2, 7, 5, 59, tzinfo=UTC),
        ):
            self.assertTrue(is_operational_preceptor_week(date(2026, 2, 2)))
        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2026, 2, 7, 6, 0, tzinfo=UTC),
        ):
            self.assertFalse(is_operational_preceptor_week(date(2026, 2, 2)))


class OperationalFindingProjectionTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="operational-scheduler", password="test-password"
        )
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))

    def create_weekday_interview_shifts(self, *, academic_year, starting_on, count):
        dates = []
        on_date = starting_on
        while len(dates) < count:
            if on_date.weekday() < 5 and not holiday_closure_for(on_date, "AM"):
                create_interview_shift(
                    academic_year=academic_year,
                    date=on_date,
                    scheduler=self.scheduler,
                )
                dates.append(on_date)
            on_date += timedelta(days=1)
        return tuple(dates)

    def test_late_doc_finding_respects_past_today_and_future_boundaries(self):
        past_week = date(2026, 8, 24)
        today_week = date(2026, 8, 31)
        future_week = date(2026, 9, 14)
        for week in (past_week, today_week, future_week):
            open_weekly_draft(week)

        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2026, 8, 31, 18, tzinfo=UTC),
        ):
            finding_codes = {
                week: {finding.code for finding in draft_findings_for_week(week).findings}
                for week in (past_week, today_week, future_week)
            }

        self.assertNotIn("missing_late_doc", finding_codes[past_week])
        self.assertIn("missing_late_doc", finding_codes[today_week])
        self.assertIn("missing_late_doc", finding_codes[future_week])

    def test_interview_quota_projection_follows_its_academic_year_attention_window(self):
        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2026, 7, 1, 18, tzinfo=UTC),
        ):
            empty_summary = operational_interview_schedule_summary(2026)

        self.assertTrue(empty_summary.attention_active)
        self.assertEqual(empty_summary.remaining_to_20, 20)
        self.assertEqual(empty_summary.unassigned_count, 0)

        self.create_weekday_interview_shifts(
            academic_year=2026, starting_on=date(2026, 7, 1), count=19
        )
        create_interview_shift(
            academic_year=2027,
            date=date(2027, 7, 1),
            scheduler=self.scheduler,
        )

        cases = (
            (date(2026, 6, 30), 2025, False, 0, 0),
            (date(2026, 7, 1), 2026, True, 1, 19),
            (date(2027, 1, 31), 2026, True, 1, 19),
            (date(2027, 2, 1), 2026, False, 0, 0),
            (date(2026, 7, 1), 2027, False, 0, 0),
        )

        for today, academic_year, attention_active, remaining, unassigned in cases:
            with (
                self.subTest(today=today, academic_year=academic_year),
                patch(
                    "schedules.services.operational_attention.timezone.now",
                    return_value=datetime(today.year, today.month, today.day, 18, tzinfo=UTC),
                ),
            ):
                summary = operational_interview_schedule_summary(academic_year)

            self.assertEqual(summary.attention_active, attention_active)
            self.assertEqual(summary.remaining_to_20, remaining)
            self.assertEqual(summary.unassigned_count, unassigned)

    def test_interview_quota_projection_keeps_scheduled_and_assigned_work_separate(self):
        scheduled_dates = self.create_weekday_interview_shifts(
            academic_year=2026, starting_on=date(2026, 7, 1), count=20
        )
        self.assertEqual(len(scheduled_dates), 20)
        self.assertTrue(all(on_date.weekday() < 5 for on_date in scheduled_dates))

        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2026, 7, 1, 18, tzinfo=UTC),
        ):
            summary = operational_interview_schedule_summary(2026)

        self.assertEqual(summary.remaining_to_20, 0)
        self.assertEqual(summary.unassigned_count, 20)

    def test_historical_preceptor_and_interview_findings_are_not_recomputed(self):
        historical_sessions = open_weekly_draft(date(2026, 1, 19))
        # Direct rows deliberately retain malformed no-Faculty assignments so this test can
        # distinguish a historical finding from the same current operational finding.
        historical_shift = InterviewShift.objects.create(
            date=date(2026, 1, 30), physician=Physician.objects.create(display_name="Historical")
        )
        current_shift = InterviewShift.objects.create(
            date=date(2026, 2, 2), physician=Physician.objects.create(display_name="Current")
        )

        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2026, 1, 31, 18, tzinfo=UTC),
        ):
            self.assertTrue(draft_findings_for_week(historical_sessions[0].date).findings)
            self.assertEqual(
                operational_draft_findings_for_week(historical_sessions[0].date).findings, ()
            )
            summary = operational_interview_schedule_summary(2025)

        self.assertEqual([finding.shift for finding in summary.findings], [current_shift])
        self.assertNotIn(historical_shift, [finding.shift for finding in summary.findings])

    def test_historical_rounding_weeks_are_quiet_but_current_week_stays_active(self):
        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2026, 2, 4, 18, tzinfo=UTC),
        ):
            projection = operational_rounding_month_projection(date(2026, 2, 1))

        historical_week = next(
            week for week in projection.normal_weeks if week.week == date(2026, 1, 26)
        )
        current_week = next(
            week for week in projection.normal_weeks if week.week == date(2026, 2, 2)
        )
        self.assertFalse(historical_week.operational_attention)
        self.assertTrue(current_week.operational_attention)
        self.assertEqual(
            [week.week for week in projection.work_summary.missing_normal_weeks],
            [date(2026, 2, 2), date(2026, 2, 9), date(2026, 2, 16), date(2026, 2, 23)],
        )


class AcademicEventAttentionTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(username="scheduler")
        self.physicians = [
            Physician.objects.create(display_name="First physician"),
            Physician.objects.create(display_name="Second physician"),
        ]

    def _at(self, today):
        return patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(today.year, today.month, today.day, 18, tzinfo=UTC),
        )

    def _definition(self, **overrides):
        values = {
            "base_name": "Quarterly event",
            "start_academic_year": 2026,
            "expected_quarter": "Q2",
            "default_required_participant_count": 2,
            "scheduler": self.scheduler,
        }
        values.update(overrides)
        return create_academic_event_definition(**values)

    def test_attention_starts_two_quarters_before_each_target_quarter(self):
        expected_starts = {
            "Q1": date(2026, 1, 1),
            "Q2": date(2026, 4, 1),
            "Q3": date(2026, 7, 1),
            "Q4": date(2026, 10, 1),
        }

        for quarter, attention_start in expected_starts.items():
            with self.subTest(quarter=quarter):
                self.assertEqual(academic_event_attention_start(2026, quarter), attention_start)

    def test_cross_year_unscheduled_work_uses_target_year_and_attention_boundaries(self):
        definition = self._definition()

        for today, expected_definitions in (
            (date(2026, 3, 31), ()),
            (date(2026, 4, 1), (definition,)),
            (date(2027, 1, 1), ()),
        ):
            with self.subTest(today=today), self._at(today):
                work_remaining = academic_event_work_remaining(2026, ())

            self.assertEqual(work_remaining.schedulable_definitions, expected_definitions)
            self.assertEqual(work_remaining.underfilled_events, ())

    def test_underfilled_last_day_occurrence_reappears_until_filled_and_then_quiets(self):
        definition = self._definition()
        event = create_academic_event_occurrence(
            definition=definition,
            academic_year=2026,
            start_date=date(2026, 12, 31),
            end_date=date(2026, 12, 31),
            scheduler=self.scheduler,
        )

        with self._at(date(2026, 4, 1)):
            work_remaining = academic_event_work_remaining(2026, (event,))
        self.assertEqual(work_remaining.underfilled_events, (event,))

        event = correct_academic_event_occurrence(
            event=event,
            start_date=event.start_date,
            end_date=event.end_date,
            period_of_day=event.period_of_day,
            required_participant_count=2,
            participants=self.physicians[:1],
            scheduler=self.scheduler,
        )
        with self._at(date(2026, 4, 1)):
            work_remaining = academic_event_work_remaining(2026, (event,))
        self.assertEqual(work_remaining.underfilled_events, (event,))

        event = correct_academic_event_occurrence(
            event=event,
            start_date=event.start_date,
            end_date=event.end_date,
            period_of_day=event.period_of_day,
            required_participant_count=2,
            participants=self.physicians,
            scheduler=self.scheduler,
        )
        with self._at(date(2026, 4, 1)):
            work_remaining = academic_event_work_remaining(2026, (event,))
        self.assertEqual(work_remaining.underfilled_events, ())

        event = correct_academic_event_occurrence(
            event=event,
            start_date=event.start_date,
            end_date=event.end_date,
            period_of_day=event.period_of_day,
            required_participant_count=2,
            participants=self.physicians[:1],
            scheduler=self.scheduler,
        )
        with self._at(date(2027, 1, 1)):
            work_remaining = academic_event_work_remaining(2026, (event,))
        self.assertEqual(work_remaining.underfilled_events, ())

    def test_underfilled_monthly_occurrence_is_actionable_until_its_end_date(self):
        definition = self._definition(
            base_name="Monthly faculty meeting",
            scheduling_mode="monthly_rules",
            default_participants=self.physicians[:1],
        )
        create_academic_event_monthly_rule(
            definition=definition, weekday=3, ordinals=[3], scheduler=self.scheduler
        )
        event = academic_events_overlapping(date(2026, 8, 1), date(2026, 8, 31))[0]
        item_key = f"academic-event-occurrence-{event.pk}"

        with self._at(date(2026, 8, 15)):
            work_remaining = academic_event_work_remaining(2026, (event,))
            projected = academic_event_projected_work_items(2026, (event,))
            self.client.force_login(self.scheduler)
            listing = self.client.get(reverse("academic-event-list"), {"academic_year": 2026})
            calendar = self.client.get(
                reverse("academic-event-workspace"),
                {"academic_year": 2026, "month": "2026-08"},
            )

        self.assertEqual(work_remaining.underfilled_events, (event,))
        self.assertEqual([item.key for item in projected], [item_key])
        self.assertIn(item_key, [item.key for item in listing.context["work_remaining"].items])
        self.assertIn(item_key, [item.key for item in calendar.context["work_remaining"].items])
        self.assertContains(listing, "Assignment incomplete (1 needed)")
        self.assertContains(calendar, "Assignment incomplete")

        with self._at(date(2026, 8, 21)):
            work_remaining = academic_event_work_remaining(2026, (event,))
        self.assertEqual(work_remaining.underfilled_events, ())

    def test_calendar_only_occurrence_produces_no_participant_work(self):
        definition = self._definition()
        event = create_academic_event_occurrence(
            definition=definition,
            academic_year=2026,
            start_date=date(2026, 12, 31),
            end_date=date(2026, 12, 31),
            period_of_day=AcademicEvent.PeriodOfDay.AM,
            precepting_impact=AcademicEvent.PreceptingImpact.NONE,
            scheduler=self.scheduler,
        )

        with self._at(date(2026, 4, 1)):
            work_remaining = academic_event_work_remaining(2026, (event,))

        self.assertEqual(work_remaining.underfilled_events, ())
        self.assertEqual(academic_event_projected_work_items(2026, (event,)), ())

    def test_not_occurring_is_complete_and_calendar_and_list_share_the_projection(self):
        definition = self._definition()
        not_occurring = self._definition(base_name="Not occurring", expected_quarter="Q4")
        record_academic_event_not_occurring(
            definition=not_occurring, academic_year=2026, scheduler=self.scheduler
        )
        self.client.force_login(self.scheduler)

        with self._at(date(2026, 4, 1)):
            work_remaining = academic_event_work_remaining(
                2026, tuple(AcademicEvent.objects.filter(academic_year=2026, is_active=True))
            )
            calendar = self.client.get(
                reverse("academic-event-workspace"),
                {"academic_year": 2026, "month": "2026-07"},
            )
            listing = self.client.get(reverse("academic-event-list"), {"academic_year": 2026})

        self.assertEqual(work_remaining.schedulable_definitions, (definition,))
        self.assertEqual(work_remaining.underfilled_events, ())
        calendar_summary = calendar.context["work_remaining"]
        listing_summary = listing.context["work_remaining"]
        self.assertEqual(calendar_summary.items, ())
        self.assertEqual(
            (calendar_summary.required_item_count, calendar_summary.advisory_item_count), (0, 0)
        )
        self.assertEqual(
            [item.key for item in listing_summary.items],
            [f"academic-event-definition-{definition.pk}-2026"],
        )
        self.assertEqual(
            (listing_summary.required_item_count, listing_summary.advisory_item_count), (1, 0)
        )
        calendar_summary_text = rendered_work_remaining_summary_text(calendar)
        listing_summary_text = rendered_work_remaining_summary_text(listing)
        self.assertRegex(
            calendar_summary_text,
            r"(?i)\b(?:no\s+work\s+remains|all\s+required\s+work\s+is\s+complete)\b",
        )
        # TEXT-CONTRACT: work-remaining-count-grammar
        self.assertRegex(listing_summary_text, r"\b1 required action remains\b")
        self.assertLess(
            calendar.content.index(b"work-remaining-summary-heading"),
            calendar.content.index(b"Schedule an event"),
        )
        self.assertLess(
            listing.content.index(b"work-remaining-summary-heading"),
            listing.content.index(b"Schedule an event"),
        )

    def test_q4_work_appears_in_scopes_and_global_projection_on_its_attention_date(self):
        definition = self._definition(base_name="Curriculum Retreat", expected_quarter="Q4")
        calendar_url = reverse("academic-event-workspace")
        list_url = reverse("academic-event-list")
        item_key = f"academic-event-definition-{definition.pk}-2026"

        with self._at(date(2026, 8, 27)):
            self.client.force_login(self.scheduler)
            calendar = self.client.get(calendar_url, {"academic_year": 2026, "month": "2026-08"})
            listing = self.client.get(list_url, {"academic_year": 2026})
            global_projection = global_work_remaining()

        self.assertEqual(calendar.context["work_remaining"].required_item_count, 0)
        self.assertEqual(calendar.context["work_remaining"].items, ())
        self.assertNotContains(calendar, "Curriculum Retreat")
        self.assertContains(calendar, "No event definitions are waiting")
        self.assertEqual(listing.context["work_remaining"].required_item_count, 1)
        self.assertEqual([item.key for item in listing.context["work_remaining"].items], [item_key])
        self.assertContains(listing, "Curriculum Retreat")
        self.assertNotIn(item_key, [item.key for item in global_projection.items])

        with self._at(date(2026, 10, 1)):
            self.client.force_login(self.scheduler)
            calendar = self.client.get(calendar_url, {"academic_year": 2026, "month": "2026-10"})
            listing = self.client.get(list_url, {"academic_year": 2026})
            global_projection = global_work_remaining()

        self.assertEqual(calendar.status_code, 200)
        self.assertEqual(calendar.context["work_remaining"].required_item_count, 0)
        self.assertNotContains(calendar, "Curriculum Retreat")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.context["work_remaining"].required_item_count, 1)
        self.assertEqual([item.key for item in listing.context["work_remaining"].items], [item_key])
        self.assertContains(listing, "Curriculum Retreat (Q4)")
        self.assertContains(listing, "Curriculum Retreat")
        self.assertContains(listing, "Not occurring this year")
        self.assertContains(
            listing,
            reverse("academic-event-mark-not-occurring", args=(definition.pk,)),
        )
        self.assertContains(listing, f"definition={definition.pk}")
        self.assertIn(
            "academic-events-unscheduled-yearly-2026-Q4",
            [item.key for item in global_projection.items],
        )

    def test_historical_academic_event_resolutions_do_not_reactivate_projected_work(self):
        incomplete = self._definition(base_name="Incomplete retreat", expected_quarter="Q4")
        not_occurring = self._definition(base_name="Cancelled retreat", expected_quarter="Q4")
        create_academic_event_occurrence(
            definition=incomplete,
            academic_year=2026,
            start_date=date(2027, 4, 1),
            end_date=date(2027, 4, 1),
            scheduler=self.scheduler,
        )
        record_academic_event_not_occurring(
            definition=not_occurring, academic_year=2026, scheduler=self.scheduler
        )
        events = tuple(AcademicEvent.objects.filter(academic_year=2026, is_active=True))

        with self._at(date(2027, 7, 1)):
            self.assertEqual(actionable_academic_event_projected_work_items(2026, events), ())

    def test_reversed_not_occurring_resolution_reappears_in_global_projection(self):
        definition = self._definition(base_name="Curriculum Retreat", expected_quarter="Q4")
        resolution = record_academic_event_not_occurring(
            definition=definition, academic_year=2026, scheduler=self.scheduler
        )
        item_key = f"academic-event-definition-{definition.pk}-2026"

        with self._at(date(2026, 10, 1)):
            self.assertNotIn(item_key, [item.key for item in global_work_remaining().items])
            reverse_academic_event_not_occurring(event=resolution, scheduler=self.scheduler)
            self.assertIn(
                "academic-events-unscheduled-yearly-2026-Q4",
                [item.key for item in global_work_remaining().items],
            )
