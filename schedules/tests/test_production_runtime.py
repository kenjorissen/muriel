import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from select import select
from unittest import mock

from django.contrib.staticfiles.storage import ManifestStaticFilesStorage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from schedules.tests.test_production_settings import PRODUCTION_ENVIRONMENT
from schedules.tests.test_staging_instance import STAGING_ENVIRONMENT
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
            "--forwarded-allow-ips",
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
        self.assertEqual(command["options"]["--forwarded-allow-ips"], "")
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

    @contextmanager
    def proxy_server(self, *, staging=False, inherited_trust=False):
        """Run captured launcher policy with only disposable listener/app overrides.

        Gunicorn owns header parsing; real deployment settings and middleware own
        Django's interpretation. No database, static deployment or live port is used.
        """
        self.create_virtual_environment()
        self.write_executable(self.fake_bin / "id", "#!/bin/sh\nprintf '1000\\n'\n")
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("PRECEPTOR_")
            and name not in {"FORWARDED_ALLOW_IPS", "GUNICORN_CMD_ARGS"}
        }
        if inherited_trust:
            environment |= {
                "FORWARDED_ALLOW_IPS": "*",
                "GUNICORN_CMD_ARGS": "--forwarded-allow-ips=*",
            }
        launcher_environment = (
            {
                "PRECEPTOR_DJANGO_SETTINGS_MODULE": "preceptor_project.staging_settings",
                "PRECEPTOR_GUNICORN_BIND": "127.0.0.1:8082",
            }
            if staging
            else {}
        )
        with mock.patch.dict(os.environ, environment, clear=True):
            result = self.run_launcher("start-production", **launcher_environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        command = self.gunicorn_command()
        fixture = self.root / "proxy_fixture"
        fixture.mkdir()
        static_root = fixture / "static"
        static_root.mkdir()
        (fixture / "proxy_settings.py").write_text(
            f"from {command['settings_module']} import *\n"
            "DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}\n"
            "ROOT_URLCONF = 'proxy_urls'\n"
            f"STATIC_ROOT = {str(static_root)!r}\n"
        )
        (fixture / "proxy_urls.py").write_text(
            "from django.http import JsonResponse\n"
            "from django.urls import path\n"
            "def probe(request):\n"
            "    return JsonResponse({\n"
            "        'secure': request.is_secure(),\n"
            "        'scheme': request.META['wsgi.url_scheme'],\n"
            "        'peer': request.META['REMOTE_ADDR'],\n"
            "        'admin': request.META.get('HTTP_X_MURIEL_ADMIN'),\n"
            "    })\n"
            "urlpatterns = [path('probe/', probe, name='health')]\n"
        )
        # A worker lifecycle hook signals readiness, not a sleep or log/PID match.
        config = fixture / "gunicorn.conf.py"
        config.write_text(
            "import json, os\n"
            "def post_worker_init(worker):\n"
            "    policy = {'ips': worker.cfg.forwarded_allow_ips,\n"
            "              'headers': worker.cfg.secure_scheme_headers}\n"
            "    os.write(int(os.environ['PROXY_READY_FD']), json.dumps(policy).encode())\n"
        )
        with (
            socket.socket() as tcp,
            socket.socket(socket.AF_UNIX) as unix,
            (fixture / "server.log").open("w+") as log,
        ):
            tcp.bind(("127.0.0.1", 0))
            tcp.listen()
            unix.bind(str(fixture / "gunicorn.sock"))
            unix.listen()
            ready_read, ready_write = os.pipe()
            try:
                server = subprocess.Popen(
                    [
                        sys.executable,
                        *command["argv"],
                        # Test-only socket activation avoids a port allocation race.
                        "--bind",
                        f"fd://{tcp.fileno()}",
                        "--bind",
                        f"fd://{unix.fileno()}",
                        "--workers",
                        "1",
                        "--config",
                        str(config),
                        "--pythonpath",
                        str(fixture),
                    ],
                    cwd=ROOT,
                    env=environment
                    | (STAGING_ENVIRONMENT if staging else PRODUCTION_ENVIRONMENT)
                    | {
                        "DJANGO_SETTINGS_MODULE": "proxy_settings",
                        "PROXY_READY_FD": str(ready_write),
                    },
                    start_new_session=True,
                    pass_fds=(tcp.fileno(), unix.fileno(), ready_write),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                try:
                    os.close(ready_write)
                    ready_write = None
                    readable, _, _ = select([ready_read], [], [], 20)
                    if not readable:
                        log.seek(0)
                        self.fail(f"Gunicorn worker readiness deadline exceeded:\n{log.read()}")
                    policy = os.read(ready_read, 65536)
                    if not policy:
                        log.seek(0)
                        self.fail(f"Gunicorn exited before readiness:\n{log.read()}")
                    yield json.loads(policy), tcp.getsockname(), str(fixture / "gunicorn.sock")
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        # This session contains only our test master and its worker.
                        os.killpg(server.pid, signal.SIGKILL)
                        server.wait(timeout=5)
                        log.seek(0)
                        self.fail(f"Gunicorn shutdown deadline exceeded:\n{log.read()}")
                    log.seek(0)
                    output = log.read()
                    self.assertEqual(server.returncode, 0, output)
                    self.assertNotIn("[ERROR]", output, output)
                    self.assertNotIn("Traceback", output, output)
            finally:
                os.close(ready_read)
                if ready_write is not None:
                    os.close(ready_write)

    def proxy_request(self, address, headers, *, staging=False):
        host = "staging.example.test:10000" if staging else "muriel.example"
        connection = HTTPConnection(host, timeout=5)
        try:
            if isinstance(address, str):
                connection.sock = socket.socket(socket.AF_UNIX)
                connection.sock.settimeout(5)
                connection.sock.connect(address)
            else:
                connection.sock = socket.create_connection(address, timeout=5)
            connection.request("GET", "/probe/?boundary=1", headers={"Host": host, **headers})
            response = connection.getresponse()
            body = response.read()
            return response.status, dict(response.getheaders()), body
        finally:
            connection.close()

    def test_real_gunicorn_production_rejects_tcp_scheme_headers_but_trusts_unix(self):
        self.assert_production_proxy_policy(inherited_trust=False)

    def test_real_gunicorn_production_trust_overrides_inherited_environment(self):
        self.assert_production_proxy_policy(inherited_trust=True)

    def assert_production_proxy_policy(self, *, inherited_trust):
        # Independent documented examples, with coverage checked against the running
        # server so a later Gunicorn upgrade cannot silently add an untested alias.
        scheme_headers = {
            "X-FORWARDED-PROTO": "https",
            "X-FORWARDED-PROTOCOL": "ssl",
            "X-FORWARDED-SSL": "on",
        }
        with self.proxy_server(inherited_trust=inherited_trust) as (policy, tcp, unix):
            self.assertEqual(policy["headers"], scheme_headers)
            for forwarded in (
                {},
                *({name: value} for name, value in scheme_headers.items()),
                scheme_headers,
            ):
                with self.subTest(headers=forwarded):
                    headers = forwarded | {"X-Muriel-Admin": "1"}
                    status, response_headers, body = self.proxy_request(tcp, headers)
                    self.assertEqual(status, 301, body)
                    self.assertEqual(
                        response_headers["Location"], "https://muriel.example/probe/?boundary=1"
                    )
                    status, _, body = self.proxy_request(unix, headers)
                    if forwarded:
                        self.assertEqual(status, 200, body)
                        self.assertEqual(
                            json.loads(body),
                            {"secure": True, "scheme": "https", "peer": "", "admin": "1"},
                        )
                    else:
                        self.assertEqual(status, 301)
            self.assertEqual(policy["ips"], [])

    def test_real_gunicorn_staging_preserves_loopback_https(self):
        with self.proxy_server(staging=True) as (policy, tcp, _):
            self.assertIn("127.0.0.1", policy["ips"])
            status, headers, _ = self.proxy_request(tcp, {}, staging=True)
            self.assertEqual(status, 301)
            self.assertEqual(
                headers["Location"], "https://staging.example.test:10000/probe/?boundary=1"
            )
            status, _, body = self.proxy_request(tcp, {"X-Forwarded-Proto": "https"}, staging=True)
            self.assertEqual(status, 200, body)
            self.assertEqual(
                json.loads(body),
                {"secure": True, "scheme": "https", "peer": "127.0.0.1", "admin": None},
            )

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
