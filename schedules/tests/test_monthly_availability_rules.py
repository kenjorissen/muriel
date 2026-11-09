import re
from datetime import UTC, date, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse

from schedules.models import (
    EffectiveRole,
    FacultyAvailabilityTemplate,
    MonthlyPhysicianAvailabilityRule,
    Physician,
    PhysicianChange,
    PreceptorAssignment,
    PreceptorSession,
)
from schedules.monthly_rule_presentation import monthly_rule_summary, monthly_rule_summary_values
from schedules.services.availability import effective_preceptor_availability_on
from schedules.services.availability_categories import (
    create_custom_availability_category,
    deactivate_custom_availability_category,
    set_custom_availability_category_physicians,
)
from schedules.services.physicians import (
    correct_monthly_physician_availability_rule,
    create_faculty_availability_template,
    create_monthly_physician_availability_rule,
    deactivate_monthly_physician_availability_rule,
    faculty_availability_on,
)
from schedules.services.preceptors import (
    assign_preceptor,
    assignment_alternatives_for_session,
    draft_findings_for_session,
    open_monthly_draft,
)
from schedules.services.slot_projection import RequestedSlot, slot_projection
from schedules.services.vacations import create_vacation
from schedules.tests.test_table_presentation import _RenderedDataTable
from schedules.tests.test_work_remaining import _action_matches, _RenderedSurfaceParser

_ANNUAL_RECURRENCE_AND_WEEKLY_FALLBACK = re.compile(
    r"(?=.*(?:\b(?:every|each) year\b|\bannually\b))"
    r"(?=.*\b(?:Outside|Beyond)(?: the)? annual window\b[^.]*"
    r"\bweekly template (?:applies|takes over)\b)",
    re.DOTALL,
)


class MonthlyPhysicianAvailabilityRuleServiceTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.physician = Physician.objects.create(display_name="Faculty physician")
        EffectiveRole.objects.create(
            physician=self.physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
        )

    def create_rule(self, **overrides):
        values = {
            "physician": self.physician,
            "weekday": 1,
            "ordinals": [1],
            "periods": ["AM"],
            "availability_selection": FacultyAvailabilityTemplate.base_selection("open"),
            "scheduler": self.scheduler,
        }
        values.update(overrides)
        return create_monthly_physician_availability_rule(**values)

    def test_recurrence_handles_all_fifth_occurrences_and_inclusive_range_edges(self):
        rule = self.create_rule(
            weekday=1,
            ordinals="all",
            periods=["AM", "PM"],
            effective_start=date(2025, 2, 4),
            effective_end=date(2025, 4, 29),
        )

        self.assertEqual(rule.ordinals, [1, 2, 3, 4, 5])
        self.assertTrue(rule.matches(date(2025, 2, 4), "AM"))
        self.assertTrue(rule.matches(date(2025, 4, 29), "PM"))
        self.assertFalse(rule.matches(date(2025, 5, 6), "AM"))
        self.assertTrue(rule.matches(date(2025, 4, 29), "AM"))
        self.assertEqual(rule.ordinal_display, "First, Second, Third, Fourth, Fifth")
        self.assertEqual(rule.periods_display, "AM and PM")

    def test_annual_windows_match_inclusively_and_preserve_all_year_rules(self):
        all_year = self.create_rule(weekday=1, ordinals=[1])
        spring = self.create_rule(weekday=2, ordinals=[1], annual_start_month=3, annual_end_month=5)
        april_only = self.create_rule(
            weekday=3, ordinals=[1], annual_start_month=4, annual_end_month=4
        )
        school_year = self.create_rule(
            weekday=0,
            ordinals=[1],
            annual_start_month=9,
            annual_end_month=5,
            effective_start=date(2025, 1, 1),
        )

        self.assertTrue(all_year.matches(date(2025, 6, 3), "AM"))
        self.assertTrue(spring.matches(date(2025, 3, 5), "AM"))
        self.assertTrue(spring.matches(date(2025, 4, 2), "AM"))
        self.assertTrue(spring.matches(date(2025, 5, 7), "AM"))
        self.assertFalse(spring.matches(date(2025, 6, 4), "AM"))
        self.assertTrue(april_only.matches(date(2025, 4, 3), "AM"))
        self.assertFalse(april_only.matches(date(2025, 5, 1), "AM"))
        self.assertTrue(school_year.matches(date(2025, 1, 6), "AM"))
        self.assertFalse(school_year.matches(date(2025, 6, 2), "AM"))
        self.assertTrue(school_year.matches(date(2025, 9, 1), "AM"))

    def test_rule_summary_describes_recurrence_effective_bounds_and_weekly_fallback(self):
        training_room = {"category_id": 1, "category_name": "Training Room"}
        annual_summary = monthly_rule_summary_values(
            weekday="Wednesday",
            ordinals=[1, 2, 3, 4, 5],
            periods=["PM"],
            availability_selection=training_room,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            annual_start_month=9,
            annual_end_month=5,
        )
        bounded_summary = monthly_rule_summary_values(
            weekday="Friday",
            ordinals=[2, 4],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("backup_only"),
            effective_start=date(2025, 2, 1),
            effective_end=date(2025, 3, 31),
            annual_start_month=3,
            annual_end_month=5,
        )
        all_year_summary = monthly_rule_summary_values(
            weekday="Monday",
            ordinals=[1],
            periods=["AM", "PM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("open"),
            effective_start=None,
            effective_end=None,
            annual_start_month=None,
            annual_end_month=None,
        )

        for expected in (
            "January 1, 2025",
            "Training Room",
            "September 1",
            "May 31",
            "every Wednesday PM",
        ):
            with self.subTest(summary="annual", expected=expected):
                self.assertIn(expected, annual_summary)
        self.assertRegex(annual_summary, _ANNUAL_RECURRENCE_AND_WEEKLY_FALLBACK)
        for expected in (
            "February 1, 2025",
            "March 31, 2025",
            "Backup only",
            "March 1",
            "May 31",
            "second and fourth Friday AM",
        ):
            with self.subTest(summary="bounded", expected=expected):
                self.assertIn(expected, bounded_summary)
        self.assertRegex(bounded_summary, _ANNUAL_RECURRENCE_AND_WEEKLY_FALLBACK)
        for expected in ("Open", "first Monday AM and PM"):
            with self.subTest(summary="all-year", expected=expected):
                self.assertIn(expected, all_year_summary)

    def test_february_annual_window_includes_leap_day_and_summarizes_the_month_end(self):
        rule = self.create_rule(
            weekday=1,
            ordinals=[5],
            annual_start_month=11,
            annual_end_month=2,
        )

        self.assertTrue(rule.matches(date(2028, 2, 29), "AM"))
        self.assertIn("Open", monthly_rule_summary(rule))
        self.assertIn("November 1", monthly_rule_summary(rule))
        self.assertIn("end of February", monthly_rule_summary(rule))
        self.assertIn("fifth Tuesday AM", monthly_rule_summary(rule))
        self.assertIn("weekly template applies", monthly_rule_summary(rule))

    def test_annual_window_requires_two_valid_calendar_months(self):
        partial = MonthlyPhysicianAvailabilityRule(
            physician=self.physician,
            weekday=1,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("open"),
            annual_start_month=3,
        )

        with self.assertRaisesRegex(ValidationError, "Choose both annual window months"):
            partial.full_clean()
        with self.assertRaisesRegex(ValidationError, "Choose both annual window months"):
            self.create_rule(annual_start_month=3)
        with self.assertRaisesRegex(ValidationError, "Choose valid calendar months"):
            self.create_rule(annual_start_month=0, annual_end_month=5)

    def test_annual_windows_allow_disjoint_rules_and_reject_overlap_across_new_year(self):
        self.create_rule(
            weekday=2,
            ordinals=[1],
            periods=["AM"],
            annual_start_month=9,
            annual_end_month=5,
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 1),
        )
        self.create_rule(
            weekday=2,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("backup_only"),
            annual_start_month=6,
            annual_end_month=8,
        )

        with self.assertRaisesRegex(ValidationError, "same physician, date, and period"):
            self.create_rule(
                weekday=2,
                ordinals=[1],
                periods=["AM"],
                availability_selection=FacultyAvailabilityTemplate.base_selection("off"),
                annual_start_month=12,
                annual_end_month=2,
                effective_start=date(2025, 1, 1),
                effective_end=date(2025, 1, 1),
            )

        self.create_rule(
            weekday=2,
            ordinals=[1],
            periods=["AM"],
            annual_start_month=9,
            annual_end_month=5,
            effective_start=date(2025, 12, 3),
            effective_end=date(2025, 12, 3),
        )
        with self.assertRaisesRegex(ValidationError, "same physician, date, and period"):
            self.create_rule(
                weekday=2,
                ordinals=[1],
                periods=["AM"],
                availability_selection=FacultyAvailabilityTemplate.base_selection("off"),
                annual_start_month=11,
                annual_end_month=12,
                effective_start=date(2025, 12, 3),
                effective_end=date(2025, 12, 3),
            )

    def test_overlap_requires_a_shared_actual_date_ordinal_and_period(self):
        first = self.create_rule(
            weekday=2,
            ordinals=[1],
            periods=["AM"],
            effective_start=date(2025, 2, 5),
            effective_end=date(2025, 2, 5),
        )
        fifth = self.create_rule(
            weekday=2,
            ordinals=[5],
            periods=["AM"],
            effective_start=date(2025, 2, 5),
            effective_end=date(2025, 2, 5),
        )
        pm = self.create_rule(
            weekday=2,
            ordinals=[1],
            periods=["PM"],
            effective_start=date(2025, 2, 5),
            effective_end=date(2025, 2, 5),
        )
        history_count = PhysicianChange.objects.count()

        with self.assertRaisesRegex(ValidationError, "same physician, date, and period"):
            self.create_rule(
                weekday=2,
                ordinals=[1],
                periods=["AM"],
                availability_selection=FacultyAvailabilityTemplate.base_selection("off"),
                effective_start=date(2025, 2, 5),
                effective_end=date(2025, 2, 5),
            )

        actual_fifth = self.create_rule(
            weekday=1,
            ordinals=[5],
            effective_start=date(2025, 4, 29),
            effective_end=date(2025, 4, 29),
        )
        with self.assertRaisesRegex(ValidationError, "same physician, date, and period"):
            self.create_rule(
                weekday=1,
                ordinals=[5],
                availability_selection=FacultyAvailabilityTemplate.base_selection("backup_only"),
                effective_start=date(2025, 4, 29),
                effective_end=date(2025, 4, 29),
            )

        self.assertEqual(
            {first.pk, fifth.pk, pm.pk, actual_fifth.pk},
            set(MonthlyPhysicianAvailabilityRule.objects.values_list("pk", flat=True)),
        )
        self.assertEqual(PhysicianChange.objects.count(), history_count + 1)

    def test_category_identity_history_and_deactivation_are_retained(self):
        category = create_custom_availability_category(
            name="Sports Med Training Room", impact="backup_only", scheduler=self.scheduler
        )
        set_custom_availability_category_physicians(
            category=category, physicians=[self.physician], scheduler=self.scheduler
        )
        rule = self.create_rule(
            availability_selection={"category_id": category.pk}, reason="Clinic"
        )

        self.assertEqual(
            rule.availability_selection,
            {"category_id": category.pk, "category_name": "Sports Med Training Room"},
        )
        deactivate_custom_availability_category(category=category, scheduler=self.scheduler)
        deactivate_monthly_physician_availability_rule(
            rule=rule, scheduler=self.scheduler, reason="No longer needed"
        )
        rule.refresh_from_db()
        changes = PhysicianChange.objects.filter(
            physician=self.physician,
            subject=PhysicianChange.Subject.MONTHLY_AVAILABILITY_RULE,
        ).order_by("pk")

        self.assertFalse(rule.is_active)
        self.assertEqual(rule.availability_display, "Sports Med Training Room")
        self.assertEqual(changes.count(), 2)
        self.assertEqual(changes.first().changed_by, self.scheduler)
        self.assertEqual(changes.latest("pk").previous_value["is_active"], True)
        self.assertFalse(changes.latest("pk").recorded_value["is_active"])
        self.assertEqual(changes.latest("pk").reason, "No longer needed")
        with self.assertRaises(ValidationError):
            rule.delete()

    def test_rules_require_faculty_and_an_authenticated_scheduler(self):
        nonfaculty = Physician.objects.create(display_name="Not faculty")
        with self.assertRaises(ValidationError):
            self.create_rule(physician=nonfaculty)
        anonymous = type("Anonymous", (), {"is_authenticated": False})()
        with self.assertRaises(PermissionDenied):
            self.create_rule(scheduler=anonymous)

    def test_matching_rule_replaces_the_template_base_availability(self):
        slots = {
            slot: FacultyAvailabilityTemplate.base_selection("off")
            for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
        }
        slots["tuesday_am"] = FacultyAvailabilityTemplate.base_selection("open")
        create_faculty_availability_template(
            physician=self.physician,
            effective_start=None,
            effective_end=None,
            slots=slots,
            scheduler=self.scheduler,
        )
        self.create_rule(
            availability_selection=FacultyAvailabilityTemplate.base_selection("off"),
            effective_start=date(2025, 2, 4),
            effective_end=date(2025, 2, 4),
        )

        self.assertEqual(
            faculty_availability_on(self.physician, date(2025, 2, 4), "AM").base_availability,
            "off",
        )

    def test_annual_window_falls_back_to_the_weekly_template_outside_its_months(self):
        slots = {
            slot: FacultyAvailabilityTemplate.base_selection("off")
            for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
        }
        slots["tuesday_am"] = FacultyAvailabilityTemplate.base_selection("open")
        create_faculty_availability_template(
            physician=self.physician,
            effective_start=None,
            effective_end=None,
            slots=slots,
            scheduler=self.scheduler,
        )
        self.create_rule(
            availability_selection=FacultyAvailabilityTemplate.base_selection("off"),
            annual_start_month=3,
            annual_end_month=5,
        )

        self.assertEqual(
            faculty_availability_on(self.physician, date(2025, 3, 4), "AM").base_availability,
            "off",
        )
        self.assertEqual(
            faculty_availability_on(self.physician, date(2025, 6, 3), "AM").base_availability,
            "open",
        )

    def test_matching_rule_is_the_shared_base_source_before_independent_blocks(self):
        create_faculty_availability_template(
            physician=self.physician,
            effective_start=None,
            effective_end=None,
            slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.scheduler,
        )
        rule = self.create_rule(
            availability_selection=FacultyAvailabilityTemplate.base_selection("backup_only"),
            effective_start=date(2025, 2, 4),
            effective_end=date(2025, 2, 4),
        )
        on_date = date(2025, 2, 4)
        availability = effective_preceptor_availability_on(self.physician, on_date, "AM")
        row = next(
            row
            for row in slot_projection((RequestedSlot(on_date, "AM"),))
            if row.physician == self.physician
        )
        session = open_monthly_draft(on_date).sessions.get(
            date=on_date, period_of_day=PreceptorSession.PeriodOfDay.AM
        )

        self.assertEqual(
            (availability.base_availability, availability.availability),
            ("backup_only", "backup_only"),
        )
        self.assertEqual(availability.monthly_availability_rule, rule)
        self.assertEqual(
            [(source.source_type, source.source_id) for source in availability.sources],
            [("monthly_availability_rule", rule.pk)],
        )
        self.assertEqual(row.tier_zero, availability)
        candidate = next(
            candidate
            for candidate in assignment_alternatives_for_session(
                session, PreceptorAssignment.Responsibility.PRIMARY, 1
            )
            if candidate.physician == self.physician
        )
        self.assertEqual(candidate.tier, "backup_only")
        self.assertEqual(candidate.sources[0].record, rule)

        assign_preceptor(
            session=session,
            physician=self.physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        finding = next(
            finding
            for finding in draft_findings_for_session(session).findings
            if finding.code == "backup_only_assignment"
        )
        self.assertEqual(finding.sources[0].record, rule)
        vacation = create_vacation(
            physician=self.physician,
            start_date=on_date,
            end_date=on_date,
            period_of_day="AM",
            scheduler=self.scheduler,
        )
        blocked = effective_preceptor_availability_on(self.physician, on_date, "AM")

        self.assertEqual(blocked.availability, "not_available")
        self.assertEqual(
            [(source.source_type, source.source_id) for source in blocked.sources],
            [("monthly_availability_rule", rule.pk), ("vacation", vacation.pk)],
        )

    def test_matching_rule_supplies_a_base_state_when_no_template_exists(self):
        rule = self.create_rule(
            availability_selection=FacultyAvailabilityTemplate.base_selection("open"),
            effective_start=date(2025, 2, 4),
            effective_end=date(2025, 2, 4),
        )

        matching = effective_preceptor_availability_on(self.physician, date(2025, 2, 4), "AM")
        unmatched = effective_preceptor_availability_on(self.physician, date(2025, 2, 11), "AM")

        self.assertEqual((matching.base_availability, matching.availability), ("open", "available"))
        self.assertEqual(matching.sources[0].record, rule)
        self.assertEqual(
            (unmatched.base_availability, unmatched.availability),
            ("off", "not_available"),
        )

    def test_base_status_chooses_the_matching_rule_among_non_overlapping_rules(self):
        self.create_rule(
            availability_selection=FacultyAvailabilityTemplate.base_selection("off"),
            effective_start=date(2025, 2, 4),
            effective_end=date(2025, 2, 4),
        )
        self.create_rule(
            ordinals=[2],
            availability_selection=FacultyAvailabilityTemplate.base_selection("open"),
            effective_start=date(2025, 2, 11),
            effective_end=date(2025, 2, 11),
        )

        status = faculty_availability_on(self.physician, date(2025, 2, 11), "AM")

        self.assertEqual(status.base_availability, "open")

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 18, tzinfo=UTC),
    )
    def test_historical_monthly_rule_can_retain_an_unassigned_option_but_cannot_extend(self, now):
        EffectiveRole.objects.create(
            physician=self.physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2024, 1, 1),
            effective_end=date(2024, 12, 31),
        )
        category = create_custom_availability_category(
            name="Retained option", impact="available", scheduler=self.scheduler
        )
        set_custom_availability_category_physicians(
            category=category, physicians=[self.physician], scheduler=self.scheduler
        )
        rule = self.create_rule(availability_selection={"category_id": category.pk})
        assignment = category.assignments.get(physician=self.physician)
        assignment.is_active = False
        assignment.save(update_fields=("is_active",))

        correct_monthly_physician_availability_rule(
            rule=rule,
            weekday=rule.weekday,
            ordinals=rule.ordinals,
            periods=rule.periods,
            availability_selection={"category_id": category.pk},
            effective_end=date(2024, 12, 31),
            scheduler=self.scheduler,
        )
        history_count = PhysicianChange.objects.count()

        with self.assertRaisesRegex(ValidationError, "Current and future schedule facts"):
            correct_monthly_physician_availability_rule(
                rule=rule,
                weekday=rule.weekday,
                ordinals=rule.ordinals,
                periods=rule.periods,
                availability_selection={"category_id": category.pk},
                effective_end=None,
                scheduler=self.scheduler,
            )

        rule.refresh_from_db()
        self.assertEqual(rule.effective_end, date(2024, 12, 31))
        self.assertEqual(PhysicianChange.objects.count(), history_count)


class MonthlyPhysicianAvailabilityRuleViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.physician = Physician.objects.create(display_name="Taylor Morgan")
        EffectiveRole.objects.create(
            physician=self.physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
        )
        self.client.force_login(self.user)

    def assert_training_rule_summary(self, summary):
        for expected in (
            "Training Room",
            "January 1, 2025",
            "September 1",
            "May 31",
            "every Wednesday PM",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, summary)
        self.assertRegex(summary, _ANNUAL_RECURRENCE_AND_WEEKLY_FALLBACK)

    @staticmethod
    def _rendered_availability_select(response):
        parser = _RenderedSurfaceParser()
        parser.feed(response.content.decode())
        selects = [
            element
            for element in parser.elements
            if element.tag == "select" and element.attributes.get("name") == "availability"
        ]
        if len(selects) != 1:
            raise AssertionError(
                f"Expected one rendered availability select, found {len(selects)}."
            )
        return selects[0]

    def assert_rendered_schedule_option(self, response, *, value, name, impact, selected=False):
        select = self._rendered_availability_select(response)
        groups = [child for child in select.children if getattr(child, "tag", None) == "optgroup"]
        fixed_states = next(
            group for group in groups if group.attributes.get("label") == "Fixed states"
        )
        schedule_options = next(
            group for group in groups if group.attributes.get("label") == "Schedule options"
        )
        self.assertLess(groups.index(fixed_states), groups.index(schedule_options))
        option = next(
            option
            for option in select.descendants()
            if option.tag == "option" and option.attributes.get("value") == value
        )
        self.assertIn(name, option.text())
        self.assertIn(impact, option.text())
        if selected:
            selected_options = [
                candidate
                for candidate in select.descendants()
                if candidate.tag == "option" and "selected" in candidate.attributes
            ]
            self.assertEqual(
                [option.attributes.get("value") for option in selected_options], [value]
            )

    @staticmethod
    def _field_group_with_error(parser, field_name):
        control = next(
            element
            for element in parser.elements
            if element.attributes.get("name") == field_name
            and element.tag in {"input", "select", "textarea"}
        )
        field_group = next(
            ancestor
            for ancestor in control.ancestors()
            if "form-field-group" in ancestor.attributes.get("class", "").split()
        )
        errors = [
            element
            for element in field_group.descendants()
            if "errorlist" in element.attributes.get("class", "").split()
        ]
        return control, errors

    @staticmethod
    def _rendered_table(response, identity):
        return _RenderedDataTable(response.content.decode(), identity)

    @staticmethod
    def _row_linking_to(table, url):
        return next(
            row
            for row in table.body_rows()
            if any(
                _action_matches(link.attributes.get("href", ""), url)
                for link in row.descendants_named("a")
            )
        )

    def test_physician_context_can_create_edit_and_deactivate_retained_rule(self):
        create_url = reverse(
            "physician-monthly-availability-rule-create", args=(self.physician.pk,)
        )
        response = self.client.get(create_url)

        self.assertContains(response, "Monthly scheduling rule")
        self.assertContains(response, "Schedule option")
        self.assertContains(response, "All occurrences")
        self.assertContains(
            response, "Active rules cannot cover the same physician, weekday, and period."
        )
        response = self.client.post(
            create_url,
            {
                "weekday": "1",
                "ordinals": ["2"],
                "periods": ["AM", "PM"],
                "annual_window": "all_year",
                "availability": "base:backup_only",
                "effective_start": "2025-02-01",
                "effective_end": "",
                "reason": "Second Tuesday clinic",
            },
        )
        self.assertRedirects(response, reverse("physician-detail", args=(self.physician.pk,)))
        rule = MonthlyPhysicianAvailabilityRule.objects.get()
        detail = self.client.get(reverse("physician-detail", args=(self.physician.pk,)))
        self.assertContains(detail, "Monthly scheduling rules")
        self.assertContains(detail, "Backup only")
        edit_url = reverse(
            "physician-monthly-availability-rule-correct", args=(self.physician.pk, rule.pk)
        )
        edit = self.client.get(edit_url)
        self.assertContains(edit, "Editing this rule")
        self.assertContains(edit, "Deactivate rule")
        self.assertContains(edit, 'id="recurrence-rule-context" class="data-table"')

        response = self.client.post(
            reverse(
                "physician-monthly-availability-rule-deactivate", args=(self.physician.pk, rule.pk)
            ),
            {"reason": "Retired clinic"},
        )
        self.assertRedirects(response, reverse("physician-detail", args=(self.physician.pk,)))
        rule.refresh_from_db()
        self.assertFalse(rule.is_active)
        self.assertContains(
            self.client.get(reverse("physician-history", args=(self.physician.pk,))),
            "Retired clinic",
        )

    def test_annual_window_form_and_retained_views_use_the_readable_summary(self):
        create_url = reverse(
            "physician-monthly-availability-rule-create", args=(self.physician.pk,)
        )
        form = self.client.get(create_url)

        self.assertContains(form, "Annual window")
        self.assertContains(form, 'value="all_year" required')
        self.assertContains(form, "data-monthly-annual-months")
        invalid = self.client.post(
            create_url,
            {
                "weekday": "2",
                "ordinals": ["all"],
                "periods": ["PM"],
                "annual_window": "window",
                "annual_start_month": "9",
                "availability": "base:open",
                "effective_start": "2025-01-01",
            },
        )
        for field_name in ("annual_start_month", "annual_end_month"):
            with self.subTest(field_name=field_name):
                self.assertTrue(invalid.context["form"][field_name].errors)
                parser = _RenderedSurfaceParser()
                parser.feed(invalid.content.decode())
                control, errors = self._field_group_with_error(parser, field_name)
                self.assertTrue(errors)
                self.assertTrue(
                    any("both annual window months" in error.text() for error in errors)
                )
                described_by_ids = control.attributes.get("aria-describedby", "").split()
                if described_by_ids:
                    self.assertTrue(
                        any(error.attributes.get("id") in described_by_ids for error in errors)
                    )
        self.assertFalse(MonthlyPhysicianAvailabilityRule.objects.exists())
        self.assertFalse(PhysicianChange.objects.exists())

        category = create_custom_availability_category(
            name="Training Room", impact="available", scheduler=self.user
        )
        set_custom_availability_category_physicians(
            category=category, physicians=[self.physician], scheduler=self.user
        )
        rule = create_monthly_physician_availability_rule(
            physician=self.physician,
            weekday=2,
            ordinals="all",
            periods=["PM"],
            availability_selection={"category_id": category.pk},
            effective_start=date(2025, 1, 1),
            annual_start_month=9,
            annual_end_month=5,
            scheduler=self.user,
        )
        create_monthly_physician_availability_rule(
            physician=self.physician,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("open"),
            scheduler=self.user,
        )
        detail = self.client.get(reverse("physician-detail", args=(self.physician.pk,)))
        correction_url = reverse(
            "physician-monthly-availability-rule-correct", args=(self.physician.pk, rule.pk)
        )
        detail_table = self._rendered_table(detail, "monthly-availability-rules")
        detail_row = self._row_linking_to(detail_table, correction_url)
        self.assert_training_rule_summary(
            detail_table.cell_for_header(detail_row, "Rule summary").text()
        )
        correction = self.client.get(correction_url)
        correction_table = self._rendered_table(correction, "recurrence-rule-context")
        correction_row = next(
            row
            for row in correction_table.body_rows()
            if "Training Room" in row.text() and "Editing this rule" in row.text()
        )
        self.assert_training_rule_summary(
            correction_table.cell_for_header(correction_row, "Rule summary").text()
        )
        history = self.client.get(reverse("physician-history", args=(self.physician.pk,)))
        history_table = self._rendered_table(history, "physician-history")
        history_row = next(
            row
            for row in history_table.body_rows()
            if history_table.cell_for_header(row, "Fact").text() == "Monthly scheduling rule"
            and "Training Room" in history_table.cell_for_header(row, "Recorded value").text()
        )
        self.assert_training_rule_summary(
            history_table.cell_for_header(history_row, "Recorded value").text()
        )

    def test_recurrence_context_labels_inactive_rules_without_losing_current_rule_attention(self):
        current_rule = MonthlyPhysicianAvailabilityRule.objects.create(
            physician=self.physician,
            weekday=1,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("open"),
        )
        MonthlyPhysicianAvailabilityRule.objects.create(
            physician=self.physician,
            weekday=2,
            ordinals=[2],
            periods=["PM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("open"),
            is_active=False,
        )

        response = self.client.get(
            reverse(
                "physician-monthly-availability-rule-correct",
                args=(self.physician.pk, current_rule.pk),
            )
        )

        self.assertContains(response, 'class="data-table-cell--status data-table-cell--attention"')
        self.assertContains(response, "Editing this rule")
        self.assertContains(response, 'class="data-table-cell--metadata">Deactivated rule</td>')

    def test_effective_availability_presents_matching_rule_provenance(self):
        rule = create_monthly_physician_availability_rule(
            physician=self.physician,
            weekday=1,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("backup_only"),
            scheduler=self.user,
        )

        response = self.client.get(
            reverse("physician-detail", args=(self.physician.pk,)), {"date": "2025-02-04"}
        )

        self.assertContains(response, f"Monthly scheduling rule {rule.pk}")

    def test_form_labels_active_and_retained_schedule_options_with_their_impacts(self):
        category = create_custom_availability_category(
            name="Sports Med Training Room", impact="backup_only", scheduler=self.user
        )
        set_custom_availability_category_physicians(
            category=category, physicians=[self.physician], scheduler=self.user
        )
        create_url = reverse(
            "physician-monthly-availability-rule-create", args=(self.physician.pk,)
        )

        response = self.client.get(create_url)

        self.assert_rendered_schedule_option(
            response,
            value=f"category:{category.pk}",
            name="Sports Med Training Room",
            impact="Backup only",
        )
        rule = create_monthly_physician_availability_rule(
            physician=self.physician,
            weekday=1,
            ordinals=[1],
            periods=["AM"],
            availability_selection={"category_id": category.pk},
            scheduler=self.user,
        )
        deactivate_custom_availability_category(category=category, scheduler=self.user)

        edit = self.client.get(
            reverse(
                "physician-monthly-availability-rule-correct", args=(self.physician.pk, rule.pk)
            )
        )

        self.assert_rendered_schedule_option(
            edit,
            value=f"category:{category.pk}",
            name="Sports Med Training Room",
            impact="Backup only",
            selected=True,
        )
        unassigned_category = create_custom_availability_category(
            name="Procedure Clinic", impact="not_available", scheduler=self.user
        )
        set_custom_availability_category_physicians(
            category=unassigned_category, physicians=[self.physician], scheduler=self.user
        )
        unassigned_rule = create_monthly_physician_availability_rule(
            physician=self.physician,
            weekday=2,
            ordinals=[1],
            periods=["AM"],
            availability_selection={"category_id": unassigned_category.pk},
            scheduler=self.user,
        )
        deactivate_monthly_physician_availability_rule(rule=unassigned_rule, scheduler=self.user)
        set_custom_availability_category_physicians(
            category=unassigned_category, physicians=[], scheduler=self.user
        )

        unassigned_edit = self.client.get(
            reverse(
                "physician-monthly-availability-rule-correct",
                args=(self.physician.pk, unassigned_rule.pk),
            )
        )

        self.assert_rendered_schedule_option(
            unassigned_edit,
            value=f"category:{unassigned_category.pk}",
            name="Procedure Clinic",
            impact="Not available",
            selected=True,
        )
