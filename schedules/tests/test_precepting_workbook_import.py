import re
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import TestCase
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Color, PatternFill
from openpyxl.writer.theme import theme_xml

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
from schedules.services.precepting_workbook_import import (
    PreceptingWorkbookData,
    PreceptingWorkbookImportError,
    SourceAssignment,
    SourceLane,
    apply_precepting_workbook_import,
    build_precepting_workbook_import_plan,
    load_precepting_workbook,
)

ORANGE = PatternFill("solid", fgColor="FFF4B183")
GREEN = PatternFill("solid", fgColor="FF00B050")
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def assert_blocker(
    test_case,
    blockers,
    *,
    sheet,
    coordinate,
    physician=None,
    condition=None,
    negative_role=None,
    on_date=None,
):
    """Find the one diagnostic for a source location and its failed condition."""
    source = f"{sheet}!{coordinate}"
    source_token = re.compile(rf"(?<![A-Z0-9!]){re.escape(source)}(?![A-Z0-9])")
    physician_token = (
        re.compile(rf"(?<!\w){re.escape(physician)}(?!\w)") if physician is not None else None
    )
    if negative_role:
        if on_date is None:
            raise AssertionError("A role blocker requires its effective date.")
        on_date_token = rf"(?<!\d){re.escape(on_date.isoformat())}(?!\d)"
        role_condition = re.compile(
            rf"(?:not\s+a\s+{re.escape(negative_role)}\s+on\s+{on_date_token}"
            rf"|does\s+not\s+hold\s+(?:a\s+)?{re.escape(negative_role)}\s+on\s+{on_date_token})",
            re.IGNORECASE,
        )
    matching = [
        blocker
        for blocker in blockers
        if source_token.search(blocker)
        and (physician_token is None or physician_token.search(blocker))
        and (condition is None or condition in blocker)
        and (not negative_role or role_condition.search(blocker))
    ]
    test_case.assertEqual(len(matching), 1)


def assert_name_resolution_blocked(test_case, plan, *, name, state):
    resolutions = (*plan.unmatched_names, *plan.ambiguous_names)
    test_case.assertEqual([(item.name, item.state) for item in resolutions], [(name, state)])
    expected_category = (
        "physician identity is ambiguous"
        if state == "ambiguous"
        else "no matching physician exists"
    )
    matching = [
        blocker
        for blocker in plan.blockers
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", blocker) and expected_category in blocker
    ]
    test_case.assertEqual(len(matching), 1)


def assert_ignored_marker(test_case, ignored, *, source, marker):
    source_token = re.compile(rf"(?<![A-Z0-9!]){re.escape(source)}(?![A-Z0-9])")
    matching = [item for item in ignored if source_token.search(item) and marker in item]
    test_case.assertEqual(len(matching), 1)


def import_state_snapshot():
    """Capture all three imported domains and their retained histories for no-op checks."""
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
                "session__date",
                "session__period_of_day",
                "action",
                "previous_value",
                "recorded_value",
                "reason",
                "changed_by_id",
            )
        ),
        "late_doc_history": tuple(
            LateDocAssignmentChange.objects.order_by("pk").values_list(
                "date", "action", "previous_value", "recorded_value", "note", "changed_by_id"
            )
        ),
        "interview_history": tuple(
            InterviewShiftChange.objects.order_by("pk").values_list(
                "shift__date", "action", "previous_value", "recorded_value", "changed_by_id"
            )
        ),
    }


class PreceptingWorkbookImportTests(TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "2026 Precepting Schedule.xlsx"
        self.community = Physician.objects.create(display_name="Community Doctor")
        self.first = Physician.objects.create(display_name="First Faculty")
        self.second = Physician.objects.create(display_name="Second Faculty")
        self.interviewer = Physician.objects.create(display_name="Interview Faculty")
        EffectiveRole.objects.create(
            physician=self.community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2026, 1, 1),
        )
        for physician in (self.first, self.second, self.interviewer):
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
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.scheduler.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )

    def write_workbook(self, *, interviewer_is_green=True, theme_colors=False):
        workbook = Workbook()
        if theme_colors:
            workbook.loaded_theme = theme_xml
        january = workbook.active
        january.title = "JAN"
        for title in MONTHS[1:]:
            workbook.create_sheet(title)
        workbook["SEP"].title = "SEPT"
        for worksheet in workbook.worksheets:
            worksheet["A1"] = "2026 Precepting"
        for column, day in enumerate(range(5, 10), start=1):
            january.cell(3, column, day)
        january["A4"] = "Community Doctor"
        january["A4"].fill = (
            PatternFill("solid", fgColor=Color(theme=9)) if theme_colors else ORANGE
        )
        january["A5"] = "First Faculty"
        january["A6"] = "Second Faculty"
        january["A7"] = "Interview Faculty"
        if interviewer_is_green:
            january["A7"].fill = (
                PatternFill("solid", fgColor=Color(theme=6)) if theme_colors else GREEN
            )
        january["A8"] = "Second Faculty"
        january["A11"] = "First Faculty"
        january["B4"] = "Resident coverage"
        january["C5"] = "First Faculty"
        january["D7"] = "Interview Faculty"
        january["E4"] = "New Years Day"
        january["D7"].fill = PatternFill("solid", fgColor=Color(theme=6)) if theme_colors else GREEN
        helper = workbook.create_sheet("Totals")
        helper["A1"] = "not a calendar year"
        helper["A2"] = "AM"
        helper["B2"] = "Unknown Person"
        workbook.save(self.path)
        workbook.close()

    def session(self, on_date, period):
        period_record, _ = SchedulePeriod.objects.get_or_create(month=on_date.replace(day=1))
        return PreceptorSession.objects.create(
            period=period_record, date=on_date, period_of_day=period
        )

    def plan_for_names(self, *names):
        on_date = date(2026, 1, 5)
        return build_precepting_workbook_import_plan(
            PreceptingWorkbookData(
                "names.xlsx",
                tuple(
                    SourceAssignment(on_date, "AM", "preceptor", name, "JAN", f"A{index}", index, 1)
                    for index, name in enumerate(names, start=1)
                ),
                (SourceLane(on_date, "AM", "AM", "JAN", "A1"),),
                (),
                (),
            )
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

    def write_split_workbook(self, value="Lark/Wren"):
        self.write_workbook()
        workbook = load_workbook(self.path)
        workbook["JAN"]["A4"] = "First Faculty"
        workbook["JAN"]["A4"].fill = PatternFill()
        workbook["JAN"]["A5"] = value
        workbook["JAN"]["A5"].fill = PatternFill()
        workbook["JAN"]["A6"] = None
        workbook.save(self.path)
        workbook.close()

    def write_colored_preceptor_cell(self, sheet, coordinate, on_date, value):
        self.write_workbook()
        workbook = load_workbook(self.path)
        worksheet = workbook[sheet]
        worksheet.cell(30, on_date.weekday() + 1, on_date.day)
        worksheet[coordinate] = value
        worksheet[coordinate].fill = ORANGE
        workbook.save(self.path)
        workbook.close()

    def test_full_name_resolution_including_last_first_form(self):
        physician = Physician.objects.create(display_name="Kira Bramble")

        for name in ("Kira Bramble", "Bramble, Kira"):
            with self.subTest(name=name):
                plan = self.plan_for_names(name)

                self.assertEqual(plan.blockers, ())
                self.assertEqual(plan.unique_names[0].physicians, (physician,))
                self.assertEqual(plan.precepting[0].assignments[0].physician, physician)

    def use_fictional_aliases(self):
        aliases = patch(
            "schedules.services.precepting_workbook_import._PRECEPTING_NAME_ALIASES",
            {"eleanor vale": "nell vale", "rook": "avery rook"},
        )
        aliases.start()
        self.addCleanup(aliases.stop)

    def test_full_name_alias_resolves_normalized_name_in_preview_and_apply(self):
        self.use_fictional_aliases()
        self.first.display_name = "Nell Vale"
        self.first.save()

        plan = self.plan_for_names("  VALE,  eLeAnOr  ")

        self.assertEqual(plan.blockers, ())
        self.assertEqual(plan.unique_names[0].name, "  VALE,  eLeAnOr  ")
        self.assertEqual(plan.unique_names[0].physicians, (self.first,))
        self.assertEqual(plan.precepting[0].assignments[0].physician, self.first)

        apply_precepting_workbook_import(plan=plan, scheduler=self.scheduler)

        self.assertEqual(
            PreceptorAssignment.objects.get(
                session__date=date(2026, 1, 5), session__period_of_day="AM"
            ).physician,
            self.first,
        )

    def test_full_name_alias_is_unmatched_without_its_target(self):
        self.use_fictional_aliases()
        plan = self.plan_for_names("Vale, Eleanor")

        assert_name_resolution_blocked(self, plan, name="Vale, Eleanor", state="unmatched")

    def test_full_name_alias_is_ambiguous_with_duplicate_targets(self):
        self.use_fictional_aliases()
        first = Physician.objects.create(display_name="Nell Vale")
        second = Physician.objects.create(display_name="Nell Vale")

        plan = self.plan_for_names("Vale, Eleanor")

        self.assertEqual(plan.ambiguous_names[0].physicians, (first, second))
        assert_name_resolution_blocked(self, plan, name="Vale, Eleanor", state="ambiguous")

    def test_alias_does_not_enable_nickname_matching_for_other_names(self):
        self.use_fictional_aliases()
        Physician.objects.create(display_name="Nell Other")

        plan = self.plan_for_names("Eleanor Other")

        assert_name_resolution_blocked(self, plan, name="Eleanor Other", state="unmatched")

    def test_one_word_alias_resolves_normalized_name_in_preview_and_apply(self):
        self.use_fictional_aliases()
        self.first.display_name = "Avery Rook"
        self.first.save()
        self.faculty("Robin Rook")

        plan = self.plan_for_names("  ROOK.  ")

        self.assertEqual(plan.blockers, ())
        self.assertEqual(plan.unique_names[0].physicians, (self.first,))
        self.assertEqual(plan.precepting[0].assignments[0].physician, self.first)

        apply_precepting_workbook_import(plan=plan, scheduler=self.scheduler)

        self.assertEqual(
            PreceptorAssignment.objects.get(
                session__date=date(2026, 1, 5), session__period_of_day="AM"
            ).physician,
            self.first,
        )

    def test_alias_target_takes_precedence_over_a_direct_match_and_surname_fallback(self):
        self.use_fictional_aliases()
        self.faculty("Rook")
        self.faculty("Robin Rook")

        absent = self.plan_for_names("Rook")

        assert_name_resolution_blocked(self, absent, name="Rook", state="unmatched")
        before = import_state_snapshot()
        with self.assertRaises(PreceptingWorkbookImportError):
            apply_precepting_workbook_import(plan=absent, scheduler=self.scheduler)
        self.assertEqual(import_state_snapshot(), before)

        first = self.faculty("Avery Rook")
        unique = self.plan_for_names("Rook")
        self.assertEqual(unique.blockers, ())
        self.assertEqual(unique.unique_names[0].physicians, (first,))

        second = self.faculty("Avery Rook")
        duplicate = self.plan_for_names("Rook")

        self.assertEqual(duplicate.ambiguous_names[0].physicians, (first, second))
        assert_name_resolution_blocked(self, duplicate, name="Rook", state="ambiguous")
        with self.assertRaises(PreceptingWorkbookImportError):
            apply_precepting_workbook_import(plan=duplicate, scheduler=self.scheduler)
        self.assertEqual(import_state_snapshot(), before)

    def test_direct_one_word_match_precedes_surname_fallback(self):
        physician = Physician.objects.create(display_name="Rook")
        Physician.objects.create(display_name="Avery Rook")

        plan = self.plan_for_names("Rook")

        self.assertEqual(plan.blockers, ())
        self.assertEqual(plan.unique_names[0].physicians, (physician,))

    def test_unique_one_word_last_name_resolution_normalizes_punctuation_and_case(self):
        physician = Physician.objects.create(display_name="Kira Bramble")

        plan = self.plan_for_names("BRAMBLE.")

        self.assertEqual(plan.blockers, ())
        self.assertEqual(plan.unique_names[0].physicians, (physician,))
        self.assertEqual(plan.precepting[0].assignments[0].physician, physician)

    def test_duplicate_last_name_is_reported_once_as_ambiguous(self):
        Physician.objects.create(display_name="Alex Smith")
        Physician.objects.create(display_name="Taylor Smith")

        plan = self.plan_for_names("Smith", "SMITH.")

        self.assertEqual(len(plan.ambiguous_names), 1)
        self.assertEqual(plan.ambiguous_names[0].name, "Smith")
        assert_name_resolution_blocked(self, plan, name="Smith", state="ambiguous")

    def test_absent_one_word_name_remains_unmatched(self):
        plan = self.plan_for_names("Unknown")

        assert_name_resolution_blocked(self, plan, name="Unknown", state="unmatched")

    def test_multi_word_source_name_does_not_fall_back_to_last_name(self):
        Physician.objects.create(display_name="Kira Bramble")

        plan = self.plan_for_names("Doctor Bramble")

        assert_name_resolution_blocked(self, plan, name="Doctor Bramble", state="unmatched")

    def test_split_preceptor_cell_reserves_secondary_and_applies_idempotently(self):
        lark = self.faculty("Lark")
        wren = self.faculty("Wren")
        self.write_split_workbook()

        plan = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))
        january_am = next(
            item
            for item in plan.precepting
            if (item.on_date, item.period_of_day) == (date(2026, 1, 5), "AM")
        )

        self.assertEqual(plan.blockers, ())
        self.assertEqual(
            [
                (item.physician, item.responsibility, item.primary_position)
                for item in january_am.assignments
            ],
            [
                (self.first, "primary", 1),
                (lark, "primary", 2),
                (wren, "secondary", None),
            ],
        )
        self.assertEqual(
            [(item.name, item.responsibility) for item in plan.data.assignments[:3]],
            [("First Faculty", "primary"), ("Lark", "primary"), ("Wren", "secondary")],
        )

        apply_precepting_workbook_import(plan=plan, scheduler=self.scheduler)
        assignments = list(
            PreceptorAssignment.objects.filter(
                session__date=date(2026, 1, 5), session__period_of_day="AM"
            )
            .order_by("responsibility", "primary_position")
            .values_list("physician_id", "responsibility", "primary_position")
        )
        self.assertEqual(
            assignments,
            [
                (self.first.pk, "primary", 1),
                (lark.pk, "primary", 2),
                (wren.pk, "secondary", None),
            ],
        )
        state_after_apply = import_state_snapshot()
        apply_precepting_workbook_import(
            plan=build_precepting_workbook_import_plan(plan.data), scheduler=self.scheduler
        )
        self.assertEqual(import_state_snapshot(), state_after_apply)

    def test_split_preceptor_cell_blocks_malformed_names_and_matching_failures(self):
        self.write_split_workbook("Lark/")

        malformed = load_precepting_workbook(self.path)

        assert_blocker(
            self,
            malformed.blockers,
            sheet="JAN",
            coordinate="A5",
            condition="slash-separated",
        )

        self.write_split_workbook("Lark/Unknown")
        self.faculty("Lark")
        unmatched = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))

        assert_name_resolution_blocked(self, unmatched, name="Unknown", state="unmatched")
        with self.assertRaises(PreceptingWorkbookImportError):
            apply_precepting_workbook_import(plan=unmatched, scheduler=self.scheduler)
        self.assertFalse(PreceptorAssignment.objects.exists())

    def test_split_preceptor_cell_resolves_alias_and_unique_last_name_independently(self):
        self.use_fictional_aliases()
        alias = self.faculty("Nell Vale")
        last_name = self.faculty("Rowan Finch")
        self.write_split_workbook("Vale, Eleanor/FINCH.")

        plan = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))
        january_am = next(
            item
            for item in plan.precepting
            if (item.on_date, item.period_of_day) == (date(2026, 1, 5), "AM")
        )

        self.assertEqual(plan.blockers, ())
        resolved = {item.name: item.physicians for item in plan.unique_names}
        self.assertEqual(resolved["Vale, Eleanor"], (alias,))
        self.assertEqual(resolved["FINCH."], (last_name,))
        self.assertEqual(
            [(item.physician, item.responsibility) for item in january_am.assignments],
            [(self.first, "primary"), (alias, "primary"), (last_name, "secondary")],
        )

        apply_precepting_workbook_import(plan=plan, scheduler=self.scheduler)
        session = PreceptorSession.objects.get(date=date(2026, 1, 5), period_of_day="AM")
        self.assertEqual(
            set(
                session.assignments.values_list(
                    "physician_id", "responsibility", "primary_position"
                )
            ),
            {
                (self.first.pk, "primary", 1),
                (alias.pk, "primary", 2),
                (last_name.pk, "secondary", None),
            },
        )
        changes = PreceptorAssignmentChange.objects.filter(session=session)
        self.assertEqual(
            {(change.recorded_value["physician_id"], change.changed_by_id) for change in changes},
            {(physician.pk, self.scheduler.pk) for physician in (self.first, alias, last_name)},
        )
        state_after_apply = import_state_snapshot()
        apply_precepting_workbook_import(
            plan=build_precepting_workbook_import_plan(plan.data), scheduler=self.scheduler
        )
        self.assertEqual(import_state_snapshot(), state_after_apply)

    def test_split_preceptor_cell_allows_an_authoritative_nonfaculty_backup(self):
        self.faculty("Lark")
        self.faculty("Wren")
        self.write_split_workbook("Lark/Community Doctor")

        plan = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))

        self.assertEqual(plan.blockers, ())
        apply_precepting_workbook_import(plan=plan, scheduler=self.scheduler)
        self.assertEqual(
            PreceptorAssignment.objects.get(
                session__date=date(2026, 1, 5),
                session__period_of_day="AM",
                responsibility="secondary",
            ).physician,
            self.community,
        )

    def test_community_split_secondary_requires_community_preceptor_role(self):
        self.write_workbook()
        workbook = load_workbook(self.path)
        workbook["JAN"]["A4"] = "First Faculty"
        workbook["JAN"]["A4"].fill = PatternFill()
        workbook["JAN"]["A5"] = "Community Doctor/Second Faculty"
        workbook["JAN"]["A5"].fill = ORANGE
        workbook["JAN"]["A6"] = None
        workbook.save(self.path)
        workbook.close()

        plan = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))

        assert_blocker(
            self,
            plan.blockers,
            sheet="JAN",
            coordinate="A5",
            physician="Second Faculty",
            negative_role="Community Preceptor",
            on_date=date(2026, 1, 5),
        )

    def test_split_preceptor_cell_blocks_duplicate_and_slot_conflicts(self):
        self.faculty("Lark")
        self.faculty("Wren")
        self.write_split_workbook("Lark/Lark")
        duplicate = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))

        assert_blocker(
            self,
            duplicate.blockers,
            sheet="JAN",
            coordinate="A3",
            condition="appears more than once",
        )

        self.write_split_workbook()
        workbook = load_workbook(self.path)
        workbook["JAN"]["A4"] = "First Faculty/Lark"
        workbook["JAN"]["A5"] = "Wren/Second Faculty"
        workbook.save(self.path)
        workbook.close()
        multiple_backups = build_precepting_workbook_import_plan(
            load_precepting_workbook(self.path)
        )

        assert_blocker(
            self,
            multiple_backups.blockers,
            sheet="JAN",
            coordinate="A3",
            condition="more than one split backup",
        )

        self.write_split_workbook()
        workbook = load_workbook(self.path)
        workbook["JAN"]["A6"] = "Second Faculty"
        workbook.save(self.path)
        workbook.close()
        capacity = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))

        assert_blocker(
            self,
            capacity.blockers,
            sheet="JAN",
            coordinate="A3",
            condition="secondary position",
        )
        with self.assertRaises(PreceptingWorkbookImportError):
            apply_precepting_workbook_import(plan=capacity, scheduler=self.scheduler)
        self.assertFalse(PreceptorAssignment.objects.exists())

    def test_authoritative_import_applies_ineligible_computed_backup_late_doc_and_interviewer(self):
        preceptor = Physician.objects.create(display_name="Authoritative Preceptor")
        backup = Physician.objects.create(display_name="Authoritative Backup")
        late_doc = Physician.objects.create(display_name="Authoritative Late Doc")
        interviewer = Physician.objects.create(display_name="Authoritative Interviewer")
        self.write_workbook()
        workbook = load_workbook(self.path)
        workbook["JAN"]["B4"] = preceptor.display_name
        workbook["JAN"]["A6"] = backup.display_name
        workbook["JAN"]["A7"] = interviewer.display_name
        workbook["JAN"]["A11"] = late_doc.display_name
        workbook.save(self.path)
        workbook.close()

        plan = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))

        january_am = next(
            item
            for item in plan.precepting
            if (item.on_date, item.period_of_day) == (date(2026, 1, 5), "AM")
        )
        self.assertEqual(plan.blockers, ())
        self.assertEqual(
            [
                (item.physician, item.responsibility, item.primary_position)
                for item in january_am.assignments
            ],
            [
                (self.community, "primary", 1),
                (self.first, "primary", 2),
                (backup, "secondary", None),
            ],
        )

        apply_precepting_workbook_import(plan=plan, scheduler=self.scheduler)

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
        self.assertEqual(LateDocAssignment.objects.get(date=date(2026, 1, 5)).physician, late_doc)
        self.assertEqual(
            InterviewShift.objects.get(date=date(2026, 1, 5), is_active=True).physician,
            interviewer,
        )

    def test_jun_d36_imports_an_ordinary_preceptor_and_applies_idempotently(self):
        thistle = self.faculty("Thistle")
        self.write_colored_preceptor_cell("JUN", "D36", date(2026, 6, 25), "Thistle")

        plan = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))
        june_pm = next(
            item
            for item in plan.precepting
            if (item.on_date, item.period_of_day) == (date(2026, 6, 25), "PM")
        )

        self.assertEqual(plan.blockers, ())
        self.assertEqual(
            [(item.physician, item.kind) for item in june_pm.assignments], [(thistle, "preceptor")]
        )

        apply_precepting_workbook_import(plan=plan, scheduler=self.scheduler)
        state_after_apply = import_state_snapshot()
        apply_precepting_workbook_import(
            plan=build_precepting_workbook_import_plan(plan.data), scheduler=self.scheduler
        )
        self.assertEqual(import_state_snapshot(), state_after_apply)

    def test_jun_d36_correction_does_not_apply_to_neighboring_cells_or_other_sheets(self):
        self.faculty("Thistle")
        for sheet, coordinate, on_date in (
            ("JUN", "C36", date(2026, 6, 24)),
            ("JUL", "D36", date(2026, 7, 30)),
        ):
            with self.subTest(sheet=sheet, coordinate=coordinate):
                self.write_colored_preceptor_cell(sheet, coordinate, on_date, "Thistle")

                plan = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))
                source = next(
                    item
                    for item in plan.data.assignments
                    if (item.sheet, item.coordinate) == (sheet, coordinate)
                )

                self.assertEqual(source.kind, "community")
                assert_blocker(
                    self,
                    plan.blockers,
                    sheet=sheet,
                    coordinate=coordinate,
                    physician="Thistle",
                    negative_role="Community Preceptor",
                    on_date=on_date,
                )

    def test_jun_d36_retains_preceptor_cell_validation_before_the_correction(self):
        self.write_colored_preceptor_cell("JUN", "D36", date(2026, 6, 25), "Thistle/")

        data = load_precepting_workbook(self.path)

        assert_blocker(
            self,
            data.blockers,
            sheet="JUN",
            coordinate="D36",
            condition="slash-separated",
        )

    def test_preview_reads_fixed_week_blocks_without_mutating(self):
        self.write_workbook()
        am = self.session(date(2026, 1, 5), "AM")
        for physician, responsibility, position in (
            (self.community, "primary", 1),
            (self.first, "primary", 2),
            (self.second, "secondary", None),
        ):
            PreceptorAssignment.objects.create(
                session=am,
                physician=physician,
                responsibility=responsibility,
                primary_position=position,
            )
        pm = self.session(date(2026, 1, 5), "PM")
        PreceptorAssignment.objects.create(
            session=pm,
            physician=self.first,
            responsibility="primary",
            primary_position=1,
        )
        removed = self.session(date(2026, 1, 6), "AM")
        PreceptorAssignment.objects.create(
            session=removed,
            physician=self.first,
            responsibility="primary",
            primary_position=1,
        )
        LateDocAssignment.objects.create(date=date(2026, 1, 5), physician=self.second)
        InterviewShift.objects.create(date=date(2026, 1, 5), physician=self.first)
        InterviewShift.objects.create(date=date(2026, 1, 6), physician=self.first)

        state_before_preview = import_state_snapshot()
        data = load_precepting_workbook(self.path)
        plan = build_precepting_workbook_import_plan(data)

        january_am = next(
            item
            for item in plan.precepting
            if (item.on_date, item.period_of_day) == (date(2026, 1, 5), "AM")
        )
        self.assertEqual(january_am.state, "exact")
        self.assertEqual(
            [(item.physician, item.kind) for item in january_am.assignments],
            [(self.community, "community"), (self.first, "preceptor"), (self.second, "preceptor")],
        )
        self.assertEqual(len(january_am.assignments), 3)
        january_7_am = next(
            item
            for item in plan.precepting
            if (item.on_date, item.period_of_day) == (date(2026, 1, 7), "AM")
        )
        self.assertEqual(
            [(item.physician, item.primary_position) for item in january_7_am.assignments],
            [(self.first, 1)],
        )
        self.assertEqual(
            {item.state for item in plan.precepting}, {"exact", "new", "corrected", "removed"}
        )
        self.assertEqual(
            {item.state for item in plan.interviews}, {"exact", "new", "corrected", "removed"}
        )
        self.assertEqual(plan.late_docs[0].state, "corrected")
        self.assertEqual(plan.blockers, ())
        assert_ignored_marker(self, data.ignored, source="JAN!B4", marker="Resident coverage")
        assert_ignored_marker(self, data.ignored, source="JAN!E4", marker="New Years Day")
        self.assertEqual(import_state_snapshot(), state_before_preview)

    def test_ambiguous_physician_and_role_inconsistency_are_blockers(self):
        self.write_workbook()
        Physician.objects.create(display_name="First Faculty")

        plan = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))

        assert_name_resolution_blocked(self, plan, name="First Faculty", state="ambiguous")

        self.community.roles.all().delete()
        role_inconsistent = build_precepting_workbook_import_plan(
            load_precepting_workbook(self.path)
        )

        assert_blocker(
            self,
            role_inconsistent.blockers,
            sheet="JAN",
            coordinate="A4",
            physician="Community Doctor",
            negative_role="Community Preceptor",
            on_date=date(2026, 1, 5),
        )

    def test_uncolored_fourth_slots_and_out_of_grid_cells_are_ignored(self):
        self.write_workbook(interviewer_is_green=False)
        workbook = load_workbook(self.path)
        workbook["JAN"]["E11"] = "Unexpected Faculty"
        workbook["JAN"]["F4"] = "Out of grid helper data"
        workbook.save(self.path)
        workbook.close()

        data = load_precepting_workbook(self.path)
        plan = build_precepting_workbook_import_plan(data)

        assert_ignored_marker(self, data.ignored, source="JAN!A7", marker="Interview Faculty")
        assert_ignored_marker(self, data.ignored, source="JAN!E11", marker="Unexpected Faculty")
        self.assertEqual(plan.blockers, ())

    def test_community_styled_fourth_slots_are_blockers(self):
        self.write_workbook(interviewer_is_green=False)
        workbook = load_workbook(self.path)
        workbook["JAN"]["A7"].fill = ORANGE
        workbook["JAN"]["A11"].fill = ORANGE
        workbook["JAN"]["E11"] = "Community Doctor"
        workbook["JAN"]["E11"].fill = ORANGE
        workbook.save(self.path)
        workbook.close()

        plan = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))

        for coordinate in ("A7", "A11", "E11"):
            with self.subTest(coordinate=coordinate):
                assert_blocker(
                    self,
                    plan.blockers,
                    sheet="JAN",
                    coordinate=coordinate,
                    condition="fourth cell",
                )

    def test_community_preceptor_in_each_source_row_is_ordered_before_faculty(self):
        self.write_workbook()
        faculty_by_cell = {"A4": self.first, "A5": self.second, "A6": self.first}
        for coordinate in ("A4", "A5", "A6"):
            with self.subTest(coordinate=coordinate):
                workbook = load_workbook(self.path)
                for cell, physician in faculty_by_cell.items():
                    workbook["JAN"][cell] = physician.display_name
                    workbook["JAN"][cell].fill = PatternFill()
                workbook["JAN"][coordinate] = "Community Doctor"
                workbook["JAN"][coordinate].fill = ORANGE
                workbook.save(self.path)
                workbook.close()

                plan = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))
                assignments = next(
                    item.assignments
                    for item in plan.precepting
                    if (item.on_date, item.period_of_day) == (date(2026, 1, 5), "AM")
                )
                ordinary_physicians = tuple(
                    physician for cell, physician in faculty_by_cell.items() if cell != coordinate
                )

                self.assertEqual(plan.blockers, ())
                self.assertEqual(
                    [
                        (item.physician, item.responsibility, item.primary_position)
                        for item in assignments
                    ],
                    [
                        (self.community, "primary", 1),
                        (ordinary_physicians[0], "primary", 2),
                        (ordinary_physicians[1], "secondary", None),
                    ],
                )

    def test_two_community_preceptors_keep_workbook_order_and_faculty_is_secondary(self):
        second_community = Physician.objects.create(display_name="Second Community Doctor")
        EffectiveRole.objects.create(
            physician=second_community,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2026, 1, 1),
        )
        self.write_workbook()
        workbook = load_workbook(self.path)
        workbook["JAN"]["A4"] = "First Faculty"
        workbook["JAN"]["A4"].fill = PatternFill()
        workbook["JAN"]["A5"] = "Community Doctor"
        workbook["JAN"]["A5"].fill = ORANGE
        workbook["JAN"]["A6"] = "Second Community Doctor"
        workbook["JAN"]["A6"].fill = ORANGE
        workbook.save(self.path)
        workbook.close()

        plan = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))
        assignments = next(
            item.assignments
            for item in plan.precepting
            if (item.on_date, item.period_of_day) == (date(2026, 1, 5), "AM")
        )

        self.assertEqual(plan.blockers, ())
        self.assertEqual(
            [(item.physician, item.responsibility, item.primary_position) for item in assignments],
            [
                (self.community, "primary", 1),
                (second_community, "primary", 2),
                (self.first, "secondary", None),
            ],
        )

    def test_third_community_preceptor_is_a_blocker(self):
        second_community = Physician.objects.create(display_name="Second Community Doctor")
        third_community = Physician.objects.create(display_name="Third Community Doctor")
        for physician in (second_community, third_community):
            EffectiveRole.objects.create(
                physician=physician,
                role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
                effective_start=date(2026, 1, 1),
            )
        self.write_workbook()
        workbook = load_workbook(self.path)
        workbook["JAN"]["A5"] = "Second Community Doctor"
        workbook["JAN"]["A5"].fill = ORANGE
        workbook["JAN"]["A6"] = "Third Community Doctor"
        workbook["JAN"]["A6"].fill = ORANGE
        workbook.save(self.path)
        workbook.close()

        plan = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))

        assert_blocker(
            self,
            plan.blockers,
            sheet="JAN",
            coordinate="A3",
            condition="more than two Community Preceptors",
        )

    def test_green_pm_fourth_slot_is_a_blocker(self):
        self.write_workbook()
        workbook = load_workbook(self.path)
        workbook["JAN"]["E11"] = "Interview Faculty"
        workbook["JAN"]["E11"].fill = GREEN
        workbook.save(self.path)
        workbook.close()

        plan = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))

        assert_blocker(
            self,
            plan.blockers,
            sheet="JAN",
            coordinate="E11",
            condition="PM fourth cell",
        )

    def test_preview_resolves_theme_based_community_and_interviewer_colors(self):
        self.write_workbook(theme_colors=True)

        plan = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))

        january_am = next(
            item
            for item in plan.precepting
            if (item.on_date, item.period_of_day) == (date(2026, 1, 5), "AM")
        )
        self.assertEqual(
            [(item.physician, item.kind) for item in january_am.assignments],
            [(self.community, "community"), (self.first, "preceptor"), (self.second, "preceptor")],
        )
        self.assertEqual(
            [item.physician for item in plan.interviews if item.on_date == date(2026, 1, 5)],
            [self.interviewer],
        )
        self.assertEqual(plan.blockers, ())

    def test_apply_reconciles_drafts_and_retains_history_idempotently(self):
        self.write_workbook()
        workbook = load_workbook(self.path)
        workbook["JAN"]["A4"] = "First Faculty"
        workbook["JAN"]["A4"].fill = PatternFill()
        workbook["JAN"]["A5"] = "Community Doctor"
        workbook["JAN"]["A5"].fill = ORANGE
        workbook["JAN"]["A7"] = None
        workbook["JAN"]["B7"] = "Interview Faculty"
        workbook["JAN"]["B7"].fill = GREEN
        workbook.save(self.path)
        workbook.close()

        january_5_am = self.session(date(2026, 1, 5), "AM")
        PreceptorAssignment.objects.create(
            session=january_5_am,
            physician=self.first,
            responsibility="primary",
            primary_position=1,
        )
        january_5_pm = self.session(date(2026, 1, 5), "PM")
        PreceptorAssignment.objects.create(
            session=january_5_pm,
            physician=self.first,
            responsibility="primary",
            primary_position=1,
        )
        january_6_am = self.session(date(2026, 1, 6), "AM")
        PreceptorAssignment.objects.create(
            session=january_6_am,
            physician=self.first,
            responsibility="primary",
            primary_position=1,
        )
        LateDocAssignment.objects.create(date=date(2026, 1, 5), physician=self.second)
        removed_interview = InterviewShift.objects.create(
            date=date(2026, 1, 5), physician=self.first
        )
        corrected_interview = InterviewShift.objects.create(
            date=date(2026, 1, 8), physician=self.first
        )

        plan = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))
        applied = apply_precepting_workbook_import(
            plan=plan, scheduler=self.scheduler, note="calendar import"
        )

        self.assertEqual(applied.blockers, ())
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
                (self.first.pk, "primary", 2),
                (self.second.pk, "secondary", None),
            ],
        )
        self.assertFalse(PreceptorAssignment.objects.filter(session=january_6_am).exists())
        self.assertEqual(LateDocAssignment.objects.get(date=date(2026, 1, 5)).physician, self.first)
        removed_interview.refresh_from_db()
        corrected_interview.refresh_from_db()
        self.assertFalse(removed_interview.is_active)
        self.assertEqual(corrected_interview.physician, self.interviewer)
        self.assertEqual(
            InterviewShift.objects.get(date=date(2026, 1, 6), is_active=True).physician,
            self.interviewer,
        )
        self.assertTrue(
            PreceptorSession.objects.filter(date=date(2026, 1, 9), period_of_day="PM").exists()
        )
        precepting_changes = tuple(PreceptorAssignmentChange.objects.all())
        late_doc_changes = tuple(LateDocAssignmentChange.objects.all())
        interview_changes = tuple(InterviewShiftChange.objects.all())
        self.assertTrue(
            all(
                change.changed_by == self.scheduler
                for change in (
                    *precepting_changes,
                    *late_doc_changes,
                    *interview_changes,
                )
            )
        )
        self.assertTrue(all("calendar import" in change.reason for change in precepting_changes))
        self.assertTrue(
            any(
                change.action == "removed"
                and change.session.date == date(2026, 1, 6)
                and change.session.period_of_day == "AM"
                and change.previous_value["physician_id"] == self.first.pk
                and change.previous_value["responsibility"] == "primary"
                and change.previous_value["primary_position"] == 1
                and change.recorded_value is None
                for change in precepting_changes
            )
        )
        self.assertTrue(
            any(
                change.action == "assigned"
                and change.session.date == date(2026, 1, 5)
                and change.session.period_of_day == "AM"
                and change.recorded_value["physician_id"] == self.second.pk
                and change.recorded_value["responsibility"] == "secondary"
                and change.recorded_value["primary_position"] is None
                for change in precepting_changes
            )
        )
        self.assertTrue(
            any(
                change.date == date(2026, 1, 5)
                and change.previous_value["physician_id"] == self.second.pk
                and change.recorded_value["physician_id"] == self.first.pk
                and "calendar import" in change.note
                for change in late_doc_changes
            )
        )
        self.assertTrue(
            any(
                change.action == "removed"
                and change.shift.date == date(2026, 1, 5)
                and change.previous_value["physician_id"] == self.first.pk
                and change.recorded_value["is_active"] is False
                for change in interview_changes
            )
        )
        self.assertTrue(
            any(
                change.action == "assigned"
                and change.shift.date == date(2026, 1, 6)
                and change.previous_value["physician_id"] is None
                and change.recorded_value
                and change.recorded_value["physician_id"] == self.interviewer.pk
                for change in interview_changes
            )
        )
        self.assertTrue(
            any(
                change.action == "assigned"
                and change.shift.date == date(2026, 1, 8)
                and change.previous_value["physician_id"] == self.first.pk
                and change.recorded_value["physician_id"] == self.interviewer.pk
                for change in interview_changes
            )
        )

        state_after_apply = import_state_snapshot()
        apply_precepting_workbook_import(
            plan=build_precepting_workbook_import_plan(plan.data), scheduler=self.scheduler
        )
        self.assertEqual(import_state_snapshot(), state_after_apply)

    def test_apply_rejects_stale_preview_and_rolls_back_domain_failures(self):
        self.write_workbook()
        workbook = load_workbook(self.path)
        workbook["JAN"]["A7"] = None
        workbook.save(self.path)
        workbook.close()
        plan = build_precepting_workbook_import_plan(load_precepting_workbook(self.path))
        stale_session = self.session(date(2026, 1, 5), "AM")
        PreceptorAssignment.objects.create(
            session=stale_session,
            physician=self.first,
            responsibility="primary",
            primary_position=1,
        )
        state_before_stale_apply = import_state_snapshot()

        with self.assertRaises(PreceptingWorkbookImportError):
            apply_precepting_workbook_import(plan=plan, scheduler=self.scheduler)
        self.assertEqual(import_state_snapshot(), state_before_stale_apply)

        PreceptorAssignment.objects.filter(session=stale_session).delete()
        fresh_plan = build_precepting_workbook_import_plan(plan.data)
        state_before_failed_apply = import_state_snapshot()
        with patch(
            "schedules.services.precepting_workbook_import.assign_interview_shift",
            side_effect=ValidationError("interview failure"),
        ):
            with self.assertRaises(ValidationError):
                apply_precepting_workbook_import(plan=fresh_plan, scheduler=self.scheduler)

        self.assertEqual(import_state_snapshot(), state_before_failed_apply)
