import re
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from schedules.academic_years import academic_year_for_date
from schedules.models import (
    AcademicEvent,
    AcademicEventDefinition,
    CustomAvailabilityCategory,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    Physician,
    PreceptorAssignment,
    PreceptorSession,
    SchedulePeriod,
    Vacation,
)
from schedules.services.faculty_admin_time import (
    can_view_faculty_admin_time_report,
)
from schedules.tests.test_table_presentation import _RenderedDataTable, _RenderedTableParser


class FacultyAdminTimeReportTests(TestCase):
    today = date(2025, 2, 15)

    def setUp(self):
        User = get_user_model()
        self.program_director = User.objects.create_user(
            username="program-director", password="test-password"
        )
        self.former_program_director = User.objects.create_user(
            username="former-program-director", password="test-password"
        )
        self.future_program_director = User.objects.create_user(
            username="future-program-director", password="test-password"
        )
        self.faculty_user = User.objects.create_user(username="faculty", password="test-password")
        self.unlinked_user = User.objects.create_user(username="unlinked", password="test-password")
        self.scheduler = User.objects.create_user(username="scheduler", password="test-password")
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.staff = User.objects.create_user(
            username="staff", password="test-password", is_staff=True
        )
        self.superuser = User.objects.create_superuser(
            username="superuser", password="test-password", email="superuser@example.com"
        )
        self.pd_physician = self._physician_with_role(
            "Current Program Director",
            self.program_director,
            leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
        )
        self._physician_with_role(
            "Former Program Director",
            self.former_program_director,
            leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
            end=date(2025, 2, 14),
        )
        self._physician_with_role(
            "Future Program Director",
            self.future_program_director,
            leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
            start=date(2025, 2, 16),
        )
        self.faculty_physician = self._physician_with_role("Faculty", self.faculty_user)

    def _physician_with_role(self, name, user, *, leadership="", start=date(2024, 1, 1), end=None):
        physician = Physician.objects.create(display_name=name, user=user)
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            faculty_leadership=leadership,
            effective_start=start,
            effective_end=end,
        )
        return physician

    def _availability_template(self, physician, slots, *, start=date(2024, 1, 1), end=None):
        selections = {
            slot: FacultyAvailabilityTemplate.base_selection("off")
            for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
        }
        selections.update(slots)
        FacultyAvailabilityTemplate.objects.create(
            physician=physician,
            effective_start=start,
            effective_end=end,
            slots=selections,
        )

    def _rendered_admin_time_table(self, response):
        return _RenderedDataTable(response.content.decode(), "faculty-admin-time")

    def _admin_time_row(self, table, physician):
        rows = [
            row
            for row in table.body_rows()
            if table.cell_for_header(row, "Physician").text().strip() == physician.display_name
        ]
        self.assertEqual(len(rows), 1, f"Expected one Admin Time row for {physician.display_name}.")
        return rows[0]

    def _admin_time_cell(self, table, physician, field):
        return table.cell_for_header(self._admin_time_row(table, physician), field)

    def assert_hours_cell(self, cell, hours, periods):
        rendered = re.fullmatch(
            r"(?P<hours>[0-9]+(?:\.[0-9]+)?)\s+hours\s+\(\s*"
            r"(?P<periods>[0-9]+(?:\.[0-9]+)?)\s+periods?\s*\)",
            cell.text().strip(),
        )
        self.assertIsNotNone(rendered, cell.text())
        self.assertEqual(Decimal(rendered["hours"]), hours)
        self.assertEqual(Decimal(rendered["periods"]), Decimal(periods))

    def assert_neutral_cell(self, cell):
        tone_classes = {
            "data-table-cell--positive",
            "data-table-cell--danger",
            "data-table-status--positive",
            "data-table-status--danger",
            "data-table-row--positive",
            "data-table-row--danger",
        }

        def descendants(element):
            for child in element.children:
                if isinstance(child, str):
                    continue
                yield child
                yield from descendants(child)

        styled_elements = [cell, *descendants(cell)]
        row = next((ancestor for ancestor in cell.ancestors() if ancestor.tag == "tr"), None)
        if row is not None:
            styled_elements.append(row)
        for element in styled_elements:
            with self.subTest(element=element.tag):
                self.assertFalse(tone_classes & set(element.attributes.get("class", "").split()))

    def assert_blocked_time_cell(self, cell, *, hours, periods, sources):
        text = cell.text().strip()
        total = re.match(
            r"(?P<hours>[0-9]+(?:\.[0-9]+)?)\s+hours\s+\(\s*"
            r"(?P<periods>[0-9]+)\s+periods?\s*\)\s+removed(?=\s|$|[·;,])",
            text,
        )
        self.assertIsNotNone(total, text)
        self.assertEqual(Decimal(total["hours"]), hours)
        self.assertEqual(int(total["periods"]), periods)
        for source, source_periods in sources.items():
            with self.subTest(source=source):
                matches = list(
                    re.finditer(
                        rf"{re.escape(source)}\s*\(\s*(?P<periods>[0-9]+)\s+periods?\s*\)",
                        text,
                    )
                )
                self.assertEqual(len(matches), 1, text)
                self.assertEqual(int(matches[0]["periods"]), source_periods)
        self.assertRegex(text, r"\bsource\s+counts\s+may\s+overlap\b")

    def assert_labeled_hours(self, text, label, expected):
        matches = list(
            re.finditer(
                rf"(?P<hours>[0-9]+(?:\.[0-9]+)?)\s+hours\s+{label}(?=\s|$|[·;|/])",
                text,
            )
        )
        self.assertEqual(len(matches), 1, text)
        self.assertEqual(Decimal(matches[0]["hours"]), expected)

    def assert_goal_cell(self, cell, *, target, ten_percent=None, cap=None, status, tone):
        text = cell.text().strip()
        self.assert_labeled_hours(text, r"target", target)
        if ten_percent is not None:
            self.assert_labeled_hours(text, r"\(\s*10%\s+Total\s+worked\s*\)", ten_percent)
        if cap is not None:
            self.assert_labeled_hours(text, r"prorated\s+cap", cap)
        status_pattern = re.compile(rf"(?:^|[·;|/])\s*{re.escape(status)}\s*$")
        self.assertRegex(text, status_pattern)
        status_elements = [
            element
            for element in cell.descendants_named("span")
            if status_pattern.search(element.text().strip())
            and f"data-table-status--{tone}" in element.attributes.get("class", "").split()
        ]
        self.assertTrue(status_elements, cell.text())

    def _rendered_document(self, response):
        parser = _RenderedTableParser()
        parser.feed(response.content.decode())
        return parser.root

    def _assignment(self, physician, on_date, period_of_day):
        period = SchedulePeriod.objects.get_or_create(month=date(2025, 2, 1))[0]
        session = PreceptorSession.objects.create(
            period=period,
            date=on_date,
            period_of_day=period_of_day,
        )
        PreceptorAssignment.objects.create(
            session=session,
            physician=physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )

    def test_authorization_requires_a_linked_current_program_director(self):
        authorized = (self.program_director,)
        unauthorized = (
            self.former_program_director,
            self.future_program_director,
            self.faculty_user,
            self.unlinked_user,
            self.scheduler,
            self.staff,
            self.superuser,
        )

        for user in authorized:
            with self.subTest(username=user.username):
                self.assertTrue(can_view_faculty_admin_time_report(user, today=self.today))
        for user in unauthorized:
            with self.subTest(username=user.username):
                self.assertFalse(can_view_faculty_admin_time_report(user, today=self.today))

    @patch("schedules.services.faculty_admin_time.organization_today", return_value=today)
    @patch("schedules.views.reports.organization_today", return_value=today)
    def test_route_and_navigation_follow_the_current_program_director_gate(
        self, _view_today, _service_today
    ):
        route = reverse("faculty-admin-time-report")
        users = (
            (self.program_director, True),
            (self.former_program_director, False),
            (self.future_program_director, False),
            (self.faculty_user, False),
            (self.unlinked_user, False),
            (self.scheduler, False),
            (self.staff, False),
            (self.superuser, False),
        )

        for user, allowed in users:
            with self.subTest(username=user.username):
                self.client.force_login(user)
                response = self.client.get(route, {"month": "2025-02", "coverage": "history"})
                self.assertEqual(response.status_code, 200 if allowed else 403)
                navigation = self.client.get(reverse("schedule-reports"))
                if allowed:
                    self.assertContains(navigation, "Admin Time")
                else:
                    self.assertNotContains(navigation, "Admin Time")

    @patch("schedules.services.faculty_admin_time.organization_today", return_value=today)
    @patch("schedules.views.reports.organization_today", return_value=today)
    def test_controls_render_distinct_coverage_inputs_and_preserve_navigation(
        self, _view_today, _service_today
    ):
        admin = CustomAvailabilityCategory.objects.get(
            system_key=CustomAvailabilityCategory.SystemKey.ADMIN
        )
        self._availability_template(
            self.pd_physician,
            {"monday_am": FacultyAvailabilityTemplate.base_selection("open")},
            end=date(2025, 2, 14),
        )
        self._availability_template(
            self.pd_physician,
            {"tuesday_pm": FacultyAvailabilityTemplate.category_selection(admin)},
            start=date(2025, 2, 15),
        )
        self.client.force_login(self.program_director)

        history = self.client.get(
            reverse("faculty-admin-time-report"), {"month": "2025-02", "coverage": "history"}
        )
        scheduled = self.client.get(
            reverse("faculty-admin-time-report"), {"month": "2025-02", "coverage": "scheduled"}
        )

        history_table = self._rendered_admin_time_table(history)
        scheduled_table = self._rendered_admin_time_table(scheduled)
        self.assert_hours_cell(
            self._admin_time_cell(history_table, self.pd_physician, "Open"), Decimal("8"), 2
        )
        self.assert_hours_cell(
            self._admin_time_cell(history_table, self.pd_physician, "Admin"), Decimal("0"), 0
        )
        self.assert_hours_cell(
            self._admin_time_cell(scheduled_table, self.pd_physician, "Open"), Decimal("0"), 0
        )
        self.assert_hours_cell(
            self._admin_time_cell(scheduled_table, self.pd_physician, "Admin"), Decimal("8"), 2
        )

        document = self._rendered_document(scheduled)

        def is_admin_time_control(element):
            return any(
                ancestor.tag == "form" and "data-faculty-admin-time-controls" in ancestor.attributes
                for ancestor in element.ancestors()
            )

        selected_coverage = [
            element
            for element in document.descendants_named("input")
            if is_admin_time_control(element)
            and element.attributes.get("name") == "coverage"
            and element.attributes.get("value") == "scheduled"
            and element.attributes.get("type") == "radio"
        ]
        self.assertEqual(len(selected_coverage), 1)
        self.assertIn("checked", selected_coverage[0].attributes)
        month_controls = [
            element
            for element in document.descendants_named("input")
            if is_admin_time_control(element) and element.attributes.get("name") == "month"
        ]
        self.assertEqual(
            [element.attributes.get("value") for element in month_controls], ["2025-02"]
        )
        navigation_queries = [
            parse_qs(urlparse(element.attributes.get("href", "")).query)
            for element in document.descendants_named("a")
            if urlparse(element.attributes.get("href", "")).path
            == reverse("faculty-admin-time-report")
        ]
        for month in ("2025-01", "2025-03"):
            with self.subTest(month=month):
                self.assertTrue(
                    any(
                        query.get("month") == [month] and query.get("coverage") == ["scheduled"]
                        for query in navigation_queries
                    )
                )
        self.assertEqual(
            self.client.get(
                reverse("faculty-admin-time-report"), {"month": "2025-02", "coverage": "forged"}
            ).status_code,
            400,
        )

    @patch("schedules.services.faculty_admin_time.organization_today", return_value=today)
    @patch("schedules.views.reports.organization_today", return_value=today)
    def test_empty_report_uses_an_actual_month_without_an_eligible_faculty_role(
        self, _view_today, _service_today
    ):
        self.client.force_login(self.program_director)

        response = self.client.get(
            reverse("faculty-admin-time-report"), {"month": "2023-02", "coverage": "history"}
        )

        table = self._rendered_admin_time_table(response)
        empty_cells = [
            cell
            for row in table.body_rows()
            for cell in row.children_named("td")
            if "data-table__empty" in cell.attributes.get("class", "").split()
        ]
        self.assertEqual(len(empty_cells), 1)
        self.assertIn("No Faculty physicians", empty_cells[0].text())

    @patch("schedules.services.faculty_admin_time.organization_today", return_value=today)
    @patch("schedules.views.reports.organization_today", return_value=today)
    def test_report_helper_explains_the_ledger_calculation(self, _view_today, _service_today):
        self.client.force_login(self.program_director)

        document = self._rendered_document(
            self.client.get(
                reverse("faculty-admin-time-report"),
                {"month": "2025-02", "coverage": "history"},
            )
        )
        helper = [
            element
            for element in document.descendants_named("p")
            if "form-helper" in element.attributes.get("class", "").split()
        ]
        self.assertEqual(len(helper), 1)
        helper_text = helper[0].text()
        for meaning in (
            r"\bAM\s*/\s*PM\s+period\s+estimates?\s+(?:four|4)\s+hours\b",
            r"\bexplicit\s+schedule-option\s+capacity\s+starts\s+gross\b",
            r"\bindependent\s+blockers\s+subtract\s+each\s+period\s+once\b",
            r"\bAvailable\s+and\s+Admin\s+count\s+alike\b",
            r"\bPrecepting\s+in\s+either\s+is\s+subtracted\b",
            r"\bTotal\s+worked\s+is\s+all\s+remaining\s+explicit-option\s+hours\b",
        ):
            with self.subTest(meaning=meaning):
                self.assertRegex(helper_text, re.compile(meaning, re.IGNORECASE))

    @patch("schedules.services.faculty_admin_time.organization_today", return_value=today)
    @patch("schedules.views.reports.organization_today", return_value=today)
    def test_table_binds_option_hours_and_goal_statuses_to_each_physician(
        self, _view_today, _service_today
    ):
        custom = CustomAvailabilityCategory.objects.create(
            name="Long retained clinic teaching allocation",
            impact=CustomAvailabilityCategory.Impact.AVAILABLE,
        )
        admin = CustomAvailabilityCategory.objects.get(
            system_key=CustomAvailabilityCategory.SystemKey.ADMIN
        )
        self._availability_template(
            self.pd_physician,
            {
                "monday_am": FacultyAvailabilityTemplate.base_selection("open"),
                "monday_pm": FacultyAvailabilityTemplate.category_selection(admin),
                "tuesday_am": FacultyAvailabilityTemplate.base_selection("patient_care"),
                "tuesday_pm": FacultyAvailabilityTemplate.category_selection(custom),
                "wednesday_am": FacultyAvailabilityTemplate.category_selection(custom),
            },
        )
        self._availability_template(
            self.faculty_physician,
            {
                "monday_am": FacultyAvailabilityTemplate.base_selection("open"),
                "tuesday_am": FacultyAvailabilityTemplate.base_selection("patient_care"),
                "tuesday_pm": FacultyAvailabilityTemplate.base_selection("patient_care"),
                "wednesday_am": FacultyAvailabilityTemplate.category_selection(custom),
                "thursday_am": FacultyAvailabilityTemplate.base_selection("backup_only"),
            },
        )
        Vacation.objects.create(
            physician=self.pd_physician,
            start_date=date(2025, 2, 3),
            end_date=date(2025, 2, 3),
            period_of_day=Vacation.PeriodOfDay.PM,
        )
        event_definition = AcademicEventDefinition.objects.create(
            base_name="Faculty planning retreat",
            start_academic_year=academic_year_for_date(date(2025, 2, 3)),
        )
        event = AcademicEvent.objects.create(
            definition=event_definition,
            academic_year=academic_year_for_date(date(2025, 2, 3)),
            base_name="Faculty planning retreat",
            required_participant_count=1,
            resolution=AcademicEvent.Resolution.DATED,
            start_date=date(2025, 2, 3),
            end_date=date(2025, 2, 3),
            period_of_day=AcademicEvent.PeriodOfDay.PM,
            precepting_impact=AcademicEvent.PreceptingImpact.HARD,
        )
        event.participants.add(self.pd_physician)
        Vacation.objects.create(
            physician=self.faculty_physician,
            start_date=date(2025, 2, 3),
            end_date=date(2025, 2, 3),
            period_of_day=Vacation.PeriodOfDay.AM,
        )
        self._assignment(self.pd_physician, date(2025, 2, 10), "AM")
        CustomAvailabilityCategory.objects.filter(pk=custom.pk).update(is_active=False)
        self.client.force_login(self.program_director)

        response = self.client.get(
            reverse("faculty-admin-time-report"), {"month": "2025-02", "coverage": "history"}
        )

        table = self._rendered_admin_time_table(response)
        self.assertEqual(
            table.header_labels()[:6],
            [
                "Physician",
                "Open",
                "Admin",
                "Backup only",
                "Patient care",
                "Off",
            ],
        )
        self.assertIn("Long retained clinic teaching allocation", table.header_labels())

        for physician, expected_hours in (
            (
                self.pd_physician,
                {
                    "Open": (Decimal("8"), 2),
                    "Admin": (Decimal("4"), 1),
                    "Backup only": (Decimal("0"), 0),
                    "Patient care": (Decimal("8"), 2),
                    "Off": (Decimal("40"), 10),
                    "Long retained clinic teaching allocation": (Decimal("16"), 4),
                    "Total worked": (Decimal("36"), 9),
                    "Precepting (Available/Admin)": (Decimal("4"), 1),
                    "Computed Admin Time": (Decimal("8"), 2),
                },
            ),
            (
                self.faculty_physician,
                {
                    "Open": (Decimal("4"), 1),
                    "Admin": (Decimal("0"), 0),
                    "Backup only": (Decimal("8"), 2),
                    "Patient care": (Decimal("16"), 4),
                    "Off": (Decimal("40"), 10),
                    "Long retained clinic teaching allocation": (Decimal("8"), 2),
                    "Total worked": (Decimal("36"), 9),
                    "Precepting (Available/Admin)": (Decimal("0"), 0),
                    "Computed Admin Time": (Decimal("4"), 1),
                },
            ),
        ):
            for field, (hours, periods) in expected_hours.items():
                with self.subTest(physician=physician.display_name, field=field):
                    cell = self._admin_time_cell(table, physician, field)
                    self.assert_hours_cell(cell, hours, periods)
                    self.assert_neutral_cell(cell)

        blocked_time = self._admin_time_cell(table, self.pd_physician, "Blocked time")
        self.assert_blocked_time_cell(
            blocked_time,
            hours=Decimal("4"),
            periods=1,
            sources={
                f"Vacation {self.pd_physician.vacations.get().pk}": 1,
                "Academic Event: Faculty planning retreat": 1,
            },
        )
        self.assert_neutral_cell(blocked_time)

        self.assert_goal_cell(
            self._admin_time_cell(table, self.pd_physician, "ACGME"),
            target=Decimal("3.6"),
            ten_percent=Decimal("3.6"),
            cap=Decimal("8"),
            status="Met",
            tone="positive",
        )
        self.assert_goal_cell(
            self._admin_time_cell(table, self.pd_physician, "UMN"),
            target=Decimal("7.2"),
            status="Met",
            tone="positive",
        )
        self.assert_goal_cell(
            self._admin_time_cell(table, self.faculty_physician, "ACGME"),
            target=Decimal("3.6"),
            ten_percent=Decimal("3.6"),
            cap=Decimal("8"),
            status="Met",
            tone="positive",
        )
        self.assert_goal_cell(
            self._admin_time_cell(table, self.faculty_physician, "UMN"),
            target=Decimal("7.2"),
            status="Below",
            tone="danger",
        )
