from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from schedules.models import ClinicHoliday, HolidayOccurrence
from schedules.tests.test_table_presentation import _RenderedDataTable
from schedules.tests.test_work_remaining import _action_matches, _RenderedSurfaceParser


def holiday_row(response, holiday_name):
    table = _RenderedDataTable(response.content.decode(), "holiday-occurrences")
    rows = [
        row
        for row in table.body_rows()
        if table.cell_for_header(row, "Holiday").text().strip() == holiday_name
    ]
    if len(rows) != 1:
        raise AssertionError(f"Expected one {holiday_name!r} holiday row, found {len(rows)}.")
    return table, rows[0]


def assert_no_holiday_action(test_case, table, row):
    actions = table.cell_for_header(row, "Actions")
    test_case.assertFalse(actions.descendants_named("a", "form"))


def assert_holiday_work_summary(test_case, response, *, evaluated):
    """Check the rendered completion or unevaluated state without fixing its prose."""
    parser = _RenderedSurfaceParser()
    parser.feed(response.content.decode())
    summaries = [
        element
        for element in parser.elements
        if element.tag == "section"
        and "workspace-work-summary" in element.attributes.get("class", "").split()
    ]
    test_case.assertEqual(len(summaries), 1)
    summary = summaries[0]
    tone = "clear" if evaluated else "neutral"
    test_case.assertIn(f"surface-tone--{tone}", summary.attributes.get("class", "").split())
    test_case.assertEqual(response.context["work_remaining"].evaluated, evaluated)
    test_case.assertEqual(
        (
            response.context["work_remaining"].required_item_count,
            response.context["work_remaining"].advisory_item_count,
        ),
        (0, 0),
    )
    summary_text = summary.text().casefold()
    if evaluated:
        test_case.assertRegex(
            summary_text,
            r"\b(?:all\s+(?:required\s+)?work\s+is\s+complete|"
            r"no\s+work\s+remains|zero\s+(?:required\s+)?work\s+remains)\b",
        )
    else:
        test_case.assertRegex(summary_text, r"\b(?:not\s+evaluat\w*|unevaluat\w*)")


class HolidayWorkspaceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.client.force_login(self.user)

    def occurrence(self, calendar_year, holiday):
        self.client.get(reverse("holiday-workspace"), {"calendar_year": calendar_year})
        return HolidayOccurrence.objects.get(calendar_year=calendar_year, holiday=holiday)

    def test_vacation_navigation_and_compact_year_list_show_holiday_states(self):
        response = self.client.get(reverse("holiday-workspace"), {"calendar_year": "2026"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-label="Vacation navigation"')
        self.assertContains(
            response,
            'href="/vacations/holidays/?calendar_year=2026" aria-current="page">Holidays</a>',
        )
        self.assertContains(response, 'aria-label="Calendar year navigation"')
        self.assertContains(response, 'aria-label="Previous calendar year"')
        self.assertContains(response, 'aria-label="Next calendar year"')
        self.assertContains(
            response,
            'class="period-navigation-form period-navigation-picker compact-inline-control-row"',
        )
        self.assertContains(response, '<select name="calendar_year"', html=False)
        self.assertContains(response, "data-calendar-period-picker")
        assert_holiday_work_summary(self, response, evaluated=True)
        table, independence_day = holiday_row(response, "Independence Day")
        self.assertNotIn("data-table-row--danger", independence_day.attributes.get("class", ""))
        self.assertEqual(
            table.cell_for_header(independence_day, "Observed date").text(), "Unscheduled"
        )
        self.assertEqual(
            table.cell_for_header(independence_day, "Affected coverage").text(), "AM and PM"
        )
        schedule_url = reverse(
            "holiday-occurrence-schedule",
            args=(self.occurrence(2026, ClinicHoliday.INDEPENDENCE_DAY).pk,),
        )
        schedule_links = table.cell_for_header(independence_day, "Actions").descendants_named("a")
        self.assertEqual(len(schedule_links), 1)
        self.assertEqual(schedule_links[0].text(), "Schedule")
        self.assertTrue(
            _action_matches(
                schedule_links[0].attributes.get("href", ""), f"{schedule_url}?calendar_year=2026"
            )
        )

        table, computed_memorial_day = holiday_row(response, "Memorial Day")
        self.assertEqual(table.cell_for_header(computed_memorial_day, "Source").text(), "Computed")
        assert_no_holiday_action(self, table, computed_memorial_day)

        table, observed_christmas_eve = holiday_row(response, "Christmas Eve")
        observed_date = table.cell_for_header(observed_christmas_eve, "Observed date")
        self.assertEqual(
            [time.attributes.get("datetime") for time in observed_date.descendants_named("time")],
            ["2026-12-24"],
        )
        self.assertEqual(
            table.cell_for_header(observed_christmas_eve, "Affected coverage").text(), "PM only"
        )
        assert_no_holiday_action(self, table, observed_christmas_eve)

        not_observed = self.client.get(reverse("holiday-workspace"), {"calendar_year": "2022"})
        table, christmas_eve = holiday_row(not_observed, "Christmas Eve")
        self.assertEqual(
            table.cell_for_header(christmas_eve, "Observed date").text(), "Not observed"
        )
        self.assertEqual(
            table.cell_for_header(christmas_eve, "Affected coverage").text(), "PM only"
        )
        assert_no_holiday_action(self, table, christmas_eve)
        assert_holiday_work_summary(self, not_observed, evaluated=False)

    def test_workspace_and_form_use_actions_section_headings(self):
        workspace = self.client.get(reverse("holiday-workspace"), {"calendar_year": "2026"})
        occurrence = self.occurrence(2026, ClinicHoliday.INDEPENDENCE_DAY)
        form = self.client.get(reverse("holiday-occurrence-schedule", args=(occurrence.pk,)))

        self.assertContains(
            workspace,
            '<h2>Actions</h2><a href="/vacations/holidays/history/?calendar_year=2026">'
            "Holiday history</a>",
        )
        self.assertContains(form, '<h2 id="holiday-notes-heading">Actions</h2>')

    def test_calendar_year_picker_and_arrows_immediately_select_that_year(self):
        response = self.client.get(reverse("holiday-workspace"), {"calendar_year": "2025"})

        self.assertEqual(response.context["holiday_calendar_year"], 2025)
        self.assertContains(response, 'href="/vacations/holidays/?calendar_year=2024"')
        self.assertContains(response, 'href="/vacations/holidays/?calendar_year=2026"')
        self.assertEqual(
            response.context["holiday_calendar_year_form"]["calendar_year"].value(), 2025
        )

    def test_unresolved_weekend_independence_form_keeps_date_blank_and_validates(self):
        occurrence = self.occurrence(2026, ClinicHoliday.INDEPENDENCE_DAY)
        url = reverse("holiday-occurrence-schedule", args=(occurrence.pk,)) + "?calendar_year=2026"

        response = self.client.get(url)
        self.assertEqual(response.context["form"]["observed_date"].value(), None)
        self.assertContains(response, "Nominal date")
        self.assertContains(response, "Affected coverage")
        self.assertContains(response, "History note (optional)")
        self.assertContains(response, "Recorded with this change in history.")

        response = self.client.post(url, {"observed_date": "", "note": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required.")

    def test_scheduling_switches_list_action_and_records_contextual_history(self):
        occurrence = self.occurrence(2026, ClinicHoliday.INDEPENDENCE_DAY)
        schedule_url = (
            reverse("holiday-occurrence-schedule", args=(occurrence.pk,)) + "?calendar_year=2026"
        )

        response = self.client.post(
            schedule_url,
            {"observed_date": "2026-07-03", "note": "Friday observance"},
        )
        self.assertRedirects(
            response,
            reverse("holiday-workspace") + "?calendar_year=2026",
            fetch_redirect_response=False,
        )
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.observed_date, date(2026, 7, 3))

        workspace = self.client.get(reverse("holiday-workspace"), {"calendar_year": "2026"})
        edit_url = reverse("holiday-occurrence-correct", args=(occurrence.pk,))
        table, independence_day = holiday_row(workspace, "Independence Day")
        self.assertEqual(table.cell_for_header(independence_day, "Source").text(), "Scheduled")
        self.assertNotIn("data-table-row--danger", independence_day.attributes.get("class", ""))
        edit_links = table.cell_for_header(independence_day, "Actions").descendants_named("a")
        self.assertEqual(len(edit_links), 1)
        self.assertEqual(edit_links[0].text().strip(), "Edit")
        self.assertTrue(
            _action_matches(
                edit_links[0].attributes.get("href", ""), f"{edit_url}?calendar_year=2026"
            )
        )
        assert_holiday_work_summary(self, workspace, evaluated=True)

        history = self.client.get(reverse("holiday-history"), {"calendar_year": "2026"})
        self.assertContains(history, "Holiday history")
        self.assertContains(history, "Friday observance")
        self.assertContains(history, "Scheduled")

    def test_dated_computed_holiday_can_be_corrected(self):
        occurrence = self.occurrence(2026, ClinicHoliday.NEW_YEARS_DAY)
        edit_url = (
            reverse("holiday-occurrence-correct", args=(occurrence.pk,)) + "?calendar_year=2026"
        )

        response = self.client.get(edit_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["observed_date"].value(), date(2026, 1, 1))

        response = self.client.post(
            edit_url,
            {"observed_date": "2026-01-02", "note": "Historical correction"},
        )

        self.assertRedirects(
            response,
            reverse("holiday-workspace") + "?calendar_year=2026",
            fetch_redirect_response=False,
        )
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.state, HolidayOccurrence.State.SCHEDULED)
        self.assertEqual(occurrence.observed_date, date(2026, 1, 2))
        self.assertEqual(occurrence.changes.first().action, "corrected")
        self.assertEqual(occurrence.changes.first().note, "Historical correction")

    def test_holiday_routes_require_a_signed_in_scheduler(self):
        occurrence = self.occurrence(2026, ClinicHoliday.INDEPENDENCE_DAY)
        self.client.logout()

        for url in (
            reverse("holiday-workspace"),
            reverse("holiday-history"),
            reverse("holiday-occurrence-schedule", args=(occurrence.pk,)),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("sign-in"), response.url)
