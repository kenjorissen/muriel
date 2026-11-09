from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from schedules.management.commands.check_production import Command as ProductionReadinessCommand


class Command(BaseCommand):
    help = "Check that the isolated synthetic staging process can safely serve Muriel."

    def handle(self, *args, **options):
        database = settings.DATABASES["default"]
        if (
            getattr(settings, "STAGING_ENVIRONMENT_MARKER", None) != "STAGING — SYNTHETIC DATA"
            or database.get("NAME") != "muriel_staging"
            or database.get("USER") != "muriel_staging"
        ):
            raise CommandError("check_staging requires the isolated staging settings and database")
        ProductionReadinessCommand().handle(*args, **options)
        self.stdout.write(self.style.SUCCESS("Staging readiness check passed."))
