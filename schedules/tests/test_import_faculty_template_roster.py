from datetime import date
from io import StringIO
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command
from django.test import TestCase

from schedules.models import (
    CustomAvailabilityCategory,
    CustomAvailabilityCategoryAssignment,
    CustomAvailabilityCategoryChange,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    Physician,
    PhysicianChange,
)
from schedules.services.availability_categories import (
    create_custom_availability_category,
    set_custom_availability_category_physicians,
)
from schedules.services.physicians import (
    add_role,
    create_faculty_availability_template,
    create_physician,
)

ROOT = Path(__file__).resolve().parents[2]
ROSTER_PATH = ROOT / "data" / "faculty_template_roster.csv"
EFFECTIVE_START = date(2026, 8, 1)
ROUNDS_SELECTION = object()

# This is maintained independently from the CSV parser.  Keeping the slots
# named prevents a column swap from being masked by shared conversion logic.
EXPECTED_ROSTER = {
    "Fake Faculty Doc 01": {
        "monday_am": {"base_state": "off"},
        "monday_pm": {"base_state": "patient_care"},
        "tuesday_am": {"base_state": "open"},
        "tuesday_pm": {"base_state": "open"},
        "wednesday_am": {"base_state": "open"},
        "wednesday_pm": {"base_state": "open"},
        "thursday_am": {"base_state": "open"},
        "thursday_pm": {"base_state": "open"},
        "friday_am": {"base_state": "off"},
        "friday_pm": {"base_state": "open"},
    },
    "Fake Faculty Doc 02": {
        "monday_am": {"base_state": "off"},
        "monday_pm": {"base_state": "off"},
        "tuesday_am": {"base_state": "open"},
        "tuesday_pm": {"base_state": "off"},
        "wednesday_am": {"base_state": "off"},
        "wednesday_pm": {"base_state": "off"},
        "thursday_am": {"base_state": "open"},
        "thursday_pm": {"base_state": "off"},
        "friday_am": {"base_state": "open"},
        "friday_pm": {"base_state": "open"},
    },
    "Fake Faculty Doc 03": {
        "monday_am": {"base_state": "open"},
        "monday_pm": {"base_state": "open"},
        "tuesday_am": {"base_state": "off"},
        "tuesday_pm": {"base_state": "off"},
        "wednesday_am": {"base_state": "patient_care"},
        "wednesday_pm": {"base_state": "off"},
        "thursday_am": {"base_state": "open"},
        "thursday_pm": {"base_state": "patient_care"},
        "friday_am": {"base_state": "open"},
        "friday_pm": {"base_state": "open"},
    },
    "Fake Faculty Doc 04": {
        "monday_am": {"base_state": "off"},
        "monday_pm": {"base_state": "off"},
        "tuesday_am": {"base_state": "patient_care"},
        "tuesday_pm": {"base_state": "open"},
        "wednesday_am": {"base_state": "open"},
        "wednesday_pm": {"base_state": "patient_care"},
        "thursday_am": {"base_state": "open"},
        "thursday_pm": {"base_state": "open"},
        "friday_am": {"base_state": "open"},
        "friday_pm": {"base_state": "patient_care"},
    },
    "Fake Faculty Doc 05": {
        "monday_am": {"base_state": "open"},
        "monday_pm": {"base_state": "open"},
        "tuesday_am": {"base_state": "off"},
        "tuesday_pm": {"base_state": "off"},
        "wednesday_am": {"base_state": "off"},
        "wednesday_pm": {"base_state": "off"},
        "thursday_am": {"base_state": "open"},
        "thursday_pm": {"base_state": "open"},
        "friday_am": {"base_state": "off"},
        "friday_pm": {"base_state": "off"},
    },
    "Fake Faculty Doc 06": {
        "monday_am": {"base_state": "patient_care"},
        "monday_pm": {"base_state": "open"},
        "tuesday_am": {"base_state": "open"},
        "tuesday_pm": {"base_state": "open"},
        "wednesday_am": {"base_state": "patient_care"},
        "wednesday_pm": {"base_state": "open"},
        "thursday_am": {"base_state": "open"},
        "thursday_pm": {"base_state": "patient_care"},
        "friday_am": {"base_state": "patient_care"},
        "friday_pm": {"base_state": "open"},
    },
    "Fake Faculty Doc 07": {
        "monday_am": {"base_state": "open"},
        "monday_pm": {"base_state": "open"},
        "tuesday_am": ROUNDS_SELECTION,
        "tuesday_pm": ROUNDS_SELECTION,
        "wednesday_am": ROUNDS_SELECTION,
        "wednesday_pm": ROUNDS_SELECTION,
        "thursday_am": {"base_state": "open"},
        "thursday_pm": {"base_state": "open"},
        "friday_am": {"base_state": "open"},
        "friday_pm": {"base_state": "open"},
    },
    "Fake Faculty Doc 08": {
        "monday_am": {"base_state": "off"},
        "monday_pm": {"base_state": "off"},
        "tuesday_am": {"base_state": "off"},
        "tuesday_pm": {"base_state": "off"},
        "wednesday_am": {"base_state": "open"},
        "wednesday_pm": {"base_state": "open"},
        "thursday_am": {"base_state": "open"},
        "thursday_pm": {"base_state": "open"},
        "friday_am": {"base_state": "off"},
        "friday_pm": {"base_state": "open"},
    },
    "Fake Faculty Doc 09": {
        "monday_am": {"base_state": "off"},
        "monday_pm": {"base_state": "off"},
        "tuesday_am": {"base_state": "open"},
        "tuesday_pm": {"base_state": "open"},
        "wednesday_am": {"base_state": "open"},
        "wednesday_pm": {"base_state": "open"},
        "thursday_am": {"base_state": "open"},
        "thursday_pm": {"base_state": "open"},
        "friday_am": {"base_state": "open"},
        "friday_pm": {"base_state": "open"},
    },
    "Fake Faculty Doc 10": {
        "monday_am": {"base_state": "off"},
        "monday_pm": {"base_state": "off"},
        "tuesday_am": {"base_state": "open"},
        "tuesday_pm": {"base_state": "open"},
        "wednesday_am": {"base_state": "off"},
        "wednesday_pm": {"base_state": "off"},
        "thursday_am": {"base_state": "off"},
        "thursday_pm": {"base_state": "off"},
        "friday_am": {"base_state": "off"},
        "friday_pm": {"base_state": "off"},
    },
    "Fake Faculty Doc 11": {
        "monday_am": {"base_state": "off"},
        "monday_pm": {"base_state": "off"},
        "tuesday_am": {"base_state": "open"},
        "tuesday_pm": {"base_state": "patient_care"},
        "wednesday_am": {"base_state": "patient_care"},
        "wednesday_pm": {"base_state": "open"},
        "thursday_am": {"base_state": "open"},
        "thursday_pm": {"base_state": "patient_care"},
        "friday_am": {"base_state": "open"},
        "friday_pm": {"base_state": "open"},
    },
    "Fake Faculty Doc 12": {
        "monday_am": {"base_state": "off"},
        "monday_pm": {"base_state": "off"},
        "tuesday_am": {"base_state": "off"},
        "tuesday_pm": {"base_state": "off"},
        "wednesday_am": {"base_state": "open"},
        "wednesday_pm": {"base_state": "open"},
        "thursday_am": {"base_state": "open"},
        "thursday_pm": {"base_state": "open"},
        "friday_am": {"base_state": "off"},
        "friday_pm": {"base_state": "off"},
    },
    "Fake Faculty Doc 13": {
        "monday_am": {"base_state": "patient_care"},
        "monday_pm": {"base_state": "open"},
        "tuesday_am": {"base_state": "off"},
        "tuesday_pm": {"base_state": "open"},
        "wednesday_am": {"base_state": "off"},
        "wednesday_pm": {"base_state": "open"},
        "thursday_am": {"base_state": "open"},
        "thursday_pm": {"base_state": "open"},
        "friday_am": {"base_state": "off"},
        "friday_pm": {"base_state": "off"},
    },
    "Fake Faculty Doc 14": {
        "monday_am": {"base_state": "off"},
        "monday_pm": {"base_state": "off"},
        "tuesday_am": ROUNDS_SELECTION,
        "tuesday_pm": ROUNDS_SELECTION,
        "wednesday_am": {"base_state": "off"},
        "wednesday_pm": {"base_state": "off"},
        "thursday_am": {"base_state": "off"},
        "thursday_pm": {"base_state": "off"},
        "friday_am": {"base_state": "off"},
        "friday_pm": {"base_state": "off"},
    },
    "Fake Faculty Doc 15": {
        "monday_am": {"base_state": "off"},
        "monday_pm": {"base_state": "off"},
        "tuesday_am": {"base_state": "off"},
        "tuesday_pm": {"base_state": "off"},
        "wednesday_am": {"base_state": "open"},
        "wednesday_pm": {"base_state": "open"},
        "thursday_am": {"base_state": "open"},
        "thursday_pm": {"base_state": "off"},
        "friday_am": {"base_state": "off"},
        "friday_pm": {"base_state": "off"},
    },
    "Fake Faculty Doc 16": {
        "monday_am": {"base_state": "off"},
        "monday_pm": {"base_state": "patient_care"},
        "tuesday_am": {"base_state": "patient_care"},
        "tuesday_pm": {"base_state": "patient_care"},
        "wednesday_am": {"base_state": "patient_care"},
        "wednesday_pm": {"base_state": "patient_care"},
        "thursday_am": {"base_state": "open"},
        "thursday_pm": {"base_state": "patient_care"},
        "friday_am": {"base_state": "off"},
        "friday_pm": {"base_state": "off"},
    },
}


class ImportFacultyTemplateRosterCommandTests(TestCase):
    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(username="administrator")
        self.scheduler.groups.add(Group.objects.get(name="Scheduling Administrators"))

    def command(self, *arguments, **options):
        return call_command(
            "import_faculty_template_roster",
            ROSTER_PATH,
            "--effective-start",
            EFFECTIVE_START.isoformat(),
            "--scheduler",
            self.scheduler.username,
            *arguments,
            **options,
        )

    def counts(self):
        return (
            Physician.objects.count(),
            EffectiveRole.objects.count(),
            FacultyAvailabilityTemplate.objects.count(),
            PhysicianChange.objects.count(),
            CustomAvailabilityCategory.objects.exclude(
                system_key=CustomAvailabilityCategory.SystemKey.ADMIN
            ).count(),
            CustomAvailabilityCategoryAssignment.objects.count(),
            CustomAvailabilityCategoryChange.objects.count(),
        )

    def persisted_roster_state(self):
        return {
            "physicians": list(Physician.objects.order_by("pk").values()),
            "roles": list(EffectiveRole.objects.order_by("pk").values()),
            "templates": list(FacultyAvailabilityTemplate.objects.order_by("pk").values()),
            "categories": list(CustomAvailabilityCategory.objects.order_by("pk").values()),
            "assignments": list(
                CustomAvailabilityCategoryAssignment.objects.order_by("pk").values()
            ),
            "physician_changes": list(PhysicianChange.objects.order_by("pk").values()),
            "category_changes": list(
                CustomAvailabilityCategoryChange.objects.order_by("pk").values()
            ),
        }

    def expected_slots(self, roster_slots, rounds):
        return {
            slot: (
                {"category_id": rounds.pk, "category_name": "Rounds"}
                if selection is ROUNDS_SELECTION
                else selection
            )
            for slot, selection in roster_slots.items()
        }

    def test_preview_is_non_mutating_and_apply_imports_every_recorded_slot_idempotently(self):
        output = StringIO()
        self.command(stdout=output)

        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0, 0))
        self.assertIn("CREATE physician: Fake Faculty Doc 01", output.getvalue())

        self.command("--apply", stdout=StringIO())

        self.assertEqual(self.counts(), (16, 16, 16, 48, 1, 2, 3))
        rounds = CustomAvailabilityCategory.objects.get(name="Rounds")
        self.assertEqual(rounds.impact, CustomAvailabilityCategory.Impact.NOT_AVAILABLE)
        self.assertEqual(
            set(
                rounds.assignments.filter(is_active=True).values_list(
                    "physician__display_name", flat=True
                )
            ),
            {"Fake Faculty Doc 07", "Fake Faculty Doc 14"},
        )
        self.assertEqual(
            set(Physician.objects.values_list("display_name", flat=True)), set(EXPECTED_ROSTER)
        )
        for name, expected_template_slots in EXPECTED_ROSTER.items():
            template = FacultyAvailabilityTemplate.objects.get(physician__display_name=name)
            self.assertEqual(template.effective_start, EFFECTIVE_START)
            self.assertIsNone(template.effective_end)
            self.assertEqual(template.slots, self.expected_slots(expected_template_slots, rounds))

        state = self.persisted_roster_state()
        self.command("--apply", stdout=StringIO())
        self.assertEqual(self.persisted_roster_state(), state)

    def test_caller_supplied_roster_uses_its_own_identities_and_attributes_changes(self):
        with NamedTemporaryFile(mode="w", suffix=".csv") as source:
            source.write(
                "Physician,Monday AM,Monday PM,Tuesday AM,Tuesday PM,Wednesday AM,"
                "Wednesday PM,Thursday AM,Thursday PM,Friday AM,Friday PM\n"
                "Example Visiting Faculty,Open,Open,Open,Open,Open,Open,Open,Open,Open,Open\n"
            )
            source.flush()
            call_command(
                "import_faculty_template_roster",
                source.name,
                "--effective-start",
                EFFECTIVE_START.isoformat(),
                "--scheduler",
                self.scheduler.username,
                "--apply",
                stdout=StringIO(),
            )

        physician = Physician.objects.get()
        self.assertEqual(physician.display_name, "Example Visiting Faculty")
        self.assertEqual(physician.roles.get().role, EffectiveRole.Role.FACULTY)
        template = FacultyAvailabilityTemplate.objects.get(physician=physician)
        self.assertEqual(template.effective_start, EFFECTIVE_START)
        self.assertEqual(
            set(PhysicianChange.objects.values_list("changed_by_id", flat=True)),
            {self.scheduler.pk},
        )
        self.assertEqual(
            set(CustomAvailabilityCategoryChange.objects.values_list("changed_by_id", flat=True)),
            {self.scheduler.pk},
        )

    def test_malformed_csv_and_unauthorized_scheduler_leave_the_database_unchanged(self):
        with NamedTemporaryFile(mode="w", suffix=".csv") as source:
            source.write("Physician,Monday AM\nFake Faculty Doc 01,Open\n")
            source.flush()
            state = self.persisted_roster_state()
            with self.assertRaisesRegex(CommandError, "CSV columns"):
                call_command(
                    "import_faculty_template_roster",
                    source.name,
                    "--effective-start",
                    EFFECTIVE_START.isoformat(),
                    "--scheduler",
                    self.scheduler.username,
                    "--apply",
                )
        self.assertEqual(self.persisted_roster_state(), state)

        unauthorized = get_user_model().objects.create_user(username="ordinary-scheduler")
        state = self.persisted_roster_state()
        with self.assertRaisesRegex(CommandError, "Scheduling Administrator"):
            call_command(
                "import_faculty_template_roster",
                ROSTER_PATH,
                "--effective-start",
                EFFECTIVE_START.isoformat(),
                "--scheduler",
                unauthorized.username,
                "--apply",
            )
        self.assertEqual(self.persisted_roster_state(), state)

    def test_effective_start_requires_exact_yyyy_mm_dd_format(self):
        for effective_start in ("20260801", "2026-W31-6"):
            with self.subTest(effective_start=effective_start):
                state = self.persisted_roster_state()
                with self.assertRaisesRegex(CommandError, "must use YYYY-MM-DD"):
                    call_command(
                        "import_faculty_template_roster",
                        ROSTER_PATH,
                        "--effective-start",
                        effective_start,
                        "--scheduler",
                        self.scheduler.username,
                        "--apply",
                    )
                self.assertEqual(self.persisted_roster_state(), state)

    def test_ambiguous_physician_identity_is_a_blocker(self):
        Physician.objects.create(display_name=" Fake Faculty Doc 01 ")
        Physician.objects.create(display_name="fake faculty doc 01")
        state = self.persisted_roster_state()
        with self.assertRaisesRegex(CommandError, "Fake Faculty Doc 01"):
            self.command("--apply", stdout=StringIO())
        self.assertEqual(self.persisted_roster_state(), state)

    def test_conflicting_role_is_a_blocker(self):
        physician = create_physician(
            display_name="Fake Faculty Doc 01", scheduler=self.scheduler, reason="Existing identity"
        )
        add_role(
            physician=physician,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
            reason="Existing role",
        )
        state = self.persisted_roster_state()
        with self.assertRaisesRegex(CommandError, "Fake Faculty Doc 01"):
            self.command("--apply", stdout=StringIO())
        self.assertEqual(self.persisted_roster_state(), state)

    def test_conflicting_template_is_a_blocker(self):
        physician = create_physician(
            display_name="Fake Faculty Doc 01", scheduler=self.scheduler, reason="Existing identity"
        )
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
            reason="Existing role",
        )
        create_faculty_availability_template(
            physician=physician,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            slots={
                slot: FacultyAvailabilityTemplate.AvailabilityState.OPEN
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
            scheduler=self.scheduler,
            reason="Existing template",
        )
        state = self.persisted_roster_state()
        with self.assertRaisesRegex(CommandError, "Fake Faculty Doc 01"):
            self.command("--apply", stdout=StringIO())
        self.assertEqual(self.persisted_roster_state(), state)

    def test_incompatible_rounds_option_is_a_blocker(self):
        create_custom_availability_category(
            name="Rounds",
            impact=CustomAvailabilityCategory.Impact.AVAILABLE,
            scheduler=self.scheduler,
            reason="Existing option",
        )
        state = self.persisted_roster_state()
        with self.assertRaisesRegex(CommandError, "Rounds"):
            self.command("--apply", stdout=StringIO())
        self.assertEqual(self.persisted_roster_state(), state)

    def test_existing_unrelated_rounds_assignments_survive(self):
        rounds = create_custom_availability_category(
            name="Rounds",
            impact=CustomAvailabilityCategory.Impact.NOT_AVAILABLE,
            scheduler=self.scheduler,
            reason="Existing option",
        )
        unrelated = create_physician(
            display_name="Unrelated Faculty", scheduler=self.scheduler, reason="Existing identity"
        )
        add_role(
            physician=unrelated,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2025, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
            reason="Existing role",
        )
        set_custom_availability_category_physicians(
            category=rounds,
            physicians=[unrelated],
            scheduler=self.scheduler,
            reason="Existing assignment",
        )
        self.command("--apply", stdout=StringIO())
        self.assertTrue(rounds.assignments.get(physician=unrelated).is_active)
