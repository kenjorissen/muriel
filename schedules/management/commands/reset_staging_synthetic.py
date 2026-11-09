"""Replace only isolated staging scheduling data with the synthetic scenario."""

from time import perf_counter

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from schedules.management.commands.reset_production_domain import (
    SYNTHETIC_DEFAULT_SEED,
    replace_domain,
)

STAGING_AUDIT_USERNAME = "staging-synthetic-audit"


def _staging_audit_actor():
    """Return the non-login account that owns synthetic staging audit history."""
    user_model = get_user_model()
    actor, _ = user_model.objects.get_or_create(
        **{user_model.USERNAME_FIELD: STAGING_AUDIT_USERNAME}
    )
    actor.is_active = True
    actor.is_staff = False
    actor.is_superuser = True
    actor.email = ""
    actor.set_unusable_password()
    actor.save()
    return actor


class Command(BaseCommand):
    help = "Replace isolated staging scheduling data with the deterministic synthetic scenario."

    def add_arguments(self, parser):
        parser.add_argument("--confirm", action="store_true")
        parser.add_argument("--seed", default=SYNTHETIC_DEFAULT_SEED)

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError(
                "Refusing to replace staging scheduling data. Re-run with --confirm."
            )
        database = settings.DATABASES["default"]
        if (
            getattr(settings, "STAGING_ENVIRONMENT_MARKER", None) != "STAGING — SYNTHETIC DATA"
            or database.get("NAME") != "muriel_staging"
            or database.get("USER") != "muriel_staging"
        ):
            raise CommandError(
                "Synthetic load requires the isolated staging settings and database."
            )
        actor = _staging_audit_actor()
        started = perf_counter()
        synthetic_summary = replace_domain(
            mode="synthetic",
            actor=actor,
            seed=options["seed"],
        )
        if synthetic_summary is None:
            raise CommandError("Synthetic load did not produce a validation summary.")
        counts, historical_assignments, completion_end = synthetic_summary
        self.stdout.write(
            "Synthetic scenario: "
            + ", ".join(f"{label}={count}" for label, count in counts.items())
        )
        self.stdout.write(
            f"Historical validation: {historical_assignments} assignments through "
            f"{completion_end:%Y-%m}."
        )
        self.stdout.write(f"Elapsed: {perf_counter() - started:.1f}s.")
        self.stdout.write(
            self.style.SUCCESS("Staging scheduling domain replaced with synthetic data.")
        )
