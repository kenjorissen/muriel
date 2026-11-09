import re
from unittest.mock import patch

from django.conf import settings
from django.contrib.sessions.models import Session
from django.db import OperationalError, connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from preceptor_project import health
from schedules.models import Physician


class HealthEndpointTests(TestCase):
    def readiness_state(self):
        return {
            "physicians": list(
                Physician.objects.order_by("pk").values_list("pk", "display_name", "user_id")
            ),
            "sessions": list(
                Session.objects.order_by("session_key").values_list(
                    "session_key", "session_data", "expire_date"
                )
            ),
        }

    def seed_application_and_session_state(self):
        Physician.objects.create(display_name="Health readiness sentinel")
        session = self.client.session
        session["health-readiness-sentinel"] = "preserve-this-session-state"
        session.save()
        self.assertNotIn("_auth_user_id", session)
        return self.readiness_state()

    def assert_bounded_database_readiness_probe(self, queries):
        self.assertGreaterEqual(len(queries.captured_queries), 1)
        # These are deliberately loose guardrails for bounded database readiness
        # work, not a prescribed query count or query order.
        self.assertLessEqual(len(queries.captured_queries), 2)

    def assert_no_captured_database_write_effects(self, queries):
        # This broad guard catches ordinary mutating statements in the probe. It
        # complements the seeded-state assertion; it is not a SQL parser.
        write_statement = re.compile(
            r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO|MERGE\s+INTO|"
            r"CREATE\s+(?:TABLE|INDEX)|ALTER\s+TABLE|DROP\s+(?:TABLE|INDEX)|TRUNCATE\s+TABLE)\b",
            re.IGNORECASE,
        )
        for query in queries.captured_queries:
            with self.subTest(sql=query["sql"]):
                self.assertNotRegex(query["sql"], write_statement)

    def test_get_reports_database_readiness_without_session_or_data_side_effects(self):
        state_before = self.seed_application_and_session_state()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"].split(";", 1)[0], "text/plain")
        # This is a deliberately loose minimal-output guardrail for readiness, not
        # an exact health response contract.
        self.assertLessEqual(len(response.content), 64)
        self.assertNotIn(settings.SECRET_KEY.encode(), response.content)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertNotIn(settings.CSRF_COOKIE_NAME, response.cookies)
        self.assertEqual(self.readiness_state(), state_before)
        self.assert_bounded_database_readiness_probe(queries)
        self.assert_no_captured_database_write_effects(queries)

    def test_head_reports_readiness_without_a_response_body(self):
        state_before = self.seed_application_and_session_state()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.head(reverse("health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(response["Content-Type"].split(";", 1)[0], "text/plain")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertNotIn(settings.CSRF_COOKIE_NAME, response.cookies)
        self.assertEqual(self.readiness_state(), state_before)
        self.assert_bounded_database_readiness_probe(queries)
        self.assert_no_captured_database_write_effects(queries)

    @override_settings(SECRET_KEY="health-configuration-secret")
    def test_database_failure_returns_a_sanitized_service_unavailable_response(self):
        connection_details = (
            "postgresql://health-user:health-password@health-host.example:5432/health-database"
        )
        exception_detail = "health-exception-detail"
        failure = f"{connection_details}: {exception_detail}"
        with patch.object(health.connection, "cursor", side_effect=OperationalError(failure)):
            response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Content-Type"].split(";", 1)[0], "text/plain")
        self.assertLessEqual(len(response.content), 64)
        self.assertIn("no-store", response["Cache-Control"])
        for protected_component in (
            "health-user",
            "health-password",
            "health-host.example",
            "health-database",
            exception_detail,
            settings.SECRET_KEY,
        ):
            with self.subTest(protected_component=protected_component):
                self.assertNotIn(protected_component.encode(), response.content)
