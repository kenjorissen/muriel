from datetime import UTC, date, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from schedules.academic_years import (
    academic_year_bounds,
    academic_year_for_date,
    academic_year_label,
    expected_quarter_start,
)
from schedules.forms.academic_events import AcademicEventDefinitionForm, AcademicEventForm
from schedules.models import AcademicEvent, AcademicEventDefinition, AcademicEventDefinitionChange
from schedules.models.physicians import Physician
from schedules.services.academic_events import (
    academic_event_definitions_for,
    correct_academic_event_definition,
    create_academic_event_definition,
    deactivate_academic_event_definition,
)


class AcademicYearTests(TestCase):
    def test_july_and_june_boundaries_and_labels(self):
        self.assertEqual(academic_year_for_date(date(2026, 7, 1)), 2026)
        self.assertEqual(academic_year_for_date(date(2027, 6, 30)), 2026)
        self.assertEqual(academic_year_bounds(2026), (date(2026, 7, 1), date(2027, 6, 30)))
        self.assertEqual(academic_year_label(2026), "2026-2027")

    def test_expected_quarter_starts_use_the_academic_year_end_for_q3_and_q4(self):
        self.assertEqual(
            [expected_quarter_start(2026, quarter) for quarter in ("Q1", "Q2", "Q3", "Q4")],
            [date(2026, 7, 1), date(2026, 10, 1), date(2027, 1, 1), date(2027, 4, 1)],
        )

    def test_quarter_first_ranges_have_inclusive_multi_day_ends(self):
        from schedules.academic_years import expected_quarter_date_range

        self.assertEqual(
            expected_quarter_date_range(2026, "Q4", 3),
            (date(2027, 4, 1), date(2027, 4, 3)),
        )


class AcademicEventDefinitionTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )

    def create_definition(self, **overrides):
        values = {
            "base_name": "Grand rounds",
            "start_academic_year": 2026,
            "end_academic_year": None,
            "scheduler": self.scheduler,
        }
        values.update(overrides)
        return create_academic_event_definition(**values)

    def test_bounded_and_unbounded_definitions_apply_inclusively_and_sort_by_quarter_and_typical_schedule_then_name(
        self,
    ):
        bounded = self.create_definition(
            base_name="Zebra conference",
            start_academic_year=2025,
            end_academic_year=2026,
            expected_quarter="Q2",
            typically_scheduled="Late October",
        )
        unbounded = self.create_definition(
            base_name="Annual retreat",
            start_academic_year=2026,
            expected_quarter="Q1",
            typically_scheduled="Late August",
        )
        earlier_q2 = self.create_definition(
            base_name="Alpha conference",
            start_academic_year=2026,
            expected_quarter="Q2",
            typically_scheduled="Early October",
        )
        unspecific_q2 = self.create_definition(
            base_name="Middle conference", start_academic_year=2026, expected_quarter="Q2"
        )

        self.assertTrue(bounded.applies_to(2025))
        self.assertTrue(bounded.applies_to(2026))
        self.assertFalse(bounded.applies_to(2027))
        self.assertTrue(unbounded.applies_to(2099))
        self.assertEqual(bounded.effective_range_label, "2025-2026 through 2026-2027")
        self.assertEqual(
            list(academic_event_definitions_for(2026)),
            [unbounded, earlier_q2, bounded, unspecific_q2],
        )

    def test_all_quarters_are_retained_and_ordered_before_names(self):
        definitions = [
            self.create_definition(base_name=f"Event {quarter}", expected_quarter=quarter)
            for quarter in ("Q4", "Q2", "Q1", "Q3")
        ]

        self.assertEqual(
            [definition.expected_quarter for definition in academic_event_definitions_for(2026)],
            ["Q1", "Q2", "Q3", "Q4"],
        )
        self.assertEqual(
            {definition.expected_quarter for definition in definitions}, {"Q1", "Q2", "Q3", "Q4"}
        )

    def test_invalid_ranges_blank_names_and_overlapping_active_names_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_definition(base_name="   ")
        with self.assertRaises(ValidationError):
            self.create_definition(start_academic_year=2027, end_academic_year=2026)
        self.create_definition(
            base_name="Orientation", start_academic_year=2025, end_academic_year=2026
        )
        with self.assertRaises(ValidationError):
            self.create_definition(base_name="orientation", start_academic_year=2026)

    def test_changes_record_scheduler_and_values_without_noop_history(self):
        definition = self.create_definition()
        correct_academic_event_definition(
            definition=definition,
            base_name="Grand rounds",
            start_academic_year=2026,
            end_academic_year=None,
            expected_quarter="Q1",
            default_duration_days=1,
            default_period_of_day=AcademicEvent.PeriodOfDay.WHOLE_DAY,
            scheduler=self.scheduler,
        )
        correct_academic_event_definition(
            definition=definition,
            base_name="Grand rounds conference",
            start_academic_year=2026,
            end_academic_year=2027,
            expected_quarter="Q3",
            default_duration_days=3,
            default_period_of_day=AcademicEvent.PeriodOfDay.PM,
            scheduler=self.scheduler,
        )
        deactivate_academic_event_definition(definition=definition, scheduler=self.scheduler)
        deactivate_academic_event_definition(definition=definition, scheduler=self.scheduler)

        changes = list(AcademicEventDefinitionChange.objects.order_by("pk"))
        self.assertEqual(
            [change.action for change in changes], ["created", "corrected", "deactivated"]
        )
        self.assertEqual(changes[0].recorded_value["default_duration_days"], 1)
        self.assertEqual(changes[0].recorded_value["default_period_of_day"], "whole_day")
        self.assertEqual(changes[0].recorded_value["typically_scheduled"], "")
        self.assertEqual(changes[1].previous_value["base_name"], "Grand rounds")
        self.assertEqual(changes[1].recorded_value["end_academic_year"], 2027)
        self.assertEqual(changes[1].previous_value["expected_quarter"], "Q1")
        self.assertEqual(changes[1].recorded_value["expected_quarter"], "Q3")
        self.assertEqual(changes[1].previous_value["default_duration_days"], 1)
        self.assertEqual(changes[1].recorded_value["default_duration_days"], 3)
        self.assertEqual(changes[1].previous_value["default_period_of_day"], "whole_day")
        self.assertEqual(changes[1].recorded_value["default_period_of_day"], "PM")
        self.assertEqual(changes[2].changed_by, self.scheduler)
        definition.refresh_from_db()
        self.assertFalse(definition.is_active)
        self.assertEqual(list(academic_event_definitions_for(2026)), [])

    def test_typically_scheduled_is_trimmed_and_retained_through_correction_history(self):
        definition = self.create_definition(typically_scheduled="  First week of August  ")

        corrected = correct_academic_event_definition(
            definition=definition,
            base_name=definition.base_name,
            start_academic_year=definition.start_academic_year,
            typically_scheduled="Mid-August",
            scheduler=self.scheduler,
        )

        changes = list(AcademicEventDefinitionChange.objects.order_by("pk"))
        self.assertEqual(corrected.typically_scheduled, "Mid-August")
        self.assertEqual(changes[0].recorded_value["typically_scheduled"], "First week of August")
        self.assertEqual(changes[1].previous_value["typically_scheduled"], "First week of August")
        self.assertEqual(changes[1].recorded_value["typically_scheduled"], "Mid-August")

    def test_form_uses_current_relative_full_year_labels_and_range_error(self):
        form = AcademicEventDefinitionForm(
            data={
                "base_name": "Orientation",
                "expected_quarter": "Q2",
                "default_duration_days": "1",
                "default_period_of_day": AcademicEvent.PeriodOfDay.WHOLE_DAY,
                "start_academic_year": "2027",
                "end_academic_year": "2026",
            },
            current_academic_year=2026,
        )
        self.assertFalse(form.is_valid())
        self.assertTrue(any("2026-2027" in error for error in form.errors["end_academic_year"]))
        self.assertIn(("2026", "2026-2027"), form.fields["start_academic_year"].choices)
        self.assertEqual(form.fields["start_academic_year"].choices[0], ("2021", "2021-2022"))
        self.assertEqual(form.fields["start_academic_year"].choices[-1], ("2036", "2036-2037"))
        self.assertEqual(form.fields["end_academic_year"].choices[0], ("", "Unbounded"))
        self.assertEqual(form.fields["end_academic_year"].choices[-1], ("2036", "2036-2037"))

    def test_default_duration_and_coverage_are_required_and_retain_invalid_input(self):
        form = AcademicEventDefinitionForm(
            data={
                "base_name": "Orientation",
                "expected_quarter": "Q4",
                "default_duration_days": "0",
                "default_period_of_day": "",
                "start_academic_year": "2026",
                "end_academic_year": "",
            },
            current_academic_year=2026,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("default_duration_days", form.errors)
        self.assertIn("default_period_of_day", form.errors)
        self.assertEqual(form["default_duration_days"].value(), "0")
        self.assertEqual(form["default_period_of_day"].value(), "")

    def test_definition_form_requires_a_yearly_quarter_and_excludes_it_for_monthly_rules(self):
        physician = Physician.objects.create(display_name="Default participant")
        yearly = AcademicEventDefinitionForm(
            data={
                "base_name": "Yearly event",
                "scheduling_mode": "yearly",
                "default_duration_days": "1",
                "default_period_of_day": "whole_day",
                "start_academic_year": "2026",
            },
            current_academic_year=2026,
        )
        monthly = AcademicEventDefinitionForm(
            data={
                "base_name": "Monthly event",
                "scheduling_mode": "monthly_rules",
                "expected_quarter": "Q2",
                "default_duration_days": "1",
                "default_period_of_day": "whole_day",
                "default_participants": [str(physician.pk)],
                "start_academic_year": "2026",
            },
            current_academic_year=2026,
        )
        conflicting = AcademicEventDefinitionForm(
            data={
                "base_name": "Conflicting defaults",
                "scheduling_mode": "monthly_rules",
                "default_duration_days": "1",
                "default_period_of_day": "whole_day",
                "all_available_faculty": "on",
                "default_participants": [str(physician.pk)],
                "start_academic_year": "2026",
            },
            current_academic_year=2026,
        )

        self.assertFalse(yearly.is_valid())
        self.assertIn("expected_quarter", yearly.errors)
        self.assertTrue(monthly.is_valid(), monthly.errors)
        self.assertIsNone(monthly.cleaned_data["expected_quarter"])
        self.assertEqual(list(monthly.cleaned_data["default_participants"]), [physician])
        self.assertFalse(conflicting.is_valid())
        self.assertIn("all_available_faculty", conflicting.errors)

    def test_monthly_definition_form_ignores_hidden_yearly_values(self):
        form = AcademicEventDefinitionForm(
            data={
                "base_name": "Monthly event",
                "scheduling_mode": "monthly_rules",
                "expected_quarter": "Q4",
                "default_duration_days": "0",
                "default_period_of_day": "whole_day",
                "start_academic_year": "2026",
            },
            current_academic_year=2026,
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data["expected_quarter"])
        self.assertIsNone(form.cleaned_data["default_duration_days"])

    @patch("schedules.views.academic_events.organization_today", return_value=date(2026, 7, 1))
    def test_definition_create_requires_and_creates_an_initial_monthly_rule(self, _today):
        self.client.force_login(self.scheduler)
        create_url = reverse("academic-event-definition-create")
        monthly_data = {
            "base_name": "Monthly planning",
            "scheduling_mode": "monthly_rules",
            "expected_quarter": "Q4",
            "default_duration_days": "0",
            "default_period_of_day": "whole_day",
            "default_precepting_impact": "hard",
            "default_required_participant_count": "1",
            "start_academic_year": "2026",
            "end_academic_year": "",
            "weekday": "3",
            "effective_start": "2026-07-01",
            "effective_end": "",
            "reason": "Initial monthly schedule",
        }

        initial = self.client.get(create_url)
        self.assertContains(initial, 'name="effective_start"')
        self.assertContains(initial, 'value="2026-07-01"')

        invalid = self.client.post(create_url, {**monthly_data, "base_name": ""})

        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "Initial monthly rule")
        self.assertIn("base_name", invalid.context["form"].errors)
        self.assertIn("ordinals", invalid.context["initial_monthly_rule_form"].errors)
        self.assertFalse(
            AcademicEventDefinition.objects.filter(base_name="Monthly planning").exists()
        )

        created = self.client.post(create_url, {**monthly_data, "ordinals": ["1", "3"]})

        definition = AcademicEventDefinition.objects.get(base_name="Monthly planning")
        self.assertRedirects(
            created, reverse("academic-event-definition-correct", args=(definition.pk,))
        )
        self.assertIsNone(definition.expected_quarter)
        self.assertEqual(definition.default_duration_days, 1)
        self.assertEqual(
            list(definition.monthly_rules.values_list("weekday", "ordinals", "effective_start")),
            [(3, [1, 3], date(2026, 7, 1))],
        )
        page = self.client.get(reverse("academic-event-definition-correct", args=(definition.pk,)))
        self.assertContains(page, "Monthly rules")
        self.assertContains(page, "Thursday")
        self.assertContains(
            page,
            reverse("academic-event-monthly-rule-create", args=(definition.pk,)),
        )

    def test_definition_create_retains_yearly_behavior_without_a_monthly_rule(self):
        self.client.force_login(self.scheduler)

        response = self.client.post(
            reverse("academic-event-definition-create"),
            {
                "base_name": "Yearly planning",
                "scheduling_mode": "yearly",
                "expected_quarter": "Q2",
                "default_duration_days": "3",
                "default_period_of_day": "whole_day",
                "default_precepting_impact": "hard",
                "default_required_participant_count": "1",
                "start_academic_year": "2026",
                "end_academic_year": "",
                "weekday": "invalid-hidden-monthly-value",
                "ordinals": [],
            },
        )

        definition = AcademicEventDefinition.objects.get(base_name="Yearly planning")
        self.assertRedirects(response, reverse("academic-event-definition-workspace"))
        self.assertEqual(definition.expected_quarter, "Q2")
        self.assertEqual(definition.default_duration_days, 3)
        self.assertFalse(definition.monthly_rules.exists())

    def test_default_duration_must_fit_a_quarter_first_academic_year_range(self):
        with self.assertRaises(ValidationError):
            self.create_definition(expected_quarter="Q4", default_duration_days=92)

        form = AcademicEventDefinitionForm(
            data={
                "base_name": "Spring planning",
                "expected_quarter": "Q4",
                "default_duration_days": "92",
                "default_period_of_day": AcademicEvent.PeriodOfDay.AM,
                "start_academic_year": "2026",
                "end_academic_year": "",
            },
            current_academic_year=2026,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("academic year", form.errors["default_duration_days"][0])

    def test_form_retains_out_of_window_existing_bounds(self):
        form = AcademicEventDefinitionForm(
            current_academic_year=2026,
            existing_start_academic_year=1999,
            existing_end_academic_year=2040,
            initial={"start_academic_year": 1999, "end_academic_year": 2040},
        )

        self.assertIn(("1999", "1999-2000"), form.fields["start_academic_year"].choices)
        self.assertIn(("2040", "2040-2041"), form.fields["end_academic_year"].choices)

    def test_occurrence_form_uses_current_relative_years_and_definition_defaults(self):
        for coverage in AcademicEvent.PeriodOfDay.values:
            with self.subTest(coverage=coverage):
                definition = self.create_definition(
                    base_name=f"Spring planning {coverage}",
                    start_academic_year=2020,
                    expected_quarter="Q4",
                    default_duration_days=3,
                    default_period_of_day=coverage,
                    default_precepting_impact=(
                        AcademicEvent.PreceptingImpact.NONE
                        if coverage == AcademicEvent.PeriodOfDay.NONE
                        else AcademicEvent.PreceptingImpact.HARD
                    ),
                    default_required_participant_count=3,
                )

                form = AcademicEventForm(
                    current_academic_year=2026,
                    initial={"definition": definition, "academic_year": 2026},
                )

                self.assertEqual(form.fields["academic_year"].choices[0], ("2026", "2026-2027"))
                self.assertEqual(form.fields["academic_year"].choices[-1], ("2036", "2036-2037"))
                self.assertEqual(form.initial["start_date"], date(2027, 4, 1))
                self.assertEqual(form.initial["end_date"], date(2027, 4, 3))
                self.assertEqual(form.initial["period_of_day"], coverage)
                self.assertEqual(form.initial["required_participant_count"], 3)

        retained = AcademicEventForm(
            current_academic_year=2026,
            existing_academic_year=2020,
            initial={
                "definition": definition,
                "academic_year": 2020,
                "start_date": date(2020, 8, 10),
                "end_date": date(2020, 8, 11),
            },
        )
        self.assertIn(("2020", "2020-2021"), retained.fields["academic_year"].choices)
        self.assertEqual(retained.initial["start_date"], date(2020, 8, 10))
        self.assertEqual(retained.initial["end_date"], date(2020, 8, 11))

    def test_january_defaults_to_the_prior_start_year(self):
        with patch(
            "schedules.views.academic_events.organization_today", return_value=date(2027, 1, 15)
        ):
            self.client.force_login(self.scheduler)
            response = self.client.get(reverse("academic-event-definition-create"))
        self.assertContains(response, 'value="2026" selected')
        self.assertContains(response, "2026-2027")

    def test_definition_picker_caption_applies_to_all_future_events(self):
        self.client.force_login(self.scheduler)

        response = self.client.get(reverse("academic-event-definition-create"))

        self.assertContains(
            response,
            "0 selected physicians. Choose physicians to preassign to future events.",
        )
        self.assertNotContains(response, "future monthly occurrences")

    def test_form_defaults_follow_the_organization_date_at_chicago_midnight(self):
        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2027, 7, 1, 4, 59, tzinfo=UTC),
        ):
            event_form = AcademicEventForm()
            definition_form = AcademicEventDefinitionForm()

        self.assertEqual(event_form.fields["academic_year"].initial, 2026)
        self.assertEqual(definition_form.fields["start_academic_year"].initial, 2026)

    def test_workspace_academic_year_navigation_uses_shared_cluster_and_stays_on_definitions(self):
        self.client.force_login(self.scheduler)
        workspace_url = reverse("academic-event-definition-workspace")

        with patch("schedules.views.academic_events._current_academic_year", return_value=2026):
            response = self.client.get(workspace_url, {"academic_year": "2026"})

            self.assertEqual(response.context["academic_year"], 2026)
            self.assertTemplateUsed(
                response, "schedules/schedule_workspace_academic_year_navigation.html"
            )
            self.assertContains(response, 'aria-label="Academic year navigation"')
            self.assertContains(response, 'value="2026" selected')
            self.assertContains(response, "2026-2027")
            self.assertContains(
                response,
                f'action="{workspace_url}" method="get" data-calendar-period-picker',
            )
            self.assertContains(response, f'href="{workspace_url}?academic_year=2025"')
            self.assertContains(response, f'href="{workspace_url}?academic_year=2027"')

            for year, disabled_label, adjacent_year in (
                (2021, "Previous", 2022),
                (2031, "Next", 2030),
            ):
                with self.subTest(year=year):
                    endpoint = self.client.get(workspace_url, {"academic_year": str(year)})

                    self.assertEqual(endpoint.status_code, 200)
                    self.assertEqual(endpoint.context["academic_year"], year)
                    self.assertContains(
                        endpoint,
                        f'<span role="link" aria-disabled="true" aria-label="{disabled_label} academic year">',
                    )
                    self.assertContains(
                        endpoint, f'href="{workspace_url}?academic_year={adjacent_year}"'
                    )
