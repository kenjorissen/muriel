import json
import shutil
import subprocess
from pathlib import Path
from unittest import SkipTest

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import LiveServerTestCase

from schedules.services.lead_times import ensure_lead_time_defaults

_BROWSER_RUNNER = r"""
const { execFileSync } = require("child_process");
const { readFileSync } = require("fs");
const path = require("path");

const input = JSON.parse(readFileSync(0, "utf8"));
const globalRoot = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
const { chromium } = require(path.join(globalRoot, "@playwright/cli/node_modules/playwright"));

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: input.browserExecutable });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.addCookies([{
    name: input.sessionCookie.name,
    value: input.sessionCookie.value,
    url: input.baseUrl,
  }]);
  const page = await context.newPage();
  await page.route("**/*", (route) => {
    if (route.request().url().endsWith("/static/schedules/application.css")) {
      return route.fulfill({ body: readFileSync(input.stylesheet), contentType: "text/css" });
    }
    return route.request().resourceType() === "document" ? route.continue() : route.abort();
  });
  await page.goto(input.baseUrl + "/schedule/lead-times/", { waitUntil: "domcontentloaded" });
  await page.getByRole("main").getByRole("heading", { name: "Lead Times" }).waitFor();
  const localNavigation = page.locator(".workspace-local-navigation");
  if (await localNavigation.getByRole("link", { name: "Lead Times" }).getAttribute("aria-current") !== "page") {
    throw new Error("Lead Times is not current in Schedule local navigation.");
  }
  for (const [label, path] of [["Master Schedule", "/schedule/"], ["Work Remaining", "/schedule/work-remaining/"]]) {
    if (await localNavigation.getByRole("link", { name: label, exact: true }).getAttribute("href") !== path) {
      throw new Error(`Schedule local navigation does not link ${label} to its workspace.`);
    }
  }
  if (await page.locator(".primary-navigation").getByText("Lead Times", { exact: true }).count()) {
    throw new Error("Lead Times was added to global navigation.");
  }
  const policyTable = page.locator("#lead-times");
  const rows = policyTable.locator("tbody tr");
  if (await rows.count() !== 5) throw new Error("Lead Times does not show the fixed five policies.");
  const headers = await policyTable.locator("thead th").allInnerTexts();
  const cell = (row, header) => {
    const index = headers.indexOf(header);
    if (index < 0) throw new Error(`Lead Times does not label its ${header} field.`);
    return row.locator(":scope > th, :scope > td").nth(index);
  };
  const policyRow = (family) => policyTable.locator("tbody tr").filter({
    has: page.getByRole("rowheader", { name: family, exact: true }),
  });
  const documentedPolicies = {
    "Holidays": ["18 months", ["holiday's nominal date"]],
    "Academic Events": ["6 months", ["expected academic-year quarter"]],
    "Precepting": ["8 months", ["affected", "week"]],
    "Normal Rounding": ["3 months", ["affected", "week"]],
    "Interviews": ["0 months", ["July 1", "interview academic year"]],
  };
  for (const [family, [months, anchorFragments]] of Object.entries(documentedPolicies)) {
    const row = policyRow(family);
    if (await row.count() !== 1) throw new Error(`Lead Times does not show one ${family} policy row.`);
    if (await cell(row, "Lead time").innerText() !== months) {
      throw new Error(`Lead Times does not show ${family}'s documented lead time.`);
    }
    const anchor = await cell(row, "Fixed anchor").innerText();
    if (!anchorFragments.every((fragment) => anchor.includes(fragment))) {
      throw new Error(`Lead Times does not show ${family}'s fixed anchor.`);
    }
  }
  const header = page.locator(".schedule-workspace-actions-header");
  if (await header.getByRole("heading", { name: "Actions", level: 2 }).count() !== 1) {
    throw new Error("Lead-time history is not in the standard Actions section.");
  }
  const alignment = await header.evaluate((element) => {
    const heading = element.querySelector("h2").getBoundingClientRect();
    const link = element.querySelector("a").getBoundingClientRect();
    return link.left > heading.right && link.right <= element.getBoundingClientRect().right;
  });
  if (!alignment) throw new Error("Lead-time history is not right-aligned in the Actions header.");
  const preceptingActions = cell(policyRow("Precepting"), "Actions").getByRole("link", { name: "Edit", exact: true });
  if (await preceptingActions.count() !== 1) throw new Error("Precepting does not expose its own Edit action.");
  await Promise.all([
    page.waitForURL((url) => url.pathname === "/schedule/lead-times/precepting/edit/"),
    preceptingActions.click(),
  ]);
  await page.locator('input[name="months"]').fill("12");
  await page.locator('textarea[name="note"]').fill("Browser update");
  await Promise.all([
    page.waitForURL((url) => url.pathname === "/schedule/lead-times/"),
    page.getByRole("button", { name: "Save changes" }).click(),
  ]);
  if (await cell(policyRow("Precepting"), "Lead time").innerText() !== "12 months") {
    throw new Error("Saved Precepting lead time is not visible in its workspace row.");
  }
  if (await cell(policyRow("Normal Rounding"), "Lead time").innerText() !== "3 months") {
    throw new Error("Saving Precepting changed Normal Rounding's independent lead time.");
  }
  await Promise.all([
    page.waitForURL((url) => url.pathname === "/schedule/lead-times/history/"),
    page.getByRole("link", { name: "Lead-time history" }).click(),
  ]);
  const history = page.locator("#lead-time-history");
  const historyHeaders = await history.locator("thead th").allInnerTexts();
  const historyCell = (row, header) => {
    const index = historyHeaders.indexOf(header);
    if (index < 0) throw new Error(`Lead-time history does not label its ${header} field.`);
    return row.locator(":scope > th, :scope > td").nth(index);
  };
  const preceptingHistoryRows = history.locator("tbody tr").filter({
    has: page.getByRole("rowheader", { name: "Precepting", exact: true }),
  });
  const rowsWith = async (header, expected) => {
    const matching = [];
    for (let index = 0; index < await preceptingHistoryRows.count(); index += 1) {
      const row = preceptingHistoryRows.nth(index);
      if (await historyCell(row, header).innerText() === expected) matching.push(row);
    }
    return matching;
  };
  const savedHistoryRows = await rowsWith("Note", "Browser update");
  if (savedHistoryRows.length !== 1
      || await historyCell(savedHistoryRows[0], "Prior value").innerText() !== "8 months"
      || await historyCell(savedHistoryRows[0], "Resulting value").innerText() !== "12 months"
      || await historyCell(savedHistoryRows[0], "Scheduler").innerText() !== "browser-administrator") {
    throw new Error("Saved Precepting history does not retain its prior value, result, and scheduler.");
  }
  const fixtureHistoryRows = await rowsWith("Resulting value", "8 months");
  if (fixtureHistoryRows.length !== 1
      || await historyCell(fixtureHistoryRows[0], "Prior value").innerText() !== "5 months") {
    throw new Error("Initial Precepting history does not retain the fixture's change to 8 months.");
  }
  await browser.close();
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""


class LeadTimeWorkspaceBrowserTests(LiveServerTestCase):
    serve_static = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.node = shutil.which("node")
        cls.browser_executable = shutil.which("google-chrome")
        if not cls.node or not cls.browser_executable:
            raise SkipTest("Playwright browser coverage requires Node and Google Chrome.")
        available = subprocess.run(
            [
                cls.node,
                "-e",
                "const p=require('path'),e=require('child_process').execFileSync;"
                "require(p.join(e('npm',['root','-g'],{encoding:'utf8'}).trim(),"
                "'@playwright/cli/node_modules/playwright'));",
            ],
            capture_output=True,
            text=True,
        )
        if available.returncode:
            raise SkipTest("Playwright is not available for browser coverage.")

    def setUp(self):
        ensure_lead_time_defaults()
        self.user = get_user_model().objects.create_user(
            username="browser-administrator", password="browser-password"
        )
        self.user.user_permissions.add(
            Permission.objects.get(codename="manage_scheduling_administration")
        )
        from schedules.models import LeadTimeSetting
        from schedules.services.lead_times import set_lead_time

        set_lead_time(
            family=LeadTimeSetting.Family.PRECEPTING,
            months=8,
            scheduler=self.user,
        )
        set_lead_time(
            family=LeadTimeSetting.Family.NORMAL_ROUNDING,
            months=3,
            scheduler=self.user,
        )

    def test_renders_and_edits_the_lead_time_workspace(self):
        self.client.force_login(self.user)
        completed = subprocess.run(
            [self.node, "-e", _BROWSER_RUNNER],
            input=json.dumps(
                {
                    "baseUrl": self.live_server_url,
                    "browserExecutable": self.browser_executable,
                    "stylesheet": str(
                        Path(__file__).resolve().parents[1] / "static/schedules/application.css"
                    ),
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
