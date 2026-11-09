import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.staticfiles.storage import ManifestStaticFilesStorage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from schedules.tests.test_work_remaining import _RenderedSurfaceParser

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_LAUNCHER = ROOT / "scripts" / "start-production"
READINESS_LAUNCHER = ROOT / "scripts" / "check-production"
REQUIRED_CONSUMER_STATIC_ASSETS = (
    "schedules/application.css",
    "schedules/images/random-health.png",
    "schedules/calendar-period-picker.js",
    "schedules/calendar-week-actions.js",
    "schedules/calendar-day-actions.js",
)
MANIFEST_STATICFILES_STORAGE = {
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
    },
}


class ProductionLauncherTests(TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "release"
        self.root.mkdir()
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        self.command_log = self.root / "commands.log"
        self.create_launcher(PRODUCTION_LAUNCHER)
        self.create_launcher(READINESS_LAUNCHER)

    def create_launcher(self, source):
        launcher = self.root / "scripts" / source.name
        launcher.parent.mkdir(exist_ok=True)
        shutil.copy2(source, launcher)
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)

    def write_executable(self, path, content):
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def create_virtual_environment(self):
        python = self.root / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        self.write_executable(
            python,
            f"""#!{sys.executable}
import json
import os
import sys

with open(os.environ["COMMAND_LOG"], "ab") as command_log:
    command_log.write(
        json.dumps(
            {{
                "argv": sys.argv[1:],
                "cwd": os.getcwd(),
                "settings_module": os.environ.get("DJANGO_SETTINGS_MODULE"),
            }}
        ).encode()
        + b"\\0"
    )
""",
        )

    def recorded_commands(self):
        parts = self.command_log.read_bytes().split(b"\0")
        self.assertEqual(parts.pop(), b"")
        return [json.loads(part) for part in parts]

    def gunicorn_command(self):
        command = next(
            (
                command
                for command in self.recorded_commands()
                if command["argv"][:3] == ["-m", "gunicorn", "preceptor_project.wsgi:application"]
            ),
            None,
        )
        self.assertIsNotNone(command)
        arguments = command["argv"][3:]
        command["options"] = {}
        valued_options = {
            "--access-logfile",
            "--bind",
            "--error-logfile",
            "--graceful-timeout",
            "--timeout",
            "--umask",
            "--workers",
        }
        position = 0
        while position < len(arguments):
            argument = arguments[position]
            option, separator, value = argument.partition("=")
            if option in valued_options and separator:
                command["options"][option] = value
            elif argument in valued_options:
                self.assertLess(position + 1, len(arguments))
                command["options"][argument] = arguments[position + 1]
                position += 1
            elif argument == "--capture-output":
                command["options"][option] = None
            position += 1
        self.assertFalse(
            any(
                argument == "-D" or argument == "--daemon" or argument.startswith("--daemon=")
                for argument in command["argv"]
            )
        )
        return command

    def readiness_command(self):
        command = next(
            (
                command
                for command in self.recorded_commands()
                if command["argv"][:2] == ["manage.py", "check_production"]
            ),
            None,
        )
        self.assertIsNotNone(command)
        self.assertTrue(all(argument.startswith("--") for argument in command["argv"][2:]))
        return command

    def run_launcher(self, launcher, **environment):
        return subprocess.run(
            [str(self.root / "scripts" / launcher)],
            cwd=Path.home(),
            env=os.environ
            | {
                "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
                "COMMAND_LOG": str(self.command_log),
                **environment,
            },
            text=True,
            capture_output=True,
            check=False,
        )

    def test_launchers_have_valid_shell_syntax(self):
        for launcher in (PRODUCTION_LAUNCHER, READINESS_LAUNCHER):
            with self.subTest(launcher=launcher):
                result = subprocess.run(["sh", "-n", str(launcher)], text=True, capture_output=True)

                self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_launcher_refuses_root(self):
        self.write_executable(self.fake_bin / "id", "#!/bin/sh\nprintf '0\\n'\n")

        result = self.run_launcher("start-production")

        self.assertEqual(result.returncode, 1)
        self.assertIn("unprivileged service user", result.stderr)

    def test_production_launcher_runs_foreground_gunicorn_with_bounded_overrides(self):
        self.create_virtual_environment()
        self.write_executable(self.fake_bin / "id", "#!/bin/sh\nprintf '1000\\n'\n")

        result = self.run_launcher(
            "start-production",
            DJANGO_SETTINGS_MODULE="preceptor_project.settings",
            PRECEPTOR_GUNICORN_WORKERS="3",
            PRECEPTOR_GUNICORN_TIMEOUT="90",
            PRECEPTOR_GUNICORN_GRACEFUL_TIMEOUT="45",
            PRECEPTOR_GUNICORN_SOCKET="/run/muriel/proxy.sock",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        command = self.gunicorn_command()
        self.assertEqual(command["cwd"], str(self.root))
        self.assertEqual(command["settings_module"], "preceptor_project.production_settings")
        self.assertEqual(command["options"]["--bind"], "unix:/run/muriel/proxy.sock")
        self.assertEqual(command["options"]["--workers"], "3")
        self.assertEqual(command["options"]["--timeout"], "90")
        self.assertEqual(command["options"]["--graceful-timeout"], "45")
        self.assertEqual(command["options"]["--umask"], "0o007")
        self.assertEqual(command["options"]["--access-logfile"], "-")
        self.assertEqual(command["options"]["--error-logfile"], "-")
        self.assertIn("--capture-output", command["options"])

    def test_production_launcher_rejects_unbounded_worker_configuration(self):
        self.create_virtual_environment()
        self.write_executable(self.fake_bin / "id", "#!/bin/sh\nprintf '1000\\n'\n")

        result = self.run_launcher("start-production", PRECEPTOR_GUNICORN_WORKERS="5")

        self.assertEqual(result.returncode, 1)
        self.assertIn("between 1 and 4", result.stderr)

    def test_staging_launcher_uses_its_fixed_loopback_port_and_settings(self):
        self.create_virtual_environment()
        self.write_executable(self.fake_bin / "id", "#!/bin/sh\nprintf '1000\\n'\n")

        result = self.run_launcher(
            "start-production",
            PRECEPTOR_DJANGO_SETTINGS_MODULE="preceptor_project.staging_settings",
            PRECEPTOR_GUNICORN_BIND="127.0.0.1:8082",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        command = self.gunicorn_command()
        self.assertEqual(command["cwd"], str(self.root))
        self.assertEqual(command["settings_module"], "preceptor_project.staging_settings")
        self.assertEqual(command["options"]["--bind"], "127.0.0.1:8082")
        self.assertEqual(command["options"]["--workers"], "2")
        self.assertEqual(command["options"]["--timeout"], "60")
        self.assertEqual(command["options"]["--graceful-timeout"], "30")
        self.assertEqual(command["options"]["--access-logfile"], "-")
        self.assertEqual(command["options"]["--error-logfile"], "-")
        self.assertIn("--capture-output", command["options"])

    def test_readiness_launcher_forces_production_settings(self):
        self.create_virtual_environment()

        result = self.run_launcher(
            "check-production", DJANGO_SETTINGS_MODULE="preceptor_project.settings"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        command = self.readiness_command()
        self.assertEqual(command["cwd"], str(self.root))
        self.assertEqual(command["settings_module"], "preceptor_project.production_settings")

    def test_development_launcher_directs_operators_to_the_production_runbook(self):
        launcher = (ROOT / "scripts" / "start-dev").read_text()

        self.assertIn("docs/production.md", launcher)


@override_settings(DEBUG=False, STORAGES=MANIFEST_STATICFILES_STORAGE)
class ProductionReadinessCommandTests(TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.static_root = Path(self.temporary_directory.name) / "staticfiles"
        self.collected_assets = {}
        for asset in REQUIRED_CONSUMER_STATIC_ASSETS:
            source_path = Path(asset)
            versioned_asset = str(
                source_path.with_name(f"{source_path.stem}.0123456789ab{source_path.suffix}")
            )
            self.collected_assets[asset] = versioned_asset
            asset_path = self.static_root / versioned_asset
            asset_path.parent.mkdir(parents=True, exist_ok=True)
            asset_path.touch()
        (self.static_root / "staticfiles.json").write_text(
            json.dumps({"paths": self.collected_assets, "version": "1.1", "hash": "test"})
        )

    def command(self):
        return "schedules.management.commands.check_production"

    @override_settings(DEBUG=False)
    def test_passes_deployment_check_database_migrations_and_collected_assets(self):
        with (
            override_settings(STATIC_ROOT=self.static_root),
            mock.patch(f"{self.command()}.call_command") as check,
            mock.patch(f"{self.command()}.MigrationExecutor") as executor_class,
        ):
            executor = executor_class.return_value
            executor.loader.graph.leaf_nodes.return_value = [("schedules", "0029")]
            executor.migration_plan.return_value = []

            call_command("check_production")

        self.assertIn(mock.call("check", "--deploy"), check.call_args_list)
        self.assertIn(
            mock.call([("schedules", "0029")]),
            executor.migration_plan.call_args_list,
        )

    @override_settings(DEBUG=False)
    def test_refuses_pending_migrations(self):
        with (
            override_settings(STATIC_ROOT=self.static_root),
            mock.patch(f"{self.command()}.call_command"),
            mock.patch(f"{self.command()}.MigrationExecutor") as executor_class,
        ):
            executor = executor_class.return_value
            executor.loader.graph.leaf_nodes.return_value = [("schedules", "0029")]
            executor.migration_plan.return_value = [mock.sentinel.pending]

            with self.assertRaisesMessage(CommandError, "Pending migrations"):
                call_command("check_production")

    @override_settings(DEBUG=False)
    def test_refuses_missing_collected_assets(self):
        for missing_asset in REQUIRED_CONSUMER_STATIC_ASSETS:
            with self.subTest(missing_asset=missing_asset):
                collected_asset = self.static_root / self.collected_assets[missing_asset]
                collected_asset.unlink()
                try:
                    with (
                        override_settings(STATIC_ROOT=self.static_root),
                        mock.patch(f"{self.command()}.call_command"),
                        mock.patch(f"{self.command()}.MigrationExecutor") as executor_class,
                    ):
                        executor = executor_class.return_value
                        executor.loader.graph.leaf_nodes.return_value = []
                        executor.migration_plan.return_value = []

                        with self.assertRaisesMessage(CommandError, missing_asset):
                            call_command("check_production")
                finally:
                    collected_asset.touch()

    @override_settings(DEBUG=True)
    def test_refuses_development_settings(self):
        with self.assertRaisesMessage(CommandError, "requires production settings"):
            call_command("check_production")

    def test_collectstatic_includes_application_and_django_admin_assets(self):
        with override_settings(STATIC_ROOT=self.static_root):
            call_command("collectstatic", interactive=False, verbosity=0)

        for asset in (*REQUIRED_CONSUMER_STATIC_ASSETS, "admin/css/base.css"):
            with self.subTest(asset=asset):
                versioned_asset = ManifestStaticFilesStorage(
                    location=self.static_root, base_url="/static/"
                ).stored_name(asset)
                self.assertNotEqual(versioned_asset, asset)
                self.assertTrue((self.static_root / versioned_asset).is_file())


class VersionedStaticAssetTests(TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.static_root = Path(self.temporary_directory.name) / "staticfiles"
        self.static_source = Path(self.temporary_directory.name) / "static"
        self.application_css = self.static_source / "schedules" / "application.css"
        self.application_css.parent.mkdir(parents=True)

    def collectstatic(self):
        call_command("collectstatic", interactive=False, verbosity=0)
        return ManifestStaticFilesStorage(location=self.static_root, base_url="/static/")

    def test_manifest_versions_template_assets_and_rejects_missing_entries(self):
        first_stylesheet = "body { color: navy; }\n"
        updated_stylesheet = "body { color: maroon; }\n"
        self.application_css.write_text(first_stylesheet)
        with override_settings(
            STATIC_ROOT=self.static_root,
            STATICFILES_DIRS=[self.static_source],
            STORAGES=MANIFEST_STATICFILES_STORAGE,
        ):
            first_storage = self.collectstatic()
            first_url = first_storage.url("schedules/application.css")
            self.assertEqual(
                first_url,
                self.collectstatic().url("schedules/application.css"),
            )
            first_response = self.client.get(reverse("sign-in"))

            self.application_css.write_text(updated_stylesheet)
            updated_storage = self.collectstatic()
            updated_url = updated_storage.url("schedules/application.css")
            updated_response = self.client.get(reverse("sign-in"))

            self.assertNotEqual(first_url, updated_url)
            self.assertTrue((self.static_root / updated_url.removeprefix("/static/")).is_file())
            self.assertEqual(
                (self.static_root / first_url.removeprefix("/static/")).read_text(),
                first_stylesheet,
            )
            self.assertEqual(
                (self.static_root / updated_url.removeprefix("/static/")).read_text(),
                updated_stylesheet,
            )
            with self.assertRaisesMessage(ValueError, "Missing staticfiles manifest entry"):
                updated_storage.url("schedules/missing.css")

            referenced_assets = set()
            for template in (ROOT / "schedules" / "templates").rglob("*.html"):
                referenced_assets.update(
                    re.findall(r"\{% static '([^']+)' %\}", template.read_text())
                )
            for asset in referenced_assets:
                with self.subTest(asset=asset):
                    versioned_asset = updated_storage.stored_name(asset)
                    self.assertNotEqual(versioned_asset, asset)
                    self.assertTrue((self.static_root / versioned_asset).is_file())

        for response, expected_url in (
            (first_response, first_url),
            (updated_response, updated_url),
        ):
            with self.subTest(expected_url=expected_url):
                self.assertEqual(response.status_code, 200)
                parser = _RenderedSurfaceParser()
                parser.feed(response.content.decode())
                stylesheet_links = [
                    element
                    for element in parser.elements
                    if element.tag == "link"
                    and element.attributes.get("rel") == "stylesheet"
                    and element.attributes.get("href", "").startswith(
                        "/static/schedules/application."
                    )
                    and element.attributes["href"].endswith(".css")
                ]
                self.assertEqual(len(stylesheet_links), 1)
                self.assertEqual(
                    stylesheet_links[0].attributes["href"],
                    expected_url,
                )
