import json
import os
import shutil
import stat
import subprocess
import tempfile
from datetime import UTC, date, datetime
from email.message import Message
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import BaseHandler, build_opener
from urllib.response import addinfourl

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.http import HttpResponse
from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
    tag,
)
from django.urls import reverse

from preceptor_project.security import StagingPerformanceProbeMiddleware
from schedules.models import LateDocAssignment, PreceptingSolverProposal, PreceptorAssignment
from schedules.staging_performance_probe import (
    PROBE_HEADER,
    PROBE_PROPOSAL_ID_HEADER,
    PROBE_TOKEN,
    STAGING_MARKER,
    ProbeError,
    StagingOrigin,
    StagingProbe,
    csrf_token,
    main,
    month_end,
    month_start,
    parse_server_timing,
    proposal_id_from_location,
    schedule_bot_feedback,
    shift_month,
    workload,
)
from schedules.views import solver

ROOT = Path(__file__).resolve().parents[2]
PROBE_WRAPPER = ROOT / "scripts" / "probe-staging-performance"
TEST_ORIGIN = StagingOrigin("probe.example.test", "https://probe.example.test:10000")


class StagingPerformanceProbeWrapperTests(SimpleTestCase):
    def test_archive_release_uses_its_local_virtualenv_without_git_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            release = Path(temporary_directory) / "release"
            wrapper = release / "scripts" / "probe-staging-performance"
            python = release / ".venv" / "bin" / "python"
            wrapper.parent.mkdir(parents=True)
            python.parent.mkdir(parents=True)
            shutil.copy2(PROBE_WRAPPER, wrapper)
            python.write_text(
                "#!/bin/sh\n"
                'printf \'%s\\n\' "$PWD" > "$PROBE_CWD"\n'
                'printf \'%s\\n\' "$@" > "$PROBE_ARGS"\n'
            )
            python.chmod(python.stat().st_mode | stat.S_IXUSR)
            cwd = release / "cwd"
            cwd.mkdir()
            probe_cwd = release / "probe-cwd"
            probe_args = release / "probe-args"

            result = subprocess.run(
                [str(wrapper), "--anchor-month", "2026-08"],
                cwd=cwd,
                env=os.environ | {"PROBE_CWD": str(probe_cwd), "PROBE_ARGS": str(probe_args)},
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertFalse((release / ".git").exists())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(probe_cwd.read_text().strip(), str(release))
            self.assertEqual(
                probe_args.read_text().splitlines(),
                ["-m", "schedules.staging_performance_probe", "--anchor-month", "2026-08"],
            )


class _ProbeTransport(BaseHandler):
    """Replace only transport; keep urllib request/response/redirect processors."""

    def __init__(self, respond):
        self.respond = respond
        self.requests = []

    def default_open(self, request):
        self.requests.append(request)
        status, body, headers = self.respond(request)
        message = Message()
        for name, value in headers.items():
            message[name] = value
        response = addinfourl(BytesIO(body), message, request.full_url, status)
        response.msg = "Offline response"
        return response


class StagingPerformanceProbeConfigurationTests(SimpleTestCase):
    def test_invalid_configuration_refuses_before_password_or_transport_setup(self):
        host = TEST_ORIGIN.host
        origin = TEST_ORIGIN.url
        invalid_pairs = [(None, None), (None, origin), (host, None), ("", origin), (host, "")]
        invalid_pairs += [
            (invalid_host, f"https://{invalid_host}:10000")
            for invalid_host in (
                "*.example.test",
                ".example.test",
                "probe.example.test:10000",
                "https://probe.example.test",
                "user@probe.example.test",
                "probe.example.test/path",
                "probe.example.test?query",
                "probe.example.test#fragment",
                "probe.example.test,other.example.test",
                "probe.example.test\\other",
                "probe.example.test\x01",
                "probe.example.test\x7f",
                " probe.example.test",
                "probe.example.test ",
                "probe. example.test",
                "probe.example.test\n",
                "probe.example.test\t",
                "[::1]",
                "PROBE.example.test",
            )
        ]
        invalid_pairs += [
            (host, invalid_origin)
            for invalid_origin in (
                "https://other.example.test:10000",
                "http://probe.example.test:10000",
                "https://probe.example.test",
                "https://probe.example.test:443",
                "https://probe.example.test:10001",
                "https://probe.example.test:invalid",
                "https://probe.example.test:99999",
                "https://[probe.example.test:10000",
                "https://user:secret@probe.example.test:10000",
                origin + "/",
                origin + "/path",
                origin + "?",
                origin + "#",
                origin + "?query",
                origin + "#fragment",
                origin + "\n",
                origin + "\t",
                origin + "\x01",
                origin + "\x7f",
                origin + " ",
                " " + origin,
            )
        ]
        for configured_host, configured_origin in invalid_pairs:
            with self.subTest(host=configured_host, origin=configured_origin):
                environment = {
                    key: value
                    for key, value in (
                        ("PRECEPTOR_STAGING_ALLOWED_HOST", configured_host),
                        ("PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN", configured_origin),
                    )
                    if value is not None
                }
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch("schedules.staging_performance_probe.getpass") as password,
                    patch("schedules.staging_performance_probe.build_opener") as transport,
                    patch("sys.stderr", new_callable=StringIO) as errors,
                ):
                    self.assertEqual(main(["--username", "probe-user"]), 1)
                password.assert_not_called()
                transport.assert_not_called()
                self.assertIn("PRECEPTOR_STAGING_", errors.getvalue())
                self.assertNotIn("secret", errors.getvalue())

    def test_help_needs_no_configuration_and_url_overrides_are_not_accepted(self):
        for arguments, exit_code in (
            (["--help"], 0),
            (["--username", "probe-user", "--url", TEST_ORIGIN.url], 2),
            (["--username", "probe-user", "--target", TEST_ORIGIN.url], 2),
        ):
            with (
                self.subTest(arguments=arguments),
                patch.dict(os.environ, {}, clear=True),
                patch("schedules.staging_performance_probe.getpass") as password,
                patch("schedules.staging_performance_probe.build_opener") as transport,
                patch("sys.stdout", new_callable=StringIO),
                patch("sys.stderr", new_callable=StringIO),
            ):
                with self.assertRaises(SystemExit) as error:
                    main(arguments)
                self.assertEqual(error.exception.code, exit_code)
                password.assert_not_called()
                transport.assert_not_called()

    def test_cli_captures_each_configured_origin_through_login_redirects_and_cleanup(self):
        form = (
            STAGING_MARKER
            + '<input type="hidden" name="csrfmiddlewaretoken" value="token">'
            + '<a href="/physicians/17/">Physician</a>'
        ).encode()
        for host in ("alpha.example.test", "beta.example.test"):
            origin = f"https://{host}:10000"
            with self.subTest(host=host):

                def respond(request, origin=origin):
                    path = urlsplit(request.full_url).path
                    if request.get_method() == "POST":
                        if path == "/sign-in/":
                            return 302, b"", {"Location": "/schedule/"}
                        if path == "/solve/":
                            return (
                                302,
                                b"",
                                {
                                    "Location": "/solve/42/",
                                    PROBE_PROPOSAL_ID_HEADER: "42",
                                    "Server-Timing": "setup;dur=1, solve;dur=2, persistence;dur=3",
                                },
                            )
                        if path == "/solve/42/performance-probe-cleanup/":
                            return 204, b"", {}
                        self.fail(f"Unexpected POST: {path}")
                    if path == "/physicians/":
                        return 302, b"", {"Location": origin + "/directory/"}
                    return 200, form, {}

                transport = _ProbeTransport(respond)

                def password_after_configuration(prompt):
                    # Configuration is captured before the prompt and not reread later.
                    os.environ["PRECEPTOR_STAGING_ALLOWED_HOST"] = "changed.example.test"
                    os.environ["PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN"] = (
                        "https://changed.example.test:10000"
                    )
                    return "secret-password"

                with (
                    patch.dict(
                        os.environ,
                        {
                            "PRECEPTOR_STAGING_ALLOWED_HOST": host,
                            "PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN": origin,
                        },
                        clear=True,
                    ),
                    patch(
                        "schedules.staging_performance_probe.getpass",
                        side_effect=password_after_configuration,
                    ),
                    patch(
                        "schedules.staging_performance_probe.build_opener",
                        side_effect=lambda *handlers, transport=transport: build_opener(
                            *handlers, transport
                        ),
                    ),
                    patch("sys.stdout", new_callable=StringIO) as output,
                ):
                    self.assertEqual(
                        main(
                            [
                                "--username",
                                "probe-user",
                                "--anchor-month",
                                "2026-08",
                                "--solver-start-month",
                                "2026-09",
                            ]
                        ),
                        0,
                    )
                measurements = json.loads(output.getvalue())
                self.assertEqual(
                    [sample["label"] for sample in measurements if sample["phases"]],
                    ["Schedule Bot 1-month", "Schedule Bot 3-month"],
                )
                urls = [request.full_url for request in transport.requests]
                self.assertEqual(urls[:2], [origin + "/sign-in/"] * 2)
                self.assertIn(origin + "/directory/", urls)
                self.assertEqual(urls.count(origin + "/solve/42/performance-probe-cleanup/"), 2)
                for request in transport.requests:
                    self.assertEqual(urlsplit(request.full_url).netloc, f"{host}:10000")
                    self.assertEqual(urlsplit(request.full_url).scheme, "https")
                    if request.get_method() == "POST":
                        self.assertEqual(request.get_header("Origin"), origin)

    def test_other_configured_installation_is_not_a_permitted_redirect(self):
        for host, other in (
            ("alpha.example.test", "beta.example.test"),
            ("beta.example.test", "alpha.example.test"),
        ):
            with self.subTest(host=host):
                probe = StagingProbe(
                    "probe-user", "password", origin=StagingOrigin(host, f"https://{host}:10000")
                )
                transport = _ProbeTransport(
                    lambda request, other=other: (302, b"", {"Location": f"https://{other}:10000/"})
                )
                probe.opener.add_handler(transport)
                with self.assertRaises(ProbeError):
                    probe.request("/start")
                self.assertEqual(
                    [request.full_url for request in transport.requests],
                    [f"https://{host}:10000/start"],
                )


class StagingPerformanceProbeOriginTests(SimpleTestCase):
    origin = TEST_ORIGIN.url
    form = (
        STAGING_MARKER + '<input type="hidden" name="csrfmiddlewaretoken" value="token">'
    ).encode()
    unsafe_urls = (
        "https://outside.example.test/private?secret-query",
        "//outside.example.test/private?secret-query",
        "http://probe.example.test:10000/private?secret-query",
        "https://probe.example.test/private?secret-query",
        "https://probe.example.test:10001/private?secret-query",
        "ftp://probe.example.test:10000/private?secret-query",
        "https://secret-user:secret-password@probe.example.test:10000/",
        "https://probe.example.test:10000@outside.example.test/",
        "https://probe.example.test:/",
        "https://probe.example.test:invalid/",
        "https://probe.example.test:99999/",
        "https://[probe.example.test/",
        "https:/probe.example.test:10000/",
        "https://probe.example.test:10000\\@outside.example.test/",
        "https://probe.example.test:10000\n/",
        " https://probe.example.test:10000/",
        "///outside.example.test/",
    )

    def make_probe(self, respond):
        probe = StagingProbe("probe-user", "secret-password", origin=TEST_ORIGIN)
        transport = _ProbeTransport(respond)
        probe.opener.add_handler(transport)
        probe.no_redirect_opener.add_handler(transport)
        return probe, transport.requests

    def assert_safe_error(self, error):
        for secret in ("secret-query", "secret-user", "secret-password", "session-secret"):
            self.assertNotIn(secret, str(error))

    def test_requests_capture_origin_and_share_secure_cookies_without_following_login(self):
        def respond(request):
            if request.get_method() == "POST":
                return 302, b"", {"Location": "/schedule/"}
            return 200, self.form, {"Set-Cookie": "session=session-secret; Secure; Path=/"}

        probe, requests = self.make_probe(respond)
        with patch.dict(
            os.environ,
            {
                "PRECEPTOR_STAGING_ALLOWED_HOST": "changed.example.test",
                "PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN": "https://changed.example.test:10000",
            },
            clear=True,
        ):
            probe.authenticate()
            for path in ("schedule/", "/schedule/", self.origin + "/schedule/"):
                probe.request(path)

        self.assertEqual(
            [request.full_url for request in requests],
            [self.origin + "/sign-in/"] * 2 + [self.origin + "/schedule/"] * 3,
        )
        login = requests[1]
        self.assertEqual(login.get_method(), "POST")
        self.assertIn(b"password=secret-password", login.data)
        self.assertEqual(login.get_header("Origin"), self.origin)
        for request in requests[1:]:
            self.assertEqual(request.get_header("Cookie"), "session=session-secret")
        for request in requests:
            self.assertEqual(request.get_header(PROBE_HEADER.capitalize()), PROBE_TOKEN)
            self.assertEqual(request.timeout, 60)

    def test_marker_is_required_before_credential_submission(self):
        probe, requests = self.make_probe(lambda request: (200, b"not staging", {}))
        with self.assertRaises(ProbeError):
            probe.authenticate()
        self.assertEqual(len(requests), 1)
        self.assertIsNone(requests[0].data)

    def test_same_origin_redirects_use_urllib_and_keep_cookies(self):
        locations = (
            "next?view=calendar",
            "/next?view=calendar",
            self.origin + "/next?view=calendar",
            "//probe.example.test:10000/next?view=calendar",
        )
        for location in locations:
            with self.subTest(location=location):

                def respond(request, location=location):
                    if request.full_url == self.origin + "/start":
                        return (
                            302,
                            b"",
                            {
                                "Location": location,
                                "Set-Cookie": "session=session-secret; Secure; Path=/",
                            },
                        )
                    return 200, b"arrived", {}

                probe, requests = self.make_probe(respond)
                status, body, _ = probe.request("/start")
                self.assertEqual((status, body), (200, b"arrived"))
                self.assertEqual(
                    [request.full_url for request in requests],
                    [self.origin + "/start", self.origin + "/next?view=calendar"],
                )
                self.assertEqual(requests[1].get_header("Cookie"), "session=session-secret")
                self.assertEqual(requests[1].get_header(PROBE_HEADER.capitalize()), PROBE_TOKEN)

    def test_host_case_is_same_origin(self):
        probe, requests = self.make_probe(lambda request: (200, b"ok", {}))
        self.assertEqual(probe.request("https://PROBE.example.test:10000/ok")[0], 200)
        self.assertEqual(len(requests), 1)

    def test_unsafe_outgoing_urls_never_reach_transport(self):
        for url in self.unsafe_urls:
            with self.subTest(url=url):
                probe, requests = self.make_probe(lambda request: (200, b"unexpected", {}))
                for follow in (True, False):
                    with self.assertRaises(ProbeError) as error:
                        probe.request(
                            url, data={"password": "secret-password"}, follow_redirects=follow
                        )
                    self.assert_safe_error(error.exception)
                self.assertEqual(requests, [])

    def test_unsafe_redirects_fail_measurement_without_dispatching_destination(self):
        cases = [(302, url) for url in self.unsafe_urls]
        cases += [(code, self.unsafe_urls[0]) for code in (301, 303, 307, 308)]
        for code, location in cases:
            with self.subTest(code=code, location=location):
                probe, requests = self.make_probe(
                    lambda request, code=code, location=location: (
                        code,
                        b"",
                        {"Location": location, "Set-Cookie": "session=session-secret; Secure"},
                    )
                )
                with self.assertRaises(ProbeError) as error:
                    probe.measure("Unsafe redirect", "/start")
                self.assert_safe_error(error.exception)
                self.assertEqual(
                    [request.full_url for request in requests], [self.origin + "/start"]
                )

    def test_no_redirect_preserves_original_response_without_parsing_destination(self):
        for location in ("/schedule/", self.unsafe_urls[0], "https://[invalid/"):
            with self.subTest(location=location):
                probe, requests = self.make_probe(
                    lambda request, location=location: (302, b"original", {"Location": location})
                )
                status, body, headers = probe.request("/sign-in/", data={}, follow_redirects=False)
                self.assertEqual((status, body, headers["Location"]), (302, b"original", location))
                self.assertEqual(len(requests), 1)

    def test_redirect_loop_remains_finite_and_is_not_a_measurement(self):
        probe, requests = self.make_probe(lambda request: (302, b"", {"Location": "/loop"}))
        with self.assertRaisesRegex(ProbeError, "HTTP 302"):
            probe.measure("Loop", "/loop")
        self.assertGreater(len(requests), 1)
        self.assertLess(len(requests), 20)

    def test_schedule_bot_keeps_no_redirect_and_reports_proposal_on_unsafe_cleanup(self):
        def respond(request):
            if request.full_url.endswith("/performance-probe-cleanup/"):
                return 302, b"", {"Location": self.unsafe_urls[0]}
            if request.get_method() == "POST":
                return (
                    302,
                    b"",
                    {
                        "Location": "/solve/42/",
                        PROBE_PROPOSAL_ID_HEADER: "42",
                        "Server-Timing": "setup;dur=1, solve;dur=2, persistence;dur=3",
                    },
                )
            return 200, self.form, {}

        probe, requests = self.make_probe(respond)
        with self.assertRaisesRegex(ProbeError, "proposal 42 requires cleanup") as error:
            probe.measure_schedule_bot(date(2026, 9, 1), 1)
        self.assert_safe_error(error.exception)
        self.assertEqual(
            [request.full_url for request in requests],
            [self.origin + "/solve/"] * 2 + [self.origin + "/solve/42/performance-probe-cleanup/"],
        )


class StagingPerformanceProbeParserTests(SimpleTestCase):
    def test_workload_holds_the_five_year_boundary(self):
        anchor = month_start("2026-08")

        self.assertEqual(shift_month(anchor, -60), date(2021, 8, 1))
        self.assertEqual(month_end(anchor), date(2026, 8, 31))
        self.assertIn(
            ("Master Schedule historical", "/schedule/?date=2021-08-01"), workload(anchor)
        )
        self.assertIn(
            "start_date=2021-08-01", dict(workload(anchor))["Five-year Precepting report"]
        )

    def test_parses_only_expected_schedule_bot_redirects_and_timing_values(self):
        self.assertEqual(proposal_id_from_location("/solve/42/"), 42)
        timing = parse_server_timing(
            'setup ; dur=125.000, db-queries ; desc="17", persistence;dur=2.000, cache;dur=1'
        )
        self.assertEqual(timing["setup"], 0.125)
        self.assertEqual(timing["db-queries:desc"], "17")
        self.assertEqual(timing["persistence"], 0.002)
        with self.assertRaisesRegex(ProbeError, "did not redirect"):
            proposal_id_from_location("https://example.test/solve/42/")
        with self.assertRaisesRegex(Exception, "YYYY-MM"):
            month_start("2026-8")

    def test_extracts_actionable_schedule_bot_feedback(self):
        body = b"""<section id="solve-feedback"><p><strong>No normal rounder is recorded for a solve week.</strong> 2026-09-07</p></section>"""

        self.assertEqual(
            schedule_bot_feedback(body), "No normal rounder is recorded for a solve week."
        )

    def test_schedule_bot_uses_one_year_balance_context_for_each_solve_range(self):
        probe = StagingProbe("probe-user", "password", origin=TEST_ORIGIN)
        form = b'<input type="hidden" name="csrfmiddlewaretoken" value="token">'
        request_data = []

        def request(path, *, data=None, follow_redirects=True):
            if data and path == "/solve/":
                request_data.append(data)
                proposal_id = len(request_data)
                return (
                    302,
                    b"",
                    {
                        PROBE_PROPOSAL_ID_HEADER: str(proposal_id),
                        "Location": f"/solve/{proposal_id}/",
                        "Server-Timing": "setup;dur=1, solve;dur=2, persistence;dur=3",
                    },
                )
            if path == "/solve/":
                return 200, form, {}
            return 204, b"", {}

        with patch.object(probe, "request", side_effect=request):
            for months in (1, 3):
                probe.measure_schedule_bot(date(2026, 9, 1), months)

        self.assertEqual([data["balance_span"] for data in request_data], ["1-year"] * 2)

    def assert_cleanup_of_recoverable_proposal(self, headers, error):
        probe = StagingProbe("probe-user", "password", origin=TEST_ORIGIN)
        form = b'<input type="hidden" name="csrfmiddlewaretoken" value="token">'
        cleanup_requests = []

        def request(path, *, data=None, follow_redirects=True):
            if path == "/solve/" and data is None:
                return 200, form, {}
            if path == "/solve/":
                return 302, b"", headers
            if path == "/solve/42/performance-probe-cleanup/":
                cleanup_requests.append((path, data))
                return 204, b"", {}
            self.fail(f"Unexpected probe request: {path}")

        with patch.object(probe, "request", side_effect=request):
            with self.assertRaisesRegex(ProbeError, error):
                probe.measure_schedule_bot(date(2026, 9, 1), 1)

        self.assertEqual(
            cleanup_requests,
            [("/solve/42/performance-probe-cleanup/", {"csrfmiddlewaretoken": "token"})],
        )

    def test_schedule_bot_attempts_cleanup_before_rejecting_a_malformed_redirect(self):
        self.assert_cleanup_of_recoverable_proposal(
            {PROBE_PROPOSAL_ID_HEADER: "42", "Location": "/unexpected/"},
            "did not redirect",
        )

    def test_schedule_bot_cleans_up_before_rejecting_a_missing_proposal_header(self):
        self.assert_cleanup_of_recoverable_proposal(
            {"Location": "/solve/42/"},
            "did not identify",
        )

    def test_schedule_bot_cleans_up_before_rejecting_a_malformed_proposal_header(self):
        self.assert_cleanup_of_recoverable_proposal(
            {PROBE_PROPOSAL_ID_HEADER: "not-an-id", "Location": "/solve/42/"},
            "did not identify",
        )

    def test_query_evidence_is_emitted_only_for_marked_staging_requests(self):
        factory = RequestFactory()
        request = factory.get("/schedule/")
        response = StagingPerformanceProbeMiddleware(lambda request: HttpResponse())(request)
        self.assertIsNone(response.get("Server-Timing"))

        with override_settings(STAGING_ENVIRONMENT_MARKER="STAGING — SYNTHETIC DATA"):
            request = factory.get(
                "/schedule/", HTTP_X_MURIEL_PERFORMANCE_PROBE="staging-performance-v1"
            )
            response = StagingPerformanceProbeMiddleware(lambda request: HttpResponse())(request)
        self.assertEqual(parse_server_timing(response["Server-Timing"])["db-queries:desc"], "0")


class StagingPerformanceProposalCleanupTests(TestCase):
    def setUp(self):
        # September solves must reach preflight/solving, not historical-range rejection.
        # Control the shared clock so imported date readers agree; clean up per test.
        self.enterContext(
            patch(
                "schedules.services.operational_attention.timezone.now",
                return_value=datetime(2026, 8, 1, 18, tzinfo=UTC),
            )
        )
        self.user = get_user_model().objects.create_user(username="probe-user")
        self.client.force_login(self.user)
        self.proposal = PreceptingSolverProposal.objects.create(
            mode="fill",
            solve_start_date=date(2026, 9, 1),
            solve_end_date=date(2026, 9, 30),
            balance_start_date=date(2026, 9, 1),
            balance_end_date=date(2026, 9, 30),
            policy_version="probe",
            solve_settings={"staging_performance_probe": True},
            input_fingerprint="probe",
            input_snapshot={"sessions": [], "late_doc_positions": []},
            proposed_assignments={"precepting": [], "late_doc": []},
            before_findings=[],
            after_findings=[],
            workload_effects={},
            compromises={},
            created_by=self.user,
        )

    def cleanup_url(self):
        return reverse("precepting-solver-performance-probe-cleanup", args=(self.proposal.pk,))

    def test_cleanup_is_not_available_without_staging_marker_and_probe_header(self):
        response = self.client.post(
            self.cleanup_url(), HTTP_X_MURIEL_PERFORMANCE_PROBE="staging-performance-v1"
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(PreceptingSolverProposal.objects.filter(pk=self.proposal.pk).exists())

    @override_settings(STAGING_ENVIRONMENT_MARKER="STAGING — SYNTHETIC DATA")
    def test_cleanup_removes_only_the_callers_marked_unapplied_proposal(self):
        response = self.client.post(
            self.cleanup_url(), HTTP_X_MURIEL_PERFORMANCE_PROBE="staging-performance-v1"
        )

        self.assertEqual(response.status_code, 204)
        self.assertFalse(PreceptingSolverProposal.objects.filter(pk=self.proposal.pk).exists())

    @override_settings(STAGING_ENVIRONMENT_MARKER="STAGING — SYNTHETIC DATA")
    def test_marked_schedule_bot_response_reports_phases_and_marks_its_proposal(self):
        preflight = SimpleNamespace(blockers=(), solver_input=object())
        retained = SimpleNamespace(pk=17)
        with (
            patch.object(solver, "validate_precepting_solver_capacity"),
            patch.object(solver, "initialize_preceptor_session_range"),
            patch.object(solver, "preflight_precepting_solve", return_value=preflight),
            patch.object(solver, "without_excluded_physicians", side_effect=lambda value, _: value),
            patch.object(solver, "produce_fill_proposal", return_value=object()),
            patch.object(solver, "retain_fill_proposal", return_value=retained) as retain,
        ):
            response = self.client.post(
                reverse("precepting-solver-workspace"),
                {
                    "mode": "fill",
                    "range_kind": "custom",
                    "start_date": "2026-09-01",
                    "end_date": "2026-09-30",
                    "balance_span": "1-month",
                },
                HTTP_X_MURIEL_PERFORMANCE_PROBE="staging-performance-v1",
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/solve/17/")
        self.assertEqual(response[PROBE_PROPOSAL_ID_HEADER], "17")
        timing = parse_server_timing(response["Server-Timing"])
        self.assertEqual(
            {phase for phase in ("setup", "solve", "persistence") if phase in timing},
            {"setup", "solve", "persistence"},
        )
        self.assertTrue(
            all(isinstance(timing[phase], float) for phase in ("setup", "solve", "persistence"))
        )
        self.assertTrue(retain.call_args.kwargs["solve_settings"]["staging_performance_probe"])

    @override_settings(STAGING_ENVIRONMENT_MARKER="STAGING — SYNTHETIC DATA")
    def test_failed_schedule_bot_probe_does_not_retain_new_state(self):
        self.user.groups.add(Group.objects.get(name="Scheduling Administrators"))
        initial_proposals = tuple(
            PreceptingSolverProposal.objects.order_by("pk").values_list("pk", "input_snapshot")
        )
        preflight = SimpleNamespace(blockers=(), solver_input=object())
        with (
            patch.object(solver, "validate_precepting_solver_capacity"),
            patch.object(solver, "initialize_preceptor_session_range"),
            patch.object(solver, "preflight_precepting_solve", return_value=preflight),
            patch.object(solver, "without_excluded_physicians", side_effect=lambda value, _: value),
            patch.object(
                solver,
                "produce_fill_proposal",
                side_effect=ValidationError(
                    "Schedule Bot reached the proportional Faculty workload fairness search "
                    "limit before finding a valid outcome."
                ),
            ) as produce,
            patch.object(solver, "retain_fill_proposal") as retain,
        ):
            response = self.client.post(
                reverse("precepting-solver-workspace"),
                {
                    "mode": "fill",
                    "range_kind": "custom",
                    "start_date": "2026-09-01",
                    "end_date": "2026-09-30",
                    "balance_span": "1-year",
                },
                HTTP_X_MURIEL_PERFORMANCE_PROBE="staging-performance-v1",
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "workload fairness")
        produce.assert_called_once()
        retain.assert_not_called()
        self.assertFalse(PreceptorAssignment.objects.exists())
        self.assertFalse(LateDocAssignment.objects.exists())
        self.assertEqual(
            tuple(
                PreceptingSolverProposal.objects.order_by("pk").values_list("pk", "input_snapshot")
            ),
            initial_proposals,
        )

    @override_settings(STAGING_ENVIRONMENT_MARKER="STAGING — SYNTHETIC DATA")
    def test_schedule_bot_probe_reports_real_preflight_feedback_without_a_timing_sample(self):
        self.user.groups.add(Group.objects.get(name="Scheduling Administrators"))
        probe = StagingProbe("probe-user", "password", origin=TEST_ORIGIN)

        def request(path, *, data=None, follow_redirects=True):
            if data is None:
                response = self.client.get(
                    path, HTTP_X_MURIEL_PERFORMANCE_PROBE="staging-performance-v1"
                )
            else:
                response = self.client.post(
                    path,
                    data,
                    follow=follow_redirects,
                    HTTP_X_MURIEL_PERFORMANCE_PROBE="staging-performance-v1",
                )
            return response.status_code, response.content, response.headers

        with (
            patch(
                "schedules.services.operational_attention.timezone.now",
                return_value=datetime(2026, 1, 1, 18, tzinfo=UTC),
            ),
            patch.object(probe, "request", side_effect=request),
        ):
            with self.assertRaises(ProbeError) as error:
                probe.measure_schedule_bot(date(2027, 2, 1), 1)

        self.assertIn("normal rounder", str(error.exception).lower())
        self.assertIn("HTTP 200", str(error.exception))
        self.assertEqual(
            tuple(
                PreceptingSolverProposal.objects.order_by("pk").values_list("pk", "input_snapshot")
            ),
            ((self.proposal.pk, self.proposal.input_snapshot),),
        )

    @tag("synthetic-solver-integration")
    @override_settings(STAGING_ENVIRONMENT_MARKER="STAGING — SYNTHETIC DATA")
    def test_one_and_three_month_probes_solve_real_scenario_and_clean_up(self):
        self.user.groups.add(Group.objects.get(name="Scheduling Administrators"))
        solver_start_month = date(2026, 9, 1)
        probe = StagingProbe("probe-user", "password", origin=TEST_ORIGIN)
        retained_proposals = []

        def request(path, *, data=None, follow_redirects=True):
            if data is None:
                response = self.client.get(
                    path, HTTP_X_MURIEL_PERFORMANCE_PROBE="staging-performance-v1"
                )
            else:
                response = self.client.post(
                    path,
                    data,
                    follow=follow_redirects,
                    HTTP_X_MURIEL_PERFORMANCE_PROBE="staging-performance-v1",
                )
                if path == "/solve/" and response.status_code == 302:
                    proposal = PreceptingSolverProposal.objects.latest("pk")
                    retained_proposals.append(
                        {
                            "solve_start": proposal.solve_start_date,
                            "solve_end": proposal.solve_end_date,
                            "balance_start": proposal.balance_start_date,
                            "balance_end": proposal.balance_end_date,
                            "balance_assignments": proposal.input_snapshot["balance_assignments"],
                            "after_findings": proposal.after_findings,
                            "faculty_workloads": proposal.workload_effects["faculty"],
                        }
                    )
            return response.status_code, response.content, response.headers

        with patch.dict(
            settings.DATABASES["default"],
            {"NAME": "muriel_staging", "USER": "muriel_staging"},
        ):
            call_command("reset_staging_synthetic", "--confirm", verbosity=0)
            with patch.object(probe, "request", side_effect=request):
                measurements = [
                    probe.measure_schedule_bot(solver_start_month, months) for months in (1, 3)
                ]

        self.assertEqual([measurement.status for measurement in measurements], [302, 302])
        self.assertEqual([measurement.path for measurement in measurements], ["/solve/", "/solve/"])
        self.assertTrue(all(measurement.phases for measurement in measurements))
        self.assertEqual(len(retained_proposals), 2)
        for proposal in retained_proposals:
            with self.subTest(solve_end=proposal["solve_end"]):
                self.assertEqual(
                    proposal["balance_end"],
                    proposal["solve_end"],
                )
                self.assertEqual(
                    proposal["balance_start"],
                    shift_month(proposal["solve_end"].replace(day=1), -11),
                )
                self.assertTrue(proposal["balance_assignments"])
                self.assertFalse(
                    any(finding["severity"] == "hard" for finding in proposal["after_findings"])
                )
                self.assertTrue(proposal["faculty_workloads"])
                self.assertTrue(
                    any(workload["current_total"] > 0 for workload in proposal["faculty_workloads"])
                )
                self.assertAlmostEqual(
                    sum(workload["target_share"] for workload in proposal["faculty_workloads"]),
                    1,
                )
        self.assertFalse(PreceptingSolverProposal.objects.exists())


class StagingPerformanceProbeCsrfTests(TestCase):
    origin = TEST_ORIGIN.url
    host = TEST_ORIGIN.host

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="probe-user", password="password")

    @override_settings(
        ALLOWED_HOSTS=[host],
        CSRF_TRUSTED_ORIGINS=[origin],
        CSRF_COOKIE_SECURE=True,
    )
    def test_shared_post_path_sends_origin_accepted_by_enforced_csrf(self):
        captured = SimpleNamespace(request=None)

        def capture_request(request, timeout):
            captured.request = request
            return SimpleNamespace(status=204, read=lambda: b"", headers={})

        probe = StagingProbe("probe-user", "password", origin=TEST_ORIGIN)
        with patch.object(probe.opener, "open", side_effect=capture_request):
            probe.request("/sign-in/", data={"username": "probe-user", "password": "password"})

        client = Client(enforce_csrf_checks=True)
        response = client.get(reverse("sign-in"), secure=True, HTTP_HOST=self.host)
        token = csrf_token(response.content)
        response = client.post(
            reverse("sign-in"),
            {"csrfmiddlewaretoken": token, "username": "probe-user", "password": "password"},
            secure=True,
            HTTP_HOST=self.host,
            HTTP_ORIGIN=captured.request.get_header("Origin"),
        )

        self.assertEqual(captured.request.get_header("Origin"), self.origin)
        self.assertEqual(response.status_code, 302)
