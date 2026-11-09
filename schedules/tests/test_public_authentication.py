import logging
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from preceptor_project.log_safety import SecretRedactionFilter
from preceptor_project.security import PublicBoundaryMiddleware
from schedules.tests.test_table_presentation import _RenderedDocument

# Public application endpoints accepted in the authentication and readiness contracts.
PUBLIC_ROUTE_NAMES = frozenset(
    {
        "health",
        "sign-in",
        "password_reset",
        "password_reset_done",
        "password_reset_confirm",
        "password_reset_complete",
        "scheduler-invitation-accept",
        "scheduler-invitation-complete",
    }
)


def named_routes(patterns, namespace=""):
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            child_namespace = ":".join(part for part in (namespace, pattern.namespace) if part)
            yield from named_routes(pattern.url_patterns, child_namespace)
        elif isinstance(pattern, URLPattern) and pattern.name:
            yield ":".join(part for part in (namespace, pattern.name) if part)


class PublicAuthenticationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="existing-password", email="scheduler@example.com"
        )

    def assert_anonymous_redirect_to_sign_in(self, response, protected_path):
        self.assertEqual(response.status_code, 302)

        destination = urlsplit(response["Location"])
        self.assertEqual(destination.scheme, "")
        self.assertEqual(destination.netloc, "")
        self.assertEqual(destination.path, reverse("sign-in"))
        self.assertEqual(parse_qs(destination.query).get("next"), [protected_path])

    def assert_safe_error_page(self, response, status, *hidden_details):
        self.assertEqual(response.status_code, status)
        document = _RenderedDocument(response.content.decode())
        headings = document.root.descendants_named("h1")
        self.assertEqual(len(headings), 1)
        expected_heading_categories = {
            400: r"(?i)\b(?:request\s+unavailable|bad\s+request)\b",
            403: r"(?i)\b(?:access\s+denied|permission\s+denied)\b",
            404: r"(?i)\b(?:page\s+unavailable|page\s+not\s+found)\b",
            500: r"(?i)\b(?:service\s+unavailable|temporarily\s+unavailable)\b",
        }
        self.assertRegex(headings[0].text().strip(), expected_heading_categories[status])
        self.assertContains(response, "Muriel", status_code=status)
        sign_in_link_pattern = re.compile(r"(?i)\b(?:sign|log)\s*in\b")
        self.assertTrue(
            any(
                (destination := urlsplit(link.attributes.get("href", ""))).scheme == ""
                and destination.netloc == ""
                and destination.path == reverse("sign-in")
                and sign_in_link_pattern.search(
                    " ".join((link.text(), link.attributes.get("aria-label", "")))
                )
                for link in document.root.descendants_named("a")
            )
        )
        self.assertNotContains(response, "Traceback", status_code=status)
        for detail in hidden_details:
            with self.subTest(detail=detail):
                self.assertNotContains(response, detail, status_code=status)

    def test_route_audit_uses_the_independently_reviewed_public_endpoint_policy(self):
        middleware = PublicBoundaryMiddleware(lambda request: None)
        request_factory = RequestFactory()
        route_names = set(named_routes(get_resolver().url_patterns))

        self.assertTrue(PUBLIC_ROUTE_NAMES <= route_names)
        self.assertIn("admin:index", route_names)
        for route_name in route_names:
            with self.subTest(route_name=route_name):
                request = request_factory.get("/protected/")
                request.user = AnonymousUser()
                request.request_id = "audit-request"
                request.resolver_match = SimpleNamespace(view_name=route_name)

                response = middleware.process_view(request, None, (), {})

                if route_name in PUBLIC_ROUTE_NAMES:
                    self.assertIsNone(response)
                else:
                    self.assert_anonymous_redirect_to_sign_in(response, "/protected/")

    def test_anonymous_domain_routes_and_mutations_redirect_before_view_execution(self):
        requests = (
            ("monthly-workspace", (), "get"),
            ("vacation-create", (), "post"),
            ("physicians-directory", (), "get"),
            ("account-invite", (), "post"),
            ("admin:index", (), "get"),
        )

        for route_name, args, method in requests:
            with self.subTest(route_name=route_name):
                protected_path = reverse(route_name, args=args)
                response = getattr(self.client, method)(protected_path)

                self.assert_anonymous_redirect_to_sign_in(response, protected_path)

    @override_settings(ROOT_URLCONF="schedules.tests.error_urls")
    def test_anonymous_request_is_redirected_before_a_protected_view_can_run(self):
        response = self.client.get("/broken/")

        self.assert_anonymous_redirect_to_sign_in(response, "/broken/")

    def test_sign_in_rejects_external_redirect_targets(self):
        response = self.client.post(
            f"{reverse('sign-in')}?next=https://outside.example/path",
            {
                "username": self.user.username,
                "password": "existing-password",
                "next": "https://outside.example/path",
            },
        )

        self.assertRedirects(response, reverse("master-schedule"), fetch_redirect_response=False)

    def test_public_flows_remain_available_without_account_enumeration(self):
        self.assertEqual(self.client.get(reverse("sign-in")).status_code, 200)
        self.assertEqual(self.client.get(reverse("password_reset")).status_code, 200)
        self.assertEqual(
            self.client.get(
                reverse("scheduler-invitation-accept", args=("invalid-token",))
            ).status_code,
            200,
        )
        existing = self.client.post(reverse("password_reset"), {"email": self.user.email})
        unknown = self.client.post(reverse("password_reset"), {"email": "unknown@example.com"})

        self.assertEqual(existing.status_code, unknown.status_code)
        self.assertEqual(existing["Location"], unknown["Location"])
        self.assertRedirects(
            existing, reverse("password_reset_done"), fetch_redirect_response=False
        )

    @override_settings(DEBUG=False)
    def test_production_error_pages_hide_internal_details(self):
        self.client.force_login(self.user)

        bad_request = self.client.get(f"{reverse('monthly-workspace')}?month=not-a-month")
        denied = self.client.get(reverse("account-workspace"))
        missing = self.client.get("/not-a-route/")

        for response, status in (
            (bad_request, 400),
            (denied, 403),
            (missing, 404),
        ):
            with self.subTest(status=status):
                self.assert_safe_error_page(response, status)

    @override_settings(DEBUG=False, ALLOWED_HOSTS=["muriel.example"])
    def test_production_invalid_host_uses_the_safe_bad_request_page(self):
        response = self.client.get("/", HTTP_HOST="invalid.example")

        self.assert_safe_error_page(response, 400)

    @override_settings(DEBUG=False)
    def test_production_csrf_rejection_uses_the_safe_forbidden_page(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)

        response = csrf_client.post(reverse("sign-out"))

        self.assert_safe_error_page(response, 403)
        self.assertNotContains(response, "More information is available", status_code=403)

    @override_settings(DEBUG=False, ROOT_URLCONF="schedules.tests.error_urls")
    def test_production_server_errors_use_the_safe_error_page(self):
        self.client.force_login(self.user)
        self.client.raise_request_exception = False

        response = self.client.get("/broken/")

        self.assert_safe_error_page(response, 500, "secret-token-must-not-reach-the-response")

    def test_log_redaction_removes_authentication_material_and_tracebacks(self):
        record = logging.LogRecord(
            "django.request",
            logging.ERROR,
            __file__,
            1,
            "Failed %s password=message-password-value token=message-reset-value",
            ("/password/reset/encoded-user/path-reset-token/?sessionid=session-value",),
            (RuntimeError, RuntimeError("secret=exception-secret-value"), None),
        )
        record.request = SimpleNamespace(request_id="request-123")

        SecretRedactionFilter().filter(record)

        self.assertEqual(record.request_id, "request-123")
        for secret in (
            "message-password-value",
            "message-reset-value",
            "path-reset-token",
            "session-value",
            "exception-secret-value",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, record.getMessage())
        self.assertIsNone(record.exc_info)
