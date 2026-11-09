"""Safely replace the canonical local SQLite scheduling domain with the synthetic scenario."""

from pathlib import Path
from time import perf_counter

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from schedules.management.commands.reset_demo import _canonical_main_worktree
from schedules.management.commands.reset_production_domain import (
    SYNTHETIC_DEFAULT_SEED,
    _select_actor,
    replace_domain,
)


def _local_database_path():
    if not settings.DEBUG:
        raise CommandError("Synthetic reset is available only while Django is in development mode.")
    database = settings.DATABASES["default"]
    if database["ENGINE"] != "django.db.backends.sqlite3":
        raise CommandError(
            "Synthetic reset only accepts the configured SQLite development database."
        )
    base_dir = Path(settings.BASE_DIR).resolve()
    expected_path = (base_dir / "db.sqlite3").resolve()
    if Path(database["NAME"]).resolve() != expected_path:
        raise CommandError(
            "Synthetic reset only accepts db.sqlite3 in the canonical main worktree."
        )
    if _canonical_main_worktree(base_dir) != base_dir:
        raise CommandError("Synthetic reset must be run from the canonical main worktree.")
    return expected_path


class Command(BaseCommand):
    help = "Replace the canonical local SQLite scheduling domain with the deterministic synthetic scenario."

    def add_arguments(self, parser):
        parser.add_argument("--confirm", action="store_true")
        parser.add_argument("--actor", metavar="USERNAME")
        parser.add_argument("--seed", default=SYNTHETIC_DEFAULT_SEED)

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError(
                "Refusing to replace the scheduling domain. Django logins, groups, permissions, "
                "and credentials will be preserved, but all scheduling records will be replaced. "
                "Re-run with --confirm."
            )
        _local_database_path()
        actor = _select_actor(options["actor"])
        started = perf_counter()
        synthetic_summary = replace_domain(
            mode="synthetic",
            actor=actor,
            seed=options["seed"],
        )
        if synthetic_summary is None:
            raise CommandError("Synthetic reset did not produce a validation summary.")
        counts, historical_assignments, completion_end = synthetic_summary
        self.stdout.write(
            "Synthetic scenario: "
            + ", ".join(f"{label}={count}" for label, count in counts.items())
        )
        self.stdout.write(
            f"Historical validation: {historical_assignments} assignments through "
            f"{completion_end:%Y-%m}."
        )
        self.stdout.write(
            f"Work remaining: Precepting is complete through {completion_end:%Y-%m}; "
            "later attention-visible work remains open."
        )
        if connection.vendor == "sqlite":
            self.stdout.write(f"Database size: {_local_database_path().stat().st_size} bytes.")
        self.stdout.write(f"Elapsed: {perf_counter() - started:.1f}s.")
        self.stdout.write(
            self.style.SUCCESS("Local scheduling domain replaced with synthetic data.")
        )
