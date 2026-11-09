from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import CommandError
from django.test import SimpleTestCase

from schedules.management import production_database

PRODUCTION_DATABASE = {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": "muriel",
    "USER": "muriel",
    "HOST": "/var/run/postgresql",
    "PORT": "5432",
}
POSTGRES_18_VERSION = "PostgreSQL 18.0 (Ubuntu 18.0-1.pgdg26.04+1)"


class ProductionDatabaseValidationTests(SimpleTestCase):
    def _settings(self, database=PRODUCTION_DATABASE, *, debug=False):
        return SimpleNamespace(DEBUG=debug, DATABASES={"default": database})

    def _connection_with_endpoint_facts(self, facts):
        """Provide endpoint facts without constraining the validation probe count or SQL layout."""
        cursor = MagicMock()
        cursor.queries = []
        cursor.queried_facts = set()
        fact_values = dict(zip(("current_database", "current_user", "version"), facts, strict=True))

        def execute(query, *parameters):
            normalized = " ".join(query.lower().split())
            cursor.queries.append(normalized)
            if not normalized.startswith(("select", "show")) or "config_file" in normalized:
                raise AssertionError(f"unexpected privileged or mutating database probe: {query}")
            cursor.queried_facts.update(field for field in fact_values if field in normalized)

        def fetchone():
            selected_fields = [
                field
                for expression in cursor.queries[-1].removeprefix("select").split(",")
                for field in fact_values
                if field in expression
            ]
            if selected_fields:
                return tuple(fact_values[field] for field in selected_fields)
            return (None,)

        cursor.execute.side_effect = execute
        cursor.fetchone.side_effect = fetchone
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        return connection, cursor

    def test_validates_an_unprivileged_production_connection_without_privileged_probes(self):
        with patch.object(production_database, "settings", self._settings()):
            connection, cursor = self._connection_with_endpoint_facts(
                ("muriel", "muriel", POSTGRES_18_VERSION)
            )
            with patch.object(production_database, "connection", connection):
                production_database.validate_production_database("test operation")

        self.assertSetEqual(
            cursor.queried_facts,
            {"current_database", "current_user", "version"},
        )

    def test_refuses_wrong_settings_before_connecting(self):
        cases = (
            ("development", self._settings(debug=True), "development settings"),
            (
                "engine",
                self._settings({"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}),
                "requires PostgreSQL",
            ),
            (
                "database",
                self._settings(PRODUCTION_DATABASE | {"NAME": "other"}),
                "local muriel database",
            ),
            (
                "transport",
                self._settings(PRODUCTION_DATABASE | {"HOST": "127.0.0.1"}),
                "local socket",
            ),
            ("port", self._settings(PRODUCTION_DATABASE | {"PORT": "5433"}), "local socket"),
        )
        for name, settings, message in cases:
            with self.subTest(name=name):
                with patch.object(production_database, "settings", settings):
                    with patch.object(production_database, "connection") as connection:
                        with self.assertRaisesRegex(CommandError, message):
                            production_database.validate_production_database("test operation")

                connection.cursor.assert_not_called()

    def test_refuses_wrong_effective_database_user_or_postgresql_version(self):
        cases = (
            ("database", ("other", "muriel", POSTGRES_18_VERSION)),
            ("user", ("muriel", "other", POSTGRES_18_VERSION)),
            ("version", ("muriel", "muriel", "PostgreSQL 17.5 (Ubuntu 17.5-1.pgdg26.04+1)")),
        )
        for name, result in cases:
            with self.subTest(name=name):
                with patch.object(production_database, "settings", self._settings()):
                    connection, _cursor = self._connection_with_endpoint_facts(result)
                    with patch.object(production_database, "connection", connection):
                        with self.assertRaisesRegex(CommandError, "requires PostgreSQL"):
                            production_database.validate_production_database("test operation")
