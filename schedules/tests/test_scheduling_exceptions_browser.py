import json
import subprocess
from datetime import date
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import LiveServerTestCase
from django.urls import reverse

from schedules.models import EffectiveRole, Physician, PreceptorSession
from schedules.services.preceptors import open_monthly_draft
from schedules.tests.browser_tools import require_browser_tools

_FIXTURE_TODAY = date(2026, 9, 8)

_BROWSER_RUNNER = r"""
const { readFileSync } = require("fs");

const input = JSON.parse(readFileSync(0, "utf8"));
const { chromium } = require(process.env.PRECEPTOR_PLAYWRIGHT_MODULE);

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: input.browserExecutable });
  try {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.addCookies([{ name: input.sessionCookie.name, value: input.sessionCookie.value, url: input.baseUrl }]);
  const page = await context.newPage();
  await page.route("**/*", (route) => {
    if (route.request().url().endsWith("/static/schedules/application.css")) {
      return route.fulfill({ body: readFileSync(input.stylesheet), contentType: "text/css" });
    }
    return route.request().resourceType() === "document" ? route.continue() : route.abort();
  });

  await page.goto(input.baseUrl + input.assignmentUrl, { waitUntil: "domcontentloaded" });
  const currentFindings = page.locator('[aria-labelledby="current-findings-heading"]');
  if ((await currentFindings.count()) !== 1) {
    throw new Error("The assignment editor did not expose one unified current-findings section.");
  }
  if (!(await currentFindings.textContent()).includes("Unacknowledged hard finding")) {
    throw new Error("The assignment editor did not expose its current hard finding.");
  }
  if ((await currentFindings.locator("details").count()) !== 0) {
    throw new Error("Current findings should be an ordinary section, not a disclosure.");
  }
  await currentFindings.locator("textarea[name=reason]").first().fill("External coverage is unavailable.");
  await Promise.all([page.waitForLoadState("domcontentloaded"), currentFindings.getByRole("button", { name: "Acknowledge exception" }).first().click()]);
  if (!(await currentFindings.textContent()).includes("Acknowledged exception") || !(await currentFindings.textContent()).includes("External coverage is unavailable.")) {
    throw new Error("The acknowledgement was not retained on the assignment editor.");
  }
  if ((await currentFindings.locator("details").count()) !== 0) {
    throw new Error("Acknowledged exceptions should remain in the ordinary findings section.");
  }

  await page.goto(input.baseUrl + input.monthUrl, { waitUntil: "domcontentloaded" });
  const acknowledgedSlot = page.locator(`a.assignment-slot-link[href^="${input.assignmentUrl}"]`);
  if ((await acknowledgedSlot.count()) !== 1 || !(await acknowledgedSlot.locator(".calendar-finding-indicator--exception").count())) {
    throw new Error("The acknowledged session finding was not marked on its assignment slot.");
  }

  await page.goto(input.baseUrl + input.assignmentUrl, { waitUntil: "domcontentloaded" });
  const weekFinding = page.locator("section.finding", { hasText: "No normal rounder is recorded" });
  await weekFinding.locator("textarea[name=reason]").fill("No faculty member is available to round this week.");
  await Promise.all([page.waitForLoadState("domcontentloaded"), weekFinding.getByRole("button", { name: "Acknowledge exception" }).click()]);

  await page.goto(input.baseUrl + input.monthUrl, { waitUntil: "domcontentloaded" });
  const dateHeader = page.locator(`time[datetime="${input.weekStart}"]`);
  const affectedWeek = page.getByRole("rowgroup").filter({ has: dateHeader });
  const dateGutter = affectedWeek.locator(".calendar-lattice-date-gutter");
  if (
    (await affectedWeek.count()) !== 1 ||
    (await dateGutter.count()) !== 1 ||
    (await dateGutter.textContent()).trim() ||
    (await dateGutter.locator(".calendar-finding-indicator").count()) !== 0
  ) {
    throw new Error("The Precepting month week gutter showed a status or finding indicator.");
  }

  await page.goto(input.baseUrl + input.listUrl, { waitUntil: "domcontentloaded" });
  const listedSession = page
    .locator("tr", { has: page.locator(`time[datetime="${input.sessionDate}"]`) })
    .filter({ hasText: "AM" });
  const listedFinding = listedSession.locator(".preceptor-schedule-list-findings");
  if ((await listedSession.count()) !== 1 || !(await listedFinding.textContent()).includes("No normal rounder is recorded")) {
    throw new Error("The acknowledged week finding was not retained in the Precepting list.");
  }

  await page.goto(input.baseUrl + input.assignmentUrl, { waitUntil: "domcontentloaded" });
  await page.locator("select[name=physician]").selectOption(String(input.physicianId));
  await Promise.all([page.waitForLoadState("domcontentloaded"), page.getByRole("button", { name: "Save assignment" }).click()]);
  await page.goto(input.baseUrl + input.exceptionsUrl, { waitUntil: "domcontentloaded" });
  const retainedAcknowledgement = page.locator("li.finding", { hasText: "External coverage is unavailable." });
  if ((await retainedAcknowledgement.count()) !== 1 || !(await retainedAcknowledgement.textContent()).includes("No longer applies.") || (await retainedAcknowledgement.textContent()).includes("fingerprint") || (await retainedAcknowledgement.textContent()).includes("stale")) {
    throw new Error("The retained exception history did not show its current status safely.");
  }
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""


class SchedulingExceptionsBrowserTests(LiveServerTestCase):
    serve_static = False

    def setUp(self):
        super().setUp()
        self.operational_today = patch(
            "schedules.services.operational_attention.organization_today",
            return_value=_FIXTURE_TODAY,
        )
        self.work_remaining_today = patch(
            "schedules.services.work_remaining.organization_today",
            return_value=_FIXTURE_TODAY,
        )
        self.preceptors_view_today = patch(
            "schedules.views.preceptors.organization_today",
            return_value=_FIXTURE_TODAY,
        )
        self.operational_today.start()
        self.work_remaining_today.start()
        self.preceptors_view_today.start()
        self.addCleanup(self.operational_today.stop)
        self.addCleanup(self.work_remaining_today.stop)
        self.addCleanup(self.preceptors_view_today.stop)

    @classmethod
    def setUpClass(cls):
        cls.node, cls.browser_executable = require_browser_tools()
        super().setUpClass()

    def test_scheduler_acknowledges_then_reviews_a_retained_exception(self):
        scheduler = get_user_model().objects.create_user(username="exception-browser-scheduler")
        self.client.force_login(scheduler)
        session = open_monthly_draft(date(2026, 9, 1)).sessions.get(
            date=date(2026, 9, 8), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        physician = Physician.objects.create(display_name="Exception browser faculty")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2026, 1, 1),
        )
        completed = subprocess.run(
            [self.node, "-e", _BROWSER_RUNNER],
            input=json.dumps(
                {
                    "baseUrl": self.live_server_url,
                    "browserExecutable": self.browser_executable,
                    "stylesheet": str(
                        Path(__file__).resolve().parents[1] / "static/schedules/application.css"
                    ),
                    "assignmentUrl": reverse(
                        "session-assignment-edit", args=(session.pk, "primary-1")
                    ),
                    "weekStart": "2026-09-07",
                    "monthUrl": f"{reverse('monthly-workspace')}?month=2026-09",
                    "listUrl": f"{reverse('preceptor-list')}?month=2026-09",
                    "sessionDate": session.date.isoformat(),
                    "exceptionsUrl": reverse("scheduling-exceptions"),
                    "physicianId": physician.pk,
                    "sessionCookie": {
                        "name": settings.SESSION_COOKIE_NAME,
                        "value": self.client.cookies[settings.SESSION_COOKIE_NAME].value,
                    },
                }
            ),
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            self.fail(completed.stderr)
