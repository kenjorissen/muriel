from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from schedules.models import LeadTimeChange, LeadTimeSetting
from schedules.services.lead_times import lead_time_policy, set_lead_time
from schedules.tests.test_table_presentation import _RenderedDataTable
from schedules.tests.test_views import (
    assert_rendered_active_navigation,
    assert_rendered_navigation_link,
)
from schedules.tests.test_work_remaining import _action_matches, _RenderedSurfaceParser

# docs/design.md section 7.2: default months by work family.
DOCUMENTED_LEAD_TIME_DEFAULTS = {
    LeadTimeSetting.Family.HOLIDAYS: 18,
    LeadTimeSetting.Family.ACADEMIC_EVENTS: 6,
    LeadTimeSetting.Family.PRECEPTING: 5,
    LeadTimeSetting.Family.NORMAL_ROUNDING: 5,
    LeadTimeSetting.Family.INTERVIEWS: 0,
}


def lead_time_row(response, family):
    """Return a family's rendered policy row without depending on table layout."""
    table = _RenderedDataTable(response.content.decode(), "lead-times")
    rows = [
        row
        for row in table.body_rows()
        if table.cell_for_header(row, "Work family").text().strip()
        == LeadTimeSetting.Family(family).label
    ]
    if len(rows) != 1:
        raise AssertionError(
            f"Expected one rendered {LeadTimeSetting.Family(family).label!r} policy row, "
            f"found {len(rows)}."
        )
    return table, rows[0]


def lead_time_history_row(response, family):
    """Return a family's retained rendered history record."""
    table = _RenderedDataTable(response.content.decode(), "lead-time-history")
    rows = [
        row
        for row in table.body_rows()
        if table.cell_for_header(row, "Work family").text().strip()
        == LeadTimeSetting.Family(family).label
    ]
    if len(rows) != 1:
        raise AssertionError(
            f"Expected one rendered {LeadTimeSetting.Family(family).label!r} history row, "
            f"found {len(rows)}."
        )
    return table, rows[0]


class LeadTimeWorkspaceTests(TestCase):
    def setUp(self):
        self.administrator = get_user_model().objects.create_user(
            username="administrator", password="test-password"
        )
        self.administrator.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )

    def test_schedule_navigation_and_workspace_show_every_fixed_policy(self):
        self.client.force_login(self.administrator)

        response = self.client.get(reverse("lead-time-workspace"))

        assert_rendered_navigation_link(
            self,
            response,
            aria_label="Schedule navigation",
            label="Master Schedule",
            url=reverse("master-schedule"),
            current=False,
        )
        assert_rendered_navigation_link(
            self,
            response,
            aria_label="Schedule navigation",
            label="Work Remaining",
            url=reverse("work-remaining"),
            current=False,
        )
        assert_rendered_navigation_link(
            self,
            response,
            aria_label="Schedule navigation",
            label="Lead Times",
            url=reverse("lead-time-workspace"),
            current=True,
        )
        assert_rendered_active_navigation(
            self, response, aria_label="Schedule navigation", current="Lead Times"
        )
        parser = _RenderedSurfaceParser()
        parser.feed(response.content.decode())
        action_headers = [
            element
            for element in parser.elements
            if element.tag == "header"
            and "schedule-workspace-actions-header" in element.attributes.get("class", "").split()
        ]
        self.assertEqual(len(action_headers), 1)
        action_header = action_headers[0]
        self.assertEqual(
            [
                " ".join(element.text().split())
                for element in action_header.descendants()
                if element.tag == "h2"
            ],
            ["Actions"],
        )
        history_links = [
            element
            for element in action_header.descendants()
            if element.tag == "a" and " ".join(element.text().split()) == "Lead-time history"
        ]
        self.assertEqual(len(history_links), 1)
        self.assertTrue(
            _action_matches(
                history_links[0].attributes.get("href", ""), reverse("lead-time-history")
            )
        )

        anchors = {
            LeadTimeSetting.Family.HOLIDAYS: "holiday's nominal date",
            LeadTimeSetting.Family.ACADEMIC_EVENTS: "expected academic-year quarter",
            LeadTimeSetting.Family.PRECEPTING: ("affected", "week"),
            LeadTimeSetting.Family.NORMAL_ROUNDING: ("affected", "week"),
            LeadTimeSetting.Family.INTERVIEWS: ("July 1", "interview academic year"),
        }
        table = _RenderedDataTable(response.content.decode(), "lead-times")
        self.assertEqual(
            {table.cell_for_header(row, "Work family").text().strip() for row in table.body_rows()},
            {LeadTimeSetting.Family(family).label for family in DOCUMENTED_LEAD_TIME_DEFAULTS},
        )
        for family, months in DOCUMENTED_LEAD_TIME_DEFAULTS.items():
            with self.subTest(family=family):
                table, row = lead_time_row(response, family)
                self.assertEqual(
                    table.cell_for_header(row, "Lead time").text().strip(), f"{months} months"
                )
                anchor = table.cell_for_header(row, "Fixed anchor").text().strip()
                if isinstance(anchors[family], str):
                    self.assertIn(anchors[family], anchor)
                else:
                    self.assertTrue(all(fragment in anchor for fragment in anchors[family]))
                actions = table.cell_for_header(row, "Actions").descendants_named("a")
                edit_actions = [action for action in actions if action.text().strip() == "Edit"]
                self.assertEqual(len(edit_actions), 1)
                self.assertTrue(
                    _action_matches(
                        edit_actions[0].attributes.get("href", ""),
                        reverse("lead-time-edit", args=(family,)),
                    )
                )

        master_schedule = self.client.get(reverse("master-schedule"))

        assert_rendered_navigation_link(
            self,
            master_schedule,
            aria_label="Schedule navigation",
            label="Master Schedule",
            url=reverse("master-schedule"),
            current=True,
        )
        assert_rendered_navigation_link(
            self,
            master_schedule,
            aria_label="Schedule navigation",
            label="Lead Times",
            url=reverse("lead-time-workspace"),
            current=False,
        )

    def test_administrator_edits_one_value_with_compact_validation_and_retained_history(self):
        self.client.force_login(self.administrator)
        url = reverse("lead-time-edit", args=(LeadTimeSetting.Family.PRECEPTING,))

        form = self.client.get(url)
        invalid = self.client.post(url, {"months": "-1", "note": "No"})

        self.assertContains(form, "Edit Precepting lead time")
        form_parser = _RenderedSurfaceParser()
        form_parser.feed(form.content.decode())
        rendered_forms = [
            element
            for element in form_parser.elements
            if element.tag == "form"
            and element.attributes.get("method", "get").lower() == "post"
            and any(control.attributes.get("name") == "months" for control in element.descendants())
        ]
        self.assertEqual(len(rendered_forms), 1)
        edit_form = rendered_forms[0]
        controls = {
            control.attributes["name"]: control
            for control in edit_form.descendants()
            if control.tag in {"input", "textarea", "select"} and "name" in control.attributes
        }
        self.assertIn("months", controls)
        self.assertIn("note", controls)
        self.assertNotIn("family", controls)
        self.assertNotIn("anchor", controls)
        self.assertEqual(controls["months"].tag, "input")
        self.assertEqual(controls["months"].attributes.get("type"), "number")
        self.assertEqual(controls["note"].tag, "textarea")
        self.assertIn("Fixed anchor:", edit_form.text())
        self.assertIn("affected week", edit_form.text())
        self.assertIn("months", invalid.context["form"].errors)
        self.assertEqual(invalid.context["form"].errors.as_data()["months"][0].code, "min_value")
        invalid_parser = _RenderedSurfaceParser()
        invalid_parser.feed(invalid.content.decode())
        months_control = next(
            element
            for element in invalid_parser.elements
            if element.tag == "input" and element.attributes.get("name") == "months"
        )
        months_field = next(
            element
            for element in months_control.ancestors()
            if "form-field-group" in element.attributes.get("class", "").split()
        )
        rendered_errors = [
            element
            for element in months_field.descendants()
            if "errorlist" in element.attributes.get("class", "").split()
        ]
        self.assertEqual(len(rendered_errors), 1)
        # The form error supplies presentation text only; field association is the contract here.
        self.assertEqual(
            [rendered_errors[0].text().strip()],
            list(invalid.context["form"].errors["months"]),
        )
        self.assertEqual(lead_time_policy()[LeadTimeSetting.Family.PRECEPTING], 5)
        self.assertFalse(LeadTimeChange.objects.exists())

        saved = self.client.post(url, {"months": "9", "note": "Extra preparation"})

        self.assertRedirects(saved, reverse("lead-time-workspace"))
        self.assertEqual(lead_time_policy()[LeadTimeSetting.Family.PRECEPTING], 9)
        change = LeadTimeChange.objects.get()
        self.assertEqual(
            (change.previous_months, change.resulting_months, change.note),
            (5, 9, "Extra preparation"),
        )

        workspace = self.client.get(reverse("lead-time-workspace"))
        history = self.client.get(reverse("lead-time-history"))

        workspace_table, workspace_row = lead_time_row(workspace, LeadTimeSetting.Family.PRECEPTING)
        self.assertEqual(
            workspace_table.cell_for_header(workspace_row, "Lead time").text(), "9 months"
        )
        history_table, history_row = lead_time_history_row(
            history, LeadTimeSetting.Family.PRECEPTING
        )
        self.assertEqual(
            history_table.cell_for_header(history_row, "Scheduler").text(), "administrator"
        )
        self.assertEqual(
            history_table.cell_for_header(history_row, "Prior value").text(), "5 months"
        )
        self.assertEqual(
            history_table.cell_for_header(history_row, "Resulting value").text(), "9 months"
        )
        self.assertEqual(
            history_table.cell_for_header(history_row, "Note").text(), "Extra preparation"
        )

    def test_workspace_binds_each_editable_policy_family_to_its_own_value(self):
        set_lead_time(
            family=LeadTimeSetting.Family.PRECEPTING,
            months=8,
            scheduler=self.administrator,
        )
        set_lead_time(
            family=LeadTimeSetting.Family.NORMAL_ROUNDING,
            months=3,
            scheduler=self.administrator,
        )
        self.client.force_login(self.administrator)

        response = self.client.get(reverse("lead-time-workspace"))
        expected_months = DOCUMENTED_LEAD_TIME_DEFAULTS | {
            LeadTimeSetting.Family.PRECEPTING: 8,
            LeadTimeSetting.Family.NORMAL_ROUNDING: 3,
        }

        for family, months in expected_months.items():
            with self.subTest(family=family):
                table, row = lead_time_row(response, family)
                self.assertEqual(table.cell_for_header(row, "Lead time").text(), f"{months} months")
                actions = table.cell_for_header(row, "Actions").descendants_named("a")
                self.assertEqual(len(actions), 1)
                self.assertTrue(
                    _action_matches(
                        actions[0].attributes.get("href", ""),
                        reverse("lead-time-edit", args=(family,)),
                    )
                )

    def test_authenticated_scheduler_can_view_but_cannot_edit_or_mutate(self):
        self.client.force_login(self.scheduler)
        url = reverse("lead-time-edit", args=(LeadTimeSetting.Family.HOLIDAYS,))

        workspace = self.client.get(reverse("lead-time-workspace"))
        edit = self.client.get(url)
        rejected = self.client.post(url, {"months": "12", "note": "Unauthorized"})

        self.assertContains(workspace, "Holidays")
        self.assertContains(workspace, "18 months")
        self.assertContains(workspace, "Lead-time history")
        self.assertNotContains(workspace, "Edit")
        self.assertEqual(edit.status_code, 403)
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(lead_time_policy(), DOCUMENTED_LEAD_TIME_DEFAULTS)
        self.assertFalse(LeadTimeChange.objects.exists())

    def test_history_is_visible_to_authenticated_schedulers(self):
        set_lead_time(
            family=LeadTimeSetting.Family.INTERVIEWS,
            months=3,
            scheduler=self.administrator,
            note="Interview planning",
        )
        self.client.force_login(self.scheduler)

        response = self.client.get(reverse("lead-time-history"))

        table, row = lead_time_history_row(response, LeadTimeSetting.Family.INTERVIEWS)
        self.assertEqual(table.cell_for_header(row, "Prior value").text(), "0 months")
        self.assertEqual(table.cell_for_header(row, "Resulting value").text(), "3 months")
        self.assertEqual(table.cell_for_header(row, "Scheduler").text(), "administrator")
        self.assertEqual(table.cell_for_header(row, "Note").text(), "Interview planning")
