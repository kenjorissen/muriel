import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, resolve, reverse

from schedules.models import (
    AcademicEvent,
    AcademicEventDateRange,
    AcademicEventDefinition,
    AcademicEventDefinitionChange,
    CasualRounderRule,
    ClinicHoliday,
    CommunityPreceptorRule,
    CommunityPreceptorScheduleChange,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    LateDocAssignment,
    MonthlyPhysicianAvailabilityRule,
    NormalRounder,
    Physician,
    PhysicianChange,
    PreceptorAssignment,
    PreceptorAssignmentChange,
    PreceptorSession,
    RoundingScheduleChange,
    SchedulePeriod,
    Vacation,
    VacationChange,
)
from schedules.precepting_presentation import precepting_finding_targets
from schedules.services.availability_categories import ensure_system_schedule_options
from schedules.services.community import create_community_preceptor_rule
from schedules.services.holidays import holiday_occurrences_for, schedule_holiday_occurrence
from schedules.services.interviews import assign_interview_shift, create_interview_shift
from schedules.services.physicians import (
    add_role,
    correct_role,
    create_faculty_availability_template,
    create_physician,
    faculty_precepting_capacity_for_week,
    physicians_status_on,
)
from schedules.services.preceptors import (
    DraftFinding,
    DraftFindingResult,
    assign_preceptor,
    draft_findings_for_session,
    open_monthly_draft,
    open_weekly_draft,
)
from schedules.services.rounding import record_normal_rounder
from schedules.services.vacations import create_vacation
from schedules.services.work_remaining import (
    InclusiveDateRange,
    ProjectedWorkItem,
    ProjectedWorkReason,
    WorkSummaryType,
)
from schedules.tests.test_table_presentation import _RenderedDataTable, _RenderedTableParser
from schedules.tests.test_work_remaining import _action_matches, _RenderedSurfaceParser


def rendered_assignment_alternative(parser, physician):
    """Return a physician's rendered option after binding it to its form control."""
    options = [
        element
        for element in parser.elements
        if element.tag == "option"
        and element.attributes.get("value") == str(physician.pk)
        and " ".join(element.text().split()) == physician.display_name
    ]
    if len(options) != 1:
        raise AssertionError(
            f"Expected one assignment choice for {physician.display_name!r}, found {len(options)}."
        )
    alternatives = [
        element
        for element in parser.elements
        if element.tag == "div"
        and "alternative" in element.attributes.get("class", "").split()
        and physician.display_name in element.text()
    ]
    if len(alternatives) != 1:
        raise AssertionError(
            f"Expected one rendered assignment option for {physician.display_name!r}, "
            f"found {len(alternatives)}."
        )
    return alternatives[0]


def alternative_tier(detail):
    return next(
        ancestor
        for ancestor in detail.ancestors()
        if ancestor.tag == "fieldset"
        and "alternative-tier" in ancestor.attributes.get("class", "").split()
    )


def assert_rendered_navigation(test_case, response, *, aria_label, expected, current):
    """Check the rendered navigation's own anchors, destinations, and active link."""
    parser = _RenderedSurfaceParser()
    parser.feed(response.content.decode())
    navigations = [
        element
        for element in parser.elements
        if element.tag == "nav" and element.attributes.get("aria-label") == aria_label
    ]
    test_case.assertEqual(len(navigations), 1)
    links = [element for element in navigations[0].descendants() if element.tag == "a"]
    matched_links = []
    previous_index = -1
    for label, url in expected:
        with test_case.subTest(label=label):
            matches = [
                (index, link)
                for index, link in enumerate(links)
                if " ".join(link.text().split()) == label
            ]
            test_case.assertEqual(len(matches), 1)
            index, link = matches[0]
            test_case.assertGreater(index, previous_index)
            test_case.assertTrue(_action_matches(link.attributes.get("href", ""), url))
            test_case.assertEqual(link.attributes.get("aria-current") == "page", label == current)
            previous_index = index
            matched_links.append(link)
    test_case.assertEqual(
        [
            " ".join(link.text().split())
            for link in links
            if link.attributes.get("aria-current") == "page"
        ],
        [current],
    )
    return matched_links


def assert_rendered_active_navigation(test_case, response, *, aria_label, current):
    parser = _RenderedSurfaceParser()
    parser.feed(response.content.decode())
    navigations = [
        element
        for element in parser.elements
        if element.tag == "nav" and element.attributes.get("aria-label") == aria_label
    ]
    test_case.assertEqual(len(navigations), 1)
    test_case.assertEqual(
        [
            " ".join(link.text().split())
            for link in navigations[0].descendants()
            if link.tag == "a" and link.attributes.get("aria-current") == "page"
        ],
        [current],
    )


def assert_rendered_navigation_link(test_case, response, *, aria_label, label, url, current):
    parser = _RenderedSurfaceParser()
    parser.feed(response.content.decode())
    navigations = [
        element
        for element in parser.elements
        if element.tag == "nav" and element.attributes.get("aria-label") == aria_label
    ]
    test_case.assertEqual(len(navigations), 1)
    links = [
        element
        for element in navigations[0].descendants()
        if element.tag == "a" and " ".join(element.text().split()) == label
    ]
    test_case.assertEqual(len(links), 1)
    test_case.assertTrue(_action_matches(links[0].attributes.get("href", ""), url))
    test_case.assertEqual(links[0].attributes.get("aria-current") == "page", current)
    return links[0]


def assert_removal_section(test_case, response, heading_id, action):
    parser = _RenderedSurfaceParser()
    parser.feed(response.content.decode())
    sections = [
        element
        for element in parser.elements
        if element.tag == "section"
        and heading_id in element.attributes.get("aria-labelledby", "").split()
    ]
    test_case.assertEqual(len(sections), 1)
    section = sections[0]
    test_case.assertIn("surface-tone--danger", section.attributes.get("class", "").split())
    test_case.assertTrue(
        any(
            element.tag == "h2" and element.attributes.get("id") == heading_id
            for element in section.descendants()
        )
    )
    forms = [
        element
        for element in section.descendants()
        if element.tag == "form"
        and element.attributes.get("method", "").lower() == "post"
        and _action_matches(element.attributes.get("action", ""), action)
    ]
    test_case.assertEqual(len(forms), 1)
    test_case.assertTrue(
        any(
            element.tag == "button"
            and element.attributes.get("type") == "submit"
            and "remove" in element.text().lower().split()
            for element in forms[0].descendants()
        )
    )


def assert_detail_note_value(test_case, response, action, expected):
    parser = _RenderedSurfaceParser()
    parser.feed(response.content.decode())
    forms = [
        element
        for element in parser.elements
        if element.tag == "form"
        and any(
            child.attributes.get("name") == "action" and child.attributes.get("value") == action
            for child in element.descendants()
        )
    ]
    test_case.assertEqual(len(forms), 1)
    notes = [
        element
        for element in forms[0].descendants()
        if element.tag == "textarea" and element.attributes.get("name") == "reason"
    ]
    test_case.assertEqual(len(notes), 1)
    # HTML ignores the first newline immediately following a textarea start tag.
    test_case.assertEqual(notes[0].text().removeprefix("\n"), expected)


def assert_rendered_heading(test_case, response, text, *, level=1):
    parser = _RenderedSurfaceParser()
    parser.feed(response.content.decode())
    headings = [
        element
        for element in parser.elements
        if element.tag == f"h{level}" and " ".join(element.text().split()) == text
    ]
    test_case.assertEqual(len(headings), 1)


def assert_academic_event_action_heading(test_case, response, action):
    # Academic Event form headings identify the action and domain, not a fixed sentence.
    parser = _RenderedSurfaceParser()
    parser.feed(response.content.decode())
    headings = [element for element in parser.elements if element.tag == "h1"]
    test_case.assertEqual(len(headings), 1)
    title = " ".join(headings[0].text().split())
    for component in (action, "Academic Event"):
        test_case.assertRegex(title, rf"(?i)\b{re.escape(component)}\b")


def rendered_work_remaining_summary_text(response):
    """Return visible summary prose from the rendered Work Remaining surface."""
    parser = _RenderedSurfaceParser()
    parser.feed(response.content.decode())
    summaries = [
        element
        for element in parser.elements
        if element.tag == "section"
        and "workspace-work-summary" in element.attributes.get("class", "").split()
    ]
    if len(summaries) != 1:
        raise AssertionError("Expected one rendered Work Remaining summary.")
    return " ".join(summaries[0].text().split())


def assert_rendered_work_remaining_summary_terms(test_case, summary_text, expected_terms):
    """Assert independently expected counts and grammatical labels in summary prose."""
    for count, label in expected_terms:
        test_case.assertRegex(
            summary_text,
            rf"(?<!\d){count}(?!\d)\s+{re.escape(label)}\b",
            f"Expected the rendered Work Remaining summary to include {count} {label}.",
        )


def assert_month_omits_page_summaries(test_case, response):
    for text in (
        "Empty draft.",
        "Assignments in progress.",
        "All shifts have their required Preceptor positions.",
        "Position order:",
        '<p class="finding-summary">',
        "Publication is blocked until hard findings are resolved.",
    ):
        test_case.assertNotContains(response, text)


class MonthlyWorkspaceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.client.force_login(self.user)

    @patch(
        "schedules.services.work_remaining.organization_today",
        return_value=date(2026, 8, 1),
    )
    def test_month_uses_one_calendar_matrix_and_omits_weekend_only_edges(self, _today):
        response = self.client.get(reverse("monthly-workspace"), {"month": "2026-08"})

        self.assertContains(response, "August 2026")
        assert_month_omits_page_summaries(self, response)
        for weekday in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"):
            self.assertContains(response, f'role="columnheader">{weekday}</div>', count=1)
        self.assertNotContains(response, "calendar-lattice-week--openable")
        self.assertContains(response, 'aria-label="AM"', count=5)
        self.assertContains(response, 'aria-label="PM"', count=5)
        self.assertContains(response, 'aria-label="Late Doc"', count=5)
        self.assertNotContains(response, ">Edit<")
        self.assertNotContains(response, "data-calendar-week-action=")
        self.assertNotContains(response, 'src="/static/schedules/calendar-week-actions.js"')
        self.assertContains(response, "calendar-selected-month", count=84)
        self.assertContains(response, "calendar-adjacent-month", count=16)
        self.assertContains(response, "Work remaining")
        self.assertContains(response, "required actions remain:")
        self.assertContains(response, "Select a name or empty slot to make changes.")
        self.assertLess(
            response.content.index(b"Select a name or empty slot to make changes."),
            response.content.index(b'class="calendar-surface calendar-lattice'),
        )
        self.assertNotContains(response, "calendar-week-header")
        self.assertNotContains(response, "week-summary")
        self.assertNotContains(response, 'datetime="2026-07-27"')
        self.assertContains(response, 'datetime="2026-08-03"')
        self.assertContains(response, 'datetime="2026-09-04"')
        first_week = response.context["weeks"][0]
        self.assertEqual(first_week["week_start"], date(2026, 8, 3))
        self.assertEqual(
            [day["date"] for day in first_week["days"]],
            [date(2026, 8, day) for day in range(3, 8)],
        )
        self.assertEqual(response.context["weeks"][-1]["week_start"], date(2026, 8, 31))
        self.assertEqual(
            [day["date"] for day in response.context["weeks"][-1]["days"]],
            [date(2026, 8, 31), *[date(2026, 9, day) for day in range(1, 5)]],
        )
        self.assertTrue(all(len(day["sessions"]) == 2 for day in first_week["days"]))
        self.assertEqual(SchedulePeriod.objects.count(), 2)
        self.assertEqual(PreceptorSession.objects.count(), 86)

        self.client.get(reverse("monthly-workspace"), {"month": "2026-08"})
        self.assertEqual(SchedulePeriod.objects.count(), 2)
        self.assertEqual(PreceptorSession.objects.count(), 86)

    def test_calendar_edit_instructions_use_shared_stack_spacing(self):
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()
        template_root = Path(__file__).resolve().parents[1] / "templates" / "schedules"

        self.assertIn(".calendar-edit-instruction { margin: 0; }", stylesheet)
        self.assertIn(
            ".surface-stack, .workspace-surface-stack { display: grid; "
            "gap: var(--surface-stack-gap); margin-top: var(--section-gap); }",
            stylesheet,
        )
        for template_name in ("monthly_workspace.html",):
            with self.subTest(template=template_name):
                template = (template_root / template_name).read_text()
                self.assertIn('class="workspace-surface-stack"', template)
                self.assertIn('class="meta calendar-edit-instruction"', template)

    def test_month_calendar_actions_align_pdf_link_and_wrap_on_narrow_screens(self):
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()
        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "schedules"
            / "monthly_workspace.html"
        ).read_text()

        self.assertIn('<div class="calendar-month-actions">', template)
        self.assertIn('<a href="{{ precepting_calendar_pdf_url }}">PDF</a>', template)
        self.assertIn(
            ".calendar-month-actions { display: flex; flex-wrap: wrap; align-items: center; "
            "gap: var(--section-action-gap); }",
            stylesheet,
        )
        self.assertIn(".calendar-month-actions > a { margin-left: auto; }", stylesheet)
        self.assertIn(
            "@media (max-width: 34rem) { .calendar-month-actions > a { flex-basis: 100%; "
            "margin-left: 0; } }",
            stylesheet,
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2026, 9, 12, 18, tzinfo=UTC),
    )
    @patch("schedules.views.preceptors._preceptor_week_work_items")
    def test_month_surfaces_required_work_from_its_summary_projection(self, work_items, _now):
        week_start = date(2026, 9, 28)
        work_items.return_value = (
            ProjectedWorkItem(
                key="precepting-week-2026-09-28",
                area="precepting",
                label="Week of Sep 28",
                affected_range=InclusiveDateRange(week_start, week_start + timedelta(days=4)),
                matters_on=week_start,
                target_url="/?month=2026-09#precepting-week-2026-09-28",
                reasons=(ProjectedWorkReason("required", "Two required actions", 2),),
            ),
            ProjectedWorkItem(
                key="precepting-week-2026-10-05",
                area="precepting",
                label="Week of Oct 5",
                affected_range=InclusiveDateRange(
                    week_start + timedelta(days=7), week_start + timedelta(days=11)
                ),
                matters_on=week_start + timedelta(days=7),
                target_url="/?month=2026-10#precepting-week-2026-10-05",
                reasons=(
                    ProjectedWorkReason("required", "Three required actions", 3),
                    ProjectedWorkReason("advisory", "Review a finding", 1),
                ),
            ),
        )

        response = self.client.get(reverse("monthly-workspace"), {"month": "2026-10"})

        summary = response.context["work_remaining"]
        self.assertEqual(summary.required_item_count, 5)
        parser = _RenderedSurfaceParser()
        parser.feed(response.content.decode())
        summary_element = next(
            element
            for element in parser.elements
            if element.attributes.get("aria-labelledby") == "work-remaining-summary-heading"
        )
        self.assertIn(summary.prose, summary_element.text())
        # Required work and real weekly findings exist in both an edge and an interior week.
        for start in (week_start, week_start + timedelta(days=7)):
            self.assertTrue(
                any(
                    finding.code == "missing_normal_rounder"
                    for finding in response.context["week_findings"][start]
                )
            )
        for start in (date(2026, 9, 28), *(date(2026, 10, day) for day in (5, 12, 19, 26))):
            with self.subTest(week=start):
                week_element = next(
                    element
                    for element in parser.elements
                    if element.attributes.get("id") == f"precepting-week-{start:%Y-%m-%d}"
                )
                gutters = [
                    element
                    for element in week_element.descendants()
                    if "calendar-lattice-date-gutter" in element.attributes.get("class", "").split()
                ]
                self.assertEqual(len(gutters), 1)
                self.assertEqual(gutters[0].text().strip(), "")
        self.assertNotContains(response, "calendar-week-publication-status--not-published")

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2026, 8, 3, 18, tzinfo=UTC),
    )
    def test_precepting_sibling_summaries_share_month_totals(self, _now):
        month = "2026-08"
        calendar = self.client.get(reverse("monthly-workspace"), {"month": month})
        listing = self.client.get(reverse("preceptor-list"), {"month": month})
        month_summary = calendar.context["work_remaining"]
        self.assertEqual(month_summary, listing.context["work_remaining"])
        self.assertIsNotNone(month_summary.range_action)
        self.assertTrue(month_summary.required_summary_items)
        self.assertEqual(
            sum(item.count for item in month_summary.required_summary_items),
            month_summary.required_item_count,
        )
        for response in (calendar, listing):
            self.assertContains(response, month_summary.prose)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2026, 8, 3, 18, tzinfo=UTC),
    )
    def test_calendar_and_list_open_the_displayed_month_in_schedule_bot(self, _now):
        expected_action = "/solve/?range_kind=month&amp;month=2026-08"
        calendar = self.client.get(reverse("monthly-workspace"), {"month": "2026-08"})
        listing = self.client.get(reverse("preceptor-list"), {"month": "2026-08"})
        for response in (calendar, listing):
            self.assertContains(response, expected_action, html=False)
            self.assertEqual(
                response.context["work_remaining"].range_action.url,
                "/solve/?range_kind=month&month=2026-08",
            )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2026, 8, 3, 18, tzinfo=UTC),
    )
    @patch("schedules.views.preceptors._preceptor_week_work_items")
    def test_calendar_and_list_schedule_bot_visibility_follows_finding_categories(
        self, work_items, _now
    ):
        selected_range = InclusiveDateRange(date(2026, 8, 3), date(2026, 8, 7))

        def item(key, *reasons):
            return ProjectedWorkItem(
                key=key,
                area="precepting",
                label="Week of Aug 3",
                affected_range=selected_range,
                matters_on=selected_range.start,
                target_url="/?month=2026-08#precepting-week-2026-08-03",
                reasons=reasons,
            )

        unrelated = ProjectedWorkReason(
            "advisory",
            "This display text is not used for visibility",
            1,
        )
        finding = ProjectedWorkReason(
            "required",
            "This display text is not used for visibility",
            1,
            WorkSummaryType("unfilled-position", "unfilled position", "unfilled positions"),
        )
        another_finding = ProjectedWorkReason(
            "required",
            "This display text is not used for visibility",
            1,
            WorkSummaryType(
                "precepting-finding-holiday_closed_assignment",
                "closed holiday finding",
                "closed holiday findings",
            ),
        )
        cases = (
            ("one finding category", (item("finding", finding),), True),
            ("several finding categories", (item("findings", finding, another_finding),), True),
            ("advisory only", (item("advisory", unrelated),), False),
            ("mixed finding and advisory", (item("mixed", finding, unrelated),), True),
            ("no work", (), False),
        )

        for case, items, expected_action in cases:
            work_items.return_value = items
            for route in ("monthly-workspace", "preceptor-list"):
                with self.subTest(case=case, route=route):
                    response = self.client.get(reverse(route), {"month": "2026-08"})

                    self.assertEqual(
                        response.context["work_remaining"].range_action is not None,
                        expected_action,
                    )
                    if expected_action:
                        self.assertContains(response, "Open in Schedule Bot")
                    else:
                        self.assertNotContains(response, "Open in Schedule Bot")

    def test_normal_precepting_calendars_explicitly_opt_into_compact_late_doc_lane_variant(self):
        responses = (self.client.get(reverse("monthly-workspace"), {"month": "2026-08"}),)

        for response in responses:
            parser = _RenderedSurfaceParser()
            parser.feed(response.content.decode())
            lanes = [
                element for element in parser.elements if element.attributes.get("role") == "row"
            ]
            compact_variant = "calendar-lattice-lane--compact"
            late_doc_lanes = [
                lane for lane in lanes if lane.attributes.get("aria-label") == "Late Doc"
            ]
            self.assertTrue(late_doc_lanes)
            for lane in late_doc_lanes:
                self.assertIn(compact_variant, lane.attributes.get("class", "").split())
            for period in ("AM", "PM"):
                period_lanes = [
                    lane for lane in lanes if lane.attributes.get("aria-label") == period
                ]
                self.assertTrue(period_lanes)
                for lane in period_lanes:
                    self.assertNotIn(compact_variant, lane.attributes.get("class", "").split())

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2026, 1, 5, 18, tzinfo=UTC),
    )
    def test_future_precepting_work_has_calendar_and_list_parity(self, _now):
        month = "2026-06"
        calendar = self.client.get(reverse("monthly-workspace"), {"month": month})
        listing = self.client.get(reverse("preceptor-list"), {"month": month})
        for response in (calendar, listing):
            with self.subTest(response=response.request["PATH_INFO"]):
                self.assertGreater(response.context["work_remaining"].required_item_count, 0)
        self.assertEqual(
            calendar.context["weeks"][0]["days"][0]["sessions"][0].assignment_slots[0]["state"],
            "gap",
        )
        first_list_session = next(
            session
            for session in listing.context["sessions"]
            if session.date == date(2026, 6, 1) and session.period_of_day == "AM"
        )
        self.assertEqual(first_list_session.assignment_slots[0]["state"], "gap")
        first_week_slot = calendar.context["calendar"].weeks[0].lanes[0].items[0].content.slots[0]
        self.assertEqual(first_week_slot.state, "gap")
        self.assertTrue(first_week_slot.url)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2026, 1, 5, 18, tzinfo=UTC),
    )
    def test_historical_precepting_slots_stay_neutral_and_correctable(self, _now):
        response = self.client.get(reverse("monthly-workspace"), {"month": "2025-06"})

        self.assertEqual(response.context["work_remaining"].required_item_count, 0)
        slot = response.context["calendar"].weeks[0].lanes[0].items[0].content.slots[0]
        self.assertEqual(slot.state, "optional-empty")
        self.assertTrue(slot.url)

    def test_holiday_closed_sessions_are_not_incomplete_and_close_monday_late_doc(self):
        month = self.client.get(reverse("monthly-workspace"), {"month": "2028-12"})
        holiday_week = next(
            week
            for week in month.context["calendar"].weeks
            if week.days[0].iso_date == "2028-12-25"
        )

        self.assertEqual(month.context["incomplete_session_count"], 40)
        late_doc = next(lane for lane in holiday_week.lanes if lane.label == "Late Doc")
        self.assertEqual(
            (late_doc.items[0].content["display"], late_doc.items[0].content["state"]),
            ("Closed", "optional-empty"),
        )
        self.assertEqual(late_doc.period, "PM")
        self.assertEqual(
            holiday_week.calendar_lanes[2].holiday_columns,
            (1,),
        )
        self.assertEqual(
            late_doc.items[0].content["accessible_label"],
            "Late Doc Monday 5-6 PM: Closed for Christmas Day, AM and PM.",
        )

    def test_slot_calendars_retain_prominent_holiday_headers_and_light_closed_cells(self):
        response = self.client.get(reverse("monthly-workspace"), {"month": "2026-12"})
        content = response.content.decode()
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()

        self.assertIn(
            'class="calendar-holiday-header" role="note" '
            'aria-label="Holiday: Christmas Eve, PM only"><span>Christmas Eve, PM</span></span>',
            content,
        )
        self.assertIn(
            'class="calendar-holiday-header" role="note" '
            'aria-label="Holiday: Christmas Day, AM and PM"><span>Christmas Day</span></span>',
            content,
        )
        self.assertIn(
            'class="month-slot session-slot month-slot-holiday-closed" '
            'aria-label="AM assignments for Friday, December 25, 2026: Closed for Christmas Day, '
            'AM and PM.">Closed',
            content,
        )
        self.assertIn(
            ".calendar-holiday-header { order: -1; display: inline-flex; gap: .15rem; "
            "color: #493967; font-size: .85rem; font-weight: 700;",
            stylesheet,
        )
        self.assertIn(
            ".calendar-closed-cell, .month-slot-holiday-closed { margin: 0; border-left: "
            ".22rem solid #6d5a96; padding: .2rem .35rem; background: #f3effa; color: #493967; "
            "font-size: .85rem; font-weight: 400; }",
            stylesheet,
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2026, 1, 1, tzinfo=UTC),
    )
    def test_future_assigned_holiday_conflicts_share_calendar_list_count_and_actionability(
        self, _now
    ):
        holiday = date(2027, 2, 1)
        physician = Physician.objects.create(display_name="Retained coverage")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2026, 1, 1),
        )
        period = open_monthly_draft(holiday)
        am_session = period.sessions.get(date=holiday, period_of_day="AM")
        pm_session = period.sessions.get(date=holiday, period_of_day="PM")
        for session in (am_session, pm_session):
            assign_preceptor(
                session=session,
                physician=physician,
                responsibility="primary",
                primary_position=1,
                scheduler=self.user,
            )
        LateDocAssignment.objects.create(date=holiday, physician=physician)
        schedule_holiday_occurrence(
            occurrence=holiday_occurrences_for(2027).get(holiday=ClinicHoliday.INDEPENDENCE_DAY),
            observed_date=holiday,
            scheduler=self.user,
        )

        month = self.client.get(reverse("monthly-workspace"), {"month": "2027-02"})
        listing = self.client.get(reverse("preceptor-list"), {"month": "2027-02"})
        for response in (month,):
            calendar = response.context["calendar"]
            am_item = next(
                item.content
                for calendar_week in calendar.weeks
                for lane in calendar_week.lanes
                if lane.label == "AM"
                for item in lane.items
                if item.content.date_label.endswith("Feb 1")
            )
            pm_item = next(
                item.content
                for calendar_week in calendar.weeks
                for lane in calendar_week.lanes
                if lane.label == "PM"
                for item in lane.items
                if item.content.date_label.endswith("Feb 1")
            )
            late_doc_item = next(
                item.content
                for calendar_week in calendar.weeks
                for lane in calendar_week.lanes
                if lane.label == "Late Doc"
                for item in lane.items
                if item.content and item.content["display"].endswith("Retained coverage")
            )

            self.assertEqual(am_item.slots[0].state, "conflict")
            self.assertEqual(pm_item.slots[0].state, "conflict")
            self.assertEqual(late_doc_item["state"], "conflict")
            if response is month:
                self.assertNotContains(
                    response,
                    "Clinic holiday closes this Precepting session while an assignment remains.",
                )
                self.assertNotContains(
                    response,
                    "Clinic holiday closes Monday Late Doc while an assignment remains.",
                )
            else:
                self.assertNotContains(
                    response,
                    "Clinic holiday closes this Precepting session while an assignment remains.",
                )
                self.assertNotContains(
                    response,
                    "Clinic holiday closes Monday Late Doc while an assignment remains.",
                )

        listed_session = next(
            item
            for item in listing.context["sessions"]
            if item.date == holiday and item.period_of_day == PreceptorSession.PeriodOfDay.AM
        )
        self.assertEqual(listed_session.assignment_slots[0]["state"], "conflict")
        self.assertEqual(month.context["work_remaining"], listing.context["work_remaining"])
        week_item = next(
            item
            for item in month.context["work_remaining"].items
            if item.key == "precepting-week-2027-02-01"
        )
        self.assertEqual(
            sum(
                reason.quantity
                for reason in week_item.reasons
                if reason.summary_type
                and reason.summary_type.key == "precepting-finding-holiday_closed_assignment"
            ),
            2,
        )
        week_slot = month.context["calendar"].weeks[0].lanes[0].items[0].content.slots[0]
        self.assertEqual(week_slot.state, "conflict")
        self.assertTrue(week_slot.url)

        late_doc_editor = self.client.get(reverse("late-doc-edit", args=(holiday.isoformat(),)))
        self.assertContains(
            late_doc_editor,
            "Clinic holiday closes Monday Late Doc while an assignment remains.",
        )

    def test_calendars_share_structural_primitives_and_preserve_schedule_hierarchy(self):
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()
        standard_guttered_responses = (
            self.client.get(reverse("monthly-workspace"), {"month": "2026-08"}),
            self.client.get(reverse("rounding-workspace"), {"month": "2026-08"}),
            self.client.get(reverse("community-preceptor-workspace"), {"month": "2026-08"}),
        )
        gutterless_responses = (
            self.client.get(reverse("vacation-workspace"), {"month": "2026-08"}),
            self.client.get(
                reverse("academic-event-workspace"), {"academic_year": "2026", "month": "2026-08"}
            ),
            self.client.get(
                reverse("interview-workspace"), {"academic_year": "2026", "month": "2026-08"}
            ),
        )
        for response in standard_guttered_responses:
            self.assertContains(response, 'class="calendar-surface calendar-lattice')
            self.assertContains(response, "calendar-lattice--with-gutter")
            self.assertContains(response, 'role="grid"')
            self.assertContains(response, "calendar-lattice-header")
            self.assertContains(response, "calendar-lattice-week")
            self.assertContains(response, "calendar-lattice-date-gutter")
            self.assertContains(response, "calendar-lattice-status-gutter")
            self.assertContains(response, "calendar-lattice-lane-label")
            self.assertContains(response, 'class="calendar-lattice-lane-body" role="presentation"')
        for response in gutterless_responses:
            self.assertContains(response, 'class="calendar-surface calendar-lattice')
            self.assertContains(response, "calendar-lattice--without-gutter")
            self.assertContains(response, 'role="grid"')
            self.assertContains(response, "calendar-lattice-header")
            self.assertContains(response, "calendar-lattice-week")
            self.assertNotContains(response, "calendar-lattice-gutter")
            self.assertNotContains(response, "calendar-lattice-lane-label")
            self.assertContains(response, 'class="calendar-lattice-lane-body" role="presentation"')

        def rule(selector):
            match = re.search(rf"\.{re.escape(selector)} \{{(?P<properties>[^}}]+)\}}", stylesheet)
            self.assertIsNotNone(match, f"Missing shared calendar rule for {selector}")
            return match["properties"]

        weekday_header = rule("calendar-weekday-header")
        date_header = rule("calendar-date-header")
        date_header_label = rule("calendar-date-header-label")
        self.assertIn("font-weight: 700;", weekday_header)
        self.assertIn("padding: .2rem .6rem;", weekday_header)
        self.assertIn("font-size: .85rem;", date_header_label)
        self.assertIn("font-weight: 600;", date_header_label)
        self.assertIn("justify-content: flex-end;", date_header)
        self.assertIn("min-height: 1.5rem;", date_header)
        self.assertIn("padding: .1rem .6rem;", date_header)
        self.assertIn("align-items: center;", date_header)
        status_gutter = rule("calendar-lattice-status-gutter")
        self.assertIn("display: flex;", status_gutter)
        self.assertIn("align-items: center;", status_gutter)
        self.assertIn("justify-content: center;", status_gutter)
        self.assertIn("gap: .35rem;", status_gutter)
        date_gutter = rule("calendar-lattice-date-gutter")
        self.assertIn("padding: 2px;", date_gutter)
        week_openable = rule("calendar-lattice-week--openable")
        self.assertIn("cursor: pointer;", week_openable)
        self.assertIn(
            ".calendar-lattice-week--openable:hover, .calendar-lattice-week--openable:focus-within",
            stylesheet,
        )
        status_tooltip = rule("calendar-week-status-tooltip")
        self.assertIn("left: 0;", status_tooltip)
        self.assertNotIn("transform:", status_tooltip)
        status_tooltip_end = rule("calendar-week-status-tooltip--end")
        self.assertIn("right: 0;", status_tooltip_end)
        self.assertIn("left: auto;", status_tooltip_end)
        action_affordance = rule("calendar-cell-action-affordance")
        self.assertIn("flex: 0 0 auto;", action_affordance)
        self.assertIn("margin-left: .35rem;", action_affordance)
        self.assertIn("background: var(--surface-tone-calendar);", rule("calendar-selected-month"))
        self.assertIn("background: #cfd5dc;", rule("calendar-adjacent-month"))
        self.assertIn("background: var(--surface);", rule("calendar-content-cell"))
        self.assertNotIn(".calendar-content-cell.calendar-selected-month", stylesheet)
        self.assertIn("background: #f5f6f8;", rule("calendar-content-cell.calendar-adjacent-month"))
        self.assertIn(
            "border-right: 1px solid var(--border);",
            rule("calendar-lattice-background > span"),
        )
        self.assertIn(".calendar-lattice-lane { border-bottom: 1px solid #dce2eb; }", stylesheet)
        self.assertIn("border-right: 1px solid #dce2eb;", rule("calendar-lattice-lane-label"))
        self.assertIn(".calendar-surface--interview { --calendar-min-width: 56rem; }", stylesheet)
        self.assertContains(gutterless_responses[-1], "calendar-surface--interview")
        self.assertNotIn("vacation-date-add-affordance", stylesheet)
        self.assertNotIn("section:not(", stylesheet)
        self.assertRegex(
            rule("calendar-lattice-week + .calendar-lattice-week"),
            r"border-top: [1-9]\d*(?:\.\d+)?(?:px|rem|em) solid [^;]+;",
        )
        self.assertNotIn(".calendar-week-boundary + .calendar-week-boundary", stylesheet)
        self.assertIn(
            ".calendar-lattice--with-gutter { grid-template-columns: max-content "
            "repeat(var(--calendar-columns), minmax(0, 1fr)); }",
            stylesheet,
        )
        self.assertIn(
            ".calendar-lattice--without-gutter { grid-template-columns: "
            "repeat(var(--calendar-columns), minmax(0, 1fr)); }",
            stylesheet,
        )
        self.assertIn(
            ".calendar-lattice-header, .calendar-lattice-week, .calendar-lattice-date-row, "
            ".calendar-lattice-lane { display: grid; grid-column: 1 / -1; "
            "grid-template-columns: subgrid; }",
            stylesheet,
        )
        self.assertIn(
            ".calendar-lattice-background, .calendar-lattice-items { display: grid; "
            "grid-template-columns: repeat(var(--calendar-columns), minmax(0, 1fr)); }",
            stylesheet,
        )
        calendar_items_match = re.search(
            r"(?m)^\.calendar-lattice-items \{(?P<properties>[^}]+)\}", stylesheet
        )
        self.assertIsNotNone(calendar_items_match)
        calendar_items = calendar_items_match["properties"]
        self.assertIn("row-gap: .3rem;", calendar_items)
        self.assertIn("padding-block: .45rem;", calendar_items)
        self.assertNotRegex(calendar_items, r"(?<![-\w])gap:")
        self.assertNotIn("column-gap:", calendar_items)
        self.assertNotIn("padding:", calendar_items)
        calendar_item = rule("calendar-lattice-item")
        self.assertIn("margin-inline: .45rem;", calendar_item)
        self.assertNotIn("grid-column:", calendar_item)
        self.assertIn("pointer-events: none;", calendar_item)
        self.assertIn("pointer-events: auto;", rule("calendar-lattice-item > *"))
        community_commitments = rule("community-preceptor-calendar-commitments")
        self.assertIn("padding-block: .35rem;", community_commitments)
        self.assertIn("padding-inline: 0;", community_commitments)
        self.assertNotIn("padding:", community_commitments)
        interview_shift = rule("interview-calendar-shift")
        self.assertIn("margin-block: .35rem;", interview_shift)
        self.assertNotIn("margin:", interview_shift)
        self.assertNotIn("margin-inline:", interview_shift)
        self.assertIn(
            ".calendar-lattice-lane-label { padding: .2rem .25rem;",
            stylesheet,
        )
        self.assertIn("white-space: nowrap;", rule("calendar-lattice-lane-label"))
        self.assertIn(
            ".calendar-surface--five-column { --calendar-min-width: 42.5rem; }", stylesheet
        )
        self.assertIn(
            ".calendar-surface--seven-column { --calendar-min-width: 49rem; }", stylesheet
        )
        template_root = Path(__file__).resolve().parents[1] / "templates" / "schedules"
        for template_name in (
            "preceptor_month_calendar.html",
            "preceptor_week_calendar.html",
            "vacation_calendar.html",
            "academic_event_calendar.html",
            "interview_calendar.html",
            "community_preceptor_calendar.html",
            "rounding_calendar_surface.html",
        ):
            with self.subTest(template=template_name):
                self.assertEqual(
                    (template_root / template_name).read_text(),
                    '{% include "schedules/calendar_lattice.html" %}\n',
                )
        for template_name in ("monthly_workspace.html",):
            with self.subTest(template=template_name):
                self.assertIn(
                    '"schedules/schedule_workspace_calendar_frame.html"',
                    (template_root / template_name).read_text(),
                )
                self.assertIn(
                    '"schedules/schedule_workspace_base.html"',
                    (template_root / template_name).read_text(),
                )

    def test_data_table_headers_use_a_shared_surface_tone_without_affecting_calendars(self):
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()
        template_root = Path(__file__).resolve().parents[1] / "templates" / "schedules"

        self.assertIn(
            'table > thead th, table > tbody > tr:first-child > th:not([scope="row"]):not([scope="rowgroup"]):not([scope="colgroup"]) { background: '
            "var(--surface-tone-subtle); }",
            stylesheet,
        )
        self.assertIn(
            "<thead><tr><th>Date</th>", (template_root / "preceptor_schedule_list.html").read_text()
        )
        self.assertIn(
            '"schedules/data_table.html" with table=vacation_history_table',
            (template_root / "vacation_history.html").read_text(),
        )
        self.assertNotIn("<table", (template_root / "calendar_lattice.html").read_text())
        self.assertIn(
            ".calendar-weekday-header { padding: .2rem .6rem; border-right: 1px solid "
            "var(--border); border-bottom: 1px solid var(--border); background: var(--surface-tone-calendar);",
            stylesheet,
        )

    def test_scrollable_tables_use_the_shared_edge_preserving_frame(self):
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()
        template_root = Path(__file__).resolve().parents[1] / "templates" / "schedules"

        self.assertIn(
            ".table-scroll-frame { max-width: 100%; overflow-x: auto; padding-right: 1px; }",
            stylesheet,
        )
        for obsolete_wrapper in (
            "schedule-report-table-wrap",
            "late-doc-review-table-wrap",
            "vacation-list-table-wrap",
            "recurrence-rule-table-wrap",
            "account-table-wrap",
            "rounding-list-table-wrap",
            "preceptor-schedule-list-table-wrap",
            "preceptor-week-table-wrap",
        ):
            with self.subTest(obsolete_wrapper=obsolete_wrapper):
                self.assertNotIn(obsolete_wrapper, stylesheet)

        for template_name in (
            "schedule_report.html",
            "precepting_solver_review.html",
            "vacation_list.html",
            "community_preceptor_list.html",
            "community_preceptor_rules.html",
            "interview_list.html",
            "preceptor_schedule_list.html",
            "_recurrence_rule_context.html",
            "account_workspace.html",
        ):
            with self.subTest(template=template_name):
                template = (template_root / template_name).read_text()
                self.assertTrue(
                    'class="table-scroll-frame"' in template
                    or '"schedules/data_table.html"' in template
                )
        self.assertIn(
            '"schedules/data_table.html" with table=rounding_list_table',
            (template_root / "rounding_list.html").read_text(),
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
    )
    def test_month_summarizes_conflicts_and_links_to_focused_editor_without_source_details(
        self, _now
    ):
        physician = Physician.objects.create(display_name="Unavailable faculty")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
        )
        period = open_monthly_draft(date(2025, 2, 1))
        session = period.sessions.get(date=date(2025, 2, 3), period_of_day="AM")
        assign_preceptor(
            session=session,
            physician=physician,
            responsibility="primary",
            primary_position=1,
            scheduler=self.user,
        )
        vacation = create_vacation(
            physician=physician,
            start_date=session.date,
            end_date=session.date,
            period_of_day="AM",
            scheduler=self.user,
        )

        response = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})

        assert_month_omits_page_summaries(self, response)
        self.assertContains(response, ">Unavailable faculty<")
        self.assertContains(
            response, 'aria-label="Preceptor 1: Hard conflict: Unavailable faculty"'
        )
        self.assertNotContains(response, "Conflict: Unavailable faculty")
        self.assertNotContains(response, "Assigned physician is not available for this session.")
        self.assertContains(response, "month-slot-conflict")
        self.assertContains(response, "month-slot-gap")
        self.assertContains(response, "month-slot-optional-empty")
        self.assertNotContains(response, "Assigned physician is not available for this session.")
        self.assertNotContains(response, f"/vacations/{vacation.pk}/correct/")
        self.assertContains(
            response, f'href="/sessions/{session.pk}/assignments/primary-1/?month=2025-02"'
        )
        self.assertNotContains(response, "Assignment history")
        for control in ("Assign", "Add", "Change", "Remove", "Locked", "Unlocked"):
            self.assertNotContains(response, f">{control}<")

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
    )
    def test_month_uses_compact_preference_and_clean_slot_styles(self, _now):
        clean = Physician.objects.create(display_name="Clean faculty")
        preference = Physician.objects.create(display_name="Backup faculty")
        for physician in (clean, preference):
            EffectiveRole.objects.create(
                physician=physician,
                role=EffectiveRole.Role.FACULTY,
                effective_start=date(2025, 1, 1),
            )
        available_slots = {slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES}
        create_faculty_availability_template(
            physician=clean,
            effective_start=None,
            effective_end=None,
            slots=available_slots,
            scheduler=self.user,
        )
        create_faculty_availability_template(
            physician=preference,
            effective_start=None,
            effective_end=None,
            slots={**available_slots, "monday_am": "backup_only"},
            scheduler=self.user,
        )
        period = open_monthly_draft(date(2025, 2, 1))
        monday = period.sessions.get(date=date(2025, 2, 3), period_of_day="AM")
        tuesday = period.sessions.get(date=date(2025, 2, 4), period_of_day="AM")
        assign_preceptor(
            session=monday,
            physician=preference,
            responsibility="primary",
            primary_position=1,
            scheduler=self.user,
        )
        assign_preceptor(
            session=tuesday,
            physician=clean,
            responsibility="primary",
            primary_position=1,
            scheduler=self.user,
        )
        self.assertEqual(PreceptorAssignment.objects.count(), 2)

        response = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})
        slots = {
            (session.date, session.period_of_day, slot["position"]): slot
            for week in response.context["weeks"]
            for day in week["days"]
            for session in day["sessions"]
            for slot in session.assignment_slots
        }

        self.assertEqual(
            (
                slots[(monday.date, "AM", "primary-1")]["state"],
                slots[(monday.date, "AM", "primary-1")]["display"],
            ),
            ("preference", "Backup faculty"),
        )
        self.assertEqual(
            (
                slots[(tuesday.date, "AM", "primary-1")]["state"],
                slots[(tuesday.date, "AM", "primary-1")]["display"],
            ),
            ("assigned", "Clean faculty"),
        )
        self.assertContains(response, "month-slot-preference")
        self.assertContains(response, ">Backup faculty<")
        self.assertContains(response, "Clean faculty")
        self.assertNotContains(response, "Check: Backup faculty")

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
    )
    def test_month_marks_insufficient_faculty_coverage_at_the_session_without_blaming_physicians(
        self, _now
    ):
        faculty = Physician.objects.create(display_name="Faculty primary")
        community = Physician.objects.create(display_name="Community primary")
        EffectiveRole.objects.create(
            physician=faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
        )
        EffectiveRole.objects.create(
            physician=community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 1, 1),
        )
        create_faculty_availability_template(
            physician=faculty,
            effective_start=None,
            effective_end=None,
            slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.user,
        )
        create_community_preceptor_rule(
            physician=community,
            weekday=0,
            ordinals=[1],
            periods=[PreceptorSession.PeriodOfDay.AM],
            scheduler=self.user,
        )
        period = open_monthly_draft(date(2025, 2, 1))
        session = period.sessions.get(date=date(2025, 2, 3), period_of_day="AM")
        assign_preceptor(
            session=session,
            physician=faculty,
            responsibility="primary",
            primary_position=1,
            scheduler=self.user,
        )
        assign_preceptor(
            session=session,
            physician=community,
            responsibility="primary",
            primary_position=2,
            scheduler=self.user,
        )

        response = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})

        self.assertContains(
            response,
            f'href="/sessions/{session.pk}/assignments/primary-1/?month=2025-02" '
            'aria-label="Hard finding: Too few faculty-equivalents. '
            'Open an assignment to review session findings."',
            count=1,
        )
        self.assertNotContains(response, "month-session-faculty-coverage-conflict")
        self.assertContains(
            response,
            "<span>Too few faculty-equivalents</span>",
        )
        self.assertNotContains(response, "Conflict: Faculty primary")
        self.assertNotContains(response, "Conflict: Community primary")
        slots = response.context["weeks"][0]["days"][0]["sessions"][0].assignment_slots
        self.assertEqual(
            [slot["state"] for slot in slots], ["assigned", "assigned", "optional-empty"]
        )

    def test_navigation_and_direct_month_selection_target_adjacent_months(self):
        response = self.client.get(reverse("monthly-workspace"), {"month": "2024-02"})
        content = response.content.decode()

        self.assertEqual(content.count("<h1"), 1)
        self.assertContains(
            response, '<h1 class="visually-hidden">Preceptor Schedule for February 2024</h1>'
        )
        self.assertNotContains(response, 'class="workspace-heading-label">Month:')
        self.assertNotContains(response, "Preceptor Schedule draft")
        self.assertContains(
            response,
            '<nav class="toolbar period-navigation period-navigation-cluster" '
            'aria-label="Month navigation">',
        )
        self.assertContains(response, 'aria-label="Previous month">&lsaquo;</a>')
        self.assertContains(response, "?month=2024-01")
        self.assertContains(
            response,
            '<label class="visually-hidden" for="schedule-workspace-month">Choose month</label>',
        )
        self.assertContains(response, "?month=2024-03")
        self.assertContains(response, 'aria-label="Next month">&rsaquo;</a>')
        self.assertContains(response, 'value="2024-02"')
        self.assertContains(response, '<noscript><button type="submit">View</button></noscript>')
        self.assertContains(response, "data-calendar-period-picker")
        self.assertContains(response, 'src="/static/schedules/calendar-period-picker.js"')
        self.assertLess(
            content.index("?month=2024-01"), content.index('id="schedule-workspace-month"')
        )
        self.assertLess(
            content.index('id="schedule-workspace-month"'), content.index("?month=2024-03")
        )

    def test_preceptor_workspace_navigation_keeps_calendar_selected_for_editor_and_omits_old_peers(
        self,
    ):
        navigation = (
            ("Calendar", reverse("monthly-workspace") + "?month=2025-02"),
            ("List", reverse("preceptor-list") + "?month=2025-02"),
            ("Schedule Bot", reverse("precepting-solver-workspace")),
        )
        exceptions = ("Exceptions", reverse("scheduling-exceptions"))
        month = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})
        schedule_bot = self.client.get(reverse("precepting-solver-workspace"))

        for response, current in (
            (month, "Calendar"),
            (
                self.client.get(
                    reverse(
                        "session-assignment-edit",
                        args=(
                            PreceptorSession.objects.get(date="2025-02-03", period_of_day="AM").pk,
                            "primary-1",
                        ),
                    )
                ),
                "Calendar",
            ),
            (schedule_bot, "Schedule Bot"),
        ):
            with self.subTest(path=response.request["PATH_INFO"]):
                self.assertEqual(
                    [
                        item["label"]
                        for item in response.context["schedule_workspace_navigation"]
                        if item["current"]
                    ],
                    [current],
                )
                if response is schedule_bot:
                    assert_rendered_active_navigation(
                        self,
                        response,
                        aria_label="Preceptor Schedule navigation",
                        current=current,
                    )
                else:
                    assert_rendered_navigation(
                        self,
                        response,
                        aria_label="Preceptor Schedule navigation",
                        expected=navigation,
                        current=current,
                    )

        rendered_links = assert_rendered_navigation(
            self,
            month,
            aria_label="Preceptor Schedule navigation",
            expected=navigation,
            current="Calendar",
        )
        exception_link = assert_rendered_navigation_link(
            self,
            month,
            aria_label="Preceptor Schedule navigation",
            label=exceptions[0],
            url=exceptions[1],
            current=False,
        )
        for link, (label, _) in (
            *zip(rendered_links, navigation, strict=True),
            (exception_link, exceptions),
        ):
            with self.subTest(destination=label):
                destination = self.client.get(link.attributes["href"])
                self.assertEqual(destination.status_code, 200)
                destination_navigation = destination.context["schedule_workspace_navigation"]
                self.assertEqual(
                    [entry["label"] for entry in destination_navigation if entry["current"]],
                    [label],
                )
                assert_rendered_active_navigation(
                    self,
                    destination,
                    aria_label="Preceptor Schedule navigation",
                    current=label,
                )
                if label in {"Calendar", "List"}:
                    self.assertEqual(
                        destination.context["schedule_workspace_selected_month"], date(2025, 2, 1)
                    )
        self.assertEqual(self.client.get("/work-items/").status_code, 404)


class PreceptorScheduleListTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.client.force_login(self.user)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
    )
    def test_list_orders_month_sessions_and_preserves_full_schedule_meaning_under_focus(self, _now):
        assigned = Physician.objects.create(display_name="Assigned outside focus")
        focused = Physician.objects.create(display_name="Focused physician")
        EffectiveRole.objects.create(
            physician=assigned,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
        )
        period = open_monthly_draft(date(2025, 2, 1))
        session = period.sessions.get(date=date(2025, 2, 3), period_of_day="AM")
        assign_preceptor(
            session=session,
            physician=assigned,
            responsibility="primary",
            primary_position=1,
            scheduler=self.user,
        )
        create_vacation(
            physician=assigned,
            start_date=session.date,
            end_date=session.date,
            period_of_day=session.period_of_day,
            scheduler=self.user,
        )
        scope_response = self.client.post(
            reverse("physician-scope"),
            {
                "physicians": [str(focused.pk)],
                "return": reverse("preceptor-list") + "?month=2025-02",
            },
        )
        self.assertRedirects(scope_response, reverse("preceptor-list") + "?month=2025-02")

        response = self.client.get(reverse("preceptor-list"), {"month": "2025-02"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<h2 id="preceptor-schedule-list-heading">Shifts</h2>')
        self.assertNotContains(response, '<h2 id="preceptor-schedule-list-heading">Sessions</h2>')
        self.assertContains(response, "<th>Preceptor 1</th>", html=True)
        self.assertContains(response, "<th>Preceptor 2</th>", html=True)
        self.assertEqual(len(response.context["sessions"]), 40)
        self.assertEqual(
            [(item.date, item.period_of_day) for item in response.context["sessions"]],
            sorted((item.date, item.period_of_day) for item in response.context["sessions"]),
        )
        self.assertEqual(response.context["sessions"][0].date, date(2025, 2, 3))
        self.assertEqual(response.context["sessions"][-1].date, date(2025, 2, 28))
        slots = response.context["sessions"][0].assignment_slots
        self.assertEqual(
            [(slot["position"], slot["display"], slot["state"]) for slot in slots],
            [
                ("primary-1", "Occupied", "conflict"),
                ("primary-2", "Unfilled", "gap"),
                ("secondary", "-", "optional-empty"),
            ],
        )
        self.assertContains(response, "Assigned physician is not available for this session.")
        self.assertContains(response, "preceptor-schedule-list-slot--gap table-cell-tone--hard")
        self.assertNotContains(response, "Publication")
        for session in (response.context["sessions"][0], response.context["sessions"][-1]):
            editor_url = (
                reverse("session-assignment-edit", args=(session.pk, "primary-1"))
                + "?month=2025-02"
            )
            self.assertContains(response, f'href="{editor_url}"')
            self.assertEqual(self.client.get(editor_url).context["session"].pk, session.pk)
        self.assertContains(response, 'href="/list/?month=2025-01"')
        self.assertContains(response, 'href="/list/?month=2025-03"')
        self.assertContains(response, "Mon, Feb 3")
        self.assertNotContains(response, "Mon, Feb 3, 2025")
        self.assertNotContains(response, "calendar-week-publication-status")
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()
        self.assertIn(
            ".preceptor-schedule-list-table { width: 100%; min-width: 58rem; table-layout: auto; }",
            stylesheet,
        )
        self.assertIn(
            ".preceptor-schedule-list-findings { width: 100%; overflow-wrap: anywhere; }",
            stylesheet,
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
    )
    @patch("schedules.views.preceptors.projected_draft_findings_for_weeks")
    def test_list_finding_cells_use_compact_hard_precedence_treatment(
        self, findings_projection, _now
    ):
        period = open_monthly_draft(date(2025, 2, 1))
        session = period.sessions.get(date=date(2025, 2, 3), period_of_day="AM")
        focused_findings = DraftFindingResult(
            (
                DraftFinding("backup_only_assignment", "preference", session, session.date, (), {}),
                DraftFinding(
                    "missing_primary_position",
                    "hard",
                    session,
                    session.date,
                    (),
                    {"position": "primary-1"},
                ),
                DraftFinding(
                    "missing_primary_position",
                    "hard",
                    session,
                    session.date,
                    (),
                    {"position": "primary-2"},
                ),
            )
        )
        findings_projection.side_effect = lambda weeks, *, complete_calendar=False: (
            focused_findings if session.date in weeks else DraftFindingResult(())
        )

        response = self.client.get(reverse("preceptor-list"), {"month": "2025-02"})
        content = response.content.decode()
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()

        self.assertIn(
            'class="preceptor-schedule-list-findings table-cell-tone--hard">'
            '<span class="table-cell-finding">Assigned physician is available as backup only for this '
            "session.</span>"
            '<span class="table-cell-finding">Preceptor 1 is unfilled.</span>'
            '<span class="table-cell-finding">Preceptor 2 is unfilled.</span>',
            content,
        )
        self.assertNotIn('class="finding finding-', content)
        self.assertIn(
            ".table-cell-tone--hard, .table-cell-tone--blocked { background: #fff0f0; "
            "color: var(--error); font-weight: 700; }",
            stylesheet,
        )
        self.assertIn(
            ".table-cell-finding + .table-cell-finding { margin-top: .2rem; }", stylesheet
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 5, 18, tzinfo=UTC),
    )
    @patch("schedules.views.preceptors.projected_draft_findings_for_weeks")
    def test_list_current_week_ignores_elapsed_findings_but_keeps_current_work(
        self, findings_projection, _now
    ):
        period = open_monthly_draft(date(2025, 2, 1))
        elapsed_session = period.sessions.get(date=date(2025, 2, 3), period_of_day="AM")
        current_session = period.sessions.get(date=date(2025, 2, 5), period_of_day="AM")
        findings = DraftFindingResult(
            tuple(
                DraftFinding(
                    "missing_primary_position",
                    "hard",
                    session,
                    session.date,
                    (),
                    {"position": "primary-1"},
                )
                for session in (elapsed_session, current_session)
            )
        )
        findings_projection.side_effect = lambda weeks, *, complete_calendar=False: (
            findings if elapsed_session.date in weeks else DraftFindingResult(())
        )

        response = self.client.get(reverse("preceptor-list"), {"month": "2025-02"})

        self.assertEqual(response.context["work_remaining"].required_item_count, 1)
        self.assertEqual(
            [
                (item.key, item.count)
                for item in response.context["work_remaining"].required_summary_items
            ],
            [("unfilled-position", 1)],
        )
        assert_rendered_work_remaining_summary_terms(
            self,
            rendered_work_remaining_summary_text(response),
            ((1, "required action"), (1, "unfilled position")),
        )
        table = _RenderedDataTable(
            response.content.decode(), table_class="preceptor-schedule-list-table"
        )
        for session, expected_finding in (
            (elapsed_session, "None"),
            (current_session, "Preceptor 1 is unfilled."),
        ):
            with self.subTest(date=session.date):
                row = next(
                    row
                    for row in table.body_rows()
                    if any(
                        element.attributes.get("datetime") == session.date.isoformat()
                        for element in row.descendants_named("time")
                    )
                    and row.children_named("td")[1].text().strip() == "AM"
                )
                finding_cell = next(
                    cell
                    for cell in row.children_named("td")
                    if "preceptor-schedule-list-findings"
                    in cell.attributes.get("class", "").split()
                )
                self.assertEqual(finding_cell.text().strip(), expected_finding)
                self.assertEqual(
                    "table-cell-tone--hard" in finding_cell.attributes.get("class", "").split(),
                    session == current_session,
                )
                self.assertIn(
                    f"/sessions/{session.pk}/assignments/primary-1/?month=2025-02",
                    [link.attributes.get("href") for link in row.descendants_named("a")],
                )

    def test_list_rejects_invalid_month(self):
        response = self.client.get(reverse("preceptor-list"), {"month": "February"})

        self.assertEqual(response.status_code, 400)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
    )
    def test_historical_list_sessions_are_readable_without_current_attention(self, _now):
        response = self.client.get(reverse("preceptor-list"), {"month": "2025-01"})

        self.assertEqual(len(response.context["sessions"]), 46)
        self.assertEqual(response.context["sessions"][0].date, date(2025, 1, 1))
        self.assertEqual(response.context["sessions"][-1].date, date(2025, 1, 31))
        self.assertNotContains(response, "preceptor-schedule-list-slot--gap")
        self.assertNotContains(response, "missing primary coverage")

    def test_workspace_heading_value_and_top_spacing_are_scoped_and_relational(self):
        month_response = self.client.get(reverse("monthly-workspace"), {"month": "2024-02"})
        rounding_response = self.client.get(reverse("rounding-workspace"))

        self.assertContains(
            month_response,
            '<main id="main-content" class="shell page-content">',
        )
        self.assertContains(
            rounding_response,
            '<main id="main-content" class="shell page-content">',
        )

    def test_period_picker_asset_enhances_the_existing_get_form(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "schedules"
            / "calendar-period-picker.js"
        ).read_text()

        self.assertIn('document.querySelectorAll("[data-calendar-period-picker]")', script)
        self.assertIn("data-calendar-period-picker-input", script)
        self.assertIn('typeof input.showPicker === "function"', script)
        self.assertIn('typeof form.requestSubmit !== "function"', script)
        self.assertIn('input.addEventListener("click"', script)
        self.assertIn('input.addEventListener("change"', script)
        self.assertIn("input.checkValidity()", script)
        self.assertIn("form.requestSubmit()", script)
        self.assertNotIn("preventDefault", script)
        self.assertFalse(
            (
                Path(__file__).resolve().parents[1] / "static" / "schedules" / "month-picker.js"
            ).exists()
        )

    def test_shared_calendar_picker_keeps_ordinary_form_controls_available(self):
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()

        self.assertRegex(stylesheet, r"button[^\{]*\{[^}]*display: inline-block;")

    def test_shared_period_picker_wrappers_match_picker_width(self):
        schedules_path = Path(__file__).resolve().parents[1]
        stylesheet = (schedules_path / "static" / "schedules" / "application.css").read_text()
        month_template = (
            schedules_path / "templates" / "schedules" / "period_navigation_month_picker.html"
        ).read_text()
        week_template = (
            schedules_path / "templates" / "schedules" / "schedule_workspace_week_navigation.html"
        ).read_text()

        self.assertIn('class="period-navigation-form period-navigation-picker"', month_template)
        self.assertIn('class="period-navigation-form period-navigation-picker"', week_template)
        self.assertIn(".period-navigation-form input { width: 11rem;", stylesheet)
        self.assertIn(
            ".period-navigation-cluster > .period-navigation-picker { flex: 0 1 11rem; }",
            stylesheet,
        )
        self.assertIn(
            ".period-navigation--academic-year { --period-navigation-center-width: 8rem;",
            stylesheet,
        )
        self.assertIn(
            ".period-navigation-cluster.period-navigation--academic-year > :is(form, select)",
            stylesheet,
        )
        self.assertIn("--period-navigation-gap: .35rem;", stylesheet)
        self.assertIn(
            ".schedule-workspace-period-controls--academic-year-month > "
            ".period-navigation-control { display: flex; flex-direction: column; "
            "align-items: flex-start; gap: .25rem; }",
            stylesheet,
        )

    def test_adjacent_year_boundary_assignment_uses_the_persisted_edge_session_and_month_origin(
        self,
    ):
        physician = Physician.objects.create(display_name="December faculty")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2024, 1, 1),
        )
        self.client.get(reverse("monthly-workspace"), {"month": "2025-01"})
        session = PreceptorSession.objects.get(date="2024-12-30", period_of_day="AM")
        assign_preceptor(
            session=session,
            physician=physician,
            responsibility="primary",
            primary_position=1,
            scheduler=self.user,
        )

        response = self.client.get(reverse("monthly-workspace"), {"month": "2025-01"})

        self.assertContains(response, "adjacent-month")
        parser = _RenderedSurfaceParser()
        parser.feed(response.content.decode())
        assigned_link = next(
            element
            for element in parser.elements
            if element.tag == "a" and element.text().strip() == physician.display_name
        )
        editor_url = (
            reverse("session-assignment-edit", args=(session.pk, "primary-1")) + "?month=2025-01"
        )
        self.assertEqual(assigned_link.attributes["href"], editor_url)
        for context in ("Preceptor 1", "AM", "December 30, 2024"):
            self.assertIn(context, assigned_link.attributes["aria-label"])
        self.assertEqual(response.context["weeks"][0]["days"][0]["sessions"][0].pk, session.pk)
        self.assertEqual(
            PreceptorSession.objects.filter(
                date__range=(date(2024, 12, 30), date(2025, 1, 3))
            ).count(),
            10,
        )
        editor = self.client.get(assigned_link.attributes["href"])
        self.assertEqual(editor.status_code, 200)
        self.assertEqual(editor.context["session"].pk, session.pk)
        self.assertEqual(editor.context["assignment"].physician_id, physician.pk)
        parser = _RenderedSurfaceParser()
        parser.feed(editor.content.decode())
        return_link = next(
            element
            for element in parser.elements
            if element.tag == "a"
            and "form-return-link" in element.attributes.get("class", "").split()
        )
        self.assertEqual(return_link.attributes["href"], "/?month=2025-01")
        returned_month = self.client.get(return_link.attributes["href"])
        self.assertEqual(returned_month.context["selected_month"], date(2025, 1, 1))

    def test_default_month_uses_the_organization_timezone(self):
        chicago_new_years_eve = datetime(2026, 1, 1, 5, tzinfo=UTC)
        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=chicago_new_years_eve,
        ):
            self.client.get(reverse("monthly-workspace"))

        self.assertTrue(SchedulePeriod.objects.filter(month="2025-12-01").exists())

    def test_rejects_an_invalid_direct_month_selection(self):
        response = self.client.get(reverse("monthly-workspace"), {"month": "February"})

        self.assertEqual(response.status_code, 400)

    def test_month_keeps_finding_legend_below_calendar(self):
        for route, selection in (("monthly-workspace", {"month": "2025-02"}),):
            with self.subTest(route=route):
                response = self.client.get(reverse(route), selection)
                parser = _RenderedSurfaceParser()
                parser.feed(response.content.decode())
                calendar = next(
                    element
                    for element in parser.elements
                    if "calendar-lattice" in element.attributes.get("class", "").split()
                )
                legend = next(
                    element
                    for element in parser.elements
                    if element.attributes.get("aria-label") == "Assignment finding legend"
                )
                self.assertLess(parser.elements.index(calendar), parser.elements.index(legend))
                for severity, symbol, label in (
                    ("hard", "!", "Hard finding"),
                    ("exception", "!", "Acknowledged exception"),
                    ("preference", "?", "Preference finding"),
                ):
                    glyph = next(
                        element
                        for element in legend.descendants()
                        if f"calendar-finding-indicator--{severity}"
                        in element.attributes.get("class", "").split()
                    )
                    self.assertEqual(glyph.text().strip(), symbol)
                    self.assertEqual(glyph.attributes.get("aria-hidden"), "true")
                    self.assertIn(label, glyph.parent.text())
                for retired_label in ("Not published", "Published", "Changed since publication"):
                    self.assertNotIn(retired_label, legend.text())

        calendar_without_key = self.client.get(reverse("vacation-workspace"), {"month": "2025-02"})
        self.assertNotContains(calendar_without_key, "calendar-status-legend")


class SchedulerSignInTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.workspace_url = reverse("monthly-workspace")
        self.sign_in_url = reverse("sign-in")

    def test_anonymous_workspace_request_redirects_without_creating_a_draft(self):
        response = self.client.get(self.workspace_url, {"month": "2025-02"})

        self.assertRedirects(
            response,
            self.sign_in_url + "?next=%2F%3Fmonth%3D2025-02",
            fetch_redirect_response=False,
        )
        self.assertEqual(SchedulePeriod.objects.count(), 0)
        self.assertEqual(PreceptorSession.objects.count(), 0)

    def test_active_user_signs_in_and_returns_to_requested_workspace(self):
        requested_workspace = self.workspace_url + "?month=2025-02"
        response = self.client.post(
            self.sign_in_url,
            {
                "username": self.user.username,
                "password": "test-password",
                "next": requested_workspace,
            },
        )

        self.assertRedirects(response, requested_workspace, fetch_redirect_response=False)
        response = self.client.get(requested_workspace)
        self.assertContains(response, "Signed in as")
        self.assertContains(response, self.user.username)
        self.assertContains(response, "Sign out")

    def test_invalid_or_inactive_credentials_do_not_sign_in(self):
        inactive_user = get_user_model().objects.create_user(
            username="inactive", password="test-password", is_active=False
        )

        for username, password in [
            (self.user.username, "wrong-password"),
            (inactive_user.username, "test-password"),
        ]:
            with self.subTest(username=username):
                response = self.client.post(
                    self.sign_in_url,
                    {"username": username, "password": password},
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Please enter a correct username and password.")
                self.assertContains(response, '<div class="sign-in-form-errors" role="alert">')
                self.assertNotContains(response, "inactive account")
                self.assertNotIn("_auth_user_id", self.client.session)

    def test_external_return_target_is_ignored(self):
        response = self.client.post(
            self.sign_in_url,
            {
                "username": self.user.username,
                "password": "test-password",
                "next": "https://example.com/not-local",
            },
        )

        self.assertRedirects(response, reverse("master-schedule"), fetch_redirect_response=False)

    def test_sign_out_requires_csrf_and_revokes_workspace_access(self):
        self.client.force_login(self.user)
        workspace_response = self.client.get(self.workspace_url)
        self.assertContains(workspace_response, "csrfmiddlewaretoken")

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(reverse("sign-out")).status_code, 403)

        response = self.client.post(reverse("sign-out"))
        self.assertRedirects(response, self.sign_in_url, fetch_redirect_response=False)

        response = self.client.get(self.workspace_url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlparse(response["Location"]).path, self.sign_in_url)
        self.assertEqual(
            parse_qs(urlparse(response["Location"]).query), {"next": [self.workspace_url]}
        )


class PreceptingCalendarDetailTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.user.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.client.force_login(self.user)
        self.url = reverse("monthly-workspace")

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 4, 18, tzinfo=UTC),
    )
    def test_late_doc_lanes_keep_compact_height_across_historical_and_operational_weeks(self, _now):
        def late_doc_slot(on_date):
            response = self.client.get(self.url, {"month": on_date[:7]})
            week = next(
                week
                for week in response.context["calendar"].weeks
                if week.days[0].iso_date == on_date
            )
            lane = next(lane for lane in week.lanes if lane.label == "Late Doc")
            return lane.items[0].content

        recorded_physician = Physician.objects.create(display_name="Recorded Late Doc")
        LateDocAssignment.objects.create(date=date(2025, 1, 27), physician=recorded_physician)

        historical = late_doc_slot("2025-01-20")
        populated_historical = late_doc_slot("2025-01-27")
        recent_past = late_doc_slot("2025-02-03")
        scheduled = late_doc_slot("2025-02-10")

        self.assertEqual(
            (historical["display"], historical["state"], historical["finding_tone"]),
            ("-", "optional-empty", ""),
        )
        self.assertEqual(
            (populated_historical["display"], populated_historical["state"]),
            ("Recorded Late Doc", "assigned"),
        )
        self.assertEqual(
            (recent_past["display"], recent_past["state"], recent_past["finding_tone"]),
            ("-", "optional-empty", ""),
        )
        self.assertEqual(
            (scheduled["display"], scheduled["state"], scheduled["finding_tone"]),
            ("Unfilled", "gap", "hard"),
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2026, 8, 26, 18, tzinfo=UTC),
    )
    def test_current_week_excludes_past_empty_late_doc_from_work_remaining(self, _now):
        week = date(2026, 8, 24)
        rounder = create_physician(
            display_name="Rounder",
            role=EffectiveRole.Role.FACULTY,
            effective_start=week,
            scheduler=self.user,
        )
        record_normal_rounder(week=week, physician=rounder, scheduler=self.user)

        monthly = self.client.get(self.url, {"month": "2026-08"})
        august_24 = next(
            week
            for week in monthly.context["calendar"].weeks
            if week.days[0].iso_date == "2026-08-24"
        )
        item = next(
            item
            for item in monthly.context["work_remaining"].items
            if item.affected_range.start == week
        )
        self.assertEqual(
            sum(reason.quantity for reason in item.reasons if reason.classification == "required"),
            12,
        )
        late_doc = (
            next(lane for lane in august_24.lanes if lane.label == "Late Doc").items[0].content
        )
        self.assertEqual(
            (late_doc["display"], late_doc["state"], late_doc["finding_tone"]),
            ("-", "optional-empty", ""),
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2026, 2, 3, 18, tzinfo=UTC),
    )
    def test_week_work_remaining_counts_late_doc_without_publication_work(self, _now):
        week = date(2027, 2, 1)
        rounder = create_physician(
            display_name="Rounder",
            role=EffectiveRole.Role.FACULTY,
            effective_start=week,
            scheduler=self.user,
        )
        record_normal_rounder(week=week, physician=rounder, scheduler=self.user)
        faculty = [
            create_physician(
                display_name=f"Faculty {position}",
                role=EffectiveRole.Role.FACULTY,
                effective_start=week,
                scheduler=self.user,
            )
            for position in (1, 2)
        ]
        for physician in faculty:
            create_faculty_availability_template(
                physician=physician,
                effective_start=week,
                effective_end=None,
                slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
                scheduler=self.user,
            )
        for session in open_weekly_draft(week):
            for position, physician in enumerate(faculty, start=1):
                assign_preceptor(
                    session=session,
                    physician=physician,
                    responsibility="primary",
                    primary_position=position,
                    scheduler=self.user,
                )

        response = self.client.get(self.url, {"month": week.strftime("%Y-%m")})

        item = next(
            item
            for item in response.context["work_remaining"].items
            if item.affected_range.start == week
        )
        self.assertEqual(
            [(reason.summary_type.key, reason.quantity) for reason in item.reasons],
            [("unfilled-late-doc", 1)],
        )
        self.assertContains(response, 'aria-label="Late Doc"')
        self.assertContains(
            response,
            'class="calendar-finding-indicator calendar-finding-indicator--hard" '
            'aria-hidden="true"><span aria-hidden="true">!</span><span '
            'class="calendar-finding-tooltip" aria-hidden="true"><span>Unfilled Late Doc</span>'
            "</span></span>",
        )
        self.assertNotContains(response, "Publish week")

    def test_shared_slots_keep_month_compact(self):
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()

        self.assertRegex(
            stylesheet,
            r"\.month-slot \{[^}]*overflow: hidden;[^}]*text-overflow: ellipsis;[^}]*white-space: nowrap;",
        )
        self.assertNotRegex(stylesheet, r"(?m)^\.session-slot \{\s*white-space: normal;\s*\}$")

    def test_month_finding_slots_layer_unclipped_details_above_calendar_cells(self):
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()

        self.assertIn(
            ".calendar-surface--assignment-month .month-slot:has(.calendar-finding-indicator) "
            "{ overflow: visible; }",
            stylesheet,
        )
        self.assertIn(".calendar-finding-tooltip { position: absolute; z-index: 3;", stylesheet)
        self.assertIn(
            ".calendar-surface--assignment-month .month-slot:has(.calendar-finding-indicator):hover "
            ".calendar-finding-tooltip { visibility: visible; opacity: 1;",
            stylesheet,
        )
        self.assertIn(
            ".calendar-lattice-item:has(.month-slot:hover .calendar-finding-indicator)",
            stylesheet,
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
    )
    def test_compact_calendars_render_historical_empty_assignment_slots_as_dashes(self, _now):
        def slot_displays(response):
            assignment = next(
                item.content
                for calendar_week in response.context["calendar"].weeks
                for lane in calendar_week.lanes
                if lane.label == "AM"
                for item in lane.items
                if item.content.date_label.startswith("Monday, Jan 20")
            )
            return [slot.display for slot in assignment.slots]

        month_response = self.client.get(reverse("monthly-workspace"), {"month": "2025-01"})

        self.assertEqual(slot_displays(month_response), ["-", "-", "-"])

    def test_finding_projection_deduplicates_ordered_target_summaries(self):
        targets = precepting_finding_targets(
            SimpleNamespace(date=date(2025, 2, 3), period_of_day="AM"),
            (
                DraftFinding(
                    "missing_primary_position", "hard", None, None, (), {"position": "primary-1"}
                ),
                DraftFinding(
                    "interview_assignment_conflict",
                    "hard",
                    None,
                    None,
                    (),
                    {"position": "primary-1"},
                ),
                DraftFinding("backup_only_assignment", "preference", None, None, (), {}),
                DraftFinding("backup_only_assignment", "preference", None, None, (), {}),
            ),
        )
        primary, day = targets

        self.assertEqual(
            (primary.date, primary.period, primary.position), (date(2025, 2, 3), "AM", "primary-1")
        )
        self.assertEqual(primary.marker.labels, ("Unfilled", "Interview conflict"))
        self.assertEqual(primary.marker.summary, "Hard finding: Unfilled, Interview conflict.")
        self.assertEqual((day.date, day.period, day.position), (date(2025, 2, 3), "AM", None))
        self.assertEqual(day.marker.severity, "preference")
        self.assertEqual(
            day.marker.summary,
            "Preference finding: Backup only.",
        )
        self.assertEqual(day.marker.labels, ("Backup only",))

    def test_direct_assignment_actions_default_to_month_and_shared_lock_state(self):
        physician = Physician.objects.create(display_name="Faculty")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        self.client.get(self.url, {"month": "2026-09"})
        session = PreceptorSession.objects.get(date="2026-09-01", period_of_day="AM")
        edit_url = reverse("session-assignment-edit", args=(session.pk, "primary-1"))

        editor = self.client.get(edit_url)
        self.assertContains(editor, "View assignment history")
        self.assertContains(
            editor,
            reverse("session-assignment-history", args=(session.pk,)),
        )

        rejected = self.client.post(edit_url, {"reason": "Missing selection"})
        self.assertEqual(rejected.status_code, 200)
        self.assertContains(rejected, 'href="/?month=2026-09"')

        response = self.client.post(
            edit_url, {"physician": physician.pk, "is_locked": "on", "reason": "Direct selection"}
        )
        month_url = self.url + "?month=2026-09"
        self.assertRedirects(response, month_url, fetch_redirect_response=False)
        assignment = PreceptorAssignment.objects.get(session=session)
        self.assertTrue(assignment.is_locked)

        month = self.client.get(month_url)
        self.assertContains(month, "Faculty")
        self.assertContains(
            month,
            '<span class="lock-indicator" role="img" aria-label="Locked" title="Locked">&#128274;</span>',
        )
        history_url = reverse("session-assignment-history", args=(session.pk,))
        history = self.client.get(history_url)
        self.assertContains(history, 'href="/?month=2026-09"')

        response = self.client.post(
            edit_url, {"physician": physician.pk, "reason": "Unlock month selection"}
        )
        self.assertRedirects(response, month_url, fetch_redirect_response=False)
        assignment.refresh_from_db()
        self.assertFalse(assignment.is_locked)
        self.assertNotContains(self.client.get(month_url), 'aria-label="Locked"')

        remove_url = reverse("session-assignment-remove", args=(session.pk, "primary-1"))
        response = self.client.post(remove_url, {"reason": "Remove month selection"})
        self.assertRedirects(response, month_url, fetch_redirect_response=False)
        self.assertFalse(PreceptorAssignment.objects.filter(pk=assignment.pk).exists())

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
    )
    def test_month_slot_findings_open_editor_with_week_details_and_rounding_access(self, _now):
        physician = Physician.objects.create(display_name="Unavailable faculty")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
        )
        self.client.get(self.url, {"month": "2025-02"})
        session = PreceptorSession.objects.get(date=date(2025, 2, 3), period_of_day="AM")
        assign_preceptor(
            session=session,
            physician=physician,
            responsibility="primary",
            primary_position=1,
            scheduler=self.user,
        )
        create_vacation(
            physician=physician,
            start_date=session.date,
            end_date=session.date,
            period_of_day="AM",
            scheduler=self.user,
        )

        response = self.client.get(self.url, {"month": "2025-02"})
        parser = _RenderedSurfaceParser()
        parser.feed(response.content.decode())
        slot_link = next(
            element
            for element in parser.elements
            if element.tag == "a"
            and urlparse(element.attributes.get("href", "")).path
            == reverse("session-assignment-edit", args=(session.pk, "primary-1"))
            and physician.display_name in element.text()
        )
        self.assertTrue(
            any(
                "calendar-finding-indicator" in element.attributes.get("class", "").split()
                for element in slot_link.descendants()
            )
        )
        assignment_editor = self.client.get(
            reverse("session-assignment-edit", args=(session.pk, "primary-1"))
        )
        self.assertContains(assignment_editor, "No normal rounder is recorded")
        self.assertContains(
            assignment_editor, "Assigned physician is not available for this session."
        )
        self.assertContains(assignment_editor, f"/physicians/{physician.pk}/?date=2025-02-03")
        rounding_url = reverse("rounding-workspace") + "?month=2025-02"
        self.assertContains(assignment_editor, f'href="{rounding_url}"')
        self.assertEqual(self.client.get(rounding_url).status_code, 200)


class PhysiciansViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.user.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.client.force_login(self.user)

    def directory_field(self, table, physician_name, field_name):
        field_indexes = {column.label: index for index, column in enumerate(table.columns)}
        self.assertIn("Physician", field_indexes)
        self.assertIn(field_name, field_indexes)
        rows = {row.cells[field_indexes["Physician"]].value: row for row in table.rows}
        self.assertIn(physician_name, rows)
        return rows[physician_name].cells[field_indexes[field_name]]

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 2, tzinfo=UTC),
    )
    def test_directory_views_use_local_today_and_keep_compatible_roles(self, now):
        faculty = Physician.objects.create(display_name="Boundary Faculty")
        dual_role = Physician.objects.create(display_name="Community and Casual")
        community_rounder = Physician.objects.create(display_name="Community rounder")
        ended = Physician.objects.create(display_name="Ended yesterday")
        future = Physician.objects.create(display_name="Future role")
        inactive = Physician.objects.create(display_name="No role")
        EffectiveRole.objects.create(
            physician=faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2024-12-31",
            effective_end="2024-12-31",
        )
        EffectiveRole.objects.create(
            physician=dual_role,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start="2024-12-31",
        )
        EffectiveRole.objects.create(
            physician=dual_role,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start="2024-12-31",
        )
        EffectiveRole.objects.create(
            physician=community_rounder,
            role=EffectiveRole.Role.COMMUNITY_ROUNDER,
            effective_start="2024-12-31",
        )
        EffectiveRole.objects.create(
            physician=ended,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2024-01-01",
            effective_end="2024-12-30",
        )
        EffectiveRole.objects.create(
            physician=future,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )

        def displayed_names(view):
            response = self.client.get(reverse("physicians-directory"), {"view": view})
            self.assertEqual(response.status_code, 200)
            return [physician.display_name for physician in response.context["physicians"]]

        self.assertEqual(
            displayed_names("all"),
            [
                faculty.display_name,
                dual_role.display_name,
                community_rounder.display_name,
                ended.display_name,
                future.display_name,
                inactive.display_name,
            ],
        )
        self.assertEqual(displayed_names("faculty"), [faculty.display_name])
        self.assertEqual(displayed_names("community-rounders"), [community_rounder.display_name])
        self.assertEqual(displayed_names("community-preceptors"), [dual_role.display_name])
        self.assertEqual(displayed_names("casual-rounders"), [dual_role.display_name])
        self.assertEqual(
            displayed_names("inactive"),
            [ended.display_name, future.display_name, inactive.display_name],
        )
        response = self.client.get(reverse("physicians-directory"))
        self.assertEqual(
            [physician.display_name for physician in response.context["active_physicians"]],
            [faculty.display_name, dual_role.display_name, community_rounder.display_name],
        )
        self.assertEqual(
            [physician.display_name for physician in response.context["inactive_physicians"]],
            [ended.display_name, future.display_name, inactive.display_name],
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 18, tzinfo=UTC),
    )
    def test_directory_shows_current_roles_attributes_and_source_configuration_in_tables(self, now):
        faculty = Physician.objects.create(display_name="Faculty leader")
        associate_program_director = Physician.objects.create(
            display_name="Associate program director"
        )
        community_and_casual = Physician.objects.create(display_name="Configured community casual")
        one_rule_community = Physician.objects.create(display_name="One community rule")
        unconfigured_community = Physician.objects.create(display_name="Unconfigured community")
        unconfigured_faculty = Physician.objects.create(display_name="Faculty without schedule")
        community_rounder = Physician.objects.create(display_name="Community rounder")
        inactive = Physician.objects.create(display_name="No current role")
        EffectiveRole.objects.create(
            physician=faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
            faculty_leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
        )
        EffectiveRole.objects.create(
            physician=associate_program_director,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
            faculty_leadership=EffectiveRole.FacultyLeadership.ASSOCIATE_PROGRAM_DIRECTOR,
        )
        EffectiveRole.objects.create(
            physician=community_and_casual,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start="2025-01-01",
            faculty_equivalent=True,
        )
        EffectiveRole.objects.create(
            physician=community_and_casual,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start="2025-01-01",
        )
        for physician in (one_rule_community, unconfigured_community):
            EffectiveRole.objects.create(
                physician=physician,
                role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
                effective_start="2025-01-01",
            )
        EffectiveRole.objects.create(
            physician=unconfigured_faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        EffectiveRole.objects.create(
            physician=community_rounder,
            role=EffectiveRole.Role.COMMUNITY_ROUNDER,
            effective_start="2025-01-01",
        )
        FacultyAvailabilityTemplate.objects.create(
            physician=faculty,
            slots={
                slot: FacultyAvailabilityTemplate.base_selection(
                    FacultyAvailabilityTemplate.AvailabilityState.OPEN
                )
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
        )
        for weekday in (0, 1):
            CommunityPreceptorRule.objects.create(
                physician=community_and_casual,
                weekday=weekday,
                ordinals=[1],
                periods=[CommunityPreceptorRule.PeriodOfDay.AM],
            )
            CasualRounderRule.objects.create(
                physician=community_and_casual, weekday=weekday, ordinals=[1]
            )
        CommunityPreceptorRule.objects.create(
            physician=one_rule_community,
            weekday=0,
            ordinals=[1],
            periods=[CommunityPreceptorRule.PeriodOfDay.AM],
        )

        response = self.client.get(reverse("physicians-directory"))

        active_table = response.context["active_physicians_table"]
        self.assertEqual(
            self.directory_field(active_table, faculty.display_name, "Physician").url,
            f"/physicians/{faculty.pk}/",
        )
        self.assertEqual(
            self.directory_field(
                active_table, faculty.display_name, "Current roles and attributes"
            ).kind,
            "tags",
        )
        self.assertEqual(
            self.directory_field(
                active_table, faculty.display_name, "Current roles and attributes"
            ).tags,
            ("Faculty", "PD"),
        )
        self.assertEqual(
            self.directory_field(
                active_table,
                associate_program_director.display_name,
                "Current roles and attributes",
            ).tags,
            ("Faculty", "APD"),
        )
        self.assertEqual(
            self.directory_field(active_table, faculty.display_name, "Source schedules").kind,
            "tags",
        )
        self.assertEqual(
            self.directory_field(active_table, faculty.display_name, "Source schedules").tags,
            ("Faculty: 1 template",),
        )
        self.assertEqual(
            self.directory_field(
                active_table, community_and_casual.display_name, "Current roles and attributes"
            ).tags,
            ("Community Preceptor", "Faculty-equivalent", "Casual Rounder"),
        )
        self.assertEqual(
            self.directory_field(
                active_table, community_and_casual.display_name, "Source schedules"
            ).tags,
            ("Community: 2 rules", "Casual: 2 rules"),
        )
        self.assertEqual(
            self.directory_field(
                active_table, unconfigured_faculty.display_name, "Source schedules"
            ).tags,
            ("Faculty: 0 templates",),
        )
        self.assertEqual(
            self.directory_field(
                active_table, community_rounder.display_name, "Current roles and attributes"
            ).tags,
            ("Community Rounder",),
        )
        self.assertEqual(
            self.directory_field(
                active_table, community_rounder.display_name, "Source schedules"
            ).value,
            "No current source schedules",
        )
        inactive_table = response.context["inactive_physicians_table"]
        self.assertEqual(
            self.directory_field(
                inactive_table, inactive.display_name, "Current roles and attributes"
            ).kind,
            "text",
        )
        self.assertEqual(
            self.directory_field(
                inactive_table, inactive.display_name, "Current roles and attributes"
            ).value,
            "No current role",
        )
        self.assertNotContains(response, "Leadership: None")
        source_details = {
            physician.display_name: physician.source_configuration
            for physician in response.context["physicians"]
        }
        self.assertEqual(source_details["One community rule"][0]["detail"], "1 rule")
        self.assertEqual(source_details["Unconfigured community"][0]["detail"], "not configured")
        self.assertContains(response, '<table id="physicians-active" class="data-table"')
        self.assertContains(response, '<th scope="row" class="data-table-cell--link"><a href=')
        self.assertContains(
            response,
            '<ul class="data-table-tags"><li class="data-table-tag">Faculty</li>'
            '<li class="data-table-tag">PD</li></ul>',
        )
        self.assertContains(
            response,
            '<ul class="data-table-tags"><li class="data-table-tag">Community: 2 rules</li>'
            '<li class="data-table-tag">Casual: 2 rules</li></ul>',
        )
        self.assertContains(response, "Faculty: 0 templates")
        self.assertContains(response, "Community: not configured")
        self.assertNotContains(response, "current scheduling rule")
        self.assertContains(response, "View details")
        self.assertContains(response, "Physician change history")
        content = response.content.decode()
        self.assertRegex(
            content,
            r'(?s)<div class="split-row physician-directory-view-row">\s*'
            r'<nav class="workspace-local-navigation" aria-label="Physician navigation">.*</nav>\s*'
            r'<a class="button-link split-row-action" href="/physicians/add/">Add physician</a>\s*</div>',
        )
        self.assertRegex(
            content,
            r'(?s)<div class="split-row physician-directory-date-row">\s*'
            r'<p class="physician-directory-date meta">.*</p>\s*'
            r'<a class="split-row-action" href="/physicians/history/">Physician change history</a>\s*</div>',
        )
        self.assertNotIn("physician-directory-actions", content)

        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()
        self.assertIn("@media (max-width: 34rem)", stylesheet)
        self.assertIn(
            ".split-row { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem 1rem; }",
            stylesheet,
        )
        self.assertIn(".split-row-action { flex: 0 0 auto; margin-left: auto; }", stylesheet)
        self.assertIn(
            ".physician-directory-view-row { align-items: end; border-bottom: 1px solid var(--border); }",
            stylesheet,
        )
        self.assertIn(
            ".physician-directory-view-row .workspace-local-navigation { flex: 1 1 24rem; border-bottom: 0; }",
            stylesheet,
        )
        self.assertIn(
            ".physician-directory-view-row .split-row-action { margin-bottom: .25rem; }",
            stylesheet,
        )
        self.assertIn(".split-row-action { flex-basis: 100%; margin-left: 0; }", stylesheet)
        self.assertNotIn("physician-directory-row", stylesheet)
        self.assertNotIn("physician-directory-badges", stylesheet)
        self.assertNotIn("physician-directory-sources", stylesheet)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 18, tzinfo=UTC),
    )
    def test_directory_capacity_uses_the_current_week_and_keeps_compact_values_accessible(
        self, now
    ):
        faculty = Physician.objects.create(display_name="Week capacity")
        overridden = Physician.objects.create(
            display_name="Custom capacity",
            faculty_precepting_capacity_override=Decimal("4.5"),
        )
        community = Physician.objects.create(display_name="Faculty equivalent community")
        inactive = Physician.objects.create(
            display_name="Inactive former faculty",
            faculty_precepting_capacity_override=Decimal("2.5"),
        )
        for physician, available_period in ((faculty, None), (overridden, "AM")):
            EffectiveRole.objects.create(
                physician=physician,
                role=EffectiveRole.Role.FACULTY,
                effective_start=date(2024, 12, 30),
            )
            FacultyAvailabilityTemplate.objects.create(
                physician=physician,
                slots={
                    slot: FacultyAvailabilityTemplate.base_selection(
                        "open"
                        if available_period is None or slot.endswith(available_period.lower())
                        else "off"
                    )
                    for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
                },
            )
        EffectiveRole.objects.create(
            physician=community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2024, 12, 30),
            faculty_equivalent=True,
        )

        response = self.client.get(reverse("physicians-directory"))

        active_table = response.context["active_physicians_table"]
        faculty_capacity = self.directory_field(active_table, faculty.display_name, "Capacity")
        overridden_capacity = self.directory_field(
            active_table, overridden.display_name, "Capacity"
        )
        community_capacity = self.directory_field(active_table, community.display_name, "Capacity")
        inactive_capacity = self.directory_field(
            response.context["inactive_physicians_table"], inactive.display_name, "Capacity"
        )
        self.assertEqual(faculty_capacity.value, "10")
        # TEXT-CONTRACT: physician-weekly-capacity-compact —
        # The directory shows custom(schedule-derived) weekly capacity with exact
        # parentheses, punctuation, and whitespace, without intervening spaces.
        # Production: schedules/views/physicians.py; protecting tests:
        # schedules/tests/test_views.py and schedules/tests/test_table_presentation_browser.py.
        self.assertEqual(overridden_capacity.value, "4.5(5)")
        self.assertEqual(overridden_capacity.kind, "capacity")
        self.assertRegex(
            overridden_capacity.accessible_label,
            r"(?i)\bcustom\b(?:(?!\bschedule(?:[-\s]+)derived\b).)*?\bcapacity\b"
            r"(?:(?!\bschedule(?:[-\s]+)derived\b).)*?\b4\.5\b",
        )
        self.assertRegex(
            overridden_capacity.accessible_label,
            r"(?i)\bschedule(?:[-\s]+)derived\b.*?\bcapacity\b.*?"
            r"(?<![\d.])5(?![\d.])",
        )
        self.assertEqual(community_capacity.value, "—")
        self.assertEqual(inactive_capacity.value, "—")

        faculty_view = self.client.get(reverse("physicians-directory"), {"view": "faculty"})
        self.assertEqual(
            self.directory_field(
                faculty_view.context["directory_table"], overridden.display_name, "Capacity"
            ).value,
            "4.5(5)",
        )
        community_view = self.client.get(
            reverse("physicians-directory"), {"view": "community-preceptors"}
        )
        self.assertEqual(
            self.directory_field(
                community_view.context["directory_table"], community.display_name, "Capacity"
            ).value,
            "—",
        )
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()
        self.assertIn(
            ".data-table-cell--capacity { position: relative; width: 1%; white-space: nowrap; }",
            stylesheet,
        )
        self.assertIn(
            ".table-scroll-frame { max-width: 100%; overflow-x: auto; padding-right: 1px; }",
            stylesheet,
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 18, tzinfo=UTC),
    )
    def test_directory_faculty_summary_counts_current_or_future_sources_once(self, now):
        faculty = Physician.objects.create(display_name="Faculty sources")
        Physician.objects.create(display_name="Another faculty source")
        EffectiveRole.objects.create(
            physician=faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        EffectiveRole.objects.create(
            physician=Physician.objects.get(display_name="Another faculty source"),
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        slots = {
            slot: FacultyAvailabilityTemplate.base_selection("open")
            for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
        }
        for effective_start, effective_end in (
            (None, None),
            (None, date(2025, 1, 1)),
            (date(2025, 2, 1), None),
            (None, date(2024, 12, 31)),
        ):
            FacultyAvailabilityTemplate.objects.create(
                physician=faculty,
                effective_start=effective_start,
                effective_end=effective_end,
                slots=slots,
            )
        for effective_end, is_active in (
            (None, True),
            (date(2025, 1, 1), True),
            (date(2024, 12, 31), True),
            (None, False),
        ):
            MonthlyPhysicianAvailabilityRule.objects.create(
                physician=faculty,
                weekday=1,
                ordinals=[1],
                periods=["AM"],
                availability_selection=FacultyAvailabilityTemplate.base_selection("open"),
                effective_end=effective_end,
                is_active=is_active,
            )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("physicians-directory"), {"view": "faculty"})

        # The batched directory query plan stays within this ceiling while allowing
        # harmless query consolidation; per-physician source lookups would exceed it.
        self.assertLessEqual(len(queries), 14)
        self.assertContains(response, "Faculty: 3 templates, 2 rules")

    def test_directory_empty_view_identifies_the_selected_current_role(self):
        Physician.objects.create(display_name="No current role")

        response = self.client.get(reverse("physicians-directory"), {"view": "faculty"})

        self.assertContains(response, "No physicians currently have a Faculty role.")
        self.assertContains(response, '<table id="physicians-faculty" class="data-table"')
        directory_table = response.context["directory_table"]
        self.assertIn("Physician", [column.label for column in directory_table.columns])
        self.assertIn("Capacity", [column.label for column in directory_table.columns])
        self.assertContains(
            response,
            f'<td class="data-table__empty" colspan="{len(directory_table.columns)}">',
        )
        assert_rendered_navigation_link(
            self,
            response,
            aria_label="Physician navigation",
            label="Faculty",
            url=reverse("physicians-directory") + "?view=faculty",
            current=True,
        )

    def test_directory_navigation_links_to_external_patient_care_in_role_filter_order(self):
        response = self.client.get(
            reverse("physicians-directory"), {"view": "external-patient-care"}
        )

        navigation_link = assert_rendered_navigation_link(
            self,
            response,
            aria_label="Physician navigation",
            label="External Patient Care",
            url=reverse("physicians-directory") + "?view=external-patient-care",
            current=True,
        )
        parser = _RenderedSurfaceParser()
        parser.feed(response.content.decode())
        navigation = next(
            element
            for element in parser.elements
            if element.tag == "nav"
            and element.attributes.get("aria-label") == "Physician navigation"
        )
        labels = [
            " ".join(link.text().split()) for link in navigation.descendants() if link.tag == "a"
        ]
        navigation_label = " ".join(navigation_link.text().split())
        self.assertLess(labels.index("Casual Rounders"), labels.index(navigation_label))
        self.assertLess(labels.index(navigation_label), labels.index("Inactive"))

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 18, tzinfo=UTC),
    )
    def test_directory_filters_external_patient_care_and_displays_it_as_an_additive_role(self, now):
        physician = Physician.objects.create(display_name="External patient care faculty")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.EXTERNAL_PATIENT_CARE,
            effective_start="2025-01-01",
        )

        response = self.client.get(
            reverse("physicians-directory"), {"view": "external-patient-care"}
        )

        self.assertContains(response, "External Patient Care")
        self.assertContains(response, physician.display_name)
        self.assertEqual(
            response.context["directory_table"].rows[0].cells[1].tags,
            ("Faculty", "External Patient Care"),
        )
        detail = self.client.get(
            reverse("physician-detail", args=(physician.pk,)), {"date": "2025-01-15"}
        )
        self.assertContains(detail, "Effective roles: External Patient Care, Faculty")

    def test_directory_all_groups_active_and_inactive_with_clear_empty_states(self):
        inactive = Physician.objects.create(display_name="Inactive")

        response = self.client.get(reverse("physicians-directory"))

        self.assertContains(response, '<h2 id="active-physicians-heading">Active</h2>')
        self.assertContains(response, '<h2 id="inactive-physicians-heading">Inactive</h2>')
        self.assertContains(response, "No physicians currently have an active role.")
        self.assertContains(
            response,
            f'<th scope="row" class="data-table-cell--link"><a href="/physicians/{inactive.pk}/">Inactive</a></th>',
        )
        self.assertContains(response, "No current role")
        self.assertContains(response, "No current source schedules")
        self.assertContains(response, 'class="table-scroll-frame data-table-scroll-frame"', count=2)
        self.assertNotContains(response, "physician-results-heading")
        self.assertNotContains(response, '<h1 class="workspace-heading">')

    def test_physicians_history_lists_all_retained_changes_in_reverse_chronological_order(self):
        first = Physician.objects.create(display_name="First physician")
        second = Physician.objects.create(display_name="Second physician")
        PhysicianChange.objects.create(
            physician=first,
            subject=PhysicianChange.Subject.IDENTITY,
            subject_id=first.pk,
            previous_value={"display_name": "First"},
            recorded_value={"display_name": first.display_name},
            reason="Initial spelling",
            changed_by=self.user,
        )
        PhysicianChange.objects.create(
            physician=second,
            subject=PhysicianChange.Subject.ROLE,
            subject_id=1,
            previous_value=None,
            recorded_value={"role": "faculty"},
            changed_by=self.user,
        )

        response = self.client.get(reverse("physicians-history"))

        self.assertEqual(
            [row.cells[0].value for row in response.context["physicians_history_table"].rows],
            [second.display_name, first.display_name],
        )
        self.assertContains(response, first.display_name)
        self.assertContains(response, second.display_name)
        self.assertContains(response, f'href="{reverse("physician-detail", args=(second.pk,))}"')
        self.assertContains(response, "<code>{&quot;role&quot;: &quot;faculty&quot;}</code>")
        self.assertContains(response, "Initial spelling")
        self.assertContains(response, "No note.")
        self.assertEqual(response.context["physicians_history_table"].rows[0].cells[4].kind, "code")
        self.assertEqual(response.context["physicians_history_table"].rows[0].cells[5].kind, "code")
        self.assertEqual(response.context["physicians_history_table"].rows[1].cells[4].kind, "code")
        self.assertEqual(response.context["physicians_history_table"].rows[1].cells[5].kind, "code")
        self.assertContains(response, 'class="table-scroll-frame data-table-scroll-frame"')
        self.assertContains(response, 'id="physicians-history" class="data-table"')

    def test_definition_history_keeps_an_unlinked_subject_in_the_shared_table(self):
        AcademicEventDefinitionChange.objects.create(
            definition_id_at_recording=987,
            action=AcademicEventDefinitionChange.Action.DEACTIVATED,
            previous_value={"base_name": "Long-retained definition"},
            recorded_value=None,
            changed_by=self.user,
        )

        response = self.client.get(reverse("academic-event-definition-history"))

        self.assertContains(response, "Definition #987")
        self.assertContains(
            response, "<code>{&quot;base_name&quot;: &quot;Long-retained definition&quot;}</code>"
        )
        self.assertContains(response, "<code>null</code>")
        self.assertContains(response, 'id="academic-event-definition-history" class="data-table"')

    def test_scheduler_can_create_revisit_and_correct_a_physician_with_history(self):
        response = self.client.post(
            reverse("physician-create"), {"display_name": "Jordan Lee", "reason": "Initial entry"}
        )
        physician = Physician.objects.get(display_name="Jordan Lee")
        self.assertRedirects(response, reverse("physician-detail", args=(physician.pk,)))
        self.assertContains(self.client.get(reverse("physicians-directory")), "Jordan Lee")

        response = self.client.post(
            reverse("physician-role-add", args=(physician.pk,)),
            {
                "role": EffectiveRole.Role.COMMUNITY_PRECEPTOR,
                "effective_start": "2025-01-01",
                "effective_end": "",
                "reason": "Community roster",
            },
        )
        self.assertRedirects(response, reverse("physician-detail", args=(physician.pk,)))
        detail = self.client.get(
            reverse("physician-detail", args=(physician.pk,)), {"date": "2025-01-01"}
        )
        self.assertContains(detail, "Effective community-preceptor role")
        self.assertContains(
            self.client.get(reverse("physician-history", args=(physician.pk,))), "Community roster"
        )
        self.assertEqual(PhysicianChange.objects.filter(physician=physician).count(), 2)

    def test_physician_detail_corrects_identity_inline_and_links_to_contextual_history(self):
        physician = Physician.objects.create(display_name="Jordan Lee")
        detail_url = reverse("physician-detail", args=(physician.pk,))

        response = self.client.get(detail_url)

        self.assertContains(response, "Identity")
        self.assertFalse(response.context["identity_form"].is_bound)
        self.assertContains(response, 'name="display_name" value="Jordan Lee"', html=False)
        self.assertNotContains(response, "This field is required.")
        # Compact, bounded, resizable notes are exercised on this actual page by
        # PhysicianDetailFormsBrowserTests; CSS layout primitives may change.
        self.assertContains(response, 'name="display_name"')
        self.assertContains(response, "Update identity")
        self.assertNotIn('<p class="eyebrow">Physician</p>', response.content.decode())
        self.assertContains(response, "View change history")
        self.assertNotContains(response, "Edit name")
        self.assertLess(response.content.index(b"Identity"), response.content.index(b"Roles"))
        self.assertLess(
            response.content.index(b"Roles"), response.content.index(b"Scheduling status")
        )
        with self.assertRaises(NoReverseMatch):
            reverse("physician-edit", args=(physician.pk,))

        response = self.client.post(
            detail_url,
            {
                "action": "identity",
                "display_name": "Jordan Morgan",
                "reason": "Corrected spelling",
            },
        )

        self.assertRedirects(response, detail_url)
        physician.refresh_from_db()
        self.assertEqual(physician.display_name, "Jordan Morgan")
        history = self.client.get(reverse("physician-history", args=(physician.pk,)))
        self.assertContains(history, "Corrected spelling")

        response = self.client.post(
            detail_url,
            {"action": "identity", "display_name": "", "reason": "Keep note"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["identity_form"].is_bound)
        self.assertContains(response, "This field is required.")
        assert_detail_note_value(self, response, "identity", "Keep note")
        physician.refresh_from_db()
        self.assertEqual(physician.display_name, "Jordan Morgan")

    def test_physician_detail_account_action_leaves_identity_form_unbound(self):
        physician = Physician.objects.create(display_name="Account physician")
        detail_url = reverse("physician-detail", args=(physician.pk,))

        response = self.client.post(
            detail_url,
            {"action": "account", "user": "missing", "reason": "Check account"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["account_form"].is_bound)
        self.assertTrue(response.context["account_form"].errors)
        self.assertFalse(response.context["identity_form"].is_bound)
        assert_detail_note_value(self, response, "account", "Check account")
        self.assertContains(response, 'name="display_name" value="Account physician"', html=False)
        self.assertNotContains(response, "This field is required.")

    def test_physician_detail_rejects_missing_or_unknown_action_without_mutating_identity(self):
        physician = Physician.objects.create(display_name="Original")
        detail_url = reverse("physician-detail", args=(physician.pk,))

        missing_action = self.client.post(detail_url, {"display_name": "Changed"})
        unknown_action = self.client.post(
            detail_url, {"action": "unknown", "display_name": "Changed"}
        )

        self.assertEqual(missing_action.status_code, 400)
        self.assertEqual(unknown_action.status_code, 400)
        physician.refresh_from_db()
        self.assertEqual(physician.display_name, "Original")

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 18, tzinfo=UTC),
    )
    def test_physician_detail_roles_attributes_and_faculty_schedule_visibility(self, _now):
        community = Physician.objects.create(display_name="Community physician")
        EffectiveRole.objects.create(
            physician=community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start="2025-01-01",
            faculty_equivalent=True,
        )
        faculty = Physician.objects.create(display_name="Past faculty")
        EffectiveRole.objects.create(
            physician=faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2024-01-01",
            effective_end="2024-12-31",
            faculty_leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
        )
        FacultyAvailabilityTemplate.objects.create(
            physician=faculty,
            slots={
                slot: FacultyAvailabilityTemplate.base_selection(
                    FacultyAvailabilityTemplate.AvailabilityState.OPEN
                )
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
        )
        future_faculty = Physician.objects.create(display_name="Future faculty")
        EffectiveRole.objects.create(
            physician=future_faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2026-01-01",
        )

        community_response = self.client.get(reverse("physician-detail", args=(community.pk,)))
        faculty_response = self.client.get(reverse("physician-detail", args=(faculty.pk,)))
        future_response = self.client.get(reverse("physician-detail", args=(future_faculty.pk,)))

        self.assertContains(community_response, "Faculty-equivalent: Yes")
        self.assertNotContains(community_response, "Faculty Schedule")
        self.assertContains(faculty_response, "Faculty leadership: Program Director (PD)")
        self.assertContains(faculty_response, "Jan. 1, 2024 through Dec. 31, 2024")
        self.assertContains(faculty_response, "Faculty Schedule")
        self.assertNotContains(faculty_response, "Add weekly template")
        self.assertContains(
            faculty_response, 'id="faculty-availability-templates" class="data-table"'
        )
        self.assertContains(future_response, "Faculty Schedule")
        self.assertContains(future_response, "Add weekly template")

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 18, tzinfo=UTC),
    )
    def test_future_faculty_detail_opens_and_saves_a_future_weekly_template(self, _now):
        physician = Physician.objects.create(display_name="Future Faculty setup")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2026, 1, 1),
        )
        detail_url = reverse("physician-detail", args=(physician.pk,))

        detail = self.client.get(detail_url)

        faculty_schedule = re.search(
            r'<section\b[^>]*\baria-labelledby="faculty-schedule-heading"[^>]*>'
            r"(?P<content>.*?)</section>",
            detail.content.decode(),
            flags=re.DOTALL,
        )
        self.assertIsNotNone(faculty_schedule)
        weekly_template_action = re.search(
            r'<a\b[^>]*\bhref="(?P<href>[^"]+)"[^>]*>\s*Add weekly template\s*</a>',
            faculty_schedule["content"],
            flags=re.DOTALL,
        )
        self.assertIsNotNone(weekly_template_action)
        action_url = unescape(weekly_template_action["href"])
        action = resolve(action_url)
        self.assertEqual(action.url_name, "physician-faculty-availability-add")
        self.assertEqual(action.kwargs["physician_id"], physician.pk)

        form = self.client.get(action_url)

        self.assertContains(form, 'name="effective_start"')
        slots = {slot: "base:open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES}
        slots["friday_pm"] = "base:backup_only"
        response = self.client.post(
            action_url,
            {
                "effective_start": "2026-01-01",
                "reason": "Prepare future Faculty schedule",
                **slots,
            },
        )

        self.assertRedirects(response, detail_url)
        template = FacultyAvailabilityTemplate.objects.get(physician=physician)
        self.assertEqual(template.effective_start, date(2026, 1, 1))
        self.assertEqual(template.slots["monday_am"], {"base_state": "open"})
        self.assertEqual(template.slots["friday_pm"], {"base_state": "backup_only"})
        change = PhysicianChange.objects.get(
            physician=physician,
            subject=PhysicianChange.Subject.FACULTY_AVAILABILITY_TEMPLATE,
            subject_id=template.pk,
        )
        self.assertIsNone(change.previous_value)
        self.assertEqual(change.recorded_value["effective_start"], "2026-01-01")
        self.assertEqual(change.recorded_value["slots"]["friday_pm"], {"base_state": "backup_only"})
        self.assertEqual(change.changed_by, self.user)
        self.assertEqual(change.reason, "Prepare future Faculty schedule")
        current_status = physicians_status_on(physician, date(2025, 1, 1))
        self.assertFalse(current_status.ordinarily_eligible_to_precept)
        self.assertFalse(current_status.faculty_equivalent)
        self.assertEqual(
            faculty_precepting_capacity_for_week(physician, date(2025, 1, 6)), Decimal("0")
        )

    def test_physician_creation_can_record_an_initial_role_without_requiring_a_note(self):
        response = self.client.post(
            reverse("physician-create"),
            {
                "display_name": "Initial faculty",
                "role": EffectiveRole.Role.FACULTY,
                "effective_start": "2025-01-01",
                "effective_end": "2025-01-31",
            },
        )

        physician = Physician.objects.get(display_name="Initial faculty")
        self.assertRedirects(response, reverse("physician-detail", args=(physician.pk,)))
        self.assertEqual(physician.roles.get().role, EffectiveRole.Role.FACULTY)
        self.assertEqual(PhysicianChange.objects.filter(physician=physician).count(), 2)

    @patch("schedules.services.operational_attention.timezone.now")
    def test_creation_defaults_optional_role_start_to_local_today_and_ignores_it_without_role(
        self, now
    ):
        now.return_value = datetime(2025, 1, 1, 2, tzinfo=UTC)

        response = self.client.get(reverse("physician-create"))
        self.assertContains(response, 'value="2024-12-31"')
        response = self.client.post(
            reverse("physician-create"),
            {"display_name": "No role", "effective_start": "2024-12-31"},
        )

        physician = Physician.objects.get(display_name="No role")
        self.assertRedirects(response, reverse("physician-detail", args=(physician.pk,)))
        self.assertFalse(physician.roles.exists())

    def test_physicians_forms_group_fields_and_retain_invalid_role_values(self):
        create = self.client.get(reverse("physician-create"))

        self.assertContains(create, "Identity")
        self.assertContains(create, "Role start")
        self.assertContains(create, "Faculty-equivalent")
        self.assertContains(create, "Faculty leadership")
        self.assertContains(create, "Program Director (PD)")
        self.assertContains(create, "Associate Program Director (APD)")
        self.assertContains(create, "data-role-attributes")
        self.assertContains(create, 'src="/static/schedules/role-attributes.js"')
        self.assertContains(create, "Actions")
        self.assertContains(create, "Return to Physicians")
        self.assertContains(create, '<textarea name="reason"')

        physician = Physician.objects.create(display_name="Detail physician")

        role = EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        template = FacultyAvailabilityTemplate.objects.create(
            physician=physician,
            slots={
                slot: FacultyAvailabilityTemplate.base_selection(
                    FacultyAvailabilityTemplate.AvailabilityState.OPEN
                )
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
        )
        detail = self.client.get(reverse("physician-detail", args=(physician.pk,)))
        self.assertContains(detail, "Update identity")
        self.assertNotContains(detail, "Edit name")
        self.assertContains(
            detail, f'href="/physicians/{physician.pk}/roles/{role.pk}/correct/">Edit</a>'
        )
        self.assertContains(
            detail,
            f'href="/physicians/{physician.pk}/faculty-schedule/{template.pk}/correct/">Edit template</a>',
        )
        role_edit = self.client.get(reverse("physician-role-correct", args=(physician.pk, role.pk)))
        self.assertContains(role_edit, "Edit Faculty")
        self.assertContains(role_edit, 'src="/static/schedules/role-attributes.js"')
        self.assertNotIn("fact-section-heading", role_edit.content.decode())
        self.assertContains(
            self.client.get(reverse("physician-role-add", args=(physician.pk,))),
            'src="/static/schedules/role-attributes.js"',
        )
        self.assertContains(
            self.client.get(
                reverse("physician-faculty-availability-correct", args=(physician.pk, template.pk))
            ),
            "Edit Faculty Schedule template",
        )

        response = self.client.post(
            reverse("physician-role-add", args=(physician.pk,)),
            {
                "role": EffectiveRole.Role.FACULTY,
                "effective_start": "2025-02-10",
                "effective_end": "2025-02-01",
                "reason": "Keep this note",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("fact-section-heading", response.content.decode())
        self.assertContains(response, '<label for="id_role">Role</label>')
        self.assertContains(response, "Effective range")
        self.assertContains(response, "Leave the end date blank to keep this fact ongoing.")
        self.assertContains(response, "End date cannot be before start date.")
        parser = _RenderedSurfaceParser()
        parser.feed(response.content.decode())
        for name, expected in (
            ("effective_start", "2025-02-10"),
            ("effective_end", "2025-02-01"),
            ("reason", "Keep this note"),
        ):
            controls = [
                element
                for element in parser.elements
                if element.attributes.get("name") == name and element.tag in ("input", "textarea")
            ]
            self.assertEqual(len(controls), 1)
            control = controls[0]
            self.assertEqual(
                control.text().removeprefix("\n")
                if control.tag == "textarea"
                else control.attributes.get("value"),
                expected,
            )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 18, tzinfo=UTC),
    )
    def test_role_forms_show_retained_role_context_only_when_present_and_mark_current_edit(
        self, _now
    ):
        physician = Physician.objects.create(display_name="Role context physician")

        empty_add = self.client.get(reverse("physician-role-add", args=(physician.pk,)))

        self.assertNotContains(empty_add, "Existing roles")

        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start="2024-01-01",
            effective_end="2024-12-31",
            faculty_equivalent=True,
        )
        faculty_role = EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
            faculty_leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
        )
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start="2026-01-01",
        )

        add = self.client.get(reverse("physician-role-add", args=(physician.pk,)))
        edit = self.client.get(
            reverse("physician-role-correct", args=(physician.pk, faculty_role.pk))
        )

        self.assertContains(add, "Existing roles")
        self.assertContains(add, 'class="table-scroll-frame data-table-scroll-frame"')
        self.assertContains(add, 'id="existing-roles" class="data-table"')
        self.assertEqual(
            list(add.context["form"].fields["role"].choices),
            list(EffectiveRole.Role.choices),
        )
        self.assertContains(
            add, '<th scope="row" class="data-table-cell--text">Casual Rounder</th>'
        )
        self.assertContains(add, "Community Preceptor")
        self.assertContains(add, "Faculty-equivalent: Yes")
        self.assertContains(add, "Jan. 1, 2024 through Dec. 31, 2024")
        self.assertContains(add, "Faculty")
        self.assertContains(add, "Faculty leadership: Program Director (PD)")
        self.assertContains(add, "Jan. 1, 2025 through ongoing")
        self.assertNotContains(add, "Editing this role")
        self.assertContains(edit, "Editing this role")
        self.assertContains(edit, 'class="data-table-row--attention"')
        self.assertContains(edit, "Retained role")

    def test_anonymous_physicians_requests_do_not_create_or_change_records(self):
        self.client.logout()
        create_response = self.client.post(
            reverse("physician-create"),
            {"display_name": "Unauthorized", "reason": "Should not save"},
        )
        self.assertEqual(create_response.status_code, 302)
        self.assertEqual(Physician.objects.count(), 0)

        physician = Physician.objects.create(display_name="Existing")
        response = self.client.post(
            reverse("physician-role-add", args=(physician.pk,)),
            {
                "role": EffectiveRole.Role.FACULTY,
                "effective_start": "2025-01-01",
                "reason": "Should not save",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(physician.roles.count(), 0)

        history_response = self.client.get(reverse("physicians-history"))
        self.assertEqual(history_response.status_code, 302)

    def test_role_conflict_is_actionable_and_does_not_write_history(self):
        physician = Physician.objects.create(display_name="Role conflict")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start="2025-01-01",
            effective_end="2025-01-31",
        )
        history_count = PhysicianChange.objects.count()

        response = self.client.post(
            reverse("physician-role-add", args=(physician.pk,)),
            {
                "role": EffectiveRole.Role.FACULTY,
                "effective_start": "2025-01-31",
                "effective_end": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "community-preceptor")
        self.assertContains(response, "2025-01-01")
        content = response.content.decode()
        notes_start = content.index('id="notes-actions-section-heading"')
        self.assertLess(
            content.index('class="surface-body form-content-stack"', notes_start),
            content.index('class="form-action-area"', notes_start),
        )
        self.assertEqual(physician.roles.count(), 1)
        self.assertEqual(PhysicianChange.objects.count(), history_count)

    def test_faculty_schedule_form_submits_all_states_rerenders_invalid_bounds_and_rejects_anonymous_changes(
        self,
    ):
        physician = Physician.objects.create(display_name="Faculty")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        add_url = reverse("physician-faculty-availability-add", args=(physician.pk,))
        response = self.client.get(add_url)
        self.assertContains(response, "Monday")
        self.assertContains(response, "Friday")
        self.assertContains(response, "<legend>AM</legend>", count=5)
        self.assertContains(response, "<legend>PM</legend>", count=5)
        self.assertContains(response, "Weekly template")
        self.assertContains(response, "Patient care")
        self.assertContains(response, "Active schedule options")
        self.assertNotContains(response, "Fixed states")
        self.assertEqual(response.content.count(b'type="radio"'), 40)
        content = response.content.decode()
        notes_start = content.index('id="availability-notes-heading"')
        self.assertLess(
            content.index('class="surface-body form-content-stack"', notes_start),
            content.index('class="form-action-area"', notes_start),
        )

        data = {
            "effective_start": "",
            "effective_end": "2025-01-08",
            "reason": "Synthetic test template",
            **{
                slot: f"base:{FacultyAvailabilityTemplate.AvailabilityState.values[index % 4]}"
                for index, (slot, _) in enumerate(FacultyAvailabilityTemplate.SLOT_CHOICES)
            },
        }
        response = self.client.post(add_url, data)
        self.assertRedirects(response, reverse("physician-detail", args=(physician.pk,)))
        template = FacultyAvailabilityTemplate.objects.get(physician=physician)
        self.assertEqual(
            template.slots,
            {
                slot: FacultyAvailabilityTemplate.base_selection(data[slot].removeprefix("base:"))
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
        )

        invalid_data = {**data, "effective_start": "2025-02-10", "effective_end": "2025-02-01"}
        response = self.client.post(add_url, invalid_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "End date cannot be before start date.")
        self.assertContains(response, 'name="effective_start" value="2025-02-10"')
        self.assertContains(response, 'name="effective_end" value="2025-02-01"')
        for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES:
            self.assertRegex(
                response.content.decode(),
                rf'<input[^>]*name="{slot}"[^>]*value="{data[slot]}"[^>]*checked',
            )

        self.client.logout()
        response = self.client.post(add_url, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(FacultyAvailabilityTemplate.objects.filter(physician=physician).count(), 1)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 2, tzinfo=UTC),
    )
    def test_faculty_schedule_add_defaults_start_to_local_today_and_all_slots_to_available(
        self, now
    ):
        physician = Physician.objects.create(display_name="Default availability faculty")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        add_url = reverse("physician-faculty-availability-add", args=(physician.pk,))

        history_count = PhysicianChange.objects.count()
        response = self.client.get(add_url)
        self.assertContains(response, 'name="effective_start" value="2024-12-31"')
        self.assertEqual(FacultyAvailabilityTemplate.objects.filter(physician=physician).count(), 0)
        self.assertEqual(PhysicianChange.objects.count(), history_count)
        self.assertEqual(len(re.findall(rb'value="base:open"[^>]*checked', response.content)), 10)

        response = self.client.post(
            add_url,
            {
                "effective_start": "2025-02-10",
                **{slot: "base:open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            },
        )

        self.assertRedirects(response, reverse("physician-detail", args=(physician.pk,)))
        template = FacultyAvailabilityTemplate.objects.get(physician=physician)
        self.assertEqual(
            template.slots,
            {
                slot: FacultyAvailabilityTemplate.base_selection(
                    FacultyAvailabilityTemplate.AvailabilityState.OPEN
                )
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
        )

        response = self.client.get(
            reverse("physician-faculty-availability-correct", args=(physician.pk, template.pk))
        )
        self.assertContains(response, 'name="effective_start" value="2025-02-10"')
        self.assertEqual(len(re.findall(rb'value="base:open"[^>]*checked', response.content)), 10)

    def test_start_later_template_form_transitions_the_latest_template_and_keeps_float_contract(
        self,
    ):
        physician = Physician.objects.create(display_name="Faculty transition")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        preceding = create_faculty_availability_template(
            physician=physician,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.user,
        )
        transition_url = reverse(
            "physician-faculty-availability-start-later", args=(physician.pk, preceding.pk)
        )

        detail = self.client.get(reverse("physician-detail", args=(physician.pk,)))
        self.assertContains(detail, "Add weekly template")
        self.assertContains(detail, "Start later template")
        form = self.client.get(transition_url)
        self.assertContains(form, "Start later Faculty Schedule template")
        self.assertContains(form, "Start date")
        self.assertNotContains(form, 'name="effective_end"')
        self.assertEqual(form.content.count(b'type="radio"'), 40)

        response = self.client.post(
            transition_url,
            {
                "effective_start": "2025-02-01",
                "reason": "February transition",
                **{
                    slot: "base:backup_only" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
                },
            },
        )

        self.assertRedirects(response, reverse("physician-detail", args=(physician.pk,)))
        preceding.refresh_from_db()
        later = FacultyAvailabilityTemplate.objects.get(
            physician=physician, effective_start=date(2025, 2, 1)
        )
        self.assertEqual(preceding.effective_end, date(2025, 1, 31))
        self.assertIsNone(later.effective_end)
        self.assertEqual(later.slots["monday_am"], {"base_state": "backup_only"})

        community_float = Physician.objects.create(display_name="Float transition")
        for role in (
            EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            EffectiveRole.Role.FLOAT_PRECEPTOR,
        ):
            add_role(
                physician=community_float,
                role=role,
                effective_start=date(2025, 1, 1),
                effective_end=None,
                scheduler=self.user,
            )
        ensure_system_schedule_options()
        float_template = create_faculty_availability_template(
            physician=community_float,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            slots={
                slot: FacultyAvailabilityTemplate.base_selection("off")
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
            scheduler=self.user,
        )

        float_form = self.client.get(
            reverse(
                "physician-faculty-availability-start-later",
                args=(community_float.pk, float_template.pk),
            )
        )
        self.assertContains(
            float_form,
            "Off does not change Community Preceptor recurrence commitments.",
        )
        self.assertContains(float_form, "Admin - Float")
        self.assertEqual(float_form.content.count(b'type="radio"'), 20)
        self.assertEqual(len(re.findall(rb'value="base:off"[^>]*checked', float_form.content)), 10)

    def test_weekend_physician_pages_preserve_status_and_do_not_create_schedule_records(self):
        template_physician = Physician.objects.create(display_name="Template faculty")
        no_template_physician = Physician.objects.create(display_name="No template faculty")
        for physician in (template_physician, no_template_physician):
            EffectiveRole.objects.create(
                physician=physician,
                role=EffectiveRole.Role.FACULTY,
                effective_start="2025-01-01",
            )
        FacultyAvailabilityTemplate.objects.create(
            physician=template_physician,
            effective_start=None,
            effective_end=None,
            slots={
                slot: FacultyAvailabilityTemplate.base_selection(
                    FacultyAvailabilityTemplate.AvailabilityState.OPEN
                )
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
        )
        before = (
            SchedulePeriod.objects.count(),
            PreceptorSession.objects.count(),
            PreceptorAssignment.objects.count(),
            PhysicianChange.objects.count(),
        )

        for on_date, physician in (
            ("2025-01-04", template_physician),
            ("2025-01-05", no_template_physician),
        ):
            with self.subTest(on_date=on_date, physician=physician):
                response = self.client.get(
                    reverse("physician-detail", args=(physician.pk,)), {"date": on_date}
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Not applicable — no weekend preceptor session")
                self.assertContains(response, "Effective Faculty role")
                self.assertContains(response, "Faculty-equivalent")

        self.assertEqual(
            (
                SchedulePeriod.objects.count(),
                PreceptorSession.objects.count(),
                PreceptorAssignment.objects.count(),
                PhysicianChange.objects.count(),
            ),
            before,
        )

    def test_default_physician_date_can_be_a_local_sunday(self):
        physician = Physician.objects.create(display_name="Weekend default")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        local_sunday = datetime(2025, 1, 5, 18, tzinfo=UTC)

        with patch(
            "schedules.services.operational_attention.timezone.now", return_value=local_sunday
        ):
            response = self.client.get(reverse("physician-detail", args=(physician.pk,)))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="2025-01-05"')
        self.assertContains(response, "Not applicable — no weekend preceptor session")

    def test_physician_effective_date_inspector_uses_the_compact_filter_treatment(self):
        physician = Physician.objects.create(display_name="Effective-date inspector")

        response = self.client.get(
            reverse("physician-detail", args=(physician.pk,)), {"date": "2025-02-03"}
        )

        self.assertContains(response, 'class="compact-filter-form"')
        self.assertContains(response, '<div class="compact-control-field"><label for="date">')
        self.assertContains(
            response, '<button type="submit" data-period-picker-submit>Check date</button>'
        )
        self.assertContains(response, 'value="2025-02-03"')
        self.assertContains(response, "data-period-picker-form")
        self.assertContains(response, "data-period-picker-input")
        self.assertContains(response, 'src="/static/schedules/period-picker.js"')

    def test_physician_detail_uses_shared_effective_results_for_am_and_pm_explanations(self):
        physician = Physician.objects.create(display_name="Effective availability")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        template = FacultyAvailabilityTemplate.objects.create(
            physician=physician,
            effective_start=None,
            effective_end=None,
            slots={
                slot: FacultyAvailabilityTemplate.base_selection(
                    FacultyAvailabilityTemplate.AvailabilityState.OPEN
                )
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
        )
        vacation = Vacation.objects.create(
            physician=physician,
            start_date="2025-02-03",
            end_date="2025-02-03",
            period_of_day=Vacation.PeriodOfDay.AM,
        )

        response = self.client.get(
            reverse("physician-detail", args=(physician.pk,)), {"date": "2025-02-03"}
        )

        self.assertContains(response, "AM effective availability")
        self.assertContains(response, "PM effective availability")
        self.assertContains(response, "Final availability", count=2)
        self.assertContains(response, "Not available")
        self.assertContains(response, "Available")
        self.assertContains(response, f"Faculty Schedule template {template.pk}")
        self.assertContains(response, f"Vacation {vacation.pk}")


class ManualDraftAssignmentViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.user.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.client.force_login(self.user)
        self.physician = Physician.objects.create(display_name="Faculty")
        EffectiveRole.objects.create(
            physician=self.physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        self.session = open_monthly_draft(date(2025, 2, 1)).sessions.get(
            date="2025-02-03", period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        self.url = reverse("session-assignment-edit", args=(self.session.pk, "primary-1"))

    def test_month_origin_survives_validation_history_and_focus_does_not_restrict_assignment(self):
        from schedules.physician_scope import SESSION_KEY

        # August 31 is an actual adjacent-month slot on September's calendar.
        session = open_monthly_draft(date(2026, 8, 1)).sessions.get(
            date=date(2026, 8, 31), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        assignment = assign_preceptor(
            session=session,
            physician=self.physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.user,
            reason="Original adjacent-month assignment",
        )
        focused = Physician.objects.create(display_name="Focused physician")
        editor = reverse("session-assignment-edit", args=(session.pk, "primary-1"))
        editor += "?month=2026-09"
        origin = reverse("monthly-workspace") + "?month=2026-09"
        calendar = self.client.get(origin)
        parser = _RenderedSurfaceParser()
        parser.feed(calendar.content.decode())
        slot = next(
            element
            for element in parser.elements
            if element.tag == "a" and element.attributes.get("href") == editor
        )
        self.assertIn(self.physician.display_name, slot.text())
        self.assertEqual(calendar.context["selected_month"], date(2026, 9, 1))

        browser_session = self.client.session
        browser_session[SESSION_KEY] = [focused.pk]
        browser_session.save()
        opened = self.client.get(slot.attributes["href"])
        self.assertEqual(opened.context["assignment"].pk, assignment.pk)
        self.assertEqual(opened.context["session"].date, date(2026, 8, 31))
        parser = _RenderedSurfaceParser()
        parser.feed(opened.content.decode())
        cancel = next(
            element
            for element in parser.elements
            if element.tag == "a"
            and "form-return-link" in element.attributes.get("class", "").split()
        )
        self.assertEqual(cancel.attributes["href"], origin)
        returned = self.client.get(cancel.attributes["href"])
        self.assertEqual(returned.context["selected_month"], date(2026, 9, 1))
        self.assertEqual(self.client.session[SESSION_KEY], [focused.pk])

        invalid = self.client.post(editor, {"physician": "invalid"})
        self.assertTrue(invalid.context["form"].errors)
        self.assertContains(invalid, f'action="{editor}"')
        self.assertEqual(invalid.context["return_url"], origin)
        self.assertEqual(self.client.session[SESSION_KEY], [focused.pk])
        saved = self.client.post(editor, {"physician": self.physician.pk, "is_locked": "on"})
        self.assertRedirects(saved, origin, fetch_redirect_response=False)
        assignment.refresh_from_db()
        self.assertEqual(assignment.physician, self.physician)
        self.assertTrue(assignment.is_locked)
        self.assertEqual(self.client.session[SESSION_KEY], [focused.pk])
        history_url = reverse("session-assignment-history", args=(session.pk,))
        self.assertContains(self.client.get(editor), f'href="{history_url}?month=2026-09"')
        history = self.client.get(history_url + "?month=2026-09")
        self.assertEqual(history.context["return_url"], origin)
        self.assertContains(history, "Original adjacent-month assignment")
        remove_url = reverse("session-assignment-remove", args=(session.pk, "primary-1"))
        remove_url += "?month=2026-09"
        self.assertContains(self.client.get(editor), f'action="{remove_url}"')
        invalid_removal = self.client.post(remove_url, {"reason": "invalid\x00note"})
        self.assertTrue(invalid_removal.context["remove_form"].errors)
        self.assertContains(invalid_removal, f'action="{editor}"')
        self.assertContains(invalid_removal, f'action="{remove_url}"')
        self.assertEqual(invalid_removal.context["return_url"], origin)
        self.assertEqual(self.client.session[SESSION_KEY], [focused.pk])
        self.assertTrue(PreceptorAssignment.objects.filter(pk=assignment.pk).exists())
        removed = self.client.post(remove_url, {"reason": "Remove from September"})
        self.assertRedirects(removed, origin, fetch_redirect_response=False)
        self.assertFalse(PreceptorAssignment.objects.filter(pk=assignment.pk).exists())
        self.assertEqual(self.client.session[SESSION_KEY], [focused.pk])

    def test_month_hides_history_while_assignment_form_history_remains_accessible(self):
        workspace = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})
        assignment_form = self.client.get(self.url)
        form_history_url = re.search(
            r'<a href="([^"]+)">View assignment history</a>',
            assignment_form.content.decode(),
        ).group(1)

        self.assertNotContains(workspace, "Assignment history")
        response = self.client.get(form_history_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Assignment history")

    def test_scheduler_can_edit_and_lock_visible_assignment_while_anonymous_post_has_no_effect(
        self,
    ):
        response = self.client.post(
            self.url,
            {"physician": self.physician.pk, "is_locked": "on", "reason": "Manual selection"},
        )

        self.assertRedirects(
            response, reverse("monthly-workspace") + "?month=2025-02", fetch_redirect_response=False
        )
        assignment = PreceptorAssignment.objects.get(session=self.session)
        self.assertTrue(assignment.is_locked)
        self.assertEqual(PreceptorAssignmentChange.objects.filter(session=self.session).count(), 2)
        workspace = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})
        self.assertContains(workspace, "Faculty")
        self.assertContains(workspace, 'role="img" aria-label="Locked"')
        self.assertNotContains(workspace, "Assignment history")

        self.client.logout()
        response = self.client.post(
            self.url,
            {"physician": self.physician.pk, "reason": "Unauthorized replacement"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            PreceptorAssignment.objects.get(pk=assignment.pk).physician, self.physician
        )
        self.assertEqual(PreceptorAssignmentChange.objects.filter(session=self.session).count(), 2)

    def test_assignment_editor_separates_choice_settings_and_removal(self):
        response = self.client.get(self.url)

        assert_rendered_heading(self, response, "Preceptor 1 assignment")
        self.assertContains(
            response,
            '<p class="form-page-helper">Monday, February 3, 2025 · AM</p>',
        )
        self.assertNotContains(response, "Primary and secondary describe assignment responsibility")
        self.assertContains(response, 'class="assignment-form form-presentation"')
        self.assertContains(response, "Choose an alternative")
        self.assertContains(response, "Lock and note")
        self.assertContains(response, 'name="physician"')
        self.assertContains(response, 'name="is_locked"')
        self.assertContains(response, '<textarea name="reason"')
        content = response.content.decode()
        settings_start = content.index('id="assignment-settings-heading"')
        self.assertLess(
            content.index('class="surface-body form-content-stack"', settings_start),
            content.index('class="form-action-area"', settings_start),
        )

        response = self.client.post(
            self.url,
            {"physician": self.physician.pk, "is_locked": "on", "reason": "Keep coverage"},
        )
        self.assertRedirects(
            response, reverse("monthly-workspace") + "?month=2025-02", fetch_redirect_response=False
        )
        assignment = PreceptorAssignment.objects.get(session=self.session)

        response = self.client.get(self.url)
        self.assertContains(response, "Current assignment")
        self.assertContains(response, "Assignment Options")
        self.assertContains(response, "Faculty")
        self.assertContains(response, "checked")
        assert_removal_section(
            self,
            response,
            "assignment-removal-heading",
            reverse("session-assignment-remove", args=(self.session.pk, "primary-1")),
        )
        self.assertContains(response, "This retains the prior assignment and optional note")

        response = self.client.post(
            reverse("session-assignment-remove", args=(self.session.pk, "primary-1")),
            {"reason": "Coverage no longer needed"},
        )
        self.assertRedirects(
            response, reverse("monthly-workspace") + "?month=2025-02", fetch_redirect_response=False
        )
        self.assertFalse(PreceptorAssignment.objects.filter(pk=assignment.pk).exists())

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
    )
    def test_assignment_editor_shows_current_assignment_and_session_findings(self, _now):
        create_faculty_availability_template(
            physician=self.physician,
            effective_start=None,
            effective_end=None,
            slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.user,
        )
        assign_preceptor(
            session=self.session,
            physician=self.physician,
            responsibility="primary",
            primary_position=1,
            scheduler=self.user,
        )
        create_vacation(
            physician=self.physician,
            start_date=self.session.date,
            end_date=self.session.date,
            period_of_day="AM",
            scheduler=self.user,
        )

        response = self.client.get(self.url)
        content = response.content.decode()

        self.assertContains(response, "Current findings")
        self.assertContains(response, "Assigned physician is not available for this session.")
        self.assertContains(response, "Hard")
        self.assertContains(response, "Related Physicians: <a", html=False)
        self.assertContains(response, self.physician.display_name)
        self.assertIn(
            '<header class="surface-header"><h2 id="current-findings-heading">Current findings</h2></header>',
            content,
        )
        self.assertNotIn("assignment-findings-disclosure", content)
        self.assertContains(response, "Preceptor 2 is unfilled.")

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 3, 18, tzinfo=UTC),
    )
    @patch(
        "schedules.views.preceptors.draft_findings_for_week",
        return_value=DraftFindingResult(()),
    )
    def test_assignment_editor_omits_current_findings_when_clean(self, _week_findings, _now):
        other = Physician.objects.create(display_name="Other Faculty")
        for physician in (self.physician, other):
            if physician == other:
                EffectiveRole.objects.create(
                    physician=physician,
                    role=EffectiveRole.Role.FACULTY,
                    effective_start="2025-01-01",
                )
            create_faculty_availability_template(
                physician=physician,
                effective_start=None,
                effective_end=None,
                slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
                scheduler=self.user,
            )
        assign_preceptor(
            session=self.session,
            physician=self.physician,
            responsibility="primary",
            primary_position=1,
            scheduler=self.user,
        )
        assign_preceptor(
            session=self.session,
            physician=other,
            responsibility="primary",
            primary_position=2,
            scheduler=self.user,
        )

        response = self.client.get(self.url)

        self.assertNotContains(response, "Current findings")

    def test_scheduler_can_lock_and_unlock_assignment_retained_after_role_correction(self):
        assignment = assign_preceptor(
            session=self.session,
            physician=self.physician,
            responsibility="primary",
            primary_position=1,
            scheduler=self.user,
            reason="Initial selection",
        )
        correct_role(
            effective_role=self.physician.roles.get(role=EffectiveRole.Role.FACULTY),
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.user,
            reason="Corrected historical roster",
        )

        response = self.client.post(
            self.url,
            {"physician": self.physician.pk, "is_locked": "on", "reason": "Keep this assignment"},
        )

        self.assertRedirects(
            response, reverse("monthly-workspace") + "?month=2025-02", fetch_redirect_response=False
        )
        assignment.refresh_from_db()
        self.assertTrue(assignment.is_locked)

        response = self.client.post(
            self.url,
            {"physician": self.physician.pk, "reason": "Unlock this assignment"},
        )

        self.assertRedirects(
            response, reverse("monthly-workspace") + "?month=2025-02", fetch_redirect_response=False
        )
        assignment.refresh_from_db()
        self.assertFalse(assignment.is_locked)
        self.assertEqual(assignment.physician, self.physician)
        self.assertEqual(
            list(
                PreceptorAssignmentChange.objects.filter(session=self.session)
                .order_by("pk")
                .values_list("action", flat=True)
            ),
            ["assigned", "lock_changed", "lock_changed"],
        )

    def test_month_hides_assignment_progress_as_primary_assignments_change(self):
        other = Physician.objects.create(display_name="Other faculty")
        replacement = Physician.objects.create(display_name="Replacement faculty")
        for physician in (other, replacement):
            EffectiveRole.objects.create(
                physician=physician,
                role=EffectiveRole.Role.FACULTY,
                effective_start="2025-01-01",
            )
        primary_two_url = reverse("session-assignment-edit", args=(self.session.pk, "primary-2"))

        self.client.post(self.url, {"physician": self.physician.pk, "reason": "First primary"})
        workspace = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})
        assert_month_omits_page_summaries(self, workspace)

        self.client.post(primary_two_url, {"physician": other.pk, "reason": "Second primary"})
        workspace = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})
        assert_month_omits_page_summaries(self, workspace)

        self.client.post(primary_two_url, {"physician": replacement.pk, "reason": "Replacement"})
        workspace = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})
        assert_month_omits_page_summaries(self, workspace)

        assignment = PreceptorAssignment.objects.get(
            session=self.session,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )
        self.client.post(
            reverse("session-assignment-remove", args=(self.session.pk, "primary-1")),
            {"reason": "Remove first primary"},
        )
        workspace = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})
        assert_month_omits_page_summaries(self, workspace)
        self.assertFalse(PreceptorAssignment.objects.filter(pk=assignment.pk).exists())

    def test_month_hides_assignment_progress_for_secondary_only_draft(self):
        secondary_url = reverse("session-assignment-edit", args=(self.session.pk, "secondary"))

        self.client.post(
            secondary_url, {"physician": self.physician.pk, "reason": "Helpful secondary"}
        )

        workspace = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})
        assert_month_omits_page_summaries(self, workspace)

    def test_month_hides_assignment_progress_when_every_session_has_two_primaries(self):
        other = Physician.objects.create(display_name="Other faculty")
        EffectiveRole.objects.create(
            physician=other,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        PreceptorAssignment.objects.bulk_create(
            assignment
            for session in self.session.period.sessions.all()
            for assignment in (
                PreceptorAssignment(
                    session=session,
                    physician=self.physician,
                    responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                    primary_position=1,
                ),
                PreceptorAssignment(
                    session=session,
                    physician=other,
                    responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                    primary_position=2,
                ),
            )
        )

        workspace = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})

        assert_month_omits_page_summaries(self, workspace)


class HolidayAssignmentCleanupViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.user.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.client.force_login(self.user)
        self.on_date = date(2027, 2, 1)
        self.session = open_monthly_draft(self.on_date).sessions.get(
            date=self.on_date, period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        self.physician = Physician.objects.create(display_name="Retained Faculty")
        EffectiveRole.objects.create(
            physician=self.physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        self.assignment = PreceptorAssignment.objects.create(
            session=self.session,
            physician=self.physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )
        schedule_holiday_occurrence(
            occurrence=holiday_occurrences_for(2027).get(holiday=ClinicHoliday.CHRISTMAS_DAY),
            observed_date=self.on_date,
            scheduler=self.user,
        )
        self.edit_url = reverse("session-assignment-edit", args=(self.session.pk, "primary-1"))

    @patch(
        "schedules.services.operational_attention.organization_today", return_value=date(2026, 1, 1)
    )
    def test_list_closed_session_actions_reach_retained_assignment_or_calendar(self, _today):
        self.assignment.primary_position = 2
        self.assignment.save()
        response = self.client.get(reverse("preceptor-list"), {"month": "2027-02"})
        table = _RenderedDataTable(
            response.content.decode(), table_class="preceptor-schedule-list-table"
        )
        for period, expected in (
            (
                "AM",
                reverse("session-assignment-edit", args=(self.session.pk, "primary-2"))
                + "?month=2027-02",
            ),
            ("PM", reverse("monthly-workspace") + "?month=2027-02#precepting-week-2027-02-01"),
        ):
            row = next(
                row
                for row in table.body_rows()
                if any(
                    element.attributes.get("datetime") == "2027-02-01"
                    for element in row.descendants_named("time")
                )
                and row.children_named("td")[1].text().strip() == period
            )
            action = next(
                link for link in row.descendants_named("a") if link.text().strip() == "Open"
            )
            self.assertEqual(action.attributes["href"], expected)
            self.assertEqual(self.client.get(expected).status_code, 200)

    @patch(
        "schedules.services.operational_attention.organization_today",
        return_value=date(2026, 1, 1),
    )
    def test_closed_retained_assignment_is_removable_from_month_without_replacement(self, _today):
        month = self.client.get(reverse("monthly-workspace"), {"month": "2027-02"})
        cleanup = self.client.get(self.edit_url)

        for response, action in ((month, f"{self.edit_url}?month=2027-02"),):
            with self.subTest(action=action):
                parser = _RenderedSurfaceParser()
                parser.feed(response.content.decode())
                links = [
                    element
                    for element in parser.elements
                    if element.tag == "a"
                    and _action_matches(element.attributes.get("href", ""), action)
                ]
                self.assertEqual(len(links), 1)
                self.assertIn("Retained Faculty", links[0].text())
                self.assertIn("Preceptor 1", links[0].attributes.get("aria-label", ""))
        assert_rendered_heading(self, cleanup, "Preceptor 1 assignment")
        self.assertContains(
            cleanup,
            '<p class="form-page-helper">Monday, February 1, 2027 · AM</p>',
        )
        self.assertContains(cleanup, "Christmas Day closes this AM and PM clinic shift.")
        self.assertContains(cleanup, "Remove assignment")
        self.assertNotContains(cleanup, "Choose an alternative")
        self.assertNotContains(cleanup, 'name="physician"')

        rejected = self.client.post(self.edit_url, {"physician": self.physician.pk})
        self.assertEqual(rejected.status_code, 400)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.physician, self.physician)
        self.assertEqual(
            self.client.get(
                reverse("session-assignment-edit", args=(self.session.pk, "primary-2"))
            ).status_code,
            404,
        )
        unassigned_edit_url = reverse(
            "session-assignment-edit", args=(self.session.pk, "primary-2")
        )
        for response in (month,):
            parser = _RenderedSurfaceParser()
            parser.feed(response.content.decode())
            self.assertFalse(
                any(
                    element.tag == "a"
                    and _action_matches(element.attributes.get("href", ""), unassigned_edit_url)
                    for element in parser.elements
                )
            )

        removed = self.client.post(
            f"{reverse('session-assignment-remove', args=(self.session.pk, 'primary-1'))}"
            "?month=2027-02",
            {"reason": "Holiday cleanup"},
        )
        self.assertRedirects(
            removed,
            reverse("monthly-workspace") + "?month=2027-02",
            fetch_redirect_response=False,
        )
        change = PreceptorAssignmentChange.objects.get(
            session=self.session, action=PreceptorAssignmentChange.Action.REMOVED
        )
        self.assertEqual(change.reason, "Holiday cleanup")
        self.assertEqual(self.client.get(self.edit_url).status_code, 404)
        parser = _RenderedSurfaceParser()
        parser.feed(
            self.client.get(reverse("monthly-workspace"), {"month": "2027-02"}).content.decode()
        )
        self.assertFalse(
            any(
                element.tag == "a"
                and _action_matches(element.attributes.get("href", ""), self.edit_url)
                for element in parser.elements
            )
        )

    @patch(
        "schedules.services.operational_attention.organization_today",
        return_value=date(2028, 1, 1),
    )
    def test_historical_closed_assignment_uses_normal_editor_and_retains_correction_history(
        self, _today
    ):
        replacement = Physician.objects.create(display_name="Historical replacement")
        EffectiveRole.objects.create(
            physician=replacement,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )

        editor = self.client.get(self.edit_url)
        corrected = self.client.post(
            self.edit_url,
            {"physician": replacement.pk, "reason": "Correct retained history"},
        )

        self.assertContains(editor, "Choose an alternative")
        self.assertContains(editor, 'name="physician"')
        self.assertNotContains(editor, "can only be removed")
        self.assertRedirects(
            corrected,
            reverse("monthly-workspace") + "?month=2027-02",
            fetch_redirect_response=False,
        )
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.physician, replacement)
        change = PreceptorAssignmentChange.objects.filter(
            session=self.session, action=PreceptorAssignmentChange.Action.ASSIGNED
        ).latest("pk")
        self.assertEqual(change.previous_value["physician_id"], self.physician.pk)
        self.assertEqual(change.recorded_value["physician_id"], replacement.pk)
        self.assertEqual(change.reason, "Correct retained history")


class AssignmentAlternativeViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.user.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.client.force_login(self.user)
        self.session = open_monthly_draft(date(2025, 2, 1)).sessions.get(
            date=date(2025, 2, 3), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        self.url = reverse("session-assignment-edit", args=(self.session.pk, "primary-1"))

    def physician(self, name, role):
        return create_physician(
            display_name=name,
            role=role,
            effective_start=date(2025, 1, 1),
            scheduler=self.user,
        )

    def faculty(self, name, availability="open"):
        physician = self.physician(name, EffectiveRole.Role.FACULTY)
        create_faculty_availability_template(
            physician=physician,
            effective_start=None,
            effective_end=None,
            slots={slot: availability for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.user,
        )
        return physician

    def test_form_presents_ordered_tiers_category_coverage_expectation_and_sources(self):
        expected = self.physician("Expected community", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        create_community_preceptor_rule(
            physician=expected,
            weekday=0,
            ordinals=[1],
            periods=[PreceptorSession.PeriodOfDay.AM],
            scheduler=self.user,
        )
        expected.roles.update(faculty_equivalent=True)
        backup = self.faculty("Backup faculty", availability="backup_only")
        unavailable = self.faculty("Unavailable faculty", availability="off")
        vacation = create_vacation(
            physician=unavailable,
            start_date=self.session.date,
            end_date=self.session.date,
            period_of_day=Vacation.PeriodOfDay.AM,
            scheduler=self.user,
        )
        available = self.faculty("Available faculty")

        before = {
            model: model.objects.count()
            for model in (
                Physician,
                EffectiveRole,
                FacultyAvailabilityTemplate,
                CommunityPreceptorRule,
                Vacation,
                VacationChange,
                SchedulePeriod,
                PreceptorSession,
                PreceptorAssignment,
                PreceptorAssignmentChange,
            )
        }
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        parser = _RenderedSurfaceParser()
        parser.feed(response.content.decode())
        expected_detail = rendered_assignment_alternative(parser, expected)
        available_detail = rendered_assignment_alternative(parser, available)
        backup_detail = rendered_assignment_alternative(parser, backup)
        unavailable_detail = rendered_assignment_alternative(parser, unavailable)
        self.assertIn("Category: Community Preceptor", expected_detail.text())
        self.assertIn("Faculty-equivalent: Yes", expected_detail.text())
        self.assertIn("Expected Community Preceptor", expected_detail.text())
        self.assertIn("Backup only", backup_detail.text())
        self.assertIn("non-blocking preference finding", backup_detail.text())
        self.assertIn("hard availability finding", unavailable_detail.text())
        self.assertIn(f"Vacation {vacation.pk}", unavailable_detail.text())
        self.assertIn("Not available", unavailable_detail.text())
        tiers = [
            element
            for element in parser.elements
            if element.tag == "fieldset"
            and "alternative-tier" in element.attributes.get("class", "").split()
        ]
        self.assertLess(
            tiers.index(alternative_tier(expected_detail)),
            tiers.index(alternative_tier(available_detail)),
        )
        self.assertLess(
            tiers.index(alternative_tier(available_detail)),
            tiers.index(alternative_tier(backup_detail)),
        )
        self.assertLess(
            tiers.index(alternative_tier(backup_detail)),
            tiers.index(alternative_tier(unavailable_detail)),
        )
        self.assertEqual(
            before,
            {model: model.objects.count() for model in before},
        )

    def test_backup_and_not_available_choices_remain_selectable_and_history_is_retained(self):
        backup = self.faculty("Backup faculty", availability="backup_only")
        response = self.client.post(self.url, {"physician": backup.pk})

        self.assertRedirects(
            response,
            reverse("monthly-workspace") + "?month=2025-02",
            fetch_redirect_response=False,
        )
        assignment = PreceptorAssignment.objects.get(session=self.session)
        self.assertEqual(assignment.physician, backup)
        self.assertEqual(PreceptorAssignmentChange.objects.count(), 1)
        self.assertEqual(PreceptorAssignmentChange.objects.get().reason, "")

        response = self.client.get(self.url)
        current = response.context["alternatives"].current
        self.assertContains(response, "Assignment Options")
        self.assertContains(response, current.availability_reason)
        self.assertContains(response, f"{current.sources[0].label}: {current.sources[0].reason}")

        unavailable = self.faculty("Unavailable faculty", availability="off")
        response = self.client.post(
            reverse("session-assignment-edit", args=(self.session.pk, "primary-1")),
            {"physician": unavailable.pk},
        )

        self.assertRedirects(
            response,
            reverse("monthly-workspace") + "?month=2025-02",
            fetch_redirect_response=False,
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.physician, unavailable)
        changes = list(PreceptorAssignmentChange.objects.order_by("pk"))
        self.assertEqual([change.action for change in changes], ["assigned", "assigned"])
        self.assertEqual(changes[1].previous_value["physician_id"], backup.pk)
        self.assertEqual(changes[1].recorded_value["physician_id"], unavailable.pk)

    def test_editor_offers_interview_conflicts_with_all_reasons_and_rejects_forged_occupancy(self):
        arlo = self.faculty("Arlo Alder")
        arlo_role = arlo.roles.get(role=EffectiveRole.Role.FACULTY)
        arlo_role.faculty_leadership = EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR
        arlo_role.save(update_fields=("faculty_leadership",))
        interviewer = self.faculty("Direct Interviewer")
        occupied = self.faculty("Already assigned")
        vacation = create_vacation(
            physician=arlo,
            start_date=self.session.date,
            end_date=self.session.date,
            period_of_day=Vacation.PeriodOfDay.AM,
            scheduler=self.user,
        )
        shift = create_interview_shift(
            academic_year=2024, date=self.session.date, scheduler=self.user
        )
        assign_interview_shift(shift=shift, physician=interviewer, scheduler=self.user)
        assign_preceptor(
            session=self.session,
            physician=occupied,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=2,
            scheduler=self.user,
        )
        before = PreceptorAssignment.objects.count()

        response = self.client.get(self.url)

        self.assertEqual(PreceptorAssignment.objects.count(), before)
        self.assertContains(response, "Hard-conflict or not-available physicians")
        self.assertContains(response, "Assessment details")
        self.assertContains(response, "Program Director Interview")
        self.assertContains(response, f"Vacation {vacation.pk}")
        alternatives = {
            alternative.physician: alternative for alternative in response.context["alternatives"]
        }
        self.assertEqual(alternatives[arlo].tier, "not_available")
        self.assertTrue(alternatives[arlo].manually_selectable)
        self.assertFalse(alternatives[arlo].automatic_eligible)
        self.assertEqual(
            [reason.code for reason in alternatives[arlo].reasons],
            [
                "not_available_assignment",
                "availability_source",
                "availability_source",
                "program_director_interview_commitment",
            ],
        )
        self.assertEqual(
            next(
                reason.source_identity
                for reason in alternatives[arlo].reasons
                if reason.source_type == "vacation"
            ),
            ("vacation", vacation.pk),
        )
        self.assertIn(interviewer, alternatives)
        self.assertIn(
            "interview_assignment_conflict",
            [reason.code for reason in alternatives[interviewer].reasons],
        )
        self.assertNotIn(occupied, alternatives)

        assigned = self.client.post(self.url, {"physician": arlo.pk, "reason": "Deliberate choice"})

        self.assertRedirects(
            assigned,
            reverse("monthly-workspace") + "?month=2025-02",
            fetch_redirect_response=False,
        )
        self.assertEqual(
            PreceptorAssignment.objects.get(session=self.session, primary_position=1).physician,
            arlo,
        )
        self.assertEqual(
            {
                finding.code
                for finding in draft_findings_for_session(self.session).findings
                if arlo in finding.physicians
            },
            {"not_available_assignment", "program_director_interview_commitment"},
        )

        forged = self.client.post(
            reverse("session-assignment-edit", args=(self.session.pk, "secondary")),
            {"physician": occupied.pk},
        )

        self.assertEqual(forged.status_code, 200)
        self.assertFalse(
            PreceptorAssignment.objects.filter(
                session=self.session, responsibility=PreceptorAssignment.Responsibility.SECONDARY
            ).exists()
        )

    def test_stale_role_submission_is_revalidated_and_anonymous_access_has_no_effect(self):
        stale = self.faculty("Stale faculty")
        self.client.get(self.url)
        correct_role(
            effective_role=stale.roles.get(role=EffectiveRole.Role.FACULTY),
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.user,
        )

        response = self.client.post(self.url, {"physician": stale.pk, "reason": "Keep the note"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice")
        self.assertContains(response, "Keep the note")
        self.assertFalse(PreceptorAssignment.objects.exists())
        self.assertFalse(PreceptorAssignmentChange.objects.exists())

        self.client.logout()
        response = self.client.post(self.url, {"physician": stale.pk})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PreceptorAssignment.objects.exists())
        self.assertFalse(PreceptorAssignmentChange.objects.exists())

    def test_retained_ineligible_current_assignment_is_separate_and_replaceable(self):
        current = self.faculty("Retained current")
        replacement = self.faculty("Replacement")
        assignment = assign_preceptor(
            session=self.session,
            physician=current,
            responsibility="primary",
            primary_position=1,
            scheduler=self.user,
        )
        correct_role(
            effective_role=current.roles.get(role=EffectiveRole.Role.FACULTY),
            effective_start=date(2025, 1, 1),
            effective_end=date(2025, 1, 31),
            scheduler=self.user,
        )

        response = self.client.get(self.url)

        self.assertContains(response, "Current assignment")
        self.assertContains(response, "Assignment Options")
        self.assertContains(response, "Retained assignment: no effective preceptor role")
        self.assertContains(response, "Replacement")
        content = response.content.decode()
        current_summary = content[
            content.index(
                'class="surface-frame surface-frame--raised assignment-current"'
            ) : content.index(
                "</section>",
                content.index('class="surface-frame surface-frame--raised assignment-current"'),
            )
        ]
        self.assertIn("Retained current", current_summary)
        self.assertNotIn("Category:", current_summary)
        self.assertNotIn("Faculty-equivalent:", current_summary)
        self.assertNotIn("Effective state:", current_summary)
        self.assertNotIn("Retained assignment:", current_summary)
        self.assertLess(
            content.index("<legend>Current assignment</legend>"),
            content.index("<legend>Other available physicians</legend>"),
        )
        self.assertEqual(
            [alternative.physician for alternative in response.context["alternatives"]],
            [replacement],
        )

        response = self.client.post(self.url, {"physician": replacement.pk})

        self.assertRedirects(
            response,
            reverse("monthly-workspace") + "?month=2025-02",
            fetch_redirect_response=False,
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.physician, replacement)
        self.assertEqual(
            list(PreceptorAssignmentChange.objects.values_list("action", flat=True)),
            ["assigned", "assigned"],
        )

    def test_retained_secondary_community_assignment_is_visible_and_replaceable(self):
        community = self.physician("Retained community", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        replacement = self.faculty("Faculty replacement")
        assignment = PreceptorAssignment.objects.create(
            session=self.session,
            physician=community,
            responsibility=PreceptorAssignment.Responsibility.SECONDARY,
            primary_position=None,
        )
        secondary_url = reverse("session-assignment-edit", args=(self.session.pk, "secondary"))

        response = self.client.get(secondary_url)

        self.assertContains(response, "Retained community")
        self.assertContains(response, "Community Preceptor assignments must be Primary")
        self.assertEqual(
            [alternative.physician for alternative in response.context["alternatives"]],
            [replacement],
        )

        response = self.client.post(secondary_url, {"physician": replacement.pk})

        self.assertRedirects(
            response,
            reverse("monthly-workspace") + "?month=2025-02",
            fetch_redirect_response=False,
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.physician, replacement)
        self.assertEqual(
            PreceptorAssignmentChange.objects.get().previous_value["physician_id"],
            community.pk,
        )


class VacationViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.physician = Physician.objects.create(display_name="Taylor Adams")
        self.client.force_login(self.user)

    def test_scheduler_can_inspect_month_record_correct_remove_and_view_history(self):
        workspace_url = reverse("vacation-workspace")
        create_url = reverse("vacation-create")
        response = self.client.get(workspace_url, {"month": "2025-02"})
        self.assertContains(response, 'aria-label="Vacation navigation"')
        self.assertContains(response, 'aria-current="page">Calendar</a>')
        self.assertContains(response, 'href="/vacations/list/?month=2025-02">List</a>')
        self.assertContains(response, 'href="/vacations/holidays/">Holidays</a>')
        self.assertContains(response, "Sunday")
        self.assertContains(response, "Add vacation")
        self.assertContains(
            response,
            "Click the “Add Vacation” button to add a new vacation starting today. Click on a day in the calendar to start a vacation on that day.",
        )
        self.assertContains(response, "Vacation history")
        self.assertNotContains(response, "Inspect range")
        self.assertNotContains(response, "No vacation entries overlap this range.")

        response = self.client.post(
            create_url,
            {
                "physician": self.physician.pk,
                "start_date": "2025-02-10",
                "end_date": "2025-02-12",
                "period_of_day": Vacation.PeriodOfDay.WHOLE_DAY,
                "note": "Planned leave",
            },
        )
        self.assertRedirects(response, workspace_url, fetch_redirect_response=False)
        vacation = Vacation.objects.get()
        self.assertEqual(vacation.note, "Planned leave")
        workspace = self.client.get(workspace_url, {"month": "2025-02"})
        self.assertContains(workspace, "Taylor Adams")
        self.assertContains(workspace, "vacation-segment")
        self.assertNotContains(workspace, "Whole day")

        correct_url = reverse("vacation-correct", args=(vacation.pk,))
        edit = self.client.get(correct_url)
        self.assertEqual(edit.context["form"]["note"].value(), "Planned leave")
        other_physician = Physician.objects.create(display_name="Morgan Chen")
        response = self.client.post(
            correct_url,
            {
                "physician": other_physician.pk,
                "start_date": "2025-02-10",
                "end_date": "2025-02-13",
                "period_of_day": Vacation.PeriodOfDay.PM,
                "note": "Correct end date",
            },
        )
        self.assertRedirects(response, workspace_url, fetch_redirect_response=False)
        vacation.refresh_from_db()
        self.assertEqual(vacation.physician, self.physician)
        self.assertEqual(vacation.note, "Correct end date")
        self.assertNotContains(self.client.get(correct_url), 'name="reason"')
        response = self.client.post(reverse("vacation-remove", args=(vacation.pk,)))
        self.assertRedirects(response, workspace_url, fetch_redirect_response=False)
        history = self.client.get(reverse("vacation-history"))
        self.assertContains(history, "Planned leave")
        self.assertContains(history, "Correct end date")
        self.assertContains(history, "Prior note")
        self.assertContains(history, "Recorded note")
        self.assertContains(history, "No prior note.")
        self.assertContains(history, "No recorded note.")
        self.assertContains(history, "Edited")
        self.assertNotContains(history, "Corrected")
        self.assertEqual(VacationChange.objects.count(), 3)

    def test_calendar_uses_complete_sunday_weeks_and_includes_adjacent_ranges(self):
        vacation = Vacation.objects.create(
            physician=self.physician,
            start_date=date(2026, 7, 31),
            end_date=date(2026, 8, 2),
        )

        response = self.client.get(reverse("vacation-workspace"), {"month": "2026-08"})

        self.assertEqual(response.context["calendar_start"], date(2026, 7, 26))
        self.assertEqual(response.context["calendar_end"], date(2026, 9, 5))
        self.assertIn(vacation, response.context["vacations"])
        self.assertEqual(len(response.context["weeks"]), 6)
        self.assertTrue(all(len(week["days"]) == 7 for week in response.context["weeks"]))
        self.assertEqual(
            [day["date"] for day in response.context["weeks"][0]["days"]],
            [date(2026, 7, day) for day in range(26, 32)] + [date(2026, 8, 1)],
        )
        self.assertEqual(
            [day["date"] for day in response.context["weeks"][-1]["days"]],
            [date(2026, 8, 30), date(2026, 8, 31), *[date(2026, 9, day) for day in range(1, 6)]],
        )

        segments = [
            segment
            for week in response.context["weeks"]
            for segment in week["segments"]
            if segment["vacation"] == vacation
        ]
        self.assertEqual(
            [
                (segment["start_date"], segment["end_date"], segment["day_count"])
                for segment in segments
            ],
            [
                (date(2026, 7, 31), date(2026, 8, 1), 2),
                (date(2026, 8, 2), date(2026, 8, 2), 1),
            ],
        )
        self.assertContains(response, 'datetime="2026-07-26"')
        self.assertContains(response, 'datetime="2026-09-05"')
        self.assertContains(
            response, 'class="calendar-lattice-date-cell calendar-adjacent-month"', count=11
        )

    def test_calendar_stacks_holidays_as_informational_range_items(self):
        response = self.client.get(reverse("vacation-workspace"), {"month": "2026-12"})
        content = response.content.decode()
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()

        self.assertNotIn("calendar-holiday-header", content)
        self.assertIn(
            '<p class="calendar-holiday" aria-label="Holiday: Christmas Eve, PM only">'
            "Christmas Eve, PM</p>",
            content,
        )
        self.assertIn(
            '<p class="calendar-holiday" aria-label="Holiday: Christmas Day, AM and PM">'
            "Christmas Day</p>",
            content,
        )
        self.assertNotIn('<a class="calendar-holiday-header"', content)
        self.assertNotIn(
            '<p class="calendar-holiday" aria-label="Holiday: Christmas Eve, PM only"><a ', content
        )
        self.assertNotIn(
            'aria-label="Holiday: Christmas Day, AM and PM"><span>Christmas Day,', content
        )
        self.assertContains(response, "calendar-lattice-item--holiday")
        self.assertContains(response, "calendar-content-cell--holiday")
        self.assertEqual(response.context["calendar"].weeks[0].lanes[0].variant, "range")
        self.assertContains(response, "calendar-lattice-lane--range")
        self.assertNotContains(response, "Clinic closures")
        self.assertIn(
            ".calendar-lattice-date-cell--holiday, .calendar-weekday-header--holiday "
            "{ background: #f3effa; }",
            stylesheet,
        )
        self.assertIn(
            ".calendar-lattice-lane--range .calendar-lattice-items { grid-auto-rows: auto; "
            "row-gap: .45rem; }",
            stylesheet,
        )

    def test_calendar_actions_are_real_links_with_adjacent_date_prefill_and_month_origin(self):
        vacation = Vacation.objects.create(
            physician=self.physician,
            start_date=date(2026, 7, 31),
            end_date=date(2026, 8, 2),
        )

        response = self.client.get(reverse("vacation-workspace"), {"month": "2026-08"})

        self.assertContains(response, "Add vacation")
        self.assertContains(response, 'id="vacation-add-2026-07-26"')
        self.assertContains(
            response,
            'role="columnheader" data-calendar-day-action="vacation-add-2026-07-26"',
        )
        self.assertContains(response, 'data-calendar-day-action="vacation-add-2026-07-26"', count=2)
        self.assertContains(
            response,
            'href="/vacations/add/?view=calendar&amp;start_date=2026-07-26&amp;end_date=2026-07-26&amp;month=2026-08"',
        )
        date_header_links = re.findall(
            r'<a id="vacation-add-[^"]+" class="calendar-cell-action vacation-date-add-link"[^>]+aria-label="Add vacation for [^"]+">(?P<content>.*?)</a>',
            response.content.decode(),
        )
        self.assertEqual(len(date_header_links), 42)
        self.assertTrue(all("<a " not in content for content in date_header_links))
        self.assertTrue(all("+" not in content for content in date_header_links))
        self.assertContains(response, '<div class="calendar-date-header"><a')
        self.assertNotContains(response, "calendar-cell-action-affordance")
        self.assertContains(
            response,
            f'href="/vacations/{vacation.pk}/correct/?view=calendar&amp;month=2026-08"',
            count=2,
        )

        response = self.client.get(
            reverse("vacation-create"),
            {"start_date": "2026-07-26", "end_date": "2026-07-26", "month": "2026-08"},
        )

        form = response.context["form"]
        self.assertEqual(form["start_date"].value(), date(2026, 7, 26))
        self.assertEqual(form["end_date"].value(), date(2026, 7, 26))
        self.assertIsNone(form["physician"].value())
        self.assertEqual(form["period_of_day"].value(), Vacation.PeriodOfDay.WHOLE_DAY)
        self.assertContains(
            response, 'href="/vacations/?month=2026-08">Back to Vacation Schedule</a>'
        )
        self.assertContains(response, 'action="/vacations/add/?month=2026-08"')

        edit_response = self.client.get(
            reverse("vacation-correct", args=(vacation.pk,)), {"month": "2026-08"}
        )
        self.assertContains(edit_response, "Edit vacation")
        self.assertContains(edit_response, "<strong>Physician:</strong> Taylor Adams", html=True)
        self.assertNotContains(edit_response, 'name="physician"')

    def test_add_edit_and_remove_forms_use_the_shared_source_form_presentation(self):
        add_response = self.client.get(reverse("vacation-create"))
        assert_rendered_heading(self, add_response, "Record vacation")
        self.assertContains(add_response, "Vacation details")
        self.assertContains(add_response, "Dates")
        self.assertContains(add_response, "Actions")
        self.assertContains(add_response, 'class="form-field-grid form-field-grid--source-details"')

        vacation = Vacation.objects.create(
            physician=self.physician, start_date=date(2026, 8, 10), end_date=date(2026, 8, 12)
        )
        edit_response = self.client.get(reverse("vacation-correct", args=(vacation.pk,)))
        self.assertContains(edit_response, 'class="form-field-group form-read-only-value"')
        self.assertContains(edit_response, "Physician:</strong> Taylor Adams")
        assert_removal_section(
            self,
            edit_response,
            "vacation-removal-heading",
            reverse("vacation-remove", args=(vacation.pk,)),
        )
        self.assertContains(edit_response, "This retains the final vacation note")

        invalid_response = self.client.post(
            reverse("vacation-create"),
            {
                "physician": self.physician.pk,
                "start_date": "2026-08-12",
                "end_date": "2026-08-10",
                "period_of_day": Vacation.PeriodOfDay.WHOLE_DAY,
            },
        )
        self.assertContains(invalid_response, 'aria-invalid="true"')
        self.assertContains(invalid_response, 'class="errorlist" id="id_end_date_error"')

    def test_workspace_actions_and_direct_create_default_to_the_clinic_local_day(self):
        clinic_previous_day = datetime(2026, 8, 12, 4, tzinfo=UTC)
        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=clinic_previous_day,
        ):
            calendar_response = self.client.get(reverse("vacation-workspace"), {"month": "2026-08"})
            list_response = self.client.get(reverse("vacation-list"), {"month": "2026-08"})
            create_response = self.client.get(reverse("vacation-create"))

        expected_calendar_url = (
            "/vacations/add/?view=calendar&amp;month=2026-08"
            "&amp;start_date=2026-08-11&amp;end_date=2026-08-11"
        )
        expected_list_url = (
            "/vacations/add/?view=list&amp;month=2026-08"
            "&amp;start_date=2026-08-11&amp;end_date=2026-08-11"
        )
        self.assertContains(
            calendar_response,
            f'<div class="form-action-area"><a class="button-link" href="{expected_calendar_url}">Add vacation</a></div>',
        )
        self.assertContains(
            list_response,
            f'<div class="form-action-area"><a class="button-link" href="{expected_list_url}">Add vacation</a></div>',
        )
        self.assertEqual(create_response.context["form"]["start_date"].value(), date(2026, 8, 11))
        self.assertEqual(create_response.context["form"]["end_date"].value(), date(2026, 8, 11))

    def test_vacation_list_actions_explain_how_to_edit_a_vacation(self):
        list_response = self.client.get(reverse("vacation-list"), {"month": "2026-08"})
        calendar_response = self.client.get(reverse("vacation-workspace"), {"month": "2026-08"})

        helper = "Select Edit in the list below to update a vacation."
        self.assertContains(list_response, f"<p>{helper}</p>", html=True)
        self.assertNotContains(calendar_response, helper)

    def test_calendar_week_bodies_have_non_focusable_day_cells_beneath_range_bars(self):
        response = self.client.get(reverse("vacation-workspace"), {"month": "2026-08"})

        self.assertContains(
            response, 'class="calendar-lattice-background" aria-hidden="true"', count=6
        )
        self.assertContains(response, 'data-calendar-day="', count=42)
        self.assertContains(response, 'id="vacation-add-2026-07-26"')
        self.assertNotContains(response, "No vacations this week.")
        self.assertNotContains(response, "calendar-lattice-empty")

    def test_vacation_prefill_and_return_context_reject_malformed_values(self):
        response = self.client.get(
            reverse("vacation-create"),
            {
                "start_date": "2026-08-09",
                "end_date": "not-a-date",
                "month": "https://outside.example",
            },
        )

        form = response.context["form"]
        self.assertIsNone(form["start_date"].value())
        self.assertIsNone(form["end_date"].value())
        self.assertIsNone(form["physician"].value())
        self.assertEqual(form["period_of_day"].value(), Vacation.PeriodOfDay.WHOLE_DAY)
        self.assertContains(response, 'href="/vacations/">Back to Vacation Schedule</a>')
        self.assertContains(response, 'action="/vacations/add/"')
        self.assertNotContains(response, "outside.example")

    def test_calendar_origin_returns_after_create_correction_and_removal(self):
        return_month = "2026-08"
        workspace_url = reverse("vacation-workspace") + f"?month={return_month}"
        create_url = reverse("vacation-create") + f"?month={return_month}"
        values = {
            "physician": self.physician.pk,
            "start_date": "2026-07-26",
            "end_date": "2026-07-26",
            "period_of_day": Vacation.PeriodOfDay.WHOLE_DAY,
        }

        response = self.client.post(create_url, values)
        self.assertRedirects(response, workspace_url, fetch_redirect_response=False)
        vacation = Vacation.objects.get()

        correct_url = reverse("vacation-correct", args=(vacation.pk,)) + f"?month={return_month}"
        response = self.client.post(
            correct_url,
            {**values, "end_date": "2026-07-27", "note": "Extend leave"},
        )
        self.assertRedirects(response, workspace_url, fetch_redirect_response=False)

        remove_url = reverse("vacation-remove", args=(vacation.pk,)) + f"?month={return_month}"
        response = self.client.post(remove_url)
        self.assertRedirects(response, workspace_url, fetch_redirect_response=False)

        response = self.client.post(
            reverse("vacation-create") + "?month=2026-8",
            {**values, "start_date": "2026-08-03", "end_date": "2026-08-03"},
        )
        self.assertRedirects(response, reverse("vacation-workspace"), fetch_redirect_response=False)

    def test_overlapping_ranges_use_name_ordered_lanes_and_explicit_coverage_labels(self):
        first = Physician.objects.create(display_name="Alex Brown")
        second = Physician.objects.create(display_name="Morgan Chen")
        whole_day = Vacation.objects.create(
            physician=self.physician,
            start_date=date(2026, 8, 12),
            end_date=date(2026, 8, 12),
        )
        am_only = Vacation.objects.create(
            physician=first,
            start_date=date(2026, 8, 12),
            end_date=date(2026, 8, 12),
            period_of_day=Vacation.PeriodOfDay.AM,
        )
        pm_only = Vacation.objects.create(
            physician=second,
            start_date=date(2026, 8, 12),
            end_date=date(2026, 8, 12),
            period_of_day=Vacation.PeriodOfDay.PM,
        )

        response = self.client.get(reverse("vacation-workspace"), {"month": "2026-08"})

        week = next(
            week
            for week in response.context["weeks"]
            if date(2026, 8, 12) in [day["date"] for day in week["days"]]
        )
        segments = tuple(
            segment for segment in week["segments"] if segment["start_date"] == date(2026, 8, 12)
        )
        self.assertCountEqual(
            [
                (
                    segment["physician_name"],
                    segment["coverage_label"],
                    segment["vacation"],
                )
                for segment in segments
            ],
            [
                ("Alex Brown", Vacation.PeriodOfDay.AM, am_only),
                ("Morgan Chen", Vacation.PeriodOfDay.PM, pm_only),
                ("Taylor Adams", "", whole_day),
            ],
        )
        self.assertContains(response, "Alex Brown")
        self.assertContains(response, "Morgan Chen")
        self.assertContains(response, "Taylor Adams")
        self.assertContains(response, "(AM)")
        self.assertContains(response, "(PM)")
        self.assertNotContains(response, "Taylor Adams (")

    def test_month_navigation_rejects_ranges_and_preserves_picker_language(self):
        response = self.client.get(reverse("vacation-workspace"), {"month": "2024-02"})
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "February 2024")
        self.assertContains(response, "?month=2024-01")
        self.assertContains(response, "?month=2024-03")
        self.assertContains(response, 'value="2024-02"')
        self.assertContains(response, "Choose month")
        self.assertContains(response, 'src="/static/schedules/calendar-period-picker.js"')
        self.assertContains(response, "data-calendar-period-picker")
        self.assertContains(response, '<noscript><button type="submit">View</button></noscript>')
        self.assertLess(
            content.index("?month=2024-01"), content.index('id="schedule-workspace-month"')
        )
        self.assertLess(
            content.index('id="schedule-workspace-month"'), content.index("?month=2024-03")
        )

        invalid = self.client.get(reverse("vacation-workspace"), {"month": "February"})
        self.assertEqual(invalid.status_code, 400)

    def test_list_shows_current_ranges_overlapping_the_selected_month_in_date_order(self):
        first = Physician.objects.create(display_name="Alex Brown")
        second = Physician.objects.create(display_name="Morgan Chen")
        leading = Vacation.objects.create(
            physician=first,
            start_date=date(2026, 7, 31),
            end_date=date(2026, 8, 1),
            note="Starts before August",
        )
        middle = Vacation.objects.create(
            physician=self.physician,
            start_date=date(2026, 8, 12),
            end_date=date(2026, 8, 13),
            period_of_day=Vacation.PeriodOfDay.AM,
        )
        trailing = Vacation.objects.create(
            physician=second,
            start_date=date(2026, 8, 31),
            end_date=date(2026, 9, 1),
            period_of_day=Vacation.PeriodOfDay.PM,
            note="Ends after August",
        )
        Vacation.objects.create(
            physician=self.physician,
            start_date=date(2026, 7, 30),
            end_date=date(2026, 7, 30),
        )
        Vacation.objects.create(
            physician=self.physician,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 1),
        )

        response = self.client.get(reverse("vacation-list"), {"month": "2026-08"})

        self.assertTemplateUsed(response, "schedules/schedule_workspace_base.html")
        self.assertTemplateUsed(response, "schedules/schedule_workspace_month_navigation.html")
        self.assertEqual(list(response.context["vacation_list_rows"]), [leading, middle, trailing])
        self.assertContains(response, 'aria-current="page">List</a>')
        self.assertContains(response, 'href="/vacations/?month=2026-08">Calendar</a>')
        self.assertContains(response, 'href="/vacations/list/?month=2026-07"')
        self.assertContains(response, 'href="/vacations/list/?month=2026-09"')
        self.assertContains(response, 'action="/vacations/list/" method="get"')
        self.assertContains(response, "Whole day")
        self.assertContains(response, "AM only")
        self.assertContains(response, "PM only")
        self.assertContains(response, "Starts before August")
        self.assertContains(response, "No note")
        self.assertContains(response, "Ends after August")
        self.assertContains(response, '<h2 id="vacation-list-heading">Vacations</h2>')
        self.assertContains(response, 'id="vacation-list" class="data-table data-table--compact"')
        self.assertNotContains(response, "Selecting a calendar day begins with the selected date.")
        self.assertContains(
            response,
            f'href="/vacations/{middle.pk}/correct/?view=list&amp;month=2026-08">Edit</a>',
        )

    def test_list_context_survives_add_edit_removal_and_history(self):
        list_url = reverse("vacation-list") + "?month=2026-08"
        create_url = reverse("vacation-create") + "?view=list&month=2026-08"
        values = {
            "physician": self.physician.pk,
            "start_date": "2026-08-10",
            "end_date": "2026-08-10",
            "period_of_day": Vacation.PeriodOfDay.WHOLE_DAY,
        }

        response = self.client.get(list_url)
        self.assertContains(response, "Add vacation")
        self.assertContains(response, 'href="/vacations/history/?view=list&amp;month=2026-08"')
        response = self.client.post(create_url, values)
        self.assertRedirects(response, list_url, fetch_redirect_response=False)
        vacation = Vacation.objects.get()

        correct_url = reverse("vacation-correct", args=(vacation.pk,)) + "?view=list&month=2026-08"
        response = self.client.get(correct_url)
        self.assertContains(
            response, 'href="/vacations/list/?month=2026-08">Back to Vacation Schedule</a>'
        )
        self.assertContains(response, f'action="{correct_url.replace("&", "&amp;")}"')
        response = self.client.post(correct_url, {**values, "note": "Updated from list"})
        self.assertRedirects(response, list_url, fetch_redirect_response=False)

        history = self.client.get(reverse("vacation-history") + "?view=list&month=2026-08")
        self.assertContains(
            history, 'href="/vacations/list/?month=2026-08">Back to Vacation Schedule</a>'
        )

        remove_url = reverse("vacation-remove", args=(vacation.pk,)) + "?view=list&month=2026-08"
        response = self.client.post(remove_url)
        self.assertRedirects(response, list_url, fetch_redirect_response=False)

    def test_list_route_requires_authentication(self):
        self.client.logout()

        response = self.client.get(reverse("vacation-list"), {"month": "2026-08"})

        self.assertEqual(response.status_code, 302)

    def test_invalid_and_anonymous_submissions_do_not_write_vacations_or_history(self):
        create_url = reverse("vacation-create")
        response = self.client.post(
            create_url,
            {
                "physician": self.physician.pk,
                "start_date": "2025-02-12",
                "end_date": "2025-02-10",
                "period_of_day": Vacation.PeriodOfDay.AM,
                "reason": "Invalid range",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "End date cannot be before start date.")
        self.assertEqual(Vacation.objects.count(), 0)
        self.assertEqual(VacationChange.objects.count(), 0)

        self.client.logout()
        response = self.client.post(
            create_url,
            {
                "physician": self.physician.pk,
                "start_date": "2025-02-10",
                "end_date": "2025-02-10",
                "period_of_day": Vacation.PeriodOfDay.AM,
                "reason": "Unauthorized",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Vacation.objects.count(), 0)
        self.assertEqual(VacationChange.objects.count(), 0)


class AcademicEventViewsTests(TestCase):
    def setUp(self):
        # January 2027 is in AY 2026-2027: keep default-year requests and
        # definition selector bounds intentional, independent of the host date.
        # Nested scenario clocks still override this per-test, cleanup-safe clock.
        self.enterContext(
            patch(
                "schedules.services.operational_attention.timezone.now",
                return_value=datetime(2027, 1, 15, 18, tzinfo=UTC),
            )
        )
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.client.force_login(self.user)

    def test_scheduler_can_create_edit_deactivate_and_view_definition_history(self):
        workspace_url = reverse("academic-event-definition-workspace")
        response = self.client.get(workspace_url, {"academic_year": "2025"})
        self.assertContains(response, "Event definitions")
        self.assertContains(response, "2025-2026")

        response = self.client.post(
            reverse("academic-event-definition-create"),
            {
                "base_name": "Clinical teaching",
                "expected_quarter": "Q2",
                "typically_scheduled": "First week of October",
                "default_duration_days": "2",
                "default_period_of_day": AcademicEvent.PeriodOfDay.AM,
                "start_academic_year": "2025",
                "end_academic_year": "2026",
            },
        )
        self.assertRedirects(response, workspace_url, fetch_redirect_response=False)
        definition = AcademicEventDefinition.objects.get()

        response = self.client.get(workspace_url, {"academic_year": "2026"})
        self.assertContains(response, "Clinical teaching")
        self.assertContains(response, "First week of October")
        self.assertContains(response, "2025-2026 through 2026-2027")
        self.assertContains(response, "2 days")
        self.assertContains(response, "AM only")

        response = self.client.post(
            reverse("academic-event-definition-correct", args=(definition.pk,)),
            {
                "base_name": "Corrected clinical teaching",
                "expected_quarter": "Q3",
                "typically_scheduled": "Mid-January",
                "default_duration_days": "3",
                "default_period_of_day": AcademicEvent.PeriodOfDay.PM,
                "start_academic_year": "2025",
                "end_academic_year": "",
            },
        )
        self.assertRedirects(response, workspace_url, fetch_redirect_response=False)
        definition.refresh_from_db()
        self.assertEqual(definition.base_name, "Corrected clinical teaching")
        self.assertIsNone(definition.end_academic_year)
        self.assertEqual(definition.default_duration_days, 3)
        self.assertEqual(definition.default_period_of_day, AcademicEvent.PeriodOfDay.PM)
        self.assertEqual(definition.typically_scheduled, "Mid-January")

        response = self.client.post(
            reverse("academic-event-definition-remove", args=(definition.pk,)),
        )
        self.assertRedirects(response, workspace_url, fetch_redirect_response=False)
        history = self.client.get(reverse("academic-event-definition-history"))
        self.assertContains(history, "Corrected clinical teaching")
        self.assertContains(history, "Mid-January")
        self.assertEqual(AcademicEventDefinitionChange.objects.count(), 3)
        definition.refresh_from_db()
        self.assertFalse(definition.is_active)
        self.assertNotContains(self.client.get(workspace_url), "Corrected clinical teaching")

    def test_definition_edit_initializes_saved_default_precepting_impact(self):
        definition = AcademicEventDefinition.objects.create(
            base_name="Calendar entry",
            start_academic_year=2026,
            default_period_of_day=AcademicEvent.PeriodOfDay.NONE,
            default_precepting_impact=AcademicEvent.PreceptingImpact.NONE,
        )
        edit_url = reverse("academic-event-definition-correct", args=(definition.pk,))

        response = self.client.get(edit_url)

        self.assertEqual(
            response.context["form"]["default_precepting_impact"].value(),
            AcademicEvent.PreceptingImpact.NONE,
        )
        response = self.client.post(
            edit_url,
            {
                "base_name": "Corrected calendar entry",
                "expected_quarter": definition.expected_quarter,
                "typically_scheduled": "",
                "default_duration_days": definition.default_duration_days,
                "default_period_of_day": AcademicEvent.PeriodOfDay.NONE,
                "default_precepting_impact": AcademicEvent.PreceptingImpact.NONE,
                "default_required_participant_count": definition.default_required_participant_count,
                "start_academic_year": definition.start_academic_year,
                "end_academic_year": "",
            },
        )

        self.assertRedirects(
            response,
            reverse("academic-event-definition-workspace"),
            fetch_redirect_response=False,
        )
        definition.refresh_from_db()
        self.assertEqual(definition.default_precepting_impact, AcademicEvent.PreceptingImpact.NONE)

    def test_invalid_and_anonymous_submissions_do_not_write_event_or_history(self):
        create_url = reverse("academic-event-definition-create")
        response = self.client.post(
            create_url,
            {
                "base_name": "Invalid range",
                "expected_quarter": "Q1",
                "default_duration_days": "1",
                "default_period_of_day": AcademicEvent.PeriodOfDay.WHOLE_DAY,
                "start_academic_year": "2027",
                "end_academic_year": "2026",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2026-2027")
        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, 'class="errorlist" id="id_end_academic_year_error"')
        self.assertContains(response, "Add Academic Event definition")
        self.assertContains(
            response,
            "This definition supplies an event that can be scheduled in applicable academic years.",
        )
        self.assertContains(response, '<label for="id_base_name">Name</label>')
        self.assertContains(
            response, '<label for="id_default_period_of_day">Affected coverage</label>'
        )
        self.assertEqual(AcademicEventDefinition.objects.count(), 0)
        self.assertEqual(AcademicEventDefinitionChange.objects.count(), 0)

        self.client.logout()
        response = self.client.post(
            create_url,
            {
                "base_name": "Unauthorized",
                "expected_quarter": "Q1",
                "default_duration_days": "1",
                "default_period_of_day": AcademicEvent.PeriodOfDay.WHOLE_DAY,
                "start_academic_year": "2025",
                "end_academic_year": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AcademicEventDefinition.objects.count(), 0)
        self.assertEqual(AcademicEventDefinitionChange.objects.count(), 0)
        self.assertEqual(
            self.client.get(reverse("academic-event-definition-workspace")).status_code, 302
        )

    def test_definition_remove_is_unavailable_for_current_and_retained_annual_use(self):
        current_definition = AcademicEventDefinition.objects.create(
            base_name="Current use", start_academic_year=2026
        )
        AcademicEvent.objects.create(
            definition=current_definition,
            academic_year=2026,
            base_name=current_definition.base_name,
            expected_quarter=current_definition.expected_quarter,
            required_participant_count=1,
            resolution=AcademicEvent.Resolution.DATED,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 10),
        )
        retained_definition = AcademicEventDefinition.objects.create(
            base_name="Retained use", start_academic_year=2026
        )
        retained_event = AcademicEvent.objects.create(
            definition=retained_definition,
            academic_year=2026,
            base_name=retained_definition.base_name,
            expected_quarter=retained_definition.expected_quarter,
            required_participant_count=1,
            resolution=AcademicEvent.Resolution.DATED,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 10),
        )
        retained_event.is_active = False
        retained_event.save(update_fields=("is_active",))

        for definition in (current_definition, retained_definition):
            with self.subTest(definition=definition.base_name):
                response = self.client.get(
                    reverse("academic-event-definition-correct", args=(definition.pk,))
                )
                remove_url = reverse("academic-event-definition-remove", args=(definition.pk,))

                self.assertContains(
                    response,
                    "Cannot remove: this definition has scheduled or retained use.",
                )
                self.assertContains(
                    response,
                    'disabled aria-describedby="definition-removal-unavailable"',
                )
                self.assertNotContains(response, f'action="{remove_url}"')
                content = response.content.decode()
                removal_start = content.index('id="definition-removal-heading"')
                self.assertLess(
                    content.index('class="surface-body form-content-stack"', removal_start),
                    content.index('class="form-action-area"', removal_start),
                )

        unused_definition = AcademicEventDefinition.objects.create(
            base_name="Unused", start_academic_year=2026
        )
        response = self.client.get(
            reverse("academic-event-definition-correct", args=(unused_definition.pk,))
        )
        self.assertContains(
            response,
            f'action="{reverse("academic-event-definition-remove", args=(unused_definition.pk,))}"',
        )
        self.assertNotContains(response, "definition-removal-unavailable")

    def test_definition_forms_use_shared_sections_and_retained_removal_treatment(self):
        add_response = self.client.get(reverse("academic-event-definition-create"))
        self.assertContains(add_response, "Add Academic Event definition")
        self.assertContains(
            add_response,
            "This definition supplies an event that can be scheduled in applicable academic years.",
        )
        self.assertContains(add_response, '<label for="id_base_name">Name</label>')
        self.assertContains(
            add_response, '<label for="id_default_period_of_day">Affected coverage</label>'
        )
        for heading in ("Definition", "Scheduling defaults", "Active academic years", "Actions"):
            assert_rendered_heading(self, add_response, heading, level=2)
        self.assertContains(add_response, "Used when a future occurrence is scheduled.")

        unused = AcademicEventDefinition.objects.create(
            base_name="Unused form", start_academic_year=2026
        )
        edit_response = self.client.get(
            reverse("academic-event-definition-correct", args=(unused.pk,))
        )
        assert_removal_section(
            self,
            edit_response,
            "definition-removal-heading",
            reverse("academic-event-definition-remove", args=(unused.pk,)),
        )
        self.assertContains(
            edit_response, "This deactivates the definition and retains its history."
        )
        retained = AcademicEventDefinition.objects.create(
            base_name="Retained form", start_academic_year=2026
        )
        AcademicEvent.objects.create(
            definition=retained,
            academic_year=2026,
            base_name=retained.base_name,
            expected_quarter=retained.expected_quarter,
            required_participant_count=1,
            resolution=AcademicEvent.Resolution.DATED,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 10),
        )
        retained_response = self.client.get(
            reverse("academic-event-definition-correct", args=(retained.pk,))
        )
        self.assertContains(
            retained_response, 'disabled aria-describedby="definition-removal-unavailable"'
        )

    def test_stale_definition_remove_with_new_annual_use_is_safe_and_reports_the_problem(self):
        definition = AcademicEventDefinition.objects.create(
            base_name="Stale removal", start_academic_year=2026
        )
        remove_url = reverse("academic-event-definition-remove", args=(definition.pk,))
        self.client.get(reverse("academic-event-definition-correct", args=(definition.pk,)))
        AcademicEvent.objects.create(
            definition=definition,
            academic_year=2026,
            base_name=definition.base_name,
            expected_quarter=definition.expected_quarter,
            required_participant_count=1,
            resolution=AcademicEvent.Resolution.DATED,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 10),
        )

        response = self.client.post(remove_url, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Cannot remove: this definition has scheduled or retained use.",
        )
        definition.refresh_from_db()
        self.assertTrue(definition.is_active)
        self.assertEqual(AcademicEventDefinitionChange.objects.count(), 0)

    def test_unused_definition_remove_still_deactivates_and_records_history(self):
        definition = AcademicEventDefinition.objects.create(
            base_name="Unused removal", start_academic_year=2026
        )

        response = self.client.post(
            reverse("academic-event-definition-remove", args=(definition.pk,))
        )

        self.assertRedirects(
            response,
            reverse("academic-event-definition-workspace"),
            fetch_redirect_response=False,
        )
        definition.refresh_from_db()
        self.assertFalse(definition.is_active)
        self.assertEqual(
            list(AcademicEventDefinitionChange.objects.values_list("action", flat=True)),
            [AcademicEventDefinitionChange.Action.DEACTIVATED],
        )

    def test_unused_monthly_definition_remove_deactivates_and_retains_history(self):
        definition = AcademicEventDefinition.objects.create(
            base_name="Unused monthly removal",
            scheduling_mode=AcademicEventDefinition.SchedulingMode.MONTHLY_RULES,
            expected_quarter=None,
            all_available_faculty=True,
            start_academic_year=2026,
        )

        response = self.client.post(
            reverse("academic-event-definition-remove", args=(definition.pk,))
        )

        self.assertRedirects(
            response,
            reverse("academic-event-definition-workspace"),
            fetch_redirect_response=False,
        )
        definition.refresh_from_db()
        self.assertFalse(definition.is_active)
        self.assertEqual(
            list(
                AcademicEventDefinitionChange.objects.filter(definition=definition).values_list(
                    "action", flat=True
                )
            ),
            [AcademicEventDefinitionChange.Action.DEACTIVATED],
        )

    def test_no_javascript_create_post_derives_blank_dates_and_coverage(self):
        definition = AcademicEventDefinition.objects.create(
            base_name="Spring planning",
            start_academic_year=2026,
            expected_quarter="Q4",
            default_duration_days=3,
            default_period_of_day=AcademicEvent.PeriodOfDay.PM,
        )

        response = self.client.post(
            reverse("academic-event-create"),
            {
                "definition": definition.pk,
                # No selected-year URL or POST year: exercise the current-year default.
                "start_date": "",
                "end_date": "",
                "period_of_day": "",
            },
        )

        self.assertRedirects(
            response, reverse("academic-event-workspace"), fetch_redirect_response=False
        )
        event = AcademicEvent.objects.get()
        self.assertEqual(event.academic_year, 2026)
        self.assertEqual(event.start_date, date(2027, 4, 1))
        self.assertEqual(event.end_date, date(2027, 4, 3))
        self.assertEqual(event.period_of_day, AcademicEvent.PeriodOfDay.PM)

    def test_no_javascript_create_post_preserves_explicit_dates_and_coverage(self):
        definition = AcademicEventDefinition.objects.create(
            base_name="Orientation",
            start_academic_year=2026,
            expected_quarter="Q1",
            default_duration_days=3,
            default_period_of_day=AcademicEvent.PeriodOfDay.PM,
        )

        response = self.client.post(
            reverse("academic-event-create") + "?academic_year=2026&view=list",
            {
                "definition": definition.pk,
                "academic_year": "2026",
                "start_date": "2026-08-10",
                "end_date": "2026-08-11",
                "period_of_day": AcademicEvent.PeriodOfDay.AM,
            },
        )

        self.assertRedirects(
            response,
            reverse("academic-event-list") + "?academic_year=2026",
            fetch_redirect_response=False,
        )
        event = AcademicEvent.objects.get()
        self.assertEqual(event.start_date, date(2026, 8, 10))
        self.assertEqual(event.end_date, date(2026, 8, 11))
        self.assertEqual(event.period_of_day, AcademicEvent.PeriodOfDay.AM)

    def test_no_javascript_create_post_retains_invalid_supplied_dates(self):
        definition = AcademicEventDefinition.objects.create(
            base_name="Fall planning",
            start_academic_year=2026,
            expected_quarter="Q2",
            default_duration_days=2,
            default_period_of_day=AcademicEvent.PeriodOfDay.AM,
        )

        response = self.client.post(
            reverse("academic-event-create") + "?academic_year=2026&view=list",
            {
                "definition": definition.pk,
                "academic_year": "2026",
                "suffix": "  ",
                "start_date": "not-a-date",
                "end_date": "",
                "period_of_day": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a valid date.")
        self.assertContains(
            response,
            'academic-event-display-preview" aria-labelledby="displayed-as-label" aria-live="polite">Fall planning</output>',
        )
        self.assertNotContains(response, "Fall planning -")
        self.assertContains(response, 'class="form-field-group"><label for="id_start_date">')
        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, '<ul class="errorlist" id="id_start_date_error">')
        self.assertEqual(response.context["form"]["start_date"].value(), "not-a-date")
        self.assertEqual(response.context["form"]["end_date"].value(), "2026-10-02")
        self.assertEqual(
            response.context["form"]["period_of_day"].value(), AcademicEvent.PeriodOfDay.AM
        )
        self.assertEqual(AcademicEvent.objects.count(), 0)

    def test_occurrence_create_hides_and_accepts_an_added_date_range(self):
        definition = AcademicEventDefinition.objects.create(
            base_name="Split planning", start_academic_year=2026
        )
        create_url = reverse("academic-event-create") + "?academic_year=2026&view=list"

        response = self.client.get(f"{create_url}&definition={definition.pk}")

        self.assertContains(response, 'name="additional_ranges-TOTAL_FORMS" value="0"')
        self.assertNotContains(response, 'name="additional_ranges-0-start_date"')
        self.assertContains(response, 'id="academic-event-range-template"')

        response = self.client.post(
            create_url,
            {
                "definition": definition.pk,
                "academic_year": "2026",
                "start_date": "2026-08-10",
                "end_date": "2026-08-10",
                "period_of_day": AcademicEvent.PeriodOfDay.WHOLE_DAY,
                "additional_ranges-TOTAL_FORMS": 1,
                "additional_ranges-INITIAL_FORMS": 0,
                "additional_ranges-MIN_NUM_FORMS": 0,
                "additional_ranges-MAX_NUM_FORMS": 1000,
                "additional_ranges-0-start_date": "2026-08-14",
                "additional_ranges-0-end_date": "2026-08-14",
            },
        )

        self.assertRedirects(
            response,
            reverse("academic-event-list") + "?academic_year=2026",
            fetch_redirect_response=False,
        )
        self.assertEqual(
            AcademicEvent.objects.get().date_ranges,
            ((date(2026, 8, 10), date(2026, 8, 10)), (date(2026, 8, 14), date(2026, 8, 14))),
        )

    def test_occurrence_create_uses_sectioned_form_with_preview_radios_and_quarter_javascript(self):
        definition = AcademicEventDefinition.objects.create(
            base_name="Spring planning",
            start_academic_year=2026,
            expected_quarter="Q4",
            default_duration_days=3,
            default_period_of_day=AcademicEvent.PeriodOfDay.PM,
            default_precepting_impact=AcademicEvent.PreceptingImpact.SOFT,
            default_required_participant_count=3,
        )

        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2026, 8, 1, tzinfo=UTC),
        ):
            response = self.client.get(
                reverse("academic-event-create"),
                {"academic_year": "2026", "definition": definition.pk},
            )

        content = response.content.decode()
        form = response.context["form"]
        self.assertEqual(content.count('name="definition"'), 1)
        self.assertEqual(content.count('name="academic_year"'), 1)
        self.assertNotContains(response, 'class="form-page-kicker"')
        assert_academic_event_action_heading(self, response, "Schedule")
        for heading in ("Event", "Schedule", "Assigned physicians", "Actions"):
            assert_rendered_heading(self, response, heading, level=2)
        self.assertEqual(content.count("Return to Academic Events"), 2)
        self.assertNotContains(response, "Set quarter date defaults")
        self.assertNotContains(response, "Leave blank to keep")
        self.assertContains(response, "Event detail (optional)")
        self.assertContains(response, "Appended to the definition name on schedules.")
        self.assertContains(response, "Event note (optional)")
        self.assertNotContains(
            response, "Part of this event, rather than the history of this change."
        )
        self.assertContains(response, "Full name for this scheduled event.")
        self.assertContains(response, "Dates, participation requirement, and affected coverage.")
        self.assertContains(response, "<legend>Affected Coverage</legend>")
        self.assertContains(
            response, '<label for="id_participants-filter">Filter physicians</label>'
        )
        self.assertContains(response, "<legend>Physicians</legend>")
        self.assertContains(response, "Click on a name to remove it.")
        self.assertContains(response, "form-read-only-value")
        self.assertContains(response, 'class="form-derived-preview"')
        self.assertContains(response, "Displayed as")
        self.assertLess(content.index("Displayed as"), content.index("Event note (optional)"))
        self.assertContains(response, 'id="academic-event-display-preview"')
        self.assertContains(
            response,
            'academic-event-display-preview" aria-labelledby="displayed-as-label" aria-live="polite">Spring planning</output>',
        )
        self.assertContains(response, "data-academic-event-form")
        self.assertContains(response, 'src="/static/schedules/academic-event-form.js"')
        self.assertContains(response, 'name="required_participant_count" value="3"')
        self.assertContains(response, 'name="start_date" value="2027-04-01"')
        self.assertContains(response, 'name="end_date" value="2027-04-03"')
        self.assertContains(response, 'type="radio" name="period_of_day" value="whole_day"')
        self.assertContains(response, 'type="radio" name="period_of_day" value="AM"')
        self.assertRegex(
            content,
            r'type="radio" name="period_of_day" value="PM"[^>]* checked',
        )
        self.assertEqual(form["precepting_impact"].value(), AcademicEvent.PreceptingImpact.SOFT)
        self.assertEqual(
            response.context["event_definition_data"][definition.pk]["precepting_impact"],
            AcademicEvent.PreceptingImpact.SOFT,
        )
        self.assertRegex(
            content,
            r'type="radio" name="precepting_impact" value="soft"[^>]* checked',
        )
        self.assertContains(response, "Whole day")
        self.assertContains(response, "AM only")
        self.assertContains(response, "PM only")
        self.assertEqual(form.fields["academic_year"].choices[0], ("2026", "2026-2027"))
        self.assertEqual(form.fields["academic_year"].choices[-1], ("2036", "2036-2037"))
        self.assertIn('<label for="id_reason">History note (optional)</label>', content)
        self.assertIn("Recorded with this change in history.", content)
        self.assertContains(
            response,
            "Hard makes physicians unavailable; Soft keeps them eligible and records a finding; None has no scheduling effect.",
        )
        self.assertNotContains(response, "Use this instead of selecting individual physicians.")
        self.assertRegex(
            content,
            r'<div class="academic-event-all-faculty">\s*'
            r'<label for="id_all_available_faculty">All available Faculty:</label>\s*'
            r'<input[^>]*name="all_available_faculty"[^>]*>\s*'
            r"<small>Check this or select individual physicians\.</small>",
        )
        self.assertContains(response, "data-academic-event-range-remove")
        self.assertContains(response, "data-academic-event-range-restore")
        self.assertContains(response, "data-physician-picker-all-available")

        script = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "academic-event-form.js"
        ).read_text()
        self.assertIn('definition.addEventListener("change"', script)
        self.assertIn('academicYear.addEventListener("change"', script)
        self.assertIn("selected.duration_days - 1", script)
        self.assertIn("startDate.value = start.toISOString().slice(0, 10)", script)
        self.assertIn("endDate.value = end.toISOString().slice(0, 10)", script)
        self.assertIn("setCoverage", script)
        self.assertIn("setRequiredParticipantCount", script)
        self.assertIn('detail.addEventListener("input"', script)

    def test_academic_event_forms_associate_labels_and_read_only_context(self):
        definition = AcademicEventDefinition.objects.create(
            base_name="Sized fields", start_academic_year=2026
        )
        definition_response = self.client.get(reverse("academic-event-definition-create"))
        occurrence_response = self.client.get(
            reverse("academic-event-create"),
            {"academic_year": "2026", "definition": definition.pk},
        )

        # Geometry is exercised with these actual routes in FormPageIntroBrowserTests.
        for response, fields in (
            (
                definition_response,
                (
                    "base_name",
                    "expected_quarter",
                    "default_duration_days",
                    "default_period_of_day",
                    "default_required_participant_count",
                    "start_academic_year",
                    "end_academic_year",
                ),
            ),
            (
                occurrence_response,
                ("suffix", "required_participant_count", "start_date", "end_date", "reason"),
            ),
        ):
            parser = _RenderedSurfaceParser()
            parser.feed(response.content.decode())
            for name in fields:
                with self.subTest(field=name):
                    controls = [
                        element
                        for element in parser.elements
                        if element.tag in ("input", "select", "textarea")
                        and element.attributes.get("name") == name
                    ]
                    self.assertEqual(len(controls), 1)
                    labels = [
                        element
                        for element in parser.elements
                        if element.tag == "label"
                        and element.attributes.get("for") == controls[0].attributes.get("id")
                    ]
                    self.assertEqual(len(labels), 1)
        parser = _RenderedSurfaceParser()
        parser.feed(occurrence_response.content.decode())
        for label, value in (("Academic year", "2026-2027"), ("Event", "Sized fields")):
            values = [
                element
                for element in parser.elements
                if "form-read-only-value" in element.attributes.get("class", "").split()
                and any(child.text().strip() == label for child in element.descendants())
            ]
            self.assertEqual(len(values), 1)
            self.assertIn(value, values[0].text())

    def test_occurrence_edit_preserves_dates_and_an_out_of_window_academic_year(self):
        definition = AcademicEventDefinition.objects.create(
            base_name="Retained occurrence", start_academic_year=2020, expected_quarter="Q4"
        )
        event = AcademicEvent.objects.create(
            definition=definition,
            academic_year=2020,
            base_name=definition.base_name,
            expected_quarter=definition.expected_quarter,
            required_participant_count=1,
            resolution=AcademicEvent.Resolution.DATED,
            start_date=date(2020, 8, 10),
            end_date=date(2020, 8, 11),
            period_of_day=AcademicEvent.PeriodOfDay.AM,
            precepting_impact=AcademicEvent.PreceptingImpact.SOFT,
        )

        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2026, 8, 1, tzinfo=UTC),
        ):
            response = self.client.get(reverse("academic-event-correct", args=(event.pk,)))

        form = response.context["form"]
        self.assertIn(("2020", "2020-2021"), form.fields["academic_year"].choices)
        self.assertEqual(form["start_date"].value(), date(2020, 8, 10))
        self.assertEqual(form["end_date"].value(), date(2020, 8, 11))
        self.assertEqual(form["period_of_day"].value(), AcademicEvent.PeriodOfDay.AM)
        self.assertEqual(form["precepting_impact"].value(), AcademicEvent.PreceptingImpact.SOFT)
        save_response = self.client.post(
            reverse("academic-event-correct", args=(event.pk,)),
            {
                "suffix": event.suffix,
                "required_participant_count": event.required_participant_count,
                "start_date": event.start_date.isoformat(),
                "end_date": event.end_date.isoformat(),
                "period_of_day": event.period_of_day,
                "precepting_impact": event.precepting_impact,
                "reason": "",
            },
        )

        self.assertEqual(save_response.status_code, 302)
        event.refresh_from_db()
        self.assertEqual(event.precepting_impact, AcademicEvent.PreceptingImpact.SOFT)
        self.assertContains(response, "academic-event-form.js")
        self.assertContains(response, "data-academic-event-form")
        self.assertContains(response, 'src="/static/schedules/physicians-picker.js"')
        self.assertNotContains(response, 'class="form-page-kicker"')
        assert_academic_event_action_heading(self, response, "Edit")
        self.assertContains(response, "Full name for this scheduled event.")
        self.assertContains(response, "Dates, participation requirement, and affected coverage.")
        self.assertContains(response, "<legend>Affected Coverage</legend>")
        self.assertContains(
            response, '<h2 id="physicians-section-heading">Assigned physicians</h2>'
        )
        self.assertContains(
            response, '<label for="id_participants-filter">Filter physicians</label>'
        )
        self.assertContains(response, "<legend>Physicians</legend>")
        self.assertContains(response, "Click on a name to remove it.")
        self.assertContains(response, "form-read-only-value")
        self.assertContains(response, '<button type="submit">Save Academic Event</button>')
        self.assertContains(response, "Return to Academic Events")
        assert_removal_section(
            self,
            response,
            "academic-event-removal-heading",
            reverse("academic-event-remove", args=(event.pk,)),
        )
        self.assertContains(response, 'aria-labelledby="academic-event-removal-heading"')
        self.assertContains(response, 'class="surface-header"')
        self.assertContains(response, 'class="surface-body"')
        self.assertContains(
            response, 'class="form-action--destructive" type="submit">Remove Academic Event'
        )
        self.assertContains(
            response,
            f'action="{reverse("academic-event-remove", args=(event.pk,))}?',
        )
        self.assertContains(response, '<label for="remove-reason">History note (optional)</label>')
        self.assertContains(response, "Recorded with this change in history.")
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()
        self.assertNotIn(".academic-event-removal {", stylesheet)

    def test_invalid_occurrence_removal_preserves_saved_precepting_impact(self):
        for impact in (
            AcademicEvent.PreceptingImpact.SOFT,
            AcademicEvent.PreceptingImpact.NONE,
        ):
            with self.subTest(impact=impact):
                definition = AcademicEventDefinition.objects.create(
                    base_name=f"Retained {impact} removal impact", start_academic_year=2026
                )
                event = AcademicEvent.objects.create(
                    definition=definition,
                    academic_year=2026,
                    base_name=definition.base_name,
                    expected_quarter=definition.expected_quarter,
                    required_participant_count=1,
                    resolution=AcademicEvent.Resolution.DATED,
                    start_date=date(2026, 8, 10),
                    end_date=date(2026, 8, 10),
                    period_of_day=AcademicEvent.PeriodOfDay.AM,
                    precepting_impact=impact,
                )

                with patch(
                    "schedules.views.academic_events.AcademicEventRemovalForm.is_valid",
                    return_value=False,
                ):
                    response = self.client.post(
                        reverse("academic-event-remove", args=(event.pk,)), {"reason": "Retain"}
                    )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["form"]["precepting_impact"].value(), impact)
                event.refresh_from_db()
                self.assertEqual(event.precepting_impact, impact)

    def test_one_range_edit_post_without_additional_range_management_fields_is_valid(self):
        definition = AcademicEventDefinition.objects.create(
            base_name="One range correction", start_academic_year=2026
        )
        event = AcademicEvent.objects.create(
            definition=definition,
            academic_year=2026,
            base_name=definition.base_name,
            expected_quarter=definition.expected_quarter,
            required_participant_count=1,
            resolution=AcademicEvent.Resolution.DATED,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 10),
        )

        response = self.client.post(
            reverse("academic-event-correct", args=(event.pk,)),
            {
                "suffix": "Corrected",
                "required_participant_count": "1",
                "start_date": "2026-08-11",
                "end_date": "2026-08-11",
                "period_of_day": AcademicEvent.PeriodOfDay.WHOLE_DAY,
                "reason": "",
            },
        )

        self.assertRedirects(
            response, reverse("academic-event-workspace"), fetch_redirect_response=False
        )
        event.refresh_from_db()
        self.assertEqual(event.title, "One range correction - Corrected")
        self.assertEqual(event.date_ranges, ((date(2026, 8, 11), date(2026, 8, 11)),))

    def test_multi_range_edit_post_without_management_fields_preserves_ranges(self):
        definition = AcademicEventDefinition.objects.create(
            base_name="Multi-range correction", start_academic_year=2026
        )
        event = AcademicEvent.objects.create(
            definition=definition,
            academic_year=2026,
            base_name=definition.base_name,
            expected_quarter=definition.expected_quarter,
            required_participant_count=1,
            resolution=AcademicEvent.Resolution.DATED,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 14),
        )
        AcademicEventDateRange.objects.bulk_create(
            (
                AcademicEventDateRange(
                    event=event, start_date=date(2026, 8, 10), end_date=date(2026, 8, 10)
                ),
                AcademicEventDateRange(
                    event=event, start_date=date(2026, 8, 14), end_date=date(2026, 8, 14)
                ),
            )
        )

        response = self.client.post(
            reverse("academic-event-correct", args=(event.pk,)),
            {
                "suffix": "Corrected",
                "required_participant_count": "1",
                "start_date": "2026-08-11",
                "end_date": "2026-08-11",
                "period_of_day": AcademicEvent.PeriodOfDay.WHOLE_DAY,
                "reason": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "Additional date ranges are required when editing this event."
        )
        event.refresh_from_db()
        self.assertEqual(
            event.date_ranges,
            ((date(2026, 8, 10), date(2026, 8, 10)), (date(2026, 8, 14), date(2026, 8, 14))),
        )

    def test_occurrence_edit_round_trips_disjoint_ranges_without_overlapping_outer_bounds(self):
        definition = AcademicEventDefinition.objects.create(
            base_name="Split occurrence", start_academic_year=2026
        )
        event = AcademicEvent.objects.create(
            definition=definition,
            academic_year=2026,
            base_name=definition.base_name,
            expected_quarter=definition.expected_quarter,
            required_participant_count=1,
            resolution=AcademicEvent.Resolution.DATED,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 14),
        )
        AcademicEventDateRange.objects.bulk_create(
            (
                AcademicEventDateRange(
                    event=event, start_date=date(2026, 8, 10), end_date=date(2026, 8, 10)
                ),
                AcademicEventDateRange(
                    event=event, start_date=date(2026, 8, 14), end_date=date(2026, 8, 14)
                ),
            )
        )
        edit_url = reverse("academic-event-correct", args=(event.pk,))

        response = self.client.get(edit_url)

        self.assertContains(response, "data-academic-event-form")
        self.assertEqual(response.context["form"]["start_date"].value(), date(2026, 8, 10))
        self.assertEqual(response.context["form"]["end_date"].value(), date(2026, 8, 10))
        self.assertEqual(len(response.context["additional_ranges"].forms), 1)
        self.assertEqual(
            response.context["additional_ranges"].forms[0]["start_date"].value(), date(2026, 8, 14)
        )

        response = self.client.post(
            edit_url,
            {
                "definition": definition.pk,
                "academic_year": 2026,
                "suffix": "",
                "start_date": "2026-08-10",
                "end_date": "2026-08-10",
                "required_participant_count": 1,
                "period_of_day": AcademicEvent.PeriodOfDay.WHOLE_DAY,
                "reason": "",
                "additional_ranges-TOTAL_FORMS": 2,
                "additional_ranges-INITIAL_FORMS": 1,
                "additional_ranges-MIN_NUM_FORMS": 0,
                "additional_ranges-MAX_NUM_FORMS": 1000,
                "additional_ranges-0-start_date": "2026-08-14",
                "additional_ranges-0-end_date": "2026-08-14",
                "additional_ranges-1-start_date": "",
                "additional_ranges-1-end_date": "",
            },
        )

        self.assertRedirects(
            response, reverse("academic-event-workspace"), fetch_redirect_response=False
        )
        event.refresh_from_db()
        self.assertEqual(
            event.date_ranges,
            ((date(2026, 8, 10), date(2026, 8, 10)), (date(2026, 8, 14), date(2026, 8, 14))),
        )

    def test_occurrence_edit_removes_a_submitted_additional_range(self):
        definition = AcademicEventDefinition.objects.create(
            base_name="Range removal", start_academic_year=2026
        )
        event = AcademicEvent.objects.create(
            definition=definition,
            academic_year=2026,
            base_name=definition.base_name,
            expected_quarter=definition.expected_quarter,
            required_participant_count=1,
            resolution=AcademicEvent.Resolution.DATED,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 14),
        )
        AcademicEventDateRange.objects.bulk_create(
            (
                AcademicEventDateRange(
                    event=event, start_date=date(2026, 8, 10), end_date=date(2026, 8, 10)
                ),
                AcademicEventDateRange(
                    event=event, start_date=date(2026, 8, 14), end_date=date(2026, 8, 14)
                ),
            )
        )

        response = self.client.post(
            reverse("academic-event-correct", args=(event.pk,)),
            {
                "definition": definition.pk,
                "academic_year": 2026,
                "suffix": "",
                "start_date": "2026-08-10",
                "end_date": "2026-08-10",
                "required_participant_count": 1,
                "period_of_day": AcademicEvent.PeriodOfDay.WHOLE_DAY,
                "reason": "",
                "additional_ranges-TOTAL_FORMS": 1,
                "additional_ranges-INITIAL_FORMS": 1,
                "additional_ranges-MIN_NUM_FORMS": 0,
                "additional_ranges-MAX_NUM_FORMS": 1000,
                "additional_ranges-0-start_date": "2026-08-14",
                "additional_ranges-0-end_date": "2026-08-14",
                "additional_ranges-0-DELETE": "on",
            },
        )

        self.assertRedirects(
            response, reverse("academic-event-workspace"), fetch_redirect_response=False
        )
        event.refresh_from_db()
        self.assertEqual(event.date_ranges, ((date(2026, 8, 10), date(2026, 8, 10)),))

    def test_physicians_picker_submits_native_choices_and_restores_selection(self):
        first = Physician.objects.create(display_name="Avery Participant")
        second = Physician.objects.create(display_name="Blair Participant")
        third = Physician.objects.create(
            display_name="Long participating physician name for wrapping"
        )
        definition = AcademicEventDefinition.objects.create(
            base_name="Physicians picker", start_academic_year=2026
        )
        create_url = reverse("academic-event-create") + "?academic_year=2026&view=list"

        response = self.client.get(f"{create_url}&definition={definition.pk}")

        def participant_controls(rendered_response):
            parser = _RenderedSurfaceParser()
            parser.feed(rendered_response.content.decode())
            forms = [
                element
                for element in parser.elements
                if element.tag == "form"
                and "academic-event-form" in element.attributes.get("class", "").split()
            ]
            self.assertEqual(len(forms), 1)
            controls = [
                element
                for element in forms[0].descendants()
                if element.tag == "input"
                and element.attributes.get("type") == "checkbox"
                and element.attributes.get("name") == "participants"
            ]
            labels = {
                element.attributes.get("for"): " ".join(element.text().split())
                for element in forms[0].descendants()
                if element.tag == "label"
            }
            return {control.attributes["value"]: control for control in controls}, labels

        controls, labels = participant_controls(response)
        for physician in (first, second, third):
            with self.subTest(physician=physician):
                control = controls[str(physician.pk)]
                self.assertNotIn("checked", control.attributes)
                self.assertEqual(labels[control.attributes["id"]], physician.display_name)

        response = self.client.post(
            create_url,
            {
                "definition": definition.pk,
                "academic_year": "2026",
                "start_date": "2026-08-10",
                "end_date": "2026-08-10",
                "period_of_day": AcademicEvent.PeriodOfDay.AM,
                "participants": [first.pk, third.pk],
            },
        )

        self.assertRedirects(
            response,
            reverse("academic-event-list") + "?academic_year=2026",
            fetch_redirect_response=False,
        )
        event = AcademicEvent.objects.get()
        self.assertEqual(set(event.participants.values_list("pk", flat=True)), {first.pk, third.pk})

        edit_response = self.client.get(reverse("academic-event-correct", args=(event.pk,)))
        controls, labels = participant_controls(edit_response)
        for physician, expected_checked in ((first, True), (second, False), (third, True)):
            with self.subTest(physician=physician):
                control = controls[str(physician.pk)]
                self.assertEqual("checked" in control.attributes, expected_checked)
                self.assertEqual(labels[control.attributes["id"]], physician.display_name)

    def test_physicians_picker_preserves_checked_choices_on_an_invalid_response(self):
        first = Physician.objects.create(display_name="First participant")
        second = Physician.objects.create(display_name="Second participant")
        unselected = Physician.objects.create(display_name="Unselected participant")
        definition = AcademicEventDefinition.objects.create(
            base_name="Invalid physicians picker", start_academic_year=2026
        )

        response = self.client.post(
            reverse("academic-event-create") + "?academic_year=2026&view=list",
            {
                "definition": definition.pk,
                "academic_year": "2026",
                "start_date": "2026-08-11",
                "end_date": "2026-08-10",
                "period_of_day": AcademicEvent.PeriodOfDay.WHOLE_DAY,
                "participants": [first.pk, second.pk],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("end_date", response.context["form"].errors)
        parser = _RenderedSurfaceParser()
        parser.feed(response.content.decode())
        forms = [
            element
            for element in parser.elements
            if element.tag == "form"
            and "academic-event-form" in element.attributes.get("class", "").split()
        ]
        self.assertEqual(len(forms), 1)
        controls = {
            control.attributes["value"]: control
            for control in forms[0].descendants()
            if control.tag == "input"
            and control.attributes.get("type") == "checkbox"
            and control.attributes.get("name") == "participants"
        }
        for physician, expected_checked in ((first, True), (second, True), (unselected, False)):
            with self.subTest(physician=physician):
                self.assertEqual(
                    "checked" in controls[str(physician.pk)].attributes, expected_checked
                )
        self.assertEqual(AcademicEvent.objects.count(), 0)

    def test_form_templates_do_not_repeat_bound_field_group_markup(self):
        templates = Path(__file__).resolve().parents[1] / "templates" / "schedules"
        duplicate_bound_field_group = re.compile(
            r'<div class="form-field-group(?: [^"]*)?">\s*<label for="\{\{ '
            r'(?:form|remove_form|[a-z_]+_form)\.[^}]+\.id_for_label \}\}">'
        )

        for template in templates.glob("*.html"):
            with self.subTest(template=template.name):
                self.assertNotRegex(template.read_text(), duplicate_bound_field_group)

    def test_action_section_heading_inventory_uses_the_shared_label(self):
        templates = Path(__file__).resolve().parents[1] / "templates" / "schedules"
        contextual_action_heading = re.compile(
            r"<h[1-6][^>]*>\s*(?!Actions\s*</h[1-6]>)[^<{]*\bactions\s*</h[1-6]>",
            re.IGNORECASE,
        )
        contextual_action_title = re.compile(r'action_title="[^"]+\sactions"', re.IGNORECASE)

        for template in templates.glob("*.html"):
            with self.subTest(template=template.name):
                content = template.read_text()
                self.assertNotRegex(content, contextual_action_heading)
                self.assertNotRegex(content, contextual_action_title)

    def test_colliding_definition_and_event_ids_use_their_own_correction_routes(self):
        participant = Physician.objects.create(display_name="Event participant")
        definition = AcademicEventDefinition.objects.create(
            base_name="Definition", start_academic_year=2026
        )
        event = AcademicEvent.objects.create(
            definition=definition,
            academic_year=2026,
            base_name="Dated event",
            required_participant_count=1,
            resolution=AcademicEvent.Resolution.DATED,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 10),
        )
        event.participants.add(participant)
        self.assertEqual(event.pk, definition.pk)

        event_response = self.client.get(reverse("academic-event-correct", args=(event.pk,)))
        definition_response = self.client.get(
            reverse("academic-event-definition-correct", args=(definition.pk,))
        )

        self.assertContains(event_response, "Edit Academic Event")
        self.assertContains(definition_response, "Edit Event definition")

        response = self.client.post(
            reverse("academic-event-correct", args=(event.pk,)),
            {
                "definition": definition.pk,
                "academic_year": "2026",
                "suffix": "Corrected dated event",
                "required_participant_count": "1",
                "start_date": "2026-08-10",
                "end_date": "2026-08-10",
                "period_of_day": AcademicEvent.PeriodOfDay.WHOLE_DAY,
                "participants": [participant.pk],
                "reason": "Correct dated event",
            },
        )
        self.assertRedirects(
            response, reverse("academic-event-workspace"), fetch_redirect_response=False
        )
        event.refresh_from_db()
        definition.refresh_from_db()
        self.assertEqual(event.title, "Dated event - Corrected dated event")
        self.assertEqual(definition.base_name, "Definition")

        response = self.client.post(
            reverse("academic-event-definition-correct", args=(definition.pk,)),
            {
                "base_name": "Corrected definition",
                "expected_quarter": "Q4",
                "default_duration_days": "1",
                "default_period_of_day": AcademicEvent.PeriodOfDay.WHOLE_DAY,
                "start_academic_year": "2026",
                "end_academic_year": "",
            },
        )
        self.assertRedirects(
            response, reverse("academic-event-definition-workspace"), fetch_redirect_response=False
        )
        event.refresh_from_db()
        definition.refresh_from_db()
        self.assertEqual(event.title, "Dated event - Corrected dated event")
        self.assertEqual(definition.base_name, "Corrected definition")


class AcademicEventWorkspaceTests(TestCase):
    def setUp(self):
        # AY 2026-2027 is current and Q1 is still operational. Pin the upstream
        # clock, not year selection or membership, so all projections agree.
        # Method-level clocks can still establish historical/alternate scenarios.
        self.enterContext(
            patch(
                "schedules.services.operational_attention.timezone.now",
                return_value=datetime(2026, 8, 1, 18, tzinfo=UTC),
            )
        )
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.administrator = get_user_model().objects.create_user(
            username="event-administrator", password="test-password"
        )
        self.administrator.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.physician = Physician.objects.create(display_name="Event participant")
        self.client.force_login(self.user)

    def definition(self, name, quarter="Q1", **kwargs):
        values = {"base_name": name, "start_academic_year": 2026, "expected_quarter": quarter}
        values.update(kwargs)
        return AcademicEventDefinition.objects.create(**values)

    def occurrence(self, definition, start, end, participants=(), required=1, **kwargs):
        values = {
            "definition": definition,
            "academic_year": 2026,
            "base_name": definition.base_name,
            "expected_quarter": definition.expected_quarter,
            "required_participant_count": required,
            "resolution": AcademicEvent.Resolution.DATED,
            "start_date": start,
            "end_date": end,
        }
        values.update(kwargs)
        event = AcademicEvent.objects.create(**values)
        event.participants.set(participants)
        return event

    def add_choices(self, response):
        choices = re.search(
            r'<select id="event-definition" name="definition">(.*?)</select>',
            response.content.decode(),
            re.DOTALL,
        ).group(1)
        return re.findall(r'<option value="(\d+)">([^<]+)</option>', choices)

    def rendered_academic_event_table(self, content, identity):
        return _RenderedDataTable(content, identity)

    def rendered_resolution_table(self, content, section_heading):
        parser = _RenderedTableParser()
        parser.feed(content)
        headings = [
            heading
            for heading in parser.root.descendants_named("h1", "h2", "h3", "h4", "h5", "h6")
            if heading.text().strip() == section_heading
        ]
        self.assertEqual(len(headings), 1, f"Missing section {section_heading!r}.")
        section = next(
            ancestor for ancestor in headings[0].ancestors() if ancestor.tag == "section"
        )
        tables = section.descendants_named("table")
        self.assertEqual(len(tables), 1, f"Expected one table in {section_heading!r}.")
        return tables[0]

    def academic_event_section_headings(self, content):
        parser = _RenderedTableParser()
        parser.feed(content)
        return [
            heading.text().strip()
            for heading in parser.root.descendants_named("h1", "h2", "h3", "h4", "h5", "h6")
            if any(ancestor.tag == "section" for ancestor in heading.ancestors())
        ]

    @staticmethod
    def rendered_table_rows(table):
        return table.children_named("tbody")[0].children_named("tr")

    @staticmethod
    def rendered_table_cell(table, row, header):
        headers = [
            item.text().strip()
            for item in table.children_named("thead")[0]
            .children_named("tr")[0]
            .children_named("th")
        ]
        return row.children_named("th", "td")[headers.index(header)]

    def assert_empty_yearly_resolution_table(self, table, *, category_terms, academic_year):
        rows = self.rendered_table_rows(table)
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

        headers = table.children_named("thead")[0].children_named("tr")[0].children_named("th")
        base_name_index = [header.text().strip() for header in headers].index("Base name")

        def row_has_base_name_record(row):
            cells = row.children_named("th", "td")
            if any("empty" in cell.attributes.get("class", "").split() for cell in cells):
                return False
            if len(cells) <= base_name_index:
                return False
            return bool(cells[base_name_index].text().strip())

        self.assertTrue(
            any(has_empty_message(message) for message in messages),
            f"Expected an empty {category_terms!r} yearly resolution message for {academic_year}.",
        )
        self.assertFalse(
            any(row_has_base_name_record(row) for row in rows),
            "An empty yearly resolution table must not contain a record identity.",
        )

    def academic_event_occurrence_row(self, table, occurrence):
        action_url = f"{reverse('academic-event-correct', args=(occurrence.pk,))}?academic_year=2026&view=list"
        rows = [
            row
            for row in table.body_rows()
            if table.cell_for_header(row, "Event").text().strip() == occurrence.title
            and any(
                _action_matches(link.attributes.get("href", ""), action_url)
                for link in table.cell_for_header(row, "Actions").descendants_named("a")
            )
        ]
        self.assertEqual(len(rows), 1, f"Expected one row for {occurrence.title!r}.")
        return rows[0]

    @staticmethod
    def table_cell_text(table, row, header):
        return table.cell_for_header(row, header).text().strip()

    def test_workspace_context_keeps_domain_projections_out_of_the_shared_namespace(self):
        views = (
            (
                reverse("academic-event-workspace") + "?academic_year=2026&month=2026-08",
                "academic_event_calendar_weeks",
            ),
            (reverse("academic-event-list") + "?academic_year=2026", "academic_event_dated_events"),
            (
                reverse("academic-event-definition-workspace") + "?academic_year=2026",
                "academic_event_definitions",
            ),
        )

        for url, domain_value in views:
            with self.subTest(url=url):
                response = self.client.get(url)
                context = response.context[0].flatten()

                self.assertEqual(response.status_code, 200)
                self.assertIn(domain_value, context)
                self.assertIn("schedule_workspace_page_title", context)
                self.assertIn("schedule_workspace_navigation", context)
                self.assertNotIn("current_view", context)
                self.assertNotIn("add_action", context)

        calendar_context = (
            self.client.get(
                reverse("academic-event-workspace"), {"academic_year": "2026", "month": "2026-08"}
            )
            .context[0]
            .flatten()
        )
        list_context = (
            self.client.get(reverse("academic-event-list"), {"academic_year": "2026"})
            .context[0]
            .flatten()
        )
        self.assertIn("schedule_workspace_selected_month", calendar_context)
        self.assertNotIn("schedule_workspace_selected_month", list_context)

    def test_workspace_navigation_has_one_authority_aware_destination_set(self):
        ordinary_views = (
            (reverse("academic-event-workspace") + "?academic_year=2026&month=2026-08", "Calendar"),
            (reverse("academic-event-list") + "?academic_year=2026", "List"),
            (reverse("academic-event-definition-workspace") + "?academic_year=2026", "Definitions"),
        )
        administrator_views = (
            *ordinary_views,
            (
                reverse("academic-event-definition-import") + "?academic_year=2026",
                "Import Definitions",
            ),
            (
                reverse("academic-event-schedule-import") + "?academic_year=2026",
                "Import Schedules",
            ),
        )

        for user, views in (
            (self.user, ordinary_views),
            (self.administrator, administrator_views),
        ):
            self.client.force_login(user)
            for url, current_view in views:
                with self.subTest(username=user.username, current_view=current_view):
                    response = self.client.get(url)
                    navigation = response.context[0]["schedule_workspace_navigation"]
                    calendar_url = reverse("academic-event-workspace") + "?academic_year=2026"
                    if current_view == "Calendar":
                        calendar_url += "&month=2026-08"
                    expected_navigation = [
                        ("Calendar", calendar_url),
                        ("List", reverse("academic-event-list") + "?academic_year=2026"),
                        (
                            "Definitions",
                            reverse("academic-event-definition-workspace") + "?academic_year=2026",
                        ),
                    ]
                    if user == self.administrator:
                        expected_navigation.extend(
                            (
                                (
                                    "Import Definitions",
                                    reverse("academic-event-definition-import")
                                    + "?academic_year=2026",
                                ),
                                (
                                    "Import Schedules",
                                    reverse("academic-event-schedule-import")
                                    + "?academic_year=2026",
                                ),
                            )
                        )
                    self.assertEqual(
                        [item["label"] for item in navigation if item["current"]], [current_view]
                    )
                    rendered_links = assert_rendered_navigation(
                        self,
                        response,
                        aria_label="Academic Events navigation",
                        expected=expected_navigation,
                        current=current_view,
                    )

                    if user == self.user:
                        parser = _RenderedSurfaceParser()
                        parser.feed(response.content.decode())
                        rendered_labels = [
                            " ".join(link.text().split())
                            for element in parser.elements
                            if element.tag == "nav"
                            and element.attributes.get("aria-label") == "Academic Events navigation"
                            for link in element.descendants()
                            if link.tag == "a"
                        ]
                        self.assertNotIn("Import Definitions", rendered_labels)
                        self.assertNotIn("Import Schedules", rendered_labels)

                    for link, (label, _) in zip(rendered_links, expected_navigation, strict=True):
                        with self.subTest(destination=label):
                            destination = self.client.get(link.attributes["href"])
                            self.assertEqual(destination.status_code, 200)
                            destination_navigation = destination.context[0][
                                "schedule_workspace_navigation"
                            ]
                            self.assertEqual(
                                [
                                    entry["label"]
                                    for entry in destination_navigation
                                    if entry["current"]
                                ],
                                [label],
                            )
                            assert_rendered_active_navigation(
                                self,
                                destination,
                                aria_label="Academic Events navigation",
                                current=label,
                            )
                            if "month=2026-08" in link.attributes["href"]:
                                self.assertEqual(
                                    destination.context[0]["academic_event_selected_month"],
                                    date(2026, 8, 1),
                                )

        self.client.force_login(self.user)
        for destination in (
            reverse("academic-event-definition-import"),
            reverse("academic-event-schedule-import"),
        ):
            with self.subTest(unauthorized_destination=destination):
                response = self.client.get(destination)
                self.assertEqual(response.status_code, 403)

    def test_workspace_views_render_local_navigation_then_year_picker_without_a_heading_row(self):
        views = (
            (reverse("academic-event-workspace") + "?academic_year=2026&month=2026-08", "Calendar"),
            (reverse("academic-event-list") + "?academic_year=2026", "List"),
            (reverse("academic-event-definition-workspace") + "?academic_year=2026", "Definitions"),
        )

        for url, current_view in views:
            with self.subTest(current_view=current_view):
                response = self.client.get(url)
                content = response.content.decode()
                self.assertContains(response, 'class="shell page-content"')
                self.assertTemplateUsed(response, "schedules/schedule_workspace_base.html")
                self.assertTemplateUsed(response, "schedules/schedule_workspace_header.html")
                self.assertTemplateUsed(response, "schedules/schedule_workspace_year_picker.html")
                header = re.search(
                    r'<header class="schedule-workspace-header">(.*?)</header>',
                    content,
                    re.DOTALL,
                ).group(1)
                self.assertIn('class="workspace-local-navigation"', header)
                self.assertNotIn('class="workspace-heading"', header)
                self.assertLess(
                    content.index('class="workspace-local-navigation"'),
                    content.index("schedule-workspace-year-picker"),
                )
                self.assertNotIn('workspace-heading-label">Academic year:', content)
                self.assertNotIn('workspace-heading-value">2026-2027', content)
                self.assertNotIn('workspace-heading-label">Academic Events', content)
                self.assertNotIn("Showing definitions effective for", content)

    def test_definition_workspace_places_helper_before_clean_action_row(self):
        response = self.client.get(
            reverse("academic-event-definition-workspace"), {"academic_year": "2026"}
        )
        content = response.content.decode()
        helper = (
            "Definitions name the Academic Events expected in each academic year. "
            "Scheduling their annual occurrences happens separately."
        )
        action_row = re.search(
            r'<nav class="contextual-action-row" aria-label="Definition actions">(.*?)</nav>',
            content,
            re.DOTALL,
        ).group(1)

        self.assertLess(content.index("schedule-workspace-year-picker"), content.index(helper))
        self.assertLess(content.index(helper), content.index('class="contextual-action-row"'))
        self.assertLess(
            content.index("</form>", content.index("schedule-workspace-year-picker")),
            content.index(helper),
        )
        self.assertIn(
            f'<a class="button-link" href="{reverse("academic-event-definition-create")}">'
            "Add Event Definition</a>",
            action_row,
        )
        self.assertNotIn("Import definitions", action_row)
        self.assertNotIn("Import annual schedules", action_row)
        self.assertIn(
            f'<a href="{reverse("academic-event-definition-history")}">Definition History</a>',
            action_row,
        )

    def test_calendar_picker_shows_the_current_eleven_academic_years_and_no_script_fallbacks(self):
        response = self.client.get(reverse("academic-event-workspace"))

        self.assertEqual(response.context["academic_year"], 2026)
        self.assertEqual(
            response.context["schedule_workspace_year_form"].fields["academic_year"].choices,
            [(str(year), f"{year}-{year + 1}") for year in range(2021, 2032)],
        )
        self.assertEqual(response.context["schedule_workspace_month_min"], date(2021, 7, 1))
        self.assertEqual(response.context["schedule_workspace_month_max"], date(2032, 6, 1))
        content = response.content.decode()
        enhanced_content = re.sub(r"<noscript>.*?</noscript>", "", content, flags=re.DOTALL)
        self.assertNotIn(">View</button>", enhanced_content)
        self.assertEqual(
            content.count('<noscript><button type="submit">View</button></noscript>'), 2
        )

    def test_workspace_views_share_year_picker_options_and_enhancement_hooks(self):
        views = (
            (reverse("academic-event-workspace"), "calendar"),
            (reverse("academic-event-list"), "list"),
            (reverse("academic-event-definition-workspace"), "definitions"),
        )
        expected_choices = [(str(year), f"{year}-{year + 1}") for year in range(2021, 2032)]

        with patch("schedules.views.academic_events._current_academic_year", return_value=2026):
            for url, view in views:
                with self.subTest(view=view):
                    response = self.client.get(url)

                    self.assertEqual(
                        response.context["schedule_workspace_year_form"]
                        .fields["academic_year"]
                        .choices,
                        expected_choices,
                    )
                    self.assertTemplateUsed(
                        response, "schedules/schedule_workspace_year_picker.html"
                    )
                    self.assertContains(response, "data-calendar-period-picker")
                    self.assertContains(response, "schedules/calendar-period-picker.js")
                    self.assertContains(
                        response, '<noscript><button type="submit">View</button></noscript>'
                    )
                    if view != "definitions":
                        self.assertTemplateUsed(
                            response,
                            "schedules/schedule_workspace_academic_year_navigation.html",
                        )
                        self.assertContains(response, 'aria-label="Academic year navigation"')
                        self.assertContains(
                            response,
                            'class="toolbar period-navigation period-navigation-cluster '
                            'period-navigation--academic-year" '
                            'aria-label="Academic year navigation">',
                        )

        calendar = self.client.get(
            reverse("academic-event-workspace"), {"academic_year": "2026", "month": "2027-01"}
        )
        self.assertTemplateUsed(
            calendar, "schedules/schedule_workspace_academic_year_navigation.html"
        )
        self.assertContains(calendar, "data-calendar-period-picker-sync-academic-year")
        self.assertContains(calendar, "data-calendar-period-picker")
        self.assertContains(calendar, "Academic Year:")
        self.assertContains(calendar, 'aria-label="Academic year navigation"')
        self.assertContains(calendar, '<option value="2026" selected>2026&ndash;2027</option>')
        self.assertContains(calendar, 'name="picker" value="academic_year"')
        self.assertContains(calendar, 'name="month" value="2027-01"')
        self.assertContains(
            calendar, 'href="/academic-events/?academic_year=2025&amp;month=2026-01"'
        )
        self.assertContains(
            calendar, 'href="/academic-events/?academic_year=2027&amp;month=2028-01"'
        )
        self.assertTemplateUsed(
            calendar, "schedules/schedule_workspace_academic_year_month_navigation.html"
        )
        self.assertContains(
            calendar,
            'class="schedule-workspace-period-controls '
            'schedule-workspace-period-controls--academic-year-month"',
        )
        self.assertContains(calendar, 'aria-label="Month navigation"')
        self.assertContains(calendar, "Month:")
        self.assertContains(
            calendar, 'href="/academic-events/?academic_year=2026&amp;month=2026-12"'
        )
        self.assertContains(
            calendar, 'href="/academic-events/?academic_year=2026&amp;month=2027-02"'
        )
        self.assertContains(
            calendar, 'action="/academic-events/" method="get" data-calendar-period-picker'
        )
        self.assertContains(calendar, 'name="picker" value="month"')

    def test_calendar_month_navigation_coordinates_year_picker_and_bounded_months(self):
        with patch("schedules.views.academic_events._current_academic_year", return_value=2026):
            june = self.client.get(
                reverse("academic-event-workspace"),
                {"academic_year": "2025", "month": "2026-06"},
            )
            july = self.client.get(
                reverse("academic-event-workspace"),
                {"academic_year": "2026", "month": "2026-07"},
            )
            picker = self.client.get(
                reverse("academic-event-workspace"),
                {"academic_year": "2025", "picker": "month", "month": "2026-07"},
            )
            year_picker = self.client.get(
                reverse("academic-event-workspace"),
                {"academic_year": "2026", "picker": "academic_year", "month": "2026-06"},
            )
            lower = self.client.get(
                reverse("academic-event-workspace"),
                {"academic_year": "2021", "month": "2021-07"},
            )
            upper = self.client.get(
                reverse("academic-event-workspace"),
                {"academic_year": "2031", "month": "2032-06"},
            )

        self.assertEqual(june.status_code, 200)
        self.assertContains(june, 'href="/academic-events/?academic_year=2026&amp;month=2026-07"')
        self.assertEqual(july.context["academic_year"], 2026)
        self.assertEqual(july.context["schedule_workspace_selected_month"], date(2026, 7, 1))
        self.assertContains(july, '<option value="2026" selected>2026&ndash;2027</option>')
        self.assertContains(july, 'href="/academic-events/?academic_year=2025&amp;month=2026-06"')
        self.assertEqual(picker.context["academic_year"], 2026)
        self.assertEqual(picker.context["schedule_workspace_selected_month"], date(2026, 7, 1))
        self.assertEqual(year_picker.context["academic_year"], 2026)
        self.assertEqual(year_picker.context["schedule_workspace_selected_month"], date(2027, 6, 1))
        self.assertContains(
            lower,
            '<span role="link" aria-disabled="true" aria-label="Previous month">&lsaquo;</span>',
        )
        self.assertContains(
            upper,
            '<span role="link" aria-disabled="true" aria-label="Next month">&rsaquo;</span>',
        )

    def test_academic_year_cluster_bounds_navigation_to_its_picker_options(self):
        with patch("schedules.views.academic_events._current_academic_year", return_value=2026):
            calendar = self.client.get(
                reverse("academic-event-workspace"),
                {"academic_year": "2026", "month": "2027-02"},
            )
            listing = self.client.get(reverse("academic-event-list"), {"academic_year": "2026"})

            for response, action in (
                (calendar, reverse("academic-event-workspace")),
                (listing, reverse("academic-event-list")),
            ):
                with self.subTest(action=action):
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, 'aria-label="Academic year navigation"')
                    self.assertContains(
                        response,
                        f'<form class="period-navigation-form compact-inline-control-row schedule-workspace-year-picker" action="{action}" method="get" data-calendar-period-picker',
                    )
                    self.assertContains(
                        response, '<noscript><button type="submit">View</button></noscript>'
                    )
                    self.assertContains(
                        response, '<option value="2026" selected>2026&ndash;2027</option>'
                    )

            self.assertContains(
                calendar, 'href="/academic-events/?academic_year=2025&amp;month=2026-02"'
            )
            self.assertContains(
                calendar, 'href="/academic-events/?academic_year=2027&amp;month=2028-02"'
            )
            self.assertContains(listing, 'href="/academic-events/list/?academic_year=2025"')
            self.assertContains(listing, 'href="/academic-events/list/?academic_year=2027"')

            previous_calendar = self.client.get(
                reverse("academic-event-workspace"),
                {"academic_year": "2025", "month": "2026-02"},
            )
            next_list = self.client.get(reverse("academic-event-list"), {"academic_year": "2027"})
            self.assertEqual(previous_calendar.context["academic_year"], 2025)
            self.assertEqual(
                previous_calendar.context["schedule_workspace_selected_month"], date(2026, 2, 1)
            )
            self.assertEqual(next_list.context["academic_year"], 2027)
            self.assertContains(next_list, "2027&ndash;2028")

            for url in (reverse("academic-event-workspace"), reverse("academic-event-list")):
                with self.subTest(url=url, bound="lower"):
                    lower = self.client.get(url, {"academic_year": "2021"})
                    self.assertEqual(lower.status_code, 200)
                    self.assertContains(
                        lower,
                        '<span role="link" aria-disabled="true" aria-label="Previous academic year">&lsaquo;</span>',
                    )
                    self.assertContains(lower, "academic_year=2022")
                with self.subTest(url=url, bound="upper"):
                    upper = self.client.get(url, {"academic_year": "2031"})
                    self.assertEqual(upper.status_code, 200)
                    self.assertContains(upper, "academic_year=2030")
                    self.assertContains(
                        upper,
                        '<span role="link" aria-disabled="true" aria-label="Next academic year">&rsaquo;</span>',
                    )
                with self.subTest(url=url, bound="outside"):
                    self.assertEqual(
                        self.client.get(url, {"academic_year": "2032"}).status_code,
                        400,
                    )

    def test_calendar_and_list_share_scheduling_actions_and_history_placement(self):
        definition = self.definition("Orientation")
        views = (
            reverse("academic-event-workspace") + "?academic_year=2026&month=2026-08",
            reverse("academic-event-list") + "?academic_year=2026",
        )

        for url in views:
            with self.subTest(url=url):
                response = self.client.get(url)
                content = response.content.decode()
                self.assertTemplateUsed(
                    response, "schedules/academic_event_scheduling_actions.html"
                )
                actions = re.search(
                    r'<section class="workspace-surface surface-frame workspace-actions academic-event-actions">'
                    r"(.*?)</section>",
                    content,
                    re.DOTALL,
                ).group(1)
                self.assertRegex(
                    actions,
                    r'<header class="surface-header schedule-workspace-actions-header"><h2>Schedule an event</h2>'
                    r'<a href="/academic-events/history/(?:\?academic_year=2026&amp;month=2026-08)?">'
                    r"Academic Event History</a></header>",
                )
                self.assertIn('class="academic-event-add-form compact-inline-control-row"', actions)
                self.assertIn(
                    '<div class="compact-control-field academic-event-add-field">'
                    '<label for="event-definition">Event</label><select id="event-definition"',
                    actions,
                )
                self.assertIn(f'<option value="{definition.pk}">Orientation (Q1)</option>', actions)
                self.assertIn('<button type="submit">Schedule</button>', actions)
                self.assertNotIn("Add occurrence", actions)

        calendar = self.client.get(
            reverse("academic-event-workspace"), {"academic_year": "2026", "month": "2026-08"}
        )
        calendar_actions = re.search(
            r'<form action="[^"]+" method="get" class="academic-event-add-form.*?</form>',
            calendar.content.decode(),
            re.DOTALL,
        ).group(0)
        self.assertIn('name="academic_year" value="2026"', calendar_actions)
        self.assertIn('name="view" value="calendar"', calendar_actions)
        self.assertIn('name="month" value="2026-08"', calendar_actions)

        list_response = self.client.get(reverse("academic-event-list"), {"academic_year": "2026"})
        list_actions = re.search(
            r'<form action="[^"]+" method="get" class="academic-event-add-form.*?</form>',
            list_response.content.decode(),
            re.DOTALL,
        ).group(0)
        self.assertIn('name="academic_year" value="2026"', list_actions)
        self.assertIn('name="view" value="list"', list_actions)
        self.assertNotIn('name="month"', list_actions)

        self.assertNotContains(
            calendar,
            "Every participating physician is unavailable during the selected event coverage.",
        )

    def test_calendar_history_round_trip_preserves_academic_year_and_month(self):
        with patch("schedules.views.academic_events._current_academic_year", return_value=2026):
            calendar = self.client.get(
                reverse("academic-event-workspace"),
                {"academic_year": "2026", "month": "2026-08"},
            )
            history = self.client.get(
                reverse("academic-event-history"),
                {"academic_year": "2026", "month": "2026-08"},
            )

        self.assertContains(
            calendar,
            'href="/academic-events/history/?academic_year=2026&amp;month=2026-08"',
        )
        self.assertContains(
            history,
            'href="/academic-events/?academic_year=2026&amp;month=2026-08"',
        )

    def test_calendar_groups_explicit_surfaces_in_a_neutral_evenly_spaced_stack(self):
        calendar = self.client.get(
            reverse("academic-event-workspace"), {"academic_year": "2026", "month": "2026-08"}
        )
        content = calendar.content.decode()
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()
        stack = re.search(
            r'<div class="workspace-surface-stack" aria-label="Academic Event schedule">\s*'
            r'<section class="workspace-surface surface-frame workspace-actions academic-event-actions">.*?</section>\s*'
            r'<div class="workspace-surface surface-frame calendar-scroll-frame">',
            content,
            re.DOTALL,
        )

        self.assertIsNotNone(stack)
        self.assertNotIn(
            '<section class="workspace-surface-stack" aria-label="Academic Event schedule">',
            content,
        )
        stack_rules = re.search(r"\.workspace-surface-stack\s*\{([^}]*)\}", stylesheet)
        self.assertIsNotNone(stack_rules)
        self.assertIn("display: grid", stack_rules.group(1))
        self.assertRegex(stack_rules.group(1), r"gap:\s*var\(--surface-stack-gap\)")
        self.assertNotRegex(
            stack_rules.group(1), r"\b(?:border|background|padding|border-radius)\s*:"
        )
        self.assertRegex(
            stylesheet,
            r"#main-content\s+\.workspace-surface-stack\s*>\s*\.workspace-surface\s*"
            r"\{\s*margin-block:\s*0;\s*\}",
        )
        self.assertRegex(
            stylesheet,
            r"\.workspace-surface-stack\s*>\s*\*\s*\{\s*min-width:\s*0;\s*\}",
        )
        self.assertRegex(
            stylesheet,
            r"\.workspace-work-summary,\s*\.workspace-status\s*\{[^}]*border-left:",
        )
        self.assertIn(
            ".surface-frame { overflow: hidden; border: 1px solid var(--border);",
            stylesheet,
        )
        self.assertNotIn(".workspace-actions { padding:", stylesheet)
        self.assertContains(
            calendar,
            'class="period-navigation-form compact-inline-control-row schedule-workspace-year-picker"',
        )

    def test_calendar_picker_sources_keep_academic_year_and_month_consistent(self):
        year_changes = (
            ("2026-08", "2025", date(2025, 8, 1)),
            ("2027-01", "2025", date(2026, 1, 1)),
        )
        for current_month, selected_year, expected_month in year_changes:
            with self.subTest(current_month=current_month, selected_year=selected_year):
                response = self.client.get(
                    reverse("academic-event-workspace"),
                    {
                        "picker": "academic_year",
                        "academic_year": selected_year,
                        "month": current_month,
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["academic_year"], int(selected_year))
                self.assertEqual(
                    response.context["schedule_workspace_selected_month"], expected_month
                )
                self.assertEqual(
                    response.context["schedule_workspace_year_form"]["academic_year"].value(),
                    int(selected_year),
                )

        month_changes = (
            ("2027-06", "2027", 2026),
            ("2027-07", "2026", 2027),
        )
        for selected_month, submitted_year, expected_year in month_changes:
            with self.subTest(selected_month=selected_month):
                response = self.client.get(
                    reverse("academic-event-workspace"),
                    {
                        "picker": "month",
                        "academic_year": submitted_year,
                        "month": selected_month,
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["academic_year"], expected_year)
                self.assertEqual(
                    response.context["schedule_workspace_selected_month"],
                    datetime.strptime(selected_month, "%Y-%m").date(),
                )

    def test_calendar_picker_rejects_invalid_and_outside_control_input(self):
        invalid_requests = (
            {"picker": "academic_year", "academic_year": "not-a-year", "month": "2026-08"},
            {"picker": "month", "academic_year": "2026", "month": "2026-13"},
            {"picker": "month", "month": "2001-08"},
            {"academic_year": "2026", "month": "2027-07"},
        )
        for query in invalid_requests:
            with self.subTest(query=query):
                response = self.client.get(reverse("academic-event-workspace"), query)
                self.assertEqual(response.status_code, 400)

        response = self.client.get(
            reverse("academic-event-workspace"), {"academic_year": "2001", "month": "2001-08"}
        )
        self.assertEqual(response.status_code, 400)

    def test_workspace_add_choices_exclude_resolved_and_out_of_range_definitions(self):
        eligible = self.definition("Eligible", "Q2")
        dated = self.definition("Already dated", "Q1")
        not_occurring = self.definition("Already not occurring", "Q3")
        self.definition("Past definition", "Q1", start_academic_year=2024, end_academic_year=2025)
        self.definition("Future definition", "Q4", start_academic_year=2027)
        self.occurrence(dated, date(2026, 8, 10), date(2026, 8, 10))
        self.client.post(
            f"{reverse('academic-event-mark-not-occurring', args=(not_occurring.pk,))}?academic_year=2026&view=list"
        )

        calendar_response = self.client.get(
            reverse("academic-event-workspace") + "?academic_year=2026&month=2026-08"
        )
        list_response = self.client.get(reverse("academic-event-list") + "?academic_year=2026")

        self.assertEqual(self.add_choices(calendar_response), [])
        self.assertEqual(self.add_choices(list_response), [(str(eligible.pk), "Eligible (Q2)")])

    def test_list_orders_undated_groups_by_quarter_typical_schedule_and_name(self):
        unresolved = (
            self.definition("Unspecified", "Q1"),
            self.definition("Zebra", "Q1", typically_scheduled="Early August"),
            self.definition("Alpha", "Q1", typically_scheduled="Late August"),
            self.definition("Zulu", "Q2"),
        )
        not_occurring = (
            self.definition("Unspecified not occurring", "Q1"),
            self.definition("Zebra not occurring", "Q1", typically_scheduled="Early August"),
            self.definition("Alpha not occurring", "Q1", typically_scheduled="Late August"),
            self.definition("Zulu not occurring", "Q2"),
        )
        for definition in not_occurring:
            self.client.post(
                f"{reverse('academic-event-mark-not-occurring', args=(definition.pk,))}?academic_year=2026&view=list"
            )

        response = self.client.get(reverse("academic-event-list"), {"academic_year": "2026"})
        content = response.content.decode()
        unresolved_table = self.rendered_resolution_table(content, "Unscheduled yearly events")
        not_occurring_table = self.rendered_resolution_table(content, "Yearly events not occurring")

        def displayed_rows(table):
            return [
                (
                    self.rendered_table_cell(table, row, "Expected quarter").text().strip(),
                    self.rendered_table_cell(table, row, "Typically scheduled").text().strip(),
                    self.rendered_table_cell(table, row, "Base name").text().strip(),
                )
                for row in self.rendered_table_rows(table)
            ]

        self.assertEqual(
            displayed_rows(unresolved_table),
            [
                ("Q1", "Early August", "Zebra"),
                ("Q1", "Late August", "Alpha"),
                ("Q1", "Not specified", "Unspecified"),
                ("Q2", "Not specified", "Zulu"),
            ],
        )
        self.assertEqual(
            displayed_rows(not_occurring_table),
            [
                ("Q1", "Early August", "Zebra not occurring"),
                ("Q1", "Late August", "Alpha not occurring"),
                ("Q1", "Not specified", "Unspecified not occurring"),
                ("Q2", "Not specified", "Zulu not occurring"),
            ],
        )
        for definition, row in zip(
            (unresolved[1], unresolved[2], unresolved[0], unresolved[3]),
            self.rendered_table_rows(unresolved_table),
            strict=True,
        ):
            action = self.rendered_table_cell(unresolved_table, row, "Actions").descendants_named(
                "a"
            )[0]
            self.assertTrue(
                _action_matches(
                    action.attributes["href"],
                    f"{reverse('academic-event-create')}?academic_year=2026&view=list&definition={definition.pk}",
                )
            )
            form = self.rendered_table_cell(unresolved_table, row, "Actions").descendants_named(
                "form"
            )[0]
            self.assertEqual(form.attributes.get("method"), "post")
            self.assertTrue(
                _action_matches(
                    form.attributes.get("action", ""),
                    f"{reverse('academic-event-mark-not-occurring', args=(definition.pk,))}"
                    "?academic_year=2026&view=list",
                )
            )
        for definition, row in zip(
            (not_occurring[1], not_occurring[2], not_occurring[0], not_occurring[3]),
            self.rendered_table_rows(not_occurring_table),
            strict=True,
        ):
            event = AcademicEvent.objects.get(
                definition=definition, resolution=AcademicEvent.Resolution.NOT_OCCURRING
            )
            form = self.rendered_table_cell(not_occurring_table, row, "Actions").descendants_named(
                "form"
            )[0]
            self.assertEqual(form.attributes.get("method"), "post")
            self.assertTrue(
                _action_matches(
                    form.attributes.get("action", ""),
                    f"{reverse('academic-event-reverse-not-occurring', args=(event.pk,))}"
                    "?academic_year=2026&view=list",
                )
            )

    def test_dated_list_and_fixed_definition_detail_show_typically_scheduled(self):
        definition = self.definition(
            "Clinical teaching", "Q1", typically_scheduled="First week of August"
        )
        event = self.occurrence(definition, date(2026, 8, 10), date(2026, 8, 10))
        other_definition = self.definition(
            "Other clinical teaching", "Q2", typically_scheduled="Late November"
        )
        self.occurrence(other_definition, date(2026, 9, 10), date(2026, 9, 10))

        list_response = self.client.get(reverse("academic-event-list"), {"academic_year": "2026"})
        detail_response = self.client.get(reverse("academic-event-correct", args=(event.pk,)))
        yearly_events = self.rendered_academic_event_table(
            list_response.content.decode(), "academic-event-yearly-events"
        )
        row = self.academic_event_occurrence_row(yearly_events, event)

        self.assertEqual(
            self.table_cell_text(yearly_events, row, "Typically scheduled"), "First week of August"
        )
        self.assertContains(detail_response, "First week of August")

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2026, 1, 5, 18, tzinfo=UTC),
    )
    def test_calendar_renders_inclusive_focused_occurrence_range_with_coverage_and_action(
        self, _now
    ):
        self.occurrence(
            self.definition("Empty orientation"),
            date(2026, 8, 30),
            date(2026, 8, 30),
            required=2,
            period_of_day=AcademicEvent.PeriodOfDay.AM,
        )
        definition = self.definition("Morning orientation")
        event = self.occurrence(
            definition,
            date(2026, 8, 30),
            date(2026, 9, 2),
            [self.physician],
            required=2,
            suffix="Residents",
            period_of_day=AcademicEvent.PeriodOfDay.AM,
        )
        filled_event = self.occurrence(
            self.definition("Filled orientation"),
            date(2026, 8, 31),
            date(2026, 8, 31),
            [self.physician],
            period_of_day=AcademicEvent.PeriodOfDay.WHOLE_DAY,
        )
        august = self.client.get(
            reverse("academic-event-workspace"),
            {"academic_year": "2026", "month": "2026-08"},
        )
        september = self.client.get(
            reverse("academic-event-workspace"),
            {"academic_year": "2026", "month": "2026-09"},
        )
        expected_link = f"{reverse('academic-event-correct', args=(event.pk,))}?academic_year=2026&amp;view=calendar"
        for response in (august, september):
            self.assertContains(response, "Morning orientation - Residents")
            self.assertContains(
                response,
                "AM only | 1/2 participants | Assignment incomplete",
            )
            self.assertContains(response, expected_link)
            self.assertTrue(
                all(
                    lane.variant == "range"
                    for week in response.context["calendar"].weeks
                    for lane in week.lanes
                )
            )
            event_segments = [
                segment
                for week in response.context["academic_event_calendar_weeks"]
                for segment in week["segments"]
                if segment["event"] == event
            ]
            self.assertTrue(all(segment["needs_participants"] for segment in event_segments))
            self.assertEqual(
                response.context["work_remaining"].required_item_count,
                3 if response is august else 1,
            )
        self.assertContains(
            august,
            "AM only | 0/2 participants | Assignment incomplete",
        )
        self.assertContains(
            august,
            "Whole day | 1/1 participants",
        )
        self.assertContains(
            august,
            'class="academic-event-segment academic-event-segment--needs-participants"',
            count=2,
        )
        august_content = august.content.decode()
        filled_details = re.search(
            rf'<span class="academic-event-segment-name">{filled_event.title}</span><span class="academic-event-segment-details">(.*?)</a>',
            august_content,
            re.DOTALL,
        ).group(1)
        self.assertNotIn("Scheduled", filled_details)
        self.assertNotIn("Complete", filled_details)
        self.assertNotIn("Filled", filled_details)
        august_segments = [
            segment
            for week in august.context["academic_event_calendar_weeks"]
            for segment in week["segments"]
            if segment["event"] == event
        ]
        self.assertEqual(
            [(segment["start_date"], segment["end_date"]) for segment in august_segments],
            [(date(2026, 8, 30), date(2026, 9, 2))],
        )
        self.assertFalse(
            any(
                segment["needs_participants"]
                for week in august.context["academic_event_calendar_weeks"]
                for segment in week["segments"]
                if segment["event"] == filled_event
            )
        )

    def test_calendar_labels_calendar_only_events(self):
        event = self.occurrence(
            self.definition("Calendar entry"),
            date(2026, 8, 10),
            date(2026, 8, 10),
            period_of_day=AcademicEvent.PeriodOfDay.NONE,
            precepting_impact=AcademicEvent.PreceptingImpact.NONE,
        )

        response = self.client.get(
            reverse("academic-event-workspace"),
            {"academic_year": "2026", "month": "2026-08"},
        )
        listing = self.client.get(reverse("academic-event-list"), {"academic_year": "2026"})

        self.assertContains(response, f"{event.title}</span><span")
        self.assertContains(response, "Calendar only")
        self.assertNotContains(response, "Precepting impact:")
        self.assertNotContains(response, "Assignment incomplete")
        self.assertFalse(
            any(
                segment["needs_participants"]
                for week in response.context["academic_event_calendar_weeks"]
                for segment in week["segments"]
                if segment["event"] == event
            )
        )
        self.assertContains(listing, "Calendar only")
        self.assertNotContains(listing, "Assignment incomplete")
        self.assertNotContains(listing, 'class="data-table-row--attention"')

    def test_calendar_day_cells_use_the_shared_seven_column_background_grid(self):
        definition = self.definition("Cross-month event")
        self.occurrence(definition, date(2026, 8, 30), date(2026, 9, 2), [self.physician])

        response = self.client.get(
            reverse("academic-event-workspace"),
            {"academic_year": "2026", "month": "2026-08"},
        )

        self.assertContains(
            response, 'class="calendar-lattice-date-cell calendar-selected-month"', count=31
        )
        self.assertContains(
            response, 'class="calendar-lattice-date-cell calendar-adjacent-month"', count=11
        )
        self.assertContains(
            response, 'class="calendar-lattice-background" aria-hidden="true"', count=6
        )
        self.assertContains(response, "Cross-month event")
        self.assertNotContains(response, "No Academic Events this week.")

    def test_reverse_not_occurring_route_enforces_csrf_and_reopens_the_definition(self):
        definition = self.definition("Reopen me")
        self.client.post(
            f"{reverse('academic-event-mark-not-occurring', args=(definition.pk,))}?academic_year=2026&view=list"
        )
        decision = AcademicEvent.objects.get(definition=definition)
        reverse_url = reverse("academic-event-reverse-not-occurring", args=(decision.pk,))

        self.client.logout()
        response = self.client.post(f"{reverse_url}?academic_year=2026&view=list")
        self.assertEqual(response.status_code, 302)
        decision.refresh_from_db()
        self.assertTrue(decision.is_active)

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        list_url = reverse("academic-event-list") + "?academic_year=2026"
        csrf_client.get(list_url)
        response = csrf_client.post(f"{reverse_url}?academic_year=2026&view=list")
        self.assertEqual(response.status_code, 403)
        decision.refresh_from_db()
        self.assertTrue(decision.is_active)

        response = csrf_client.post(
            f"{reverse_url}?academic_year=2027&view=list",
            {"csrfmiddlewaretoken": csrf_client.cookies["csrftoken"].value},
        )
        self.assertRedirects(response, list_url, fetch_redirect_response=False)
        decision.refresh_from_db()
        self.assertFalse(decision.is_active)
        response = csrf_client.get(list_url)
        self.assertContains(response, "1 required action remains.")
        self.assertEqual(self.add_choices(response), [(str(definition.pk), "Reopen me (Q1)")])

    def test_calendar_and_list_share_selected_year_facts_and_month_boundaries(self):
        first = self.definition("August event")
        second = self.definition("June event", "Q4")
        self.occurrence(first, date(2026, 8, 30), date(2026, 9, 2), [self.physician])
        self.occurrence(second, date(2027, 6, 30), date(2027, 6, 30), [self.physician])

        calendar_response = self.client.get(
            reverse("academic-event-workspace"),
            {"academic_year": "2026", "month": "2026-08"},
        )
        list_response = self.client.get(reverse("academic-event-list"), {"academic_year": "2026"})

        self.assertContains(calendar_response, "2026&ndash;2027")
        self.assertContains(calendar_response, "August event")
        self.assertContains(calendar_response, "calendar-surface--seven-column")
        self.assertContains(calendar_response, "calendar-lattice--without-gutter")
        self.assertContains(calendar_response, "calendar-lattice--header-two-level")
        self.assertContains(list_response, "August event")
        self.assertContains(list_response, "June event")
        self.assertLess(
            list_response.content.index(b"August event"), list_response.content.index(b"June event")
        )
        self.assertContains(calendar_response, "?academic_year=2025&amp;month=2025-08")
        june_response = self.client.get(
            reverse("academic-event-workspace"),
            {"academic_year": "2026", "month": "2027-06"},
        )
        self.assertContains(june_response, "?academic_year=2027&amp;month=2028-06")

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2026, 8, 1, 18, tzinfo=UTC),
    )
    def test_list_groups_outstanding_and_not_occurring_resolutions_with_progress(self, _now):
        self.definition("Unresolved", "Q2")
        empty = self.definition("Empty", "Q1")
        partial = self.definition("Partial", "Q3")
        complete = self.definition("Complete", "Q4")
        not_occurring = self.definition("Not occurring", "Q1")
        self.occurrence(empty, date(2026, 8, 10), date(2026, 8, 10), required=2)
        self.occurrence(partial, date(2027, 1, 10), date(2027, 1, 10), [self.physician], required=2)
        self.occurrence(complete, date(2027, 4, 10), date(2027, 4, 10), [self.physician])

        response = self.client.post(
            f"{reverse('academic-event-mark-not-occurring', args=(not_occurring.pk,))}?academic_year=2026&view=list",
        )
        self.assertRedirects(
            response,
            f"{reverse('academic-event-list')}?academic_year=2026",
            fetch_redirect_response=False,
        )

        response = self.client.get(reverse("academic-event-list"), {"academic_year": "2026"})
        self.assertContains(response, "4 required actions remain.")
        self.assertContains(response, "surface-tone--danger workspace-work-summary")
        self.assertContains(response, "Unresolved")
        self.assertContains(response, "Not occurring")
        self.assertContains(response, "Reverse not occurring")
        self.assertContains(response, 'class="inline-form academic-event-resolution-form"', count=2)
        self.assertLess(
            response.content.index(b"Unresolved"),
            response.content.index(b"Not occurring this year"),
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2026, 8, 1, 18, tzinfo=UTC),
    )
    def test_list_uses_scheduler_wording_and_compact_semantic_resolution_actions(self, _now):
        empty_response = self.client.get(reverse("academic-event-list"), {"academic_year": "2026"})
        self.assertContains(empty_response, "No work remains in this range.")
        self.assertContains(empty_response, "Unscheduled yearly events")
        self.assertContains(empty_response, "2026-2027")
        empty_content = empty_response.content.decode()
        for section_heading, category_terms in (
            ("Unscheduled yearly events", ("unscheduled", "yearly")),
            ("Yearly events not occurring", ("yearly", "not occurring")),
        ):
            with self.subTest(empty_section=section_heading):
                table = self.rendered_resolution_table(empty_content, section_heading)
                section = next(
                    ancestor for ancestor in table.ancestors() if ancestor.tag == "section"
                )
                self.assertNotIn("surface-tone--danger", section.attributes.get("class", ""))
                self.assert_empty_yearly_resolution_table(
                    table, category_terms=category_terms, academic_year="2026-2027"
                )

        complete = self.definition("Orientation", "Q1", typically_scheduled="Early August")
        unassigned = self.definition("Unassigned", "Q2", typically_scheduled="Late September")
        partially_assigned = self.definition(
            "Partially assigned", "Q3", typically_scheduled="Early November"
        )
        historical = self.definition("Historical", "Q1", typically_scheduled="Last July")
        unresolved = self.definition("Planning", "Q2", typically_scheduled="Mid-October")
        complete_occurrence = self.occurrence(
            complete, date(2026, 8, 10), date(2026, 8, 10), [self.physician]
        )
        unassigned_occurrence = self.occurrence(
            unassigned, date(2026, 9, 10), date(2026, 9, 10), required=2
        )
        partially_assigned_occurrence = self.occurrence(
            partially_assigned,
            date(2026, 10, 10),
            date(2026, 10, 10),
            [self.physician],
            required=2,
        )
        historical_occurrence = self.occurrence(
            historical, date(2026, 7, 10), date(2026, 7, 10), required=2
        )

        response = self.client.get(reverse("academic-event-list"), {"academic_year": "2026"})
        content = response.content.decode()
        yearly_events = self.rendered_academic_event_table(content, "academic-event-yearly-events")
        section_headings = self.academic_event_section_headings(content)
        self.assertLess(
            section_headings.index("Monthly Events"),
            section_headings.index("Scheduled yearly events"),
        )
        self.assertLess(
            section_headings.index("Scheduled yearly events"),
            section_headings.index("Unscheduled yearly events"),
        )
        self.assertLess(
            section_headings.index("Unscheduled yearly events"),
            section_headings.index("Yearly events not occurring"),
        )
        self.assertNotIn("Dated occurrences", content)
        self.assertNotIn("Unscheduled definitions", content)
        self.assertNotIn(">Complete</", content)
        # TEXT-CONTRACT: academic-event-list-assignment-incomplete-status —
        # docs/design.md, Academic Events assignment-state presentation, requires
        # this exact List label and remaining-count form. Capitalization,
        # punctuation, and whitespace matter. Production:
        # schedules/views/academic_events.py.
        expected_rows = (
            (
                complete_occurrence,
                {"Typically scheduled": "Early August", "Assigned": "1/1", "Status": "Recorded"},
            ),
            (
                unassigned_occurrence,
                {
                    "Typically scheduled": "Late September",
                    "Assigned": "0/2",
                    "Status": "Assignment incomplete (2 needed)",
                },
            ),
            (
                partially_assigned_occurrence,
                {
                    "Typically scheduled": "Early November",
                    "Assigned": "1/2",
                    "Status": "Assignment incomplete (1 needed)",
                },
            ),
            (
                historical_occurrence,
                {"Typically scheduled": "Last July", "Assigned": "0/2", "Status": "Recorded"},
            ),
        )
        for occurrence, expected_fields in expected_rows:
            with self.subTest(occurrence=occurrence.base_name):
                row = self.academic_event_occurrence_row(yearly_events, occurrence)
                for header, expected_value in expected_fields.items():
                    self.assertEqual(
                        self.table_cell_text(yearly_events, row, header), expected_value
                    )
        for occurrence in (unassigned_occurrence, partially_assigned_occurrence):
            with self.subTest(incomplete_occurrence=occurrence.base_name):
                row = self.academic_event_occurrence_row(yearly_events, occurrence)
                self.assertIn("data-table-row--danger", row.attributes.get("class", ""))
                self.assertIn(
                    "data-table-status--danger",
                    " ".join(
                        span.attributes.get("class", "")
                        for span in yearly_events.cell_for_header(row, "Status").descendants_named(
                            "span"
                        )
                    ),
                )
        for occurrence in (complete_occurrence, historical_occurrence):
            with self.subTest(neutral_occurrence=occurrence.base_name):
                row = self.academic_event_occurrence_row(yearly_events, occurrence)
                self.assertNotIn("data-table-row--danger", row.attributes.get("class", ""))
                self.assertNotIn(
                    "data-table-status--danger",
                    " ".join(
                        span.attributes.get("class", "")
                        for span in yearly_events.cell_for_header(row, "Status").descendants_named(
                            "span"
                        )
                    ),
                )
        unresolved_table = self.rendered_resolution_table(content, "Unscheduled yearly events")
        unresolved_section = next(
            ancestor for ancestor in unresolved_table.ancestors() if ancestor.tag == "section"
        )
        self.assertIn("surface-tone--danger", unresolved_section.attributes.get("class", ""))
        planning_rows = [
            row
            for row in self.rendered_table_rows(unresolved_table)
            if self.rendered_table_cell(unresolved_table, row, "Base name").text().strip()
            == "Planning"
        ]
        self.assertEqual(len(planning_rows), 1)
        self.assertNotIn("data-table-row--danger", planning_rows[0].attributes.get("class", ""))
        planning_actions = self.rendered_table_cell(unresolved_table, planning_rows[0], "Actions")
        schedule = planning_actions.descendants_named("a")
        forms = planning_actions.descendants_named("form")
        # TEXT-CONTRACT: academic-event-unscheduled-schedule-action —
        # docs/design.md, Academic Events assignment-state presentation, requires
        # the exact navigation label Schedule; capitalization and whitespace
        # matter, but URL query order and surrounding markup do not. Production:
        # schedules/templates/schedules/academic_event_list.html.
        self.assertEqual([link.text().strip() for link in schedule], ["Schedule"])
        self.assertTrue(
            _action_matches(
                schedule[0].attributes.get("href", ""),
                f"{reverse('academic-event-create')}?academic_year=2026&view=list&definition={unresolved.pk}",
            )
        )
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0].attributes.get("method"), "post")
        self.assertTrue(
            _action_matches(
                forms[0].attributes.get("action", ""),
                f"{reverse('academic-event-mark-not-occurring', args=(unresolved.pk,))}"
                "?academic_year=2026&view=list",
            )
        )

    def test_selected_definition_form_and_mutations_return_to_the_originating_list(self):
        definition = self.definition(
            "Spring planning",
            "Q4",
            default_duration_days=3,
            default_period_of_day=AcademicEvent.PeriodOfDay.PM,
        )
        query = "academic_year=2026&view=list"
        create_url = f"{reverse('academic-event-create')}?{query}&definition={definition.pk}"

        response = self.client.get(create_url)
        self.assertContains(response, "2026-2027")
        self.assertContains(response, 'name="academic_year" value="2026"')
        self.assertContains(response, 'name="start_date" value="2027-04-01"')
        self.assertContains(response, 'name="end_date" value="2027-04-03"')

        response = self.client.post(
            create_url,
            {
                "definition": definition.pk,
                "start_date": "2027-04-01",
                "end_date": "2027-04-03",
                "period_of_day": AcademicEvent.PeriodOfDay.PM,
            },
        )
        self.assertRedirects(
            response,
            f"{reverse('academic-event-list')}?academic_year=2026",
            fetch_redirect_response=False,
        )
        event = AcademicEvent.objects.get()

        response = self.client.post(
            f"{reverse('academic-event-remove', args=(event.pk,))}?{query}",
            {"reason": "No longer needed"},
        )
        self.assertRedirects(
            response,
            f"{reverse('academic-event-list')}?academic_year=2026",
            fetch_redirect_response=False,
        )

    def test_schedule_form_keeps_fixed_definition_and_calendar_context_through_errors(self):
        definition = self.definition(
            "Earlier-year planning",
            "Q3",
            start_academic_year=2025,
            default_duration_days=2,
        )
        other_definition = self.definition("Other planning", start_academic_year=2025)
        create_url = (
            f"{reverse('academic-event-create')}?academic_year=2025&view=calendar&month=2026-01"
            f"&definition={definition.pk}"
        )

        response = self.client.get(create_url)
        content = response.content.decode()
        event_section = re.search(
            r'<section class="surface-frame surface-frame--raised" aria-labelledby="event-section-heading">(.*?)</section>',
            content,
            re.DOTALL,
        ).group(1)
        self.assertContains(response, "Schedule Academic Event")
        self.assertContains(response, '<button type="submit">Schedule</button>')
        self.assertRegex(
            content,
            rf'<form method="post" action="/academic-events/add/\?academic_year=2025&amp;view=calendar'
            rf'&amp;month=2026-01&amp;definition={definition.pk}"',
        )
        self.assertRegex(
            event_section,
            rf'<div class="form-field-grid form-field-grid--event">[\s\S]*?'
            r'<div class="form-field-group form-read-only-value">[\s\S]*?'
            r'<span class="form-field-label">Academic year</span>[\s\S]*?<span>2025-2026</span>'
            rf'[\s\S]*?name="definition" value="{definition.pk}"'
            r'[\s\S]*?<div class="form-field-group form-read-only-value">[\s\S]*?'
            r'<span class="form-field-label">Event</span>[\s\S]*?'
            r"<span>Earlier-year planning</span>"
            r"[\s\S]*?Event detail \(optional\)[\s\S]*?"
            r'<div class="form-derived-preview">[\s\S]*?Displayed as',
        )
        self.assertEqual(event_section.count('name="definition"'), 1)
        self.assertNotRegex(event_section, r'<select[^>]+name="definition"')
        self.assertNotIn("Recorded with this change in history.", event_section)

        response = self.client.post(
            create_url,
            {
                "definition": other_definition.pk,
                "start_date": "2026-01-02",
                "end_date": "2026-01-01",
                "period_of_day": AcademicEvent.PeriodOfDay.WHOLE_DAY,
            },
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertContains(response, "End date cannot be before start date.")
        self.assertIn(f'name="definition" value="{definition.pk}"', content)
        self.assertEqual(content.count('name="definition"'), 1)
        self.assertNotRegex(content, r'<select[^>]+name="definition"')

        response = self.client.post(
            create_url,
            {
                "definition": other_definition.pk,
                "start_date": "2026-01-01",
                "end_date": "2026-01-02",
                "period_of_day": AcademicEvent.PeriodOfDay.WHOLE_DAY,
            },
        )
        self.assertRedirects(
            response,
            f"{reverse('academic-event-workspace')}?academic_year=2025&month=2026-01",
            fetch_redirect_response=False,
        )
        self.assertEqual(AcademicEvent.objects.get().definition, definition)

    def test_schedule_form_uses_list_context_and_rejects_invalid_definition_context(self):
        definition = self.definition("List scheduling", start_academic_year=2025)
        create_url = f"{reverse('academic-event-create')}?academic_year=2025&view=list&definition={definition.pk}"

        response = self.client.get(create_url)
        self.assertContains(response, "2025-2026")
        self.assertContains(
            response,
            f'action="{create_url.replace("&", "&amp;")}"',
        )

        selector_response = self.client.get(
            reverse("academic-event-create"), {"academic_year": "2025", "view": "list"}
        )
        self.assertContains(selector_response, 'name="definition"')

        for definition_query in (
            "missing",
            "",
            str(self.definition("Future", start_academic_year=2026).pk),
        ):
            with self.subTest(definition_query=definition_query):
                response = self.client.get(
                    reverse("academic-event-create"),
                    {
                        "academic_year": "2025",
                        "view": "list",
                        "definition": definition_query,
                    },
                )
                self.assertEqual(response.status_code, 400)

    def test_edit_form_retains_edit_wording_and_submit_label(self):
        definition = self.definition("Editable event")
        event = self.occurrence(definition, date(2026, 8, 10), date(2026, 8, 10))

        response = self.client.get(reverse("academic-event-correct", args=(event.pk,)))

        self.assertContains(response, "Edit Academic Event")
        self.assertContains(response, '<button type="submit">Save Academic Event</button>')

    def test_list_and_resolution_actions_require_authentication(self):
        definition = self.definition("Protected")
        self.client.logout()

        self.assertEqual(self.client.get(reverse("academic-event-list")).status_code, 302)
        self.assertEqual(
            self.client.post(
                reverse("academic-event-mark-not-occurring", args=(definition.pk,)),
                {"academic_year": "2026"},
            ).status_code,
            302,
        )
        self.assertEqual(AcademicEvent.objects.count(), 0)


class ViewRouteOwnershipTests(TestCase):
    def test_routes_use_their_owner_modules(self):
        route_owners = {
            "schedules.views.preceptors": (
                ("monthly-workspace", ()),
                ("session-assignment-history", (1,)),
                ("session-assignment-edit", (1, "primary-1")),
                ("session-assignment-remove", (1, "primary-1")),
            ),
            "schedules.views.physicians": (
                ("physicians-directory", ()),
                ("physicians-history", ()),
                ("custom-availability-category-workspace", ()),
                ("custom-availability-category-create", ()),
                ("custom-availability-category-history", ()),
                ("custom-availability-category-edit", (1,)),
                ("physician-create", ()),
                ("physician-detail", (1,)),
                ("physician-history", (1,)),
                ("physician-role-add", (1,)),
                ("physician-role-correct", (1, 1)),
                ("physician-role-end", (1, 1)),
                ("physician-faculty-availability-add", (1,)),
                ("physician-faculty-availability-correct", (1, 1)),
                ("physician-faculty-availability-start-later", (1, 1)),
                ("physician-community-preceptor-rule-create", (1,)),
                ("physician-community-preceptor-rule-correct", (1, 1)),
                ("physician-community-preceptor-rule-remove", (1, 1)),
                ("physician-casual-rounder-rule-create", (1,)),
                ("physician-casual-rounder-rule-correct", (1, 1)),
                ("physician-casual-rounder-rule-remove", (1, 1)),
            ),
            "schedules.views.rounding": (
                ("rounding-workspace", ()),
                ("rounding-list", ()),
                ("normal-rounder-workspace", ()),
                ("casual-rounder-workspace", ()),
                ("normal-rounder-record", ()),
                ("normal-rounder-correct", (1,)),
                ("normal-rounder-remove", (1,)),
                ("casual-rounder-rule-create", ()),
                ("casual-rounder-rule-create-for-physician", (1,)),
                ("casual-rounder-rule-correct", (1,)),
                ("casual-rounder-rule-remove", (1,)),
                ("rounding-history", ()),
            ),
            "schedules.views.community_preceptors": (
                ("community-preceptor-workspace", ()),
                ("community-preceptor-rule-create", ()),
                ("community-preceptor-rule-correct", (1,)),
                ("community-preceptor-rule-remove", (1,)),
                ("community-preceptor-history", ()),
            ),
        }

        for owner, routes in route_owners.items():
            for route_name, args in routes:
                with self.subTest(route_name=route_name):
                    self.assertEqual(resolve(reverse(route_name, args=args)).func.__module__, owner)


class RoundingScheduleViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.normal = Physician.objects.create(display_name="Normal")
        self.casual = Physician.objects.create(display_name="Casual")
        EffectiveRole.objects.create(
            physician=self.normal,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        EffectiveRole.objects.create(
            physician=self.casual,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start="2020-01-01",
        )
        self.client.force_login(self.user)

    def test_workspace_context_keeps_rounding_projections_and_periods_explicit(self):
        views = (
            (reverse("rounding-workspace") + "?month=2025-02", "rounding_projection"),
            (reverse("rounding-list") + "?month=2025-02", "rounding_projection"),
            (reverse("normal-rounder-workspace"), "rounding_weeks"),
            (reverse("casual-rounder-workspace"), "casual_rounders"),
        )

        for url, domain_value in views:
            with self.subTest(url=url):
                response = self.client.get(url)
                context = response.context[0].flatten()

                self.assertEqual(response.status_code, 200)
                self.assertIn(domain_value, context)
                self.assertIn("rounding_view", context)
                self.assertNotIn("current_view", context)
                self.assertNotIn("add_action", context)

        for route in (reverse("rounding-workspace"), reverse("rounding-list")):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route, {"month": "invalid"}).status_code, 400)

        self.assertEqual(
            self.client.get(
                reverse("normal-rounder-workspace"),
                {"start_date": "invalid", "end_date": "2025-02-16"},
            ).status_code,
            400,
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 6, 18, tzinfo=UTC),
    )
    def test_rounding_work_summary_uses_shared_flow_for_incomplete_and_complete_states(
        self, _operational_now
    ):
        response = self.client.get(reverse("rounding-workspace"), {"month": "2025-02"})
        listing = self.client.get(reverse("rounding-list"), {"month": "2025-02"})

        self.assertContains(response, "required actions remain.")
        self.assertContains(response, "surface-tone--danger workspace-work-summary")
        self.assertEqual(response.context["work_remaining"], listing.context["work_remaining"])

        NormalRounder.objects.bulk_create(
            NormalRounder(week=week, physician=self.normal)
            for week in (
                date(2025, 1, 27),
                date(2025, 2, 3),
                date(2025, 2, 10),
                date(2025, 2, 17),
                date(2025, 2, 24),
            )
        )
        response = self.client.get(reverse("rounding-workspace"), {"month": "2025-02"})

        self.assertContains(response, "No work remains in this range.")
        self.assertContains(response, "surface-tone--clear workspace-work-summary")

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 6, 18, tzinfo=UTC),
    )
    def test_normal_rounding_summary_scopes_the_inclusive_inspected_range(self, _operational_now):
        response = self.client.get(
            reverse("normal-rounder-workspace"),
            {"start_date": "2025-02-03", "end_date": "2025-02-16"},
        )

        self.assertEqual(response.context["work_remaining"].required_item_count, 2)
        self.assertEqual(response.context["work_remaining"].tone, "danger")
        self.assertContains(response, "2 required actions remain.")
        self.assertLess(
            response.content.index(b"work-remaining-summary-heading"),
            response.content.index(b"workspace-surface-stack"),
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 6, 18, tzinfo=UTC),
    )
    def test_rounding_work_items_retain_context_preserving_record_and_correction_targets(
        self, _operational_now
    ):
        assignment = NormalRounder.objects.create(week=date(2025, 2, 10), physician=self.normal)
        faculty_role = EffectiveRole.objects.get(physician=self.normal)
        faculty_role.effective_end = date(2025, 2, 9)
        faculty_role.save(update_fields=("effective_end",))

        for url, record_target, correction_target in (
            (
                f"{reverse('rounding-workspace')}?month=2025-02",
                f"{reverse('normal-rounder-record')}?view=normal&start_date=2025-02-03&end_date=2025-02-09&week=2025-02-03",
                f"{reverse('normal-rounder-correct', args=(assignment.pk,))}?view=normal&start_date=2025-02-10&end_date=2025-02-16",
            ),
            (
                f"{reverse('rounding-list')}?month=2025-02",
                f"{reverse('normal-rounder-record')}?view=normal&start_date=2025-02-03&end_date=2025-02-09&week=2025-02-03",
                f"{reverse('normal-rounder-correct', args=(assignment.pk,))}?view=normal&start_date=2025-02-10&end_date=2025-02-16",
            ),
            (
                f"{reverse('normal-rounder-workspace')}?start_date=2025-02-03&end_date=2025-02-16",
                f"{reverse('normal-rounder-record')}?view=normal&start_date=2025-02-03&end_date=2025-02-16&week=2025-02-03",
                f"{reverse('normal-rounder-correct', args=(assignment.pk,))}?view=normal&start_date=2025-02-03&end_date=2025-02-16",
            ),
        ):
            with self.subTest(url=url):
                items = self.client.get(url).context["work_remaining"].items
                targets = {item.key: item.target_url for item in items}

                self.assertEqual(targets["rounding-week-2025-02-03"], record_target)
                self.assertEqual(targets["rounding-week-2025-02-10"], correction_target)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 6, 18, tzinfo=UTC),
    )
    def test_rounding_summary_is_link_free_and_detail_actions_follow_it(self, _operational_now):
        response = self.client.get(reverse("rounding-list"), {"month": "2025-02"})
        content = response.content.decode()
        summary_end = content.index("</section>", content.index("workspace-work-summary"))

        self.assertNotIn("<a ", content[content.index("workspace-work-summary") : summary_end])
        self.assertLess(summary_end, content.index('href="/rounding/normal/add/?view=list'))

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 6, 18, tzinfo=UTC),
    )
    def test_scheduler_can_inspect_incomplete_weeks_and_maintain_rounding_facts_with_history(
        self, _now
    ):
        workspace = reverse("normal-rounder-workspace")
        response = self.client.get(
            workspace, {"start_date": "2025-02-03", "end_date": "2025-02-16"}
        )
        self.assertContains(response, 'class="compact-filter-form"')
        self.assertContains(response, "data-period-picker-form")
        self.assertContains(response, "data-period-picker-input", count=2)
        self.assertContains(
            response, '<button type="submit" data-period-picker-submit>Inspect weeks</button>'
        )
        self.assertContains(response, 'src="/static/schedules/period-picker.js"')
        self.assertContains(response, 'value="2025-02-03"')
        self.assertContains(response, 'value="2025-02-16"')
        self.assertContains(response, "INCOMPLETE: no normal rounder recorded", count=2)
        response = self.client.post(
            f"{reverse('normal-rounder-record')}?week=2025-02-03",
            {"physician": self.normal.pk},
        )
        self.assertRedirects(response, reverse("rounding-workspace"), fetch_redirect_response=False)
        normal = NormalRounder.objects.get()
        response = self.client.post(
            reverse("casual-rounder-rule-create-for-physician", args=(self.casual.pk,)),
            {
                "physician": self.casual.pk,
                "weekday": "1",
                "ordinals": ["all"],
                "effective_start": "",
                "effective_end": "",
                "reason": "Coverage pattern",
            },
        )
        self.assertRedirects(response, reverse("rounding-workspace"), fetch_redirect_response=False)
        rule = CasualRounderRule.objects.get()
        response = self.client.post(
            reverse("casual-rounder-rule-correct", args=(rule.pk,)),
            {
                "physician": self.casual.pk,
                "weekday": "1",
                "ordinals": ["2"],
                "effective_start": "2025-01-01",
                "effective_end": "2025-12-31",
            },
        )
        self.assertRedirects(response, reverse("rounding-workspace"), fetch_redirect_response=False)
        response = self.client.post(
            reverse("normal-rounder-remove", args=(normal.pk,)), {"reason": "No longer assigned"}
        )
        self.assertRedirects(response, reverse("rounding-workspace"), fetch_redirect_response=False)
        history = self.client.get(reverse("rounding-history"))
        self.assertContains(history, "Coverage pattern")
        self.assertContains(history, "No longer assigned")
        self.assertEqual(RoundingScheduleChange.objects.count(), 4)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 6, 18, tzinfo=UTC),
    )
    def test_normal_rounding_emphasizes_incomplete_rows_but_not_filled_rows(self, _now):
        NormalRounder.objects.create(week=date(2025, 2, 3), physician=self.normal)

        response = self.client.get(
            reverse("normal-rounder-workspace"),
            {"start_date": "2025-02-03", "end_date": "2025-02-16"},
        )
        content = response.content.decode()
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()

        self.assertIn(".data-table-row--danger", stylesheet)
        self.assertRegex(
            content,
            r'<tr class="data-table-row--danger"><th scope="row" class="data-table-cell--date">'
            r'<time datetime="2025-02-10">Feb\. 10, 2025</time></th>'
            r".*?INCOMPLETE: no normal rounder recorded",
        )
        self.assertRegex(
            content, r'<time datetime="2025-02-03">Feb\. 3, 2025</time>.*?>Normal</td>'
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2026, 2, 4, 18, tzinfo=UTC),
    )
    def test_rounding_month_uses_one_focused_projection_for_summary_rows_and_segments(self, _now):
        calendar = self.client.get(reverse("rounding-workspace"), {"month": "2026-02"})
        listing = self.client.get(reverse("rounding-list"), {"month": "2026-02"})
        normal = self.client.get(
            reverse("normal-rounder-workspace"),
            {"start_date": "2026-02-01", "end_date": "2026-02-28"},
        )

        self.assertEqual(calendar.context["work_remaining"].required_item_count, 4)
        self.assertEqual(listing.context["work_remaining"].required_item_count, 4)
        self.assertEqual(normal.context["work_remaining"].required_item_count, 4)
        first_list_row = listing.context["rounding_list_rows"][0]
        self.assertEqual(first_list_row["presentation"].table_tone, "")
        self.assertEqual(
            listing.context["rounding_list_rows"][1]["presentation"].table_tone, "danger"
        )
        first_segment = (
            calendar.context["rounding_projection"].calendar_weeks[0].normal_week_segments[0]
        )
        self.assertFalse(first_segment.normal_week.focused_membership)
        self.assertTrue(
            calendar.context["rounding_projection"]
            .calendar_weeks[1]
            .normal_week_segments[0]
            .normal_week.focused_membership
        )
        self.assertContains(listing, "No normal rounder recorded")
        self.assertContains(listing, "week=2026-01-26")

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 6, 18, tzinfo=UTC),
    )
    def test_rounding_list_and_source_share_normal_rounder_row_tones(self, _now):
        NormalRounder.objects.create(week=date(2025, 2, 3), physician=self.normal)
        NormalRounder.objects.create(week=date(2025, 2, 10), physician=self.normal)
        faculty_role = EffectiveRole.objects.get(physician=self.normal)
        faculty_role.effective_end = date(2025, 2, 12)
        faculty_role.save(update_fields=("effective_end",))

        listing = self.client.get(reverse("rounding-list"), {"month": "2025-02"}).content.decode()
        source = self.client.get(
            reverse("normal-rounder-workspace"),
            {"start_date": "2025-02-03", "end_date": "2025-02-17"},
        ).content.decode()

        self.assertRegex(
            listing,
            r'<tr><td class="data-table-cell--date"><time datetime="2025-01-27">'
            r"Jan 27 - Feb 2</time></td><td class=\"data-table-cell--text\">None</td>"
            r"[\s\S]*?No normal rounder recorded",
        )
        self.assertRegex(
            listing,
            r'<tr><td class="data-table-cell--date"><time datetime="2025-02-03">'
            r"Feb 3 - Feb 9</time></td><td class=\"data-table-cell--text\">Normal</td>"
            r"[\s\S]*?>Filled</td>",
        )
        self.assertRegex(
            listing,
            r'<tr class="data-table-row--attention"><td class="data-table-cell--date">'
            r'<time datetime="2025-02-10">Feb 10 - Feb 16</time></td>'
            r"[\s\S]*?data-table-status--attention\">Inconsistent:",
        )
        self.assertRegex(
            listing,
            r'<tr class="data-table-row--danger"><td class="data-table-cell--date">'
            r'<time datetime="2025-02-17">Feb 17 - Feb 23</time></td>'
            r"[\s\S]*?data-table-status--danger\">Missing normal rounder</span>",
        )
        self.assertRegex(
            source,
            r'<time datetime="2025-02-03">Feb\. 3, 2025</time>.*?>Normal</td>',
        )
        self.assertRegex(
            source,
            r'<tr class="data-table-row--attention">.*?data-table-status--attention">Normal - INCONSISTENT:',
        )
        self.assertRegex(
            source,
            r'<tr class="data-table-row--danger">.*?INCOMPLETE: no normal rounder recorded',
        )

    def test_normal_rounder_record_normalizes_its_target_week_and_anonymous_submissions_do_not_write(
        self,
    ):
        response = self.client.post(
            f"{reverse('normal-rounder-record')}?week=2025-02-04",
            {"physician": self.normal.pk},
        )
        self.assertRedirects(
            response,
            reverse("rounding-workspace"),
            fetch_redirect_response=False,
        )
        self.assertTrue(NormalRounder.objects.filter(week=date(2025, 2, 3)).exists())
        change_count = RoundingScheduleChange.objects.count()
        self.client.logout()
        response = self.client.post(
            reverse("casual-rounder-rule-create"),
            {"physician": self.casual.pk, "weekday": "1", "ordinals": ["1"]},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CasualRounderRule.objects.exists())
        self.assertEqual(RoundingScheduleChange.objects.count(), change_count)

    def test_normal_rounder_save_and_add_next_advances_within_its_preserved_range(self):
        record_url = (
            f"{reverse('normal-rounder-record')}?view=normal&start_date=2025-02-03&"
            "end_date=2025-02-16&week=2025-02-03"
        )

        response = self.client.get(record_url)

        self.assertContains(response, "Save and add next")
        response = self.client.post(
            record_url,
            {"physician": self.normal.pk, "reason": "Imported list", "save_and_add_next": "1"},
        )

        next_url = (
            f"{reverse('normal-rounder-record')}?view=normal&start_date=2025-02-03&"
            "end_date=2025-02-16&week=2025-02-10"
        )
        self.assertRedirects(response, next_url, fetch_redirect_response=False)
        self.assertTrue(NormalRounder.objects.filter(week=date(2025, 2, 3)).exists())
        self.assertEqual(RoundingScheduleChange.objects.get().reason, "Imported list")
        next_form = self.client.get(next_url)
        self.assertContains(next_form, '<time datetime="2025-02-10">Feb. 10, 2025</time>')
        self.assertNotContains(next_form, "Imported list")
        self.assertNotContains(next_form, f'<option value="{self.normal.pk}" selected>')

    def test_normal_rounder_save_and_add_next_skips_assigned_weeks_and_completes_at_range_end(self):
        NormalRounder.objects.create(week=date(2025, 2, 10), physician=self.normal)
        record_url = (
            f"{reverse('normal-rounder-record')}?view=normal&start_date=2025-02-03&"
            "end_date=2025-02-23&week=2025-02-03"
        )

        response = self.client.post(
            record_url,
            {"physician": self.normal.pk, "save_and_add_next": "1"},
        )

        last_week_url = (
            f"{reverse('normal-rounder-record')}?view=normal&start_date=2025-02-03&"
            "end_date=2025-02-23&week=2025-02-17"
        )
        self.assertRedirects(response, last_week_url, fetch_redirect_response=False)
        response = self.client.post(
            last_week_url,
            {"physician": self.normal.pk, "save_and_add_next": "1"},
            follow=True,
        )

        workspace_url = (
            f"{reverse('normal-rounder-workspace')}?start_date=2025-02-03&end_date=2025-02-23"
        )
        self.assertRedirects(response, workspace_url)
        self.assertContains(
            response, "Normal rounder saved. No unassigned weeks remain in this range."
        )

    def test_normal_rounder_save_and_add_next_keeps_invalid_submission_on_current_form(self):
        record_url = (
            f"{reverse('normal-rounder-record')}?view=normal&start_date=2025-02-03&"
            "end_date=2025-02-16&week=2025-02-03"
        )

        response = self.client.post(record_url, {"save_and_add_next": "1"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required.")
        self.assertContains(response, '<time datetime="2025-02-03">Feb. 3, 2025</time>')
        self.assertFalse(NormalRounder.objects.exists())

    def test_normal_rounder_save_and_add_next_is_limited_to_bounded_normal_add_forms(self):
        normal = NormalRounder.objects.create(week=date(2025, 2, 3), physician=self.normal)

        for url in (
            f"{reverse('normal-rounder-record')}?week=2025-02-10",
            f"{reverse('normal-rounder-record')}?view=normal&week=2025-02-10",
            f"{reverse('normal-rounder-correct', args=(normal.pk,))}?view=normal&"
            "start_date=2025-02-03&end_date=2025-02-16",
        ):
            with self.subTest(url=url):
                self.assertNotContains(self.client.get(url), "Save and add next")

    def test_normal_rounder_choices_require_full_week_eligibility(self):
        community = Physician.objects.create(display_name="Community only")
        community_rounder = Physician.objects.create(display_name="Community rounder")
        EffectiveRole.objects.create(
            physician=community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start="2025-01-01",
        )
        EffectiveRole.objects.create(
            physician=community_rounder,
            role=EffectiveRole.Role.COMMUNITY_ROUNDER,
            effective_start="2025-01-01",
        )

        form = self.client.get(f"{reverse('normal-rounder-record')}?week=2025-02-03")
        self.assertContains(form, "Normal")
        self.assertFalse(
            form.context["form"].fields["physician"].queryset.filter(pk=community.pk).exists()
        )
        self.assertTrue(
            form.context["form"]
            .fields["physician"]
            .queryset.filter(pk=community_rounder.pk)
            .exists()
        )

        response = self.client.post(
            f"{reverse('normal-rounder-record')}?week=2025-02-03",
            {"physician": community.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "full Monday-Sunday")
        self.assertFalse(NormalRounder.objects.exists())
        self.assertFalse(RoundingScheduleChange.objects.exists())

    def test_normal_and_casual_forms_keep_their_distinct_shared_form_groups(self):
        normal_response = self.client.get(f"{reverse('normal-rounder-record')}?week=2025-02-03")
        self.assertContains(normal_response, "Rounding week")
        self.assertContains(normal_response, "Actions")
        self.assertContains(
            normal_response, 'class="form-field-grid form-field-grid--source-details"'
        )
        normal = NormalRounder.objects.create(week=date(2025, 2, 3), physician=self.normal)
        self.assertContains(
            self.client.get(reverse("normal-rounder-correct", args=(normal.pk,))),
            "Edit normal rounder",
        )

        casual_response = self.client.get(
            reverse("casual-rounder-rule-create-for-physician", args=(self.casual.pk,))
        )
        self.assertContains(casual_response, "Casual rounder rule")
        self.assertContains(casual_response, "Effective range")
        self.assertContains(casual_response, 'class="form-choice-group"')

        response = self.client.post(
            reverse("casual-rounder-rule-create-for-physician", args=(self.casual.pk,)),
            {
                "physician": self.casual.pk,
                "weekday": "1",
                "ordinals": ["1"],
                "effective_start": "2026-08-12",
                "effective_end": "2026-08-10",
            },
        )
        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, 'class="errorlist" id="id_effective_end_error"')

        rule = CasualRounderRule.objects.create(physician=self.casual, weekday=1, ordinals=[1])
        edit_response = self.client.get(reverse("casual-rounder-rule-correct", args=(rule.pk,)))
        self.assertContains(edit_response, "Edit casual rounder rule")
        assert_removal_section(
            self,
            edit_response,
            "rounding-removal-heading",
            reverse("casual-rounder-rule-remove", args=(rule.pk,)),
        )
        self.assertContains(edit_response, "retains the prior value and optional note")
        workspace_response = self.client.get(
            reverse("normal-rounder-workspace"),
            {"start_date": "2025-02-03", "end_date": "2025-02-03"},
        )
        self.assertContains(
            workspace_response,
            (
                f'href="/rounding/normal/{normal.pk}/correct/?view=normal&amp;'
                'start_date=2025-02-03&amp;end_date=2025-02-03">Edit</a>'
            ),
        )
        workspace_response = self.client.get(reverse("casual-rounder-workspace"))
        self.assertContains(
            workspace_response,
            f'href="/rounding/casual/add/{self.casual.pk}/?view=casual">Add rule</a>',
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 6, 18, tzinfo=UTC),
    )
    def test_rounding_actions_use_edit_language_for_assigned_and_empty_weeks(
        self, _operational_now
    ):
        normal = NormalRounder.objects.create(week=date(2025, 2, 3), physician=self.normal)

        source = self.client.get(
            reverse("normal-rounder-workspace"),
            {"start_date": "2025-02-03", "end_date": "2025-02-16"},
        )

        self.assertContains(source, "Click the specific week you want to edit.")
        self.assertContains(source, '<h2 id="normal-rounding-list-heading">Rounding shifts</h2>')
        self.assertContains(source, '<th scope="col">Week starting</th>')
        self.assertContains(
            source,
            (
                f'href="/rounding/normal/{normal.pk}/correct/?view=normal&amp;'
                'start_date=2025-02-03&amp;end_date=2025-02-16">Edit</a>'
            ),
        )
        self.assertContains(
            source,
            (
                'href="/rounding/normal/add/?view=normal&amp;start_date=2025-02-03&amp;'
                'end_date=2025-02-16&amp;week=2025-02-10">Edit</a>'
            ),
        )

        calendar = self.client.get(reverse("rounding-workspace"), {"month": "2025-02"})
        self.assertContains(calendar, 'href="/rounding/normal/">Edit normal rounder</a>')
        self.assertContains(calendar, "Edit week of 2025-02-10")

        listing = self.client.get(reverse("rounding-list"), {"month": "2025-02"})
        self.assertContains(listing, '<h2 id="rounding-list-heading">Rounding Shifts</h2>')
        self.assertContains(listing, 'href="/rounding/normal/">Edit normal rounder</a>')
        self.assertContains(
            listing,
            'href="/rounding/normal/add/?view=list&amp;month=2025-02&amp;week=2025-02-10">Edit</a>',
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 6, 18, tzinfo=UTC),
    )
    def test_rounding_workspace_calendar_list_and_source_views_preserve_month_context(
        self, _operational_now
    ):
        normal = NormalRounder.objects.create(week=date(2025, 2, 3), physician=self.normal)
        alpha = Physician.objects.create(display_name="Alpha casual")
        beta = Physician.objects.create(display_name="Beta casual")
        for physician in (alpha, beta):
            EffectiveRole.objects.create(
                physician=physician,
                role=EffectiveRole.Role.CASUAL_ROUNDER,
                effective_start="2025-01-01",
            )
        alpha_rule = CasualRounderRule.objects.create(physician=alpha, weekday=1, ordinals=[1])
        CasualRounderRule.objects.create(physician=beta, weekday=1, ordinals=[1])
        faculty_role = EffectiveRole.objects.get(physician=self.normal)
        faculty_role.effective_end = date(2025, 2, 5)
        faculty_role.save(update_fields=("effective_end",))

        calendar = self.client.get(reverse("rounding-workspace"), {"month": "2025-02"})

        self.assertEqual(calendar.status_code, 200)
        self.assertEqual(calendar.context["schedule_workspace_selected_month"], date(2025, 2, 1))
        content = calendar.content.decode()
        self.assertLess(content.index(">Calendar<"), content.index(">List<"))
        self.assertLess(content.index(">List<"), content.index(">Normal Rounding<"))
        self.assertLess(content.index(">Normal Rounding<"), content.index(">Casual Rules<"))
        self.assertIn('href="/rounding/list/?month=2025-02"', content)
        self.assertIn("calendar-surface--seven-column", content)
        self.assertIn("calendar-lattice--with-gutter", content)
        self.assertIn("calendar-lattice--header-two-level", content)
        self.assertIn("calendar-selected-month", content)
        self.assertIn("calendar-adjacent-month", content)
        self.assertIn("rounding-normal-span--inconsistent", content)
        self.assertIn("rounding-normal-span--missing", content)
        self.assertIn("Normal rounder missing", content)
        self.assertNotIn("No casual rounders", content)
        self.assertIn(
            f"/rounding/normal/{normal.pk}/correct/?view=calendar&amp;month=2025-02", content
        )
        self.assertIn(
            f"/rounding/casual/{alpha_rule.pk}/correct/?view=calendar&amp;month=2025-02", content
        )
        self.assertIn("Add casual rounder rule", content)

        listing = self.client.get(reverse("rounding-list"), {"month": "2025-02"})
        listing_content = listing.content.decode()
        self.assertEqual(len(listing.context["rounding_projection"].list_rows), 5)
        self.assertIn("Rounding Shifts", listing_content)
        self.assertIn("Missing normal rounder", listing_content)
        self.assertIn("Inconsistent:", listing_content)
        self.assertLess(
            listing_content.index("Alpha casual (Tuesday)"),
            listing_content.index("Beta casual (Tuesday)"),
        )
        self.assertIn(
            "/rounding/normal/add/?view=list&amp;month=2025-02&amp;week=2025-01-27", listing_content
        )
        self.assertIn("Add casual rounder rule", listing_content)

        source = self.client.get(reverse("normal-rounder-workspace"))
        self.assertContains(source, 'class="compact-filter-form"')
        self.assertContains(source, "Inspect weeks")
        self.assertEqual(source.context["form"].cleaned_data["start_date"], date(2025, 2, 3))
        self.assertEqual(source.context["form"].cleaned_data["end_date"], date(2026, 2, 2))
        self.assertEqual(len(source.context["rounding_weeks"]), 53)
        self.assertContains(
            source,
            (
                'href="/rounding/history/?view=normal&amp;start_date=2025-02-03&amp;'
                'end_date=2026-02-02"'
            ),
        )
        self.assertContains(source, 'class="workspace-surface-stack" aria-label="Normal Rounding"')
        casual_source = self.client.get(reverse("casual-rounder-workspace"))
        self.assertContains(casual_source, "Add casual rounder rule")
        self.assertContains(casual_source, "Alpha casual")
        self.assertContains(casual_source, 'href="/rounding/history/?view=casual"')
        self.assertContains(
            casual_source, 'class="workspace-surface-stack" aria-label="Casual Rules"'
        )

        history = self.client.get(reverse("rounding-history"), {"view": "list", "month": "2025-02"})
        self.assertContains(history, 'href="/rounding/list/?month=2025-02"')

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 6, 18, tzinfo=UTC),
    )
    def test_rounding_calendar_distinguishes_missing_and_historical_normal_rounder_spans(
        self, _operational_now
    ):
        historical = NormalRounder.objects.create(week=date(2025, 1, 27), physician=self.normal)
        NormalRounder.objects.create(week=date(2025, 2, 3), physician=self.normal)
        faculty_role = EffectiveRole.objects.get(physician=self.normal)
        faculty_role.effective_end = date(2025, 2, 5)
        faculty_role.save(update_fields=("effective_end",))

        response = self.client.get(reverse("rounding-workspace"), {"month": "2025-02"})
        content = response.content.decode()
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()

        self.assertRegex(
            content,
            r'<a class="rounding-normal-span rounding-normal-span--historical-gap" '
            r'href="/rounding/normal/add/\?view=calendar&amp;month=2025-02&amp;week=2025-01-20">'
            r"<strong>No normal rounder recorded</strong><span>Historical record</span></a>",
        )
        self.assertRegex(
            content,
            rf'<a class="rounding-normal-span" href="/rounding/normal/{historical.pk}/correct/'
            r'\?view=calendar&amp;month=2025-02"><strong>Normal</strong><span>Normal rounder</span></a>',
        )
        self.assertRegex(
            content,
            r'<a class="rounding-normal-span rounding-normal-span--inconsistent" '
            r'href="/rounding/normal/\d+/correct/\?view=calendar&amp;month=2025-02">'
            r"<strong>Normal</strong><span>Inconsistent:",
        )
        self.assertRegex(
            content,
            r'<a class="rounding-normal-span rounding-normal-span--missing" '
            r'href="/rounding/normal/add/\?view=calendar&amp;month=2025-02&amp;week=2025-02-10">'
            r"<strong>Normal rounder missing</strong><span>Edit week of 2025-02-10</span></a>",
        )
        self.assertIn(
            ".rounding-normal-span--missing { border-color: var(--error); border-left-color: "
            "var(--error); background: #fff0f0; }",
            stylesheet,
        )
        self.assertIn(
            ".rounding-normal-span--historical-gap { border-color: var(--border); border-left-color: "
            "var(--muted); background: var(--surface-tone-subtle); }",
            stylesheet,
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 2, 6, 18, tzinfo=UTC),
    )
    def test_rounding_weekly_list_shows_filled_rows_and_retains_edit_and_record_actions(self, _now):
        normal = NormalRounder.objects.create(week=date(2025, 2, 3), physician=self.normal)
        alpha = Physician.objects.create(display_name="Alpha casual")
        beta = Physician.objects.create(display_name="Beta casual")
        alpha_rule = CasualRounderRule.objects.create(physician=alpha, weekday=1, ordinals=[1])
        CasualRounderRule.objects.create(physician=beta, weekday=1, ordinals=[1])

        response = self.client.get(reverse("rounding-list"), {"month": "2025-02"})
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rounding_projection"].list_rows), 5)
        self.assertIn("Jan 27 - Feb 2", content)
        self.assertIn("Feb 3 - Feb 9", content)
        self.assertIn("Filled", content)
        self.assertLess(
            content.index("Alpha casual (Tuesday)"), content.index("Beta casual (Tuesday)")
        )
        self.assertIn(f"/rounding/normal/{normal.pk}/correct/?view=list&amp;month=2025-02", content)
        self.assertIn(
            f"/rounding/casual/{alpha_rule.pk}/correct/?view=list&amp;month=2025-02", content
        )
        self.assertIn(
            "/rounding/normal/add/?view=list&amp;month=2025-02&amp;week=2025-01-27", content
        )

    def test_rounding_forms_prefill_and_return_to_valid_originating_workspace(self):
        record_url = f"{reverse('normal-rounder-record')}?view=list&month=2025-02&week=2025-02-03"
        response = self.client.get(record_url)
        self.assertContains(response, '<time datetime="2025-02-03">Feb. 3, 2025</time>')
        self.assertNotContains(response, 'name="week"')

        response = self.client.post(record_url, {"week": "2025-02-10", "physician": self.normal.pk})
        self.assertRedirects(
            response, f"{reverse('rounding-list')}?month=2025-02", fetch_redirect_response=False
        )
        normal = NormalRounder.objects.get()
        edit_url = (
            f"{reverse('normal-rounder-correct', args=(normal.pk,))}?view=calendar&month=2025-02"
        )
        response = self.client.get(edit_url)
        self.assertContains(response, 'href="/rounding/?month=2025-02"')
        self.assertContains(
            response,
            f'action="/rounding/normal/{normal.pk}/remove/?view=calendar&amp;month=2025-02"',
        )

        casual_url = (
            f"{reverse('casual-rounder-rule-create-for-physician', args=(self.casual.pk,))}"
            "?view=casual"
        )
        response = self.client.post(
            casual_url,
            {
                "physician": self.casual.pk,
                "weekday": "1",
                "ordinals": ["1"],
                "effective_start": "",
                "effective_end": "",
            },
        )
        self.assertRedirects(
            response, reverse("casual-rounder-workspace"), fetch_redirect_response=False
        )

        self.assertEqual(
            self.client.get(reverse("rounding-workspace"), {"month": "invalid"}).status_code, 400
        )
        self.assertRedirects(
            self.client.get(reverse("normal-rounder-record")),
            reverse("normal-rounder-workspace"),
            fetch_redirect_response=False,
        )
        self.client.logout()
        for route in (
            reverse("rounding-workspace"),
            reverse("rounding-list"),
            reverse("normal-rounder-workspace"),
            reverse("casual-rounder-workspace"),
        ):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 302)

    def test_stale_normal_rounder_record_target_redirects_to_current_assignment(self):
        replacement = Physician.objects.create(display_name="Replacement")
        EffectiveRole.objects.create(
            physician=replacement,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        record_url = (
            f"{reverse('normal-rounder-record')}?view=normal&"
            "start_date=2025-02-03&end_date=2025-02-16&week=2025-02-03"
        )

        self.assertEqual(self.client.get(record_url).status_code, 200)
        assignment = NormalRounder.objects.create(week=date(2025, 2, 3), physician=self.normal)
        response = self.client.post(record_url, {"physician": replacement.pk})

        self.assertRedirects(
            response,
            (
                f"{reverse('normal-rounder-correct', args=(assignment.pk,))}?view=normal&"
                "start_date=2025-02-03&end_date=2025-02-16"
            ),
            fetch_redirect_response=False,
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.physician, self.normal)

    def test_normal_rounding_range_context_survives_actions_forms_and_history(self):
        range_query = "start_date=2025-02-03&end_date=2025-02-16"
        workspace_url = f"{reverse('normal-rounder-workspace')}?{range_query}"
        response = self.client.get(workspace_url)
        content = response.content.decode()
        action_query = "view=normal&amp;start_date=2025-02-03&amp;end_date=2025-02-16"

        self.assertNotIn(
            f'/rounding/normal/add/?{action_query}">Record normal rounder</a>', content
        )
        self.assertIn(f"/rounding/history/?{action_query}", content)
        record_url = f"{reverse('normal-rounder-record')}?view=normal&{range_query}&week=2025-02-03"
        self.assertIn(f"/rounding/normal/add/?{action_query}&amp;week=2025-02-03", content)

        response = self.client.post(record_url, {"week": "2025-02-03", "physician": self.normal.pk})
        self.assertRedirects(response, workspace_url, fetch_redirect_response=False)
        normal = NormalRounder.objects.get()
        response = self.client.get(workspace_url)
        edit_url = (
            f"{reverse('normal-rounder-correct', args=(normal.pk,))}?view=normal&{range_query}"
        )

        self.assertContains(
            response,
            f'href="/rounding/normal/{normal.pk}/correct/?{action_query}">Edit</a>',
        )
        form = self.client.get(edit_url)
        self.assertContains(
            form,
            f'action="/rounding/normal/{normal.pk}/remove/?{action_query}"',
        )
        response = self.client.post(edit_url, {"week": "2025-02-03", "physician": self.normal.pk})
        self.assertRedirects(response, workspace_url, fetch_redirect_response=False)

        history = self.client.get(f"{reverse('rounding-history')}?view=normal&{range_query}")
        self.assertContains(
            history,
            'href="/rounding/normal/?start_date=2025-02-03&amp;end_date=2025-02-16">',
        )
        remove_url = (
            f"{reverse('normal-rounder-remove', args=(normal.pk,))}?view=normal&{range_query}"
        )
        response = self.client.post(remove_url, {"reason": "No longer assigned"})
        self.assertRedirects(response, workspace_url, fetch_redirect_response=False)

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 2, tzinfo=UTC),
    )
    def test_casual_rule_entry_uses_current_roster_and_scoped_retained_context(self, _now):
        active = Physician.objects.create(display_name="Casual active today")
        single_rule = Physician.objects.create(display_name="Casual one current rule")
        past = Physician.objects.create(display_name="Casual past only")
        future = Physician.objects.create(display_name="Casual future only")
        wrong_role = Physician.objects.create(display_name="Community but not casual")
        no_role = Physician.objects.create(display_name="No role")
        EffectiveRole.objects.create(
            physician=active,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start="2024-12-31",
        )
        EffectiveRole.objects.create(
            physician=single_rule,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start="2024-12-31",
        )
        EffectiveRole.objects.create(
            physician=past,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start="2024-01-01",
            effective_end="2024-12-30",
        )
        EffectiveRole.objects.create(
            physician=future,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start="2025-01-01",
        )
        EffectiveRole.objects.create(
            physician=wrong_role,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start="2024-12-31",
        )
        rule = CasualRounderRule.objects.create(
            physician=active,
            weekday=1,
            ordinals=[1, 3],
            effective_start="2024-12-31",
        )
        retained_rule = CasualRounderRule.objects.create(
            physician=active,
            weekday=3,
            ordinals=[2],
            effective_start="2024-12-31",
        )
        CasualRounderRule.objects.create(
            physician=single_rule,
            weekday=1,
            ordinals=[1],
            effective_start="2024-12-31",
        )
        CasualRounderRule.objects.create(
            physician=self.casual,
            weekday=2,
            ordinals=[2],
            effective_end="2024-12-30",
        )

        response = self.client.get(
            f"{reverse('casual-rounder-rule-create')}?view=normal&"
            "start_date=2024-12-30&end_date=2025-01-12"
        )

        self.assertContains(response, "Current casual rounders")
        self.assertContains(response, "2 current rules")
        self.assertContains(response, "1 current rule")
        self.assertContains(response, "Not configured")
        self.assertContains(
            response,
            (
                f'href="/rounding/casual/add/{active.pk}/?view=normal&amp;'
                'start_date=2024-12-30&amp;end_date=2025-01-12">Add rule</a>'
            ),
        )
        self.assertNotContains(response, 'name="physician"')
        roster_ids = {physician.pk for physician in response.context["casual_rounders"]}
        self.assertEqual(roster_ids, {self.casual.pk, active.pk, single_rule.pk})
        self.assertNotIn(no_role.pk, roster_ids)
        self.assertFalse(RoundingScheduleChange.objects.exists())
        self.assertEqual(CasualRounderRule.objects.count(), 4)

        scoped_url = (
            f"{reverse('casual-rounder-rule-create-for-physician', args=(active.pk,))}"
            "?view=list&month=2025-01"
        )
        response = self.client.get(scoped_url)

        self.assertContains(response, "Add casual rounder rule")
        self.assertContains(response, 'value="2024-12-31"')
        self.assertContains(response, "Casual active today")
        self.assertNotContains(response, 'name="physician"')
        self.assertContains(response, 'href="/rounding/list/?month=2025-01"')
        self.assertContains(response, "Back to Rounding Schedule", count=2)
        self.assertNotContains(response, "Return to Casual active today")
        self.assertContains(response, "Only one effective rule per weekday can apply at a time.")
        self.assertContains(response, "First, Third")
        self.assertEqual(list(response.context["recurrence_rules"]), [rule, retained_rule])
        self.assertContains(
            response,
            f'href="/rounding/casual/{rule.pk}/correct/?view=list&amp;month=2025-01">Retained rule: Edit</a>',
        )
        self.assertContains(response, 'id="casual-rounder-rule-context" class="data-table"')

        invalid = self.client.post(
            scoped_url,
            {
                "physician": self.casual.pk,
                "weekday": "2",
                "ordinals": ["1"],
                "effective_start": "2025-02-12",
                "effective_end": "2025-02-10",
            },
        )
        self.assertContains(invalid, 'value="2025-02-12"')
        self.assertContains(invalid, 'value="2025-02-10"')
        self.assertEqual(CasualRounderRule.objects.count(), 4)

        response = self.client.post(
            scoped_url,
            {
                "physician": self.casual.pk,
                "weekday": "2",
                "ordinals": ["1"],
                "effective_start": "2025-02-12",
                "effective_end": "",
            },
        )
        self.assertRedirects(
            response,
            f"{reverse('rounding-list')}?month=2025-01",
            fetch_redirect_response=False,
        )
        self.assertEqual(
            CasualRounderRule.objects.get(physician=active, weekday=2).physician, active
        )

        physician_response = self.client.get(
            reverse("physician-casual-rounder-rule-create", args=(future.pk,))
        )
        self.assertContains(physician_response, 'value="2024-12-31"')
        self.assertContains(physician_response, "Casual future only")
        self.assertNotContains(physician_response, 'name="physician"')

        self.assertEqual(
            self.client.get(
                reverse("casual-rounder-rule-create-for-physician", args=(future.pk,))
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse("casual-rounder-rule-create-for-physician", args=(past.pk,)),
                {"weekday": "2", "ordinals": ["1"]},
            ).status_code,
            404,
        )

        edit = self.client.get(reverse("casual-rounder-rule-correct", args=(rule.pk,)))
        self.assertContains(edit, "Editing this rule")
        self.assertContains(edit, "Edit casual rounder rule")
        self.assertContains(edit, 'value="2024-12-31"')
        self.assertContains(edit, 'href="/rounding/"')
        self.assertContains(edit, "Back to Rounding Schedule", count=2)
        self.assertNotContains(
            edit,
            f'href="/rounding/casual/{rule.pk}/correct/?view=calendar',
        )
        self.assertContains(
            edit,
            f'href="/rounding/casual/{retained_rule.pk}/correct/?view=calendar',
        )


class CommunityPreceptorScheduleViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.physician = Physician.objects.create(display_name="Community")
        self.other_physician = Physician.objects.create(display_name="Other community")
        self.client.force_login(self.user)

    def test_scheduler_can_inspect_create_correct_remove_and_view_history(self):
        workspace = reverse("community-preceptor-workspace")
        rules_workspace = reverse("community-preceptor-rules") + "?month=2025-02"
        response = self.client.get(workspace)
        self.assertContains(response, "Community Precepting calendar")
        self.assertContains(response, "Calendar")
        self.assertContains(response, "List")
        self.assertContains(response, "Rules")
        self.assertContains(
            self.client.get(rules_workspace), "No Community Preceptor rules recorded."
        )
        form = self.client.get(reverse("community-preceptor-rule-create"))
        self.assertContains(form, "AM")
        self.assertContains(form, "PM")
        self.assertContains(form, 'name="periods"', count=2)
        self.assertContains(form, 'value="AM" id="id_periods_0" checked')
        self.assertContains(form, 'value="PM" id="id_periods_1" checked')

        response = self.client.post(
            reverse("community-preceptor-rule-create"),
            {
                "physician": self.physician.pk,
                "weekday": "1",
                "ordinals": ["1", "3", "5"],
                "periods": ["AM", "PM"],
                "effective_start": "2025-02-01",
                "effective_end": "2025-04-30",
                "reason": "Expected Tuesday coverage",
            },
        )
        self.assertRedirects(response, workspace, fetch_redirect_response=False)
        rule = CommunityPreceptorRule.objects.get()
        workspace_response = self.client.get(rules_workspace)
        self.assertContains(workspace_response, "Community")
        self.assertContains(workspace_response, "Tuesday")
        self.assertContains(workspace_response, "First, Third, Fifth")
        self.assertContains(workspace_response, "AM and PM")
        self.assertContains(workspace_response, "Feb. 1, 2025 through April 30, 2025")
        self.assertContains(
            workspace_response,
            f'href="/community-preceptors/{rule.pk}/correct/?view=rules&amp;month=2025-02">Edit</a>',
        )

        response = self.client.post(
            reverse("community-preceptor-rule-correct", args=(rule.pk,)),
            {
                "physician": self.other_physician.pk,
                "weekday": "4",
                "ordinals": ["2"],
                "periods": ["PM"],
                "effective_start": "2025-05-01",
                "effective_end": "",
                "reason": "Corrected source entry",
            },
        )
        self.assertRedirects(response, workspace, fetch_redirect_response=False)
        rule.refresh_from_db()
        self.assertEqual(rule.physician, self.other_physician)
        self.assertEqual(rule.periods, ["PM"])

        response = self.client.post(
            reverse("community-preceptor-rule-remove", args=(rule.pk,)),
            {"reason": "Source retired"},
        )
        self.assertRedirects(response, workspace, fetch_redirect_response=False)
        history = self.client.get(reverse("community-preceptor-history"))
        self.assertContains(history, "Expected Tuesday coverage")
        self.assertContains(history, "Corrected source entry")
        self.assertContains(history, "Source retired")
        self.assertContains(history, "2025-02-01")
        self.assertEqual(CommunityPreceptorScheduleChange.objects.count(), 3)

    def test_workspace_views_use_the_shared_frame_and_surface_stack(self):
        views = (
            (reverse("community-preceptor-workspace") + "?month=2025-02", True, "schedule"),
            (reverse("community-preceptor-list") + "?month=2025-02", True, "schedule"),
            (reverse("community-preceptor-rules") + "?month=2025-02", False, "rules"),
        )

        for url, has_month_control, surface_name in views:
            with self.subTest(url=url):
                response = self.client.get(url)

                self.assertTemplateUsed(response, "schedules/schedule_workspace_base.html")
                self.assertTemplateUsed(response, "schedules/schedule_workspace_header.html")
                self.assertContains(
                    response,
                    f'class="workspace-surface-stack" aria-label="Community Precepting {surface_name}"',
                )
                if has_month_control:
                    self.assertTemplateUsed(
                        response, "schedules/schedule_workspace_month_navigation.html"
                    )

    def test_workspace_views_use_actions_and_standard_second_surface_headers(self):
        views = (
            (reverse("community-preceptor-workspace") + "?month=2025-02", None),
            (reverse("community-preceptor-list") + "?month=2025-02", "Shifts"),
            (reverse("community-preceptor-rules") + "?month=2025-02", "Rules"),
        )

        for url, second_surface_title in views:
            with self.subTest(url=url):
                response = self.client.get(url)

                self.assertContains(response, "<h2>Actions</h2>")
                self.assertContains(response, "Community Preceptor history")
                self.assertContains(response, "Add Community Preceptor rule")
                if second_surface_title:
                    self.assertContains(response, f"<h2>{second_surface_title}</h2>")

    def test_calendar_and_table_views_keep_narrow_overflow_inside_their_local_surfaces(self):
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        ).read_text()
        calendar = self.client.get(reverse("community-preceptor-workspace"), {"month": "2025-02"})
        list_response = self.client.get(reverse("community-preceptor-list"), {"month": "2025-02"})
        rules = self.client.get(reverse("community-preceptor-rules"), {"month": "2025-02"})

        self.assertNotContains(calendar, "No projected commitments")
        self.assertTrue(
            all(
                not lane.items for week in calendar.context["calendar"].weeks for lane in week.lanes
            )
        )
        self.assertRegex(
            stylesheet,
            r"\.community-preceptor-calendar-commitments\s*\{\s*position:\s*relative;",
        )
        self.assertContains(
            rules, 'class="workspace-surface surface-frame community-preceptor-rules-section"'
        )
        self.assertContains(list_response, 'class="table-scroll-frame data-table-scroll-frame"')
        self.assertContains(rules, 'class="table-scroll-frame data-table-scroll-frame"')
        self.assertNotIn(".community-preceptor-list-section .surface-body", stylesheet)

    def test_rules_and_list_keep_compact_width_and_localized_dates(self):
        CommunityPreceptorRule.objects.create(
            physician=self.physician, weekday=0, ordinals=[1], periods=["AM"]
        )

        list_response = self.client.get(reverse("community-preceptor-list"), {"month": "2025-02"})
        rules_response = self.client.get(reverse("community-preceptor-rules"), {"month": "2025-02"})

        self.assertContains(
            list_response, 'id="community-preceptor-list" class="data-table data-table--compact"'
        )
        self.assertContains(
            rules_response, 'id="community-preceptor-rules" class="data-table data-table--compact"'
        )
        self.assertContains(list_response, '<time datetime="2025-02-03">Feb. 3, 2025</time>')

    def test_invalid_and_anonymous_submissions_do_not_write_rule_or_history(self):
        create_url = reverse("community-preceptor-rule-create")
        values = {
            "physician": self.physician.pk,
            "weekday": "1",
            "ordinals": ["1"],
            "periods": ["AM"],
            "effective_start": "2025-02-12",
            "effective_end": "2025-02-10",
            "reason": "Invalid range",
        }
        response = self.client.post(create_url, values)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "End date cannot be before start date.")
        self.assertFalse(CommunityPreceptorRule.objects.exists())
        self.assertFalse(CommunityPreceptorScheduleChange.objects.exists())

        values.update(effective_start="", effective_end="", periods=[])
        response = self.client.post(create_url, values)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CommunityPreceptorRule.objects.exists())
        self.assertFalse(CommunityPreceptorScheduleChange.objects.exists())

        self.client.logout()
        response = self.client.post(create_url, values)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CommunityPreceptorRule.objects.exists())
        self.assertFalse(CommunityPreceptorScheduleChange.objects.exists())
        self.assertEqual(self.client.get(reverse("community-preceptor-history")).status_code, 302)

    def test_rule_forms_group_recurrence_coverage_range_and_retained_removal(self):
        add_response = self.client.get(reverse("community-preceptor-rule-create"))
        self.assertContains(add_response, "Community Preceptor rule")
        self.assertContains(add_response, "Recurrence and coverage")
        self.assertContains(add_response, "Effective range")
        self.assertContains(add_response, 'class="form-choice-group"', count=2)

        invalid_response = self.client.post(
            reverse("community-preceptor-rule-create"),
            {
                "physician": self.physician.pk,
                "weekday": "1",
                "ordinals": ["1"],
                "periods": ["AM"],
                "effective_start": "2026-08-12",
                "effective_end": "2026-08-10",
            },
        )
        self.assertContains(invalid_response, 'aria-invalid="true"')
        self.assertContains(invalid_response, 'class="errorlist" id="id_effective_end_error"')

        rule = CommunityPreceptorRule.objects.create(
            physician=self.physician, weekday=1, ordinals=[1], periods=["AM"]
        )
        edit_response = self.client.get(
            reverse("community-preceptor-rule-correct", args=(rule.pk,))
        )
        self.assertContains(edit_response, "Edit Community Preceptor rule")
        assert_removal_section(
            self,
            edit_response,
            "community-removal-heading",
            reverse("community-preceptor-rule-remove", args=(rule.pk,)),
        )
        self.assertContains(edit_response, "retains the prior value and optional note")

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2025, 1, 1, 2, tzinfo=UTC),
    )
    def test_community_rule_entry_uses_local_role_picker_default_and_retained_context(self, _now):
        active = Physician.objects.create(display_name="Community active today")
        past = Physician.objects.create(display_name="Community past only")
        future = Physician.objects.create(display_name="Community future only")
        wrong_role = Physician.objects.create(display_name="Casual but not community")
        no_role = Physician.objects.create(display_name="No community role")
        EffectiveRole.objects.create(
            physician=active,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start="2024-12-31",
        )
        EffectiveRole.objects.create(
            physician=past,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start="2024-01-01",
            effective_end="2024-12-30",
        )
        EffectiveRole.objects.create(
            physician=future,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start="2025-01-01",
        )
        EffectiveRole.objects.create(
            physician=wrong_role,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start="2024-12-31",
        )
        rule = CommunityPreceptorRule.objects.create(
            physician=active,
            weekday=1,
            ordinals=[1, 3],
            periods=["AM"],
            effective_start="2024-12-31",
        )

        response = self.client.get(reverse("community-preceptor-rule-create"))

        self.assertContains(response, 'value="2024-12-31"')
        self.assertContains(
            response, f'<option value="{active.pk}">Community active today</option>'
        )
        for physician in (past, future, wrong_role, no_role):
            self.assertFalse(
                response.context["form"]
                .fields["physician"]
                .queryset.filter(pk=physician.pk)
                .exists()
            )
        self.assertContains(response, "Only one effective rule per weekday can apply at a time.")
        self.assertContains(response, "First, Third")
        self.assertContains(response, "AM")
        self.assertContains(response, 'id="community-preceptor-rule-context" class="data-table"')
        self.assertFalse(CommunityPreceptorScheduleChange.objects.exists())
        self.assertEqual(CommunityPreceptorRule.objects.count(), 1)

        invalid = self.client.post(
            reverse("community-preceptor-rule-create"),
            {
                "physician": active.pk,
                "weekday": "2",
                "ordinals": ["1"],
                "periods": ["AM"],
                "effective_start": "2025-02-12",
                "effective_end": "2025-02-10",
            },
        )
        self.assertContains(invalid, 'value="2025-02-12"')
        self.assertContains(invalid, 'value="2025-02-10"')
        self.assertEqual(CommunityPreceptorRule.objects.count(), 1)

        physician_response = self.client.get(
            reverse("physician-community-preceptor-rule-create", args=(future.pk,))
        )
        self.assertContains(physician_response, 'value="2024-12-31"')
        self.assertContains(physician_response, "Community future only")
        self.assertNotContains(physician_response, 'name="physician"')

        edit = self.client.get(reverse("community-preceptor-rule-correct", args=(rule.pk,)))
        self.assertContains(edit, "Editing this rule")
        self.assertContains(edit, 'value="2024-12-31"')

    def test_calendar_and_list_show_selected_month_commitments_and_preserve_context(self):
        alpha = Physician.objects.create(display_name="Alpha Community")
        beta = Physician.objects.create(display_name="Beta Community")
        alpha_rule = CommunityPreceptorRule.objects.create(
            physician=alpha, weekday=0, ordinals=[1], periods=["AM"]
        )
        beta_rule = CommunityPreceptorRule.objects.create(
            physician=beta, weekday=0, ordinals=[1], periods=["PM"]
        )
        CommunityPreceptorRule.objects.create(
            physician=beta, weekday=0, ordinals=[1], periods=["AM"], effective_start="2025-03-01"
        )
        calendar_url = reverse("community-preceptor-workspace") + "?month=2025-02"
        list_url = reverse("community-preceptor-list") + "?month=2025-02"

        calendar = self.client.get(calendar_url)
        content = calendar.content.decode()

        self.assertEqual(calendar.status_code, 200)
        self.assertTemplateUsed(calendar, "schedules/schedule_workspace_base.html")
        self.assertTemplateUsed(calendar, "schedules/schedule_workspace_month_navigation.html")
        self.assertContains(calendar, 'aria-label="February 2025 Community Precepting calendar"')
        self.assertContains(calendar, 'datetime="2025-01-26"')
        self.assertContains(calendar, 'datetime="2025-03-01"')
        self.assertContains(calendar, "Alpha Community")
        self.assertContains(calendar, "Beta Community")
        self.assertContains(calendar, ">AM</span>")
        self.assertContains(calendar, ">PM</span>")
        self.assertContains(calendar, 'role="row" aria-label="AM"', count=5)
        self.assertContains(calendar, 'role="row" aria-label="PM"', count=5)
        self.assertIn(
            f"/community-preceptors/{alpha_rule.pk}/correct/?view=calendar&amp;month=2025-02",
            content,
        )
        self.assertNotContains(calendar, "Assignment incomplete")
        self.assertNotContains(calendar, "Filled")
        expected_navigation = (
            ("Calendar", reverse("community-preceptor-workspace") + "?month=2025-02"),
            ("List", reverse("community-preceptor-list") + "?month=2025-02"),
            ("Rules", reverse("community-preceptor-rules") + "?month=2025-02"),
        )
        local_navigation = calendar.context["schedule_workspace_navigation"]
        self.assertEqual(
            [item["label"] for item in local_navigation if item["current"]], ["Calendar"]
        )
        rendered_links = assert_rendered_navigation(
            self,
            calendar,
            aria_label="Community Precepting navigation",
            expected=expected_navigation,
            current="Calendar",
        )
        for link, (label, _) in zip(rendered_links, expected_navigation, strict=True):
            with self.subTest(destination=label):
                destination = self.client.get(link.attributes["href"])
                self.assertEqual(destination.status_code, 200)
                destination_navigation = destination.context["schedule_workspace_navigation"]
                self.assertEqual(
                    [entry["label"] for entry in destination_navigation if entry["current"]],
                    [label],
                )
                self.assertEqual(
                    destination.context["community_preceptor_selected_month"], date(2025, 2, 1)
                )
                assert_rendered_navigation(
                    self,
                    destination,
                    aria_label="Community Precepting navigation",
                    expected=expected_navigation,
                    current=label,
                )
        self.assertContains(calendar, 'href="/community-preceptors/?month=2025-01"')
        self.assertContains(calendar, 'href="/community-preceptors/?month=2025-03"')
        self.assertContains(calendar, 'name="month" value="2025-02"')
        self.assertContains(calendar, "data-calendar-period-picker")
        self.assertContains(calendar, '<noscript><button type="submit">View</button></noscript>')
        self.assertContains(
            calendar,
            'href="/community-preceptors/history/?view=calendar&amp;month=2025-02"',
        )
        history = self.client.get(
            reverse("community-preceptor-history"), {"view": "calendar", "month": "2025-02"}
        )
        self.assertContains(
            history,
            'href="/community-preceptors/?month=2025-02">Back to Community Preceptor Schedule</a>',
        )

        list_response = self.client.get(list_url)
        list_content = list_response.content.decode()

        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, "2025-02-03")
        self.assertContains(list_response, "Alpha Community")
        self.assertContains(list_response, "Beta Community")
        self.assertNotContains(list_response, "2025-03-03")
        self.assertLess(list_content.index("Alpha Community"), list_content.index("Beta Community"))
        self.assertIn(
            f"/community-preceptors/{beta_rule.pk}/correct/?view=list&amp;month=2025-02",
            list_content,
        )

        create_url = reverse("community-preceptor-rule-create") + "?view=list&month=2025-02"
        response = self.client.post(
            create_url,
            {
                "physician": self.physician.pk,
                "weekday": "1",
                "ordinals": ["1"],
                "periods": ["AM"],
                "effective_start": "2025-02-01",
                "effective_end": "",
            },
        )
        self.assertRedirects(response, list_url, fetch_redirect_response=False)

    def test_month_views_reject_invalid_month_and_require_authentication(self):
        for view_name in (
            "community-preceptor-workspace",
            "community-preceptor-list",
            "community-preceptor-rules",
        ):
            with self.subTest(view_name=view_name):
                self.assertEqual(
                    self.client.get(reverse(view_name), {"month": "2025-13"}).status_code, 400
                )

        self.client.logout()
        for view_name in (
            "community-preceptor-workspace",
            "community-preceptor-list",
            "community-preceptor-rules",
        ):
            with self.subTest(view_name=view_name):
                self.assertEqual(self.client.get(reverse(view_name)).status_code, 302)


class PhysicianCenteredSourceScheduleViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.client.force_login(self.user)
        self.physician = Physician.objects.create(display_name="Past and future source physician")
        self.other_physician = Physician.objects.create(display_name="Other physician")
        self.never_held_role = Physician.objects.create(display_name="No source roles")
        EffectiveRole.objects.create(
            physician=self.physician,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start="2024-01-01",
            effective_end="2024-12-31",
        )
        EffectiveRole.objects.create(
            physician=self.physician,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            effective_start="2026-01-01",
        )

    def test_detail_keeps_source_sections_for_historical_and_future_roles(self):
        community_rule = CommunityPreceptorRule.objects.create(
            physician=self.physician, weekday=1, ordinals=[1, 3], periods=["PM"]
        )
        casual_rule = CasualRounderRule.objects.create(
            physician=self.physician, weekday=2, ordinals=[2], effective_start="2026-02-01"
        )

        response = self.client.get(reverse("physician-detail", args=(self.physician.pk,)))
        no_role_response = self.client.get(
            reverse("physician-detail", args=(self.never_held_role.pk,))
        )

        self.assertContains(response, "Community Preceptor Schedule")
        self.assertContains(response, "Casual Rounder rules")
        self.assertContains(response, "Tuesday")
        self.assertContains(response, "First, Third")
        self.assertContains(response, "PM")
        self.assertContains(response, "Wednesday")
        self.assertContains(response, "Second")
        self.assertContains(response, "Feb. 1, 2026 through Unbounded future")
        self.assertContains(
            response,
            f'href="/physicians/{self.physician.pk}/community-preceptor-schedule/{community_rule.pk}/correct/">Edit</a>',
        )
        self.assertContains(
            response,
            f'href="/physicians/{self.physician.pk}/casual-rounder-rules/{casual_rule.pk}/remove/">Remove</a>',
        )
        self.assertNotContains(no_role_response, "Community Preceptor Schedule")
        self.assertNotContains(no_role_response, "Casual Rounder rules")

    def test_physician_source_forms_fix_physician_and_retain_shared_records_history_and_return(
        self,
    ):
        detail_url = reverse("physician-detail", args=(self.physician.pk,))
        community_create = reverse(
            "physician-community-preceptor-rule-create", args=(self.physician.pk,)
        )
        casual_create = reverse("physician-casual-rounder-rule-create", args=(self.physician.pk,))

        form = self.client.get(casual_create)
        self.assertContains(form, "Add casual rounder rule")
        self.assertContains(form, "Return to Past and future source physician", count=2)

        form = self.client.get(community_create)
        self.assertContains(form, "Return to Past and future source physician")
        self.assertContains(form, "Physician")
        self.assertNotContains(form, 'name="physician"')
        invalid = self.client.post(
            community_create,
            {
                "physician": self.other_physician.pk,
                "weekday": "1",
                "ordinals": ["1"],
                "periods": ["AM"],
                "effective_start": "2026-02-02",
                "effective_end": "2026-02-01",
            },
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "End date cannot be before start date.")
        self.assertFalse(CommunityPreceptorRule.objects.exists())

        response = self.client.post(
            community_create,
            {
                "physician": self.other_physician.pk,
                "weekday": "1",
                "ordinals": ["1", "3"],
                "periods": ["AM", "PM"],
                "effective_start": "2025-02-01",
                "effective_end": "2025-12-31",
                "reason": "Community source",
            },
        )
        self.assertRedirects(response, detail_url, fetch_redirect_response=False)
        community_rule = CommunityPreceptorRule.objects.get()
        self.assertEqual(community_rule.physician, self.physician)
        self.assertContains(
            self.client.get(reverse("community-preceptor-rules")),
            f'href="/physicians/{self.physician.pk}/">Past and future source physician</a>',
        )

        response = self.client.post(
            casual_create,
            {
                "physician": self.other_physician.pk,
                "weekday": "2",
                "ordinals": ["2"],
                "effective_start": "2026-01-01",
                "effective_end": "",
                "reason": "Casual source",
            },
        )
        self.assertRedirects(response, detail_url, fetch_redirect_response=False)
        casual_rule = CasualRounderRule.objects.get()
        self.assertEqual(casual_rule.physician, self.physician)
        self.assertContains(
            self.client.get(reverse("casual-rounder-workspace")),
            f'href="/physicians/{self.physician.pk}/">Past and future source physician</a>',
        )

        response = self.client.post(
            reverse(
                "physician-community-preceptor-rule-correct",
                args=(self.physician.pk, community_rule.pk),
            ),
            {
                "physician": self.other_physician.pk,
                "weekday": "4",
                "ordinals": ["2"],
                "periods": ["PM"],
                "effective_start": "2025-03-01",
                "effective_end": "",
                "reason": "Corrected community source",
            },
        )
        self.assertRedirects(response, detail_url, fetch_redirect_response=False)
        community_rule.refresh_from_db()
        self.assertEqual(community_rule.physician, self.physician)
        self.assertEqual(community_rule.weekday, 4)

        response = self.client.post(
            reverse(
                "physician-casual-rounder-rule-remove", args=(self.physician.pk, casual_rule.pk)
            ),
            {"reason": "Casual source retired"},
        )
        self.assertRedirects(response, detail_url, fetch_redirect_response=False)
        self.assertFalse(CasualRounderRule.objects.exists())
        self.assertContains(self.client.get(reverse("rounding-history")), "Casual source retired")
        self.assertContains(
            self.client.get(reverse("community-preceptor-history")), "Corrected community source"
        )
        self.assertEqual(CommunityPreceptorScheduleChange.objects.count(), 2)
        self.assertEqual(RoundingScheduleChange.objects.count(), 2)

    def test_physician_source_forms_retain_existing_rules_without_active_state(self):
        rule_contexts = (
            (
                CasualRounderRule,
                {"effective_start": "2026-02-01"},
                "physician-casual-rounder-rule-create",
                "physician-casual-rounder-rule-correct",
            ),
            (
                CommunityPreceptorRule,
                {"periods": ["AM"], "effective_start": "2025-02-01"},
                "physician-community-preceptor-rule-create",
                "physician-community-preceptor-rule-correct",
            ),
        )
        for rule_model, extra_fields, create_name, correct_name in rule_contexts:
            with self.subTest(rule_model=rule_model.__name__):
                current_rule = rule_model.objects.create(
                    physician=self.physician, weekday=1, ordinals=[1], **extra_fields
                )
                rule_model.objects.create(
                    physician=self.physician, weekday=2, ordinals=[2], **extra_fields
                )

                create_response = self.client.get(reverse(create_name, args=(self.physician.pk,)))
                self.assertContains(create_response, "Retained rule", count=2)
                self.assertNotContains(create_response, "Deactivated rule")

                correct_response = self.client.get(
                    reverse(correct_name, args=(self.physician.pk, current_rule.pk))
                )
                self.assertContains(correct_response, "Editing this rule")
                self.assertContains(correct_response, "Retained rule")
                self.assertNotContains(correct_response, "Deactivated rule")

    def test_physician_source_routes_require_authentication_and_scope_rules_to_the_physician(self):
        community_rule = CommunityPreceptorRule.objects.create(
            physician=self.other_physician, weekday=1, ordinals=[1], periods=["AM"]
        )
        casual_rule = CasualRounderRule.objects.create(
            physician=self.other_physician, weekday=1, ordinals=[1]
        )
        self.client.logout()

        self.assertEqual(
            self.client.get(
                reverse("physician-community-preceptor-rule-create", args=(self.physician.pk,))
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.get(
                reverse("physician-casual-rounder-rule-create", args=(self.physician.pk,))
            ).status_code,
            302,
        )
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(
                reverse(
                    "physician-community-preceptor-rule-correct",
                    args=(self.physician.pk, community_rule.pk),
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse(
                    "physician-casual-rounder-rule-remove",
                    args=(self.physician.pk, casual_rule.pk),
                ),
                {"reason": "Should not remove another physician's source"},
            ).status_code,
            404,
        )
        self.assertTrue(CasualRounderRule.objects.filter(pk=casual_rule.pk).exists())


class OptionalChangeNoteViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.user.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.client.force_login(self.user)

    def test_forms_accept_omitted_notes_and_history_marks_their_absence(self):
        response = self.client.get(reverse("physician-create"))
        self.assertContains(response, "History note (optional)")
        self.assertContains(response, "Recorded with this change in history.")
        response = self.client.post(reverse("physician-create"), {"display_name": "Faculty"})
        physician = Physician.objects.get(display_name="Faculty")
        self.assertRedirects(response, reverse("physician-detail", args=(physician.pk,)))
        response = self.client.post(
            reverse("physician-role-add", args=(physician.pk,)),
            {
                "role": EffectiveRole.Role.FACULTY,
                "effective_start": "2025-01-01",
                "effective_end": "",
            },
        )
        self.assertRedirects(response, reverse("physician-detail", args=(physician.pk,)))
        physicians_history = self.client.get(reverse("physician-history", args=(physician.pk,)))
        self.assertContains(physicians_history, "Note")
        self.assertContains(physicians_history, "No note.")

        response = self.client.post(
            reverse("vacation-create"),
            {
                "physician": physician.pk,
                "start_date": "2025-02-03",
                "end_date": "2025-02-03",
                "period_of_day": Vacation.PeriodOfDay.WHOLE_DAY,
            },
        )
        self.assertRedirects(response, reverse("vacation-workspace"), fetch_redirect_response=False)
        vacation_history = self.client.get(reverse("vacation-history"))
        self.assertContains(vacation_history, "Prior note")
        self.assertContains(vacation_history, "No prior note.")
        self.assertContains(vacation_history, "Blank note.")

        session = open_monthly_draft(date(2025, 2, 1)).sessions.get(
            date="2025-02-03", period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        assignment_url = reverse("session-assignment-edit", args=(session.pk, "primary-1"))
        response = self.client.post(assignment_url, {"physician": physician.pk})
        self.assertRedirects(
            response,
            reverse("monthly-workspace") + "?month=2025-02",
            fetch_redirect_response=False,
        )
        assignment_history = self.client.get(
            reverse("session-assignment-history", args=(session.pk,))
        )
        self.assertContains(assignment_history, "No note.")


class RetainedPhysiciansRecordsAdminTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="admin", password="test-password", email="admin@example.com"
        )
        self.client.force_login(self.user)
        self.physician = Physician.objects.create(display_name="Admin Physician")
        self.role = EffectiveRole.objects.create(
            physician=self.physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2025-01-01",
        )
        self.change = PhysicianChange.objects.create(
            physician=self.physician,
            subject=PhysicianChange.Subject.IDENTITY,
            subject_id=self.physician.pk,
            previous_value=None,
            recorded_value={"display_name": self.physician.display_name},
            reason="Initial entry",
            changed_by=self.user,
        )

    def test_admin_allows_inspection_but_denies_mutating_retained_physicians_records(self):
        for record in (self.physician, self.role, self.change):
            with self.subTest(model=record._meta.label):
                url_name = f"admin:{record._meta.app_label}_{record._meta.model_name}"

                inspection = self.client.get(reverse(f"{url_name}_change", args=(record.pk,)))
                self.assertEqual(inspection.status_code, 200)
                self.assertContains(inspection, self.physician.display_name)
                self.assertEqual(self.client.get(reverse(f"{url_name}_add")).status_code, 403)
                self.assertEqual(
                    self.client.post(reverse(f"{url_name}_change", args=(record.pk,))).status_code,
                    403,
                )
                self.assertEqual(
                    self.client.post(reverse(f"{url_name}_delete", args=(record.pk,))).status_code,
                    403,
                )
                self.assertTrue(type(record).objects.filter(pk=record.pk).exists())
