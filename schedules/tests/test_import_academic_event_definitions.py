import re
from datetime import date
from io import BytesIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook

from schedules.models import (
    AcademicEvent,
    AcademicEventDefinition,
    AcademicEventDefinitionChange,
    AcademicEventMonthlyRule,
    Physician,
)
from schedules.services.academic_event_definition_import import (
    AcademicEventDefinitionImportError,
    apply_academic_event_definition_import,
    build_academic_event_definition_import_plan,
    load_academic_event_definition_import,
)
from schedules.services.academic_event_import_preview import (
    AcademicEventImportPreviewError,
    create_academic_event_import_preview_token,
    load_academic_event_import_preview_token,
)
from schedules.services.academic_events import (
    correct_academic_event_definition,
    correct_academic_event_monthly_rule,
    create_academic_event_definition,
    deactivate_academic_event_definition,
)
from schedules.tests.test_table_presentation import _RenderedDataTable, _RenderedDocument

_EXPECTED_ACADEMIC_EVENT_HEADERS = (
    # TEXT-CONTRACT: academic-event-import-event-column — The independent input literal
    # `Event` is required by docs/design.md's Academic Event import paragraph beginning
    # “The importer selects exactly one worksheet with a supported Event column”. Scope:
    # recognition of that token after accepted case-insensitive, whitespace normalization,
    # not the private header collection. Protected by
    # schedules/tests/test_import_academic_event_definitions.py::AcademicEventDefinitionImportTests.test_parser_recognizes_faculty_meetings_and_preserves_other_rows_as_yearly.
    "Event",
    "Date",
    "End Date",
    "Typically Scheduled",
)

_RECONCILIATION_CLAIM_OWNERS = "Muriel|current|existing|workbook|imported"
_RECONCILIATION_CLAIM_SEPARATOR = r"(?:is|was|:|=|—|-)"


def _preview_row(response, *, event, item_type=None):
    table = _RenderedDataTable(response.content.decode(), table_class="vacation-import-table")
    rows = table.body_rows()
    matches = [
        row
        for row in rows
        if table.cell_for_header(row, "Event").text() == event
        and (item_type is None or table.cell_for_header(row, "Item").text() == item_type)
    ]
    if len(matches) != 1:
        raise AssertionError(f"Expected one preview row for {event!r} and {item_type!r}.")
    return {
        header: table.cell_for_header(matches[0], header).text() for header in table.header_labels()
    }


def _assert_reconciliation_claims(test_case, detail, *, field, current_value, imported_value):
    """Require one rendered reconciliation field to retain each fact's owner."""
    if not isinstance(detail, str):
        detail = "; ".join(detail)
    field_match = re.search(
        rf"(?:^|;\s*){re.escape(field)}\s*(?::|—|-)?\s*"
        rf".*?(?=$|;(?!(?:\s*(?:{_RECONCILIATION_CLAIM_OWNERS})\b\s*"
        rf"{_RECONCILIATION_CLAIM_SEPARATOR}?\s*))\s*[^:;]+:)",
        detail,
        flags=re.IGNORECASE,
    )
    if field_match is None:
        raise AssertionError(f"Expected a {field!r} reconciliation in {detail!r}.")
    field_detail = field_match.group()

    def claim(owner, value):
        value_pattern = rf"(?<!\w){re.escape(str(value))}(?!\w)"
        test_case.assertRegex(
            field_detail,
            re.compile(
                rf"\b(?:{owner})\b\s*{_RECONCILIATION_CLAIM_SEPARATOR}?\s*"
                rf"{value_pattern}",
                flags=re.IGNORECASE,
            ),
        )

    claim("Muriel|current|existing", current_value)
    claim("workbook|imported", imported_value)


class AcademicEventDefinitionImportTests(TestCase):
    def setUp(self):
        self.administrator = get_user_model().objects.create_user(username="administrator")
        self.administrator.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.ordinary = get_user_model().objects.create_user(username="ordinary")

    def workbook(
        self,
        rows,
        *,
        name="Academic Year Calendar 2026-2027.xlsx",
        worksheet_title="Academic Year Calendar",
        academic_year_label="Academic Year Calendar 2026-2027",
        headers=_EXPECTED_ACADEMIC_EVENT_HEADERS,
        extra_sheets=(),
    ):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = worksheet_title
        worksheet.append([academic_year_label])
        worksheet.append(headers)
        current_quarter = None
        for item in rows:
            if isinstance(item, str):
                current_quarter = item
                worksheet.append([current_quarter])
            else:
                worksheet.append(item)
        for title, sheet_rows in extra_sheets:
            worksheet = workbook.create_sheet(title)
            for row in sheet_rows:
                worksheet.append(row)
        content = BytesIO()
        workbook.save(content)
        workbook.close()
        content.seek(0)
        content.name = name
        return content

    def plan(self, rows):
        data = load_academic_event_definition_import(self.workbook(rows))
        return build_academic_event_definition_import_plan(data)

    def test_parser_recognizes_faculty_meetings_and_preserves_other_rows_as_yearly(self):
        plan = self.plan(
            [
                [
                    "Faculty Meetings",
                    None,
                    None,
                    "Thursdays except the second Thursday; review the third Thursday.",
                ],
                "Q1",
                ["Orientation", date(2026, 7, 10), date(2026, 7, 12), " Second week of July "],
                "Q2",
                ["Residency Interviews", None, None, "October"],
                ["Committee Meetings", None, None, "First Tuesday each month"],
            ]
        )

        self.assertEqual(plan.data.academic_year, 2026)
        self.assertEqual(
            [(item.base_name, item.scheduling_mode) for item in plan.create],
            [
                ("Faculty Meetings", "monthly_rules"),
                ("Orientation", "yearly"),
                ("Committee Meetings", "yearly"),
            ],
        )
        faculty = plan.create[0]
        self.assertEqual(faculty.expected_quarter, None)
        self.assertEqual(
            faculty.typically_scheduled,
            "Thursdays except the second Thursday; review the third Thursday.",
        )
        self.assertEqual(faculty.default_precepting_impact, AcademicEvent.PreceptingImpact.NONE)
        self.assertTrue(faculty.all_available_faculty)
        self.assertEqual(faculty.monthly_rule.weekday, 3)
        self.assertEqual(faculty.monthly_rule.ordinals, (1, 3, 4, 5))
        self.assertEqual(faculty.monthly_rule.effective_start, date(2026, 7, 1))
        self.assertEqual(plan.rule_create, (faculty,))
        self.assertEqual(len(plan.data.unsupported), 1)
        self.assertEqual(plan.data.unsupported[0].source.base_name, "Residency Interviews")
        self.assertEqual(plan.data.unsupported[0].source.row_number, 7)

    def test_preview_token_restores_only_the_original_user_and_workflow(self):
        data = load_academic_event_definition_import(
            self.workbook(["Q1", ["Orientation", None, None, "July"]])
        )
        token = create_academic_event_import_preview_token(
            data=data, user=self.administrator, workflow="definition"
        )

        restored = load_academic_event_import_preview_token(
            token=token, user=self.administrator, workflow="definition"
        )

        self.assertEqual(restored, data)
        for user, workflow in ((self.ordinary, "definition"), (self.administrator, "schedule")):
            with self.subTest(user=user.username, workflow=workflow):
                with self.assertRaisesRegex(AcademicEventImportPreviewError, "no longer valid"):
                    load_academic_event_import_preview_token(
                        token=token, user=user, workflow=workflow
                    )
        altered = token[:-1] + ("a" if token[-1] != "a" else "b")
        with self.assertRaisesRegex(AcademicEventImportPreviewError, "no longer valid"):
            load_academic_event_import_preview_token(
                token=altered, user=self.administrator, workflow="definition"
            )
        with override_settings(ACADEMIC_EVENT_IMPORT_PREVIEW_MAX_AGE=-1):
            with self.assertRaisesRegex(AcademicEventImportPreviewError, "no longer valid"):
                load_academic_event_import_preview_token(
                    token=token, user=self.administrator, workflow="definition"
                )

    def test_parser_recognizes_pipe_delimited_quarter_section_labels(self):
        plan = self.plan(
            [
                "Q1 | July, August, September",
                ["Orientation", None, None, "July"],
                "Q2 | October, November, December",
                ["Grand Rounds", None, None, "October"],
                "Q3 | January, February, March",
                ["Clinical Skills", None, None, "January"],
                "Q4 | April, May, June",
                ["Capstone", None, None, "April"],
            ]
        )

        self.assertEqual(
            [(source.base_name, source.expected_quarter) for source in plan.create],
            [
                ("Orientation", "Q1"),
                ("Grand Rounds", "Q2"),
                ("Clinical Skills", "Q3"),
                ("Capstone", "Q4"),
            ],
        )

    def test_parser_recognizes_a_year_named_sheet_and_ignores_unrelated_worksheets(self):
        data = load_academic_event_definition_import(
            self.workbook(
                ["Q1", ["Orientation", None, None, "July"]],
                worksheet_title="2026-2027",
                academic_year_label="Academic Year Calendar",
                extra_sheets=(("Notes", [["Reviewed by scheduling"]]),),
            )
        )

        self.assertEqual(data.academic_year, 2026)
        self.assertEqual([definition.base_name for definition in data.definitions], ["Orientation"])

    def test_parser_rejects_missing_or_ambiguous_matching_calendar_sheets(self):
        with self.assertRaisesRegex(
            AcademicEventDefinitionImportError, "exactly one matching calendar worksheet"
        ):
            load_academic_event_definition_import(
                self.workbook(
                    [["Orientation", None, None, "July"]],
                    headers=("Description",),
                    worksheet_title="2026-2027",
                    academic_year_label="Academic Year Calendar",
                )
            )

        with self.assertRaisesRegex(
            AcademicEventDefinitionImportError, "multiple matching calendar worksheets"
        ):
            load_academic_event_definition_import(
                self.workbook(
                    ["Q1", ["Orientation", None, None, "July"]],
                    worksheet_title="2026-2027",
                    academic_year_label="Academic Year Calendar",
                    extra_sheets=(
                        (
                            "2027-2028",
                            [
                                ["Academic Year Calendar"],
                                ["Event", "Date", "End Date", "Typically Scheduled"],
                                ["Q1"],
                                ["Orientation", None, None, "July"],
                            ],
                        ),
                    ),
                )
            )

    def test_parser_rejects_other_out_of_quarter_rows_and_does_not_guess_recurrence(self):
        plan = self.plan(["Q1", ["Grand Rounds", None, None, "First Thursday each month"]])

        self.assertEqual(
            plan.create[0].scheduling_mode, AcademicEventDefinition.SchedulingMode.YEARLY
        )
        self.assertIsNone(plan.create[0].monthly_rule)
        with self.assertRaisesRegex(AcademicEventDefinitionImportError, "appears before a Q1-Q4"):
            self.plan([["Grand Rounds", None, None, "First Thursday each month"]])

    def test_apply_creates_faculty_meetings_definition_and_initial_rule(self):
        plan = self.plan(
            [
                [
                    "Faculty Meetings",
                    None,
                    None,
                    "Thursdays except the second Thursday; review the third Thursday.",
                ]
            ]
        )

        apply_academic_event_definition_import(plan=plan, scheduler=self.administrator)

        definition = AcademicEventDefinition.objects.get(base_name="Faculty Meetings")
        rule = AcademicEventMonthlyRule.objects.get(definition=definition)
        self.assertEqual(
            definition.scheduling_mode, AcademicEventDefinition.SchedulingMode.MONTHLY_RULES
        )
        self.assertIsNone(definition.expected_quarter)
        self.assertEqual(definition.start_academic_year, 2026)
        self.assertIsNone(definition.end_academic_year)
        self.assertEqual(definition.default_period_of_day, AcademicEvent.PeriodOfDay.WHOLE_DAY)
        self.assertEqual(definition.default_precepting_impact, AcademicEvent.PreceptingImpact.NONE)
        self.assertTrue(definition.all_available_faculty)
        self.assertEqual(
            definition.typically_scheduled,
            "Thursdays except the second Thursday; review the third Thursday.",
        )
        self.assertEqual(
            (rule.weekday, rule.ordinals, rule.effective_start, rule.effective_end),
            (3, [1, 3, 4, 5], date(2026, 7, 1), None),
        )
        reimport = self.plan(
            [
                [
                    "Faculty Meetings",
                    None,
                    None,
                    "Thursdays except the second Thursday; review the third Thursday.",
                ]
            ]
        )
        self.assertEqual((len(reimport.exact), len(reimport.rule_exact)), (1, 1))

    def test_preview_reports_a_rule_conflict_when_faculty_meetings_is_yearly(self):
        existing = create_academic_event_definition(
            base_name="Faculty Meetings",
            start_academic_year=2026,
            expected_quarter="Q1",
            scheduler=self.administrator,
        )
        plan = self.plan([["Faculty Meetings", None, None, "Thursdays except the second Thursday"]])

        self.assertEqual(len(plan.different), 1)
        difference = plan.different[0]
        self.assertEqual(difference.definition, existing)
        self.assertEqual(
            (difference.source.base_name, difference.source.row_number),
            ("Faculty Meetings", 3),
        )
        self.assertEqual(
            (difference.definition.scheduling_mode, difference.source.scheduling_mode),
            (AcademicEventDefinition.SchedulingMode.YEARLY, "monthly_rules"),
        )
        self.assertEqual(len(plan.rule_conflicts), 1)
        self.assertEqual(plan.rule_conflicts[0].source, difference.source)
        with self.assertRaises(AcademicEventDefinitionImportError):
            apply_academic_event_definition_import(plan=plan, scheduler=self.administrator)
        self.assertEqual(
            AcademicEventDefinition.objects.get(pk=existing.pk).scheduling_mode, "yearly"
        )

    def test_reimport_preserves_customized_faculty_definition_and_rule(self):
        rows = [["Faculty Meetings", None, None, "Thursdays except the second Thursday"]]
        apply_academic_event_definition_import(plan=self.plan(rows), scheduler=self.administrator)
        definition = AcademicEventDefinition.objects.get(base_name="Faculty Meetings")
        rule = AcademicEventMonthlyRule.objects.get(definition=definition)
        participant = Physician.objects.create(display_name="Local default participant")
        correct_academic_event_definition(
            definition=definition,
            base_name=definition.base_name,
            start_academic_year=2024,
            end_academic_year=2027,
            typically_scheduled="Locally scheduled after faculty review",
            all_available_faculty=False,
            default_participants=(participant,),
            scheduler=self.administrator,
        )
        correct_academic_event_monthly_rule(
            rule=rule,
            weekday=4,
            ordinals=[2],
            effective_start=date(2026, 7, 1),
            scheduler=self.administrator,
        )
        plan = self.plan(rows)

        self.assertEqual((len(plan.different), len(plan.rule_different)), (1, 1))
        self.assertEqual(plan.conflicts, ())
        apply_academic_event_definition_import(plan=plan, scheduler=self.administrator)

        definition.refresh_from_db()
        rule.refresh_from_db()
        self.assertEqual(
            (definition.start_academic_year, definition.end_academic_year), (2024, 2027)
        )
        self.assertEqual(definition.typically_scheduled, "Locally scheduled after faculty review")
        self.assertFalse(definition.all_available_faculty)
        self.assertEqual(list(definition.default_participants.all()), [participant])
        self.assertEqual((rule.weekday, rule.ordinals), (4, [2]))

    def test_apply_creates_missing_repeated_names_without_changing_existing_definitions(self):
        existing = create_academic_event_definition(
            base_name="Orientation",
            start_academic_year=2024,
            expected_quarter="Q2",
            default_duration_days=1,
            default_period_of_day=AcademicEvent.PeriodOfDay.AM,
            default_required_participant_count=2,
            typically_scheduled="Local orientation week",
            scheduler=self.administrator,
        )
        absent = create_academic_event_definition(
            base_name="Unlisted event",
            start_academic_year=2024,
            expected_quarter="Q4",
            scheduler=self.administrator,
        )
        plan = self.plan(
            [
                "Q1",
                ["Orientation", date(2026, 7, 10), date(2026, 7, 12), "July"],
                "Q2",
                ["Orientation", None, None, "October"],
                ["Grand Rounds", None, None, "Wednesday"],
            ]
        )

        self.assertEqual(len(plan.create), 2)
        self.assertEqual(len(plan.different), 1)
        difference = plan.different[0]
        self.assertEqual(
            (difference.definition, difference.source.base_name, difference.source.row_number),
            (existing, "Orientation", 6),
        )
        self.assertEqual(
            (
                difference.definition.start_academic_year,
                difference.definition.default_period_of_day,
                difference.definition.default_required_participant_count,
                difference.definition.typically_scheduled,
            ),
            (2024, AcademicEvent.PeriodOfDay.AM, 2, "Local orientation week"),
        )
        self.assertEqual(
            (
                plan.data.academic_year,
                difference.source.expected_quarter,
                difference.source.default_duration_days,
                difference.source.typically_scheduled,
            ),
            (2026, "Q2", 1, "October"),
        )
        _assert_reconciliation_claims(
            self,
            difference.differences,
            field="Coverage",
            current_value="AM",
            imported_value="Whole day",
        )
        _assert_reconciliation_claims(
            self,
            difference.differences,
            field="Participant requirement",
            current_value=2,
            imported_value=1,
        )
        apply_academic_event_definition_import(plan=plan, scheduler=self.administrator)

        existing.refresh_from_db()
        absent.refresh_from_db()
        orientation_q1 = AcademicEventDefinition.objects.get(
            base_name="Orientation", expected_quarter="Q1"
        )
        self.assertEqual(existing.expected_quarter, "Q2")
        self.assertEqual(existing.start_academic_year, 2024)
        self.assertEqual(existing.default_period_of_day, AcademicEvent.PeriodOfDay.AM)
        self.assertEqual(existing.default_required_participant_count, 2)
        self.assertEqual(existing.typically_scheduled, "Local orientation week")
        self.assertEqual(orientation_q1.default_duration_days, 3)
        self.assertEqual(orientation_q1.start_academic_year, 2026)
        self.assertIsNone(orientation_q1.end_academic_year)
        self.assertEqual(absent.pk, AcademicEventDefinition.objects.get(pk=absent.pk).pk)
        self.assertEqual(AcademicEventDefinitionChange.objects.count(), 4)

    def test_separate_date_columns_preserve_their_written_duration(self):
        plan = self.plan(
            [
                "Q4",
                ["Spring-to-summer course", date(2027, 4, 1), date(2027, 7, 1), "Spring"],
            ]
        )

        self.assertEqual(plan.create[0].default_duration_days, 92)

    def test_reimport_is_a_noop_and_stale_plan_cannot_overwrite_a_newly_existing_definition(self):
        rows = ["Q1", ["Orientation", None, None, "July"]]
        plan = self.plan(rows)
        apply_academic_event_definition_import(plan=plan, scheduler=self.administrator)
        counts = (
            AcademicEventDefinition.objects.count(),
            AcademicEventDefinitionChange.objects.count(),
        )

        apply_academic_event_definition_import(plan=self.plan(rows), scheduler=self.administrator)
        self.assertEqual(
            (
                AcademicEventDefinition.objects.count(),
                AcademicEventDefinitionChange.objects.count(),
            ),
            counts,
        )

        stale_plan = self.plan(["Q2", ["Grand Rounds", None, None, "October"]])
        grand_rounds = create_academic_event_definition(
            base_name="Grand Rounds",
            start_academic_year=2024,
            expected_quarter="Q2",
            default_duration_days=2,
            default_period_of_day=AcademicEvent.PeriodOfDay.AM,
            default_required_participant_count=2,
            typically_scheduled="First Tuesday",
            scheduler=self.administrator,
        )
        with self.assertRaisesRegex(AcademicEventDefinitionImportError, "changed after preview"):
            apply_academic_event_definition_import(plan=stale_plan, scheduler=self.administrator)
        grand_rounds.refresh_from_db()
        self.assertEqual(grand_rounds.start_academic_year, 2024)
        self.assertEqual(grand_rounds.default_duration_days, 2)
        self.assertEqual(grand_rounds.default_period_of_day, AcademicEvent.PeriodOfDay.AM)
        self.assertEqual(grand_rounds.default_required_participant_count, 2)
        self.assertEqual(grand_rounds.typically_scheduled, "First Tuesday")

    def test_inactive_definition_is_previewed_and_preserved_without_recreation(self):
        definition = create_academic_event_definition(
            base_name="Orientation",
            start_academic_year=2026,
            expected_quarter="Q1",
            typically_scheduled="July",
            scheduler=self.administrator,
        )
        deactivate_academic_event_definition(definition=definition, scheduler=self.administrator)

        plan = self.plan(["Q1", ["Orientation", None, None, "July"]])

        self.assertEqual(plan.create, ())
        self.assertEqual(plan.conflicts, ())
        self.assertEqual(len(plan.exact), 1)
        self.assertEqual(plan.exact[0].definition, definition)
        apply_academic_event_definition_import(plan=plan, scheduler=self.administrator)

        definition.refresh_from_db()
        self.assertFalse(definition.is_active)
        self.assertEqual(AcademicEventDefinition.objects.count(), 1)
        self.assertEqual(AcademicEventDefinitionChange.objects.count(), 2)

    def test_active_and_inactive_definitions_with_the_same_name_are_ambiguous(self):
        active = create_academic_event_definition(
            base_name="Orientation",
            start_academic_year=2026,
            expected_quarter="Q1",
            scheduler=self.administrator,
        )
        inactive = create_academic_event_definition(
            base_name="Orientation",
            start_academic_year=2026,
            expected_quarter="Q2",
            scheduler=self.administrator,
        )
        deactivate_academic_event_definition(definition=inactive, scheduler=self.administrator)

        plan = self.plan(["Q1", ["Orientation", None, None, "July"]])

        self.assertEqual(plan.create, ())
        self.assertEqual(len(plan.conflicts), 1)
        self.assertEqual(
            (plan.conflicts[0].source.base_name, plan.conflicts[0].source.row_number),
            ("Orientation", 4),
        )
        with self.assertRaises(AcademicEventDefinitionImportError):
            apply_academic_event_definition_import(plan=plan, scheduler=self.administrator)
        self.assertEqual(AcademicEventDefinition.objects.count(), 2)
        self.assertTrue(AcademicEventDefinition.objects.get(pk=active.pk).is_active)
        self.assertFalse(AcademicEventDefinition.objects.get(pk=inactive.pk).is_active)

    def test_malformed_authority_and_atomic_failure_leave_no_partial_definitions(self):
        with self.assertRaisesRegex(
            AcademicEventDefinitionImportError, "not a valid .xlsx archive"
        ):
            load_academic_event_definition_import(
                BytesIO(b"not a workbook"), source_name="bad.xlsx"
            )

        plan = self.plan(["Q1", ["Orientation", None, None, "July"]])
        with self.assertRaises(PermissionDenied):
            apply_academic_event_definition_import(plan=plan, scheduler=self.ordinary)
        self.assertEqual(AcademicEventDefinition.objects.count(), 0)

        atomic_plan = self.plan(
            [
                "Q1",
                ["Orientation", None, None, "July"],
                "Q4",
                ["Too Long", date(2027, 4, 1), date(2027, 7, 1), "Spring"],
            ]
        )
        with self.assertRaisesRegex(ValidationError, "Default duration must fit"):
            apply_academic_event_definition_import(plan=atomic_plan, scheduler=self.administrator)
        self.assertEqual(AcademicEventDefinition.objects.count(), 0)

    def test_parser_rejects_an_unrecognized_quarter_section(self):
        with self.assertRaisesRegex(
            AcademicEventDefinitionImportError, "unrecognized quarter section Q5"
        ):
            self.plan(
                [
                    "Q1",
                    ["Orientation", None, None, "July"],
                    "Q5",
                    ["Grand Rounds", None, None, "Spring"],
                ]
            )

    def test_parser_rejects_conflicting_pipe_delimited_quarter_section_labels(self):
        with self.assertRaisesRegex(
            AcademicEventDefinitionImportError, "names multiple quarter sections"
        ):
            self.plan([["Q1 | July, August, September", "Q2 | October, November, December"]])


class AcademicEventDefinitionImportBrowserTests(TestCase):
    def setUp(self):
        self.administrator = get_user_model().objects.create_user(username="administrator")
        self.administrator.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.ordinary = get_user_model().objects.create_user(username="ordinary")
        self.url = reverse("academic-event-definition-import")

    def upload(self):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "2026-2027"
        worksheet.append(["Academic Year Calendar"])
        worksheet.append(["Event", "Date", "End Date", "Typically Scheduled"])
        worksheet.append(["Q1 | July, August, September"])
        worksheet.append(["Orientation", date(2026, 7, 10), date(2026, 7, 12), "July"])
        worksheet.append(["Faculty Meetings", None, None, "Every Friday"])
        worksheet.append(["Q2 | October, November, December"])
        worksheet.append(["Grand Rounds", None, None, "October"])
        worksheet.append(["Q3 | January, February, March"])
        worksheet.append(["Clinical Skills", None, None, "January"])
        worksheet.append(["Q4 | April, May, June"])
        worksheet.append(["Capstone", None, None, "April"])
        worksheet = workbook.create_sheet("Notes")
        worksheet.append(["Reviewed by scheduling"])
        content = BytesIO()
        workbook.save(content)
        workbook.close()
        return SimpleUploadedFile(
            "Academic Year Calendar 2026-2027.xlsx",
            content.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_admin_previews_then_applies_definition_import_without_reuploading(self):
        self.client.force_login(self.administrator)
        workspace = self.client.get(reverse("academic-event-definition-workspace"))
        preview = self.client.post(self.url, {"workbook": self.upload(), "action": "preview"})
        token = preview.context["preview_token"]
        applied = self.client.post(self.url, {"preview_token": token, "action": "apply"})

        self.assertContains(workspace, f'href="{self.url}?academic_year=')
        orientation = _preview_row(preview, event="Orientation", item_type="Definition")
        self.assertEqual(orientation["Result"], "Create")
        self.assertEqual(orientation["Workbook row"], "4")
        self.assertEqual(orientation["Quarter"], "Q1")
        self.assertEqual(orientation["Default duration"], "3 days")
        self.assertEqual(orientation["Typically scheduled"], "July")
        faculty_rule = _preview_row(preview, event="Faculty Meetings", item_type="Recurrence rule")
        self.assertEqual(faculty_rule["Result"], "Create")
        self.assertEqual(faculty_rule["Workbook row"], "5")
        self.assertEqual(faculty_rule["Quarter"], "All year")
        self.assertContains(preview, "Apply previewed import")
        self.assertContains(applied, "Import applied")
        self.assertNotContains(applied, "Apply previewed import")
        self.assertEqual(AcademicEventDefinition.objects.count(), 5)
        self.assertEqual(AcademicEventMonthlyRule.objects.count(), 1)

        self.client.force_login(self.ordinary)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_preview_and_apply_use_shared_processing_feedback_presentation(self):
        self.client.force_login(self.administrator)

        preview = self.client.post(self.url, {"workbook": self.upload(), "action": "preview"})
        document = _RenderedDocument(preview.content.decode())
        for action, operation_word in (("preview", "workbook"), ("apply", "applying")):
            with self.subTest(action=action):
                form = document.form_with_submission_action(name="action", value=action)
                self.assertIn("data-processing-feedback-form", form.attributes)
                dialogs = [
                    element
                    for element in form.descendants_named("div")
                    if "data-processing-feedback" in element.attributes
                ]
                self.assertEqual(len(dialogs), 1)
                dialog = dialogs[0]
                self.assertEqual(dialog.attributes.get("role"), "dialog")
                self.assertEqual(dialog.attributes.get("aria-modal"), "true")
                self.assertIn(operation_word, dialog.attributes.get("aria-label", "").casefold())

        applied = self.client.post(
            self.url, {"preview_token": preview.context["preview_token"], "action": "apply"}
        )
        self.assertFalse(
            _RenderedDocument(applied.content.decode()).forms_with_submission_action(
                name="action", value="apply"
            )
        )

    def test_replaying_a_definition_preview_is_a_noop(self):
        self.client.force_login(self.administrator)
        preview = self.client.post(self.url, {"workbook": self.upload(), "action": "preview"})
        token = preview.context["preview_token"]

        self.client.post(self.url, {"preview_token": token, "action": "apply"})
        counts = (
            AcademicEventDefinition.objects.count(),
            AcademicEventDefinitionChange.objects.count(),
        )
        replay = self.client.post(self.url, {"preview_token": token, "action": "apply"})

        self.assertEqual(replay.status_code, 200)
        self.assertEqual(
            (
                AcademicEventDefinition.objects.count(),
                AcademicEventDefinitionChange.objects.count(),
            ),
            counts,
        )

    def test_preview_reports_existing_differences_without_an_overwrite_action(self):
        create_academic_event_definition(
            base_name="Orientation",
            start_academic_year=2024,
            expected_quarter="Q2",
            default_period_of_day=AcademicEvent.PeriodOfDay.AM,
            typically_scheduled="Local orientation week",
            scheduler=self.administrator,
        )

        self.client.force_login(self.administrator)
        preview = self.client.post(self.url, {"workbook": self.upload(), "action": "preview"})

        orientation = _preview_row(preview, event="Orientation", item_type="Definition")
        self.assertEqual(orientation["Result"], "Different")
        self.assertEqual(orientation["Workbook row"], "4")
        self.assertEqual(orientation["Quarter"], "Q1")
        self.assertEqual(orientation["Default duration"], "3 days")
        self.assertEqual(orientation["Typically scheduled"], "July")
        detail = orientation["Detail"]
        _assert_reconciliation_claims(
            self,
            detail,
            field="Quarter",
            current_value="Q2",
            imported_value="Q1",
        )
        _assert_reconciliation_claims(
            self,
            detail,
            field="Coverage",
            current_value="AM",
            imported_value="Whole day",
        )

        document = _RenderedDocument(preview.content.decode())
        self.assertEqual(
            len(document.forms_with_submission_action(name="action", value="preview")), 1
        )
        self.assertEqual(
            len(document.forms_with_submission_action(name="action", value="apply")), 1
        )
        self.assertFalse(document.forms_with_submission_action(name="action", value="correct"))
