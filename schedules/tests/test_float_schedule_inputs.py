import json
import socket
import subprocess
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from schedules.forms.physicians import (
    FacultyAvailabilityTemplateForm,
    MonthlyPhysicianAvailabilityRuleForm,
)
from schedules.models import (
    CustomAvailabilityCategoryAssignment,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    MonthlyPhysicianAvailabilityRule,
    Physician,
    PhysicianChange,
)
from schedules.services.availability import effective_preceptor_availability_on
from schedules.services.availability_categories import (
    create_custom_availability_category,
    ensure_system_schedule_options,
    set_custom_availability_category_physicians,
)
from schedules.services.community import create_community_preceptor_rule
from schedules.services.operational_attention import organization_today
from schedules.services.physicians import (
    add_role,
    create_faculty_availability_template,
    create_monthly_physician_availability_rule,
)
from schedules.tests.browser_tools import require_browser_tools


def _selection_choices(field):
    for value, label in field.choices:
        if isinstance(label, list):
            yield from label
        else:
            yield value, label


def _base_choices(field):
    return [choice for choice in _selection_choices(field) if choice[0].startswith("base:")]


def _custom_choice_values(field):
    return [value for value, _label in _selection_choices(field) if value.startswith("category:")]


def _choice_label(field, value):
    return dict(_selection_choices(field))[value]


_BROWSER_RUNNER = r"""
const { readFileSync } = require("fs");

const input = JSON.parse(readFileSync(0, "utf8"));
const { chromium } = require(process.env.PRECEPTOR_PLAYWRIGHT_MODULE);

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: input.browserExecutable });
  try {
  const context = await browser.newContext();
  await context.addCookies([{ name: input.sessionCookie.name, value: input.sessionCookie.value, url: input.baseUrl }]);
  const page = await context.newPage();
  for (const [url, allowedValues] of [[input.templateUrl, input.templateAllowedValues], [input.ruleUrl, input.ruleAllowedValues]]) {
    const response = await page.goto(input.baseUrl + url, { waitUntil: "domcontentloaded" });
    if (!response || !response.ok()) throw new Error(`Schedule input did not load: ${url}`);
    const values = await page.locator(input.templateUrl === url ? 'input[name="monday_am"]' : 'select[name="availability"] option').evaluateAll((elements) => elements.map((element) => element.value));
    const missingValues = allowedValues.filter((value) => !values.includes(value));
    const unexpectedValues = values.filter((value) => !allowedValues.includes(value));
    if (missingValues.length || unexpectedValues.length) throw new Error(`Unexpected choices for ${url}: ${JSON.stringify(values)}`);
    const adminValue = allowedValues.find((value) => value.startsWith("category:"));
    const adminControl = page.locator(input.templateUrl === url ? `input[name="monday_am"][value="${adminValue}"]` : `select[name="availability"] option[value="${adminValue}"]`);
    const adminLabel = await adminControl.evaluate((element) => element.labels ? element.labels[0].textContent : element.textContent);
    if (!adminLabel.includes("Admin") || !adminLabel.includes("Float") || adminLabel.includes("Not available")) {
      throw new Error(`Admin control does not express Float capability for ${url}: ${adminLabel}`);
    }
    const helperScope = input.templateUrl === url
      ? page.locator("header.form-page-intro:has(h1.form-page-title):has(a.form-return-link)")
      : page.locator('section[aria-labelledby="monthly-value-heading"]');
    const helperText = await helperScope.innerText();
    const offPreservesCommitments =
      /Off[^.]*\b(?:does not (?:change|alter)|cannot suppress)\b[^.]*Community Preceptor recurrence commitments/.test(helperText)
      || /Community Preceptor recurrence commitments[^.]*\bcannot be suppressed by\b[^.]*Off/.test(helperText);
    if (!offPreservesCommitments) {
      throw new Error(`Missing Off non-suppression meaning for ${url}: ${helperText}`);
    }
    if (url === input.templateUrl) {
      const defaultValue = await page.locator('input[name="monday_am"]:checked').inputValue();
      if (defaultValue !== "base:off") throw new Error(`Unexpected weekly-template default: ${defaultValue}`);
    }
  }
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""


class FloatScheduleInputTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="float-scheduler", password="test-password"
        )
        administrators, _ = Group.objects.get_or_create(name="Scheduling Administrators")
        administrators.permissions.add(
            Permission.objects.get(codename="manage_scheduling_administration")
        )
        self.scheduler.groups.add(administrators)
        self.client.force_login(self.scheduler)
        self.admin = ensure_system_schedule_options()

    def _physician(self, name, *roles):
        physician = Physician.objects.create(display_name=name)
        for role in roles:
            add_role(
                physician=physician,
                role=role,
                effective_start=date(2025, 1, 1),
                effective_end=None,
                scheduler=self.scheduler,
            )
        return physician

    @staticmethod
    def _slots(selection):
        return {slot: selection for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES}

    def test_faculty_without_float_keeps_assigned_option_choices_and_admin_impact(self):
        faculty = self._physician("Faculty", EffectiveRole.Role.FACULTY)
        custom_option = create_custom_availability_category(
            name="Sports Med Training Room", impact="backup_only", scheduler=self.scheduler
        )

        form = FacultyAvailabilityTemplateForm(physician=faculty)
        self.assertNotIn(
            f"category:{self.admin.pk}", _custom_choice_values(form.fields["monday_am"])
        )
        with self.assertRaisesRegex(ValidationError, "assigned to this physician"):
            create_faculty_availability_template(
                physician=faculty,
                effective_start=None,
                effective_end=None,
                slots=self._slots({"category_id": self.admin.pk}),
                scheduler=self.scheduler,
            )

        set_custom_availability_category_physicians(
            category=self.admin, physicians=[faculty], scheduler=self.scheduler
        )
        set_custom_availability_category_physicians(
            category=custom_option, physicians=[faculty], scheduler=self.scheduler
        )
        form = FacultyAvailabilityTemplateForm(physician=faculty)
        self.assertIn(f"category:{self.admin.pk}", _custom_choice_values(form.fields["monday_am"]))
        self.assertEqual(
            _choice_label(form.fields["monday_am"], f"category:{custom_option.pk}"),
            custom_option.name,
        )
        rule_form = MonthlyPhysicianAvailabilityRuleForm(physician=faculty)
        rule_label = _choice_label(rule_form.fields["availability"], f"category:{custom_option.pk}")
        self.assertIn(custom_option.name, rule_label)
        self.assertIn("Backup only", rule_label)
        template = create_faculty_availability_template(
            physician=faculty,
            effective_start=None,
            effective_end=None,
            slots=self._slots({"category_id": self.admin.pk}),
            scheduler=self.scheduler,
        )

        availability = effective_preceptor_availability_on(faculty, date(2025, 1, 6), "AM")
        self.assertEqual(template.slots["monday_am"]["category_name"], "Admin")
        self.assertEqual(availability.availability, "not_available")

    def test_float_faculty_receives_admin_without_an_assignment_row(self):
        faculty = self._physician(
            "Float Faculty", EffectiveRole.Role.FACULTY, EffectiveRole.Role.FLOAT_PRECEPTOR
        )

        form = FacultyAvailabilityTemplateForm(physician=faculty)
        self.assertIn(f"category:{self.admin.pk}", _custom_choice_values(form.fields["monday_am"]))
        rule_form = MonthlyPhysicianAvailabilityRuleForm(physician=faculty)
        self.assertIn(
            f"category:{self.admin.pk}", _custom_choice_values(rule_form.fields["availability"])
        )
        template = create_faculty_availability_template(
            physician=faculty,
            effective_start=None,
            effective_end=None,
            slots=self._slots({"category_id": self.admin.pk}),
            scheduler=self.scheduler,
        )
        rule = create_monthly_physician_availability_rule(
            physician=faculty,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            availability_selection={"category_id": self.admin.pk},
            scheduler=self.scheduler,
        )

        self.assertEqual(template.slots["monday_am"]["category_name"], "Admin")
        self.assertEqual(rule.availability_selection["category_name"], "Admin")
        self.assertFalse(
            CustomAvailabilityCategoryAssignment.objects.filter(
                physician=faculty, category=self.admin
            ).exists()
        )

    def test_community_preceptor_float_keeps_off_base_and_admin_float_capability(self):
        community_float = self._physician(
            "Float Community",
            EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            EffectiveRole.Role.FLOAT_PRECEPTOR,
        )
        custom = create_custom_availability_category(
            name="Community Float Custom", impact="available", scheduler=self.scheduler
        )
        template_form = FacultyAvailabilityTemplateForm(physician=community_float)
        self.assertEqual(_base_choices(template_form.fields["monday_am"]), [("base:off", "Off")])
        self.assertEqual(
            _custom_choice_values(template_form.fields["monday_am"]), [f"category:{self.admin.pk}"]
        )
        template_admin_label = _choice_label(
            template_form.fields["monday_am"], f"category:{self.admin.pk}"
        )
        self.assertIn("Admin", template_admin_label)
        self.assertIn("Float", template_admin_label)
        self.assertNotIn("Not available", template_admin_label)
        self.assertNotIn(
            f"category:{custom.pk}", _custom_choice_values(template_form.fields["monday_am"])
        )
        self.assertTrue(
            all(
                field.initial == "base:off"
                for field_name, field in template_form.fields.items()
                if field_name.endswith(("_am", "_pm"))
            )
        )
        rule_form = MonthlyPhysicianAvailabilityRuleForm(physician=community_float)
        self.assertEqual(_base_choices(rule_form.fields["availability"]), [("base:off", "Off")])
        self.assertEqual(
            _custom_choice_values(rule_form.fields["availability"]), [f"category:{self.admin.pk}"]
        )
        rule_admin_label = _choice_label(
            rule_form.fields["availability"], f"category:{self.admin.pk}"
        )
        self.assertIn("Admin", rule_admin_label)
        self.assertIn("Float", rule_admin_label)
        self.assertNotIn("Not available", rule_admin_label)
        self.assertNotIn(
            f"category:{custom.pk}", _custom_choice_values(rule_form.fields["availability"])
        )
        for selection in (
            FacultyAvailabilityTemplate.base_selection("open"),
            FacultyAvailabilityTemplate.base_selection("backup_only"),
            FacultyAvailabilityTemplate.base_selection("patient_care"),
            {"category_id": custom.pk, "category_name": custom.name},
        ):
            with self.subTest(selection=selection):
                invalid = FacultyAvailabilityTemplate(
                    physician=community_float,
                    slots=self._slots(selection),
                )
                with self.assertRaisesRegex(ValidationError, "only Off or Admin"):
                    invalid.full_clean()
                with self.assertRaises(ValidationError):
                    create_monthly_physician_availability_rule(
                        physician=community_float,
                        weekday=0,
                        ordinals=[1],
                        periods=["AM"],
                        availability_selection=selection,
                        scheduler=self.scheduler,
                    )

        rule = create_monthly_physician_availability_rule(
            physician=community_float,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            availability_selection={"category_id": self.admin.pk},
            scheduler=self.scheduler,
        )
        self.assertEqual(rule.availability_selection["category_name"], "Admin")
        template_ids = set(
            FacultyAvailabilityTemplate.objects.filter(physician=community_float).values_list(
                "pk", flat=True
            )
        )
        rule_ids = set(
            MonthlyPhysicianAvailabilityRule.objects.filter(physician=community_float).values_list(
                "pk", flat=True
            )
        )
        history_count = PhysicianChange.objects.count()

        response = self.client.post(
            reverse("physician-faculty-availability-add", args=(community_float.pk,)),
            {
                "monday_am": f"category:{custom.pk}",
                **{
                    slot: "base:off"
                    for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
                    if slot != "monday_am"
                },
            },
        )
        self.assertContains(response, "Select a valid choice")
        self.assertTrue(response.context["form"]["monday_am"].errors)
        response = self.client.post(
            reverse("physician-monthly-availability-rule-create", args=(community_float.pk,)),
            {
                "weekday": "1",
                "ordinals": ["1"],
                "periods": ["AM"],
                "availability": f"category:{custom.pk}",
            },
        )
        self.assertContains(response, "Select a valid choice")
        self.assertTrue(response.context["form"]["availability"].errors)
        self.assertEqual(
            set(
                FacultyAvailabilityTemplate.objects.filter(physician=community_float).values_list(
                    "pk", flat=True
                )
            ),
            template_ids,
        )
        self.assertEqual(
            set(
                MonthlyPhysicianAvailabilityRule.objects.filter(
                    physician=community_float
                ).values_list("pk", flat=True)
            ),
            rule_ids,
        )
        self.assertEqual(PhysicianChange.objects.count(), history_count)

    def test_blank_schedule_inputs_use_organization_date_across_chicago_utc_boundary(self):
        boundary_now = datetime(2026, 2, 3, 5, 59, tzinfo=UTC)
        chicago_today = date(2026, 2, 2)
        community_float = Physician.objects.create(display_name="Boundary Float Community")
        for role in (
            EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            EffectiveRole.Role.FLOAT_PRECEPTOR,
        ):
            add_role(
                physician=community_float,
                role=role,
                effective_start=chicago_today,
                effective_end=chicago_today,
                scheduler=self.scheduler,
            )

        with patch(
            "schedules.services.operational_attention.timezone.now", return_value=boundary_now
        ):
            template_form = FacultyAvailabilityTemplateForm(physician=community_float)
            rule_form = MonthlyPhysicianAvailabilityRuleForm(physician=community_float)
            self.assertEqual(
                _base_choices(template_form.fields["monday_am"]), [("base:off", "Off")]
            )
            self.assertIn(
                f"category:{self.admin.pk}",
                _custom_choice_values(template_form.fields["monday_am"]),
            )
            self.assertEqual(_base_choices(rule_form.fields["availability"]), [("base:off", "Off")])
            self.assertIn(
                f"category:{self.admin.pk}",
                _custom_choice_values(rule_form.fields["availability"]),
            )
            with self.assertRaisesRegex(ValidationError, "only Off or Admin"):
                FacultyAvailabilityTemplate(
                    physician=community_float,
                    slots=self._slots(FacultyAvailabilityTemplate.base_selection("open")),
                ).full_clean()
            with self.assertRaisesRegex(ValidationError, "only Off or Admin"):
                MonthlyPhysicianAvailabilityRule(
                    physician=community_float,
                    weekday=0,
                    ordinals=[1],
                    periods=["AM"],
                    availability_selection=FacultyAvailabilityTemplate.base_selection("open"),
                ).full_clean()
            with self.assertRaisesRegex(ValidationError, "only Off or Admin"):
                create_faculty_availability_template(
                    physician=community_float,
                    effective_start=None,
                    effective_end=None,
                    slots=self._slots(FacultyAvailabilityTemplate.base_selection("open")),
                    scheduler=self.scheduler,
                )
            with self.assertRaisesRegex(ValidationError, "only Off or Admin"):
                create_monthly_physician_availability_rule(
                    physician=community_float,
                    weekday=0,
                    ordinals=[1],
                    periods=["AM"],
                    availability_selection=FacultyAvailabilityTemplate.base_selection("open"),
                    scheduler=self.scheduler,
                )

    def test_community_preceptor_float_inputs_are_additive_and_detail_explains_them(self):
        community = self._physician(
            "Additive Community",
            EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            EffectiveRole.Role.FLOAT_PRECEPTOR,
        )
        create_community_preceptor_rule(
            physician=community,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            scheduler=self.scheduler,
        )
        create_faculty_availability_template(
            physician=community,
            effective_start=None,
            effective_end=None,
            slots=self._slots(FacultyAvailabilityTemplate.base_selection("off")),
            scheduler=self.scheduler,
        )

        availability = effective_preceptor_availability_on(community, date(2025, 1, 6), "AM")
        detail = self.client.get(reverse("physician-detail", args=(community.pk,)))

        self.assertEqual(availability.availability, "available")
        self.assertTrue(availability.is_expected_community_commitment)
        self.assertContains(detail, "Float availability")
        self.assertContains(detail, "Off does not change Community Preceptor recurrence")

    def test_community_preceptor_without_float_does_not_receive_schedule_input_controls(self):
        community = self._physician("Community", EffectiveRole.Role.COMMUNITY_PRECEPTOR)

        detail = self.client.get(reverse("physician-detail", args=(community.pk,)))

        self.assertNotContains(detail, "Add weekly template")
        self.assertNotContains(detail, "Add monthly scheduling rule")

    def test_former_faculty_uses_current_community_float_choices_and_displays_history(self):
        today = organization_today()
        physician = Physician.objects.create(display_name="Former Faculty Float Community")
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=today - timedelta(days=4),
            effective_end=today - timedelta(days=3),
            scheduler=self.scheduler,
        )
        add_role(
            physician=physician,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=today - timedelta(days=2),
            effective_end=None,
            scheduler=self.scheduler,
        )
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FLOAT_PRECEPTOR,
            effective_start=today - timedelta(days=2),
            effective_end=None,
            scheduler=self.scheduler,
        )
        historical_template = create_faculty_availability_template(
            physician=physician,
            effective_start=today - timedelta(days=4),
            effective_end=today - timedelta(days=3),
            slots=self._slots(FacultyAvailabilityTemplate.base_selection("open")),
            scheduler=self.scheduler,
        )
        historical_rule = create_monthly_physician_availability_rule(
            physician=physician,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("open"),
            effective_start=today - timedelta(days=4),
            effective_end=today - timedelta(days=3),
            scheduler=self.scheduler,
        )

        historical_form = FacultyAvailabilityTemplateForm(
            physician=physician, template=historical_template
        )
        historical_rule_form = MonthlyPhysicianAvailabilityRuleForm(
            physician=physician, rule=historical_rule
        )
        template_form = FacultyAvailabilityTemplateForm(
            physician=physician, initial={"effective_start": today}
        )
        rule_form = MonthlyPhysicianAvailabilityRuleForm(
            physician=physician, initial={"effective_start": today}
        )

        self.assertIn(("base:open", "Open"), historical_form.fields["monday_am"].choices)
        self.assertIn(
            ("base:open", "Open"),
            historical_rule_form.fields["availability"].choices[0][1],
        )
        self.assertEqual(_base_choices(template_form.fields["monday_am"]), [("base:off", "Off")])
        self.assertIn(
            f"category:{self.admin.pk}",
            _custom_choice_values(template_form.fields["monday_am"]),
        )
        self.assertEqual(_base_choices(rule_form.fields["availability"]), [("base:off", "Off")])
        self.assertIn(
            f"category:{self.admin.pk}",
            _custom_choice_values(rule_form.fields["availability"]),
        )
        with self.assertRaisesRegex(ValidationError, "only Off or Admin"):
            create_faculty_availability_template(
                physician=physician,
                effective_start=today,
                effective_end=None,
                slots=self._slots(FacultyAvailabilityTemplate.base_selection("open")),
                scheduler=self.scheduler,
            )
        with self.assertRaisesRegex(ValidationError, "only Off or Admin"):
            create_monthly_physician_availability_rule(
                physician=physician,
                weekday=0,
                ordinals=[1],
                periods=["AM"],
                availability_selection=FacultyAvailabilityTemplate.base_selection("open"),
                effective_start=today,
                scheduler=self.scheduler,
            )

    def test_former_faculty_detail_displays_retained_schedule_history_without_controls(self):
        today = organization_today()
        physician = Physician.objects.create(display_name="Former Faculty")
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=today - timedelta(days=4),
            effective_end=today - timedelta(days=3),
            scheduler=self.scheduler,
        )
        create_faculty_availability_template(
            physician=physician,
            effective_start=today - timedelta(days=4),
            effective_end=today - timedelta(days=3),
            slots=self._slots(FacultyAvailabilityTemplate.base_selection("open")),
            scheduler=self.scheduler,
        )
        create_monthly_physician_availability_rule(
            physician=physician,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("open"),
            effective_start=today - timedelta(days=4),
            effective_end=today - timedelta(days=3),
            scheduler=self.scheduler,
        )

        detail = self.client.get(reverse("physician-detail", args=(physician.pk,)))

        self.assertContains(detail, "Faculty Schedule templates")
        self.assertContains(detail, "Monthly scheduling rules")
        self.assertContains(detail, "Edit template")
        self.assertContains(detail, "Edit")
        self.assertNotContains(detail, "Add weekly template")
        self.assertNotContains(detail, "Add monthly scheduling rule")

    def test_faculty_schedule_inputs_allow_future_and_mid_range_faculty_overlap(self):
        today = organization_today()
        future_faculty = Physician.objects.create(display_name="Future Faculty")
        add_role(
            physician=future_faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start=today + timedelta(days=10),
            effective_end=None,
            scheduler=self.scheduler,
        )
        future_form = FacultyAvailabilityTemplateForm(
            physician=future_faculty, initial={"effective_start": today + timedelta(days=10)}
        )
        self.assertIn(("base:open", "Open"), future_form.fields["monday_am"].choices)
        create_faculty_availability_template(
            physician=future_faculty,
            effective_start=today + timedelta(days=10),
            effective_end=None,
            slots=self._slots(FacultyAvailabilityTemplate.base_selection("open")),
            scheduler=self.scheduler,
        )
        create_monthly_physician_availability_rule(
            physician=future_faculty,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("open"),
            effective_start=today + timedelta(days=10),
            scheduler=self.scheduler,
        )

        mid_range_faculty = Physician.objects.create(display_name="Mid-range Faculty")
        add_role(
            physician=mid_range_faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start=today + timedelta(days=10),
            effective_end=today + timedelta(days=20),
            scheduler=self.scheduler,
        )
        create_faculty_availability_template(
            physician=mid_range_faculty,
            effective_start=today,
            effective_end=today + timedelta(days=15),
            slots=self._slots(FacultyAvailabilityTemplate.base_selection("open")),
            scheduler=self.scheduler,
        )
        create_monthly_physician_availability_rule(
            physician=mid_range_faculty,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            availability_selection=FacultyAvailabilityTemplate.base_selection("open"),
            effective_start=today,
            effective_end=today + timedelta(days=15),
            scheduler=self.scheduler,
        )

    def test_community_float_overlap_rejects_non_float_selection_across_a_range(self):
        today = organization_today()
        physician = Physician.objects.create(display_name="Mid-range Float Community")
        for role in (
            EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            EffectiveRole.Role.FLOAT_PRECEPTOR,
        ):
            add_role(
                physician=physician,
                role=role,
                effective_start=today + timedelta(days=10),
                effective_end=today + timedelta(days=20),
                scheduler=self.scheduler,
            )

        with self.assertRaisesRegex(ValidationError, "only Off or Admin"):
            create_faculty_availability_template(
                physician=physician,
                effective_start=today,
                effective_end=today + timedelta(days=15),
                slots=self._slots(FacultyAvailabilityTemplate.base_selection("open")),
                scheduler=self.scheduler,
            )
        with self.assertRaisesRegex(ValidationError, "only Off or Admin"):
            create_monthly_physician_availability_rule(
                physician=physician,
                weekday=0,
                ordinals=[1],
                periods=["AM"],
                availability_selection=FacultyAvailabilityTemplate.base_selection("open"),
                effective_start=today,
                effective_end=today + timedelta(days=15),
                scheduler=self.scheduler,
            )

    def test_community_float_overlap_authorizes_admin_across_template_and_rule_ranges(self):
        today = organization_today()
        physician = Physician.objects.create(display_name="Overlapping Float Community")
        for role in (
            EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            EffectiveRole.Role.FLOAT_PRECEPTOR,
        ):
            add_role(
                physician=physician,
                role=role,
                effective_start=today + timedelta(days=10),
                effective_end=today + timedelta(days=20),
                scheduler=self.scheduler,
            )

        template = create_faculty_availability_template(
            physician=physician,
            effective_start=today,
            effective_end=today + timedelta(days=15),
            slots=self._slots({"category_id": self.admin.pk}),
            scheduler=self.scheduler,
        )
        rule = create_monthly_physician_availability_rule(
            physician=physician,
            weekday=0,
            ordinals=[1],
            periods=["AM"],
            availability_selection={"category_id": self.admin.pk},
            effective_start=today,
            effective_end=today + timedelta(days=15),
            scheduler=self.scheduler,
        )

        self.assertEqual(template.slots["monday_am"]["category_name"], "Admin")
        self.assertEqual(rule.availability_selection["category_name"], "Admin")
        self.assertFalse(
            CustomAvailabilityCategoryAssignment.objects.filter(
                physician=physician, category=self.admin
            ).exists()
        )

    def test_admin_outside_float_range_requires_an_assignment(self):
        today = organization_today()
        physician = Physician.objects.create(display_name="Future Float Faculty")
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=today,
            effective_end=None,
            scheduler=self.scheduler,
        )
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FLOAT_PRECEPTOR,
            effective_start=today + timedelta(days=10),
            effective_end=today + timedelta(days=20),
            scheduler=self.scheduler,
        )

        with self.assertRaisesRegex(ValidationError, "assigned to this physician"):
            create_faculty_availability_template(
                physician=physician,
                effective_start=today,
                effective_end=today + timedelta(days=5),
                slots=self._slots({"category_id": self.admin.pk}),
                scheduler=self.scheduler,
            )
        with self.assertRaisesRegex(ValidationError, "assigned to this physician"):
            create_monthly_physician_availability_rule(
                physician=physician,
                weekday=0,
                ordinals=[1],
                periods=["AM"],
                availability_selection={"category_id": self.admin.pk},
                effective_start=today,
                effective_end=today + timedelta(days=5),
                scheduler=self.scheduler,
            )

    def test_ended_float_does_not_keep_automatic_admin(self):
        today = organization_today()
        physician = Physician.objects.create(display_name="Former Float Faculty")
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=today - timedelta(days=4),
            effective_end=None,
            scheduler=self.scheduler,
        )
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FLOAT_PRECEPTOR,
            effective_start=today - timedelta(days=4),
            effective_end=today - timedelta(days=1),
            scheduler=self.scheduler,
        )

        template_form = FacultyAvailabilityTemplateForm(
            physician=physician, initial={"effective_start": today}
        )
        rule_form = MonthlyPhysicianAvailabilityRuleForm(
            physician=physician, initial={"effective_start": today}
        )

        self.assertNotIn(
            f"category:{self.admin.pk}", _custom_choice_values(template_form.fields["monday_am"])
        )
        self.assertNotIn(
            f"category:{self.admin.pk}", _custom_choice_values(rule_form.fields["availability"])
        )
        with self.assertRaisesRegex(ValidationError, "assigned to this physician"):
            create_faculty_availability_template(
                physician=physician,
                effective_start=today,
                effective_end=None,
                slots=self._slots({"category_id": self.admin.pk}),
                scheduler=self.scheduler,
            )
        with self.assertRaisesRegex(ValidationError, "assigned to this physician"):
            create_monthly_physician_availability_rule(
                physician=physician,
                weekday=0,
                ordinals=[1],
                periods=["AM"],
                availability_selection={"category_id": self.admin.pk},
                effective_start=today,
                scheduler=self.scheduler,
            )


class FloatScheduleInputBrowserTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        cls.node, cls.browser_executable = require_browser_tools()
        try:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
        except PermissionError:
            raise RuntimeError(
                "Playwright browser coverage requires local test-server access."
            ) from None
        super().setUpClass()

    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="float-browser-scheduler", password="browser-password"
        )
        administrators, _ = Group.objects.get_or_create(name="Scheduling Administrators")
        administrators.permissions.add(
            Permission.objects.get(codename="manage_scheduling_administration")
        )
        self.scheduler.groups.add(administrators)
        self.physician = Physician.objects.create(display_name="Float browser community")
        for role in (
            EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            EffectiveRole.Role.FLOAT_PRECEPTOR,
        ):
            add_role(
                physician=self.physician,
                role=role,
                effective_start=date(2025, 1, 1),
                effective_end=None,
                scheduler=self.scheduler,
            )
        self.admin = ensure_system_schedule_options()

    def test_community_float_forms_expose_only_additive_options_and_meaning(self):
        self.client.force_login(self.scheduler)
        template_url = reverse("physician-faculty-availability-add", args=(self.physician.pk,))
        rule_url = reverse("physician-monthly-availability-rule-create", args=(self.physician.pk,))
        completed = subprocess.run(
            [self.node, "-e", _BROWSER_RUNNER],
            input=json.dumps(
                {
                    "baseUrl": self.live_server_url,
                    "browserExecutable": self.browser_executable,
                    "templateUrl": template_url,
                    "ruleUrl": rule_url,
                    "templateAllowedValues": ["base:off", f"category:{self.admin.pk}"],
                    "ruleAllowedValues": ["base:off", f"category:{self.admin.pk}"],
                    "sessionCookie": {
                        "name": settings.SESSION_COOKIE_NAME,
                        "value": self.client.cookies[settings.SESSION_COOKIE_NAME].value,
                    },
                }
            ),
            capture_output=True,
            text=True,
        )

        if completed.returncode:
            self.fail(completed.stderr)
