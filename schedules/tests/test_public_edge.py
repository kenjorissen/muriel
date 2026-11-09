import io
import ipaddress
import os
import re
import shutil
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings

from . import test_host_bootstrap

ROOT = Path(__file__).resolve().parents[2]
EDGE_PROBE = ROOT / "ops" / "check-public-edge"
RUNBOOK = ROOT / "docs" / "public-edge.md"
PUBLIC_HOST = "funnel.example.test"
CLOUDFLARE_HOST = "edge.example.test"
ADMIN_SERVE_PORT = 8443
PROBE_STYLESHEET_PATH = "/static/schedules/application-probe-a1b2c3d4.css"


def probe_environment(primary="funnel.example.test", secondary="edge.example.test"):
    return {
        "PRECEPTOR_PRODUCTION_ALLOWED_HOST": primary,
        "PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN": f"https://{primary}",
        "PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST": secondary,
        "PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN": f"https://{secondary}",
    }


def load_edge_probe():
    loader = SourceFileLoader("check_public_edge", str(EDGE_PROBE))
    specification = spec_from_loader(loader.name, loader)
    assert specification is not None
    probe = module_from_spec(specification)
    loader.exec_module(probe)
    return probe


class ProbeWireConnection:
    def __init__(self, boundary, address, timeout):
        self.boundary = boundary
        self.address = address
        self.timeout = timeout
        self.chunks = []

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def settimeout(self, timeout):
        self.boundary.socket_timeouts.append(timeout)

    def sendall(self, payload):
        self.chunks = self.boundary.respond(self.address, payload)

    def recv(self, unused_size):
        if self.chunks:
            return self.chunks.pop(0)
        return b""


class ProbeWireBoundary:
    """A bounded local socket peer for the executable public-edge probe."""

    def __init__(
        self,
        *,
        funnel_statuses=None,
        cloudflare_statuses=None,
        sign_in_statuses=None,
        malformed_path=None,
        socket_error=None,
    ):
        self.funnel_statuses = funnel_statuses or {}
        self.cloudflare_statuses = cloudflare_statuses or {}
        self.expected_sign_in_statuses = sign_in_statuses
        self.malformed_path = malformed_path
        self.socket_error = socket_error
        self.requests = []
        self.connection_timeouts = []
        self.socket_timeouts = []
        self.sign_in_statuses = []

    def create_connection(self, address, *, timeout):
        self.connection_timeouts.append(timeout)
        if self.socket_error:
            raise self.socket_error
        return ProbeWireConnection(self, address, timeout)

    def respond(self, address, payload):
        request_start = 0
        if address == ("127.0.0.1", 8080):
            request_start = 16 + int.from_bytes(payload[14:16], "big")
        request_line, header_lines = payload[request_start:].split(b"\r\n", 1)
        method, path, version = request_line.decode("ascii").split(" ")
        headers = {
            name.decode("ascii").casefold(): value.lstrip().decode("ascii")
            for line in header_lines.split(b"\r\n")
            if line
            for name, value in [line.split(b":", 1)]
        }
        request = {
            "address": address,
            "payload": payload,
            "method": method,
            "path": path,
            "version": version,
            "headers": headers,
        }
        self.requests.append(request)

        if path == self.malformed_path:
            return [b"not an HTTP", b" response"]

        status, body = self.response_for(address, path)
        request["status"] = status
        response = (
            f"HTTP/1.1 {status} test response\r\nContent-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        return [response[:7], response[7:23], response[23:]]

    def response_for(self, address, path):
        if address == ("127.0.0.1", 8080):
            if path == "/sign-in/":
                if self.expected_sign_in_statuses is not None:
                    status = self.expected_sign_in_statuses[len(self.sign_in_statuses)]
                    self.sign_in_statuses.append(status)
                else:
                    status = self.funnel_statuses.get(path)
                if status is None:
                    self.sign_in_statuses.append(200 if len(self.sign_in_statuses) < 6 else 429)
                    status = self.sign_in_statuses[-1]
                elif self.expected_sign_in_statuses is None:
                    self.sign_in_statuses.append(status)
                if len(self.sign_in_statuses) == 1 and status == 200:
                    return status, (
                        f'<link rel="stylesheet" href="{PROBE_STYLESHEET_PATH}">'.encode()
                    )
                return status, b""
            return self.funnel_statuses.get(
                path,
                {"/health/": 200, "/admin/": 404, "/.env": 403, PROBE_STYLESHEET_PATH: 200}[path],
            ), b""
        return self.cloudflare_statuses.get(
            path, {"/health/": 200, "/admin/": 404, "/.env": 403}[path]
        ), b""


class PublicEdgeConfigurationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Consume installed bytes from the real bootstrap, including its renderer.
        # Only privileged host commands are isolated by the owning bootstrap fixture.
        fixture = test_host_bootstrap.UbuntuHostBootstrapTests()
        cls.addClassCleanup(fixture.doCleanups)
        fixture.setUp()
        result = fixture.run_bootstrap("--apply")
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.nginx_root = fixture.fake_root / "etc/nginx"

    def setUp(self):
        self.site = self.without_comments((self.nginx_root / "sites-available/muriel").read_text())
        snippets = self.nginx_root / "snippets"
        self.proxy = self.without_comments((snippets / "muriel-proxy.conf").read_text())
        self.public_app = self.without_comments((snippets / "muriel-public-app.conf").read_text())
        self.admin_proxy = self.without_comments((snippets / "muriel-admin-proxy.conf").read_text())

    @staticmethod
    def without_comments(configuration):
        return "\n".join(line.split("#", 1)[0] for line in configuration.splitlines())

    @staticmethod
    def block_body(configuration, opening_brace):
        depth = 0
        for index, character in enumerate(configuration[opening_brace:], start=opening_brace):
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    return configuration[opening_brace + 1 : index]
        raise AssertionError("unclosed nginx configuration block")

    def server_with(self, directive):
        for server in re.finditer(r"server\s*\{", self.site):
            body = self.block_body(self.site, server.end() - 1)
            if directive in body:
                return body
        raise AssertionError(f"no nginx server contains {directive!r}")

    @staticmethod
    def trusted_peers(server):
        return re.findall(r"^\s*set_real_ip_from\s+([^;]+);", server, re.MULTILINE)

    def location_with(self, configuration, directive):
        location = re.search(rf"location\s+{re.escape(directive)}\s*\{{", configuration)
        assert location is not None
        return self.block_body(configuration, location.end() - 1)

    def test_public_listener_is_loopback_proxy_v2_and_accepts_only_the_funnel_host(self):
        funnel = self.server_with("listen 127.0.0.1:8080 proxy_protocol;")
        default_funnel = self.server_with("listen 127.0.0.1:8080 proxy_protocol default_server;")

        self.assertIn("listen 127.0.0.1:8080 proxy_protocol;", funnel)
        self.assertIn("listen [::1]:8080 proxy_protocol;", funnel)
        self.assertIn(f"server_name {PUBLIC_HOST};", funnel)
        self.assertNotIn("listen 0.0.0.0", self.site)
        self.assertNotIn("listen 8080", self.site)
        self.assertIn("real_ip_header proxy_protocol;", funnel)
        self.assertCountEqual(self.trusted_peers(funnel), ["127.0.0.1", "::1"])
        self.assertIn("include /etc/nginx/snippets/muriel-public-app.conf;", funnel)
        self.assertIn("listen [::1]:8080 proxy_protocol default_server;", default_funnel)
        self.assertIn("return 444;", default_funnel)

    def test_cloudflare_listener_is_plain_http_loopback_and_trusts_only_its_local_peer(self):
        cloudflare = self.server_with("listen 127.0.0.1:8083;")
        default_cloudflare = self.server_with("listen 127.0.0.1:8083 default_server;")

        self.assertIn("listen 127.0.0.1:8083;", cloudflare)
        self.assertIn("listen [::1]:8083;", cloudflare)
        self.assertIn(f"server_name {CLOUDFLARE_HOST};", cloudflare)
        self.assertIn("real_ip_header CF-Connecting-IP;", cloudflare)
        self.assertCountEqual(self.trusted_peers(cloudflare), ["127.0.0.1", "::1"])
        self.assertIn("include /etc/nginx/snippets/muriel-public-app.conf;", cloudflare)
        self.assertIn("listen [::1]:8083 default_server;", default_cloudflare)
        self.assertIn("return 444;", default_cloudflare)

    def test_proxy_chain_is_replaced_not_taken_from_the_caller(self):
        for directive in (
            "proxy_set_header Host $host;",
            "proxy_set_header X-Forwarded-Proto https;",
            "proxy_set_header X-Forwarded-For $remote_addr;",
            "proxy_set_header X-Real-IP $remote_addr;",
            "proxy_connect_timeout 5s;",
            "proxy_send_timeout 30s;",
            "proxy_read_timeout 65s;",
        ):
            self.assertIn(directive, self.proxy)
        self.assertNotIn("$proxy_add_x_forwarded_for", self.site + self.proxy)

    def test_public_paths_are_bounded_and_sensitive_requests_return_429(self):
        for directive in (
            "client_max_body_size 11m;",
            "client_body_timeout 10s;",
            "client_header_timeout 10s;",
            "keepalive_timeout 15s;",
            "send_timeout 30s;",
            "limit_req_zone $binary_remote_addr zone=muriel_public:10m rate=30r/s;",
            "limit_req_zone $binary_remote_addr zone=muriel_sensitive:10m rate=1r/s;",
            "limit_conn_zone $binary_remote_addr zone=muriel_per_client:10m;",
            "limit_conn muriel_per_client 20;",
            "limit_conn_status 429;",
            "limit_req_status 429;",
        ):
            self.assertIn(directive, self.site + self.public_app)
        for path in ("/sign-in/", "/password/reset/", "/invitation/"):
            location = self.location_with(
                self.public_app, f"{'^~ ' if path == '/invitation/' else '= '}{path}"
            )
            self.assertIn("limit_req zone=muriel_sensitive burst=5 nodelay;", location)
        self.assertIn("location ^~ /static/", self.public_app)
        self.assertIn("location ~ /\\.", self.public_app)
        self.assertIn("location ^~ /admin/", self.public_app)
        self.assertIn("return 404;", self.public_app)

    def test_admin_listener_is_separate_loopback_without_proxy_protocol(self):
        admin = self.server_with("listen 127.0.0.1:8081 ssl;")
        self.assertIn("listen 127.0.0.1:8081 ssl;", admin)
        self.assertIn("listen [::1]:8081 ssl;", admin)
        self.assertIn(f"server_name {PUBLIC_HOST};", admin)
        self.assertIn("ssl_certificate /etc/nginx/tailscale/muriel.crt;", admin)
        self.assertIn("ssl_certificate_key /etc/nginx/tailscale/muriel.key;", admin)
        self.assertNotIn("proxy_protocol", admin)
        for directive in (
            f"proxy_set_header Host {PUBLIC_HOST}:{ADMIN_SERVE_PORT};",
            "proxy_set_header X-Forwarded-Proto https;",
            "proxy_set_header X-Forwarded-For $remote_addr;",
            "proxy_set_header X-Real-IP $remote_addr;",
            "proxy_set_header X-Muriel-Admin 1;",
        ):
            self.assertIn(directive, self.admin_proxy)
        self.assertIn('proxy_set_header X-Muriel-Admin "";', self.proxy)

    def test_admin_listener_serves_static_files_without_proxying_them(self):
        admin = self.server_with("listen 127.0.0.1:8081 ssl;")
        static = self.location_with(admin, "^~ /static/")
        public_static = self.location_with(self.public_app, "^~ /static/")

        self.assertIn("alias /srv/muriel/current/staticfiles/;", static)
        self.assertIn("access_log off;", static)
        self.assertIn("expires 7d;", static)
        self.assertIn("try_files $uri =404;", static)
        self.assertNotIn("proxy_pass", static)
        self.assertIn("alias /srv/muriel/current/staticfiles/;", public_static)

    def run_probe(self, boundary, *arguments, environment=None):
        probe = load_edge_probe()
        with (
            patch.dict(
                os.environ, probe_environment() if environment is None else environment, clear=True
            ),
            patch.object(probe.socket, "create_connection", boundary.create_connection),
            patch.object(sys, "argv", [str(EDGE_PROBE), *arguments]),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            try:
                probe.main()
            except SystemExit as error:
                self.probe_diagnostic = stderr.getvalue()
                return error.code
        return 0

    def test_probe_executes_proxy_v2_and_bounded_sensitive_limit_over_local_wire(self):
        boundary = ProbeWireBoundary()

        self.assertEqual(self.run_probe(boundary), 0)

        funnel_requests = [
            request for request in boundary.requests if request["address"] == ("127.0.0.1", 8080)
        ]
        cloudflare_requests = [
            request for request in boundary.requests if request["address"] == ("127.0.0.1", 8083)
        ]
        self.assertEqual(
            [request["status"] for request in funnel_requests if request["path"] == "/sign-in/"],
            [200, 200, 200, 200, 200, 200, 429],
        )
        self.assertTrue(
            {"/health/", "/sign-in/", PROBE_STYLESHEET_PATH, "/admin/", "/.env"}
            <= {request["path"] for request in funnel_requests}
        )
        self.assertTrue(
            {"/health/", "/admin/", "/.env"} <= {request["path"] for request in cloudflare_requests}
        )

        source_addresses = set()
        for request in funnel_requests:
            frame = request["payload"]
            self.assertEqual(frame[:12], b"\r\n\r\n\x00\r\nQUIT\n")
            self.assertEqual(frame[12], 0x21)
            self.assertEqual(frame[13], 0x11)
            header_length = int.from_bytes(frame[14:16], "big")
            self.assertEqual(header_length, 12)
            source_addresses.add(str(ipaddress.ip_address(frame[16:20])))
            self.assertEqual(str(ipaddress.ip_address(frame[20:24])), "127.0.0.1")
            self.assertEqual(int.from_bytes(frame[26:28], "big"), 8080)
            self.assertTrue(frame[16 + header_length :].startswith(b"GET "))
            self.assertEqual(request["method"], "GET")
            self.assertEqual(request["version"], "HTTP/1.1")
            self.assertEqual(request["headers"]["host"], "funnel.example.test")
            self.assertEqual(request["headers"]["connection"], "close")
        self.assertEqual(len(source_addresses), 1)
        self.assertEqual(
            {request["headers"]["cf-connecting-ip"] for request in cloudflare_requests},
            source_addresses,
        )
        self.assertEqual(
            {request["headers"]["host"] for request in cloudflare_requests}, {"edge.example.test"}
        )
        for request in cloudflare_requests:
            self.assertEqual(request["method"], "GET")
            self.assertEqual(request["version"], "HTTP/1.1")
            self.assertEqual(request["headers"]["connection"], "close")
        self.assertTrue(boundary.connection_timeouts)
        self.assertTrue(boundary.socket_timeouts)
        self.assertTrue(all(timeout == 5 for timeout in boundary.connection_timeouts))
        self.assertTrue(all(timeout == 5 for timeout in boundary.socket_timeouts))

    def test_configured_hosts_select_headers_not_destinations_and_are_captured_once(self):
        for primary, secondary in (
            ("funnel.example.test", "edge.example.test"),
            ("fallback.other.test", "public.other.test"),
        ):
            with self.subTest(primary=primary):
                boundary = ProbeWireBoundary()
                connect = boundary.create_connection

                def change_environment_after_validation(address, *, timeout, connect=connect):
                    os.environ.update(probe_environment("changed.test", "changed-secondary.test"))
                    return connect(address, timeout=timeout)

                boundary.create_connection = change_environment_after_validation
                self.assertEqual(
                    self.run_probe(
                        boundary,
                        "--timeout",
                        "2.5",
                        environment=probe_environment(primary, secondary),
                    ),
                    0,
                )
                self.assertEqual(
                    {
                        (request["address"], request["headers"]["host"])
                        for request in boundary.requests
                    },
                    {(("127.0.0.1", 8080), primary), (("127.0.0.1", 8083), secondary)},
                )
                self.assertTrue(all(timeout == 2.5 for timeout in boundary.connection_timeouts))
                self.assertTrue(all(timeout == 2.5 for timeout in boundary.socket_timeouts))

    def test_invalid_configuration_fails_before_any_socket_without_echoing_values(self):
        invalid_values = (
            None,
            "",
            " padded.test",
            "padded.test ",
            "*.test",
            "user@host.test",
            "host.test/path",
            "host.test?query",
            "host.test#fragment",
            "host.test:443",
            "host.test\r\nInjected: secret",
            "host\t.test",
            "host\n.test",
            "host\x01.test",
            "host\x7f.test",
            "[broken",
            "bad..test",
            "-bad.test",
            "bad-.test",
            "é.test",
            "a" * 64 + ".test",
            ".".join(["a" * 63] * 4),
        )
        for setting in probe_environment():
            for value in invalid_values:
                with self.subTest(setting=setting, value=value):
                    environment = probe_environment()
                    if value is None:
                        del environment[setting]
                    else:
                        environment[setting] = value
                    boundary = ProbeWireBoundary()
                    self.assertEqual(self.run_probe(boundary, environment=environment), 1)
                    self.assertEqual(boundary.connection_timeouts, [])
                    self.assertIn(setting, self.probe_diagnostic)
                    self.assertNotIn("Injected", self.probe_diagnostic)
                    self.assertNotIn("Traceback", self.probe_diagnostic)

        for primary, secondary in (("same.test", "same.test"), ("same.test", "SAME.test")):
            boundary = ProbeWireBoundary()
            self.assertEqual(
                self.run_probe(boundary, environment=probe_environment(primary, secondary)), 1
            )
            self.assertEqual(boundary.connection_timeouts, [])

        for setting in (
            "PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN",
            "PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN",
        ):
            for origin in (
                "http://edge.example.test",
                "https://different.test",
                "https://[broken",
                "https://edge.example.test/",
                "https://edge.example.test:443",
            ):
                boundary = ProbeWireBoundary()
                self.assertEqual(
                    self.run_probe(boundary, environment=probe_environment() | {setting: origin}), 1
                )
                self.assertEqual(boundary.connection_timeouts, [])

    def test_help_needs_no_configuration_or_socket(self):
        boundary = ProbeWireBoundary()
        self.assertEqual(self.run_probe(boundary, "--help", environment={}), 0)
        self.assertEqual(boundary.connection_timeouts, [])

    def test_probe_rejects_an_unbounded_timeout_before_socket_connection(self):
        boundary = ProbeWireBoundary()

        for timeout in ("0", "-1", "nan", "inf", "11"):
            with self.subTest(timeout=timeout):
                self.assertEqual(self.run_probe(boundary, "--timeout", timeout), 2)
        self.assertEqual(boundary.connection_timeouts, [])

    def test_probe_fails_for_unready_or_malformed_local_wire_responses(self):
        cases = (
            (ProbeWireBoundary(funnel_statuses={"/health/": 503}), "unready health response"),
            (ProbeWireBoundary(sign_in_statuses=[503]), "unready sign-in render"),
            (ProbeWireBoundary(funnel_statuses={PROBE_STYLESHEET_PATH: 503}), "unready stylesheet"),
            (ProbeWireBoundary(funnel_statuses={"/admin/": 200}), "public admin response"),
            (ProbeWireBoundary(funnel_statuses={"/.env": 404}), "dotfile response"),
            (
                ProbeWireBoundary(sign_in_statuses=[200, 429]),
                "premature sensitive limit response",
            ),
            (
                ProbeWireBoundary(sign_in_statuses=[200, 200, 200, 200, 200, 200, 200]),
                "missing final sensitive limit response",
            ),
            (
                ProbeWireBoundary(cloudflare_statuses={"/health/": 503}),
                "Cloudflare health response",
            ),
            (
                ProbeWireBoundary(cloudflare_statuses={"/admin/": 200}),
                "Cloudflare admin response",
            ),
            (
                ProbeWireBoundary(cloudflare_statuses={"/.env": 404}),
                "Cloudflare dotfile response",
            ),
            (ProbeWireBoundary(malformed_path="/health/"), "malformed HTTP response"),
            (ProbeWireBoundary(socket_error=OSError("connection refused")), "socket error"),
        )

        for boundary, case in cases:
            with self.subTest(case=case):
                self.assertEqual(self.run_probe(boundary), 1)

    def test_runbook_uses_persistent_public_funnel_and_private_admin_serve(self):
        runbook = RUNBOOK.read_text()

        self.assertIn(
            "tailscale funnel --bg --tls-terminated-tcp=443 --proxy-protocol=2 "
            "tcp://127.0.0.1:8080",
            runbook,
        )
        self.assertIn("tailscale funnel --tls-terminated-tcp=443 --proxy-protocol=2 off", runbook)
        self.assertIn("tailscale funnel status", runbook)
        self.assertIn("tailscale serve status --json", runbook)
        self.assertIn("tailscale serve --bg --https=8443 https+insecure://127.0.0.1:8081", runbook)
        self.assertIn("tailscale serve --https=8443 off", runbook)
        self.assertIn("sudo tailscale serve status", runbook)
        self.assertIn("http://127.0.0.1:8083", runbook)
        self.assertIn(f"https://{CLOUDFLARE_HOST}/health/", runbook)
        self.assertIn(f"https://{PUBLIC_HOST}:{ADMIN_SERVE_PORT}/admin/", runbook)
        self.assertIn("tailnet clients permitted by the tailnet ACLs", runbook)
        self.assertIn("Django Admin still requires a Django superuser login", runbook)
        self.assertNotIn("ssh -N -L", runbook)

    def nginx_syntax_result(self, proxy):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            snippets = temporary / "snippets"
            snippets.mkdir()
            proxy_path = snippets / "muriel-proxy.conf"
            proxy_path.write_text(proxy)
            public_app_path = snippets / "muriel-public-app.conf"
            public_app_path.write_text(
                self.public_app.replace("/etc/nginx/snippets/muriel-proxy.conf", str(proxy_path))
            )
            admin_proxy_path = snippets / "muriel-admin-proxy.conf"
            admin_proxy_path.write_text(self.admin_proxy)
            certificates = temporary / "tailscale"
            certificates.mkdir()
            certificate_path = certificates / "muriel.crt"
            key_path = certificates / "muriel.key"
            certificate_result = subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(key_path),
                    "-out",
                    str(certificate_path),
                    "-subj",
                    "/CN=localhost",
                    "-days",
                    "1",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(certificate_result.returncode, 0, certificate_result.stderr)
            temporary_paths = {
                "client_body_temp_path": temporary / "client-body",
                "proxy_temp_path": temporary / "proxy",
                "fastcgi_temp_path": temporary / "fastcgi",
                "uwsgi_temp_path": temporary / "uwsgi",
                "scgi_temp_path": temporary / "scgi",
            }
            for path in temporary_paths.values():
                path.mkdir()
            site = (
                self.site.replace("/etc/nginx/snippets/muriel-proxy.conf", str(proxy_path))
                .replace("/etc/nginx/snippets/muriel-public-app.conf", str(public_app_path))
                .replace("/etc/nginx/snippets/muriel-admin-proxy.conf", str(admin_proxy_path))
                .replace("/etc/nginx/tailscale/muriel.crt", str(certificate_path))
                .replace("/etc/nginx/tailscale/muriel.key", str(key_path))
                .replace("127.0.0.1:8080", "127.0.0.1:18080")
                .replace("[::1]:8080", "[::1]:18080")
                .replace("127.0.0.1:8081", "127.0.0.1:18081")
                .replace("[::1]:8081", "[::1]:18081")
                .replace("127.0.0.1:8083", "127.0.0.1:18083")
                .replace("[::1]:8083", "[::1]:18083")
            )
            site_path = temporary / "muriel.conf"
            site_path.write_text(site)
            configuration = temporary / "nginx.conf"
            configuration.write_text(
                f"pid {temporary / 'nginx.pid'};\n"
                f"error_log {temporary / 'nginx-error.log'};\n"
                f"events {{}}\nhttp {{\n"
                f"access_log {temporary / 'nginx-access.log'};\n"
                + "".join(f"{directive} {path};\n" for directive, path in temporary_paths.items())
                + f"include {site_path};\n}}\n"
            )

            return subprocess.run(
                ["nginx", "-t", "-p", directory, "-c", str(configuration)],
                text=True,
                capture_output=True,
                check=False,
            )

    @skipUnless(
        shutil.which("nginx") and shutil.which("openssl"),
        "nginx and openssl are required for this test",
    )
    def test_nginx_accepts_the_site_syntax(self):
        result = self.nginx_syntax_result(self.proxy)

        self.assertEqual(result.returncode, 0, result.stderr)

    @skipUnless(
        shutil.which("nginx") and shutil.which("openssl"),
        "nginx and openssl are required for this test",
    )
    def test_nginx_rejects_invalid_nested_proxy_syntax(self):
        result = self.nginx_syntax_result(f"{self.proxy}\ninvalid_nested_directive;")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid_nested_directive", result.stderr)


@override_settings(
    ALLOWED_HOSTS=[PUBLIC_HOST],
    CSRF_TRUSTED_ORIGINS=[f"https://{PUBLIC_HOST}"],
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
    MIDDLEWARE=[
        "preceptor_project.production_proxy.UnixSocketProxySSLHeaderMiddleware",
        *settings.MIDDLEWARE,
    ],
)
class PrivateAdminTransportTests(TestCase):
    def test_network_callers_cannot_spoof_the_private_admin_header(self):
        for remote_address in ("127.0.0.1", "203.0.113.17"):
            with self.subTest(remote_address=remote_address):
                response = Client().get(
                    "/admin/",
                    HTTP_HOST=f"{PUBLIC_HOST}:{ADMIN_SERVE_PORT}",
                    HTTP_X_FORWARDED_PROTO="https",
                    HTTP_X_MURIEL_ADMIN="1",
                    REMOTE_ADDR=remote_address,
                )

                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], "/sign-in/?next=/admin/")

    def test_private_serve_headers_allow_admin_login_and_a_secure_session(self):
        password = "private-admin-password"
        administrator = get_user_model().objects.create_superuser(
            username="administrator",
            email="administrator@example.com",
            password=password,
        )
        client = Client(enforce_csrf_checks=True)
        authority = f"{PUBLIC_HOST}:{ADMIN_SERVE_PORT}"
        origin = f"https://{authority}"
        transport = {
            "HTTP_HOST": authority,
            "HTTP_X_FORWARDED_PROTO": "https",
            "HTTP_X_MURIEL_ADMIN": "1",
            "REMOTE_ADDR": "",
        }

        initial_admin = client.get("/admin/", **transport)
        self.assertEqual(initial_admin.status_code, 302)
        self.assertEqual(initial_admin["Location"], "/admin/login/?next=/admin/")

        login = client.get("/admin/login/?next=/admin/", **transport)
        self.assertEqual(login.status_code, 200)
        csrf_token = login.cookies["csrftoken"].value
        response = client.post(
            "/admin/login/?next=/admin/",
            {
                "username": administrator.username,
                "password": password,
                "csrfmiddlewaretoken": csrf_token,
            },
            HTTP_ORIGIN=origin,
            HTTP_REFERER=f"{origin}/admin/login/?next=/admin/",
            **transport,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/admin/")
        self.assertTrue(response.cookies["sessionid"]["secure"])
        self.assertEqual(client.get("/admin/", **transport).status_code, 200)
