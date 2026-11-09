"""Bounded HTTP probes for the isolated synthetic staging instance."""

import argparse
import http.cookiejar
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date
from getpass import getpass
from html.parser import HTMLParser
from time import perf_counter
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

STAGING_MARKER = "STAGING — SYNTHETIC DATA"
PROBE_HEADER = "X-Muriel-Performance-Probe"
PROBE_TOKEN = "staging-performance-v1"
PROBE_PROPOSAL_ID_HEADER = "X-Muriel-Performance-Probe-Proposal-ID"
MONTH_PATTERN = re.compile(r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])$")
PROPOSAL_LOCATION_PATTERN = re.compile(r"^/solve/(?P<proposal_id>\d+)/$")
PROPOSAL_ID_PATTERN = re.compile(r"^[1-9]\d*$")
PHYSICIAN_PATH_PATTERN = re.compile(r'href="/physicians/(?P<physician_id>\d+)/"')


class ProbeError(RuntimeError):
    """The target did not meet this probe's safety or response contract."""


class _HiddenInputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag != "input":
            return
        values = dict(attrs)
        if values.get("type") == "hidden" and values.get("name") and "value" in values:
            self.values[values["name"]] = values["value"]


class _ScheduleBotFeedback(HTMLParser):
    def __init__(self):
        super().__init__()
        self.messages = []
        self._feedback_depth = None
        self._message_depth = 0
        self._message = []

    def handle_starttag(self, tag, attrs):
        if self._feedback_depth is None:
            if tag == "section" and dict(attrs).get("id") == "solve-feedback":
                self._feedback_depth = 1
            return
        if tag == "section":
            self._feedback_depth += 1
        if tag in {"strong", "li"}:
            if self._message_depth == 0:
                self._message = []
            self._message_depth += 1

    def handle_endtag(self, tag):
        if self._feedback_depth is None:
            return
        if tag in {"strong", "li"} and self._message_depth:
            self._message_depth -= 1
            if self._message_depth == 0:
                message = " ".join("".join(self._message).split())
                if message and message not in self.messages:
                    self.messages.append(message)
        if tag == "section":
            self._feedback_depth -= 1
            if self._feedback_depth == 0:
                self._feedback_depth = None

    def handle_data(self, data):
        if self._message_depth:
            self._message.append(data)


def _split_probe_url(url):
    # urlsplit is a parser, not a validator: reject authority spellings it would
    # repair/ignore before either urljoin or urllib normalizes the input.
    try:
        if url != url.lstrip() or any(ord(char) < 32 or ord(char) == 127 for char in url):
            raise ValueError
        if "\\" in url:
            raise ValueError
        parts = urlsplit(url)
        if (parts.scheme or url.startswith("//")) and not parts.netloc:
            raise ValueError
        if parts.netloc:
            host = parts.hostname
            if not host or parts.username is not None or parts.password is not None:
                raise ValueError
            authority_host = f"[{host}]" if ":" in host else host
            if not re.fullmatch(re.escape(authority_host) + r"(?::[0-9]+)?", parts.netloc, re.I):
                raise ValueError
            # Accessing port also checks its numeric range.
            if parts.port == 0:
                raise ValueError
        return parts
    except ValueError:
        # Never echo untrusted URLs (including their userinfo/query) in errors.
        raise ProbeError("probe URL has a malformed authority or unsafe syntax") from None


@dataclass(frozen=True)
class StagingOrigin:
    """Explicit nonsecret installation identity, validated once at construction."""

    host: str
    url: str

    def __post_init__(self):
        # Keep this client boundary independent of settings and server secrets.
        label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        if (
            not self.host
            or len(self.host) > 253
            or not re.fullmatch(rf"{label}(?:\.{label})*\.?", self.host)
        ):
            raise ProbeError(
                "PRECEPTOR_STAGING_ALLOWED_HOST must be one exact lowercase hostname "
                "without a scheme, port, whitespace, or wildcards"
            )
        if self.url != f"https://{self.host}:10000":
            raise ProbeError(
                "PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN must be exactly "
                "https://<PRECEPTOR_STAGING_ALLOWED_HOST>:10000 (no trailing slash)"
            )
        self.validate(self.url)

    def validate(self, url):
        parts = _split_probe_url(url)
        selected = _split_probe_url(self.url)
        if (
            parts.scheme != "https"
            or not parts.netloc
            or parts.hostname != selected.hostname
            or (parts.port or 443) != (selected.port or 443)
        ):
            raise ProbeError("probe request would leave the selected HTTPS origin")
        return url

    def resolve(self, base, reference):
        _split_probe_url(reference)
        return self.validate(urljoin(base, reference))


class _SameOriginRedirect(HTTPRedirectHandler):
    def __init__(self, origin):
        self.origin = origin

    def http_error_302(self, req, fp, code, msg, headers):
        location = headers.get("Location", headers.get("URI"))
        if location is not None:
            try:
                # Inspect the raw reference before urllib repairs/quotes it.
                self.origin.resolve(req.full_url, location)
            except ProbeError:
                fp.close()
                raise
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.origin.validate(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoRedirect(HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        # Preserve the original response, even for a malformed Location. Login
        # and proposal cleanup own its interpretation; no target is dispatched.
        return None

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


@dataclass(frozen=True)
class Measurement:
    label: str
    path: str
    status: int
    response_bytes: int
    wall_seconds: float
    database_queries: int | None
    phases: dict[str, float] | None = None


def month_start(value):
    match = MONTH_PATTERN.fullmatch(value)
    if not match:
        raise argparse.ArgumentTypeError("months must use YYYY-MM")
    return date(int(match["year"]), int(match["month"]), 1)


def shift_month(month, offset):
    serial = month.year * 12 + month.month - 1 + offset
    return date(serial // 12, serial % 12 + 1, 1)


def month_end(month):
    return date.fromordinal(shift_month(month, 1).toordinal() - 1)


def hidden_inputs(body):
    parser = _HiddenInputs()
    parser.feed(body.decode())
    return parser.values


def csrf_token(body):
    try:
        return hidden_inputs(body)["csrfmiddlewaretoken"]
    except KeyError as error:
        raise ProbeError("response did not include a CSRF token") from error


def schedule_bot_feedback(body):
    parser = _ScheduleBotFeedback()
    parser.feed(body.decode(errors="replace"))
    return "; ".join(parser.messages)


def parse_server_timing(value):
    timings = {}
    for item in value.split(","):
        parts = [part.strip() for part in item.split(";")]
        if not parts or not parts[0]:
            continue
        parameters = {}
        for parameter in parts[1:]:
            key, separator, parameter_value = parameter.partition("=")
            if separator:
                parameters[key] = parameter_value.strip('"')
        if "dur" in parameters:
            timings[parts[0]] = float(parameters["dur"]) / 1000
        if "desc" in parameters:
            timings[f"{parts[0]}:desc"] = parameters["desc"]
    return timings


def proposal_id_from_location(location):
    match = PROPOSAL_LOCATION_PATTERN.fullmatch(location)
    if not match:
        raise ProbeError("Schedule Bot response did not redirect to one proposal")
    return int(match["proposal_id"])


def proposal_id_from_probe_header(value):
    if not PROPOSAL_ID_PATTERN.fullmatch(value):
        raise ProbeError("Schedule Bot response did not identify its marked proposal")
    return int(value)


def physician_id_from_directory(body):
    match = PHYSICIAN_PATH_PATTERN.search(body.decode())
    if not match:
        raise ProbeError("Physicians directory did not include a physician detail link")
    return int(match["physician_id"])


def workload(anchor_month):
    historical_month = shift_month(anchor_month, -60)
    report_parameters = {
        "period": "custom",
        "start_date": historical_month.isoformat(),
        "end_date": month_end(anchor_month).isoformat(),
        "coverage": "history-and-scheduled",
    }
    return (
        ("Master Schedule current", f"/schedule/?date={anchor_month.isoformat()}"),
        ("Master Schedule historical", f"/schedule/?date={historical_month.isoformat()}"),
        ("Work Remaining", "/schedule/work-remaining/"),
        ("Precepting calendar current", f"/?month={anchor_month:%Y-%m}"),
        ("Precepting list current", f"/list/?month={anchor_month:%Y-%m}"),
        ("Precepting calendar historical", f"/?month={historical_month:%Y-%m}"),
        ("Precepting list historical", f"/list/?month={historical_month:%Y-%m}"),
        *tuple(
            (
                f"Five-year {label} report",
                "/reports/?" + urlencode({"schedule": schedule, **report_parameters}),
            )
            for schedule, label in (
                ("precepting", "Precepting"),
                ("community-precepting", "Community Precepting"),
                ("interviews", "Interviews"),
                ("academic-events", "Academic Events"),
                ("normal-rounding", "Normal Rounding"),
                ("casual-rounding", "Casual Rounding"),
                ("vacation", "Vacation"),
            )
        ),
    )


class StagingProbe:
    def __init__(self, username, password, *, origin: StagingOrigin):
        self.username = username
        self.password = password
        self._origin = origin
        cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(_SameOriginRedirect(self._origin), HTTPCookieProcessor(cookies))
        self.no_redirect_opener = build_opener(_NoRedirect(), HTTPCookieProcessor(cookies))

    def _url(self, path):
        return self._origin.resolve(self._origin.url, path)

    def request(self, path, *, data=None, follow_redirects=True):
        headers = {PROBE_HEADER: PROBE_TOKEN}
        if data is not None:
            headers["Origin"] = self._origin.url
        request = Request(
            self._url(path),
            data=urlencode(data).encode() if data is not None else None,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        opener = self.opener if follow_redirects else self.no_redirect_opener
        try:
            response = opener.open(request, timeout=60)
        except HTTPError as error:
            response = error
        return response.status, response.read(), response.headers

    def authenticate(self):
        status, body, _ = self.request("/sign-in/")
        if status != 200 or STAGING_MARKER not in body.decode():
            raise ProbeError("configured target is not the marked synthetic staging site")
        status, _, headers = self.request(
            "/sign-in/",
            data={
                "csrfmiddlewaretoken": csrf_token(body),
                "username": self.username,
                "password": self.password,
            },
            follow_redirects=False,
        )
        if status != 302 or headers.get("Location") != "/schedule/":
            raise ProbeError("staging sign-in failed")

    def measure(self, label, path):
        started = perf_counter()
        status, body, headers = self.request(path)
        elapsed = perf_counter() - started
        if status != 200:
            raise ProbeError(f"{label} returned HTTP {status}")
        timing = parse_server_timing(headers.get("Server-Timing", ""))
        query_count = timing.get("db-queries:desc")
        return Measurement(
            label=label,
            path=path,
            status=status,
            response_bytes=len(body),
            wall_seconds=elapsed,
            database_queries=int(query_count) if query_count is not None else None,
        ), body

    def measure_schedule_bot(self, start_month, months):
        label = f"Schedule Bot {months}-month"
        status, form, _ = self.request("/solve/")
        if status != 200:
            raise ProbeError(f"{label} setup form returned HTTP {status}")
        started = perf_counter()
        status, body, headers = self.request(
            "/solve/",
            data={
                "csrfmiddlewaretoken": csrf_token(form),
                "mode": "fill",
                "range_kind": "custom",
                "start_date": start_month.isoformat(),
                "end_date": month_end(shift_month(start_month, months - 1)).isoformat(),
                "balance_span": "1-year",
            },
            follow_redirects=False,
        )
        elapsed = perf_counter() - started
        if status != 302:
            feedback = schedule_bot_feedback(body)
            detail = f": {feedback}" if feedback else ""
            raise ProbeError(f"{label} returned HTTP {status}{detail}")
        location = headers.get("Location", "")
        probe_proposal_id = headers.get(PROBE_PROPOSAL_ID_HEADER, "")
        try:
            proposal_id = proposal_id_from_location(location)
        except ProbeError:
            proposal_id = proposal_id_from_probe_header(probe_proposal_id)
        try:
            header_proposal_id = proposal_id_from_probe_header(probe_proposal_id)
            location_proposal_id = proposal_id_from_location(location)
            if header_proposal_id != location_proposal_id:
                raise ProbeError("Schedule Bot response identified different marked proposals")
            timing = parse_server_timing(headers.get("Server-Timing", ""))
            phases = {
                phase: timing[phase]
                for phase in ("setup", "solve", "persistence")
                if phase in timing
            }
            if len(phases) != 3:
                raise ProbeError(f"{label} did not report setup, solve, and persistence timings")
            query_count = timing.get("db-queries:desc")
            return Measurement(
                label=label,
                path="/solve/",
                status=status,
                response_bytes=len(body),
                wall_seconds=elapsed,
                database_queries=int(query_count) if query_count is not None else None,
                phases=phases,
            )
        finally:
            try:
                cleanup_status, _, _ = self.request(
                    f"/solve/{proposal_id}/performance-probe-cleanup/",
                    data={"csrfmiddlewaretoken": csrf_token(form)},
                )
                if cleanup_status != 204:
                    raise ProbeError(f"cleanup returned HTTP {cleanup_status}")
            except Exception as error:
                raise ProbeError(f"{label} proposal {proposal_id} requires cleanup") from error


def run_probe(username, password, anchor_month, solver_start_month, *, origin: StagingOrigin):
    probe = StagingProbe(username, password, origin=origin)
    probe.authenticate()
    measurements = [probe.measure(label, path)[0] for label, path in workload(anchor_month)]
    _, physician_directory = probe.measure("Physicians directory", "/physicians/")
    physician_id = physician_id_from_directory(physician_directory)
    for label, path in (
        ("Physician detail", f"/physicians/{physician_id}/?date={anchor_month.isoformat()}"),
        ("Physician history", f"/physicians/{physician_id}/history/"),
    ):
        measurements.append(probe.measure(label, path)[0])
    for months in (1, 3):
        measurements.append(probe.measure_schedule_bot(solver_start_month, months))
    return measurements


def parse_arguments(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True, help="existing staging scheduler username")
    parser.add_argument(
        "--anchor-month", type=month_start, default=date.today().replace(day=1), help="YYYY-MM"
    )
    parser.add_argument("--solver-start-month", type=month_start, help="YYYY-MM")
    return parser.parse_args(arguments)


def main(arguments=None):
    options = parse_arguments(arguments)
    solver_start_month = options.solver_start_month or shift_month(options.anchor_month, 1)
    try:
        origin = StagingOrigin(
            os.environ.get("PRECEPTOR_STAGING_ALLOWED_HOST", ""),
            os.environ.get("PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN", ""),
        )
        password = getpass("Staging password: ")
        measurements = run_probe(
            options.username, password, options.anchor_month, solver_start_month, origin=origin
        )
    except ProbeError as error:
        print(f"staging performance probe: {error}", file=sys.stderr)
        return 1
    print(json.dumps([asdict(measurement) for measurement in measurements], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
