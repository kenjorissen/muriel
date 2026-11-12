"""Request outcomes must reach Django even when rendered-content checks pass."""

import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase

from schedules.tests.browser_tools import require_browser_tools

_RUNNER = r"""
const { withBrowserPage } = require(process.env.PRECEPTOR_BROWSER_SESSION);
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
let release;
let observed;
const requested = new Promise(resolve => { observed = resolve; });
const released = new Promise(resolve => { release = resolve; });
withBrowserPage(input.executable, async ({ page, requests }) => {
  await page.route("http://request-fixture.invalid/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/") {
      const trigger = input.mode === "server"
        ? '<script>fetch("/application-result").catch(() => {});</script>'
        : '<img src="/required.svg" alt="Fixture asset">';
      return route.fulfill({
        contentType: "text/html",
        body: `<h1>Usable content</h1><img src="/excluded.svg" alt="Excluded fixture">${trigger}`,
      });
    }
    if (path === "/excluded.svg") return requests.abortExcluded(route);
    observed();
    await released;
    if (input.mode === "transport") return route.abort("connectionreset");
    if (input.mode === "excluded") return requests.abortExcluded(route);
    if (["expected", "wrong-status"].includes(input.mode)) {
      requests.expectFailure(route.request(), { status: 404 });
    }
    return route.fulfill({
      status: ["server", "wrong-status"].includes(input.mode) ? 503 : input.mode === "fulfilled" ? 200 : 404,
      contentType: "image/svg+xml",
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>',
    });
  });
  await page.goto("http://request-fixture.invalid/", { waitUntil: "domcontentloaded" });
  await requested;
  if (!await page.getByRole("heading", { name: "Usable content" }).isVisible()) {
    throw new Error("Fixture content assertion failed");
  }
  console.log("Content assertion passed");
  // Outcome arrives only after the otherwise-successful content assertion.
  release();
}, { beforeClose: () => release?.() }).catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
"""


class BrowserRequestTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        cls.node, cls.executable = require_browser_tools()
        super().setUpClass()

    def run_browser(self, mode):
        with TemporaryDirectory() as scratch:
            completed = subprocess.run(
                [self.node, "-e", _RUNNER],
                input=json.dumps({"executable": self.executable, "mode": mode}),
                env={**os.environ, "TMPDIR": scratch, "TMP": scratch, "TEMP": scratch},
                capture_output=True,
                text=True,
            )
            self.assertEqual(list(Path(scratch).iterdir()), [], completed.stderr)
        self.assertIn("Content assertion passed", completed.stdout, completed.stderr)
        return completed

    def assert_runner_success(self, completed):
        # The same Node -> Django assertion boundary used by embedded runners.
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_missing_required_asset_fails_after_successful_content_assertion(self):
        completed = self.run_browser("missing")
        with self.assertRaisesRegex(self.failureException, r"required\.svg: HTTP 404"):
            self.assert_runner_success(completed)

    def test_application_server_error_fails_after_successful_content_assertion(self):
        completed = self.run_browser("server")
        with self.assertRaisesRegex(self.failureException, r"application-result: HTTP 503"):
            self.assert_runner_success(completed)

    def test_required_asset_transport_failure_is_attributed(self):
        completed = self.run_browser("transport")
        with self.assertRaisesRegex(
            self.failureException, r"required\.svg: net::ERR_CONNECTION_RESET"
        ):
            self.assert_runner_success(completed)

    def test_expected_http_error_does_not_exempt_a_different_status(self):
        completed = self.run_browser("wrong-status")
        with self.assertRaisesRegex(self.failureException, r"required\.svg: HTTP 503"):
            self.assert_runner_success(completed)

    def test_intentional_fixture_outcomes_remain_accepted(self):
        for mode in ("fulfilled", "excluded", "expected"):
            with self.subTest(mode=mode):
                self.assert_runner_success(self.run_browser(mode))
