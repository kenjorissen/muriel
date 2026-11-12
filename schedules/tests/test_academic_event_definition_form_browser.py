import json
import subprocess
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from schedules.models import Physician
from schedules.tests.browser_tools import require_browser_tools

_BROWSER_RUNNER = r"""
const { readFileSync } = require("fs");

const input = JSON.parse(readFileSync(0, "utf8"));
const { chromium } = require(process.env.PRECEPTOR_PLAYWRIGHT_MODULE);

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: input.browserExecutable });
  try {
  const context = await browser.newContext();
  await context.addCookies([{ name: input.sessionCookie.name, value: input.sessionCookie.value, url: input.baseUrl }]);
  const page = await context.newPage();
  await page.route("**/*", (route) => {
    const script = Object.entries(input.staticFiles).find(([suffix]) => route.request().url().endsWith(suffix));
    if (script) return route.fulfill({ body: readFileSync(script[1]), contentType: "text/javascript" });
    return route.request().resourceType() === "document" ? route.continue() : route.abort();
  });
  const response = await page.goto(input.baseUrl + input.url, { waitUntil: "load" });
  if (!response || !response.ok()) throw new Error("Definition form did not load.");

  const form = page.locator("[data-academic-event-definition-form]");
  const quarter = form.locator('select[name="expected_quarter"]');
  const duration = form.locator('input[name="default_duration_days"]');
  const mode = form.locator('select[name="scheduling_mode"]');
  const initialRule = form.locator('[data-initial-monthly-rule-section]');
  const picker = form.locator("[data-physician-picker]");
  if (await picker.getAttribute("data-physician-picker-ready") === null) {
    throw new Error("Definition physician picker did not enhance.");
  }
  if (await quarter.isDisabled() || await duration.isDisabled() || await initialRule.isVisible()) {
    throw new Error("Yearly definitions must show only yearly scheduling controls.");
  }

  await mode.selectOption("monthly_rules");
  if (!await quarter.isDisabled() || !await duration.isDisabled() || !await initialRule.isVisible()) {
    throw new Error("Monthly rules did not show the initial rule controls.");
  }
  const hasDisabledQuarter = await form.evaluate((element) => !new FormData(element).has("expected_quarter"));
  if (!hasDisabledQuarter) throw new Error("Disabled expected quarter remained in form submission.");
  const hasDisabledDuration = await form.evaluate((element) => !new FormData(element).has("default_duration_days"));
  if (!hasDisabledDuration) throw new Error("Disabled duration remained in form submission.");
  if (await initialRule.locator('select[name="weekday"]').isDisabled()) {
    throw new Error("Initial rule controls remained disabled in Monthly rules mode.");
  }

  await mode.selectOption("yearly");
  if (await quarter.isDisabled() || await duration.isDisabled() || await initialRule.isVisible()) {
    throw new Error("Yearly controls did not return after leaving Monthly rules mode.");
  }

  const choice = picker.getByRole("checkbox", { name: "Default Physician", exact: true });
  await choice.check();
  if (!await picker.getByRole("button", { name: "Remove Default Physician", exact: true }).isVisible()) {
    throw new Error("Selected default physician was not shown by the picker.");
  }
  const allFaculty = form.locator('input[name="all_available_faculty"]');
  await allFaculty.check();
  if (!await choice.isDisabled()) throw new Error("All-Faculty did not disable individual defaults.");
  await allFaculty.uncheck();
  if (await choice.isDisabled() || !await choice.isChecked()) {
    throw new Error("Individual default selection did not return after clearing All-Faculty.");
  }
  await picker.getByRole("button", { name: "Remove Default Physician", exact: true }).press("Enter");
  if (await choice.isChecked()) throw new Error("Removing the default physician retained its checkbox.");
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""


class AcademicEventDefinitionFormBrowserTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        cls.node, cls.browser_executable = require_browser_tools()
        super().setUpClass()

    def setUp(self):
        scheduler = get_user_model().objects.create_user(username="definition-browser-scheduler")
        self.client.force_login(scheduler)
        Physician.objects.create(display_name="Default Physician")

    def test_monthly_mode_and_assignment_defaults_use_the_shared_picker(self):
        static_root = Path(__file__).resolve().parents[1] / "static" / "schedules"
        completed = subprocess.run(
            [self.node, "-e", _BROWSER_RUNNER],
            input=json.dumps(
                {
                    "baseUrl": self.live_server_url,
                    "browserExecutable": self.browser_executable,
                    "url": reverse("academic-event-definition-create"),
                    "sessionCookie": {
                        "name": settings.SESSION_COOKIE_NAME,
                        "value": self.client.cookies[settings.SESSION_COOKIE_NAME].value,
                    },
                    "staticFiles": {
                        "/static/schedules/physicians-picker.js": str(
                            static_root / "physicians-picker.js"
                        ),
                        "/static/schedules/academic-event-form.js": str(
                            static_root / "academic-event-form.js"
                        ),
                    },
                }
            ),
            capture_output=True,
            text=True,
        )

        if completed.returncode:
            self.fail(completed.stderr)
