import re
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from schedules.forms.academic_events import AcademicEventDefinitionForm, AcademicEventForm
from schedules.models import (
    AcademicEvent,
    AcademicEventChange,
    AcademicEventDefinition,
    AcademicEventDefinitionChange,
    AcademicEventMonthlyRule,
    AcademicEventMonthlyRuleChange,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    Physician,
)
from schedules.services.academic_events import (
    academic_event_projected_work_items,
    academic_events_overlapping,
    correct_academic_event_definition,
    correct_academic_event_monthly_rule,
    correct_academic_event_occurrence,
    create_academic_event_definition,
    create_academic_event_definition_with_initial_monthly_rule,
    create_academic_event_monthly_rule,
    create_academic_event_occurrence,
    deactivate_academic_event_definition,
    initialize_monthly_academic_event_occurrences,
    remove_academic_event_monthly_rule,
)
from schedules.services.availability import effective_preceptor_availability_on
from schedules.services.master_schedule import master_schedule_week
from schedules.services.schedule_reports import (
    ScheduleReportParameters,
    academic_event_schedule_report,
)
from schedules.services.slot_projection import RequestedSlot, slot_projection
from schedules.tests.test_table_presentation import _RenderedDataTable, _RenderedTableParser
from schedules.tests.test_work_remaining import _action_matches


class MonthlyAcademicEventRuleTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(username="scheduler")
        self.definition = create_academic_event_definition(
            base_name="Faculty meeting",
            scheduling_mode="monthly_rules",
            start_academic_year=2024,
            all_available_faculty=True,
            typically_scheduled="First Thursday",
            scheduler=self.scheduler,
        )

    def rendered_academic_event_table(self, content, identity):
        return _RenderedDataTable(content, identity)

    def academic_event_occurrence_rows(self, table, occurrence, action_url):
        return [
            row
            for row in table.body_rows()
            if table.cell_for_header(row, "Event").text().strip() == occurrence.title
            and any(
                _action_matches(link.attributes.get("href", ""), action_url)
                for link in table.cell_for_header(row, "Actions").descendants_named("a")
            )
        ]

    def academic_event_occurrence_row(self, table, occurrence, action_url):
        rows = self.academic_event_occurrence_rows(table, occurrence, action_url)
        self.assertEqual(len(rows), 1, f"Expected one row for {occurrence.title!r}.")
        return rows[0]

    @staticmethod
    def table_cell_text(table, row, header):
        return table.cell_for_header(row, header).text().strip()

    def assert_academic_event_empty_table(self, table, *, category_terms, academic_year):
        rows = table.body_rows()
        messages = [" ".join(row.text().split()).casefold() for row in rows]
        year_start, year_end = academic_year.split("-", maxsplit=1)
        year_pattern = rf"(?<!\d){year_start}\s*-\s*{year_end}(?!\d)"

        def has_empty_message(message):
            return (
                re.search(r"\b(?:no|none|zero|empty)\b", message)
                and all(
                    re.search(
                        r"\b" + r"\s+".join(re.escape(word) for word in term.split()) + r"\b",
                        message,
                    )
                    for term in category_terms
                )
                and re.search(year_pattern, message)
            )

        self.assertTrue(
            any(has_empty_message(message) for message in messages),
            f"Expected an empty {category_terms!r} Academic Event message for {academic_year}.",
        )
        self.assertFalse(
            any(
                any(
                    cell.attributes.get("scope") == "row" and cell.text().strip()
                    for cell in row.children_named("th")
                )
                for row in rows
            ),
            "An empty Academic Event table must not contain a record identity row.",
        )

    def test_monthly_definition_has_no_quarter_or_annual_work(self):
        self.assertIsNone(self.definition.expected_quarter)
        self.assertEqual(academic_event_projected_work_items(2024, ()), ())
        form = AcademicEventDefinitionForm(
            data={
                "base_name": "Monthly meeting",
                "scheduling_mode": "monthly_rules",
                "default_duration_days": "1",
                "default_period_of_day": "whole_day",
                "default_precepting_impact": "hard",
                "all_available_faculty": "on",
                "start_academic_year": "2024",
                "end_academic_year": "",
            },
            current_academic_year=2024,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data["expected_quarter"])

    def test_initial_monthly_rule_creation_is_atomic(self):
        with self.assertRaisesRegex(ValidationError, "one or more"):
            create_academic_event_definition_with_initial_monthly_rule(
                base_name="Invalid initial rule",
                scheduling_mode=AcademicEventDefinition.SchedulingMode.MONTHLY_RULES,
                start_academic_year=2024,
                scheduler=self.scheduler,
                initial_rule={"weekday": 3, "ordinals": []},
            )

        self.assertFalse(
            AcademicEventDefinition.objects.filter(base_name="Invalid initial rule").exists()
        )
        definition = create_academic_event_definition_with_initial_monthly_rule(
            base_name="Initial rule meeting",
            scheduling_mode=AcademicEventDefinition.SchedulingMode.MONTHLY_RULES,
            start_academic_year=2024,
            scheduler=self.scheduler,
            initial_rule={
                "weekday": 3,
                "ordinals": [1],
                "effective_start": date(2024, 7, 1),
            },
        )

        self.assertEqual(
            list(definition.monthly_rules.values_list("weekday", "ordinals", "effective_start")),
            [(3, [1], date(2024, 7, 1))],
        )

    def test_range_reads_materialize_only_selected_real_ordinals(self):
        rule = create_academic_event_monthly_rule(
            definition=self.definition,
            weekday=3,
            ordinals=[1, 3, 5],
            scheduler=self.scheduler,
        )
        events = academic_events_overlapping(date(2025, 1, 1), date(2025, 1, 31))
        self.assertEqual(
            [event.start_date for event in events],
            [date(2025, 1, 2), date(2025, 1, 16), date(2025, 1, 30)],
        )
        self.assertEqual(events[0].monthly_rule, rule)
        self.assertTrue(all(isinstance(event, AcademicEvent) and event.pk > 0 for event in events))
        first_ids = [event.pk for event in events]
        self.assertEqual(
            [
                event.pk
                for event in academic_events_overlapping(date(2025, 1, 1), date(2025, 1, 31))
            ],
            first_ids,
        )
        self.assertEqual(
            AcademicEvent.objects.filter(monthly_rule=rule, is_active=True).count(), len(first_ids)
        )
        with self.assertRaisesRegex(ValidationError, "cannot overlap"):
            create_academic_event_monthly_rule(
                definition=self.definition, weekday=3, ordinals=[2], scheduler=self.scheduler
            )

    def test_collective_projection_blocks_faculty_and_counts_in_reports(self):
        create_academic_event_monthly_rule(
            definition=self.definition, weekday=3, ordinals=[3], scheduler=self.scheduler
        )
        physician = Physician.objects.create(display_name="Faculty physician")
        future_faculty = Physician.objects.create(display_name="Future Faculty physician")
        EffectiveRole.objects.create(
            physician=physician, role=EffectiveRole.Role.FACULTY, effective_start=date(2024, 1, 1)
        )
        EffectiveRole.objects.create(
            physician=future_faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 17),
        )
        FacultyAvailabilityTemplate.objects.create(
            physician=physician,
            effective_start=date(2024, 1, 1),
            slots={
                slot: FacultyAvailabilityTemplate.base_selection("open")
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
        )
        availability = effective_preceptor_availability_on(physician, date(2025, 1, 16), "AM")
        self.assertEqual(availability.availability, "not_available")
        self.assertEqual(len(availability.academic_events), 1)
        self.assertEqual(
            effective_preceptor_availability_on(
                future_faculty, date(2025, 1, 16), "AM"
            ).academic_events,
            (),
        )
        report = academic_event_schedule_report(
            ScheduleReportParameters(
                "academic-events",
                "custom",
                "history",
                date(2025, 1, 1),
                date(2025, 1, 31),
                date(2025, 1, 1),
                date(2025, 1, 31),
                date(2025, 2, 1),
            )
        )
        self.assertEqual(
            (report.rows[0].events, report.rows[0].event_days, report.rows[0].affected_shifts),
            (1, 1, 2),
        )

    def test_monthly_definition_and_rule_validation_allow_individual_defaults(self):
        definition = AcademicEventDefinition(
            base_name="Unassigned monthly event",
            scheduling_mode=AcademicEventDefinition.SchedulingMode.MONTHLY_RULES,
            start_academic_year=2024,
            expected_quarter=None,
            default_precepting_impact="hard",
        )
        invalid_rule = AcademicEventMonthlyRule(
            definition=self.definition,
            weekday=3,
            ordinals=[],
        )

        definition.full_clean()
        with self.assertRaisesRegex(ValidationError, "one or more"):
            invalid_rule.full_clean()

    @patch("schedules.services.academic_events.organization_today", return_value=date(2025, 1, 15))
    def test_definition_assignment_defaults_materialize_and_preserve_customized_or_past_occurrences(
        self, _today
    ):
        first_default = Physician.objects.create(display_name="First default")
        replacement_default = Physician.objects.create(display_name="Replacement default")
        customized_physician = Physician.objects.create(display_name="Customized physician")
        definition = create_academic_event_definition(
            base_name="Individual monthly meeting",
            scheduling_mode="monthly_rules",
            start_academic_year=2024,
            default_required_participant_count=2,
            default_participants=(first_default,),
            scheduler=self.scheduler,
        )
        rule = create_academic_event_monthly_rule(
            definition=definition, weekday=3, ordinals=[1], scheduler=self.scheduler
        )
        initialize_monthly_academic_event_occurrences(
            start_date=date(2025, 1, 1), end_date=date(2025, 3, 31), scheduler=self.scheduler
        )
        past = AcademicEvent.objects.get(monthly_rule=rule, start_date=date(2025, 1, 2))
        customized = AcademicEvent.objects.get(monthly_rule=rule, start_date=date(2025, 2, 6))
        untouched = AcademicEvent.objects.get(monthly_rule=rule, start_date=date(2025, 3, 6))
        self.assertEqual(past.required_participant_count, 2)
        self.assertEqual(set(past.participants.all()), {first_default})
        self.assertEqual(
            past.changes.get(action=AcademicEventChange.Action.CREATED).recorded_value[
                "participant_ids"
            ],
            [first_default.pk],
        )
        self.assertTrue(untouched.assignment_is_default)

        correct_academic_event_occurrence(
            event=customized,
            start_date=customized.start_date,
            end_date=customized.end_date,
            participants=(customized_physician,),
            all_available_faculty=False,
            required_participant_count=customized.required_participant_count,
            period_of_day=customized.period_of_day,
            precepting_impact=customized.precepting_impact,
            scheduler=self.scheduler,
        )
        correct_academic_event_definition(
            definition=definition,
            base_name=definition.base_name,
            start_academic_year=definition.start_academic_year,
            default_participants=(replacement_default,),
            scheduler=self.scheduler,
        )

        past.refresh_from_db()
        customized.refresh_from_db()
        untouched.refresh_from_db()
        definition.refresh_from_db()
        self.assertEqual(set(past.participants.all()), {first_default})
        self.assertEqual(set(customized.participants.all()), {customized_physician})
        self.assertFalse(customized.assignment_is_default)
        self.assertEqual(customized.changes.count(), 2)
        self.assertEqual(
            [
                participant["id"]
                for participant in customized.changes.latest("pk").recorded_value["participants"]
            ],
            [customized_physician.pk],
        )
        self.assertFalse(customized.changes.latest("pk").recorded_value["assignment_is_default"])
        self.assertEqual(set(untouched.participants.all()), {replacement_default})
        self.assertTrue(untouched.assignment_is_default)
        change = definition.changes.latest("pk")
        self.assertEqual(change.previous_value["default_participant_ids"], [first_default.pk])
        self.assertEqual(change.recorded_value["default_participant_ids"], [replacement_default.pk])

    def test_monthly_collective_defaults_materialize_without_individuals(self):
        default_physician = Physician.objects.create(display_name="Ignored collective default")
        with self.assertRaisesRegex(ValidationError, "cannot name individual"):
            create_academic_event_definition(
                base_name="Invalid collective monthly meeting",
                scheduling_mode="monthly_rules",
                start_academic_year=2024,
                all_available_faculty=True,
                default_participants=(default_physician,),
                scheduler=self.scheduler,
            )
        rule = create_academic_event_monthly_rule(
            definition=self.definition, weekday=3, ordinals=[1], scheduler=self.scheduler
        )
        occurrence = initialize_monthly_academic_event_occurrences(
            start_date=date(2025, 1, 1), end_date=date(2025, 1, 31), scheduler=self.scheduler
        )[0]

        self.assertEqual(occurrence.monthly_rule, rule)
        self.assertTrue(occurrence.all_available_faculty)
        self.assertEqual(list(occurrence.participants.all()), [])

    def test_effective_dates_bound_monthly_occurrences_to_definition_years(self):
        definition = create_academic_event_definition(
            base_name="Bounded meeting",
            scheduling_mode="monthly_rules",
            start_academic_year=2025,
            end_academic_year=2025,
            all_available_faculty=True,
            scheduler=self.scheduler,
        )
        create_academic_event_monthly_rule(
            definition=definition, weekday=3, ordinals=[1], scheduler=self.scheduler
        )

        self.assertEqual(academic_events_overlapping(date(2025, 1, 1), date(2025, 1, 31)), ())
        self.assertEqual(
            [
                event.start_date
                for event in academic_events_overlapping(date(2025, 7, 1), date(2025, 7, 31))
            ],
            [date(2025, 7, 3)],
        )
        self.assertEqual(academic_events_overlapping(date(2026, 7, 1), date(2026, 7, 31)), ())

    def test_unused_monthly_definition_deactivates_and_stops_occurrences(self):
        rule = create_academic_event_monthly_rule(
            definition=self.definition, weekday=3, ordinals=[1], scheduler=self.scheduler
        )
        self.assertEqual(
            [
                event.start_date
                for event in academic_events_overlapping(date(2025, 1, 1), date(2025, 1, 31))
            ],
            [date(2025, 1, 2)],
        )

        deactivate_academic_event_definition(definition=self.definition, scheduler=self.scheduler)

        self.definition.refresh_from_db()
        self.assertFalse(self.definition.is_active)
        self.assertTrue(AcademicEventMonthlyRule.objects.filter(pk=rule.pk).exists())
        self.assertEqual(academic_events_overlapping(date(2025, 1, 1), date(2025, 1, 31)), ())
        self.assertEqual(
            list(
                AcademicEventDefinitionChange.objects.filter(definition=self.definition)
                .order_by("pk")
                .values_list("action", flat=True)
            ),
            ["created", "deactivated"],
        )

    def test_rule_actions_retain_history_and_definition_page(self):
        self.client.force_login(self.scheduler)
        create_url = reverse("academic-event-monthly-rule-create", args=(self.definition.pk,))
        response = self.client.post(
            create_url,
            {
                "weekday": "3",
                "ordinals": ["1"],
                "effective_start": "2025-01-01",
                "effective_end": "",
                "reason": "Initial schedule",
            },
        )
        rule = AcademicEventMonthlyRule.objects.get(definition=self.definition)
        self.assertRedirects(
            response, reverse("academic-event-definition-correct", args=(self.definition.pk,))
        )

        response = self.client.post(
            reverse("academic-event-monthly-rule-correct", args=(rule.pk,)),
            {
                "weekday": "3",
                "ordinals": ["2"],
                "effective_start": "2025-01-01",
                "effective_end": "",
                "reason": "Corrected schedule",
            },
        )
        self.assertRedirects(
            response, reverse("academic-event-definition-correct", args=(self.definition.pk,))
        )
        response = self.client.post(
            reverse("academic-event-monthly-rule-remove", args=(rule.pk,)),
            {"reason": "Retired schedule"},
        )

        self.assertRedirects(
            response, reverse("academic-event-definition-correct", args=(self.definition.pk,))
        )
        self.assertEqual(
            list(
                AcademicEventMonthlyRuleChange.objects.filter(definition=self.definition)
                .order_by("pk")
                .values_list("action", flat=True)
            ),
            ["created", "corrected", "removed"],
        )
        page = self.client.get(
            reverse("academic-event-definition-correct", args=(self.definition.pk,))
        )
        self.assertContains(page, "Monthly rule history")
        self.assertContains(page, "Retired schedule")

    def test_calendar_list_master_schedule_and_scheduled_report_render_monthly_occurrences(self):
        self.client.force_login(self.scheduler)
        create_academic_event_monthly_rule(
            definition=self.definition, weekday=3, ordinals=[3], scheduler=self.scheduler
        )
        physician = Physician.objects.create(display_name="Faculty physician")
        EffectiveRole.objects.create(
            physician=physician, role=EffectiveRole.Role.FACULTY, effective_start=date(2024, 1, 1)
        )

        calendar = self.client.get(
            reverse("academic-event-workspace"), {"academic_year": "2024", "month": "2025-01"}
        )
        listing = self.client.get(reverse("academic-event-list"), {"academic_year": "2024"})
        week = master_schedule_week(date(2025, 1, 13))
        report = academic_event_schedule_report(
            ScheduleReportParameters(
                "academic-events",
                "custom",
                "scheduled",
                date(2025, 1, 1),
                date(2025, 1, 31),
                date(2025, 1, 1),
                date(2025, 1, 31),
                date(2025, 1, 1),
            )
        )

        self.assertContains(calendar, "Faculty meeting")
        self.assertContains(listing, "Faculty meeting")
        self.assertTrue(
            any(
                fact.kind == "academic_event" and fact.record.title == "Faculty meeting"
                for fact in week.supporting_facts
            )
        )
        self.assertEqual([(row.physician, row.events) for row in report.rows], [(physician, 1)])
        self.assertEqual(report.findings, ())

    @patch("schedules.services.academic_events.organization_today", return_value=date(2025, 1, 15))
    def test_monthly_assignment_editor_customizes_an_occurrence_and_preserves_it(self, _today):
        default_physician = Physician.objects.create(display_name="Default physician")
        customized_physician = Physician.objects.create(display_name="Customized physician")
        replacement_physician = Physician.objects.create(display_name="Replacement physician")
        definition = create_academic_event_definition(
            base_name="Individual monthly meeting",
            scheduling_mode="monthly_rules",
            start_academic_year=2024,
            default_participants=(default_physician,),
            scheduler=self.scheduler,
        )
        rule = create_academic_event_monthly_rule(
            definition=definition, weekday=3, ordinals=[1], scheduler=self.scheduler
        )
        initialize_monthly_academic_event_occurrences(
            start_date=date(2025, 2, 1), end_date=date(2025, 2, 28), scheduler=self.scheduler
        )
        occurrence = AcademicEvent.objects.get(monthly_rule=rule, start_date=date(2025, 2, 6))
        assignment_url = reverse("academic-event-monthly-assignment-correct", args=(occurrence.pk,))
        self.client.force_login(self.scheduler)

        response = self.client.get(assignment_url)

        self.assertContains(response, "Rule-owned event details")
        self.assertContains(
            response,
            f'href="{reverse("academic-event-monthly-rule-correct", args=(rule.pk,))}">Manage monthly rule</a>',
        )
        self.assertContains(response, "Default physician")
        self.assertNotContains(response, 'name="start_date"')
        self.assertNotContains(response, "Remove Academic Event")
        self.assertNotContains(response, "Not occurring this year")

        response = self.client.post(
            assignment_url,
            {"participants": [str(customized_physician.pk)], "reason": "Coverage arranged."},
        )

        self.assertEqual(response.status_code, 302)
        occurrence.refresh_from_db()
        self.assertEqual(set(occurrence.participants.all()), {customized_physician})
        self.assertFalse(occurrence.assignment_is_default)
        self.assertEqual(occurrence.changes.latest("pk").reason, "Coverage arranged.")
        correct_academic_event_definition(
            definition=definition,
            base_name=definition.base_name,
            start_academic_year=definition.start_academic_year,
            default_participants=(replacement_physician,),
            scheduler=self.scheduler,
        )
        occurrence.refresh_from_db()
        self.assertEqual(set(occurrence.participants.all()), {customized_physician})

    @patch("schedules.services.work_remaining.organization_today", return_value=date(2025, 1, 1))
    def test_monthly_browser_simplifies_event_card_assignment_details(self, _today):
        filled_physician = Physician.objects.create(display_name="Filled physician")
        partial_physician = Physician.objects.create(display_name="Partial physician")
        filled_definition = create_academic_event_definition(
            base_name="Filled monthly event",
            scheduling_mode="monthly_rules",
            start_academic_year=2024,
            typically_scheduled="Second Thursday",
            default_precepting_impact=AcademicEvent.PreceptingImpact.SOFT,
            default_participants=(filled_physician,),
            scheduler=self.scheduler,
        )
        partial_definition = create_academic_event_definition(
            base_name="Partial monthly event",
            scheduling_mode="monthly_rules",
            start_academic_year=2024,
            typically_scheduled="Third Thursday",
            default_precepting_impact=AcademicEvent.PreceptingImpact.NONE,
            default_period_of_day=AcademicEvent.PeriodOfDay.NONE,
            default_required_participant_count=2,
            default_participants=(partial_physician,),
            scheduler=self.scheduler,
        )
        create_academic_event_monthly_rule(
            definition=self.definition, weekday=3, ordinals=[1], scheduler=self.scheduler
        )
        create_academic_event_monthly_rule(
            definition=filled_definition, weekday=3, ordinals=[2], scheduler=self.scheduler
        )
        create_academic_event_monthly_rule(
            definition=partial_definition, weekday=3, ordinals=[3], scheduler=self.scheduler
        )
        self.client.force_login(self.scheduler)

        calendar = self.client.get(
            reverse("academic-event-workspace"), {"academic_year": "2024", "month": "2025-01"}
        )
        listing = self.client.get(reverse("academic-event-list"), {"academic_year": "2024"})
        all_faculty = AcademicEvent.objects.get(
            definition=self.definition, start_date=date(2025, 1, 2)
        )
        filled = AcademicEvent.objects.get(
            definition=filled_definition, start_date=date(2025, 1, 9)
        )
        partial = AcademicEvent.objects.get(
            definition=partial_definition, start_date=date(2025, 1, 16)
        )
        calendar_content = calendar.content.decode()
        all_faculty_card = re.search(
            rf'<span class="academic-event-segment-name">{all_faculty.title}</span>'
            r'<span class="academic-event-segment-details">(.*?)</a>',
            calendar_content,
        ).group(1)
        filled_card = re.search(
            rf'<span class="academic-event-segment-name">{filled.title}</span>'
            r'<span class="academic-event-segment-details">(.*?)</a>',
            calendar_content,
        ).group(1)
        partial_card = re.search(
            rf'<span class="academic-event-segment-name">{partial.title}</span>'
            r'<span class="academic-event-segment-details">(.*?)</a>',
            calendar_content,
        ).group(1)

        self.assertEqual(all_faculty_card.count("All available Faculty"), 1)
        self.assertNotIn("participants", all_faculty_card)
        self.assertNotIn("Filled", all_faculty_card)
        self.assertNotContains(calendar, "Precepting impact:")
        self.assertNotContains(calendar, 'class="academic-event-impact"')
        self.assertIn("1/1 participants", filled_card)
        self.assertNotIn("Filled", filled_card)
        self.assertIn("1/2 participants", partial_card)
        self.assertIn("Assignment incomplete", partial_card)
        self.assertContains(
            calendar,
            'class="academic-event-segment academic-event-segment--needs-participants"',
            count=1,
        )
        self.assertNotContains(calendar, "Calendar only")
        for event in (all_faculty, filled):
            with self.subTest(event=event.title):
                self.assertFalse(
                    any(
                        segment["needs_participants"]
                        for week in calendar.context["academic_event_calendar_weeks"]
                        for segment in week["segments"]
                        if segment["event"] == event
                    )
                )

        listing_content = listing.content.decode()
        filled_monthly_events = self.rendered_academic_event_table(
            listing_content, "academic-event-monthly-filled-events"
        )
        unfilled_monthly_events = self.rendered_academic_event_table(
            listing_content, "academic-event-monthly-unfilled-events"
        )
        # TEXT-CONTRACT: academic-event-list-filled-status —
        # docs/design.md, Academic Events assignment-state presentation, requires
        # the exact List status label Filled; capitalization and whitespace matter.
        # Production: schedules/views/academic_events.py. This assertion protects
        # the Monthly List status without constraining its surrounding markup.
        expected_rows = (
            (
                all_faculty,
                filled_monthly_events,
                unfilled_monthly_events,
                {
                    "Typically scheduled": "First Thursday",
                    "Participants": "All available Faculty",
                    "Assigned": "All Faculty",
                    "Status": "Filled",
                },
            ),
            (
                filled,
                filled_monthly_events,
                unfilled_monthly_events,
                {
                    "Typically scheduled": "Second Thursday",
                    "Impact": "Soft",
                    "Participants": "Filled physician",
                    "Assigned": "1/1",
                    "Status": "Filled",
                },
            ),
            (
                partial,
                unfilled_monthly_events,
                filled_monthly_events,
                {
                    "Typically scheduled": "Third Thursday",
                    "Impact": "None",
                    "Participants": "Partial physician",
                    "Assigned": "1/2",
                    "Status": "Assignment incomplete (1 needed)",
                },
            ),
        )
        for occurrence, expected_table, other_table, expected_fields in expected_rows:
            with self.subTest(occurrence=occurrence.start_date):
                action_url = (
                    f"{reverse('academic-event-monthly-assignment-correct', args=(occurrence.pk,))}"
                    "?academic_year=2024&view=list"
                )
                row = self.academic_event_occurrence_row(expected_table, occurrence, action_url)
                self.assertEqual(
                    self.academic_event_occurrence_rows(other_table, occurrence, action_url), []
                )
                for header, expected_value in expected_fields.items():
                    self.assertEqual(
                        self.table_cell_text(expected_table, row, header), expected_value
                    )
        self.assertIn(
            f"academic-event-occurrence-{partial.pk}",
            [item.key for item in listing.context["work_remaining"].items],
        )
        self.assertEqual(
            [
                item.key
                for item in academic_event_projected_work_items(
                    2024, (all_faculty, filled, partial)
                )
            ],
            [f"academic-event-occurrence-{partial.pk}"],
        )

    def test_definitions_and_list_browser_distinguish_yearly_and_monthly_rules(self):
        yearly_definition = create_academic_event_definition(
            base_name="Annual retreat",
            expected_quarter="Q3",
            start_academic_year=2024,
            scheduler=self.scheduler,
        )
        yearly_event = create_academic_event_occurrence(
            definition=yearly_definition,
            academic_year=2024,
            start_date=date(2024, 8, 1),
            end_date=date(2024, 8, 1),
            scheduler=self.scheduler,
        )
        create_academic_event_monthly_rule(
            definition=self.definition, weekday=3, ordinals=[3], scheduler=self.scheduler
        )
        self.client.force_login(self.scheduler)

        definitions = self.client.get(
            reverse("academic-event-definition-workspace"), {"academic_year": "2024"}
        )
        listing = self.client.get(reverse("academic-event-list"), {"academic_year": "2024"})
        monthly_event = AcademicEvent.objects.get(
            definition=self.definition, start_date=date(2025, 1, 16)
        )

        definitions_content = definitions.content.decode()
        definitions_parser = _RenderedTableParser()
        definitions_parser.feed(definitions_content)
        definition_section_headings = [
            heading.text().strip()
            for heading in definitions_parser.root.descendants_named(
                "h1", "h2", "h3", "h4", "h5", "h6"
            )
            if any(ancestor.tag == "section" for ancestor in heading.ancestors())
        ]
        self.assertLess(
            definition_section_headings.index("Monthly-rule definitions"),
            definition_section_headings.index("Yearly definitions"),
        )
        monthly_definitions = self.rendered_academic_event_table(
            definitions_content, "academic-event-monthly-rule-definitions"
        )
        yearly_definitions = self.rendered_academic_event_table(
            definitions_content, "academic-event-yearly-definitions"
        )
        monthly_rows = [
            row
            for row in monthly_definitions.body_rows()
            if monthly_definitions.cell_for_header(row, "Base name").text().strip()
            == self.definition.base_name
        ]
        yearly_rows = [
            row
            for row in yearly_definitions.body_rows()
            if yearly_definitions.cell_for_header(row, "Base name").text().strip()
            == yearly_definition.base_name
        ]
        self.assertEqual(len(monthly_rows), 1)
        self.assertEqual(len(yearly_rows), 1)
        self.assertEqual(
            self.table_cell_text(monthly_definitions, monthly_rows[0], "Expected quarter"),
            "Not applicable",
        )
        self.assertEqual(
            self.table_cell_text(yearly_definitions, yearly_rows[0], "Expected quarter"), "Q3"
        )
        self.assertNotIn("Type", monthly_definitions.header_labels())
        self.assertNotIn("Type", yearly_definitions.header_labels())
        listing_content = listing.content.decode()
        listing_parser = _RenderedTableParser()
        listing_parser.feed(listing_content)
        listing_section_headings = [
            heading.text().strip()
            for heading in listing_parser.root.descendants_named("h1", "h2", "h3", "h4", "h5", "h6")
            if any(ancestor.tag == "section" for ancestor in heading.ancestors())
        ]
        self.assertLess(
            listing_section_headings.index("Monthly Events"),
            listing_section_headings.index("Scheduled yearly events"),
        )
        filled_monthly_events = self.rendered_academic_event_table(
            listing_content, "academic-event-monthly-filled-events"
        )
        unfilled_monthly_events = self.rendered_academic_event_table(
            listing_content, "academic-event-monthly-unfilled-events"
        )
        yearly_events = self.rendered_academic_event_table(
            listing_content, "academic-event-yearly-events"
        )
        monthly_action = (
            f"{reverse('academic-event-monthly-assignment-correct', args=(monthly_event.pk,))}"
            "?academic_year=2024&view=list"
        )
        yearly_action = (
            f"{reverse('academic-event-correct', args=(yearly_event.pk,))}"
            "?academic_year=2024&view=list"
        )
        monthly_row = self.academic_event_occurrence_row(
            filled_monthly_events, monthly_event, monthly_action
        )
        yearly_row = self.academic_event_occurrence_row(yearly_events, yearly_event, yearly_action)
        self.assertEqual(
            self.academic_event_occurrence_rows(
                unfilled_monthly_events, monthly_event, monthly_action
            ),
            [],
        )
        self.assertEqual(
            self.academic_event_occurrence_rows(yearly_events, monthly_event, monthly_action), []
        )
        self.assertEqual(
            self.table_cell_text(filled_monthly_events, monthly_row, "Typically scheduled"),
            "First Thursday",
        )
        self.assertEqual(
            self.table_cell_text(filled_monthly_events, monthly_row, "Status"), "Filled"
        )
        self.assertEqual(self.table_cell_text(yearly_events, yearly_row, "Type"), "Yearly")
        self.assertRedirects(
            self.client.get(reverse("academic-event-correct", args=(monthly_event.pk,))),
            reverse("academic-event-monthly-assignment-correct", args=(monthly_event.pk,)),
            fetch_redirect_response=False,
        )
        self.assertRedirects(
            self.client.post(reverse("academic-event-correct", args=(monthly_event.pk,)), {}),
            reverse("academic-event-monthly-assignment-correct", args=(monthly_event.pk,)),
            fetch_redirect_response=False,
        )
        self.assertRedirects(
            self.client.post(reverse("academic-event-remove", args=(monthly_event.pk,)), {}),
            reverse("academic-event-monthly-rule-correct", args=(monthly_event.monthly_rule_id,)),
            fetch_redirect_response=False,
        )
        monthly_event.refresh_from_db()
        self.assertTrue(monthly_event.is_active)

    def test_definitions_and_list_present_recurring_modes_without_null_cells(self):
        yearly_definition = create_academic_event_definition(
            base_name="Annual retreat",
            start_academic_year=2024,
            scheduler=self.scheduler,
        )
        yearly_event = create_academic_event_occurrence(
            definition=yearly_definition,
            academic_year=2024,
            start_date=date(2024, 8, 1),
            end_date=date(2024, 8, 1),
            scheduler=self.scheduler,
        )
        rule = create_academic_event_monthly_rule(
            definition=self.definition, weekday=3, ordinals=[3], scheduler=self.scheduler
        )
        monthly_event = academic_events_overlapping(date(2025, 1, 1), date(2025, 1, 31))[0]
        self.assertEqual(monthly_event.monthly_rule, rule)
        self.client.force_login(self.scheduler)
        listing = self.client.get(reverse("academic-event-list"), {"academic_year": "2024"})
        content = listing.content.decode()
        filled_monthly_events = self.rendered_academic_event_table(
            content, "academic-event-monthly-filled-events"
        )
        yearly_events = self.rendered_academic_event_table(content, "academic-event-yearly-events")
        monthly_row = self.academic_event_occurrence_row(
            filled_monthly_events,
            monthly_event,
            f"{reverse('academic-event-monthly-assignment-correct', args=(monthly_event.pk,))}?academic_year=2024&view=list",
        )
        yearly_row = self.academic_event_occurrence_row(
            yearly_events,
            yearly_event,
            f"{reverse('academic-event-correct', args=(yearly_event.pk,))}?academic_year=2024&view=list",
        )

        self.assertEqual(
            self.table_cell_text(filled_monthly_events, monthly_row, "Type"), "Monthly Event"
        )
        self.assertEqual(
            self.table_cell_text(filled_monthly_events, monthly_row, "Typically scheduled"),
            "First Thursday",
        )
        self.assertEqual(self.table_cell_text(filled_monthly_events, monthly_row, "Impact"), "Hard")
        self.assertEqual(
            self.table_cell_text(filled_monthly_events, monthly_row, "Status"), "Filled"
        )
        self.assertEqual(self.table_cell_text(yearly_events, yearly_row, "Type"), "Yearly")
        self.assertEqual(self.table_cell_text(yearly_events, yearly_row, "Status"), "Recorded")

    def test_sectioned_empty_states_are_visible_for_each_scheduling_mode(self):
        self.client.force_login(self.scheduler)

        definitions = self.client.get(
            reverse("academic-event-definition-workspace"), {"academic_year": "2024"}
        )
        listing = self.client.get(reverse("academic-event-list"), {"academic_year": "2024"})

        self.assertContains(definitions, "No yearly definitions apply to 2024-2025.")
        content = listing.content.decode()
        for identity, category_terms in (
            ("academic-event-monthly-filled-events", ("filled", "monthly")),
            ("academic-event-monthly-unfilled-events", ("unfilled", "monthly")),
            ("academic-event-yearly-events", ("scheduled", "yearly")),
        ):
            with self.subTest(identity=identity):
                table = self.rendered_academic_event_table(content, identity)
                self.assert_academic_event_empty_table(
                    table, category_terms=category_terms, academic_year="2024-2025"
                )

    def test_annual_form_excludes_monthly_definitions_and_never_derives_annual_defaults(self):
        form = AcademicEventForm(
            data={"academic_year": "2024", "definition": str(self.definition.pk)},
            apply_create_defaults=True,
            current_academic_year=2024,
        )

        self.assertNotIn(self.definition, form.fields["definition"].queryset)
        self.assertIn("definition", form.errors)

    def test_collective_monthly_rules_preserve_slot_projection_query_bound(self):
        create_academic_event_monthly_rule(
            definition=self.definition, weekday=3, ordinals=[3], scheduler=self.scheduler
        )
        physician = Physician.objects.create(display_name="Faculty physician")
        EffectiveRole.objects.create(
            physician=physician, role=EffectiveRole.Role.FACULTY, effective_start=date(2024, 1, 1)
        )

        academic_events_overlapping(date(2025, 1, 16), date(2025, 1, 16))
        with CaptureQueriesContext(connection) as queries:
            rows = slot_projection((RequestedSlot(date(2025, 1, 16), "AM"),))

        self.assertEqual(
            [(row.physician, len(row.tier_zero.academic_events)) for row in rows], [(physician, 1)]
        )
        self.assertLessEqual(len(queries), 14)

    def test_materialized_occurrence_history_uses_system_actor(self):
        create_academic_event_monthly_rule(
            definition=self.definition, weekday=3, ordinals=[3], scheduler=self.scheduler
        )
        academic_events_overlapping(date(2025, 1, 16), date(2025, 1, 16))
        self.client.force_login(self.scheduler)

        response = self.client.get(reverse("academic-event-history"))

        self.assertContains(response, "System")
        self.assertContains(response, "Monthly rule occurrence")

    @patch("schedules.services.academic_events.organization_today", return_value=date(2025, 1, 15))
    def test_retained_monthly_occurrence_links_to_its_assignment_editor_after_rule_removal(
        self, _today
    ):
        rule = create_academic_event_monthly_rule(
            definition=self.definition, weekday=3, ordinals=[1], scheduler=self.scheduler
        )
        initialize_monthly_academic_event_occurrences(
            start_date=date(2025, 1, 1), end_date=date(2025, 1, 31), scheduler=self.scheduler
        )
        remove_academic_event_monthly_rule(rule=rule, scheduler=self.scheduler)
        retained = AcademicEvent.objects.get(start_date=date(2025, 1, 2))
        self.client.force_login(self.scheduler)

        response = self.client.get(
            reverse("academic-event-workspace"), {"academic_year": "2024", "month": "2025-01"}
        )

        self.assertTrue(retained.is_active)
        self.assertIsNone(retained.monthly_rule_id)
        self.assertContains(
            response,
            f'href="{reverse("academic-event-monthly-assignment-correct", args=(retained.pk,))}?academic_year=2024&amp;view=calendar&amp;month=2025-01"',
        )
        assignment = self.client.get(
            reverse("academic-event-monthly-assignment-correct", args=(retained.pk,))
        )
        self.assertContains(
            assignment,
            f'href="{reverse("academic-event-definition-correct", args=(self.definition.pk,))}">Manage monthly rule</a>',
        )
        self.assertNotContains(assignment, 'name="start_date"')
        self.assertNotContains(assignment, "Add another date range")
        self.assertNotContains(assignment, "Remove Academic Event")
        self.assertNotContains(assignment, "Not occurring this year")
        self.assertContains(
            assignment, f'href="{reverse("academic-event-history")}">Academic Event History</a>'
        )

    def test_initializer_materializes_rule_owned_dates_idempotently_across_overlapping_ranges(self):
        rule = create_academic_event_monthly_rule(
            definition=self.definition, weekday=3, ordinals=[1, 5], scheduler=self.scheduler
        )

        initialize_monthly_academic_event_occurrences(
            start_date=date(2025, 1, 1), end_date=date(2025, 2, 28), scheduler=self.scheduler
        )
        initialize_monthly_academic_event_occurrences(
            start_date=date(2025, 1, 15), end_date=date(2025, 3, 31), scheduler=self.scheduler
        )

        occurrences = AcademicEvent.objects.filter(monthly_rule=rule, is_active=True)
        self.assertEqual(
            list(occurrences.order_by("start_date").values_list("start_date", flat=True)),
            [date(2025, 1, 2), date(2025, 1, 30), date(2025, 2, 6), date(2025, 3, 6)],
        )
        self.assertEqual(occurrences.count(), 4)
        self.assertEqual(
            AcademicEventChange.objects.filter(event__monthly_rule=rule, action="created").count(),
            4,
        )
        self.assertTrue(
            all(
                event.monthly_rule_source_id == rule.pk
                and event.start_date == event.end_date
                and event.resolution == AcademicEvent.Resolution.DATED
                for event in occurrences
            )
        )

    def test_initializer_recovers_competing_monthly_occurrence_creation_without_duplicate_history(
        self,
    ):
        rule = create_academic_event_monthly_rule(
            definition=self.definition, weekday=3, ordinals=[1], scheduler=self.scheduler
        )
        occurrence_date = date(2025, 1, 2)
        original_filter = AcademicEvent.objects.filter
        injected_competitor = False
        competing_event = None

        # Simulate a stale occurrence read followed by a competing unique insert;
        # this is not a row-locking test.
        class CompetingOccurrenceDates:
            def __init__(self, query):
                self.query = query

            def values_list(self, *fields, **kwargs):
                nonlocal competing_event, injected_competitor
                dates = tuple(self.query.values_list(*fields, **kwargs))
                injected_competitor = True
                competing_event = AcademicEvent.objects.create(
                    definition=rule.definition,
                    monthly_rule=rule,
                    monthly_rule_source_id=rule.pk,
                    academic_year=2024,
                    base_name=rule.definition.base_name,
                    expected_quarter=None,
                    required_participant_count=rule.definition.default_required_participant_count,
                    resolution=AcademicEvent.Resolution.DATED,
                    start_date=occurrence_date,
                    end_date=occurrence_date,
                    period_of_day=rule.definition.default_period_of_day,
                    precepting_impact=rule.definition.default_precepting_impact,
                    all_available_faculty=rule.definition.all_available_faculty,
                    assignment_is_default=True,
                )
                AcademicEventChange.objects.create(
                    event=competing_event,
                    event_id_at_recording=competing_event.pk,
                    action=AcademicEventChange.Action.CREATED,
                    previous_value=None,
                    recorded_value={},
                    changed_by=None,
                    reason="Competing read materialized the occurrence.",
                )
                return dates

        def inject_competitor_after_initial_occurrence_read(*args, **kwargs):
            query = original_filter(*args, **kwargs)
            if (
                not injected_competitor
                and kwargs.get("monthly_rule") == rule
                and set(kwargs.get("start_date__in", ())) == {occurrence_date}
                and kwargs.get("is_active") is True
            ):
                return CompetingOccurrenceDates(query)
            return query

        with patch.object(
            AcademicEvent.objects,
            "filter",
            side_effect=inject_competitor_after_initial_occurrence_read,
        ):
            occurrences = initialize_monthly_academic_event_occurrences(
                start_date=date(2025, 1, 1), end_date=date(2025, 1, 31)
            )

        self.assertTrue(injected_competitor)
        self.assertEqual(occurrences, (competing_event,))
        repeated_occurrences = initialize_monthly_academic_event_occurrences(
            start_date=date(2025, 1, 1), end_date=date(2025, 1, 31)
        )
        self.assertEqual(repeated_occurrences, (competing_event,))
        self.assertEqual(AcademicEvent.objects.filter(monthly_rule=rule, is_active=True).count(), 1)
        self.assertEqual(
            AcademicEventChange.objects.filter(
                event__monthly_rule=rule, action=AcademicEventChange.Action.CREATED
            ).count(),
            1,
        )

    def test_monthly_date_uniqueness_allows_multiple_instances_without_changing_yearly_invariant(
        self,
    ):
        rule = create_academic_event_monthly_rule(
            definition=self.definition, weekday=3, ordinals=[1, 2], scheduler=self.scheduler
        )
        initialize_monthly_academic_event_occurrences(
            start_date=date(2025, 1, 1), end_date=date(2025, 1, 31), scheduler=self.scheduler
        )
        first, second = AcademicEvent.objects.filter(monthly_rule=rule).order_by("start_date")

        self.assertEqual((first.academic_year, second.academic_year), (2024, 2024))
        with self.assertRaises(IntegrityError), transaction.atomic():
            AcademicEvent.objects.create(
                definition=self.definition,
                monthly_rule=rule,
                monthly_rule_source_id=rule.pk,
                academic_year=2024,
                base_name=self.definition.base_name,
                required_participant_count=1,
                resolution=AcademicEvent.Resolution.DATED,
                start_date=first.start_date,
                end_date=first.start_date,
                period_of_day=AcademicEvent.PeriodOfDay.WHOLE_DAY,
                precepting_impact=AcademicEvent.PreceptingImpact.HARD,
                all_available_faculty=True,
            )

    def test_initializer_respects_definition_academic_year_boundaries(self):
        definition = create_academic_event_definition(
            base_name="One-year monthly meeting",
            scheduling_mode="monthly_rules",
            start_academic_year=2024,
            end_academic_year=2024,
            all_available_faculty=True,
            scheduler=self.scheduler,
        )
        rule = create_academic_event_monthly_rule(
            definition=definition, weekday=3, ordinals=[1], scheduler=self.scheduler
        )

        initialize_monthly_academic_event_occurrences(
            start_date=date(2025, 6, 1), end_date=date(2025, 7, 31), scheduler=self.scheduler
        )

        self.assertEqual(
            list(
                AcademicEvent.objects.filter(monthly_rule=rule).values_list("start_date", flat=True)
            ),
            [date(2025, 6, 5)],
        )

    @patch("schedules.services.academic_events.organization_today", return_value=date(2025, 1, 15))
    def test_new_rule_materializes_today_and_future_dates_in_the_existing_operational_range(
        self, _today
    ):
        create_academic_event_monthly_rule(
            definition=self.definition, weekday=3, ordinals=[1], scheduler=self.scheduler
        )
        initialize_monthly_academic_event_occurrences(
            start_date=date(2025, 1, 1), end_date=date(2025, 6, 30), scheduler=self.scheduler
        )

        added_rule = create_academic_event_monthly_rule(
            definition=self.definition, weekday=4, ordinals=[1], scheduler=self.scheduler
        )

        self.assertEqual(
            list(
                AcademicEvent.objects.filter(monthly_rule=added_rule)
                .order_by("start_date")
                .values_list("start_date", flat=True)
            ),
            [
                date(2025, 2, 7),
                date(2025, 3, 7),
                date(2025, 4, 4),
                date(2025, 5, 2),
                date(2025, 6, 6),
            ],
        )

    @patch("schedules.services.academic_events.organization_today", return_value=date(2025, 1, 15))
    def test_rule_correction_retires_future_dates_and_keeps_past_occurrences_and_history(
        self, _today
    ):
        rule = create_academic_event_monthly_rule(
            definition=self.definition, weekday=3, ordinals=[1], scheduler=self.scheduler
        )
        initialize_monthly_academic_event_occurrences(
            start_date=date(2025, 1, 1), end_date=date(2025, 6, 30), scheduler=self.scheduler
        )
        january = AcademicEvent.objects.get(monthly_rule=rule, start_date=date(2025, 1, 2))
        history_count = january.changes.count()

        correct_academic_event_monthly_rule(
            rule=rule, weekday=3, ordinals=[2], scheduler=self.scheduler, reason="Move the meeting"
        )

        january.refresh_from_db()
        self.assertTrue(january.is_active)
        self.assertEqual(january.changes.count(), history_count)
        self.assertEqual(
            list(
                AcademicEvent.objects.filter(monthly_rule=rule, is_active=True)
                .order_by("start_date")
                .values_list("start_date", flat=True)
            ),
            [
                date(2025, 1, 2),
                date(2025, 2, 13),
                date(2025, 3, 13),
                date(2025, 4, 10),
                date(2025, 5, 8),
                date(2025, 6, 12),
            ],
        )
        retired = AcademicEvent.objects.get(monthly_rule=rule, start_date=date(2025, 2, 6))
        self.assertFalse(retired.is_active)
        self.assertEqual(retired.changes.latest("pk").action, AcademicEventChange.Action.REMOVED)
        self.assertIn("no longer matches", retired.changes.latest("pk").reason)
