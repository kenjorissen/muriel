import re
from datetime import date, datetime
from io import BytesIO
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook
from openpyxl.styles import PatternFill

from schedules.models import (
    EffectiveRole,
    FacultyAvailabilityTemplate,
    InterviewShift,
    InterviewShiftChange,
    LateDocAssignment,
    LateDocAssignmentChange,
    Physician,
    PreceptorAssignment,
    PreceptorAssignmentChange,
    PreceptorSession,
    SchedulePeriod,
)
from schedules.services.precepting_workbook_import_preview import (
    PreceptingWorkbookImportPreviewError,
    load_precepting_workbook_import_preview_token,
)
from schedules.tests.test_table_presentation import _RenderedDocument


def preview_row(response, *, section, locator, fields):
    """Return one row from its named preview section without freezing presentation."""
    document = _RenderedDocument(response.content.decode())
    sections = [
        item
        for item in document.root.descendants_named("section")
        if item.attributes.get("aria-labelledby") == section
    ]
    if len(sections) != 1:
        raise AssertionError(f"Expected one preview section {section!r}, found {len(sections)}.")
    required_headers = (*locator, *fields)
    matching_tables = []
    for table in sections[0].descendants_named("table"):
        table_headers = [
            header.text().strip()
            for header in table.children_named("thead")[0]
            .children_named("tr")[0]
            .children_named("th")
        ]
        if set(required_headers).issubset(table_headers):
            matching_tables.append((table, table_headers))
    if len(matching_tables) != 1:
        raise AssertionError(f"Expected one preview table with columns {required_headers!r}.")
    table, table_headers = matching_tables[0]
    rows = []
    for row in table.children_named("tbody")[0].children_named("tr"):
        cells = row.children_named("th", "td")
        rows.append(
            {header: cells[index].text().strip() for index, header in enumerate(table_headers)}
        )

    def matches(row, header, expected):
        actual = row[header]
        if isinstance(expected, date):
            return rendered_date(actual) == expected
        return actual == expected

    matching_rows = [
        row
        for row in rows
        if all(matches(row, header, expected) for header, expected in locator.items())
    ]
    if len(matching_rows) != 1:
        raise AssertionError(f"Expected one preview row {locator!r}, found {matching_rows!r}.")
    row = matching_rows[0]
    for header, expected in fields.items():
        if not matches(row, header, expected):
            raise AssertionError(f"Expected {header!r} to be {expected!r}; found {row[header]!r}.")
    return row


def rendered_date(value):
    """Read the supported typed-date renderings without fixing date prose."""
    for format_string in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, format_string).date()
        except ValueError:
            continue
    raise AssertionError(f"Expected a rendered date; found {value!r}.")


def assert_assignment_pairs(test_case, text, pairs):
    """Check complete ordered rendered assignment pairs, not isolated text fragments."""
    rendered_pairs = []
    for rendered_pair in re.split(r"\s*[,;]\s*", text.strip()):
        match = re.fullmatch(
            r"(?P<physician>.+?)\s*(?:\((?P<parenthesized_kind>[^()]+)\)|"
            r"[-:]\s*(?P<separated_kind>[a-z][a-z ]*))",
            rendered_pair,
        )
        test_case.assertIsNotNone(
            match, f"Malformed assignment pair {rendered_pair!r} in {text!r}."
        )
        rendered_pairs.append(
            (
                match["physician"].strip(),
                (match["parenthesized_kind"] or match["separated_kind"]).strip(),
            )
        )
    test_case.assertEqual(rendered_pairs, list(pairs))


def imported_state_snapshot():
    """Capture imported records and their retained histories for rejected-token no-effect checks."""
    return {
        "precepting": tuple(
            PreceptorAssignment.objects.order_by(
                "session__date",
                "session__period_of_day",
                "responsibility",
                "primary_position",
                "pk",
            ).values_list(
                "session__date",
                "session__period_of_day",
                "physician_id",
                "responsibility",
                "primary_position",
                "is_locked",
            )
        ),
        "late_docs": tuple(
            LateDocAssignment.objects.order_by("date", "pk").values_list("date", "physician_id")
        ),
        "interviews": tuple(
            InterviewShift.objects.order_by("date", "pk").values_list(
                "date", "physician_id", "note", "is_active"
            )
        ),
        "precepting_history": tuple(
            PreceptorAssignmentChange.objects.order_by("pk").values_list(
                "action", "previous_value", "recorded_value", "reason", "changed_by_id"
            )
        ),
        "late_doc_history": tuple(
            LateDocAssignmentChange.objects.order_by("pk").values_list(
                "action", "previous_value", "recorded_value", "note", "changed_by_id"
            )
        ),
        "interview_history": tuple(
            InterviewShiftChange.objects.order_by("pk").values_list(
                "action", "previous_value", "recorded_value", "changed_by_id"
            )
        ),
    }


def rendered_links(container, path):
    root = getattr(container, "root", container)
    return [
        link
        for link in root.descendants_named("a")
        if urlparse(link.attributes.get("href", "")).path == path
    ]


def assert_source_marker(test_case, markers, *, source, marker):
    source_token = re.compile(rf"(?<![A-Z0-9!]){re.escape(source)}(?![A-Z0-9])")
    matching = [item for item in markers if source_token.search(item) and marker in item]
    test_case.assertEqual(len(matching), 1)


class PreceptingWorkbookImportBrowserTests(TestCase):
    def setUp(self):
        self.administrator = get_user_model().objects.create_user(username="administrator")
        self.administrator.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )
        self.ordinary = get_user_model().objects.create_user(username="ordinary")
        self.community = Physician.objects.create(display_name="Community Doctor")
        self.preceptor = Physician.objects.create(display_name="First Faculty")
        self.interviewer = Physician.objects.create(display_name="Interview Faculty")
        EffectiveRole.objects.create(
            physician=self.community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2026, 1, 1),
        )
        for physician in (self.preceptor, self.interviewer):
            EffectiveRole.objects.create(
                physician=physician,
                role=EffectiveRole.Role.FACULTY,
                effective_start=date(2026, 1, 1),
            )
            FacultyAvailabilityTemplate.objects.create(
                physician=physician,
                slots={
                    slot: FacultyAvailabilityTemplate.base_selection("open")
                    for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
                },
            )
        self.url = reverse("precepting-workbook-import")

    def workbook(self, *, ambiguous=False, authoritative=False):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "JAN"
        for title in ("FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"):
            workbook.create_sheet(title)
        for worksheet in workbook.worksheets:
            worksheet["A1"] = "2026 Precepting"
        january = workbook["JAN"]
        for column, day in enumerate(range(5, 10), start=1):
            january.cell(3, column, day)
        january["A4"] = "First Faculty"
        january["A5"] = "Community Doctor"
        january["A5"].fill = PatternFill("solid", fgColor="FFF4B183")
        january["A7"] = "Interview Faculty"
        january["A7"].fill = PatternFill("solid", fgColor="FF00B050")
        january["B4"] = "Resident coverage"
        if authoritative:
            january["B4"] = "Authoritative Preceptor"
            january["A6"] = "Authoritative Backup"
            january["A7"] = "Authoritative Interviewer"
            january["A11"] = "Authoritative Late Doc"
        if ambiguous:
            Physician.objects.create(display_name="First Faculty")
        content = BytesIO()
        workbook.save(content)
        workbook.close()
        return SimpleUploadedFile(
            "2026 Precepting Schedule.xlsx",
            content.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def faculty(self, name):
        physician = Physician.objects.create(display_name=name)
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2026, 1, 1),
        )
        FacultyAvailabilityTemplate.objects.create(
            physician=physician,
            slots={
                slot: FacultyAvailabilityTemplate.base_selection("open")
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
        )
        return physician

    def split_workbook(self):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "JAN"
        for title in ("FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"):
            workbook.create_sheet(title)
        for worksheet in workbook.worksheets:
            worksheet["A1"] = "2026 Precepting"
        january = workbook["JAN"]
        for column, day in enumerate(range(5, 10), start=1):
            january.cell(3, column, day)
        january["A4"] = "First Faculty"
        january["A5"] = "Lark/Wren"
        january["A7"] = "Interview Faculty"
        january["A7"].fill = PatternFill("solid", fgColor="FF00B050")
        content = BytesIO()
        workbook.save(content)
        workbook.close()
        return SimpleUploadedFile(
            "2026 Precepting Schedule.xlsx",
            content.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_administrator_previews_then_applies_combined_import_without_reuploading(self):
        self.client.force_login(self.administrator)

        preview = self.client.post(self.url, {"workbook": self.workbook(), "action": "preview"})

        self.assertEqual(preview.status_code, 200)
        precepting = preview_row(
            preview,
            section="precepting-import-plan-heading",
            locator={
                "Date": date(2026, 1, 5),
                "Period": "AM",
            },
            fields={
                "Result": "New",
            },
        )
        assert_assignment_pairs(
            self,
            precepting["Assignments"],
            (("Community Doctor", "community"), ("First Faculty", "preceptor")),
        )
        preview_row(
            preview,
            section="precepting-import-interviews-heading",
            locator={
                "Domain": "Interview",
                "Date": date(2026, 1, 5),
            },
            fields={
                "Result": "New",
                "Assignment": "Interview Faculty",
            },
        )
        preview_document = _RenderedDocument(preview.content.decode())
        ignored_section = next(
            section
            for section in preview_document.root.descendants_named("section")
            if section.attributes.get("aria-labelledby") == "precepting-import-names-heading"
        )
        assert_source_marker(
            self,
            [marker.text() for marker in ignored_section.descendants_named("li")],
            source="JAN!B4",
            marker="Resident coverage",
        )
        token = preview.context["preview_token"]
        apply_form = preview_document.form_with_submission_action(name="action", value="apply")
        self.assertEqual(
            [
                input.attributes.get("value")
                for input in apply_form.descendants_named("input")
                if input.attributes.get("name") == "preview_token"
            ],
            [token],
        )

        applied = self.client.post(self.url, {"preview_token": token, "action": "apply"})

        self.assertEqual(applied.status_code, 200)
        self.assertTrue(applied.context["applied"])
        applied_document = _RenderedDocument(applied.content.decode())
        status = next(
            section
            for section in applied_document.root.descendants_named("section")
            if section.attributes.get("aria-labelledby") == "precepting-import-status-heading"
        )
        self.assertIn("Import applied", status.text())
        self.assertEqual(
            applied_document.forms_with_submission_action(name="action", value="apply"), []
        )
        self.assertEqual(len(rendered_links(status, reverse("monthly-workspace"))), 1)
        self.assertEqual(
            list(
                PreceptorAssignment.objects.filter(
                    session__date=date(2026, 1, 5), session__period_of_day="AM"
                )
                .order_by("responsibility", "primary_position")
                .values_list("physician_id", "responsibility", "primary_position")
            ),
            [
                (self.community.pk, "primary", 1),
                (self.preceptor.pk, "primary", 2),
            ],
        )
        self.assertEqual(
            InterviewShift.objects.get(date=date(2026, 1, 5), is_active=True).physician,
            self.interviewer,
        )

    def test_signed_preview_round_trip_labels_and_applies_split_secondary(self):
        lark = self.faculty("Lark")
        wren = self.faculty("Wren")
        self.client.force_login(self.administrator)

        preview = self.client.post(
            self.url, {"workbook": self.split_workbook(), "action": "preview"}
        )

        precepting = preview_row(
            preview,
            section="precepting-import-plan-heading",
            locator={
                "Date": date(2026, 1, 5),
                "Period": "AM",
            },
            fields={
                "Result": "New",
            },
        )
        assert_assignment_pairs(
            self,
            precepting["Assignments"],
            (("First Faculty", "preceptor"), ("Lark", "preceptor"), ("Wren", "secondary")),
        )
        token = preview.context["preview_token"]
        applied = self.client.post(self.url, {"preview_token": token, "action": "apply"})

        self.assertContains(applied, "Import applied")
        self.assertEqual(
            list(
                PreceptorAssignment.objects.filter(
                    session__date=date(2026, 1, 5), session__period_of_day="AM"
                )
                .order_by("responsibility", "primary_position")
                .values_list("physician_id", "responsibility", "primary_position")
            ),
            [
                (self.preceptor.pk, "primary", 1),
                (lark.pk, "primary", 2),
                (wren.pk, "secondary", None),
            ],
        )

    def test_signed_preview_round_trip_applies_authoritative_assignments(self):
        preceptor = Physician.objects.create(display_name="Authoritative Preceptor")
        backup = Physician.objects.create(display_name="Authoritative Backup")
        late_doc = Physician.objects.create(display_name="Authoritative Late Doc")
        interviewer = Physician.objects.create(display_name="Authoritative Interviewer")
        self.client.force_login(self.administrator)

        preview = self.client.post(
            self.url, {"workbook": self.workbook(authoritative=True), "action": "preview"}
        )

        january_5 = preview_row(
            preview,
            section="precepting-import-plan-heading",
            locator={
                "Date": date(2026, 1, 5),
                "Period": "AM",
            },
            fields={
                "Result": "New",
            },
        )
        assert_assignment_pairs(
            self,
            january_5["Assignments"],
            (
                ("Community Doctor", "community"),
                ("First Faculty", "preceptor"),
                ("Authoritative Backup", "secondary"),
            ),
        )
        january_6 = preview_row(
            preview,
            section="precepting-import-plan-heading",
            locator={"Date": date(2026, 1, 6), "Period": "AM"},
            fields={"Result": "New"},
        )
        assert_assignment_pairs(
            self,
            january_6["Assignments"],
            (("Authoritative Preceptor", "preceptor"),),
        )
        preview_row(
            preview,
            section="precepting-import-interviews-heading",
            locator={
                "Domain": "Late Doc",
                "Date": date(2026, 1, 5),
            },
            fields={
                "Result": "New",
                "Assignment": "Authoritative Late Doc",
            },
        )
        preview_row(
            preview,
            section="precepting-import-interviews-heading",
            locator={
                "Domain": "Interview",
                "Date": date(2026, 1, 5),
            },
            fields={
                "Result": "New",
                "Assignment": "Authoritative Interviewer",
            },
        )
        token = preview.context["preview_token"]
        applied = self.client.post(self.url, {"preview_token": token, "action": "apply"})

        self.assertContains(applied, "Import applied")
        self.assertEqual(
            PreceptorAssignment.objects.get(
                session__date=date(2026, 1, 6),
                session__period_of_day="AM",
                responsibility="primary",
                primary_position=1,
            ).physician,
            preceptor,
        )
        self.assertEqual(
            PreceptorAssignment.objects.get(
                session__date=date(2026, 1, 5),
                session__period_of_day="AM",
                responsibility="secondary",
            ).physician,
            backup,
        )
        self.assertEqual(
            InterviewShift.objects.get(date=date(2026, 1, 5), is_active=True).physician,
            interviewer,
        )
        self.assertEqual(LateDocAssignment.objects.get(date=date(2026, 1, 5)).physician, late_doc)

    def test_blocked_preview_cannot_be_applied_and_ordinary_users_cannot_access_or_see_it(self):
        self.client.force_login(self.administrator)
        state_before_blocked_apply = imported_state_snapshot()
        blocked = self.client.post(
            self.url, {"workbook": self.workbook(ambiguous=True), "action": "preview"}
        )

        self.assertContains(blocked, "Preview found blockers")
        self.assertIsNone(blocked.context["preview_token"])
        blocked_document = _RenderedDocument(blocked.content.decode())
        self.assertEqual(
            blocked_document.forms_with_submission_action(name="action", value="apply"), []
        )
        self.assertEqual(imported_state_snapshot(), state_before_blocked_apply)

        self.client.force_login(self.ordinary)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        calendar = self.client.get(reverse("monthly-workspace"))
        self.assertNotContains(calendar, "Import Schedule")

        self.client.force_login(self.administrator)
        calendar = self.client.get(reverse("monthly-workspace"))
        calendar_document = _RenderedDocument(calendar.content.decode())
        links = rendered_links(calendar_document, self.url)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].text().strip(), "Import Schedule")

    def test_ordinary_user_cannot_apply_preview(self):
        self.client.force_login(self.administrator)
        preview = self.client.post(self.url, {"workbook": self.workbook(), "action": "preview"})
        token = preview.context["preview_token"]

        self.client.force_login(self.ordinary)
        state_before_rejection = imported_state_snapshot()
        applied = self.client.post(self.url, {"preview_token": token, "action": "apply"})

        self.assertEqual(applied.status_code, 403)
        self.assertEqual(imported_state_snapshot(), state_before_rejection)

    def test_preview_token_is_bound_to_its_administrator(self):
        other_administrator = get_user_model().objects.create_user(username="other-administrator")
        other_administrator.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )
        preceptor = self.faculty("Authoritative Preceptor")
        backup = self.faculty("Authoritative Backup")
        late_doc = self.faculty("Authoritative Late Doc")
        interviewer = self.faculty("Authoritative Interviewer")
        self.client.force_login(self.administrator)
        preview = self.client.post(
            self.url, {"workbook": self.workbook(authoritative=True), "action": "preview"}
        )
        token = preview.context["preview_token"]
        state_before_rejection = imported_state_snapshot()

        self.client.force_login(other_administrator)
        applied = self.client.post(self.url, {"preview_token": token, "action": "apply"})

        self.assertEqual(applied.status_code, 200)
        self.assertFalse(applied.context["applied"])
        self.assertEqual(imported_state_snapshot(), state_before_rejection)

        self.client.force_login(self.administrator)
        owner_applied = self.client.post(self.url, {"preview_token": token, "action": "apply"})

        self.assertTrue(owner_applied.context["applied"])
        self.assertEqual(
            PreceptorAssignment.objects.get(
                session__date=date(2026, 1, 6),
                session__period_of_day="AM",
                responsibility="primary",
                primary_position=1,
            ).physician,
            preceptor,
        )
        self.assertEqual(
            PreceptorAssignment.objects.get(
                session__date=date(2026, 1, 5), responsibility="secondary"
            ).physician,
            backup,
        )
        self.assertEqual(LateDocAssignment.objects.get(date=date(2026, 1, 5)).physician, late_doc)
        self.assertEqual(
            InterviewShift.objects.get(date=date(2026, 1, 5), is_active=True).physician,
            interviewer,
        )

    def test_preview_token_expires_and_is_bound_to_the_precepting_workflow(self):
        self.client.force_login(self.administrator)
        preview = self.client.post(self.url, {"workbook": self.workbook(), "action": "preview"})
        token = preview.context["preview_token"]
        state_before_expired_apply = imported_state_snapshot()

        with self.assertRaises(PreceptingWorkbookImportPreviewError):
            load_precepting_workbook_import_preview_token(
                token=token, user=self.administrator, workflow="other"
            )
        with override_settings(PRECEPTING_WORKBOOK_IMPORT_PREVIEW_MAX_AGE=-1):
            expired = self.client.post(self.url, {"preview_token": token, "action": "apply"})

        self.assertEqual(expired.status_code, 200)
        self.assertFalse(expired.context["applied"])
        expired_document = _RenderedDocument(expired.content.decode())
        errors = [
            error.text().strip().lower()
            for error in expired_document.root.descendants_named("div")
            if "form-validation-message" in error.attributes.get("class", "").split()
        ]
        self.assertTrue(
            any(
                re.search(
                    r"\b(?:expired|invalid|unusable|required|could not be used|no longer valid)\b",
                    error,
                )
                and re.search(r"\b(?:preview|re-preview|new preview)\b", error)
                for error in errors
            )
        )
        self.assertEqual(
            expired_document.forms_with_submission_action(name="action", value="apply"), []
        )
        self.assertEqual(imported_state_snapshot(), state_before_expired_apply)

    def test_administrator_navigation_stays_shared_on_precepting_pages_and_drilldowns(self):
        period, _ = SchedulePeriod.objects.get_or_create(month=date(2026, 1, 1))
        session = PreceptorSession.objects.create(
            period=period, date=date(2026, 1, 5), period_of_day="AM"
        )
        self.client.force_login(self.administrator)
        pages = (
            reverse("monthly-workspace") + "?month=2026-01",
            reverse("preceptor-list") + "?month=2026-01",
            reverse("precepting-solver-workspace"),
            reverse("session-assignment-edit", args=(session.pk, "primary-1")),
            self.url,
        )

        for url in pages:
            with self.subTest(url=url):
                page = self.client.get(url)
                links = rendered_links(_RenderedDocument(page.content.decode()), self.url)
                self.assertEqual(len(links), 1)
                self.assertEqual(links[0].text().strip(), "Import Schedule")

        imported = self.client.get(self.url)
        current_links = [
            link
            for link in rendered_links(_RenderedDocument(imported.content.decode()), self.url)
            if link.attributes.get("aria-current") == "page"
        ]
        self.assertEqual([link.text().strip() for link in current_links], ["Import Schedule"])
