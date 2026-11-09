from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

REQUIRED_STATIC_ASSETS = (
    "schedules/application.css",
    "schedules/images/random-health.png",
    "schedules/calendar-period-picker.js",
    "schedules/calendar-week-actions.js",
    "schedules/calendar-day-actions.js",
)


class Command(BaseCommand):
    help = "Check that the local production process can safely serve Muriel."

    def handle(self, *args, **options):
        if settings.DEBUG:
            raise CommandError("check_production requires production settings with DEBUG disabled")

        call_command("check", "--deploy")
        connection.ensure_connection()
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        if executor.migration_plan(targets):
            raise CommandError("Pending migrations must be applied before starting production")

        static_root = Path(settings.STATIC_ROOT)
        missing_assets = []
        for asset in REQUIRED_STATIC_ASSETS:
            try:
                versioned_asset = staticfiles_storage.stored_name(asset)
            except ValueError:
                missing_assets.append(asset)
                continue
            if not (static_root / versioned_asset).is_file():
                missing_assets.append(asset)
        if missing_assets:
            raise CommandError("Collected static assets are missing: " + ", ".join(missing_assets))

        self.stdout.write(self.style.SUCCESS("Production readiness check passed."))
