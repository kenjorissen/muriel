import calendar
import re
from datetime import date
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command
from django.test import TestCase
from openpyxl import Workbook, load_workbook

from schedules.models import Physician, Vacation, VacationChange
from schedules.services.vacation_calendar_import import (
    apply_vacation_calendar_import,
    build_vacation_calendar_import_plan,
    load_vacation_calendar,
)
from schedules.services.vacations import create_vacation

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


def _report_has_line(report, pattern):
    return any(re.search(pattern, line, flags=re.IGNORECASE) for line in report.splitlines())


def _assert_recorded_vacation_value(test_case, recorded_value, start, end, period):
    test_case.assertEqual(recorded_value["start_date"], start.isoformat())
    test_case.assertEqual(recorded_value["end_date"], end.isoformat())
    test_case.assertEqual(recorded_value["period_of_day"], period)


class ImportVacationCalendarCommandTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(username="administrator")
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.physician = Physician.objects.create(display_name="Amy Example")
        self.second_physician = Physician.objects.create(display_name="Blair Example")
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "Vacation Calendar 2026-2027.xlsx"

    def write_workbook(self, markers=None, *, second_faculty="Unknown, Empty", faculty_rows=None):
        markers = markers or {
            date(2026, 7, 1): "V",
            date(2026, 7, 2): "V",
            date(2026, 7, 3): "E",
            date(2026, 7, 4): "I",
            date(2026, 8, 1): "V",
        }
        faculty_rows = faculty_rows or (
            ("Example, Amy", markers),
            (second_faculty, {}),
        )
        workbook = Workbook()
        workbook.remove(workbook.active)
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
            for row, (faculty_name, faculty_markers) in enumerate(faculty_rows, start=6):
                worksheet.cell(row, 2, faculty_name)
                for marker_date, marker in faculty_markers.items():
                    if marker_date.year == year and marker_date.month == month:
                        worksheet.cell(row, marker_date.day + 2, marker)
        metadata = workbook.create_sheet("`Mos.Yr")
        metadata["A1"] = "Months"
        metadata["B1"] = "Years"
        for row, (_, year, month) in enumerate(MONTHS, start=2):
            metadata.cell(row, 1, calendar.month_name[month].upper())
            metadata.cell(row, 2, year)
        workbook.save(self.path)
        workbook.close()

    def command(self, *arguments, **options):
        return call_command(
            "import_vacation_calendar",
            self.path,
            "--scheduler",
            self.scheduler.username,
            *arguments,
            **options,
        )

    def test_preview_reports_source_records_and_apply_is_idempotent(self):
        self.write_workbook(
            faculty_rows=(
                (
                    "Example, Amy",
                    {
                        date(2026, 7, 1): "V",
                        date(2026, 7, 2): "V",
                        date(2026, 7, 3): "E",
                    },
                ),
                (
                    "Example, Blair",
                    {
                        date(2026, 8, 1): "V",
                        date(2026, 8, 2): "I",
                        date(2026, 8, 3): "I",
                    },
                ),
                ("Unknown, Empty", {}),
            )
        )
        data = load_vacation_calendar(self.path)

        self.assertEqual(
            {(item.faculty_key, item.start_date, item.end_date) for item in data.vacations},
            {
                ("amy example", date(2026, 7, 1), date(2026, 7, 2)),
                ("blair example", date(2026, 8, 1), date(2026, 8, 1)),
            },
        )
        self.assertEqual(
            {(item.faculty_key, item.marker): item.dates for item in data.ignored_markers},
            {
                ("amy example", "E"): (date(2026, 7, 3),),
                ("blair example", "I"): (date(2026, 8, 2), date(2026, 8, 3)),
            },
        )

        unaffected_physician = Physician.objects.create(display_name="Unaffected Physician")
        unaffected = create_vacation(
            physician=unaffected_physician,
            start_date=date(2026, 10, 10),
            end_date=date(2026, 10, 10),
            period_of_day=Vacation.PeriodOfDay.PM,
            note="Keep this unrelated record.",
            scheduler=self.scheduler,
        )
        unaffected_state = _vacation_state(vacation_ids=(unaffected.pk,))
        before_preview = _vacation_state()
        preview_output = StringIO()
        self.command(stdout=preview_output)
        report = preview_output.getvalue()

        self.assertEqual(_vacation_state(), before_preview)
        self.assertTrue(_report_has_line(report, r"CREATE.*Amy Example.*2026-07-01.*2026-07-02"))
        self.assertTrue(_report_has_line(report, r"CREATE.*Blair Example.*2026-08-01.*2026-08-01"))
        self.assertTrue(_report_has_line(report, r"IGNORE\s+E.*Amy Example.*\b1\b"))
        self.assertTrue(_report_has_line(report, r"IGNORE\s+I.*Blair Example.*\b2\b"))
        self.assertTrue(
            _report_has_line(report, r"WARNING(?=.*Empty Unknown)(?=.*no vacation markers)")
        )

        self.command("--apply", stdout=StringIO())

        amy_vacation = Vacation.objects.get(physician=self.physician)
        blair_vacation = Vacation.objects.get(physician=self.second_physician)
        self.assertEqual(
            (amy_vacation.start_date, amy_vacation.end_date), (date(2026, 7, 1), date(2026, 7, 2))
        )
        self.assertEqual(amy_vacation.period_of_day, Vacation.PeriodOfDay.WHOLE_DAY)
        self.assertIn(self.path.name, amy_vacation.note)
        self.assertEqual(
            (blair_vacation.start_date, blair_vacation.end_date),
            (date(2026, 8, 1), date(2026, 8, 1)),
        )
        self.assertEqual(blair_vacation.period_of_day, Vacation.PeriodOfDay.WHOLE_DAY)
        self.assertIn(self.path.name, blair_vacation.note)
        amy_change = VacationChange.objects.get(vacation=amy_vacation)
        self.assertEqual(amy_change.changed_by, self.scheduler)
        _assert_recorded_vacation_value(
            self,
            amy_change.recorded_value,
            date(2026, 7, 1),
            date(2026, 7, 2),
            Vacation.PeriodOfDay.WHOLE_DAY,
        )
        self.assertIn(self.path.name, amy_change.recorded_note)
        blair_change = VacationChange.objects.get(vacation=blair_vacation)
        self.assertEqual(blair_change.changed_by, self.scheduler)
        _assert_recorded_vacation_value(
            self,
            blair_change.recorded_value,
            date(2026, 8, 1),
            date(2026, 8, 1),
            Vacation.PeriodOfDay.WHOLE_DAY,
        )
        self.assertIn(self.path.name, blair_change.recorded_note)
        self.assertEqual(_vacation_state(vacation_ids=(unaffected.pk,)), unaffected_state)

        before_reapply = _vacation_state()
        self.command("--apply", stdout=StringIO())
        self.assertEqual(_vacation_state(), before_reapply)

    def test_replace_removes_only_stale_workbook_scoped_vacations_for_matched_faculty(self):
        self.write_workbook()
        self.command("--apply", stdout=StringIO())
        stale = create_vacation(
            physician=self.physician,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 2),
            period_of_day=Vacation.PeriodOfDay.AM,
            scheduler=self.scheduler,
        )
        outside = create_vacation(
            physician=self.physician,
            start_date=date(2027, 7, 1),
            end_date=date(2027, 7, 2),
            period_of_day=Vacation.PeriodOfDay.PM,
            scheduler=self.scheduler,
        )
        unrelated_physician = Physician.objects.create(display_name="Unaffiliated Physician")
        unrelated = create_vacation(
            physician=unrelated_physician,
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 2),
            scheduler=self.scheduler,
        )

        before_blocked_apply = _vacation_state()
        with self.assertRaisesRegex(CommandError, r"(?is)(?=.*Amy Example)(?=.*replace)"):
            self.command("--apply", stdout=StringIO())
        self.assertEqual(_vacation_state(), before_blocked_apply)

        scoped_state = _vacation_state(vacation_ids=(outside.pk, unrelated.pk))
        replace_preview = StringIO()
        self.command("--replace", stdout=replace_preview)
        self.assertEqual(_vacation_state(), before_blocked_apply)
        self.assertTrue(
            _report_has_line(
                replace_preview.getvalue(), r"REMOVE.*Amy Example.*2026-09-01.*2026-09-02.*AM"
            )
        )

        self.command("--replace", "--apply", stdout=StringIO())

        self.assertFalse(Vacation.objects.filter(pk=stale.pk).exists())
        self.assertEqual(_vacation_state(vacation_ids=(outside.pk, unrelated.pk)), scoped_state)
        self.assertEqual(Vacation.objects.filter(physician=self.physician).count(), 3)

    def test_apply_rolls_back_all_created_vacations_when_one_mutation_fails(self):
        self.write_workbook(
            faculty_rows=(
                ("Example, Amy", {date(2026, 7, 1): "V"}),
                ("Example, Blair", {date(2026, 8, 1): "V"}),
            )
        )
        plan = build_vacation_calendar_import_plan(load_vacation_calendar(self.path), replace=False)
        original_create = create_vacation
        preserved_physician = Physician.objects.create(display_name="Preserved Physician")
        create_vacation(
            physician=preserved_physician,
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 1),
            scheduler=self.scheduler,
        )
        before_apply = _vacation_state()

        def create_then_fail_for_blair(**kwargs):
            result = original_create(**kwargs)
            if kwargs["physician"] == self.second_physician:
                raise RuntimeError("Simulated mutation failure")
            return result

        with patch(
            "schedules.services.vacation_calendar_import.create_vacation",
            side_effect=create_then_fail_for_blair,
        ):
            with self.assertRaises(RuntimeError):
                apply_vacation_calendar_import(plan, scheduler=self.scheduler)

        self.assertEqual(_vacation_state(), before_apply)

    def test_unknown_marker_and_unmatched_vacation_physician_block_without_changes(self):
        self.write_workbook({date(2026, 7, 1): "?"})
        with self.assertRaisesRegex(CommandError, r"(?is)(?=.*Faculty marker)(?=.*\?)"):
            self.command("--apply", stdout=StringIO())
        self.assertEqual(Vacation.objects.count(), 0)

        self.write_workbook(second_faculty="Missing, Person")
        edited = load_workbook(self.path)
        edited["JUL"]["D7"] = "V"
        edited.save(self.path)
        edited.close()
        with self.assertRaisesRegex(
            CommandError, r"(?is)(?=.*Person Missing)(?=.*matching physician)"
        ):
            self.command("--apply", stdout=StringIO())
        self.assertEqual(Vacation.objects.count(), 0)

    def test_unauthorized_scheduler_is_rejected(self):
        self.write_workbook()
        ordinary = get_user_model().objects.create_user(username="ordinary")

        with self.assertRaisesRegex(CommandError, r"(?is)(?=.*ordinary)(?=.*authority)"):
            call_command(
                "import_vacation_calendar",
                self.path,
                "--scheduler",
                ordinary.username,
                "--apply",
            )

        self.assertEqual(Vacation.objects.count(), 0)
