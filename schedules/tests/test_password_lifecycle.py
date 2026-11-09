import re

from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from schedules.tests.test_work_remaining import _RenderedSurfaceParser


def assert_invalid_link_feedback(test_case, response, *, recovery_url):
    """Assert an invalid-link panel directs the person to its usable recovery path."""
    parser = _RenderedSurfaceParser()
    parser.feed(response.content.decode())
    panels = [
        element
        for element in parser.elements
        if element.tag == "section"
        and {"panel", "sign-in-panel"}.issubset(element.attributes.get("class", "").split())
    ]
    matching_panels = [
        panel
        for panel in panels
        if any(
            element.tag == "a" and element.attributes.get("href") == recovery_url
            for element in panel.descendants()
        )
    ]
    test_case.assertEqual(len(matching_panels), 1)
    panel = matching_panels[0]
    test_case.assertTrue(
        any(
            category in panel.text().casefold()
            for category in (
                "invalid",
                "unavailable",
                "expired",
                "used",
                "revoked",
                "replaced",
                "no longer",
            )
        )
    )
    test_case.assertFalse(
        any(
            element.tag == "input"
            and element.attributes.get("name") in {"new_password1", "new_password2"}
            and "disabled" not in element.attributes
            for element in panel.descendants()
        )
    )


class PasswordLifecycleTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="existing-password", email="scheduler@example.com"
        )

    def test_password_reset_delivers_a_link_and_sets_a_validated_password(self):
        response = self.client.post(reverse("password_reset"), {"email": self.user.email})

        self.assertRedirects(
            response, reverse("password_reset_done"), fetch_redirect_response=False
        )
        self.assertEqual(len(mail.outbox), 1)
        match = re.search(r"http://testserver(?P<path>/password/reset/[^\s]+)", mail.outbox[0].body)
        self.assertIsNotNone(match)
        response = self.client.get(match["path"], follow=True)
        self.assertContains(response, 'name="new_password1"')
        response = self.client.post(
            response.request["PATH_INFO"],
            {"new_password1": "new-schedule-password", "new_password2": "new-schedule-password"},
        )

        self.assertRedirects(
            response, reverse("password_reset_complete"), fetch_redirect_response=False
        )
        self.assertIsNone(authenticate(username=self.user.username, password="existing-password"))
        self.assertEqual(
            authenticate(username=self.user.username, password="new-schedule-password"), self.user
        )

    def test_ineligible_reset_requests_have_the_same_public_outcome_without_delivery(self):
        inactive = get_user_model().objects.create_user(
            username="inactive",
            password="existing-password",
            email="inactive@example.com",
            is_active=False,
        )
        unusable = get_user_model().objects.create_user(
            username="unusable", password="existing-password", email="unusable@example.com"
        )
        unusable.set_unusable_password()
        unusable.save(update_fields=("password",))

        for email in ("unknown@example.com", inactive.email, unusable.email):
            with self.subTest(email=email):
                mail.outbox.clear()
                response = self.client.post(reverse("password_reset"), {"email": email})

                self.assertRedirects(
                    response, reverse("password_reset_done"), fetch_redirect_response=False
                )
                self.assertEqual(mail.outbox, [])

    def test_invalid_and_expired_reset_links_explain_that_a_new_link_is_needed(self):
        invalid = self.client.get(reverse("password_reset_confirm", args=("bad", "bad-token")))
        self.assertFalse(invalid.context["validlink"])
        assert_invalid_link_feedback(self, invalid, recovery_url=reverse("password_reset"))

        self.client.post(reverse("password_reset"), {"email": self.user.email})
        path = re.search(r"http://testserver(?P<path>/password/reset/[^\s]+)", mail.outbox[0].body)[
            "path"
        ]
        self.user.set_password("replacement-password")
        self.user.save(update_fields=("password",))

        expired = self.client.get(path)
        self.assertFalse(expired.context["validlink"])
        assert_invalid_link_feedback(self, expired, recovery_url=reverse("password_reset"))

    def test_username_and_case_insensitive_email_sign_in_preserve_existing_credentials(self):
        demo = get_user_model().objects.create_user(
            username="demo-scheduler", password="demo-password", email="demo@example.com"
        )

        for identifier in (demo.username, "DEMO@example.com"):
            with self.subTest(identifier=identifier):
                self.client.logout()
                response = self.client.post(
                    reverse("sign-in"), {"username": identifier, "password": "demo-password"}
                )

                self.assertRedirects(
                    response, reverse("master-schedule"), fetch_redirect_response=False
                )

        response = self.client.get(reverse("sign-in"))
        self.assertContains(response, "Username or email")
        self.assertContains(response, "Forgot password?")

    def test_invalid_email_credentials_fail_without_signing_in(self):
        response = self.client.post(
            reverse("sign-in"),
            {"username": "SCHEDULER@example.com", "password": "incorrect-password"},
        )

        error_codes = [
            error.code for error in response.context["form"].non_field_errors().as_data()
        ]
        self.assertIn("invalid_login", error_codes)
        self.assertNotContains(response, self.user.username)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_configured_password_validators_reject_each_required_weak_password(self):
        for password, error_code in (
            ("scheduler-123", "password_too_similar"),
            ("short", "password_too_short"),
            ("password", "password_too_common"),
            ("12345678", "password_entirely_numeric"),
        ):
            with self.subTest(password=password):
                with self.assertRaises(ValidationError) as error:
                    validate_password(password, self.user)
                self.assertIn(error_code, [item.code for item in error.exception.error_list])

    def test_signed_in_user_can_change_password_and_remains_signed_in(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("monthly-workspace")), "Change password")

        response = self.client.post(
            reverse("password_change"),
            {
                "old_password": "existing-password",
                "new_password1": "changed-schedule-password",
                "new_password2": "changed-schedule-password",
            },
        )

        self.assertRedirects(
            response, reverse("password_change_done"), fetch_redirect_response=False
        )
        self.assertEqual(self.client.get(reverse("monthly-workspace")).status_code, 200)
        self.assertEqual(
            authenticate(username=self.user.username, password="changed-schedule-password"),
            self.user,
        )

    @override_settings(
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
        USE_X_FORWARDED_HOST=True,
        ALLOWED_HOSTS=["muriel.example"],
    )
    def test_reset_email_uses_the_external_proxy_scheme_and_host(self):
        self.client.post(
            reverse("password_reset"),
            {"email": self.user.email},
            HTTP_HOST="muriel.example",
            HTTP_X_FORWARDED_HOST="muriel.example",
            HTTP_X_FORWARDED_PROTO="https",
        )

        self.assertIn("https://muriel.example/password/reset/", mail.outbox[0].body)
