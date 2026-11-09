from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse

from schedules.models import (
    ClinicHoliday,
    HolidayOccurrence,
    HolidayOccurrenceChange,
    LateDocAssignment,
    LeadTimeSetting,
    Physician,
    PreceptorAssignment,
    PreceptorAssignmentChange,
    PreceptorSession,
    SchedulePeriod,
)
from schedules.physician_scope import SESSION_KEY
from schedules.services.lead_times import set_lead_time
from schedules.services.preceptors import open_monthly_draft
from schedules.services.work_remaining import (
    GlobalWorkBucket,
    GlobalWorkRemaining,
    InclusiveDateRange,
    ProjectedWorkItem,
    ProjectedWorkReason,
    global_work_remaining,
)


class GlobalWorkRemainingServiceTests(TestCase):
    today = date(2026, 7, 1)

    def item(self, key, area, *, matters_on, end, classification="required", quantity=1):
        return ProjectedWorkItem(
            key=key,
            area=area,
            label=key,
            affected_range=InclusiveDateRange(matters_on, end),
            matters_on=matters_on,
            target_url=f"/open/{key}/",
            reasons=(ProjectedWorkReason(classification, f"{key} reason", quantity),),
        )

    @patch("schedules.services.work_remaining.organization_today", return_value=today)
    @patch("schedules.services.work_remaining._global_projected_work_items")
    def test_orders_cross_domain_items_and_excludes_future_and_historical_items(
        self, source_items, _today
    ):
        source_items.return_value = (
            self.item("future", "holidays", matters_on=date(2026, 7, 2), end=date(2026, 8, 1)),
            self.item("later", "precepting", matters_on=date(2026, 6, 2), end=date(2026, 7, 4)),
            self.item(
                "advisory",
                "interviews",
                matters_on=date(2026, 6, 1),
                end=date(2026, 7, 1),
                classification="advisory",
            ),
            self.item("first", "holidays", matters_on=date(2026, 6, 1), end=date(2026, 7, 1)),
            self.item("historical", "rounding", matters_on=date(2026, 5, 1), end=date(2026, 6, 30)),
        )

        summary = global_work_remaining()

        self.assertEqual([item.key for item in summary.items], ["first", "advisory", "later"])
        self.assertEqual((summary.required_item_count, summary.advisory_item_count), (2, 1))
        self.assertEqual(summary.tone, "danger")

    @patch("schedules.services.work_remaining.organization_today", return_value=today)
    @patch("schedules.services.work_remaining._global_projected_work_items", return_value=())
    def test_empty_projection_is_clear(self, _source_items, _today):
        summary = global_work_remaining()

        self.assertEqual(summary.items, ())
        self.assertEqual(summary.tone, "clear")

    @patch("schedules.services.work_remaining.organization_today", return_value=today)
    @patch("schedules.services.work_remaining._global_projected_work_items")
    def test_sums_mixed_reason_quantities_without_splitting_the_grouped_row(
        self, source_items, _today
    ):
        source_items.return_value = (
            ProjectedWorkItem(
                key="grouped-week",
                area="precepting",
                label="Week of Jul 6",
                affected_range=InclusiveDateRange(date(2026, 7, 6), date(2026, 7, 10)),
                matters_on=date(2026, 6, 1),
                target_url="/?month=2026-07#precepting-week-2026-07-06",
                reasons=(
                    ProjectedWorkReason("required", "Three positions are unfilled", 3),
                    ProjectedWorkReason("advisory", "Two findings remain", 2),
                ),
            ),
        )

        summary = global_work_remaining()

        self.assertEqual([item.key for item in summary.items], ["grouped-week"])
        self.assertEqual((summary.required_item_count, summary.advisory_item_count), (3, 2))
        self.assertEqual(summary.tone, "danger")

    @patch("schedules.services.work_remaining.organization_today", return_value=today)
    @patch("schedules.services.work_remaining._global_projected_work_items")
    def test_groups_academic_event_work_by_quarter_or_month_without_changing_counts(
        self, source_items, _today
    ):
        list_url = f"{reverse('academic-event-list')}?academic_year=2026"
        q1_bucket = GlobalWorkBucket(
            "academic-events-unscheduled-yearly-2026-Q1",
            "Unscheduled Yearly",
            InclusiveDateRange(date(2026, 7, 1), date(2026, 9, 30)),
            list_url,
        )
        q2_bucket = GlobalWorkBucket(
            "academic-events-unscheduled-yearly-2026-Q2",
            "Unscheduled Yearly",
            InclusiveDateRange(date(2026, 10, 1), date(2026, 12, 31)),
            list_url,
        )
        october_bucket = GlobalWorkBucket(
            "academic-events-scheduled-and-monthly-2026-2026-10",
            "Scheduled and Monthly",
            InclusiveDateRange(date(2026, 10, 1), date(2026, 10, 31)),
            list_url,
        )
        next_year_bucket = GlobalWorkBucket(
            "academic-events-unscheduled-yearly-2027-Q1",
            "Unscheduled Yearly",
            InclusiveDateRange(date(2027, 7, 1), date(2027, 9, 30)),
            f"{reverse('academic-event-list')}?academic_year=2027",
        )

        def event_item(key, bucket, matters_on, classification, quantity):
            return ProjectedWorkItem(
                key=key,
                area="academic-events",
                label=f"Individual {key}",
                affected_range=bucket.affected_range,
                matters_on=matters_on,
                target_url=f"/academic-events/{key}/",
                reasons=(ProjectedWorkReason(classification, f"{key} detail", quantity),),
                global_bucket=bucket,
            )

        source_items.return_value = (
            event_item("q1-first", q1_bucket, date(2026, 4, 1), "required", 1),
            event_item("q1-second", q1_bucket, date(2026, 4, 10), "advisory", 2),
            event_item("q2", q2_bucket, date(2026, 5, 1), "required", 3),
            event_item("yearly", october_bucket, date(2026, 4, 15), "required", 2),
            event_item("monthly", october_bucket, date(2026, 6, 15), "advisory", 1),
            event_item("next-year", next_year_bucket, date(2026, 6, 1), "required", 1),
        )

        summary = global_work_remaining()

        self.assertEqual(
            [item.key for item in summary.items],
            [q1_bucket.key, october_bucket.key, q2_bucket.key, next_year_bucket.key],
        )
        self.assertEqual((summary.required_item_count, summary.advisory_item_count), (7, 3))
        self.assertEqual(summary.items[0].matters_on, date(2026, 4, 1))
        self.assertEqual(summary.items[0].affected_range, q1_bucket.affected_range)
        self.assertEqual(summary.items[0].target_url, list_url)
        self.assertEqual(
            tuple((reason.classification, reason.quantity) for reason in summary.items[0].reasons),
            (("required", 1), ("advisory", 2)),
        )
        self.assertEqual(summary.items[1].matters_on, date(2026, 4, 15))
        self.assertEqual(summary.items[1].affected_range, october_bucket.affected_range)
        self.assertEqual(summary.items[1].target_url, list_url)
        self.assertEqual(
            tuple((reason.classification, reason.quantity) for reason in summary.items[1].reasons),
            (("required", 2), ("advisory", 1)),
        )


class GlobalPreceptingHorizonInitializationTests(TestCase):
    today = date(2026, 7, 1)

    def precepting_items(self, summary):
        return tuple(item for item in summary.items if item.area == "precepting")

    def precepting_work_snapshot(self, summary):
        return tuple(
            (
                item.key,
                tuple((reason.classification, reason.quantity) for reason in item.reasons),
            )
            for item in self.precepting_items(summary)
        )

    def expected_weeks_through(self, final_week):
        return tuple(
            self.today - date.resolution * self.today.weekday() + date.resolution * 7 * offset
            for offset in range(
                ((final_week - (self.today - date.resolution * self.today.weekday())).days // 7) + 1
            )
        )

    @patch("schedules.services.operational_attention.organization_today", return_value=today)
    @patch("schedules.services.work_remaining.organization_today", return_value=today)
    def test_global_projection_initializes_exact_cross_month_horizon_idempotently(
        self, _work_today, _operational_today
    ):
        summary = global_work_remaining()
        expected_weeks = self.expected_weeks_through(date(2026, 11, 30))

        self.assertEqual(
            [item.key for item in self.precepting_items(summary)],
            [f"precepting-week-{week:%Y-%m-%d}" for week in expected_weeks],
        )
        self.assertEqual(
            list(SchedulePeriod.objects.values_list("month", flat=True)),
            [
                date(2026, 6, 1),
                date(2026, 7, 1),
                date(2026, 8, 1),
                date(2026, 9, 1),
                date(2026, 10, 1),
                date(2026, 11, 1),
                date(2026, 12, 1),
            ],
        )
        self.assertEqual(PreceptorSession.objects.count(), len(expected_weeks) * 10)
        self.assertFalse(PreceptorSession.objects.filter(date__gte=date(2026, 12, 7)).exists())
        self.assertFalse(PreceptorAssignment.objects.exists())
        self.assertFalse(LateDocAssignment.objects.exists())
        self.assertFalse(PreceptorAssignmentChange.objects.exists())

        repeated = global_work_remaining()

        self.assertEqual(repeated, summary)
        self.assertEqual(
            self.precepting_work_snapshot(repeated), self.precepting_work_snapshot(summary)
        )
        self.assertEqual(PreceptorSession.objects.count(), len(expected_weeks) * 10)

    @patch("schedules.services.operational_attention.organization_today", return_value=today)
    @patch("schedules.services.work_remaining.organization_today", return_value=today)
    def test_month_visits_do_not_extend_the_global_precepting_horizon(
        self, _work_today, _operational_today
    ):
        expected_snapshot = self.precepting_work_snapshot(global_work_remaining())
        SchedulePeriod.objects.all().delete()

        for month in (
            date(2026, 6, 1),
            date(2026, 7, 1),
            date(2026, 8, 1),
            date(2026, 9, 1),
            date(2026, 10, 1),
            date(2026, 11, 1),
            date(2026, 12, 1),
        ):
            open_monthly_draft(month)

        summary = global_work_remaining()

        self.assertEqual(
            self.precepting_work_snapshot(summary),
            expected_snapshot,
        )

    @patch("schedules.services.operational_attention.organization_today", return_value=today)
    @patch("schedules.services.work_remaining.organization_today", return_value=today)
    def test_larger_precepting_lead_time_extends_only_the_calculated_weeks(
        self, _work_today, _operational_today
    ):
        administrator = get_user_model().objects.create_user(username="administrator")
        administrator.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )
        set_lead_time(family=LeadTimeSetting.Family.PRECEPTING, months=7, scheduler=administrator)

        summary = global_work_remaining()
        expected_weeks = self.expected_weeks_through(date(2027, 2, 1))

        self.assertEqual(
            [item.key for item in self.precepting_items(summary)],
            [f"precepting-week-{week:%Y-%m-%d}" for week in expected_weeks],
        )
        self.assertEqual(PreceptorSession.objects.count(), len(expected_weeks) * 10)
        self.assertFalse(PreceptorSession.objects.filter(date__gte=date(2027, 2, 8)).exists())


class GlobalHolidayHorizonInitializationTests(TestCase):
    today = date(2026, 7, 1)

    def setUp(self):
        self.administrator = get_user_model().objects.create_user(username="administrator")
        self.administrator.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )
        self.client.force_login(self.administrator)

    def holiday_work_snapshot(self, summary):
        return tuple(
            (item.key, tuple((reason.classification, reason.quantity) for reason in item.reasons))
            for item in summary.items
            if item.area == "holidays"
        )

    @patch("schedules.services.operational_attention.organization_today", return_value=today)
    @patch("schedules.services.work_remaining.organization_today", return_value=today)
    def test_global_projection_materializes_holiday_horizon_idempotently_and_expands_with_lead_time(
        self, _work_today, _operational_today
    ):
        summary = global_work_remaining()
        independence_days = HolidayOccurrence.objects.filter(
            calendar_year__in=(2026, 2027), holiday=ClinicHoliday.INDEPENDENCE_DAY
        ).order_by("calendar_year")

        self.assertEqual(
            list(
                HolidayOccurrence.objects.order_by("calendar_year")
                .values_list("calendar_year", flat=True)
                .distinct()
            ),
            [2026, 2027, 2028],
        )
        self.assertEqual(HolidayOccurrence.objects.count(), 21)
        self.assertEqual(HolidayOccurrenceChange.objects.count(), 21)
        self.assertEqual(
            self.holiday_work_snapshot(summary),
            tuple(
                (f"holiday-{occurrence.pk}", (("required", 1),)) for occurrence in independence_days
            ),
        )
        self.assertFalse(HolidayOccurrence.objects.filter(calendar_year=2029).exists())

        repeated = global_work_remaining()

        self.assertEqual(repeated, summary)
        self.assertEqual(self.holiday_work_snapshot(repeated), self.holiday_work_snapshot(summary))
        self.assertEqual(HolidayOccurrence.objects.count(), 21)
        self.assertEqual(HolidayOccurrenceChange.objects.count(), 21)

        for calendar_year in (2026, 2027, 2028):
            with self.subTest(calendar_year=calendar_year):
                self.assertEqual(
                    self.client.get(
                        reverse("holiday-workspace"), {"calendar_year": calendar_year}
                    ).status_code,
                    200,
                )

        self.assertEqual(HolidayOccurrence.objects.count(), 21)
        self.assertEqual(HolidayOccurrenceChange.objects.count(), 21)
        self.assertEqual(
            self.holiday_work_snapshot(global_work_remaining()), self.holiday_work_snapshot(summary)
        )

        set_lead_time(
            family=LeadTimeSetting.Family.HOLIDAYS, months=30, scheduler=self.administrator
        )
        global_work_remaining()

        self.assertEqual(
            list(
                HolidayOccurrence.objects.order_by("calendar_year")
                .values_list("calendar_year", flat=True)
                .distinct()
            ),
            [2026, 2027, 2028, 2029],
        )
        self.assertEqual(HolidayOccurrence.objects.count(), 28)
        self.assertEqual(HolidayOccurrenceChange.objects.count(), 28)
        self.assertFalse(HolidayOccurrence.objects.filter(calendar_year=2030).exists())


class WorkRemainingWorkspaceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.user.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.client.force_login(self.user)
        self.physician = Physician.objects.create(display_name="Focused physician")

    def summary(self):
        item = ProjectedWorkItem(
            key="precepting-week-2026-07-06",
            area="precepting",
            label="Week of Jul 6",
            affected_range=InclusiveDateRange(date(2026, 7, 6), date(2026, 7, 10)),
            matters_on=date(2026, 2, 6),
            target_url="/?month=2026-07#precepting-week-2026-07-06",
            reasons=(
                ProjectedWorkReason("required", "Late Doc is unfilled", 1),
                ProjectedWorkReason("advisory", "1 non-blocking finding remains", 1),
            ),
        )
        return GlobalWorkRemaining(
            items=(item,),
            required_item_count=1,
            advisory_item_count=1,
            tone="danger",
            prose="1 required action remains. 1 advisory action remains.",
        )

    @patch("schedules.views.work_remaining.global_work_remaining")
    def test_renders_shared_summary_table_area_link_and_schedule_navigation(self, projection):
        projection.return_value = self.summary()
        session = self.client.session
        session[SESSION_KEY] = [self.physician.pk]
        session.save()

        response = self.client.get(reverse("work-remaining"))

        self.assertEqual(response.context["work_remaining"], self.summary())
        self.assertContains(response, 'href="/schedule/">Master Schedule</a>')
        self.assertContains(
            response,
            'href="/schedule/work-remaining/" aria-current="page">Work Remaining</a>',
        )
        self.assertContains(
            response,
            'class="workspace-surface surface-frame surface-tone--danger workspace-work-summary"',
        )
        self.assertContains(
            response,
            '<h2 id="work-remaining-summary-heading">Work remaining</h2>',
        )
        self.assertContains(response, '<h2 id="work-items-heading">Work items</h2>')
        self.assertNotContains(response, 'id="work-remaining-heading"')
        self.assertContains(
            response, '<table id="work-remaining" class="data-table data-table--bounded"'
        )
        self.assertContains(response, "Attention starting")
        self.assertContains(response, '<th scope="col">Area</th>')
        self.assertNotContains(response, '<th scope="col">Actions</th>')
        self.assertNotContains(response, "Attention from")
        self.assertNotContains(response, "Matters since")
        self.assertContains(response, "Affected period")
        self.assertContains(response, "Late Doc is unfilled; 1 non-blocking finding remains")
        self.assertContains(
            response, 'href="/?month=2026-07#precepting-week-2026-07-06">Precepting</a>'
        )
        self.assertNotContains(response, "Actions")
        self.assertNotContains(response, ">Open</a>")
        self.assertNotContains(response, "Snooze")
        self.assertNotContains(response, "Dismiss")
        self.assertNotContains(response, "Assignment")

    @patch("schedules.views.work_remaining.global_work_remaining")
    def test_empty_workspace_uses_the_clear_summary_and_table_zero_state(self, projection):
        projection.return_value = GlobalWorkRemaining(
            (), 0, 0, "clear", "No work remains in this range."
        )

        response = self.client.get(reverse("work-remaining"))

        self.assertContains(
            response,
            'class="workspace-surface surface-frame surface-tone--clear workspace-work-summary"',
        )
        self.assertEqual(response.context["work_remaining"].tone, "clear")
        self.assertEqual(response.context["work_remaining_table"].rows, ())
        self.assertContains(response, 'class="data-table__empty"')
        self.assertContains(response, response.context["work_remaining"].prose)
        self.assertContains(response, response.context["work_remaining_table"].empty_state)

    @patch("schedules.views.work_remaining.global_work_remaining")
    def test_area_links_preserve_every_producer_target_shape(self, projection):
        area_labels = {
            "academic-events": "Academic Events",
            "holidays": "Holidays",
            "interviews": "Interviews",
            "precepting": "Precepting",
            "rounding": "Normal Rounding",
        }
        items = tuple(
            ProjectedWorkItem(
                key=area,
                area=area,
                label=area,
                affected_range=InclusiveDateRange(date(2026, 7, 6), date(2026, 7, 10)),
                matters_on=date(2026, 2, 6),
                target_url=target_url,
                reasons=(ProjectedWorkReason("required", "Needs attention", 1),),
            )
            for area, target_url in (
                ("academic-events", "/academic-events/add/?academic_year=2026&definition=1"),
                ("academic-events", "/academic-events/1/correct/"),
                ("holidays", "/vacations/holidays/1/schedule/"),
                ("precepting", "/?month=2026-07#precepting-week-2026-07-06"),
                (
                    "rounding",
                    "/rounding/normal/?start_date=2026-07-06&end_date=2026-07-06",
                ),
                ("interviews", "/interviews/?academic_year=2026"),
            )
        )
        projection.return_value = GlobalWorkRemaining(
            items, 6, 0, "danger", "6 required actions remain."
        )

        response = self.client.get(reverse("work-remaining"))

        for item in items:
            with self.subTest(target_url=item.target_url):
                self.assertContains(
                    response,
                    f'href="{item.target_url.replace("&", "&amp;")}">{area_labels[item.area]}</a>',
                )
