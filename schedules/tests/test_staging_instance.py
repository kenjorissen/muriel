import json
import os
import stat
import subprocess
import sys
import tempfile
from configparser import ConfigParser
from math import isfinite
from pathlib import Path
from unittest import TestCase

from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import Client, SimpleTestCase, override_settings
from django.test.client import RequestFactory

from schedules.tests.test_work_remaining import _RenderedSurfaceParser

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "ops" / "bootstrap-ubuntu"
VALID_SECRET = "staging-secret-key-with-more-than-fifty-safe-characters-1234567890"
STAGING_ENVIRONMENT = {
    "PRECEPTOR_STAGING_SECRET_KEY": VALID_SECRET,
    "PRECEPTOR_STAGING_ALLOWED_HOST": "staging.example.test",
    "PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN": "https://staging.example.test:10000",
    "PRECEPTOR_STAGING_POSTGRES_NAME": "muriel_staging",
    "PRECEPTOR_STAGING_POSTGRES_USER": "muriel_staging",
}


class StagingSettingsTests(TestCase):
    def test_staging_rejects_any_non_isolated_postgres_identity(self):
        environment = {
            name: value for name, value in os.environ.items() if not name.startswith("PRECEPTOR_")
        }
        for name in (
            "PRECEPTOR_STAGING_POSTGRES_NAME",
            "PRECEPTOR_STAGING_POSTGRES_USER",
        ):
            for value in ("muriel", "another_database"):
                with self.subTest(name=name, value=value):
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            "from django.conf import settings; settings.DATABASES",
                        ],
                        cwd=ROOT,
                        env=environment
                        | {"DJANGO_SETTINGS_MODULE": "preceptor_project.staging_settings"}
                        | STAGING_ENVIRONMENT
                        | {name: value},
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(name, result.stderr)
                    self.assertIn("muriel_staging", result.stderr)

    def test_staging_uses_its_own_identity_cookies_static_state_and_dummy_email(self):
        environment = {
            name: value for name, value in os.environ.items() if not name.startswith("PRECEPTOR_")
        }
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import json
from django.conf import settings

print(json.dumps({
    "database": settings.DATABASES["default"],
    "marker": settings.STAGING_ENVIRONMENT_MARKER,
    "session_cookie": settings.SESSION_COOKIE_NAME,
    "csrf_cookie": settings.CSRF_COOKIE_NAME,
    "email_backend": settings.EMAIL_BACKEND,
    "static_root": str(settings.STATIC_ROOT),
    "origins": settings.CSRF_TRUSTED_ORIGINS,
}))
""",
            ],
            cwd=ROOT,
            env=environment
            | {"DJANGO_SETTINGS_MODULE": "preceptor_project.staging_settings"}
            | STAGING_ENVIRONMENT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        settings = json.loads(result.stdout)
        self.assertEqual(settings["database"]["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(settings["database"]["NAME"], "muriel_staging")
        self.assertEqual(settings["database"]["USER"], "muriel_staging")
        self.assertEqual(settings["database"]["HOST"], "/var/run/postgresql")
        self.assertEqual(settings["database"]["PORT"], "5432")
        connect_timeout = settings["database"].get("OPTIONS", {}).get("connect_timeout")
        self.assertIsInstance(connect_timeout, (int, float))
        self.assertNotIsInstance(connect_timeout, bool)
        self.assertTrue(isfinite(connect_timeout))
        self.assertGreater(connect_timeout, 0)
        # TEXT-CONTRACT: staging-synthetic-environment-marker — docs/staging.md requires
        # this visible identifier to distinguish synthetic staging data from production;
        # scope: capitalization, U+2014 em dash, and single spaces surrounding it.
        # Production: preceptor_project/staging_settings.py;
        # protecting test: StagingSettingsTests.test_staging_uses_its_own_identity_cookies_static_state_and_dummy_email.
        self.assertEqual(settings["marker"], "STAGING — SYNTHETIC DATA")
        self.assertEqual(settings["session_cookie"], "muriel_staging_sessionid")
        self.assertEqual(settings["csrf_cookie"], "muriel_staging_csrftoken")
        self.assertEqual(settings["email_backend"], "django.core.mail.backends.dummy.EmailBackend")
        self.assertEqual(settings["static_root"], "/srv/muriel-staging/shared/static")
        self.assertEqual(
            settings["origins"], [STAGING_ENVIRONMENT["PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN"]]
        )

    def assert_visible_staging_marker(self, rendered):
        parser = _RenderedSurfaceParser()
        parser.feed(rendered)
        markers = [
            element
            for element in parser.elements
            if element.attributes.get("role") == "status"
            and element.text() == "STAGING — SYNTHETIC DATA"
        ]
        self.assertEqual(len(markers), 1)
        for element in (markers[0], *markers[0].ancestors()):
            self.assertNotIn("hidden", element.attributes)
            self.assertNotEqual(element.attributes.get("aria-hidden", "").lower(), "true")

    @override_settings(STAGING_ENVIRONMENT_MARKER="STAGING — SYNTHETIC DATA")
    def test_base_template_persistently_displays_the_staging_marker(self):
        request = RequestFactory().get("/")
        request.user = AnonymousUser()

        rendered = render_to_string("schedules/base.html", request=request)

        self.assert_visible_staging_marker(rendered)

    @override_settings(STAGING_ENVIRONMENT_MARKER="STAGING — SYNTHETIC DATA")
    def test_admin_login_persistently_displays_the_staging_marker(self):
        response = Client().get("/admin/login/", HTTP_X_MURIEL_ADMIN="1")

        self.assertEqual(response.status_code, 200)
        self.assert_visible_staging_marker(response.content.decode())

    def test_staging_serves_a_collected_stylesheet(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
import json
from pathlib import Path
import sys

import django

django.setup()

from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management import call_command
from django.test import Client, override_settings

with override_settings(STATIC_ROOT=Path(sys.argv[1])):
    call_command("collectstatic", interactive=False, verbosity=0)
    client = Client(HTTP_HOST=settings.STAGING_ALLOWED_HOST)
    manifest_url = staticfiles_storage.url("schedules/application.css")
    responses = {}
    for url in ("/static/schedules/application.css", manifest_url):
        response = client.get(url, secure=True)
        responses[url] = {
            "status_code": response.status_code,
            "content_type": response["Content-Type"],
            "charset": response.charset,
            "content": (b"".join(response.streaming_content)
                        if response.streaming else response.content).decode(),
        }
        response.close()
    missing = client.get("/static/schedules/missing.css", secure=True)
    print(json.dumps({
        "debug": settings.DEBUG,
        "manifest_url": manifest_url,
        "responses": responses,
        "missing_status": missing.status_code,
    }))
    missing.close()
""",
                    temporary_directory,
                ],
                cwd=ROOT,
                env=os.environ
                | {"DJANGO_SETTINGS_MODULE": "preceptor_project.staging_settings"}
                | STAGING_ENVIRONMENT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        result_data = json.loads(result.stdout)
        self.assertFalse(result_data["debug"])
        self.assertNotEqual(result_data["manifest_url"], "/static/schedules/application.css")
        for url, response in result_data["responses"].items():
            with self.subTest(url=url):
                self.assertEqual(response["status_code"], 200)
                self.assertEqual(response["content_type"].split(";", 1)[0], "text/css")
                self.assertEqual(response["charset"], "utf-8")
                self.assertIn("--surface", response["content"])
        self.assertEqual(result_data["missing_status"], 404)


class StagingBootstrapTests(SimpleTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name)
        self.root = self.temporary_path / "root"
        self.fake_bin = self.temporary_path / "bin"
        self.state = self.temporary_path / "state"
        self.log = self.temporary_path / "commands.log"
        self.root.mkdir()
        self.fake_bin.mkdir()
        self.state.mkdir()
        (self.root / "etc/systemd/system").mkdir(parents=True)
        self.write_dispatcher()

    def write_executable(self, path, content):
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def write_dispatcher(self):
        dispatcher = self.fake_bin / "dispatcher"
        self.write_executable(
            dispatcher,
            """#!/bin/sh
set -eu
command=$(basename "$0")
case "$command" in
    getent)
        case "$1:$2" in
            group:muriel_staging) [ -f "$FAKE_STATE/group" ] && printf 'muriel_staging:x:998:\\n' ;;
            passwd:muriel_staging) [ -f "$FAKE_STATE/user" ] && printf 'muriel_staging:x:998:998::/srv/muriel-staging:/usr/sbin/nologin\\n' ;;
        esac
        ;;
    groupadd) touch "$FAKE_STATE/group" ;;
    useradd) touch "$FAKE_STATE/user" ;;
    chown) ;;
    stat)
        for argument; do path=$argument; done
        case "$path" in
            */etc/muriel-staging/environment) value=root:muriel_staging:640 ;;
            */srv/muriel-staging) value=root:muriel_staging:750 ;;
            */srv/muriel-staging/releases|*/srv/muriel-staging/shared|*/srv/muriel-staging/shared/static) value=muriel_staging:muriel_staging:750 ;;
            */srv/muriel-staging/shared/pip-cache) value=muriel_staging:muriel_staging:700 ;;
            */etc/muriel-staging) value=root:muriel_staging:750 ;;
            */etc/systemd/system/muriel-staging.service) value=root:root:644 ;;
            */srv/muriel-staging/deploy-release|*/srv/muriel-staging/create-superuser) value=root:root:755 ;;
            *) value=root:root:644 ;;
        esac
        printf '%s\\n' "$value"
        ;;
    systemctl) printf 'systemctl %s\\n' "$*" >> "$FAKE_LOG" ;;
    runuser)
        for argument; do query=$argument; done
        case "$query" in
            *"FROM pg_authid"*) [ -f "$FAKE_STATE/role" ] && printf 'true:true:false:false\\n' ;;
            *"FROM pg_database"*) [ -f "$FAKE_STATE/database" ] && printf 'muriel_staging\\n' ;;
            "CREATE ROLE muriel_staging LOGIN") touch "$FAKE_STATE/role"; printf 'create-role\\n' >> "$FAKE_LOG" ;;
            "CREATE DATABASE muriel_staging OWNER muriel_staging") touch "$FAKE_STATE/database"; printf 'create-database\\n' >> "$FAKE_LOG" ;;
        esac
        true
        ;;
esac
""",
        )
        for command in ("chown", "getent", "groupadd", "runuser", "stat", "systemctl", "useradd"):
            (self.fake_bin / command).symlink_to(dispatcher.name)

    def run_bootstrap(self, mode):
        return subprocess.run(
            [str(BOOTSTRAP), "--instance", "staging", mode, "--root", str(self.root)],
            cwd=Path.home(),
            env=os.environ
            | {
                "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
                "FAKE_LOG": str(self.log),
                "FAKE_STATE": str(self.state),
                "PRECEPTOR_STAGING_ALLOWED_HOST": "staging.example.test",
                "PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN": "https://staging.example.test:10000",
            },
            text=True,
            capture_output=True,
            check=False,
        )

    def test_apply_converges_only_the_staging_identity_and_paths(self):
        self.assertEqual(self.run_bootstrap("--apply").returncode, 0)
        self.assertEqual(self.run_bootstrap("--check").returncode, 0)
        self.assertEqual(self.run_bootstrap("--apply").returncode, 0)

        commands = self.log.read_text()
        self.assertEqual(commands.count("create-role\n"), 1)
        self.assertEqual(commands.count("create-database\n"), 1)
        self.assertTrue((self.root / "srv/muriel-staging/shared/static").is_dir())
        self.assertTrue((self.root / "srv/muriel-staging/shared/pip-cache").is_dir())
        self.assertTrue((self.root / "etc/systemd/system/muriel-staging.service").is_file())
        self.assertTrue((self.root / "srv/muriel-staging/deploy-release").is_file())
        self.assertTrue((self.root / "srv/muriel-staging/create-superuser").is_file())
        self.assertFalse((self.root / "srv/muriel").exists())
        self.assertFalse((self.root / "etc/muriel").exists())

    def test_service_identity_matches_the_passwordless_postgresql_peer_role(self):
        configuration = ConfigParser(strict=False)
        configuration.read(ROOT / "ops/systemd/muriel-staging.service")

        self.assertEqual(configuration["Service"]["User"], "muriel_staging")
        self.assertEqual(configuration["Service"]["Group"], "muriel_staging")
