from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from schedules.academic_years import expected_quarter_start
from schedules.models import AcademicEvent, AcademicEventChange, AcademicEventDefinitionChange
from schedules.services.academic_events import (
    academic_event_definitions_for,
    academic_events_covering,
    correct_academic_event_definition,
    correct_academic_event_occurrence,
    create_academic_event_definition,
    create_academic_event_occurrence,
    deactivate_academic_event_definition,
    record_academic_event_not_occurring,
    remove_academic_event_occurrence,
    reverse_academic_event_not_occurring,
    schedulable_academic_event_definitions_for,
)


class AnnualAcademicEventLifecycleTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(username="scheduler")
        self.physicians = [
            self._physician("First physician"),
            self._physician("Second physician"),
            self._physician("Third physician"),
        ]
        self.definition = create_academic_event_definition(
            base_name="Grand rounds",
            start_academic_year=2026,
            default_duration_days=2,
            default_period_of_day=AcademicEvent.PeriodOfDay.AM,
            default_required_participant_count=2,
            scheduler=self.scheduler,
        )

    def _physician(self, name):
        from schedules.models import Physician

        return Physician.objects.create(display_name=name)

    def create_occurrence(self, **overrides):
        values = {
            "definition": self.definition,
            "academic_year": 2026,
            "start_date": date(2026, 8, 10),
            "end_date": date(2026, 8, 11),
            "scheduler": self.scheduler,
        }
        values.update(overrides)
        return create_academic_event_occurrence(**values)

    def test_occurrence_snapshots_definition_name_and_requirement_and_tracks_population_states(
        self,
    ):
        event = self.create_occurrence()
        self.assertEqual(event.title, "Grand rounds")
        self.assertEqual(event.required_participant_count, 2)
        self.assertEqual(event.expected_quarter, "Q1")
        self.assertEqual(event.participant_status, "Scheduled, unpopulated")

        event = correct_academic_event_occurrence(
            event=event,
            start_date=event.start_date,
            end_date=event.end_date,
            suffix="  Visiting faculty  ",
            required_participant_count=3,
            period_of_day=event.period_of_day,
            participants=self.physicians[:1],
            scheduler=self.scheduler,
        )
        self.assertEqual(event.title, "Grand rounds - Visiting faculty")
        self.assertEqual(event.participant_status, "Scheduled, needs participants")
        event = correct_academic_event_occurrence(
            event=event,
            start_date=event.start_date,
            end_date=event.end_date,
            suffix=event.suffix,
            required_participant_count=3,
            period_of_day=event.period_of_day,
            participants=self.physicians,
            scheduler=self.scheduler,
        )
        self.assertEqual(event.participant_status, "Complete")
        event = correct_academic_event_occurrence(
            event=event,
            start_date=event.start_date,
            end_date=event.end_date,
            suffix=event.suffix,
            required_participant_count=3,
            period_of_day=event.period_of_day,
            participants=[*self.physicians, self._physician("Fourth physician")],
            scheduler=self.scheduler,
        )
        self.assertEqual(event.participant_status, "Complete")

        correct_academic_event_definition(
            definition=self.definition,
            base_name="Renamed rounds",
            start_academic_year=2026,
            default_duration_days=1,
            default_period_of_day=AcademicEvent.PeriodOfDay.PM,
            default_required_participant_count=4,
            expected_quarter="Q4",
            scheduler=self.scheduler,
        )
        definition_change = AcademicEventDefinitionChange.objects.get(action="corrected")
        self.assertEqual(
            definition_change.previous_value["default_required_participant_count"],
            2,
        )
        self.assertEqual(
            definition_change.recorded_value["default_required_participant_count"],
            4,
        )
        self.assertEqual(definition_change.previous_value["default_duration_days"], 2)
        self.assertEqual(definition_change.recorded_value["default_duration_days"], 1)
        self.assertEqual(definition_change.previous_value["default_period_of_day"], "AM")
        self.assertEqual(definition_change.recorded_value["default_period_of_day"], "PM")
        event.refresh_from_db()
        self.assertEqual(event.title, "Grand rounds - Visiting faculty")
        self.assertEqual(event.required_participant_count, 3)
        self.assertEqual(event.expected_quarter, "Q1")
        self.assertEqual(event.start_date, date(2026, 8, 10))
        self.assertEqual(event.end_date, date(2026, 8, 11))
        self.assertEqual(event.period_of_day, AcademicEvent.PeriodOfDay.AM)

    def test_occurrence_snapshots_impact_and_coverage_and_validates_their_pair(self):
        definition = create_academic_event_definition(
            base_name="Calendar event",
            start_academic_year=2026,
            default_period_of_day=AcademicEvent.PeriodOfDay.PM,
            default_precepting_impact=AcademicEvent.PreceptingImpact.SOFT,
            scheduler=self.scheduler,
        )

        event = create_academic_event_occurrence(
            definition=definition,
            academic_year=2026,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 10),
            participants=self.physicians[:1],
            scheduler=self.scheduler,
        )

        self.assertEqual(event.period_of_day, AcademicEvent.PeriodOfDay.PM)
        self.assertEqual(event.precepting_impact, AcademicEvent.PreceptingImpact.SOFT)
        self.assertEqual(
            AcademicEventChange.objects.get(action="created").recorded_value["precepting_impact"],
            "soft",
        )
        with self.assertRaises(ValidationError):
            create_academic_event_occurrence(
                definition=definition,
                academic_year=2027,
                start_date=date(2027, 8, 10),
                end_date=date(2027, 8, 10),
                period_of_day=AcademicEvent.PeriodOfDay.NONE,
                precepting_impact=AcademicEvent.PreceptingImpact.HARD,
                scheduler=self.scheduler,
            )

    def test_occurrences_and_not_occurring_decisions_snapshot_the_expected_quarter(self):
        for quarter in ("Q1", "Q2", "Q3", "Q4"):
            with self.subTest(quarter=quarter):
                definition = create_academic_event_definition(
                    base_name=f"{quarter} event",
                    start_academic_year=2026,
                    expected_quarter=quarter,
                    scheduler=self.scheduler,
                )
                start = expected_quarter_start(2026, quarter)
                event = create_academic_event_occurrence(
                    definition=definition,
                    academic_year=2026,
                    start_date=start,
                    end_date=start,
                    scheduler=self.scheduler,
                )
                self.assertEqual(event.expected_quarter, quarter)

        decision = record_academic_event_not_occurring(
            definition=self.definition, academic_year=2027, scheduler=self.scheduler
        )
        self.assertEqual(decision.expected_quarter, "Q1")

    def test_out_of_quarter_dates_are_accepted_without_reinterpreting_the_snapshot(self):
        definition = create_academic_event_definition(
            base_name="Spring planning",
            start_academic_year=2026,
            expected_quarter="Q4",
            scheduler=self.scheduler,
        )

        event = create_academic_event_occurrence(
            definition=definition,
            academic_year=2026,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 10),
            scheduler=self.scheduler,
        )

        self.assertEqual(event.expected_quarter, "Q4")
        self.assertEqual(event.start_date, date(2026, 8, 10))

    def test_dates_must_remain_in_the_selected_academic_year(self):
        with self.assertRaises(ValidationError):
            self.create_occurrence(end_date=date(2027, 7, 1))
        with self.assertRaises(ValidationError):
            self.create_occurrence(start_date=date(2027, 6, 30), end_date=date(2027, 6, 29))

    def test_active_resolution_blocks_duplicate_choices_and_retention_reopens_them(self):
        event = self.create_occurrence(participants=self.physicians[:1])
        self.assertEqual(list(schedulable_academic_event_definitions_for(2026)), [])
        self.assertEqual(self.create_occurrence().pk, event.pk)

        remove_academic_event_occurrence(event=event, scheduler=self.scheduler)
        event.refresh_from_db()
        self.assertFalse(event.is_active)
        self.assertEqual(list(schedulable_academic_event_definitions_for(2026)), [self.definition])
        replacement = self.create_occurrence(participants=self.physicians[:2])
        self.assertNotEqual(replacement.pk, event.pk)
        self.assertEqual(AcademicEvent.objects.count(), 2)
        self.assertEqual(
            list(AcademicEventChange.objects.order_by("pk").values_list("action", flat=True)),
            ["created", "removed", "created"],
        )

    def test_not_occurring_is_retained_and_reversible_without_availability_effect(self):
        decision = record_academic_event_not_occurring(
            definition=self.definition, academic_year=2026, scheduler=self.scheduler
        )
        self.assertEqual(decision.participant_status, "Not occurring this year")
        self.assertIsNone(decision.start_date)
        self.assertEqual(
            list(academic_events_covering(self.physicians[0], date(2026, 8, 10), "AM")), []
        )
        self.assertEqual(list(schedulable_academic_event_definitions_for(2026)), [])

        reverse_academic_event_not_occurring(event=decision, scheduler=self.scheduler)
        decision.refresh_from_db()
        self.assertFalse(decision.is_active)
        self.assertEqual(list(schedulable_academic_event_definitions_for(2026)), [self.definition])
        self.assertEqual(
            list(AcademicEventChange.objects.order_by("pk").values_list("action", flat=True)),
            ["not_occurring", "reversed"],
        )

    def test_retained_use_protects_the_definition_and_dated_occurrences_block_availability(self):
        event = self.create_occurrence(participants=self.physicians[:1])
        self.assertEqual(
            list(academic_events_covering(self.physicians[0], date(2026, 8, 10), "AM")), [event]
        )
        remove_academic_event_occurrence(event=event, scheduler=self.scheduler)
        self.assertEqual(
            list(academic_events_covering(self.physicians[0], date(2026, 8, 10), "AM")), []
        )
        with self.assertRaises(ValidationError):
            deactivate_academic_event_definition(
                definition=self.definition, scheduler=self.scheduler
            )
        with self.assertRaises(ValidationError):
            correct_academic_event_definition(
                definition=self.definition,
                base_name=self.definition.base_name,
                start_academic_year=2027,
                default_required_participant_count=2,
                scheduler=self.scheduler,
            )
        self.assertEqual(list(academic_event_definitions_for(2026)), [self.definition])

    def test_disjoint_ranges_are_retained_in_history_and_do_not_cover_the_gap(self):
        event = self.create_occurrence(
            date_ranges=(
                (date(2026, 8, 10), date(2026, 8, 10)),
                (date(2026, 8, 12), date(2026, 8, 13)),
            ),
            participants=self.physicians[:1],
        )

        self.assertEqual(
            event.date_ranges,
            ((date(2026, 8, 10), date(2026, 8, 10)), (date(2026, 8, 12), date(2026, 8, 13))),
        )
        self.assertFalse(event.covers(date(2026, 8, 11), "AM"))
        corrected = correct_academic_event_occurrence(
            event=event,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 13),
            date_ranges=(
                (date(2026, 8, 10), date(2026, 8, 10)),
                (date(2026, 8, 13), date(2026, 8, 13)),
            ),
            participants=self.physicians[:1],
            scheduler=self.scheduler,
        )
        change = AcademicEventChange.objects.get(action=AcademicEventChange.Action.CORRECTED)
        self.assertEqual(len(change.previous_value["date_ranges"]), 2)
        self.assertEqual(corrected.date_ranges[-1], (date(2026, 8, 13), date(2026, 8, 13)))

    def test_same_name_definitions_require_distinct_quarters(self):
        create_academic_event_definition(
            base_name="Orientation",
            start_academic_year=2026,
            expected_quarter="Q1",
            scheduler=self.scheduler,
        )
        q2 = create_academic_event_definition(
            base_name="orientation",
            start_academic_year=2026,
            expected_quarter="Q2",
            scheduler=self.scheduler,
        )
        self.assertEqual(q2.expected_quarter, "Q2")
        with self.assertRaises(ValidationError):
            create_academic_event_definition(
                base_name="Orientation",
                start_academic_year=2026,
                expected_quarter="Q1",
                scheduler=self.scheduler,
            )
