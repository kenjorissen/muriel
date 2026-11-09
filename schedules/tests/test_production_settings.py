import json
import os
import subprocess
import sys
from math import isfinite
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[2]
VALID_SECRET = "production-secret-key-with-more-than-fifty-safe-characters-1234567890"
PRODUCTION_ENVIRONMENT = {
    "PRECEPTOR_PRODUCTION_SECRET_KEY": VALID_SECRET,
    "PRECEPTOR_PRODUCTION_ALLOWED_HOST": "muriel.example",
    "PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN": "https://muriel.example",
    "PRECEPTOR_POSTGRES_NAME": "muriel",
    "PRECEPTOR_POSTGRES_USER": "muriel",
}
MANIFEST_STATICFILES_STORAGE = "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"


class ProductionSettingsTests(TestCase):
    def run_production_settings(self, code, **environment):
        clean_environment = {
            name: value for name, value in os.environ.items() if not name.startswith("PRECEPTOR_")
        }
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=clean_environment
            | {"DJANGO_SETTINGS_MODULE": "preceptor_project.production_settings"}
            | environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def inspect(self, **environment):
        result = self.run_production_settings(
            """
import json
from django.conf import settings

print(json.dumps({
    "debug": settings.DEBUG,
    "hosts": settings.ALLOWED_HOSTS,
    "origins": settings.CSRF_TRUSTED_ORIGINS,
    "database": settings.DATABASES["default"],
    "proxy_header": settings.SECURE_PROXY_SSL_HEADER,
    "ssl_redirect": settings.SECURE_SSL_REDIRECT,
    "session_secure": settings.SESSION_COOKIE_SECURE,
    "csrf_secure": settings.CSRF_COOKIE_SECURE,
    "hsts_seconds": settings.SECURE_HSTS_SECONDS,
    "hsts_subdomains": settings.SECURE_HSTS_INCLUDE_SUBDOMAINS,
    "hsts_preload": settings.SECURE_HSTS_PRELOAD,
    "timezone": settings.TIME_ZONE,
    "invitation_mode": settings.SCHEDULER_INVITATION_DELIVERY_MODE,
    "static_storage": settings.STORAGES["staticfiles"]["BACKEND"],
}))
""",
            **PRODUCTION_ENVIRONMENT,
            **environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_requires_the_secret_host_origin_and_postgresql_identity(self):
        for missing in PRODUCTION_ENVIRONMENT:
            with self.subTest(missing=missing):
                environment = PRODUCTION_ENVIRONMENT.copy()
                environment.pop(missing)

                result = self.run_production_settings(
                    "from django.conf import settings; settings.DEBUG", **environment
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(missing, result.stderr)

    def test_rejects_unsafe_secret_and_host_or_origin_values(self):
        invalid_environments = (
            {"PRECEPTOR_PRODUCTION_SECRET_KEY": "django-insecure-development-secret"},
            {"PRECEPTOR_PRODUCTION_ALLOWED_HOST": "*"},
            {"PRECEPTOR_PRODUCTION_ALLOWED_HOST": "*.muriel.example"},
            {"PRECEPTOR_PRODUCTION_ALLOWED_HOST": "muriel.*"},
            {"PRECEPTOR_PRODUCTION_ALLOWED_HOST": "muriel.example,other.example"},
            {"PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN": "http://muriel.example"},
            {"PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN": "https://other.example"},
            {"PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN": "https://muriel.example/path"},
            {"PRECEPTOR_POSTGRES_HOST": "postgres.example:5432"},
            {"PRECEPTOR_POSTGRES_PORT": "0"},
        )

        for environment in invalid_environments:
            with self.subTest(environment=environment):
                result = self.run_production_settings(
                    "from django.conf import settings; settings.DEBUG",
                    **(PRODUCTION_ENVIRONMENT | environment),
                )

                self.assertNotEqual(result.returncode, 0)

    def test_uses_postgresql_and_secure_loopback_proxy_settings(self):
        settings = self.inspect()

        self.assertFalse(settings["debug"])
        self.assertEqual(settings["hosts"], ["muriel.example"])
        self.assertEqual(settings["origins"], ["https://muriel.example"])
        self.assertEqual(settings["database"]["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(settings["database"]["NAME"], "muriel")
        self.assertEqual(settings["database"]["USER"], "muriel")
        self.assertEqual(settings["database"]["HOST"], "/var/run/postgresql")
        self.assertEqual(settings["database"]["PORT"], "5432")
        connect_timeout = settings["database"].get("OPTIONS", {}).get("connect_timeout")
        self.assertIsInstance(connect_timeout, (int, float))
        self.assertNotIsInstance(connect_timeout, bool)
        self.assertTrue(isfinite(connect_timeout))
        self.assertGreater(connect_timeout, 0)
        self.assertEqual(settings["proxy_header"], ["HTTP_X_FORWARDED_PROTO", "https"])
        self.assertTrue(settings["ssl_redirect"])
        self.assertTrue(settings["session_secure"])
        self.assertTrue(settings["csrf_secure"])
        self.assertGreater(settings["hsts_seconds"], 0)
        self.assertFalse(settings["hsts_subdomains"])
        self.assertFalse(settings["hsts_preload"])
        self.assertEqual(settings["timezone"], "America/Chicago")
        self.assertEqual(settings["invitation_mode"], "manual")
        self.assertEqual(settings["static_storage"], MANIFEST_STATICFILES_STORAGE)

    def test_accepts_one_explicit_secondary_public_host_and_origin(self):
        settings = self.inspect(
            PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST="edge.example.test",
            PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN="https://edge.example.test",
        )

        self.assertCountEqual(settings["hosts"], ["muriel.example", "edge.example.test"])
        self.assertCountEqual(
            settings["origins"],
            ["https://muriel.example", "https://edge.example.test"],
        )

    def test_rejects_incomplete_unsafe_or_duplicate_secondary_origin(self):
        invalid_environments = (
            {"PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST": "edge.example.test"},
            {"PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN": "https://edge.example.test"},
            {
                "PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST": "edge.example.test",
                "PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN": ("https://other.example"),
            },
            {
                "PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST": "muriel.example",
                "PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN": ("https://muriel.example"),
            },
        )

        for environment in invalid_environments:
            with self.subTest(environment=environment):
                result = self.run_production_settings(
                    "from django.conf import settings; settings.DEBUG",
                    **(PRODUCTION_ENVIRONMENT | environment),
                )

                self.assertNotEqual(result.returncode, 0)

    def test_allows_only_the_commissioned_socket_and_port_environment_values(self):
        settings = self.inspect(
            PRECEPTOR_POSTGRES_HOST="/var/run/postgresql",
            PRECEPTOR_POSTGRES_PORT="5432",
            PRECEPTOR_POSTGRES_PASSWORD="environment-only-password",
        )

        self.assertEqual(settings["database"]["HOST"], "/var/run/postgresql")
        self.assertEqual(settings["database"]["PORT"], "5432")
        self.assertEqual(settings["database"]["PASSWORD"], "environment-only-password")

    def test_only_the_unix_socket_proxy_https_header_prevents_the_https_redirect(self):
        result = self.run_production_settings(
            """
import django
import json
from django.test import Client

django.setup()
client = Client()
http = client.get("/", HTTP_HOST="muriel.example")
unix_socket_https = client.get(
    "/",
    HTTP_HOST="muriel.example",
    HTTP_X_FORWARDED_PROTO="https",
    REMOTE_ADDR="",
)
loopback_spoofed_https = client.get(
    "/",
    HTTP_HOST="muriel.example",
    HTTP_X_FORWARDED_PROTO="https",
    REMOTE_ADDR="127.0.0.1",
)
spoofed_https = client.get(
    "/",
    HTTP_HOST="muriel.example",
    HTTP_X_FORWARDED_PROTO="https",
    REMOTE_ADDR="203.0.113.17",
)
print(json.dumps({
    "http": {"status": http.status_code, "location": http["Location"]},
    "unix_socket_https": {
        "status": unix_socket_https.status_code,
        "location": unix_socket_https["Location"],
    },
    "loopback_spoofed_https": {
        "status": loopback_spoofed_https.status_code,
        "location": loopback_spoofed_https["Location"],
    },
    "spoofed_https": {
        "status": spoofed_https.status_code,
        "location": spoofed_https["Location"],
    },
}))
""",
            **PRODUCTION_ENVIRONMENT,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        responses = json.loads(result.stdout)
        self.assertEqual(responses["http"], {"status": 301, "location": "https://muriel.example/"})
        self.assertEqual(responses["unix_socket_https"]["status"], 302)
        self.assertNotEqual(responses["unix_socket_https"]["location"], "https://muriel.example/")
        self.assertEqual(
            responses["loopback_spoofed_https"],
            {"status": 301, "location": "https://muriel.example/"},
        )
        self.assertEqual(
            responses["spoofed_https"],
            {"status": 301, "location": "https://muriel.example/"},
        )

    def test_development_and_test_settings_remain_sqlite_based(self):
        clean_environment = {
            name: value for name, value in os.environ.items() if not name.startswith("PRECEPTOR_")
        }
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json; from django.conf import settings; print(json.dumps({'debug': settings.DEBUG, 'engine': settings.DATABASES['default']['ENGINE'], 'email': settings.EMAIL_BACKEND}))",
            ],
            cwd=ROOT,
            env=clean_environment | {"DJANGO_SETTINGS_MODULE": "preceptor_project.test_settings"},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        settings = json.loads(result.stdout)
        self.assertTrue(settings["debug"])
        self.assertEqual(settings["engine"], "django.db.backends.sqlite3")
        self.assertEqual(settings["email"], "django.core.mail.backends.locmem.EmailBackend")

    def test_deploy_check_has_no_security_warnings(self):
        clean_environment = {
            name: value for name, value in os.environ.items() if not name.startswith("PRECEPTOR_")
        }
        result = subprocess.run(
            [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "WARNING"],
            cwd=ROOT,
            env=clean_environment
            | {"DJANGO_SETTINGS_MODULE": "preceptor_project.production_settings"}
            | PRODUCTION_ENVIRONMENT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
