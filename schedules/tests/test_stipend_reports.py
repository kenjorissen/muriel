from datetime import date, datetime
from decimal import Decimal
from email.message import Message
from io import BytesIO
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from pypdf import PdfReader

from schedules.models import (
    EffectiveRole,
    LateDocAssignment,
    Physician,
    PreceptorAssignment,
    PreceptorSession,
    StipendRate,
    StipendRateChange,
)
from schedules.pdf import (
    build_monthly_stipend_pdf,
)
from schedules.services.preceptors import assign_preceptor, open_monthly_draft
from schedules.services.stipend_rates import (
    append_stipend_rate,
    remove_stipend_rate,
    set_initial_stipend_rate,
)
from schedules.services.stipend_reports import (
    community_preceptor_stipend_report,
    late_doc_stipend_report,
)
from schedules.tests.test_precepting_calendar_pdf import (
    _grid_cell_at,
    _horizontal_bounds_are_within,
    _pdf_artifact,
)
from schedules.tests.test_table_presentation import _RenderedDataTable, _RenderedTableParser


class StipendReportServicesTests(TestCase):
    month = date(2025, 2, 1)

    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(
            username="administrator", password="test-password"
        )
        self.scheduler.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )

    def physician(self, name, role):
        physician = Physician.objects.create(display_name=name)
        EffectiveRole.objects.create(
            physician=physician, role=role, effective_start=date(2024, 1, 1)
        )
        return physician

    def session(self, on_date, period):
        open_monthly_draft(self.month)
        return PreceptorSession.objects.get(date=on_date, period_of_day=period)

    def test_community_report_uses_current_corrections_future_assignments_and_shift_rates(self):
        corrected_from = self.physician("Corrected from", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        physician = self.physician("Community physician", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        interleaved = self.physician("Alpha physician", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        faculty = self.physician("Zero-hour faculty", EffectiveRole.Role.FACULTY)
        first_session = self.session(date(2025, 2, 3), "AM")
        first_pm_session = self.session(date(2025, 2, 3), "PM")
        second_am_session = self.session(date(2025, 2, 10), "AM")
        second_session = self.session(date(2025, 2, 17), "PM")
        assign_preceptor(
            session=first_session,
            physician=corrected_from,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        assign_preceptor(
            session=first_session,
            physician=physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        assign_preceptor(
            session=first_pm_session,
            physician=interleaved,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        assign_preceptor(
            session=second_am_session,
            physician=interleaved,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        assign_preceptor(
            session=second_session,
            physician=physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        PreceptorAssignment.objects.create(
            session=first_session,
            physician=faculty,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=2,
        )
        set_initial_stipend_rate(
            kind=StipendRate.Kind.COMMUNITY_PRECEPTOR,
            hourly_rate="100.00",
            scheduler=self.scheduler,
        )
        append_stipend_rate(
            kind=StipendRate.Kind.COMMUNITY_PRECEPTOR,
            hourly_rate="125.00",
            transition_date=date(2025, 2, 15),
            direction="later",
            scheduler=self.scheduler,
        )

        with CaptureQueriesContext(connection) as queries:
            report = community_preceptor_stipend_report(self.month)

        # The projection owns all detail loading in a fixed query set; adding
        # assignments must not add a query per assignment or physician.
        baseline_query_count = len(queries)
        self.assertEqual([row.physician for row in report.physicians], [interleaved, physician])
        self.assertEqual(
            [(detail.date, detail.period, detail.physician) for detail in report.details],
            [
                (date(2025, 2, 3), "AM", physician),
                (date(2025, 2, 3), "PM", interleaved),
                (date(2025, 2, 10), "AM", interleaved),
                (date(2025, 2, 17), "PM", physician),
            ],
        )
        self.assertEqual(
            [detail.hourly_rate for detail in report.details],
            [Decimal("100.00"), Decimal("100.00"), Decimal("100.00"), Decimal("125.00")],
        )
        self.assertEqual([row.shift_total for row in report.physicians], [2, 2])
        self.assertEqual(
            [(row.physician, row.amount_total) for row in report.physicians],
            [(interleaved, Decimal("800.00")), (physician, Decimal("900.00"))],
        )
        self.assertEqual(report.amount_total, Decimal("1700.00"))

        for index, session in enumerate(
            PreceptorSession.objects.filter(date__month=self.month.month)
            .exclude(assignments__primary_position=1)
            .order_by("pk")[:10]
        ):
            physician = self.physician(
                f"Additional physician {index}", EffectiveRole.Role.COMMUNITY_PRECEPTOR
            )
            PreceptorAssignment.objects.create(
                session=session,
                physician=physician,
                responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                primary_position=1,
            )
        with CaptureQueriesContext(connection) as larger_queries:
            larger_report = community_preceptor_stipend_report(self.month)
        self.assertLessEqual(len(larger_queries), baseline_query_count)
        self.assertEqual(len(larger_report.details), len(report.details) + 10)

    def test_community_report_recalculates_from_the_active_timeline_after_removal(self):
        physician = self.physician("Community physician", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        session = self.session(date(2025, 2, 17), "AM")
        assign_preceptor(
            session=session,
            physician=physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        first = set_initial_stipend_rate(
            kind=StipendRate.Kind.COMMUNITY_PRECEPTOR,
            hourly_rate="100.00",
            scheduler=self.scheduler,
        )
        later = append_stipend_rate(
            kind=first.kind,
            hourly_rate="125.00",
            transition_date=date(2025, 2, 15),
            direction="later",
            scheduler=self.scheduler,
        )

        remove_stipend_rate(rate=later, scheduler=self.scheduler)

        report = community_preceptor_stipend_report(self.month)
        self.assertEqual([detail.hourly_rate for detail in report.details], [Decimal("100.00")])
        self.assertEqual(report.rate_periods[0].effective_end, None)

    def test_late_doc_report_warns_for_each_missing_monday_without_inferring_a_preceptor(self):
        assigned = self.physician("Late Doc", EffectiveRole.Role.FACULTY)
        pm_primary = self.physician("Monday PM primary", EffectiveRole.Role.FACULTY)
        monday_pm = self.session(date(2025, 2, 3), "PM")
        PreceptorAssignment.objects.create(
            session=monday_pm,
            physician=pm_primary,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )
        LateDocAssignment.objects.create(date=date(2025, 2, 17), physician=assigned)
        set_initial_stipend_rate(
            kind=StipendRate.Kind.LATE_DOC, hourly_rate="80.00", scheduler=self.scheduler
        )

        with CaptureQueriesContext(connection) as queries:
            report = late_doc_stipend_report(self.month)

        # The report fetches all selected-month assignments at once, rather
        # than querying each Late Doc assignment or its physician.
        baseline_query_count = len(queries)
        self.assertEqual([row.physician for row in report.physicians], [assigned])
        self.assertEqual(report.details[0].period, "Late Doc")
        self.assertEqual(report.amount_total, Decimal("80.00"))
        self.assertEqual(
            report.missing_late_doc_dates,
            (date(2025, 2, 3), date(2025, 2, 10), date(2025, 2, 24)),
        )

        for day_number in (3, 10, 24):
            LateDocAssignment.objects.create(
                date=date(2025, 2, day_number),
                physician=self.physician(
                    f"Additional late doc {day_number}", EffectiveRole.Role.FACULTY
                ),
            )
        with CaptureQueriesContext(connection) as larger_queries:
            larger_report = late_doc_stipend_report(self.month)
        self.assertLessEqual(len(larger_queries), baseline_query_count)
        self.assertEqual(len(larger_report.details), len(report.details) + 3)

    def test_missing_rate_keeps_qualifying_shift_visible_without_an_amount_or_total(self):
        physician = self.physician("Unrated community", EffectiveRole.Role.COMMUNITY_PRECEPTOR)
        session = self.session(date(2025, 2, 3), "AM")
        PreceptorAssignment.objects.create(
            session=session,
            physician=physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )

        report = community_preceptor_stipend_report(self.month)

        self.assertEqual(report.missing_rate_dates, (date(2025, 2, 3),))
        self.assertIsNone(report.details[0].amount)
        self.assertIsNone(report.physicians[0].amount_total)
        self.assertIsNone(report.amount_total)


class StipendReportPresentationTests(TestCase):
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

    def test_reports_use_month_navigation_and_restrict_rate_navigation_to_administrators(self):
        self.client.force_login(self.administrator)

        response = self.client.get(
            reverse("community-preceptor-stipend-report"), {"month": "2025-02"}
        )

        self.assertContains(response, "Community Preceptor Stipend")
        self.assertContains(response, "Late Doc Stipend")
        self.assertContains(response, "Stipend Rates")
        self.assertContains(response, 'name="month" value="2025-02"')
        self.assertContains(response, "data-calendar-period-picker")
        details = _RenderedDataTable(response.content.decode(), "stipend-itemized-shifts")
        self.assertEqual(len(details.body_rows()), 1)
        empty_state = details.body_rows()[0].text()
        self.assertIn("qualifying shifts", empty_state.casefold())
        self.assertIn("February 2025", empty_state)
        self.assertEqual(
            reverse("community-preceptor-stipend-report"),
            "/reports/community-preceptor-stipend/",
        )
        content = response.content.decode()
        navigation = _navigation_links(content)
        self.assertEqual(
            navigation["Community Preceptor Stipend"],
            {"path": "/reports/community-preceptor-stipend/", "month": "2025-02", "current": True},
        )
        self.assertEqual(
            navigation["Schedule Reports"], {"path": "/reports/", "month": None, "current": False}
        )
        self.assertEqual(sum(link["current"] for link in navigation.values()), 1)
        self.assertIn(
            {
                "path": "/reports/community-preceptor-stipend/",
                "month": "2025-01",
                "label": "Previous month",
            },
            _month_navigation_links(content),
        )
        self.assertIn(
            {
                "path": "/reports/community-preceptor-stipend/",
                "month": "2025-03",
                "label": "Next month",
            },
            _month_navigation_links(content),
        )
        self.assertEqual(
            self.client.get(
                reverse("community-preceptor-stipend-report"), {"month": "February"}
            ).status_code,
            400,
        )

        self.client.force_login(self.scheduler)
        response = self.client.get(reverse("late-doc-stipend-report"), {"month": "2025-02"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Late Doc Stipend")
        self.assertNotContains(response, "Stipend Rates")

    def test_screen_uses_one_chronological_register_and_physician_totals(self):
        month = date(2025, 2, 1)
        alpha = Physician.objects.create(display_name="Alpha physician")
        zulu = Physician.objects.create(display_name="Zulu physician")
        for physician in (alpha, zulu):
            EffectiveRole.objects.create(
                physician=physician,
                role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
                effective_start=date(2024, 1, 1),
            )
        open_monthly_draft(month)
        for on_date, period, physician in (
            (date(2025, 2, 3), "AM", alpha),
            (date(2025, 2, 3), "PM", zulu),
            (date(2025, 2, 10), "AM", zulu),
            (date(2025, 2, 17), "PM", alpha),
        ):
            PreceptorAssignment.objects.create(
                session=PreceptorSession.objects.get(date=on_date, period_of_day=period),
                physician=physician,
                responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                primary_position=1,
            )
        set_initial_stipend_rate(
            kind=StipendRate.Kind.COMMUNITY_PRECEPTOR,
            hourly_rate="100.00",
            scheduler=self.administrator,
        )
        append_stipend_rate(
            kind=StipendRate.Kind.COMMUNITY_PRECEPTOR,
            hourly_rate="125.00",
            transition_date=date(2025, 2, 15),
            direction="later",
            scheduler=self.administrator,
        )
        self.client.force_login(self.scheduler)

        response = self.client.get(
            reverse("community-preceptor-stipend-report"), {"month": "2025-02"}
        )
        detail_table = _RenderedDataTable(response.content.decode(), "stipend-itemized-shifts")
        total_table = _RenderedDataTable(response.content.decode(), "stipend-totals")

        expected_details = (
            {
                "Date": "Mon, Feb 3",
                "Period": "AM",
                "Physician": "Alpha physician",
                "Hours": "4",
                "Hourly rate": "$100.00",
                "Amount": "$400.00",
            },
            {
                "Date": "Mon, Feb 3",
                "Period": "PM",
                "Physician": "Zulu physician",
                "Hours": "4",
                "Hourly rate": "$100.00",
                "Amount": "$400.00",
            },
            {
                "Date": "Mon, Feb 10",
                "Period": "AM",
                "Physician": "Zulu physician",
                "Hours": "4",
                "Hourly rate": "$100.00",
                "Amount": "$400.00",
            },
            {
                "Date": "Mon, Feb 17",
                "Period": "PM",
                "Physician": "Alpha physician",
                "Hours": "4",
                "Hourly rate": "$125.00",
                "Amount": "$500.00",
            },
        )
        self.assertEqual(
            [
                _table_fields(detail_table, row, ("Date", "Period", "Physician"))
                for row in detail_table.body_rows()
            ],
            [
                {field: expected[field] for field in ("Date", "Period", "Physician")}
                for expected in expected_details
            ],
        )
        for expected in expected_details:
            _assert_table_row_fields(
                self, detail_table, expected, identity=("Date", "Period", "Physician")
            )
        for expected in (
            {
                "Physician": "Alpha physician",
                "Shifts": "2 shifts",
                "Hours": "8",
                "Stipend amount": "$900.00",
            },
            {
                "Physician": "Zulu physician",
                "Shifts": "2 shifts",
                "Hours": "8",
                "Stipend amount": "$800.00",
            },
        ):
            _assert_table_row_fields(self, total_table, expected, identity=("Physician",))
        _assert_table_row_fields(
            self,
            total_table,
            {
                "Physician": "Overall total",
                "Shifts": "4 shifts",
                "Hours": "16",
                "Stipend amount": "$1700.00",
            },
            identity=("Physician",),
            rows=total_table.footer_rows(),
        )


class StipendReportPdfTests(TestCase):
    month = date(2025, 2, 1)

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

    def community_physician(self, name="Community physician"):
        physician = Physician.objects.create(display_name=name)
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2024, 1, 1),
        )
        return physician

    def community_assignment(self, on_date, name="Community physician"):
        physician = self.community_physician(name)
        open_monthly_draft(self.month)
        session = PreceptorSession.objects.get(date=on_date, period_of_day="AM")
        PreceptorAssignment.objects.create(
            session=session,
            physician=physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )
        return physician

    def test_community_download_extracts_shared_projection_with_provenance_and_totals(self):
        physician = self.community_assignment(date(2025, 2, 3))
        set_initial_stipend_rate(
            kind=StipendRate.Kind.COMMUNITY_PRECEPTOR,
            hourly_rate="100.00",
            scheduler=self.administrator,
        )
        before = _stipend_report_source_state()
        self.client.force_login(self.scheduler)

        response = self.client.get(reverse("community-preceptor-stipend-pdf"), {"month": "2025-02"})
        text = _pdf_text(response.content)

        self.assertEqual(response["Content-Type"], "application/pdf")
        attachment = Message()
        attachment["Content-Disposition"] = response["Content-Disposition"]
        self.assertEqual(attachment.get_content_disposition(), "attachment")
        self.assertTrue(attachment.get_filename().endswith(".pdf"))
        self.assertTrue(response.content.startswith(b"%PDF-"))
        # TEXT-CONTRACT: stipend-pdf-title-prefix
        # docs/design.md section 25.1 requires the shell's fictional
        # facility in the visible and PDF-metadata title. The prefix retains its
        # capitalization, hyphen-minus and surrounding spaces.
        # Production: schedules/pdf.py::build_monthly_stipend_pdf. Protecting
        # method: schedules/tests/test_stipend_reports.py::StipendReportPdfTests.test_community_download_extracts_shared_projection_with_provenance_and_totals.
        title = "Family Practice Clinic - Community Preceptor Stipend Report"
        self.assertEqual(reader_metadata_title(response.content), title)
        self.assertIn(title, " ".join(text.split()))
        for value in (
            "February 2025",
            "Current corrected and future scheduled assignment records",
            "Generated by",
            "scheduler",
            "Itemized shifts",
            "Totals",
            "Community physician",
            "Monday, February 3, 2025",
            "$400.00",
        ):
            self.assertIn(value, text)
        self.assertNotIn("Applied hourly rate periods", text)
        self.assertNotIn("Warnings", text)
        self.assertNotIn("No warnings.", text)
        self.assertEqual(before, _stipend_report_source_state())
        self.assertEqual(
            community_preceptor_stipend_report(self.month).physicians[0].physician, physician
        )

    def test_pdf_uses_the_chronological_register_and_physician_totals(self):
        alpha = self.community_physician("Alpha physician")
        zulu = self.community_physician("Zulu physician")
        open_monthly_draft(self.month)
        for on_date, period, physician in (
            (date(2025, 2, 3), "AM", alpha),
            (date(2025, 2, 3), "PM", zulu),
            (date(2025, 2, 10), "AM", zulu),
            (date(2025, 2, 17), "PM", alpha),
        ):
            PreceptorAssignment.objects.create(
                session=PreceptorSession.objects.get(date=on_date, period_of_day=period),
                physician=physician,
                responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                primary_position=1,
            )
        set_initial_stipend_rate(
            kind=StipendRate.Kind.COMMUNITY_PRECEPTOR,
            hourly_rate="100.00",
            scheduler=self.administrator,
        )
        append_stipend_rate(
            kind=StipendRate.Kind.COMMUNITY_PRECEPTOR,
            hourly_rate="125.00",
            transition_date=date(2025, 2, 15),
            direction="later",
            scheduler=self.administrator,
        )
        self.client.force_login(self.scheduler)

        response = self.client.get(reverse("community-preceptor-stipend-pdf"), {"month": "2025-02"})
        self.assertEqual(response.status_code, 200)
        artifact = _stipend_pdf_cells(
            response.content,
            {
                *_DETAIL_HEADERS,
                *_TOTAL_HEADERS,
                "Monday, February 3, 2025",
                "Monday, February 10, 2025",
                "Monday, February 17, 2025",
                "Alpha physician",
                "Zulu physician",
                "$100.00",
                "$125.00",
                "$400.00",
                "$500.00",
                "$800.00",
                "$900.00",
                "$1700.00",
                "Overall total",
                "AM",
                "PM",
                "2",
                "4",
                "8",
                "16",
            },
        )
        expected_details = (
            {
                "Date": "Monday, February 3, 2025",
                "Period": "AM",
                "Physician": "Alpha physician",
                "Hours": "4",
                "Hourly rate": "$100.00",
                "Amount": "$400.00",
            },
            {
                "Date": "Monday, February 3, 2025",
                "Period": "PM",
                "Physician": "Zulu physician",
                "Hours": "4",
                "Hourly rate": "$100.00",
                "Amount": "$400.00",
            },
            {
                "Date": "Monday, February 10, 2025",
                "Period": "AM",
                "Physician": "Zulu physician",
                "Hours": "4",
                "Hourly rate": "$100.00",
                "Amount": "$400.00",
            },
            {
                "Date": "Monday, February 17, 2025",
                "Period": "PM",
                "Physician": "Alpha physician",
                "Hours": "4",
                "Hourly rate": "$125.00",
                "Amount": "$500.00",
            },
        )
        detail_rows = [
            _assert_pdf_table_row(self, artifact, "detail", expected)
            for expected in expected_details
        ]
        self.assertEqual(
            [
                (row["page"], -row["top"], expected["Date"], expected["Period"])
                for row, expected in zip(detail_rows, expected_details, strict=True)
            ],
            sorted(
                (row["page"], -row["top"], expected["Date"], expected["Period"])
                for row, expected in zip(detail_rows, expected_details, strict=True)
            ),
        )
        for expected in (
            {
                "Physician": "Alpha physician",
                "Shifts": "2",
                "Hours": "8",
                "Stipend amount": "$900.00",
            },
            {
                "Physician": "Zulu physician",
                "Shifts": "2",
                "Hours": "8",
                "Stipend amount": "$800.00",
            },
            {
                "Physician": "Overall total",
                "Shifts": "4",
                "Hours": "16",
                "Stipend amount": "$1700.00",
            },
        ):
            _assert_pdf_table_row(self, artifact, "totals", expected)

    def test_late_doc_download_keeps_missing_mondays_as_warnings(self):
        physician = Physician.objects.create(display_name="Late Doc physician")
        LateDocAssignment.objects.create(date=date(2025, 2, 17), physician=physician)
        set_initial_stipend_rate(
            kind=StipendRate.Kind.LATE_DOC,
            hourly_rate="80.00",
            scheduler=self.administrator,
        )
        self.client.force_login(self.scheduler)

        response = self.client.get(reverse("late-doc-stipend-pdf"), {"month": "2025-02"})
        text = _pdf_text(response.content)

        self.assertEqual(response.status_code, 200)
        # TEXT-CONTRACT: stipend-pdf-title-prefix (see the Community download test).
        title = "Family Practice Clinic - Late Doc Stipend Report"
        self.assertEqual(reader_metadata_title(response.content), title)
        self.assertIn(title, " ".join(text.split()))
        self.assertIn("Warnings", text)
        self.assertIn("Late Doc work item", text)
        self.assertIn("Monday, February 3, 2025", text)
        self.assertIn("Monday, February 10, 2025", text)
        self.assertIn("Late Doc physician", text)
        self.assertIn("$80.00", text)
        self.assertNotIn("Applied hourly rate periods", text)

    def test_stipend_pdf_artifacts_keep_amount_heading_legible_within_page_geometry(self):
        month = date(2025, 9, 1)
        community_name = (
            "Community lengthy physician identity retaining every readable name fragment"
        )
        late_doc_name = "Late Doc lengthy physician identity retaining every readable name fragment"
        community = self.community_physician(community_name)
        late_doc = Physician.objects.create(display_name=late_doc_name)
        open_monthly_draft(month)
        PreceptorAssignment.objects.create(
            session=PreceptorSession.objects.get(date=date(2025, 9, 17), period_of_day="AM"),
            physician=community,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
        )
        LateDocAssignment.objects.create(date=date(2025, 9, 15), physician=late_doc)
        set_initial_stipend_rate(
            kind=StipendRate.Kind.COMMUNITY_PRECEPTOR,
            hourly_rate="100.00",
            scheduler=self.administrator,
        )
        set_initial_stipend_rate(
            kind=StipendRate.Kind.LATE_DOC,
            hourly_rate="80.00",
            scheduler=self.administrator,
        )
        for report in (community_preceptor_stipend_report(month), late_doc_stipend_report(month)):
            artifact = build_monthly_stipend_pdf(
                report, generated_at=datetime(2025, 9, 30), generated_by=self.scheduler
            )
            name_words = (
                (
                    "Community",
                    "lengthy",
                    "physician",
                    "identity",
                    "retaining",
                    "every",
                    "readable",
                    "name",
                    "fragment",
                )
                if report.kind == "community_preceptor"
                else (
                    "Late",
                    "Doc",
                    "lengthy",
                    "physician",
                    "identity",
                    "retaining",
                    "every",
                    "readable",
                    "name",
                    "fragment",
                )
            )
            date_words = (
                ("Wednesday,", "September", "17,", "2025")
                if report.kind == "community_preceptor"
                else ("Monday,", "September", "15,", "2025")
            )
            geometry = _stipend_pdf_cells(
                artifact,
                {
                    "Generated by",
                    "scheduler",
                    *_DETAIL_HEADERS,
                    *_TOTAL_HEADERS,
                    "$400.00" if report.kind == "community_preceptor" else "$80.00",
                },
                fragments=(*name_words, *date_words),
            )
            amount = "$400.00" if report.kind == "community_preceptor" else "$80.00"
            detail_row = _stipend_pdf_geometry_row(
                self, geometry, "detail", amount, name_words, date_words
            )
            total_row = _stipend_pdf_geometry_row(self, geometry, "totals", amount, name_words, ())
            _assert_stipend_pdf_geometry(
                self, geometry, detail_row, total_row, name_words, date_words, amount
            )

    def test_stipend_pdf_repeats_detail_and_totals_headings_across_real_page_breaks(self):
        month = date(2025, 9, 1)
        open_monthly_draft(month)
        sessions = list(
            PreceptorSession.objects.filter(date__year=2025, date__month=9).order_by(
                "date", "period_of_day"
            )
        )
        self.assertGreaterEqual(len(sessions), 40)
        for index, session in enumerate(sessions):
            physician = self.community_physician(f"Page-break physician {index:02d}")
            PreceptorAssignment.objects.create(
                session=session,
                physician=physician,
                responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                primary_position=1,
            )
        set_initial_stipend_rate(
            kind=StipendRate.Kind.COMMUNITY_PRECEPTOR,
            hourly_rate="100.00",
            scheduler=self.administrator,
        )

        artifact = build_monthly_stipend_pdf(
            community_preceptor_stipend_report(month),
            generated_at=datetime(2025, 9, 30),
            generated_by=self.scheduler,
        )
        _, locations, _, grid_lines = _pdf_artifact(
            artifact,
            {
                *_DETAIL_HEADERS,
                *_TOTAL_HEADERS,
                *(f"Page-break physician {index:02d}" for index in range(len(sessions))),
            },
        )
        detail_headers = _stipend_pdf_header_rows(self, locations, grid_lines, _DETAIL_HEADERS)
        total_headers = _stipend_pdf_header_rows(self, locations, grid_lines, _TOTAL_HEADERS)
        names = [f"Page-break physician {index:02d}" for index in range(len(sessions))]
        detail_data_pages = _pdf_data_pages(
            locations, grid_lines, detail_headers, "Physician", names
        )
        total_data_pages = _pdf_data_pages(locations, grid_lines, total_headers, "Physician", names)
        self.assertGreater(len(detail_data_pages), 1)
        self.assertGreater(len(total_data_pages), 1)
        self.assertTrue(detail_data_pages.issubset({header["page"] for header in detail_headers}))
        self.assertTrue(total_data_pages.issubset({header["page"] for header in total_headers}))

    def test_missing_rate_redirects_to_the_report_with_an_actionable_work_item(self):
        self.community_assignment(date(2025, 2, 3))
        self.client.force_login(self.scheduler)

        response = self.client.get(reverse("community-preceptor-stipend-pdf"), {"month": "2025-02"})

        self.assertRedirects(
            response,
            "/reports/community-preceptor-stipend/?month=2025-02",
            fetch_redirect_response=False,
        )
        report = self.client.get(response["Location"])
        details = _RenderedDataTable(report.content.decode(), "stipend-itemized-shifts")
        _assert_table_row_fields(
            self,
            details,
            {
                "Date": "Mon, Feb 3",
                "Period": "AM",
                "Physician": "Community physician",
                "Amount": "Incomplete",
            },
            identity=("Date", "Period", "Physician"),
        )
        self.assertIn("Feb 3", report.content.decode())
        self.assertIn("no hourly rate", report.content.decode().casefold())

    def test_pdf_includes_only_assignments_inside_the_selected_calendar_month(self):
        physician = self.community_physician()
        for on_date in (date(2025, 1, 31), date(2025, 2, 3)):
            open_monthly_draft(on_date.replace(day=1))
            session = PreceptorSession.objects.get(date=on_date, period_of_day="AM")
            PreceptorAssignment.objects.create(
                session=session,
                physician=physician,
                responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                primary_position=1,
            )
        set_initial_stipend_rate(
            kind=StipendRate.Kind.COMMUNITY_PRECEPTOR,
            hourly_rate="100.00",
            scheduler=self.administrator,
        )
        self.client.force_login(self.scheduler)

        response = self.client.get(reverse("community-preceptor-stipend-pdf"), {"month": "2025-02"})
        artifact = _stipend_pdf_cells(
            response.content,
            {
                "Monday, February 3, 2025",
                "$100.00",
                "$400.00",
                "Overall total",
                "Community physician",
                "AM",
                "1",
                "4",
                *_DETAIL_HEADERS,
                *_TOTAL_HEADERS,
            },
        )

        self.assertNotIn("Friday, January 31, 2025", _pdf_text(response.content))
        _assert_pdf_table_row(
            self,
            artifact,
            "detail",
            {
                "Date": "Monday, February 3, 2025",
                "Period": "AM",
                "Physician": "Community physician",
                "Hours": "4",
                "Hourly rate": "$100.00",
                "Amount": "$400.00",
            },
        )
        _assert_pdf_table_row(
            self,
            artifact,
            "totals",
            {
                "Physician": "Overall total",
                "Shifts": "1",
                "Hours": "4",
                "Stipend amount": "$400.00",
            },
        )

    def test_download_requires_sign_in_and_accepts_only_calendar_months(self):
        url = reverse("late-doc-stipend-pdf")

        anonymous = self.client.get(url, {"month": "2025-02"})
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn(reverse("sign-in"), anonymous["Location"])
        self.client.force_login(self.scheduler)
        self.assertEqual(self.client.get(url, {"month": "February"}).status_code, 400)


def _pdf_text(content):
    extracted = "\n".join(page.extract_text() for page in PdfReader(BytesIO(content)).pages)
    return " ".join(extracted.split())


def reader_metadata_title(content):
    return PdfReader(BytesIO(content)).metadata.title


_DETAIL_HEADERS = ("Date", "Period", "Physician", "Hours", "Hourly rate", "Amount")
_TOTAL_HEADERS = ("Physician", "Shifts", "Hours", "Stipend amount")


def _table_fields(table, row, headers):
    return {header: table.cell_for_header(row, header).text().strip() for header in headers}


def _assert_table_row_fields(test_case, table, expected, *, identity, rows=None):
    rows = table.body_rows() if rows is None else rows
    matching_rows = [
        row
        for row in rows
        if _table_fields(table, row, identity) == {header: expected[header] for header in identity}
    ]
    test_case.assertEqual(len(matching_rows), 1, f"Expected one row identified by {identity!r}")
    test_case.assertEqual(_table_fields(table, matching_rows[0], expected), expected)
    return matching_rows[0]


def _month_navigation_links(content):
    parser = _RenderedTableParser()
    parser.feed(content)
    return [
        {
            **_parsed_link(link.attributes.get("href")),
            "label": link.attributes.get("aria-label"),
        }
        for link in parser.root.descendants_named("a")
        if link.attributes.get("aria-label") in {"Previous month", "Next month"}
    ]


def _navigation_links(content):
    parser = _RenderedTableParser()
    parser.feed(content)
    navigation = next(
        element
        for element in parser.root.descendants_named("nav")
        if "workspace-local-navigation" in element.attributes.get("class", "").split()
    )
    return {
        link.text().strip(): {
            **_parsed_link(link.attributes.get("href")),
            "current": link.attributes.get("aria-current") == "page",
        }
        for link in navigation.descendants_named("a")
    }


def _parsed_link(url):
    parsed = urlparse(url)
    month = parse_qs(parsed.query).get("month", [None])
    return {"path": parsed.path, "month": month[0]}


def _stipend_pdf_cells(content, labels, *, fragments=()):
    _, locations, _, grid_lines = _pdf_artifact(content, labels, fragments=fragments)
    return locations, grid_lines


def _stipend_pdf_header_rows(test_case, locations, grid_lines, headings):
    rows = []
    for anchor in locations[headings[0]]:
        anchor_cell = _grid_cell_at(grid_lines, anchor)
        row = {headings[0]: anchor_cell}
        for heading in headings[1:]:
            matches = [
                location
                for location in locations[heading]
                if location["page"] == anchor["page"]
                and _same_pdf_row(_grid_cell_at(grid_lines, location), anchor_cell)
            ]
            if len(matches) != 1:
                break
            row[heading] = _grid_cell_at(grid_lines, matches[0])
        else:
            row["page"] = anchor["page"]
            rows.append(row)
    test_case.assertTrue(rows, f"Expected a complete {headings!r} header row")
    return rows


def _same_pdf_row(first, second):
    return (
        first["page"] == second["page"]
        and first["bottom"] == second["bottom"]
        and first["top"] == second["top"]
    )


def _pdf_row_location(locations, grid_lines, headers, field, value):
    matches = []
    for header_row in headers:
        for location in locations[value]:
            if location["page"] != header_row["page"]:
                continue
            try:
                cell = _grid_cell_at(grid_lines, location)
            except ValueError:
                continue
            if _same_pdf_column(cell, header_row[field]):
                matches.append((cell, location))
    return matches


def _assert_pdf_table_row(test_case, artifact, table, expected):
    locations, grid_lines = artifact
    headings = _DETAIL_HEADERS if table == "detail" else _TOTAL_HEADERS
    headers = _stipend_pdf_header_rows(test_case, locations, grid_lines, headings)
    identity = ("Date", "Period", "Physician") if table == "detail" else ("Physician",)
    candidates = _pdf_row_location(
        locations, grid_lines, headers, identity[0], expected[identity[0]]
    )
    rows = []
    for candidate, _ in candidates:
        if all(
            any(
                _same_pdf_row(candidate, cell)
                for cell, _ in _pdf_row_location(
                    locations, grid_lines, headers, field, expected[field]
                )
            )
            for field in identity[1:]
        ):
            rows.append(candidate)
    unique_rows = {(row["page"], row["bottom"], row["top"]): row for row in rows}
    test_case.assertEqual(
        len(unique_rows), 1, f"Expected one {table} row identified by {identity!r}"
    )
    row = next(iter(unique_rows.values()))
    for field, value in expected.items():
        matches = [
            (cell, location)
            for cell, location in _pdf_row_location(locations, grid_lines, headers, field, value)
            if _same_pdf_row(cell, row)
        ]
        test_case.assertEqual(len(matches), 1, f"Expected {value!r} in the {field!r} column")
        cell, location = matches[0]
        test_case.assertTrue(_horizontal_bounds_are_within(location["bounds"], cell))
    return row


def _pdf_data_pages(locations, grid_lines, headers, field, values):
    pages = set()
    for value in values:
        matches = _pdf_row_location(locations, grid_lines, headers, field, value)
        if not matches:
            raise AssertionError(f"Expected {value!r} beneath {field!r}")
        pages.update(cell["page"] for cell, _ in matches)
    return pages


def _stipend_pdf_geometry_row(
    test_case, artifact, table, amount, physician_fragments, date_fragments
):
    locations, grid_lines = artifact
    headings = _DETAIL_HEADERS if table == "detail" else _TOTAL_HEADERS
    amount_field = "Amount" if table == "detail" else "Stipend amount"
    headers = _stipend_pdf_header_rows(test_case, locations, grid_lines, headings)
    candidates = []
    for amount_cell, _amount_location in _pdf_row_location(
        locations, grid_lines, headers, amount_field, amount
    ):
        row = {
            "page": amount_cell["page"],
            "bottom": amount_cell["bottom"],
            "top": amount_cell["top"],
        }
        physician_cell = _detail_cell_for_row(row, headers, "Physician")
        date_cell = _detail_cell_for_row(row, headers, "Date") if table == "detail" else None
        if not all(
            any(
                _grid_cell_or_none(grid_lines, location) == physician_cell
                for location in locations[word]
            )
            for word in physician_fragments
        ):
            continue
        if not all(
            any(
                _grid_cell_or_none(grid_lines, location) == date_cell
                for location in locations[word]
            )
            for word in date_fragments
        ):
            continue
        candidates.append(row)
    test_case.assertEqual(len(candidates), 1, f"Expected one {table} row for amount {amount!r}")
    return candidates[0]


def _assert_stipend_pdf_geometry(
    test_case, artifact, detail_row, total_row, name_words, date_words, amount
):
    locations, grid_lines = artifact
    basis_label = _grid_cell_at(grid_lines, locations["Generated by"][0])
    basis_value = _grid_cell_at(grid_lines, locations["scheduler"][0])
    basis_bounds = (basis_label["left"], basis_value["right"])

    detail_headers = _stipend_pdf_header_rows(test_case, locations, grid_lines, _DETAIL_HEADERS)
    for header in detail_headers:
        date_cell = header["Date"]
        amount_cell = header["Amount"]
        test_case.assertAlmostEqual(date_cell["left"], basis_bounds[0], places=2)
        test_case.assertAlmostEqual(amount_cell["right"], basis_bounds[1], places=2)
        amount_header = next(
            location
            for location in locations["Amount"]
            if _grid_cell_at(grid_lines, location) == amount_cell
        )
        _assert_grid_span_is_within_page(test_case, date_cell, amount_cell, amount_header["bounds"])
        test_case.assertTrue(_horizontal_bounds_are_within(amount_header["bounds"], amount_cell))
        test_case.assertEqual(_cell_texts(locations, grid_lines, amount_cell), ["Amount"])

    total_headers = _stipend_pdf_header_rows(test_case, locations, grid_lines, _TOTAL_HEADERS)
    for header in total_headers:
        physician_cell = header["Physician"]
        amount_cell = header["Stipend amount"]
        test_case.assertAlmostEqual(physician_cell["left"], basis_bounds[0], places=2)
        test_case.assertAlmostEqual(amount_cell["right"], basis_bounds[1], places=2)
        amount_header = next(
            location
            for location in locations["Stipend amount"]
            if _grid_cell_at(grid_lines, location) == amount_cell
        )
        _assert_grid_span_is_within_page(
            test_case, physician_cell, amount_cell, amount_header["bounds"]
        )
        test_case.assertTrue(_horizontal_bounds_are_within(amount_header["bounds"], amount_cell))

    detail_physician_cell = _detail_cell_for_row(detail_row, detail_headers, "Physician")
    detail_date_cell = _detail_cell_for_row(detail_row, detail_headers, "Date")
    detail_amount_cell = _detail_cell_for_row(detail_row, detail_headers, "Amount")
    total_physician_cell = _detail_cell_for_row(total_row, total_headers, "Physician")
    amount_location = next(
        location
        for location in locations[amount]
        if _grid_cell_at(grid_lines, location) == detail_amount_cell
    )
    test_case.assertTrue(
        _horizontal_bounds_are_within(amount_location["bounds"], detail_amount_cell)
    )
    for word in name_words:
        location = next(
            location
            for location in locations[word]
            if _grid_cell_or_none(grid_lines, location) == detail_physician_cell
        )
        test_case.assertTrue(
            _horizontal_bounds_are_within(location["bounds"], detail_physician_cell)
        )
    for word in date_words:
        location = next(
            location
            for location in locations[word]
            if _grid_cell_or_none(grid_lines, location) == detail_date_cell
        )
        test_case.assertTrue(_horizontal_bounds_are_within(location["bounds"], detail_date_cell))
    for word in name_words:
        location = next(
            location
            for location in locations[word]
            if _grid_cell_or_none(grid_lines, location) == total_physician_cell
        )
        test_case.assertTrue(
            _horizontal_bounds_are_within(location["bounds"], total_physician_cell)
        )


def _detail_cell_for_row(row, headers, field):
    header = next(header for header in headers if header["page"] == row["page"])
    column = header[field]
    return {**row, "left": column["left"], "right": column["right"]}


def _grid_cell_or_none(grid_lines, location):
    try:
        return _grid_cell_at(grid_lines, location)
    except ValueError:
        return None


def _assert_grid_span_is_within_page(test_case, first, last, bounds):
    page, _, _, _, _, page_width, page_height = bounds
    test_case.assertEqual(first["page"], page)
    test_case.assertEqual(last["page"], page)
    test_case.assertLessEqual(0, first["left"])
    test_case.assertLess(first["left"], last["right"])
    test_case.assertLessEqual(last["right"], page_width)
    test_case.assertLessEqual(0, first["bottom"])
    test_case.assertLess(first["bottom"], first["top"])
    test_case.assertLessEqual(first["top"], page_height)


def _same_pdf_column(first, second):
    return first["left"] == second["left"] and first["right"] == second["right"]


def _cell_texts(locations, grid_lines, cell):
    texts = []
    for location in locations["_all_text_fragments"]:
        if location["page"] != cell["page"]:
            continue
        try:
            location_cell = _grid_cell_at(grid_lines, location)
        except ValueError:
            continue
        if location_cell == cell:
            texts.append((location["y"], location["x"], location["text"]))
    return [text for _, _, text in sorted(texts, reverse=True)]


def _stipend_report_source_state():
    return {
        "preceptor_assignments": list(
            PreceptorAssignment.objects.order_by("pk").values_list(
                "pk", "session_id", "physician_id", "responsibility", "primary_position"
            )
        ),
        "late_doc_assignments": list(
            LateDocAssignment.objects.order_by("pk").values_list("pk", "date", "physician_id")
        ),
        "rates": list(
            StipendRate.objects.order_by("pk").values_list(
                "pk", "kind", "hourly_rate", "effective_start", "effective_end", "retired_at"
            )
        ),
        "rate_history": list(
            StipendRateChange.objects.order_by("pk").values_list(
                "pk", "rate_id", "action", "previous_value", "recorded_value", "note"
            )
        ),
    }
