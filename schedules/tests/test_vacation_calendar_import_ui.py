import calendar
import re
from datetime import date
from io import BytesIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook

from schedules.models import Physician, Vacation, VacationChange
from schedules.services.vacations import create_vacation
from schedules.tests.test_table_presentation import _RenderedDataTable, _RenderedDocument

MONTHS = (
    ("JUL", 2026, 7),
    ("AUG", 2026, 8),
    ("SEPT", 2026, 9),
    ("OCT", 2026, 10),
    ("NOV", 2026, 11),
    ("DEC", 2026, 12),
    ("JAN", 2027, 1),
    ("FEB", 2027, 2),
    ("MAR", 2027, 3),
    ("APR", 2027, 4),
    ("MAY", 2027, 5),
    ("JUN", 2027, 6),
)


def _operation_row(response, *, action, physician):
    table = _RenderedDataTable(response.content.decode(), table_class="vacation-import-table")
    rows = [
        row
        for row in table.body_rows()
        if table.cell_for_header(row, "Action").text() == action
        and table.cell_for_header(row, "Physician").text() == physician
    ]
    if len(rows) != 1:
        raise AssertionError(f"Expected one {action!r} operation for {physician!r}.")
    return {
        header: table.cell_for_header(rows[0], header).text() for header in table.header_labels()
    }


def _vacation_state(*, vacation_ids=None):
    vacations = Vacation.objects.order_by("pk")
    changes = VacationChange.objects.order_by("pk")
    if vacation_ids is not None:
        vacations = vacations.filter(pk__in=vacation_ids)
        changes = changes.filter(vacation_id_at_recording__in=vacation_ids)
    return {
        "vacations": tuple(
            vacations.values_list(
                "pk",
                "physician_id",
                "start_date",
                "end_date",
                "period_of_day",
                "note",
                "created_at",
            )
        ),
        "changes": tuple(
            changes.values_list(
                "pk",
                "vacation_id",
                "vacation_id_at_recording",
                "physician_id",
                "action",
                "previous_value",
                "recorded_value",
                "previous_note",
                "recorded_note",
                "changed_by_id",
                "changed_at",
            )
        ),
    }


def _feedback_item(document, heading, *required_fragments):
    headings = [
        element
        for element in document.root.descendants_named("h2", "h3")
        if element.text().strip() == heading
    ]
    if len(headings) != 1:
        raise AssertionError(f"Expected one rendered feedback heading {heading!r}.")
    feedback_groups = [
        element
        for element in headings[0].ancestors()
        if "vacation-import-feedback" in element.attributes.get("class", "").split()
    ]
    if not feedback_groups:
        raise AssertionError(f"Expected {heading!r} to be in import feedback.")
    items = [
        item
        for item in feedback_groups[0].descendants_named("li")
        if all(fragment.casefold() in item.text().casefold() for fragment in required_fragments)
    ]
    if len(items) != 1:
        raise AssertionError(
            f"Expected one {heading!r} feedback item containing {required_fragments!r}."
        )
    return items[0]


def _definition_value(document, label):
    definitions = [
        element
        for element in document.root.descendants_named("dt")
        if element.text().strip() == label
    ]
    if len(definitions) != 1:
        raise AssertionError(f"Expected one rendered definition named {label!r}.")
    values = definitions[0].parent.children_named("dd")
    if len(values) != 1:
        raise AssertionError(f"Expected one rendered value for {label!r}.")
    return values[0].text()


def _marker_summary_item(document, marker, label, count):
    headings = [
        element
        for element in document.root.descendants_named("h2", "h3")
        if element.text().strip() == "Reported, not imported"
    ]
    if len(headings) != 1:
        raise AssertionError("Expected one rendered ignored-marker heading.")
    sections = [element for element in headings[0].ancestors() if element.tag == "section"]
    if not sections:
        raise AssertionError("Expected ignored-marker heading to be in a rendered section.")
    summaries = [
        element
        for element in sections[0].descendants_named("ul")
        if "vacation-import-marker-summary" in element.attributes.get("class", "").split()
    ]
    if len(summaries) != 1:
        raise AssertionError("Expected one rendered ignored-marker summary.")
    items = [
        item
        for item in summaries[0].children_named("li")
        if any(strong.text().strip() == marker for strong in item.descendants_named("strong"))
    ]
    if len(items) != 1:
        raise AssertionError(f"Expected one rendered {marker!r} marker summary.")
    item_text = items[0].text()
    if label.casefold() not in item_text.casefold():
        raise AssertionError(f"Expected {marker!r} marker summary to identify {label!r}.")
    matches = re.findall(r"\b(\d+)\s+physician-days?\b", item_text, flags=re.IGNORECASE)
    if len(matches) != 1:
        raise AssertionError(f"Expected one physician-day count for {marker!r}.")
    if int(matches[0]) != count:
        raise AssertionError(f"Expected {count} physician-days for {marker!r}.")


_DISPLAYED_DATE = re.compile(
    r"(?<!\d)(?:(?P<iso>\d{4}-\d{1,2}-\d{1,2})|"
    r"(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+(?P<day>\d{1,2}),\s*(?P<year>\d{4}))(?!\d)",
    flags=re.IGNORECASE,
)


def _displayed_dates(value):
    dates = []
    for match in _DISPLAYED_DATE.finditer(value):
        if match.group("iso"):
            dates.append(date.fromisoformat(match.group("iso")))
        else:
            dates.append(
                date(
                    int(match.group("year")),
                    next(
                        month
                        for month in range(1, 13)
                        if calendar.month_name[month]
                        .casefold()
                        .startswith(match.group("month").casefold())
                    ),
                    int(match.group("day")),
                )
            )
    return dates


def _assert_displayed_date_range(test_case, value, start, end):
    displayed = _displayed_dates(value)
    if start == end:
        test_case.assertIn(len(displayed), (1, 2))
        test_case.assertEqual(set(displayed), {start})
    else:
        test_case.assertEqual(displayed, [start, end])


def _assert_recorded_vacation_value(test_case, recorded_value, start, end, period):
    test_case.assertEqual(recorded_value["start_date"], start.isoformat())
    test_case.assertEqual(recorded_value["end_date"], end.isoformat())
    test_case.assertEqual(recorded_value["period_of_day"], period)


def _workbook_error(document):
    fields = [
        element
        for element in document.root.descendants_named("div")
        if "form-field-group" in element.attributes.get("class", "").split()
        and any(
            input_.attributes.get("name") == "workbook"
            for input_ in element.descendants_named("input")
        )
    ]
    if len(fields) != 1:
        raise AssertionError("Expected one workbook form field.")
    errors = [
        error
        for error in fields[0].descendants_named("ul")
        if "errorlist" in error.attributes.get("class", "").split()
    ]
    if len(errors) != 1:
        raise AssertionError("Expected one bound workbook error list.")
    return errors[0].text()


class VacationCalendarImportUITests(TestCase):
    def setUp(self):
        self.administrator = get_user_model().objects.create_user(
            username="administrator", password="test-password"
        )
        self.administrator.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.physician = Physician.objects.create(display_name="Amy Example")
        self.second_physician = Physician.objects.create(display_name="Blair Example")
        self.url = reverse("vacation-calendar-import")

    def workbook_upload(self, *, missing_physician_vacation=False):
        workbook = Workbook()
        workbook.remove(workbook.active)
        markers = {
            date(2026, 7, 1): "V",
            date(2026, 7, 2): "V",
            date(2026, 7, 3): "E",
            date(2026, 7, 4): "I",
        }
        for title, year, month in MONTHS:
            worksheet = workbook.create_sheet(title)
            worksheet["B1"] = calendar.month_name[month].upper()
            worksheet["B2"] = year
            worksheet["B3"] = "RESIDENTS NAME"
            for day in range(1, calendar.monthrange(year, month)[1] + 1):
                worksheet.cell(3, day + 2, day)
            worksheet["B4"] = "Resident, Ignore"
            worksheet["C4"] = "V"
            worksheet["B5"] = "FACULTY:"
            worksheet["B6"] = "Example, Amy"
            for marker_date, marker in markers.items():
                if marker_date.year == year and marker_date.month == month:
                    worksheet.cell(6, marker_date.day + 2, marker)
            worksheet["B7"] = "Example, Blair"
            if month == 8:
                worksheet["C7"] = "V"
            if month == 7:
                worksheet["D7"] = "I"
            worksheet["B8"] = "Missing, Person"
            if missing_physician_vacation and month == 7:
                worksheet["C8"] = "V"
        metadata = workbook.create_sheet("`Mos.Yr")
        metadata["A1"] = "Months"
        metadata["B1"] = "Years"
        for row, (_, year, month) in enumerate(MONTHS, start=2):
            metadata.cell(row, 1, calendar.month_name[month].upper())
            metadata.cell(row, 2, year)
        content = BytesIO()
        workbook.save(content)
        workbook.close()
        return SimpleUploadedFile(
            "Vacation Calendar 2026-2027.xlsx",
            content.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_administrator_only_upload_page_is_linked_from_vacation_navigation(self):
        self.client.force_login(self.administrator)

        page = self.client.get(self.url)
        vacation = self.client.get(reverse("vacation-workspace"))
        holidays = self.client.get(reverse("holiday-workspace"))

        document = _RenderedDocument(page.content.decode())
        preview_form = document.form_with_submission_action(name="action", value="preview")
        apply_form = document.form_with_submission_action(name="action", value="apply")
        self.assertIs(preview_form, apply_form)
        self.assertEqual(preview_form.attributes.get("method"), "post")
        self.assertEqual(preview_form.attributes.get("enctype"), "multipart/form-data")
        controls = preview_form.descendants_named("input")
        self.assertEqual(
            [
                control.attributes.get("type")
                for control in controls
                if control.attributes.get("name") == "workbook"
            ],
            ["file"],
        )
        self.assertEqual(
            [
                control.attributes.get("type")
                for control in controls
                if control.attributes.get("name") == "replace"
            ],
            ["checkbox"],
        )
        active_navigation = [
            link
            for link in document.root.descendants_named("a")
            if link.text().strip() == "Import"
            and link.attributes.get("href") == self.url
            and link.attributes.get("aria-current") == "page"
        ]
        self.assertEqual(len(active_navigation), 1)
        for workspace in (vacation, holidays):
            links = [
                link
                for link in _RenderedDocument(workspace.content.decode()).root.descendants_named(
                    "a"
                )
                if link.text().strip() == "Import" and link.attributes.get("href") == self.url
            ]
            self.assertEqual(len(links), 1)

        self.client.force_login(self.scheduler)
        ordinary_vacation = self.client.get(reverse("vacation-workspace"))
        forbidden = self.client.get(self.url)

        self.assertFalse(
            [
                link
                for link in _RenderedDocument(
                    ordinary_vacation.content.decode()
                ).root.descendants_named("a")
                if link.attributes.get("href") == self.url
            ]
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_import_form_uses_shared_processing_feedback_presentation(self):
        self.client.force_login(self.administrator)

        response = self.client.get(self.url)

        document = _RenderedDocument(response.content.decode())
        form = document.form_with_submission_action(name="action", value="preview")
        self.assertIn("data-processing-feedback-form", form.attributes)
        dialogs = [
            element
            for element in form.descendants_named("div")
            if "data-processing-feedback" in element.attributes
        ]
        self.assertEqual(len(dialogs), 1)
        dialog = dialogs[0]
        self.assertIn("hidden", dialog.attributes)
        self.assertEqual(dialog.attributes.get("role"), "dialog")
        self.assertEqual(dialog.attributes.get("aria-modal"), "true")
        self.assertIn("processing", dialog.attributes.get("aria-label", "").lower())
        self.assertIn("workbook", dialog.attributes.get("aria-label", "").lower())
        self.assertTrue(
            any(
                script.attributes.get("src", "").endswith("form-processing-feedback.js")
                for script in document.root.descendants_named("script")
            )
        )

    def test_preview_is_non_mutating_and_apply_shows_the_interpreted_result(self):
        self.client.force_login(self.administrator)
        unaffected_physician = Physician.objects.create(display_name="Unaffected Physician")
        unaffected = create_vacation(
            physician=unaffected_physician,
            start_date=date(2026, 10, 10),
            end_date=date(2026, 10, 10),
            period_of_day=Vacation.PeriodOfDay.PM,
            note="Keep this unrelated record.",
            scheduler=self.administrator,
        )
        unaffected_state = _vacation_state(vacation_ids=(unaffected.pk,))
        before_preview = _vacation_state()

        preview = self.client.post(
            self.url,
            {"workbook": self.workbook_upload(), "action": "preview"},
        )

        self.assertEqual(preview.status_code, 200)
        self.assertEqual(_vacation_state(), before_preview)
        document = _RenderedDocument(preview.content.decode())
        window = _definition_value(document, "Workbook window")
        _assert_displayed_date_range(self, window, date(2026, 7, 1), date(2027, 6, 30))
        _feedback_item(document, "Warnings", "Person Missing", "no vacation markers")
        _marker_summary_item(document, "E", "Event", 1)
        _marker_summary_item(document, "I", "Interview", 2)
        amy_preview = _operation_row(preview, action="Create", physician="Amy Example")
        _assert_displayed_date_range(self, amy_preview["Dates"], date(2026, 7, 1), date(2026, 7, 2))
        self.assertEqual(amy_preview["Coverage"], "Whole day")
        blair_preview = _operation_row(preview, action="Create", physician="Blair Example")
        _assert_displayed_date_range(
            self, blair_preview["Dates"], date(2026, 8, 1), date(2026, 8, 1)
        )
        self.assertEqual(blair_preview["Coverage"], "Whole day")

        applied = self.client.post(
            self.url,
            {"workbook": self.workbook_upload(), "action": "apply"},
        )

        self.assertEqual(applied.status_code, 200)
        self.assertContains(applied, "Import applied")
        self.assertEqual(
            set(
                Vacation.objects.filter(
                    physician_id__in=(self.physician.pk, self.second_physician.pk)
                ).values_list("physician_id", "start_date", "end_date", "period_of_day")
            ),
            {
                (
                    self.physician.pk,
                    date(2026, 7, 1),
                    date(2026, 7, 2),
                    Vacation.PeriodOfDay.WHOLE_DAY,
                ),
                (
                    self.second_physician.pk,
                    date(2026, 8, 1),
                    date(2026, 8, 1),
                    Vacation.PeriodOfDay.WHOLE_DAY,
                ),
            },
        )
        amy_vacation = Vacation.objects.get(physician=self.physician)
        blair_vacation = Vacation.objects.get(physician=self.second_physician)
        for vacation, start, end in (
            (amy_vacation, date(2026, 7, 1), date(2026, 7, 2)),
            (blair_vacation, date(2026, 8, 1), date(2026, 8, 1)),
        ):
            self.assertIn("Vacation Calendar 2026-2027.xlsx", vacation.note)
            change = VacationChange.objects.get(vacation=vacation)
            self.assertEqual(change.changed_by, self.administrator)
            self.assertEqual(change.action, VacationChange.Action.CREATED)
            _assert_recorded_vacation_value(
                self, change.recorded_value, start, end, Vacation.PeriodOfDay.WHOLE_DAY
            )
            self.assertIn("Vacation Calendar 2026-2027.xlsx", change.recorded_note)
        self.assertEqual(_vacation_state(vacation_ids=(unaffected.pk,)), unaffected_state)

        before_reapply = _vacation_state()
        self.client.post(
            self.url,
            {"workbook": self.workbook_upload(), "action": "apply"},
        )
        self.assertEqual(_vacation_state(), before_reapply)

    def test_apply_reports_blockers_and_replace_removes_only_stale_scoped_data(self):
        self.client.force_login(self.administrator)
        blocked = self.client.post(
            self.url,
            {"workbook": self.workbook_upload(missing_physician_vacation=True), "action": "apply"},
        )

        _feedback_item(
            _RenderedDocument(blocked.content.decode()),
            "Blockers",
            "Person Missing",
            "no matching physician",
        )
        self.assertEqual(_vacation_state(), {"vacations": (), "changes": ()})

        stale = create_vacation(
            physician=self.physician,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 2),
            period_of_day=Vacation.PeriodOfDay.AM,
            scheduler=self.administrator,
        )
        outside = create_vacation(
            physician=self.physician,
            start_date=date(2027, 7, 1),
            end_date=date(2027, 7, 2),
            period_of_day=Vacation.PeriodOfDay.PM,
            note="Outside the workbook window.",
            scheduler=self.administrator,
        )
        unrelated_physician = Physician.objects.create(display_name="Unaffiliated Physician")
        unrelated = create_vacation(
            physician=unrelated_physician,
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 2),
            note="A different physician's vacation.",
            scheduler=self.administrator,
        )
        unaffected_state = _vacation_state(vacation_ids=(outside.pk, unrelated.pk))
        replaced = self.client.post(
            self.url,
            {"workbook": self.workbook_upload(), "replace": "on", "action": "apply"},
        )

        self.assertContains(replaced, "Import applied")
        stale_preview = _operation_row(replaced, action="Remove", physician="Amy Example")
        _assert_displayed_date_range(
            self, stale_preview["Dates"], date(2026, 9, 1), date(2026, 9, 2)
        )
        self.assertEqual(stale_preview["Coverage"], "AM only")
        blair_preview = _operation_row(replaced, action="Create", physician="Blair Example")
        self.assertEqual(blair_preview["Coverage"], "Whole day")
        self.assertFalse(Vacation.objects.filter(pk=stale.pk).exists())
        self.assertEqual(_vacation_state(vacation_ids=(outside.pk, unrelated.pk)), unaffected_state)
        self.assertEqual(Vacation.objects.filter(physician=self.physician).count(), 2)
        self.assertEqual(Vacation.objects.filter(physician=self.second_physician).count(), 1)

    def test_invalid_excel_upload_has_compact_feedback(self):
        self.client.force_login(self.administrator)
        upload = SimpleUploadedFile(
            "vacation-calendar.xlsx",
            b"not an Excel workbook",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        response = self.client.post(self.url, {"workbook": upload, "action": "preview"})

        self.assertRegex(
            _workbook_error(_RenderedDocument(response.content.decode())),
            r"(?i)\bnot\s+a\s+valid\s+\.xlsx\s+archive\b",
        )
        self.assertEqual(Vacation.objects.count(), 0)
