from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from schedules.models import ClinicHoliday, HolidayOccurrence, HolidayOccurrenceChange
from schedules.services.holidays import holiday_occurrences_for, schedule_holiday_occurrence


class HolidayOccurrenceTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(username="scheduler")

    def occurrence(self, calendar_year, holiday):
        holiday_occurrences_for(calendar_year)
        return HolidayOccurrence.objects.get(calendar_year=calendar_year, holiday=holiday)

    def test_materializes_fixed_definitions_with_stable_computed_defaults(self):
        occurrences = list(holiday_occurrences_for(2026))

        self.assertEqual(len(occurrences), 7)
        self.assertEqual(
            {
                occurrence.holiday: (
                    occurrence.nominal_date,
                    occurrence.observed_date,
                    occurrence.coverage,
                    occurrence.state,
                )
                for occurrence in occurrences
            },
            {
                ClinicHoliday.NEW_YEARS_DAY: (
                    date(2026, 1, 1),
                    date(2026, 1, 1),
                    HolidayOccurrence.Coverage.WHOLE_DAY,
                    HolidayOccurrence.State.COMPUTED,
                ),
                ClinicHoliday.MEMORIAL_DAY: (
                    date(2026, 5, 25),
                    date(2026, 5, 25),
                    HolidayOccurrence.Coverage.WHOLE_DAY,
                    HolidayOccurrence.State.COMPUTED,
                ),
                ClinicHoliday.INDEPENDENCE_DAY: (
                    date(2026, 7, 4),
                    None,
                    HolidayOccurrence.Coverage.WHOLE_DAY,
                    HolidayOccurrence.State.UNSCHEDULED,
                ),
                ClinicHoliday.LABOR_DAY: (
                    date(2026, 9, 7),
                    date(2026, 9, 7),
                    HolidayOccurrence.Coverage.WHOLE_DAY,
                    HolidayOccurrence.State.COMPUTED,
                ),
                ClinicHoliday.THANKSGIVING_DAY: (
                    date(2026, 11, 26),
                    date(2026, 11, 26),
                    HolidayOccurrence.Coverage.WHOLE_DAY,
                    HolidayOccurrence.State.COMPUTED,
                ),
                ClinicHoliday.CHRISTMAS_EVE: (
                    date(2026, 12, 24),
                    date(2026, 12, 24),
                    HolidayOccurrence.Coverage.PM,
                    HolidayOccurrence.State.COMPUTED,
                ),
                ClinicHoliday.CHRISTMAS_DAY: (
                    date(2026, 12, 25),
                    date(2026, 12, 25),
                    HolidayOccurrence.Coverage.WHOLE_DAY,
                    HolidayOccurrence.State.COMPUTED,
                ),
            },
        )
        self.assertEqual(HolidayOccurrenceChange.objects.count(), 7)
        self.assertEqual(len(holiday_occurrences_for(2026)), 7)
        self.assertEqual(HolidayOccurrenceChange.objects.count(), 7)

        self.assertEqual(
            self.occurrence(2022, ClinicHoliday.NEW_YEARS_DAY).observed_date, date(2021, 12, 31)
        )
        self.assertEqual(
            self.occurrence(2023, ClinicHoliday.NEW_YEARS_DAY).observed_date, date(2023, 1, 2)
        )
        self.assertEqual(
            self.occurrence(2021, ClinicHoliday.CHRISTMAS_DAY).observed_date, date(2021, 12, 24)
        )
        self.assertEqual(
            self.occurrence(2022, ClinicHoliday.CHRISTMAS_DAY).observed_date, date(2022, 12, 26)
        )

    def test_weekend_independence_and_christmas_eve_retain_distinct_states(self):
        independence_day = self.occurrence(2021, ClinicHoliday.INDEPENDENCE_DAY)
        christmas_eve = self.occurrence(2022, ClinicHoliday.CHRISTMAS_EVE)

        self.assertEqual(independence_day.state, HolidayOccurrence.State.UNSCHEDULED)
        self.assertIsNone(independence_day.observed_date)
        self.assertEqual(christmas_eve.state, HolidayOccurrence.State.NOT_OBSERVED)
        self.assertIsNone(christmas_eve.observed_date)
        self.assertEqual(christmas_eve.coverage, HolidayOccurrence.Coverage.PM)

    def test_scheduling_and_historical_correction_preserve_coverage_and_audit(self):
        occurrence = self.occurrence(2021, ClinicHoliday.INDEPENDENCE_DAY)

        occurrence = schedule_holiday_occurrence(
            occurrence=occurrence,
            observed_date=date(2021, 7, 5),
            scheduler=self.scheduler,
            note="Observed Monday",
        )
        self.assertEqual(occurrence.state, HolidayOccurrence.State.SCHEDULED)
        self.assertEqual(occurrence.observed_date, date(2021, 7, 5))
        self.assertEqual(occurrence.coverage, HolidayOccurrence.Coverage.WHOLE_DAY)
        self.assertEqual(occurrence.source, "Scheduled")
        self.assertEqual(
            HolidayOccurrence.objects.filter(
                calendar_year=2021, holiday=ClinicHoliday.INDEPENDENCE_DAY
            ).count(),
            1,
        )

        occurrence = schedule_holiday_occurrence(
            occurrence=occurrence,
            observed_date=date(2021, 7, 2),
            scheduler=self.scheduler,
            note="Historical correction",
        )
        self.assertEqual(occurrence.observed_date, date(2021, 7, 2))
        changes = list(occurrence.changes.order_by("pk"))
        self.assertEqual(
            [change.action for change in changes], ["materialized", "scheduled", "corrected"]
        )
        self.assertEqual(changes[-1].previous_value["observed_date"], "2021-07-05")
        self.assertEqual(changes[-1].recorded_value["observed_date"], "2021-07-02")
        self.assertEqual(changes[-1].changed_by, self.scheduler)
        self.assertEqual(changes[-1].note, "Historical correction")

        retained = self.occurrence(2021, ClinicHoliday.INDEPENDENCE_DAY)
        self.assertEqual(retained.observed_date, date(2021, 7, 2))

    def test_only_designated_holidays_accept_manual_scheduling(self):
        memorial_day = self.occurrence(2026, ClinicHoliday.MEMORIAL_DAY)
        occurrence_before_rejection = {
            "holiday": memorial_day.holiday,
            "calendar_year": memorial_day.calendar_year,
            "name": memorial_day.name,
            "nominal_date": memorial_day.nominal_date,
            "observed_date": memorial_day.observed_date,
            "coverage": memorial_day.coverage,
            "state": memorial_day.state,
            "source": memorial_day.source,
        }
        history_before_rejection = list(
            memorial_day.changes.order_by("pk").values(
                "action", "previous_value", "recorded_value", "note", "changed_by_id"
            )
        )

        with self.assertRaises(ValidationError):
            schedule_holiday_occurrence(
                occurrence=memorial_day,
                observed_date=date(2026, 5, 22),
                scheduler=self.scheduler,
            )

        memorial_day.refresh_from_db()
        self.assertEqual(
            {
                "holiday": memorial_day.holiday,
                "calendar_year": memorial_day.calendar_year,
                "name": memorial_day.name,
                "nominal_date": memorial_day.nominal_date,
                "observed_date": memorial_day.observed_date,
                "coverage": memorial_day.coverage,
                "state": memorial_day.state,
                "source": memorial_day.source,
            },
            occurrence_before_rejection,
        )
        self.assertEqual(
            list(
                memorial_day.changes.order_by("pk").values(
                    "action", "previous_value", "recorded_value", "note", "changed_by_id"
                )
            ),
            history_before_rejection,
        )
