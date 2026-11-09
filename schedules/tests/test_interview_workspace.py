from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from schedules.models import (
    EffectiveRole,
    FacultyAvailabilityTemplate,
    InterviewShift,
    PreceptorAssignment,
    PreceptorSession,
)
from schedules.services.holidays import holiday_closure_for
from schedules.services.interviews import (
    assign_interview_shift,
    create_interview_shift,
    remove_interview_shift,
)
from schedules.services.physicians import (
    create_faculty_availability_template,
    create_physician,
)
from schedules.services.preceptors import assign_preceptor, open_monthly_draft
from schedules.tests.test_table_presentation import _RenderedDataTable
from schedules.tests.test_work_remaining import _RenderedSurfaceParser


def interview_list_row(response, on_date):
    """Return the rendered interview-list record for one dated shift."""
    table = _RenderedDataTable(response.content.decode(), "interview-list")
    rows = [
        row
        for row in table.body_rows()
        if any(
            time.attributes.get("datetime") == on_date.isoformat()
            for time in table.cell_for_header(row, "Date").descendants_named("time")
        )
    ]
    if len(rows) != 1:
        raise AssertionError(
            f"Expected one rendered interview record for {on_date}, found {len(rows)}."
        )
    return table, rows[0]


class InterviewWorkspaceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.user.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.client.force_login(self.user)

    def faculty(self, name, *, available=True, availability_state=None, leadership=""):
        physician = create_physician(display_name=name, scheduler=self.user)
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2024, 7, 1),
            faculty_leadership=leadership,
        )
        if available:
            create_faculty_availability_template(
                physician=physician,
                effective_start=None,
                effective_end=None,
                slots={
                    slot: availability_state or FacultyAvailabilityTemplate.AvailabilityState.OPEN
                    for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
                },
                scheduler=self.user,
            )
        return physician

    def calendar(self, today, academic_year, month=None):
        query = {"academic_year": academic_year}
        if month:
            query["month"] = month
        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(today.year, today.month, today.day, 18, tzinfo=UTC),
        ):
            self.client.force_login(self.user)
            return self.client.get(reverse("interview-workspace"), query)

    def workspace_views(self, today):
        now = datetime(today.year, today.month, today.day, 18, tzinfo=UTC)
        with patch("schedules.services.operational_attention.timezone.now", return_value=now):
            return (
                self.client.get(
                    reverse("interview-workspace"),
                    {"academic_year": 2024, "month": "2025-02"},
                ),
                self.client.get(reverse("interview-list"), {"academic_year": 2024}),
            )

    def schedule_interviews(self, total):
        on_date = date(2024, 7, 1)
        while InterviewShift.objects.filter(is_active=True).count() < total:
            if on_date.weekday() < 5 and not holiday_closure_for(on_date, "AM"):
                create_interview_shift(
                    academic_year=2024,
                    date=on_date,
                    scheduler=self.user,
                )
            on_date += timedelta(days=1)

    def assert_quota_status(self, responses, status, *, indicator=False):
        for view, response in zip(("calendar", "list"), responses, strict=True):
            with self.subTest(view=view, status=status):
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Schedule Interview")
                self.assertContains(response, status)
                if indicator:
                    parser = _RenderedSurfaceParser()
                    parser.feed(response.content.decode())
                    indicators = [
                        element
                        for element in parser.elements
                        if element.attributes.get("role") == "img"
                        and element.attributes.get("aria-label") == status
                    ]
                    self.assertEqual(
                        len(indicators),
                        1,
                        "The quota finding should be exposed by its own accessible marker.",
                    )

    def assert_calendar_month(self, today, academic_year, expected_month, month=None):
        response = self.calendar(today, academic_year, month)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["schedule_workspace_selected_month"], expected_month)

    def test_calendar_and_list_reuse_workspace_navigation_and_expose_incomplete_work(self):
        shift = create_interview_shift(
            academic_year=2024, date=date(2025, 2, 3), scheduler=self.user
        )

        calendar = self.calendar(date(2025, 1, 31), 2024, "2025-02")
        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2025, 1, 31, 18, tzinfo=UTC),
        ):
            listing = self.client.get(reverse("interview-list"), {"academic_year": 2024})
        content = calendar.content.decode()

        self.assertTemplateUsed(calendar, "schedules/schedule_workspace_surface_stack.html")
        self.assertTemplateUsed(calendar, "schedules/interview_calendar_surfaces.html")
        self.assertContains(calendar, 'aria-label="Interviews navigation"')
        self.assertContains(
            calendar,
            'href="/interviews/?academic_year=2024&amp;month=2025-02" aria-current="page">Calendar</a>',
        )
        self.assertContains(calendar, 'href="/interviews/list/?academic_year=2024">List</a>')
        self.assertContains(calendar, "20 required actions remain.")
        self.assertEqual(calendar.context["work_remaining"].required_item_count, 20)
        self.assertEqual(listing.context["work_remaining"].required_item_count, 20)
        self.assertContains(calendar, "surface-tone--danger workspace-work-summary")
        self.assertContains(calendar, "Assignment incomplete")
        self.assertContains(calendar, f"/interviews/{shift.pk}/correct/")
        self.assertContains(calendar, 'action="/interviews/"')
        self.assertContains(calendar, "Academic Year:")
        self.assertContains(calendar, 'aria-label="Academic year navigation"')
        self.assertContains(calendar, '<option value="2024" selected>2024&ndash;2025</option>')
        self.assertContains(calendar, 'name="picker" value="academic_year"')
        self.assertContains(calendar, 'name="month" value="2025-02"')
        self.assertContains(calendar, 'href="/interviews/?academic_year=2023&amp;month=2024-02"')
        self.assertContains(calendar, 'href="/interviews/?academic_year=2025&amp;month=2026-02"')
        self.assertTemplateUsed(
            calendar, "schedules/schedule_workspace_academic_year_month_navigation.html"
        )
        self.assertContains(
            calendar,
            'class="schedule-workspace-period-controls '
            'schedule-workspace-period-controls--academic-year-month"',
        )
        self.assertContains(calendar, 'aria-label="Month navigation"')
        self.assertContains(calendar, 'href="/interviews/?academic_year=2024&amp;month=2025-01"')
        self.assertContains(calendar, 'href="/interviews/?academic_year=2024&amp;month=2025-03"')
        self.assertEqual(
            content.count('<noscript><button type="submit">View</button></noscript>'), 2
        )
        self.assertContains(calendar, '<noscript><button type="submit">View</button></noscript>')
        self.assertContains(
            calendar,
            'class="workspace-surface surface-frame surface-tone--danger workspace-work-summary"',
        )
        self.assertContains(
            calendar, 'class="workspace-surface surface-frame workspace-actions interview-actions"'
        )
        self.assertContains(
            calendar,
            '<header class="surface-header schedule-workspace-actions-header">'
            "<h2>Actions</h2>"
            '<a href="/interviews/history/?academic_year=2024&amp;month=2025-02">Interview history</a>'
            "</header>",
        )
        self.assertLess(content.index(">Interviews<"), content.index(">Physicians<"))
        self.assertContains(listing, 'id="interview-list" class="data-table data-table--compact"')
        self.assertContains(listing, "Mon, Feb 3, 2025")
        self.assertContains(listing, "AM")
        self.assertContains(listing, "Interview history")
        self.assertContains(
            listing,
            'class="workspace-surface surface-frame surface-tone--danger workspace-work-summary"',
        )
        self.assertContains(
            listing, 'class="workspace-surface surface-frame workspace-actions interview-actions"'
        )
        self.assertContains(listing, "<h2>Actions</h2>")

    def test_actions_explain_academic_year_quota_in_calendar_and_list(self):
        self.schedule_interviews(15)
        self.assert_quota_status(
            self.workspace_views(date(2025, 1, 31)),
            "5 interviews remain to be scheduled (15 of 20 scheduled).",
            indicator=True,
        )

        self.schedule_interviews(19)
        self.assert_quota_status(
            self.workspace_views(date(2025, 1, 31)),
            "1 interview remains to be scheduled (19 of 20 scheduled).",
            indicator=True,
        )

        self.schedule_interviews(20)
        self.assert_quota_status(
            self.workspace_views(date(2025, 1, 31)),
            "20 of 20 required interviews scheduled.",
        )

        self.schedule_interviews(21)
        self.assert_quota_status(
            self.workspace_views(date(2025, 1, 31)),
            "1 interview exceeds the required 20 (21 scheduled).",
            indicator=True,
        )

        self.schedule_interviews(22)
        self.assert_quota_status(
            self.workspace_views(date(2025, 1, 31)),
            "2 interviews exceed the required 20 (22 scheduled).",
            indicator=True,
        )
        self.assert_quota_status(
            self.workspace_views(date(2026, 2, 1)),
            "2 interviews exceed the required 20 (22 scheduled).",
        )

    def test_calendar_history_round_trip_preserves_academic_year_and_month(self):
        with patch("schedules.views.interviews._current_academic_year", return_value=2026):
            calendar = self.client.get(
                reverse("interview-workspace"),
                {"academic_year": "2026", "month": "2026-08"},
            )
            history = self.client.get(
                reverse("interview-history"),
                {"academic_year": "2026", "month": "2026-08"},
            )

        self.assertContains(
            calendar,
            'href="/interviews/history/?academic_year=2026&amp;month=2026-08"',
        )
        self.assertContains(
            history,
            'href="/interviews/?academic_year=2026&amp;month=2026-08"',
        )

    def test_academic_year_cluster_is_shared_by_calendar_and_list_and_rejects_outside_years(self):
        with patch("schedules.views.interviews._current_academic_year", return_value=2026):
            calendar = self.client.get(
                reverse("interview-workspace"),
                {"academic_year": "2026", "month": "2027-02"},
            )
            listing = self.client.get(reverse("interview-list"), {"academic_year": "2026"})

            for response, action in (
                (calendar, reverse("interview-workspace")),
                (listing, reverse("interview-list")),
            ):
                with self.subTest(action=action):
                    self.assertEqual(response.status_code, 200)
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
                calendar, 'href="/interviews/?academic_year=2025&amp;month=2026-02"'
            )
            self.assertContains(
                calendar, 'href="/interviews/?academic_year=2027&amp;month=2028-02"'
            )
            self.assertContains(listing, 'href="/interviews/list/?academic_year=2025"')
            self.assertContains(listing, 'href="/interviews/list/?academic_year=2027"')

            previous_calendar = self.client.get(
                reverse("interview-workspace"),
                {"academic_year": "2025", "month": "2026-02"},
            )
            next_list = self.client.get(reverse("interview-list"), {"academic_year": "2027"})
            self.assertEqual(previous_calendar.context["academic_year"], 2025)
            self.assertEqual(
                previous_calendar.context["schedule_workspace_selected_month"], date(2026, 2, 1)
            )
            self.assertEqual(next_list.context["academic_year"], 2027)
            self.assertContains(next_list, "2027&ndash;2028")

            for url in (reverse("interview-workspace"), reverse("interview-list")):
                with self.subTest(url=url, bound="lower"):
                    lower = self.client.get(url, {"academic_year": "2021"})
                    self.assertEqual(lower.status_code, 200)
                    self.assertContains(
                        lower,
                        '<span role="link" aria-disabled="true" aria-label="Previous academic year">&lsaquo;</span>',
                    )
                with self.subTest(url=url, bound="upper"):
                    upper = self.client.get(url, {"academic_year": "2031"})
                    self.assertEqual(upper.status_code, 200)
                    self.assertContains(
                        upper,
                        '<span role="link" aria-disabled="true" aria-label="Next academic year">&rsaquo;</span>',
                    )
                with self.subTest(url=url, bound="outside"):
                    self.assertEqual(
                        self.client.get(url, {"academic_year": "2032"}).status_code,
                        400,
                    )

            self.assertEqual(
                self.client.get(
                    reverse("interview-workspace"),
                    {"picker": "month", "month": "2001-08"},
                ).status_code,
                400,
            )

    def test_calendar_year_cluster_submits_a_native_get_and_keeps_the_month_position(self):
        response = self.client.get(
            reverse("interview-workspace"),
            {"picker": "academic_year", "academic_year": "2025", "month": "2025-02"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["academic_year"], 2025)
        self.assertEqual(response.context["schedule_workspace_selected_month"], date(2026, 2, 1))
        self.assertContains(response, '<option value="2025" selected>2025&ndash;2026</option>')

    def test_calendar_month_navigation_crosses_years_and_stops_at_picker_bounds(self):
        with patch("schedules.views.interviews._current_academic_year", return_value=2026):
            june = self.client.get(
                reverse("interview-workspace"),
                {"academic_year": "2025", "month": "2026-06"},
            )
            july = self.client.get(
                reverse("interview-workspace"),
                {"academic_year": "2026", "month": "2026-07"},
            )
            picker = self.client.get(
                reverse("interview-workspace"),
                {"academic_year": "2025", "picker": "month", "month": "2026-07"},
            )
            lower = self.client.get(
                reverse("interview-workspace"),
                {"academic_year": "2021", "month": "2021-07"},
            )
            upper = self.client.get(
                reverse("interview-workspace"),
                {"academic_year": "2031", "month": "2032-06"},
            )

        self.assertEqual(june.status_code, 200)
        self.assertContains(june, 'href="/interviews/?academic_year=2026&amp;month=2026-07"')
        self.assertEqual(july.context["academic_year"], 2026)
        self.assertEqual(july.context["schedule_workspace_selected_month"], date(2026, 7, 1))
        self.assertContains(july, '<option value="2026" selected>2026&ndash;2027</option>')
        self.assertContains(july, 'href="/interviews/?academic_year=2025&amp;month=2026-06"')
        self.assertEqual(picker.context["academic_year"], 2026)
        self.assertEqual(picker.context["schedule_workspace_selected_month"], date(2026, 7, 1))
        self.assertContains(
            lower,
            '<span role="link" aria-disabled="true" aria-label="Previous month">&lsaquo;</span>',
        )
        self.assertContains(
            upper,
            '<span role="link" aria-disabled="true" aria-label="Next month">&rsaquo;</span>',
        )

    def test_shared_surface_primitives_compose_workspace_and_form_sections(self):
        workspace = self.calendar(date(2025, 1, 31), 2024, "2025-02")
        self.client.force_login(self.user)
        form = self.client.get(reverse("interview-create") + "?academic_year=2024")
        workspace_parser = _RenderedSurfaceParser()
        workspace_parser.feed(workspace.content.decode())
        create_url = reverse("interview-create") + "?academic_year=2024&view=calendar&month=2025-02"
        action_sections = workspace_parser.records_with(
            record_tag="section",
            record_class="workspace-actions",
            detail="Schedule Interview",
            action=create_url,
        )
        self.assertEqual(len(action_sections), 1)
        form_parser = _RenderedSurfaceParser()
        form_parser.feed(form.content.decode())
        self.assertTrue(
            any(
                element.tag == "h1" and "Schedule Interview" in element.text()
                for element in form_parser.elements
            )
        )

    def test_calendar_day_actions_prefill_clicked_dates_and_keep_boundary_return_context(self):
        shift = create_interview_shift(
            academic_year=2025, date=date(2025, 7, 1), scheduler=self.user
        )

        calendar = self.calendar(date(2025, 7, 1), 2025, "2025-07")
        boundary_url = (
            "/interviews/add/?academic_year=2024&amp;view=calendar&amp;month=2025-06"
            "&amp;date=2025-06-29"
        )

        self.assertContains(calendar, 'src="/static/schedules/calendar-day-actions.js"')
        self.assertContains(calendar, 'id="interview-add-2025-06-29"')
        self.assertContains(calendar, f'href="{boundary_url}"')
        self.assertContains(
            calendar,
            'data-calendar-day="2025-06-29" data-calendar-day-action="interview-add-2025-06-29"',
        )
        self.assertContains(
            calendar,
            'role="columnheader" data-calendar-day-action="interview-add-2025-06-29"',
        )
        self.assertContains(calendar, f"/interviews/{shift.pk}/correct/")

        self.client.force_login(self.user)
        form = self.client.get(
            reverse("interview-create")
            + "?academic_year=2024&view=calendar&month=2025-06&date=2025-06-29"
        )

        self.assertEqual(form.status_code, 200)
        self.assertEqual(form.context["form"]["date"].value(), date(2025, 6, 29))
        self.assertEqual(
            form.context["interview_return_url"], "/interviews/?academic_year=2024&month=2025-06"
        )

    def test_workspace_context_uses_interview_projections_and_rejects_invalid_periods(self):
        views = (
            (
                reverse("interview-workspace") + "?academic_year=2024&month=2025-02",
                "interview_calendar_weeks",
            ),
            (reverse("interview-list") + "?academic_year=2024", "interview_shifts"),
        )

        for url, domain_value in views:
            with self.subTest(url=url):
                response = self.client.get(url)
                context = response.context[0].flatten()

                self.assertEqual(response.status_code, 200)
                self.assertIn(domain_value, context)
                self.assertIn("interview_summary", context)
                self.assertNotIn("summary", context)
                self.assertNotIn("shifts", context)
                self.assertNotIn("current_view", context)

        for query in (
            {"academic_year": "not-a-year"},
            {"academic_year": "2024", "month": "2025-13"},
        ):
            with self.subTest(query=query):
                self.assertEqual(
                    self.client.get(reverse("interview-workspace"), query).status_code, 400
                )

    def test_create_uses_october_then_latest_active_weekday_and_normal_tiers(self):
        director = self.faculty(
            "Director", leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR
        )
        self.faculty("Other Faculty")
        create = reverse("interview-create") + "?academic_year=2024&view=calendar&month=2024-10"

        form = self.client.get(create)
        self.assertContains(form, 'value="2024-10-01"')
        self.assertContains(form, "Available Program Directors")
        self.assertContains(form, "Program Director preference")
        self.assertContains(form, "No Faculty assignment")
        self.assertNotContains(form, "Human override")

        response = self.client.post(
            create,
            {"date": "2024-10-01", "physician": director.pk, "note": "First date"},
        )
        self.assertRedirects(
            response,
            reverse("interview-workspace") + "?academic_year=2024&month=2024-10",
            fetch_redirect_response=False,
        )
        shift = InterviewShift.objects.get(date=date(2024, 10, 1))
        self.assertEqual(shift.physician, director)

        next_form = self.client.get(create)
        self.assertContains(next_form, 'value="2024-10-02"')

    def test_faculty_assignment_picker_orders_groups_and_explains_overrides(self):
        director = self.faculty(
            "Director", leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR
        )
        other = self.faculty("Other Faculty")
        backup = self.faculty(
            "Backup Faculty",
            availability_state=FacultyAvailabilityTemplate.AvailabilityState.BACKUP_ONLY,
        )
        unavailable = self.faculty("Unavailable Faculty", available=False)
        conflict = self.faculty("Conflicting Faculty")
        nonfaculty = create_physician(display_name="Not Faculty", scheduler=self.user)
        session = open_monthly_draft(date(2025, 2, 1)).sessions.get(
            date=date(2025, 2, 3), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        assign_preceptor(
            session=session,
            physician=conflict,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.user,
        )

        form = self.client.get(reverse("interview-create") + "?academic_year=2024&date=2025-02-03")
        parser = _RenderedSurfaceParser()
        parser.feed(form.content.decode())
        pickers = [
            element
            for element in parser.elements
            if element.tag == "select" and element.attributes.get("name") == "physician"
        ]
        self.assertEqual(len(pickers), 1)
        picker = pickers[0]
        tiers = [child for child in picker.children if getattr(child, "tag", None) == "optgroup"]
        options = {
            option.attributes.get("value"): option
            for option in picker.descendants()
            if option.tag == "option"
        }
        unassigned = options[""]
        self.assertIn("No Faculty assignment", unassigned.text())
        self.assertNotIn("disabled", unassigned.attributes)
        self.assertIn(str(director.pk), options)
        self.assertIn(str(other.pk), options)
        self.assertIn(str(backup.pk), options)
        self.assertIn(str(unavailable.pk), options)
        self.assertIn(str(conflict.pk), options)
        self.assertNotIn(str(nonfaculty.pk), options)
        self.assertIn(director.display_name, options[str(director.pk)].text())
        self.assertIn(other.display_name, options[str(other.pk)].text())
        self.assertIn(backup.display_name, options[str(backup.pk)].text())
        self.assertIn(unavailable.display_name, options[str(unavailable.pk)].text())
        self.assertIn(conflict.display_name, options[str(conflict.pk)].text())
        self.assertLess(
            tiers.index(options[str(director.pk)].parent),
            tiers.index(options[str(other.pk)].parent),
        )
        self.assertLess(
            tiers.index(options[str(other.pk)].parent), tiers.index(options[str(backup.pk)].parent)
        )
        self.assertLess(
            tiers.index(options[str(backup.pk)].parent),
            tiers.index(options[str(unavailable.pk)].parent),
        )
        self.assertEqual(options[str(unavailable.pk)].parent, options[str(conflict.pk)].parent)
        self.assertNotIn("disabled", options[str(unavailable.pk)].attributes)
        self.assertNotIn("disabled", options[str(conflict.pk)].attributes)
        self.assertIn("Backup-only preference", options[str(backup.pk)].text())
        self.assertIn("unavailable", options[str(unavailable.pk)].text().casefold())
        self.assertIn("hard finding", options[str(unavailable.pk)].text())
        self.assertIn("AM Preceptor", options[str(conflict.pk)].text())
        self.assertIn("hard finding", options[str(conflict.pk)].text())

    def test_picker_rebuilds_for_submitted_date_and_rejects_forged_nonfaculty_choices(self):
        dated_faculty = create_physician(display_name="Dated Faculty", scheduler=self.user)
        EffectiveRole.objects.create(
            physician=dated_faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 2, 3),
            effective_end=date(2025, 2, 3),
        )
        create_faculty_availability_template(
            physician=dated_faculty,
            effective_start=date(2025, 2, 3),
            effective_end=date(2025, 2, 3),
            slots={
                slot: FacultyAvailabilityTemplate.AvailabilityState.OPEN
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
            scheduler=self.user,
        )
        nonfaculty = create_physician(display_name="Not Faculty", scheduler=self.user)
        create = reverse("interview-create") + "?academic_year=2024&date=2025-02-03"

        initial = self.client.get(create)
        self.assertContains(initial, dated_faculty.display_name)
        changed_date = self.client.post(
            create,
            {"date": "2025-02-04", "physician": dated_faculty.pk, "note": "Keep this note"},
        )
        forged = self.client.post(
            create,
            {"date": "2025-02-03", "physician": nonfaculty.pk, "note": "Forged"},
        )

        self.assertContains(changed_date, "Select a valid choice.")
        self.assertContains(changed_date, 'value="2025-02-04"')
        self.assertContains(changed_date, "Keep this note")
        self.assertContains(forged, "Select a valid choice.")
        self.assertEqual(InterviewShift.objects.count(), 0)

    def test_override_picker_dispatches_authority_and_edit_keeps_current_override_selected(self):
        unavailable = self.faculty("Unavailable", available=False)
        available = self.faculty("Available")
        create = reverse("interview-create") + "?academic_year=2024&view=list"

        response = self.client.post(
            create,
            {"date": "2025-02-03", "physician": unavailable.pk, "note": "Override"},
        )
        self.assertRedirects(
            response,
            reverse("interview-list") + "?academic_year=2024",
            fetch_redirect_response=False,
        )
        shift = InterviewShift.objects.get(date=date(2025, 2, 3))
        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2025, 1, 31, 18, tzinfo=UTC),
        ):
            listing = self.client.get(reverse("interview-list"), {"academic_year": 2024})
        self.assertContains(
            listing, "Assigned physician is not available for this Interview shift."
        )

        edit = self.client.get(reverse("interview-correct", args=(shift.pk,)) + "?view=list")
        self.assertContains(edit, "Remove Interview")
        self.assertContains(edit, "Override-required Faculty")
        self.assertContains(
            edit,
            f'<option value="{unavailable.pk}" selected>Unavailable | Tier 0 unavailable; selection records a hard finding.</option>',
        )
        response = self.client.post(
            reverse("interview-correct", args=(shift.pk,)) + "?view=list",
            {"date": "2025-02-04", "physician": available.pk, "note": "Corrected"},
        )
        self.assertRedirects(response, reverse("interview-list") + "?academic_year=2024")
        shift.refresh_from_db()
        self.assertEqual(shift.date, date(2025, 2, 4))
        self.assertEqual(shift.physician, available)
        self.assertEqual(shift.note, "Corrected")
        response = self.client.post(
            reverse("interview-remove", args=(shift.pk,)) + "?view=list", {}
        )
        self.assertRedirects(response, reverse("interview-list") + "?academic_year=2024")
        shift.refresh_from_db()
        self.assertFalse(shift.is_active)
        self.assertIsNone(shift.physician)

    def test_list_emphasizes_only_the_unavailable_physician_hard_finding(self):
        unavailable = self.faculty("Unavailable", available=False)
        conflicted = self.faculty("Conflicted")
        ordinary = self.faculty("Ordinary")
        unavailable_shift = create_interview_shift(
            academic_year=2024, date=date(2025, 2, 3), scheduler=self.user
        )
        conflicted_shift = create_interview_shift(
            academic_year=2024, date=date(2025, 2, 4), scheduler=self.user
        )
        ordinary_shift = create_interview_shift(
            academic_year=2024, date=date(2025, 2, 5), scheduler=self.user
        )
        assign_interview_shift(
            shift=unavailable_shift, physician=unavailable, scheduler=self.user, override=True
        )
        assign_interview_shift(shift=conflicted_shift, physician=conflicted, scheduler=self.user)
        assign_interview_shift(shift=ordinary_shift, physician=ordinary, scheduler=self.user)
        session = open_monthly_draft(date(2025, 2, 1)).sessions.get(
            date=date(2025, 2, 4), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        assign_preceptor(
            session=session,
            physician=conflicted,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.user,
        )

        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2025, 1, 31, 18, tzinfo=UTC),
        ):
            response = self.client.get(reverse("interview-list"), {"academic_year": 2024})
        table, unavailable_row = interview_list_row(response, unavailable_shift.date)
        _, conflicted_row = interview_list_row(response, conflicted_shift.date)
        _, ordinary_row = interview_list_row(response, ordinary_shift.date)
        self.assertIn("data-table-row--danger", unavailable_row.attributes.get("class", "").split())
        self.assertEqual(
            table.cell_for_header(unavailable_row, "Faculty").text().strip(),
            unavailable.display_name,
        )
        self.assertRegex(
            table.cell_for_header(unavailable_row, "Findings").text().casefold(),
            r"\b(?:not available|unavailable)\b",
        )
        self.assertIn(
            "data-table-row--attention", conflicted_row.attributes.get("class", "").split()
        )
        self.assertIn(
            "also assigned to the AM Preceptor Schedule",
            table.cell_for_header(conflicted_row, "Findings").text(),
        )
        self.assertNotIn("data-table-row--", ordinary_row.attributes.get("class", ""))
        self.assertEqual(table.cell_for_header(ordinary_row, "Findings").text(), "None")

    def test_partial_academic_year_keeps_historical_findings_neutral_in_calendar_and_list(self):
        unavailable = self.faculty("Unavailable", available=False)
        historical_shift = create_interview_shift(
            academic_year=2024, date=date(2025, 2, 3), scheduler=self.user
        )
        future_shift = create_interview_shift(
            academic_year=2024, date=date(2025, 2, 11), scheduler=self.user
        )
        for shift in (historical_shift, future_shift):
            assign_interview_shift(
                shift=shift, physician=unavailable, scheduler=self.user, override=True
            )

        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2025, 2, 10, 18, tzinfo=UTC),
        ):
            calendar = self.client.get(
                reverse("interview-workspace"), {"academic_year": 2024, "month": "2025-02"}
            )
            listing = self.client.get(reverse("interview-list"), {"academic_year": 2024})

        for response in (calendar, listing):
            shifts = {shift.pk: shift for shift in response.context["interview_shifts"]}
            self.assertEqual(shifts[historical_shift.pk].findings, ())
            self.assertFalse(shifts[historical_shift.pk].operational_attention)
            self.assertEqual(
                tuple(finding.code for finding in shifts[future_shift.pk].findings),
                ("not_available_assignment",),
            )
            self.assertTrue(shifts[future_shift.pk].operational_attention)
            self.assertEqual(response.context["interview_finding_tone"], "hard")
            self.assertEqual(response.context["work_remaining"].required_item_count, 19)

        calendar_items = {
            item.content.url: item.content
            for week in calendar.context["calendar"].weeks
            for lane in week.lanes
            for item in lane.items
            if getattr(item.content, "url", "")
        }
        historical_calendar_item = next(
            content
            for url, content in calendar_items.items()
            if f"/interviews/{historical_shift.pk}/correct/" in url
        )
        future_calendar_item = next(
            content
            for url, content in calendar_items.items()
            if f"/interviews/{future_shift.pk}/correct/" in url
        )
        self.assertEqual(historical_calendar_item.findings, ())
        self.assertFalse(historical_calendar_item.attention)
        self.assertEqual(historical_calendar_item.finding_tone, "")
        self.assertEqual(
            tuple(finding.severity for finding in future_calendar_item.findings), ("hard",)
        )
        self.assertTrue(future_calendar_item.attention)
        self.assertEqual(future_calendar_item.finding_tone, "hard")

        table, historical_list_row = interview_list_row(listing, historical_shift.date)
        _, future_list_row = interview_list_row(listing, future_shift.date)
        self.assertNotIn("data-table-row--", historical_list_row.attributes.get("class", ""))
        self.assertEqual(table.cell_for_header(historical_list_row, "Findings").text(), "None")
        self.assertIn("data-table-row--danger", future_list_row.attributes.get("class", "").split())
        self.assertEqual(
            table.cell_for_header(future_list_row, "Faculty").text().strip(),
            unavailable.display_name,
        )
        self.assertRegex(
            table.cell_for_header(future_list_row, "Findings").text().casefold(),
            r"\b(?:not available|unavailable)\b",
        )

    def test_partial_current_range_excludes_historical_findings_and_unassigned_shifts(self):
        available = self.faculty("Available")
        unavailable = self.faculty("Unavailable", available=False)
        self.schedule_interviews(20)
        shifts = tuple(InterviewShift.objects.order_by("date", "pk"))
        historical_hard_shift, historical_unassigned_shift, *assigned_shifts = shifts
        assign_interview_shift(
            shift=historical_hard_shift,
            physician=unavailable,
            scheduler=self.user,
            override=True,
        )
        for shift in assigned_shifts:
            assign_interview_shift(shift=shift, physician=available, scheduler=self.user)

        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2025, 2, 10, 18, tzinfo=UTC),
        ):
            calendar = self.client.get(
                reverse("interview-workspace"), {"academic_year": 2024, "month": "2025-02"}
            )
            listing = self.client.get(reverse("interview-list"), {"academic_year": 2024})

        for response in (calendar, listing):
            self.assertEqual(response.context["work_remaining"].required_item_count, 0)
            self.assertEqual(response.context["work_remaining"].advisory_item_count, 0)
            self.assertEqual(response.context["work_remaining"].tone, "clear")
            self.assertEqual(response.context["interview_finding_tone"], "")
            self.assertContains(response, "No work remains in this range.")

        list_rows = {
            row.cells[0].machine_value: row for row in listing.context["interview_list_table"].rows
        }
        self.assertEqual(list_rows[historical_hard_shift.date.isoformat()].tone, "")
        self.assertEqual(list_rows[historical_unassigned_shift.date.isoformat()].tone, "")

    def test_calendar_finding_tones_override_assignment_attention(self):
        unavailable = self.faculty("Unavailable", available=False)
        backup_only = self.faculty(
            "Backup only",
            availability_state=FacultyAvailabilityTemplate.AvailabilityState.BACKUP_ONLY,
        )
        ordinary = self.faculty("Ordinary")
        create = reverse("interview-create") + "?academic_year=2024&view=calendar&month=2025-02"
        for on_date, physician in (
            ("2025-02-03", unavailable),
            ("2025-02-04", backup_only),
            ("2025-02-05", ordinary),
        ):
            response = self.client.post(create, {"date": on_date, "physician": physician.pk})
            self.assertEqual(response.status_code, 302)

        calendar = self.calendar(date(2025, 1, 31), 2024, "2025-02")
        presentations = {
            item.content.physician_name: item.content
            for week in calendar.context["calendar"].weeks
            for lane in week.lanes
            for item in lane.items
        }

        self.assertEqual(presentations["Unavailable"].finding_tone, "hard")
        self.assertEqual(presentations["Backup only"].finding_tone, "preference")
        self.assertEqual(presentations["Ordinary"].finding_tone, "")
        self.assertContains(calendar, "interview-calendar-shift--hard")
        self.assertContains(calendar, "interview-calendar-shift--preference")
        self.assertContains(calendar, "surface-tone--danger workspace-work-summary")

    def test_duplicate_and_out_of_year_dates_are_rejected_without_mutating_a_shift(self):
        existing = create_interview_shift(
            academic_year=2024, date=date(2025, 2, 3), scheduler=self.user
        )
        create = reverse("interview-create") + "?academic_year=2024"

        duplicate = self.client.post(create, {"date": "2025-02-03", "note": "Duplicate"})
        outside_year = self.client.post(create, {"date": "2025-07-01", "note": "Outside"})

        self.assertContains(
            duplicate, "An active Interview shift is already scheduled for this date."
        )
        self.assertContains(
            outside_year, "Interview shift dates must fall within the selected academic year."
        )
        self.assertEqual(InterviewShift.objects.filter(is_active=True).count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.note, "")

    def test_completed_message_requires_assigned_shifts_without_hard_findings(self):
        physician = self.faculty("Faculty")
        for day in range(1, 32):
            on_date = date(2024, 7, day)
            if (
                on_date.weekday() < 5
                and not holiday_closure_for(on_date, "AM")
                and InterviewShift.objects.count() < 20
            ):
                shift = create_interview_shift(
                    academic_year=2024, date=on_date, scheduler=self.user
                )
                assign_interview_shift(shift=shift, physician=physician, scheduler=self.user)

        with patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(2024, 7, 1, 18, tzinfo=UTC),
        ):
            response = self.client.get(reverse("interview-list"), {"academic_year": 2024})

        self.assertContains(response, "No work remains in this range.")
        self.assertContains(response, "surface-tone--clear workspace-work-summary")

    def test_historical_calendar_is_neutral_while_list_keeps_global_attention(self):
        shift = create_interview_shift(
            academic_year=2025, date=date(2025, 7, 1), scheduler=self.user
        )
        now = datetime(2026, 2, 1, 18, tzinfo=UTC)

        with patch("schedules.services.operational_attention.timezone.now", return_value=now):
            calendar = self.client.get(
                reverse("interview-workspace"), {"academic_year": 2025, "month": "2025-07"}
            )
            listing = self.client.get(reverse("interview-list"), {"academic_year": 2025})

        self.assertContains(calendar, "Work remaining is not evaluated for this range.")
        self.assertContains(calendar, "surface-tone--neutral workspace-work-summary")
        self.assertNotContains(calendar, "Interview schedule for 2025")
        self.assertContains(listing, "19 required actions remain.")
        self.assertContains(listing, "surface-tone--danger workspace-work-summary")
        self.assertNotContains(calendar, "None recorded")
        self.assertContains(listing, "None recorded")
        self.assertContains(calendar, "Interview AM")
        self.assertContains(calendar, f"/interviews/{shift.pk}/correct/")
        self.assertContains(calendar, '<span class="interview-edit-link">Edit</span>')
        self.assertNotContains(calendar, "<strong>Interview AM</strong><span></span>")
        self.assertContains(listing, f"/interviews/{shift.pk}/correct/")

    def test_current_year_early_season_uses_the_earlier_first_active_shift_or_october(self):
        cases = (
            (date(2024, 7, 15), date(2024, 7, 8), date(2024, 7, 1)),
            (date(2025, 8, 15), date(2025, 11, 3), date(2025, 10, 1)),
            (date(2026, 9, 15), date(2026, 9, 2), date(2026, 9, 1)),
        )

        for today, shift_date, expected_month in cases:
            with self.subTest(today=today):
                academic_year = today.year
                create_interview_shift(
                    academic_year=academic_year, date=shift_date, scheduler=self.user
                )
                self.assert_calendar_month(today, academic_year, expected_month)

    def test_current_year_midseason_defaults_to_the_current_month(self):
        for today in (
            date(2024, 10, 15),
            date(2025, 11, 15),
            date(2025, 12, 15),
            date(2026, 1, 15),
        ):
            with self.subTest(today=today):
                self.assert_calendar_month(
                    today, today.year if today.month >= 7 else today.year - 1, today.replace(day=1)
                )

    def test_current_year_late_season_uses_the_later_last_active_shift_or_january(self):
        cases = (
            (date(2025, 2, 15), date(2024, 12, 2), date(2025, 1, 1)),
            (date(2026, 3, 15), date(2026, 3, 2), date(2026, 3, 1)),
            (date(2027, 4, 15), date(2027, 4, 2), date(2027, 4, 1)),
            (date(2028, 5, 15), date(2028, 5, 2), date(2028, 5, 1)),
            (date(2029, 6, 15), date(2029, 6, 2), date(2029, 6, 1)),
        )

        for today, shift_date, expected_month in cases:
            with self.subTest(today=today):
                academic_year = today.year - 1
                create_interview_shift(
                    academic_year=academic_year, date=shift_date, scheduler=self.user
                )
                self.assert_calendar_month(today, academic_year, expected_month)

    def test_current_year_empty_early_and_late_seasons_use_october_and_january(self):
        self.assert_calendar_month(date(2030, 7, 15), 2030, date(2030, 10, 1))
        self.assert_calendar_month(date(2031, 2, 15), 2030, date(2031, 1, 1))

    def test_removed_interview_shifts_do_not_influence_the_default_month(self):
        removed = create_interview_shift(
            academic_year=2031, date=date(2032, 6, 2), scheduler=self.user
        )
        create_interview_shift(academic_year=2031, date=date(2031, 12, 2), scheduler=self.user)
        remove_interview_shift(shift=removed, scheduler=self.user)

        self.assert_calendar_month(date(2032, 2, 15), 2031, date(2032, 1, 1))

    def test_noncurrent_year_defaults_to_october_and_explicit_month_is_preserved(self):
        create_interview_shift(academic_year=2031, date=date(2031, 7, 2), scheduler=self.user)

        self.assert_calendar_month(date(2032, 8, 15), 2031, date(2031, 10, 1))
        self.assert_calendar_month(date(2032, 8, 15), 2032, date(2033, 4, 1), "2033-04")
