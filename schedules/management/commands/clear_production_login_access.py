"""Clear ordinary production login access while retaining application records."""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from schedules.management.production_database import validate_production_database
from schedules.services.accounts import clear_production_login_access, production_recovery_account


class Command(BaseCommand):
    help = "Disable production login access except for one active superuser recovery account."

    def add_arguments(self, parser):
        parser.add_argument("recovery_username", metavar="RECOVERY_USERNAME")
        parser.add_argument("--check-recovery", action="store_true")

    def handle(self, *args, **options):
        validate_production_database("login-access clear")
        try:
            recovery_account = production_recovery_account(options["recovery_username"])
        except ValidationError as error:
            raise CommandError(error.messages[0]) from error
        if options["check_recovery"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Recovery account {recovery_account.get_username()} is an active superuser."
                )
            )
            return
        try:
            clear_production_login_access(recovery_username=options["recovery_username"])
        except ValidationError as error:
            raise CommandError(error.messages[0]) from error
        self.stdout.write(
            self.style.SUCCESS(
                f"Production login access cleared; {recovery_account.get_username()} remains active."
            )
        )
