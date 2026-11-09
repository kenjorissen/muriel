"""Protect Node-owned deadlines and cleanup, not application UI behavior."""

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
const events = [];
let release;

withBrowserPage(input.executable, async ({ page, context }) => {
  page.on("close", () => events.push("page closed"));
  context.on("close", () => events.push("context closed"));
  context.browser().on("disconnected", () => events.push("browser disconnected"));
  if (input.mode === "success") {
    await page.setContent("<button>Available</button>");
    await page.getByRole("button", { name: "Available" }).click();
    return;
  }
  // Keep an intercepted request pending while the missing-element wait fails.
  // Cleanup must release it, without losing the primary timeout diagnostic.
  let observed;
  const requestObserved = new Promise(resolve => { observed = resolve; });
  const released = new Promise(resolve => { release = resolve; });
  await page.route("http://browser-cleanup.invalid/", async route => {
    observed();
    await released;
    events.push("request released");
    // The context may already be closing; that is the expected cancellation.
    await route.abort().catch(() => {});
  });
  const navigation = page.goto("http://browser-cleanup.invalid/").catch(() => {});
  await requestObserved;
  events.push("waiting for missing element");
  await page.locator("#deliberately-missing").click();
  await navigation;
}, {
  actionTimeoutMs: input.mode === "action" ? 100 : 30_000,
  workTimeoutMs: input.mode === "scenario" ? 1_000 : 90_000,
  beforeClose: () => { release?.(); },
}).then(() => {
  console.log(JSON.stringify({ events }));
}).catch(error => {
  console.log(JSON.stringify({ events, error: error.message }));
  process.exitCode = 1;
});
"""


class BrowserSessionTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        cls.node, cls.executable = require_browser_tools()
        super().setUpClass()

    def run_session(self, mode):
        # Playwright's profiles and artifacts must not survive the owned Node
        # invocation. Inspect only this test's explicitly allocated directory.
        with TemporaryDirectory() as scratch:
            completed = subprocess.run(
                [self.node, "-e", _RUNNER],
                input=json.dumps({"executable": self.executable, "mode": mode}),
                env={**os.environ, "TMPDIR": scratch, "TMP": scratch, "TEMP": scratch},
                capture_output=True,
                text=True,
            )
            self.assertEqual(list(Path(scratch).iterdir()), [], completed.stderr)
        self.assertEqual(completed.returncode, 0 if mode == "success" else 1, completed.stderr)
        result = json.loads(completed.stdout)
        for event in ("page closed", "context closed", "browser disconnected"):
            self.assertIn(event, result["events"])
        if mode != "success":
            self.assertIn("waiting for missing element", result["events"])
            self.assertIn("request released", result["events"])
        return result

    def test_success_closes_browser_and_removes_owned_resources(self):
        self.assertNotIn("error", self.run_session("success"))

    def test_missing_element_preserves_action_timeout_and_cleans_up(self):
        result = self.run_session("action")
        self.assertIn("locator.click: Timeout", result["error"])
        self.assertIn("#deliberately-missing", result["error"])

    def test_scenario_deadline_interrupts_pending_work_and_cleans_up(self):
        result = self.run_session("scenario")
        self.assertIn("Browser scenario exceeded", result["error"])
