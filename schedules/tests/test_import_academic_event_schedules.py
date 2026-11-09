import re
from datetime import date
from io import BytesIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook

from schedules.models import (
    AcademicEvent,
    AcademicEventChange,
    AcademicEventDefinition,
    AcademicEventMonthlyRule,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    Physician,
)
from schedules.services.academic_event_definition_import import (
    AcademicEventDefinitionImportError,
    apply_academic_event_definition_import,
    build_academic_event_definition_import_plan,
    load_academic_event_definition_import,
)
from schedules.services.academic_event_schedule_import import (
    AcademicEventScheduleImportError,
    apply_academic_event_schedule_import,
    build_academic_event_schedule_import_plan,
)
from schedules.services.academic_events import (
    correct_academic_event_definition,
    correct_academic_event_occurrence,
    create_academic_event_definition,
    create_academic_event_monthly_rule,
    create_academic_event_occurrence,
)
from schedules.services.availability import effective_preceptor_availability_on
from schedules.tests.test_table_presentation import _RenderedDataTable, _RenderedDocument


def _preview_row(response, *, event):
    table = _RenderedDataTable(response.content.decode(), table_class="vacation-import-table")
    matches = [
        row for row in table.body_rows() if table.cell_for_header(row, "Event").text() == event
    ]
    if len(matches) != 1:
        raise AssertionError(f"Expected one preview row for {event!r}.")
    return {
        header: table.cell_for_header(matches[0], header).text() for header in table.header_labels()
    }


def _preview_date_endpoints(value):
    """Extract the ISO date endpoints rendered in one annual-import table cell."""
    return tuple(date.fromisoformat(token) for token in re.findall(r"\d{4}-\d{2}-\d{2}", value))


def _event_and_history_state(event):
    event.refresh_from_db()
    return (
        event.start_date,
        event.end_date,
        event.period_of_day,
        event.suffix,
        tuple(event.date_ranges),
        list(
            AcademicEventChange.objects.filter(event=event).values_list(
                "action", "reason", "recorded_value"
            )
        ),
    )


class AcademicEventScheduleImportTests(TestCase):
    def setUp(self):
        self.administrator = get_user_model().objects.create_user(username="administrator")
        self.administrator.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.ordinary = get_user_model().objects.create_user(username="ordinary")

    def workbook(self, rows):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "2026-2027"
        worksheet.append(["Academic Year Calendar 2026-2027"])
        worksheet.append(
            ("Event", "Date", "End Date", "Coverage", "Faculty", "Residents", "Detail", "Notes")
        )
        for row in rows:
            worksheet.append((row,) if isinstance(row, str) else row)
        content = BytesIO()
        workbook.save(content)
        workbook.close()
        content.seek(0)
        content.name = "Academic Year Calendar 2026-2027.xlsx"
        return content

    def plan(self, rows):
        return build_academic_event_schedule_import_plan(
            load_academic_event_definition_import(self.workbook(rows))
        )

    def real_workbook(self, rows):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "2026-2027"
        worksheet.append(
            (
                "Event",
                "Typically Scheduled",
                "AY 26-27 Dates",
                "Residents Attending",
                "Faculty Attending",
                "Notes/Links",
            )
        )
        for row in rows:
            worksheet.append((row,) if isinstance(row, str) else row)
        content = BytesIO()
        workbook.save(content)
        workbook.close()
        content.seek(0)
        content.name = "Academic Year Calendar 2026-2027.xlsx"
        return content

    def definition(self, name="ITE"):
        return create_academic_event_definition(
            base_name=name,
            start_academic_year=2026,
            expected_quarter="Q1",
            scheduler=self.administrator,
        )

    def test_import_normalizes_embedded_year_and_keeps_disjoint_ranges(self):
        definition = self.definition()
        physician = Physician.objects.create(display_name="Ada Lovelace")
        plan = self.plan(
            [
                "Q1",
                [
                    "ITE",
                    "08/10/2040, 08/11/2040, 08/13/2040",
                    None,
                    "AM",
                    "Ada Lovelace",
                    None,
                    "Orientation group",
                    "Workbook note",
                ],
            ]
        )

        self.assertEqual(len(plan.create), 1)
        self.assertEqual(len(plan.unique_names), 1)
        self.assertEqual(
            plan.create[0].date_ranges,
            ((date(2026, 8, 10), date(2026, 8, 11)), (date(2026, 8, 13), date(2026, 8, 13))),
        )
        apply_academic_event_schedule_import(plan=plan, scheduler=self.administrator)

        event = AcademicEvent.objects.get(definition=definition)
        self.assertEqual(
            event.date_ranges,
            ((date(2026, 8, 10), date(2026, 8, 11)), (date(2026, 8, 13), date(2026, 8, 13))),
        )
        self.assertFalse(event.covers(date(2026, 8, 12), "AM"))
        self.assertEqual(event.period_of_day, "AM")
        self.assertEqual(event.suffix, "Orientation group")
        self.assertEqual(event.note, "Workbook note")
        self.assertEqual(list(event.participants.all()), [physician])
        change = AcademicEventChange.objects.get(
            event=event, action=AcademicEventChange.Action.CREATED
        )
        self.assertEqual(change.changed_by, self.administrator)
        self.assertEqual(
            change.recorded_value["date_ranges"],
            [
                {"start_date": "2026-08-10", "end_date": "2026-08-11"},
                {"start_date": "2026-08-13", "end_date": "2026-08-13"},
            ],
        )
        self.assertIn("Academic Year Calendar 2026-2027.xlsx", change.reason)
        self.assertIn("Workbook note", change.reason)

    def test_real_calendar_headers_preserve_dates_attendance_and_preview_results(self):
        soccer = self.definition("USA Soccer Cup Course & Tournament")
        self.definition("ITE")
        future = self.definition("AAFP FUTURE Conference")
        collective = self.definition("Incoming G3 Retreat")
        self.definition("Sports Medicine 2 Course")
        faculty_meetings = create_academic_event_definition(
            base_name="Faculty Meetings",
            scheduling_mode=AcademicEventDefinition.SchedulingMode.MONTHLY_RULES,
            start_academic_year=2026,
            default_precepting_impact=AcademicEvent.PreceptingImpact.NONE,
            all_available_faculty=True,
            scheduler=self.administrator,
        )
        create_academic_event_monthly_rule(
            definition=faculty_meetings,
            weekday=3,
            ordinals=[1, 3, 4, 5],
            effective_start=date(2026, 7, 1),
            scheduler=self.administrator,
        )
        alder = Physician.objects.create(display_name="Bea Alder")
        fern = Physician.objects.create(display_name="Milo Fern")
        workbook = self.real_workbook(
            [
                ["Faculty Meetings", "Thursdays", None, None, None, None],
                "Q1 | July, August, September",
                ["USA Soccer Cup Course & Tournament", "July", "7/7 - 7/17", None, None, None],
                [
                    "AAFP FUTURE Conference",
                    "July",
                    date(2040, 7, 31),
                    "Clover, Moss, Reed",
                    "Alder, Fern",
                    "Conference details",
                ],
                "Q2 | October, November, December",
                ["ITE", "November", "10/21, 10/27, 10/28, 10/29", None, None, None],
                "Q3 | January, February, March",
                ["Sports Medicine 2 Course", "February", date(2026, 2, 18), None, None, None],
                "Q4 | April, May, June",
                ["Incoming G3 Retreat", "May", "5/3 - 5/4", None, "All available faculty", None],
                ["Residency Interviews", "October - January", None, None, None, None],
            ]
        )
        data = load_academic_event_definition_import(workbook)
        definition_plan = build_academic_event_definition_import_plan(data)
        soccer_source = next(
            source for source in data.definitions if source.base_name == soccer.base_name
        )

        self.assertEqual(soccer_source.default_duration_days, 11)
        self.assertIn(soccer_source, [item.source for item in definition_plan.different])
        plan = build_academic_event_schedule_import_plan(data)

        self.assertFalse(plan.blockers)
        self.assertEqual(len(plan.dated), 5)
        self.assertEqual([source.base_name for source in plan.multi_range], ["ITE"])
        self.assertEqual(len(plan.unsupported), 2)
        self.assertEqual(len(plan.unique_names), 2)
        self.assertEqual([item.token for item in plan.unique_names], ["Alder", "Fern"])
        self.assertEqual(plan.all_available_faculty[0].base_name, collective.base_name)
        self.assertEqual(plan.ignored_residents[0].base_name, future.base_name)
        planned = {item.source.base_name: item for item in plan.create}
        self.assertEqual(
            planned[soccer.base_name].date_ranges,
            ((date(2026, 7, 7), date(2026, 7, 17)),),
        )
        self.assertEqual(
            planned["ITE"].date_ranges,
            ((date(2026, 10, 21), date(2026, 10, 21)), (date(2026, 10, 27), date(2026, 10, 29))),
        )
        self.assertEqual(
            planned["Sports Medicine 2 Course"].date_ranges,
            ((date(2027, 2, 18), date(2027, 2, 18)),),
        )
        self.assertEqual(planned[future.base_name].participants, (alder, fern))
        self.assertEqual(planned[future.base_name].source.note, "Conference details")

        apply_academic_event_schedule_import(plan=plan, scheduler=self.administrator)
        self.assertEqual(AcademicEvent.objects.count(), 5)
        self.assertEqual(AcademicEventChange.objects.count(), 5)
        exact = build_academic_event_schedule_import_plan(data)
        self.assertEqual(len(exact.exact), 5)
        apply_academic_event_schedule_import(plan=exact, scheduler=self.administrator)
        self.assertEqual(AcademicEventChange.objects.count(), 5)

    def test_new_definition_uses_single_column_span_duration(self):
        data = load_academic_event_definition_import(
            self.real_workbook(
                [
                    "Q1 | July, August, September",
                    ["USA Soccer Cup Course & Tournament", "July", "7/7 - 7/17", None, None, None],
                ]
            )
        )
        plan = build_academic_event_definition_import_plan(data)

        self.assertEqual(plan.create[0].default_duration_days, 11)
        apply_academic_event_definition_import(plan=plan, scheduler=self.administrator)
        self.assertEqual(
            AcademicEventDefinition.objects.get().default_duration_days,
            11,
        )

    def test_real_date_column_rejects_unsupported_text_shape(self):
        with self.assertRaisesRegex(AcademicEventDefinitionImportError, "invalid event date"):
            load_academic_event_definition_import(
                self.real_workbook(
                    [
                        "Q1 | July, August, September",
                        ["USA Soccer Cup Course & Tournament", "July", "July 7 through July 17"],
                    ]
                )
            )

    def test_import_pairs_listed_start_and_end_dates_before_forming_ranges(self):
        self.definition("ITE")
        plan = self.plan(
            [
                "Q1",
                [
                    "ITE",
                    "08/10/2040, 08/20/2040",
                    "08/12/2040, 08/22/2040",
                    None,
                    None,
                    None,
                    None,
                    None,
                ],
            ]
        )

        self.assertEqual(
            plan.create[0].date_ranges,
            ((date(2026, 8, 10), date(2026, 8, 12)), (date(2026, 8, 20), date(2026, 8, 22))),
        )

    def test_import_resolves_semicolon_delimited_last_first_faculty_names(self):
        self.definition("ITE")
        ada = Physician.objects.create(display_name="Ada Lovelace")
        grace = Physician.objects.create(display_name="Grace Hopper")
        plan = self.plan(
            [
                "Q1",
                [
                    "ITE",
                    date(2026, 8, 10),
                    None,
                    None,
                    "Lovelace, Ada; Hopper, Grace",
                    None,
                    None,
                    None,
                ],
            ]
        )

        self.assertFalse(plan.blockers)
        self.assertEqual(
            [item.token for item in plan.unique_names], ["Lovelace, Ada", "Hopper, Grace"]
        )
        self.assertEqual(plan.create[0].participants, (ada, grace))

    def test_collective_and_resident_only_rows_are_reported_without_individual_assignments(self):
        collective = self.definition("Faculty retreat")
        residents = self.definition("Resident teaching")
        plan = self.plan(
            [
                "Q1",
                [
                    "Faculty retreat",
                    date(2026, 8, 10),
                    None,
                    None,
                    "All available faculty",
                    None,
                    None,
                    None,
                ],
                ["Resident teaching", date(2026, 8, 12), None, None, None, "R1, R2", None, None],
            ]
        )

        self.assertEqual(len(plan.all_available_faculty), 1)
        self.assertEqual(len(plan.ignored_residents), 1)
        apply_academic_event_schedule_import(plan=plan, scheduler=self.administrator)
        self.assertTrue(AcademicEvent.objects.get(definition=collective).all_available_faculty)
        self.assertEqual(AcademicEvent.objects.get(definition=collective).participants.count(), 0)
        self.assertEqual(AcademicEvent.objects.get(definition=residents).participants.count(), 0)

    def test_imported_individual_and_collective_occurrences_compose_with_availability(self):
        individual = self.definition("Individual event")
        collective = self.definition("Collective event")
        ada = Physician.objects.create(display_name="Ada Lovelace")
        grace = Physician.objects.create(display_name="Grace Hopper")
        available_slots = {
            slot: FacultyAvailabilityTemplate.base_selection("open")
            for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
        }
        for physician in (ada, grace):
            EffectiveRole.objects.create(
                physician=physician,
                role=EffectiveRole.Role.FACULTY,
                effective_start=date(2026, 7, 1),
            )
            FacultyAvailabilityTemplate.objects.create(
                physician=physician,
                effective_start=date(2026, 7, 1),
                slots=available_slots,
            )
        plan = self.plan(
            [
                "Q1",
                [
                    "Individual event",
                    date(2026, 8, 10),
                    None,
                    "AM",
                    "Ada Lovelace",
                    None,
                    None,
                    None,
                ],
                [
                    "Collective event",
                    date(2026, 8, 11),
                    None,
                    "AM",
                    "All available faculty",
                    None,
                    None,
                    None,
                ],
            ]
        )

        apply_academic_event_schedule_import(plan=plan, scheduler=self.administrator)

        individual_event = AcademicEvent.objects.get(definition=individual)
        collective_event = AcademicEvent.objects.get(definition=collective)
        ada_individual = effective_preceptor_availability_on(ada, date(2026, 8, 10), "AM")
        grace_individual = effective_preceptor_availability_on(grace, date(2026, 8, 10), "AM")
        ada_collective = effective_preceptor_availability_on(ada, date(2026, 8, 11), "AM")
        grace_collective = effective_preceptor_availability_on(grace, date(2026, 8, 11), "AM")

        self.assertEqual(ada_individual.availability, "not_available")
        self.assertEqual(ada_individual.academic_events, (individual_event,))
        self.assertEqual(grace_individual.availability, "available")
        self.assertEqual(grace_individual.academic_events, ())
        self.assertEqual(ada_collective.availability, "not_available")
        self.assertEqual(ada_collective.academic_events, (collective_event,))
        self.assertEqual(grace_collective.availability, "not_available")
        self.assertEqual(grace_collective.academic_events, (collective_event,))

    def test_unmatched_or_ambiguous_faculty_blocks_without_mutating_any_rows(self):
        self.definition("One")
        self.definition("Two")
        Physician.objects.create(display_name="Duplicate Name")
        Physician.objects.create(display_name="Duplicate Name")
        plan = self.plan(
            [
                "Q1",
                ["One", date(2026, 8, 10), None, None, "Unknown Name", None, None, None],
                ["Two", date(2026, 8, 11), None, None, "Duplicate Name", None, None, None],
            ]
        )

        self.assertEqual(len(plan.unmatched_names), 1)
        self.assertEqual(len(plan.ambiguous_names), 1)
        with self.assertRaisesRegex(AcademicEventScheduleImportError, "blockers remain"):
            apply_academic_event_schedule_import(plan=plan, scheduler=self.administrator)
        self.assertEqual(AcademicEvent.objects.count(), 0)

    def test_correction_history_stale_plan_and_unchanged_reimport(self):
        definition = self.definition("Grand Rounds")
        physician = Physician.objects.create(display_name="Grace Hopper")
        event = create_academic_event_occurrence(
            definition=definition,
            academic_year=2026,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            participants=[physician],
            scheduler=self.administrator,
        )
        rows = [
            "Q1",
            [
                "Grand Rounds",
                date(2026, 8, 10),
                None,
                "PM",
                "Grace Hopper",
                None,
                "New detail",
                "Corrected",
            ],
        ]
        plan = self.plan(rows)
        self.assertEqual(len(plan.correct), 1)
        correct_academic_event_occurrence(
            event=event,
            start_date=date(2026, 8, 2),
            end_date=date(2026, 8, 2),
            participants=[physician],
            suffix="Manual",
            period_of_day="AM",
            scheduler=self.administrator,
        )
        before_stale_apply = _event_and_history_state(event)
        with self.assertRaisesRegex(AcademicEventScheduleImportError, "changed after preview"):
            apply_academic_event_schedule_import(plan=plan, scheduler=self.administrator)
        self.assertEqual(_event_and_history_state(event), before_stale_apply)

        plan = self.plan(rows)
        prior_history_pks = set(
            AcademicEventChange.objects.filter(event=event).values_list("pk", flat=True)
        )
        apply_academic_event_schedule_import(plan=plan, scheduler=self.administrator)
        event.refresh_from_db()
        self.assertEqual(
            (event.start_date, event.period_of_day, event.suffix),
            (date(2026, 8, 10), "PM", "New detail"),
        )
        imported_changes = AcademicEventChange.objects.filter(
            event=event,
            action=AcademicEventChange.Action.CORRECTED,
        ).exclude(pk__in=prior_history_pks)
        self.assertEqual(imported_changes.count(), 1)
        imported_change = imported_changes.get()
        self.assertEqual(imported_change.changed_by, self.administrator)
        self.assertIn("Academic Year Calendar 2026-2027.xlsx", imported_change.reason)
        self.assertIn("Corrected", imported_change.reason)
        self.assertEqual(imported_change.previous_value["start_date"], "2026-08-02")
        self.assertEqual(imported_change.previous_value["end_date"], "2026-08-02")
        self.assertEqual(
            [
                (date_range["start_date"], date_range["end_date"])
                for date_range in imported_change.previous_value["date_ranges"]
            ],
            [("2026-08-02", "2026-08-02")],
        )
        self.assertEqual(imported_change.previous_value["period_of_day"], "AM")
        self.assertEqual(imported_change.previous_value["suffix"], "Manual")
        self.assertEqual(
            [participant["id"] for participant in imported_change.previous_value["participants"]],
            [physician.pk],
        )
        self.assertEqual(imported_change.recorded_value["start_date"], "2026-08-10")
        self.assertEqual(imported_change.recorded_value["end_date"], "2026-08-10")
        self.assertEqual(
            [
                (date_range["start_date"], date_range["end_date"])
                for date_range in imported_change.recorded_value["date_ranges"]
            ],
            [("2026-08-10", "2026-08-10")],
        )
        self.assertEqual(imported_change.recorded_value["period_of_day"], "PM")
        self.assertEqual(imported_change.recorded_value["suffix"], "New detail")
        self.assertEqual(imported_change.recorded_value["note"], "Corrected")
        self.assertEqual(
            [participant["id"] for participant in imported_change.recorded_value["participants"]],
            [physician.pk],
        )

        exact = self.plan(rows)
        self.assertEqual(len(exact.exact), 1)
        before_exact_apply = _event_and_history_state(event)
        apply_academic_event_schedule_import(plan=exact, scheduler=self.administrator)
        self.assertEqual(_event_and_history_state(event), before_exact_apply)

        stale = self.plan(rows)
        correct_academic_event_occurrence(
            event=event,
            start_date=date(2026, 8, 11),
            end_date=date(2026, 8, 11),
            participants=[physician],
            suffix="Manual",
            period_of_day="PM",
            scheduler=self.administrator,
        )
        before_final_stale_apply = _event_and_history_state(event)
        with self.assertRaisesRegex(AcademicEventScheduleImportError, "changed after preview"):
            apply_academic_event_schedule_import(plan=stale, scheduler=self.administrator)
        self.assertEqual(_event_and_history_state(event), before_final_stale_apply)

    def test_customized_matched_definition_can_receive_an_annual_occurrence(self):
        definition = self.definition()
        correct_academic_event_definition(
            definition=definition,
            base_name=definition.base_name,
            start_academic_year=definition.start_academic_year,
            default_period_of_day=AcademicEvent.PeriodOfDay.AM,
            scheduler=self.administrator,
        )
        plan = self.plan(["Q1", ["ITE", date(2026, 8, 10), None, None, None, None, None, None]])

        self.assertEqual(len(plan.create), 1)
        self.assertFalse(plan.blockers)
        apply_academic_event_schedule_import(plan=plan, scheduler=self.administrator)

        definition.refresh_from_db()
        self.assertEqual(definition.default_period_of_day, AcademicEvent.PeriodOfDay.AM)
        self.assertEqual(
            AcademicEvent.objects.get(definition=definition).start_date, date(2026, 8, 10)
        )

    def test_schedule_import_does_not_date_or_create_monthly_rule_owned_occurrences(self):
        definition = create_academic_event_definition(
            base_name="Monthly workshop",
            scheduling_mode=AcademicEventDefinition.SchedulingMode.MONTHLY_RULES,
            start_academic_year=2026,
            scheduler=self.administrator,
        )
        create_academic_event_monthly_rule(
            definition=definition,
            weekday=3,
            ordinals=[1, 3],
            effective_start=date(2026, 7, 1),
            scheduler=self.administrator,
        )

        plan = self.plan(
            [
                "Q1",
                ["Monthly workshop", date(2026, 8, 6), None, "AM", None, None, None, None],
            ]
        )

        self.assertFalse(plan.blockers)
        self.assertEqual((plan.dated, plan.create, plan.correct, plan.exact), ((), (), (), ()))
        self.assertEqual(len(plan.unsupported), 1)
        self.assertEqual(plan.unsupported[0].source, plan.data.definitions[0])
        self.assertEqual(plan.unsupported[0].source.base_name, "Monthly workshop")
        self.assertEqual(plan.unsupported[0].source.row_number, 4)
        apply_academic_event_schedule_import(plan=plan, scheduler=self.administrator)
        self.assertFalse(AcademicEvent.objects.filter(definition=definition).exists())

    def test_missing_definition_rule_and_conflict_gate_annual_import_and_stale_rechecks(self):
        missing_definition = self.plan(
            ["Q1", ["Missing", date(2026, 8, 10), None, None, None, None, None, None]]
        )
        self.assertTrue(missing_definition.blockers)
        with self.assertRaisesRegex(AcademicEventScheduleImportError, "blockers remain"):
            apply_academic_event_schedule_import(
                plan=missing_definition, scheduler=self.administrator
            )

        conflict = self.plan(
            [
                "Q1",
                ["Conflicted", date(2026, 8, 10), None, None, None, None, None, None],
                ["Conflicted", date(2026, 8, 11), None, None, None, None, None, None],
            ]
        )
        self.assertTrue(conflict.blockers)
        with self.assertRaisesRegex(AcademicEventScheduleImportError, "blockers remain"):
            apply_academic_event_schedule_import(plan=conflict, scheduler=self.administrator)

        definition = create_academic_event_definition(
            base_name="Faculty Meetings",
            scheduling_mode="monthly_rules",
            start_academic_year=2026,
            default_precepting_impact=AcademicEvent.PreceptingImpact.NONE,
            all_available_faculty=True,
            scheduler=self.administrator,
        )
        rule = create_academic_event_monthly_rule(
            definition=definition,
            weekday=3,
            ordinals=[1, 3, 4, 5],
            effective_start=date(2026, 7, 1),
            scheduler=self.administrator,
        )
        self.definition("ITE")
        rows = [
            "Q1",
            ["ITE", date(2026, 8, 10), None, None, None, None, None, None],
            ["Faculty Meetings", None, None, None, None, None, None, None],
        ]
        fresh = self.plan(rows)
        self.assertFalse(fresh.blockers)

        rule.delete()
        blocked = self.plan(rows)
        self.assertEqual(AcademicEventMonthlyRule.objects.filter(definition=definition).count(), 0)
        self.assertEqual(len(blocked.blockers), 1)
        with self.assertRaises(AcademicEventScheduleImportError):
            apply_academic_event_schedule_import(plan=blocked, scheduler=self.administrator)
        with self.assertRaisesRegex(AcademicEventScheduleImportError, "changed after preview"):
            apply_academic_event_schedule_import(plan=fresh, scheduler=self.administrator)

    def test_apply_is_atomic_when_a_later_row_fails_occurrence_validation(self):
        self.definition("First")
        self.definition("Second")
        plan = self.plan(
            [
                "Q1",
                ["First", date(2026, 8, 10), None, None, None, None, None, None],
                ["Second", date(2026, 8, 11), None, None, None, None, "x" * 121, None],
            ]
        )

        with self.assertRaises(ValidationError) as raised:
            apply_academic_event_schedule_import(plan=plan, scheduler=self.administrator)
        suffix_error = raised.exception.error_dict["suffix"][0]
        self.assertEqual(suffix_error.code, "max_length")
        self.assertEqual(suffix_error.params["limit_value"], 120)
        self.assertEqual(AcademicEvent.objects.count(), 0)
        self.assertEqual(AcademicEventChange.objects.count(), 0)


class AcademicEventScheduleImportBrowserTests(TestCase):
    def setUp(self):
        self.administrator = get_user_model().objects.create_user(username="administrator")
        self.administrator.groups.add(Group.objects.get(name="Scheduling Administrators"))
        create_academic_event_definition(
            base_name="Orientation",
            start_academic_year=2026,
            expected_quarter="Q1",
            scheduler=self.administrator,
        )
        create_academic_event_definition(
            base_name="ITE",
            start_academic_year=2026,
            expected_quarter="Q1",
            scheduler=self.administrator,
        )

    def upload(self):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["Academic Year Calendar 2026-2027"])
        worksheet.append(["Event", "AY 26-27 Dates", "Faculty Attending"])
        worksheet.append(["Q1"])
        worksheet.append(["Orientation", date(2026, 7, 10), "All available faculty"])
        worksheet.append(["ITE", "08/10/2040, 08/11/2040, 08/13/2040"])
        content = BytesIO()
        workbook.save(content)
        workbook.close()
        return SimpleUploadedFile(
            "Academic Year Calendar 2026-2027.xlsx",
            content.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_admin_can_preview_and_apply_annual_schedule_import(self):
        self.client.force_login(self.administrator)
        url = reverse("academic-event-schedule-import")
        preview = self.client.post(url, {"workbook": self.upload(), "action": "preview"})
        applied = self.client.post(
            url, {"preview_token": preview.context["preview_token"], "action": "apply"}
        )

        orientation = _preview_row(preview, event="Orientation")
        self.assertEqual(orientation["Result"], "Create")
        self.assertEqual(orientation["Workbook row"], "4")
        self.assertEqual(
            _preview_date_endpoints(orientation["Inclusive dates"]),
            (date(2026, 7, 10), date(2026, 7, 10)),
        )
        self.assertEqual(orientation["Coverage"], "Whole day")
        self.assertEqual(orientation["Faculty"], "All available Faculty")
        ite = _preview_row(preview, event="ITE")
        self.assertEqual(ite["Result"], "Create")
        self.assertEqual(ite["Workbook row"], "5")
        self.assertEqual(
            _preview_date_endpoints(ite["Inclusive dates"]),
            (
                date(2026, 8, 10),
                date(2026, 8, 11),
                date(2026, 8, 13),
                date(2026, 8, 13),
            ),
        )
        self.assertEqual(ite["Faculty"], "None")
        self.assertContains(preview, "Apply previewed import")
        self.assertContains(applied, "Import applied")
        self.assertNotContains(applied, "Apply previewed import")
        self.assertEqual(AcademicEvent.objects.count(), 2)

    def test_preview_and_apply_use_shared_processing_feedback_presentation(self):
        self.client.force_login(self.administrator)
        url = reverse("academic-event-schedule-import")

        preview = self.client.post(url, {"workbook": self.upload(), "action": "preview"})
        document = _RenderedDocument(preview.content.decode())
        for action, operation_word in (("preview", "workbook"), ("apply", "applying")):
            with self.subTest(action=action):
                form = document.form_with_submission_action(name="action", value=action)
                self.assertIn("data-processing-feedback-form", form.attributes)
                dialogs = [
                    element
                    for element in form.descendants_named("div")
                    if "data-processing-feedback" in element.attributes
                ]
                self.assertEqual(len(dialogs), 1)
                dialog = dialogs[0]
                self.assertEqual(dialog.attributes.get("role"), "dialog")
                self.assertEqual(dialog.attributes.get("aria-modal"), "true")
                self.assertIn(operation_word, dialog.attributes.get("aria-label", "").casefold())

        applied = self.client.post(
            url, {"preview_token": preview.context["preview_token"], "action": "apply"}
        )
        self.assertFalse(
            _RenderedDocument(applied.content.decode()).forms_with_submission_action(
                name="action", value="apply"
            )
        )

    def test_apply_rechecks_new_schedule_blockers_without_mutating(self):
        self.client.force_login(self.administrator)
        url = reverse("academic-event-schedule-import")
        preview = self.client.post(url, {"workbook": self.upload(), "action": "preview"})
        create_academic_event_definition(
            base_name="Orientation",
            start_academic_year=2026,
            expected_quarter="Q2",
            scheduler=self.administrator,
        )

        response = self.client.post(
            url, {"preview_token": preview.context["preview_token"], "action": "apply"}
        )

        self.assertContains(response, "Preview found blockers")
        self.assertContains(response, "No data changed")
        orientation = _preview_row(response, event="Orientation")
        self.assertEqual(orientation["Workbook row"], "4")
        self.assertEqual(orientation["Result"], "Not imported")
        self.assertIn("ambiguous", orientation["Detail"].casefold())
        self.assertNotContains(response, "Apply previewed import")
        self.assertEqual(AcademicEvent.objects.count(), 0)
        self.assertEqual(AcademicEventChange.objects.count(), 0)
