from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase

from schedules.models import (
    ClinicHoliday,
    HolidayOccurrence,
    HolidayOccurrenceChange,
    LeadTimeSetting,
)
from schedules.services.academic_events import (
    academic_event_projected_work_items,
    create_academic_event_definition,
)
from schedules.services.holidays import holiday_projected_work_items
from schedules.services.interviews import (
    interview_projected_work_items,
)
from schedules.services.lead_times import set_lead_time
from schedules.services.preceptors import (
    DraftFinding,
    DraftFindingResult,
    precepting_projected_work_items,
)
from schedules.services.rounding import (
    NormalRounderStatus,
    normal_rounding_projected_work_items,
)
from schedules.services.work_remaining import (
    InclusiveDateRange,
    global_work_items,
    global_work_remaining,
    scoped_work_remaining,
)


class ConfiguredLeadTimeProducerTests(TestCase):
    week = date(2026, 7, 6)

    def setUp(self):
        self.administrator = get_user_model().objects.create_user(username="administrator")
        self.administrator.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )
        self.scheduler = get_user_model().objects.create_user(username="scheduler")

    def set_lead_time(self, family, months):
        set_lead_time(family=family, months=months, scheduler=self.administrator)

    def test_each_producer_uses_its_own_current_lead_time(self):
        self.set_lead_time(LeadTimeSetting.Family.HOLIDAYS, 1)
        self.set_lead_time(LeadTimeSetting.Family.ACADEMIC_EVENTS, 0)
        self.set_lead_time(LeadTimeSetting.Family.PRECEPTING, 2)
        self.set_lead_time(LeadTimeSetting.Family.NORMAL_ROUNDING, 3)
        self.set_lead_time(LeadTimeSetting.Family.INTERVIEWS, 4)
        holiday = HolidayOccurrence.objects.create(
            holiday=ClinicHoliday.INDEPENDENCE_DAY,
            calendar_year=2026,
            name="Independence Day",
            nominal_date=date(2026, 3, 31),
            coverage=HolidayOccurrence.Coverage.WHOLE_DAY,
            state=HolidayOccurrence.State.UNSCHEDULED,
        )
        create_academic_event_definition(
            base_name="Quarterly event",
            start_academic_year=2026,
            expected_quarter="Q1",
            default_required_participant_count=1,
            scheduler=self.scheduler,
        )

        holiday_item = holiday_projected_work_items((holiday,))[0]
        academic_item = academic_event_projected_work_items(2026, ())[0]
        with (
            patch("schedules.services.preceptors.is_operational_preceptor_week", return_value=True),
            patch(
                "schedules.services.preceptors.operational_draft_findings_for_week",
                return_value=DraftFindingResult(
                    (DraftFinding("missing_late_doc", "hard", None, self.week, (), {}),)
                ),
            ),
        ):
            precepting_item = precepting_projected_work_items((self.week,))[0]
        with (
            patch("schedules.services.rounding.is_operational_preceptor_week", return_value=True),
            patch(
                "schedules.services.rounding.normal_rounder_statuses_for_weeks",
                return_value=(NormalRounderStatus(self.week, None, None, None, None, None),),
            ),
        ):
            rounding_item = normal_rounding_projected_work_items((self.week,))[0]
        with patch(
            "schedules.services.interviews.organization_today", return_value=date(2026, 7, 1)
        ):
            interview_item = interview_projected_work_items((2026,))[0]

        self.assertEqual(holiday_item.matters_on, date(2026, 2, 28))
        self.assertEqual(academic_item.matters_on, date(2026, 7, 1))
        self.assertEqual(precepting_item.matters_on, date(2026, 5, 6))
        self.assertEqual(rounding_item.matters_on, date(2026, 4, 6))
        self.assertEqual(interview_item.matters_on, date(2026, 3, 1))
        self.set_lead_time(LeadTimeSetting.Family.PRECEPTING, 0)
        with (
            patch("schedules.services.preceptors.is_operational_preceptor_week", return_value=True),
            patch(
                "schedules.services.preceptors.operational_draft_findings_for_week",
                return_value=DraftFindingResult(
                    (DraftFinding("missing_late_doc", "hard", None, self.week, (), {}),)
                ),
            ),
        ):
            changed_precepting_item = precepting_projected_work_items((self.week,))[0]

        self.assertEqual(changed_precepting_item.matters_on, self.week)
        self.assertEqual(rounding_item.matters_on, date(2026, 4, 6))
        self.assertEqual(changed_precepting_item.affected_range, precepting_item.affected_range)
        self.assertEqual(changed_precepting_item.reasons, precepting_item.reasons)

    def test_attention_boundaries_change_global_visibility_not_scoped_summaries(self):
        self.set_lead_time(LeadTimeSetting.Family.PRECEPTING, 2)
        with (
            patch("schedules.services.preceptors.is_operational_preceptor_week", return_value=True),
            patch(
                "schedules.services.preceptors.operational_draft_findings_for_week",
                return_value=DraftFindingResult(
                    (DraftFinding("missing_late_doc", "hard", None, self.week, (), {}),)
                ),
            ),
        ):
            item = precepting_projected_work_items((self.week,))[0]

        for today, expected_count in (
            (date(2026, 5, 5), 0),
            (date(2026, 5, 6), 1),
            (date(2026, 5, 7), 1),
        ):
            with (
                self.subTest(today=today),
                patch("schedules.services.work_remaining.organization_today", return_value=today),
            ):
                self.assertEqual(len(global_work_items((item,))), expected_count)

        summary = scoped_work_remaining(
            (item,), InclusiveDateRange(self.week, self.week), evaluated=True
        )
        self.assertEqual(summary.items, (item,))
        self.assertEqual(summary.required_item_count, 1)


class ConfiguredLeadTimeAggregateTests(TestCase):
    def setUp(self):
        self.administrator = get_user_model().objects.create_user(username="administrator")
        self.administrator.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )

    def test_global_holiday_boundary_uses_the_configured_horizon_without_altering_existing_occurrences(
        self,
    ):
        set_lead_time(
            family=LeadTimeSetting.Family.HOLIDAYS, months=1, scheduler=self.administrator
        )
        holiday = HolidayOccurrence.objects.create(
            holiday=ClinicHoliday.INDEPENDENCE_DAY,
            calendar_year=2026,
            name="Independence Day",
            nominal_date=date(2026, 3, 31),
            coverage=HolidayOccurrence.Coverage.WHOLE_DAY,
            state=HolidayOccurrence.State.UNSCHEDULED,
        )

        for today, expected_keys in (
            (date(2026, 2, 27), []),
            (date(2026, 2, 28), [f"holiday-{holiday.pk}"]),
            (date(2026, 3, 1), [f"holiday-{holiday.pk}"]),
        ):
            with (
                self.subTest(today=today),
                patch("schedules.services.work_remaining.organization_today", return_value=today),
            ):
                self.assertEqual(
                    [item.key for item in global_work_remaining().items], expected_keys
                )

        holiday.refresh_from_db()
        self.assertEqual(holiday.state, HolidayOccurrence.State.UNSCHEDULED)
        self.assertEqual(HolidayOccurrenceChange.objects.count(), 6)

    def test_global_interview_boundary_includes_future_year_with_configured_lead_time(self):
        set_lead_time(
            family=LeadTimeSetting.Family.INTERVIEWS, months=4, scheduler=self.administrator
        )

        for today, expected_keys in (
            (date(2026, 2, 28), []),
            (date(2026, 3, 1), ["interview-2026"]),
            (date(2026, 3, 2), ["interview-2026"]),
        ):
            with (
                self.subTest(today=today),
                patch("schedules.services.work_remaining.organization_today", return_value=today),
                patch("schedules.services.interviews.organization_today", return_value=today),
            ):
                self.assertEqual(
                    [
                        item.key
                        for item in global_work_remaining().items
                        if item.area == "interviews"
                    ],
                    expected_keys,
                )

    def test_global_work_includes_far_future_academic_event_work_only_with_its_configured_horizon(
        self,
    ):
        set_lead_time(
            family=LeadTimeSetting.Family.ACADEMIC_EVENTS,
            months=18,
            scheduler=self.administrator,
        )
        set_lead_time(
            family=LeadTimeSetting.Family.INTERVIEWS,
            months=0,
            scheduler=self.administrator,
        )
        create_academic_event_definition(
            base_name="Far-future Q1 event",
            start_academic_year=2027,
            expected_quarter="Q1",
            default_required_participant_count=1,
            scheduler=self.administrator,
        )
        create_academic_event_definition(
            base_name="Second far-future Q1 event",
            start_academic_year=2027,
            expected_quarter="Q1",
            default_required_participant_count=1,
            scheduler=self.administrator,
        )

        def global_academic_event_records():
            with (
                patch(
                    "schedules.services.work_remaining.organization_today",
                    return_value=date(2026, 1, 31),
                ),
                patch(
                    "schedules.services.academic_events.organization_today",
                    return_value=date(2026, 1, 31),
                ),
            ):
                return {
                    item.key: sum(
                        reason.quantity
                        for reason in item.reasons
                        if reason.classification == "required"
                    )
                    for item in global_work_remaining().items
                    if item.area == "academic-events"
                }

        self.assertEqual(
            global_academic_event_records(),
            {"academic-events-unscheduled-yearly-2027-Q1": 2},
        )

        set_lead_time(
            family=LeadTimeSetting.Family.ACADEMIC_EVENTS,
            months=17,
            scheduler=self.administrator,
        )

        self.assertEqual(global_academic_event_records(), {})

    def test_global_work_reads_each_family_through_its_own_far_future_horizon(self):
        """Each producer contributes only when its independently configured range reaches it."""
        today = date(2026, 1, 31)
        far_future_horizons = {
            LeadTimeSetting.Family.HOLIDAYS: 18,
            LeadTimeSetting.Family.ACADEMIC_EVENTS: 18,
            LeadTimeSetting.Family.PRECEPTING: 18,
            LeadTimeSetting.Family.NORMAL_ROUNDING: 17,
            LeadTimeSetting.Family.INTERVIEWS: 18,
        }
        for family, months in far_future_horizons.items():
            set_lead_time(family=family, months=months, scheduler=self.administrator)

        holiday = HolidayOccurrence.objects.create(
            holiday=ClinicHoliday.INDEPENDENCE_DAY,
            calendar_year=2027,
            name="Independence Day",
            nominal_date=date(2027, 7, 4),
            coverage=HolidayOccurrence.Coverage.WHOLE_DAY,
            state=HolidayOccurrence.State.UNSCHEDULED,
        )
        create_academic_event_definition(
            base_name="Included far-future Q1 event",
            start_academic_year=2027,
            expected_quarter="Q1",
            default_required_participant_count=1,
            scheduler=self.administrator,
        )
        create_academic_event_definition(
            base_name="Outside far-future Q1 event",
            start_academic_year=2028,
            expected_quarter="Q1",
            default_required_participant_count=1,
            scheduler=self.administrator,
        )

        def global_keys():
            with (
                patch("schedules.services.work_remaining.organization_today", return_value=today),
                patch(
                    "schedules.services.operational_attention.organization_today",
                    return_value=today,
                ),
                patch("schedules.services.academic_events.organization_today", return_value=today),
                patch("schedules.services.interviews.organization_today", return_value=today),
            ):
                return {item.key for item in global_work_remaining().items}

        included = {
            f"holiday-{holiday.pk}",
            "academic-events-unscheduled-yearly-2027-Q1",
            "precepting-week-2027-07-26",
            "rounding-week-2027-06-28",
            "interview-2027",
        }
        self.assertTrue(included <= global_keys())
        self.assertNotIn("academic-events-unscheduled-yearly-2028-Q1", global_keys())

        for family, shorter_months in (
            (LeadTimeSetting.Family.HOLIDAYS, 17),
            (LeadTimeSetting.Family.ACADEMIC_EVENTS, 17),
            (LeadTimeSetting.Family.PRECEPTING, 17),
            (LeadTimeSetting.Family.NORMAL_ROUNDING, 16),
            (LeadTimeSetting.Family.INTERVIEWS, 17),
        ):
            set_lead_time(
                family=family,
                months=shorter_months,
                scheduler=self.administrator,
            )

        shortened = global_keys()
        self.assertTrue(included.isdisjoint(shortened))
