"""Production database target validation for destructive management commands."""

from django.conf import settings
from django.core.management.base import CommandError
from django.db import connection


def validate_production_database(operation):
    """Refuse a destructive operation unless it is connected to Muriel production."""
    database = settings.DATABASES["default"]
    if settings.DEBUG:
        raise CommandError(f"Production {operation} refuses development settings.")
    if database.get("ENGINE") != "django.db.backends.postgresql":
        raise CommandError(f"Production {operation} requires PostgreSQL.")
    if database.get("NAME") != "muriel":
        raise CommandError(f"Production {operation} requires the local muriel database.")
    if database.get("HOST") != "/var/run/postgresql" or str(database.get("PORT")) != "5432":
        raise CommandError(f"Production {operation} requires the PostgreSQL 18/main local socket.")

    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_user, version()")
        database_name, database_user, version = cursor.fetchone()
    if (
        database_name != "muriel"
        or database_user != "muriel"
        or not str(version).startswith("PostgreSQL 18.")
    ):
        raise CommandError(f"Production {operation} requires PostgreSQL 18/main.")
