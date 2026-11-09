from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from schedules.models import AcademicEvent, ClinicHoliday, HolidayOccurrence, LeadTimeSetting
from schedules.services.academic_events import (
    academic_event_projected_work_items,
    create_academic_event_definition,
    create_academic_event_monthly_rule,
    create_academic_event_occurrence,
    initialize_monthly_academic_event_occurrences,
    record_academic_event_not_occurring,
)
from schedules.services.holidays import (
    holiday_occurrences_for,
    holiday_projected_work_items,
    schedule_holiday_occurrence,
)
from schedules.services.lead_times import set_lead_time
from schedules.services.work_remaining import (
    InclusiveDateRange,
    focused_work_remaining,
    global_work_items,
    global_work_remaining,
    scoped_work_remaining,
    shift_date_months,
)


class ProjectedAcademicEventWorkTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(username="scheduler")

    def definition(self, **overrides):
        values = {
            "base_name": "Quarterly event",
            "start_academic_year": 2026,
            "expected_quarter": "Q2",
            "default_required_participant_count": 2,
            "scheduler": self.scheduler,
        }
        values.update(overrides)
        return create_academic_event_definition(**values)

    def test_unscheduled_and_underfilled_event_share_identity_and_definition_anchor(self):
        definition = self.definition()
        unscheduled = academic_event_projected_work_items(2027, ())

        self.assertEqual(len(unscheduled), 1)
        self.assertEqual(unscheduled[0].key, f"academic-event-definition-{definition.pk}-2027")
        self.assertEqual(unscheduled[0].matters_on, date(2027, 4, 1))
        self.assertEqual(unscheduled[0].affected_range.start, date(2027, 10, 1))
        self.assertEqual(
            unscheduled[0].target_url,
            f"{reverse('academic-event-create')}?academic_year=2027&definition={definition.pk}",
        )
        self.assertEqual(
            (unscheduled[0].reasons[0].classification, unscheduled[0].reasons[0].quantity),
            ("required", 1),
        )

        event = create_academic_event_occurrence(
            definition=definition,
            academic_year=2027,
            start_date=date(2027, 12, 31),
            end_date=date(2027, 12, 31),
            scheduler=self.scheduler,
        )
        underfilled = academic_event_projected_work_items(2027, (event,))

        self.assertEqual(len(underfilled), 1)
        self.assertEqual(underfilled[0].key, unscheduled[0].key)
        self.assertEqual(underfilled[0].matters_on, unscheduled[0].matters_on)
        self.assertEqual(underfilled[0].affected_range.start, date(2027, 12, 31))
        self.assertEqual(
            underfilled[0].target_url, reverse("academic-event-correct", args=(event.pk,))
        )
        self.assertEqual(
            (underfilled[0].reasons[0].classification, underfilled[0].reasons[0].quantity),
            ("required", 2),
        )

    def test_academic_event_resolution_and_global_boundaries(self):
        definition = self.definition(expected_quarter="Q4")
        items = academic_event_projected_work_items(2026, ())

        self.assertEqual(items[0].matters_on, date(2026, 10, 1))
        self.assertEqual(
            items[0].affected_range,
            InclusiveDateRange(date(2027, 4, 1), date(2027, 6, 30)),
        )
        scoped = scoped_work_remaining(items, items[0].affected_range, evaluated=False)
        self.assertEqual(scoped.items, items)

        for today, expected_count in (
            (date(2026, 9, 30), 0),
            (date(2026, 10, 1), 1),
            (date(2027, 6, 30), 1),
            (date(2027, 7, 1), 0),
        ):
            with (
                self.subTest(today=today),
                patch("schedules.services.work_remaining.organization_today", return_value=today),
            ):
                self.assertEqual(len(global_work_items(items)), expected_count)

        record_academic_event_not_occurring(
            definition=definition, academic_year=2026, scheduler=self.scheduler
        )
        events = AcademicEvent.objects.filter(academic_year=2026, is_active=True)
        self.assertEqual(academic_event_projected_work_items(2026, events), ())

    def test_fully_populated_event_produces_no_work(self):
        definition = self.definition()
        physicians = [
            self._physician("First physician"),
            self._physician("Second physician"),
        ]
        event = create_academic_event_occurrence(
            definition=definition,
            academic_year=2026,
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 1),
            participants=physicians,
            scheduler=self.scheduler,
        )

        self.assertEqual(academic_event_projected_work_items(2026, (event,)), ())

    def test_global_buckets_follow_unscheduled_quarters_and_first_scheduled_month(self):
        yearly_definition = self.definition(base_name="Yearly event")
        unscheduled = academic_event_projected_work_items(2026, ())

        self.assertEqual(unscheduled[0].global_bucket.label, "Unscheduled Yearly")
        self.assertEqual(
            unscheduled[0].global_bucket.affected_range,
            InclusiveDateRange(date(2026, 10, 1), date(2026, 12, 31)),
        )
        self.assertEqual(
            unscheduled[0].global_bucket.target_url,
            f"{reverse('academic-event-list')}?academic_year=2026",
        )

        yearly_event = create_academic_event_occurrence(
            definition=yearly_definition,
            academic_year=2026,
            start_date=date(2026, 10, 5),
            end_date=date(2026, 10, 5),
            scheduler=self.scheduler,
        )
        monthly_definition = self.definition(
            base_name="Monthly event", scheduling_mode="monthly_rules"
        )
        rule = create_academic_event_monthly_rule(
            definition=monthly_definition, weekday=3, ordinals=[1], scheduler=self.scheduler
        )
        monthly_event = initialize_monthly_academic_event_occurrences(
            start_date=date(2026, 10, 1), end_date=date(2026, 10, 31)
        )[0]

        scheduled = academic_event_projected_work_items(2026, (yearly_event, monthly_event))

        self.assertEqual(len(scheduled), 2)
        self.assertTrue(
            all(item.global_bucket.label == "Scheduled and Monthly" for item in scheduled)
        )
        self.assertTrue(all(item.global_bucket == scheduled[0].global_bucket for item in scheduled))
        self.assertEqual(
            scheduled[0].global_bucket.affected_range,
            InclusiveDateRange(date(2026, 10, 1), date(2026, 10, 31)),
        )
        self.assertEqual(monthly_event.monthly_rule, rule)

    def test_global_yearly_work_uses_configured_lead_time_and_expected_quarter_anchor(self):
        administrator = get_user_model().objects.create_user(username="administrator")
        administrator.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )
        unscheduled_definition = self.definition(
            base_name="Unscheduled Q1 2027",
            start_academic_year=2027,
            expected_quarter="Q1",
        )
        dated_definition = self.definition(
            base_name="Dated Q1 2027",
            start_academic_year=2027,
            expected_quarter="Q1",
        )
        self.definition(
            base_name="Outside Q1 2028",
            start_academic_year=2028,
            expected_quarter="Q1",
        )
        dated_event = create_academic_event_occurrence(
            definition=dated_definition,
            academic_year=2027,
            start_date=date(2027, 12, 15),
            end_date=date(2027, 12, 15),
            scheduler=self.scheduler,
        )
        expected_quarter_range = InclusiveDateRange(date(2027, 7, 1), date(2027, 9, 30))
        dated_range = InclusiveDateRange(date(2027, 12, 15), date(2027, 12, 15))
        expected_global_records = {
            "academic-events-unscheduled-yearly-2027-Q1": (expected_quarter_range, (1,)),
            "academic-events-scheduled-and-monthly-2027-2027-12": (
                InclusiveDateRange(date(2027, 12, 1), date(2027, 12, 31)),
                (2,),
            ),
        }

        set_lead_time(
            family=LeadTimeSetting.Family.ACADEMIC_EVENTS,
            months=18,
            scheduler=administrator,
        )

        projected = {
            item.key: item for item in academic_event_projected_work_items(2027, (dated_event,))
        }
        self.assertEqual(
            projected[f"academic-event-definition-{unscheduled_definition.pk}-2027"].affected_range,
            expected_quarter_range,
        )
        self.assertEqual(
            projected[f"academic-event-definition-{unscheduled_definition.pk}-2027"].matters_on,
            date(2026, 1, 1),
        )
        self.assertEqual(
            projected[f"academic-event-definition-{unscheduled_definition.pk}-2027"]
            .reasons[0]
            .quantity,
            1,
        )
        self.assertEqual(
            projected[f"academic-event-definition-{dated_definition.pk}-2027"].affected_range,
            dated_range,
        )
        self.assertEqual(
            projected[f"academic-event-definition-{dated_definition.pk}-2027"].matters_on,
            date(2026, 1, 1),
        )
        self.assertEqual(
            projected[f"academic-event-definition-{dated_definition.pk}-2027"].reasons[0].quantity,
            2,
        )

        def academic_global_records(today):
            with (
                patch("schedules.services.work_remaining.organization_today", return_value=today),
                patch("schedules.services.academic_events.organization_today", return_value=today),
            ):
                return {
                    item.key: (
                        item.affected_range,
                        tuple(
                            reason.quantity
                            for reason in item.reasons
                            if reason.classification == "required"
                        ),
                    )
                    for item in global_work_remaining().items
                    if item.area == "academic-events"
                }

        self.assertEqual(academic_global_records(date(2025, 12, 31)), {})
        self.assertEqual(academic_global_records(date(2026, 1, 1)), expected_global_records)
        self.assertEqual(academic_global_records(date(2026, 1, 2)), expected_global_records)
        on_boundary = academic_global_records(date(2026, 1, 31))
        self.assertEqual(on_boundary, expected_global_records)
        self.assertNotIn(
            "academic-events-unscheduled-yearly-2028-Q1",
            on_boundary,
        )

        set_lead_time(
            family=LeadTimeSetting.Family.ACADEMIC_EVENTS,
            months=17,
            scheduler=administrator,
        )
        self.assertEqual(academic_global_records(date(2026, 1, 31)), {})

        set_lead_time(
            family=LeadTimeSetting.Family.ACADEMIC_EVENTS,
            months=0,
            scheduler=administrator,
        )
        self.assertEqual(academic_global_records(date(2026, 1, 31)), {})
        reset_projected = tuple(academic_event_projected_work_items(2027, (dated_event,)))
        expected_focused_keys = {
            f"academic-event-definition-{unscheduled_definition.pk}-2027",
            f"academic-event-definition-{dated_definition.pk}-2027",
        }
        with patch(
            "schedules.services.work_remaining.organization_today", return_value=date(2026, 1, 31)
        ):
            self.assertEqual(
                {
                    item.key
                    for item in focused_work_remaining(
                        reset_projected,
                        InclusiveDateRange(date(2027, 7, 1), date(2028, 6, 30)),
                    ).items
                },
                expected_focused_keys,
            )

    def test_monthly_work_uses_its_start_date_lead_time_in_global_and_focused_projections(self):
        administrator = get_user_model().objects.create_user(username="administrator")
        administrator.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )
        definition = self.definition(
            base_name="Monthly event",
            scheduling_mode="monthly_rules",
        )
        rule = create_academic_event_monthly_rule(
            definition=definition,
            weekday=1,
            ordinals=[2],
            scheduler=self.scheduler,
        )
        collective_definition = self.definition(
            base_name="Collective monthly event",
            scheduling_mode="monthly_rules",
            all_available_faculty=True,
        )
        create_academic_event_monthly_rule(
            definition=collective_definition,
            weekday=1,
            ordinals=[2],
            scheduler=self.scheduler,
        )
        initialize_monthly_academic_event_occurrences(
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 30)
        )
        occurrence = AcademicEvent.objects.get(definition=definition, start_date=date(2026, 9, 8))
        collective_occurrence = AcademicEvent.objects.get(
            definition=collective_definition, start_date=date(2026, 9, 8)
        )
        self.assertEqual(occurrence.monthly_rule, rule)

        set_lead_time(
            family=LeadTimeSetting.Family.ACADEMIC_EVENTS,
            months=6,
            scheduler=administrator,
        )
        items = academic_event_projected_work_items(2026, (occurrence, collective_occurrence))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].key, f"academic-event-occurrence-{occurrence.pk}")
        self.assertEqual(items[0].reasons[0].classification, "required")
        self.assertEqual(items[0].matters_on, date(2026, 3, 8))
        self.assertEqual(items[0].global_bucket.label, "Scheduled and Monthly")
        self.assertEqual(
            items[0].global_bucket.affected_range,
            InclusiveDateRange(date(2026, 9, 1), date(2026, 9, 30)),
        )

        def global_monthly_items(today):
            with (
                patch("schedules.services.work_remaining.organization_today", return_value=today),
                patch("schedules.services.academic_events.organization_today", return_value=today),
            ):
                return tuple(
                    item
                    for item in global_work_remaining().items
                    if item.key == "academic-events-scheduled-and-monthly-2026-2026-09"
                )

        self.assertEqual(global_monthly_items(date(2026, 3, 7)), ())
        global_items = global_monthly_items(date(2026, 3, 8))
        self.assertEqual(len(global_items), 1)
        self.assertEqual(global_items[0].matters_on, date(2026, 3, 8))
        self.assertEqual(global_monthly_items(date(2026, 8, 31)), global_items)

        set_lead_time(
            family=LeadTimeSetting.Family.ACADEMIC_EVENTS,
            months=0,
            scheduler=administrator,
        )
        zero_lead_items = academic_event_projected_work_items(
            2026, (occurrence, collective_occurrence)
        )
        self.assertEqual(zero_lead_items[0].matters_on, date(2026, 9, 8))
        self.assertEqual(global_monthly_items(date(2026, 8, 31)), ())
        with patch(
            "schedules.services.work_remaining.organization_today", return_value=date(2026, 8, 31)
        ):
            self.assertEqual(
                focused_work_remaining(
                    zero_lead_items,
                    InclusiveDateRange(date(2026, 9, 1), date(2026, 9, 30)),
                ).items,
                zero_lead_items,
            )

        occurrence.participants.set(
            (self._physician("First physician"), self._physician("Second physician"))
        )
        self.assertEqual(
            academic_event_projected_work_items(2026, (occurrence, collective_occurrence)), ()
        )

    def _physician(self, name):
        from schedules.models import Physician

        return Physician.objects.create(display_name=name)


class ProjectedHolidayWorkTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(username="scheduler")

    def test_month_shifting_clamps_end_of_month_across_years(self):
        self.assertEqual(shift_date_months(date(2026, 8, 31), -18), date(2025, 2, 28))
        self.assertEqual(shift_date_months(date(2024, 8, 31), -18), date(2023, 2, 28))
        self.assertEqual(shift_date_months(date(2024, 3, 31), -1), date(2024, 2, 29))

    def test_only_unresolved_manually_schedulable_holidays_project_and_expire(self):
        occurrences = tuple(holiday_occurrences_for(2026))
        independence_day = next(
            occurrence
            for occurrence in occurrences
            if occurrence.holiday == ClinicHoliday.INDEPENDENCE_DAY
        )
        items = holiday_projected_work_items(occurrences)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].key, f"holiday-{independence_day.pk}")
        self.assertEqual(items[0].matters_on, date(2025, 1, 4))
        self.assertEqual(items[0].affected_range.start, date(2026, 7, 4))
        self.assertEqual(
            items[0].target_url,
            reverse("holiday-occurrence-schedule", args=(independence_day.pk,)),
        )
        self.assertEqual(items[0].reasons[0].quantity, 1)

        for today, expected_count in (
            (date(2025, 1, 3), 0),
            (date(2025, 1, 4), 1),
            (date(2026, 7, 4), 1),
            (date(2026, 7, 5), 0),
        ):
            with (
                self.subTest(today=today),
                patch("schedules.services.work_remaining.organization_today", return_value=today),
            ):
                self.assertEqual(len(global_work_items(items)), expected_count)

        schedule_holiday_occurrence(
            occurrence=independence_day,
            observed_date=date(2026, 7, 3),
            scheduler=self.scheduler,
        )
        independence_day.refresh_from_db()
        self.assertEqual(holiday_projected_work_items((independence_day,)), ())

    def test_computed_and_not_observed_holidays_produce_no_work(self):
        computed = HolidayOccurrence.objects.create(
            holiday=ClinicHoliday.NEW_YEARS_DAY,
            calendar_year=2026,
            name="New Year's Day",
            nominal_date=date(2026, 1, 1),
            observed_date=date(2026, 1, 1),
            coverage=HolidayOccurrence.Coverage.WHOLE_DAY,
            state=HolidayOccurrence.State.COMPUTED,
        )
        not_observed = HolidayOccurrence.objects.create(
            holiday=ClinicHoliday.CHRISTMAS_EVE,
            calendar_year=2026,
            name="Christmas Eve",
            nominal_date=date(2026, 12, 24),
            coverage=HolidayOccurrence.Coverage.PM,
            state=HolidayOccurrence.State.NOT_OBSERVED,
        )

        self.assertEqual(holiday_projected_work_items((computed, not_observed)), ())
