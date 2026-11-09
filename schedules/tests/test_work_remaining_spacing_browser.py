import json
import shutil
import subprocess
from pathlib import Path
from unittest import SkipTest

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import LiveServerTestCase

from schedules.services.lead_times import ensure_lead_time_defaults
from schedules.services.operational_attention import organization_today

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

  for (const [url, periodControls, firstSection, label] of [
    ["/vacations/list/?month=2026-08", ".period-navigation", ".workspace-surface-stack", "Vacation List Actions"],
    ["/vacations/holidays/?calendar_year=2026", ".period-navigation", ".workspace-work-summary", "Vacation Holidays Work remaining"],
    ["/rounding/list/?month=2026-08", ".period-navigation", ".workspace-work-summary", "Rounding List Work remaining"],
  ]) {
    const response = await page.goto(input.baseUrl + url, { waitUntil: "domcontentloaded" });
    if (!response || !response.ok()) {
      throw new Error(`${label}: page did not load successfully (${response ? response.status() : "no response"}).`);
    }
    const spacing = await page.evaluate(({ periodControls, firstSection, label }) => {
      const controls = document.querySelector(periodControls);
      const section = document.querySelector(firstSection);
      if (!controls) {
        throw new Error(`${label}: missing period controls (${periodControls}).`);
      }
      if (!section) {
        throw new Error(`${label}: missing first section (${firstSection}).`);
      }
      const gapProbe = document.createElement("div");
      gapProbe.style.marginTop = "var(--section-gap)";
      document.body.append(gapProbe);
      const gap = Number.parseFloat(getComputedStyle(gapProbe).marginTop);
      gapProbe.remove();
      return {
        actual: section.getBoundingClientRect().top - controls.getBoundingClientRect().bottom,
        expected: gap,
      };
    }, { periodControls, firstSection, label });
    if (Math.abs(spacing.actual - spacing.expected) > 0.1) {
      throw new Error(`${label} is ${spacing.actual}px below period controls, expected ${spacing.expected}px.`);
    }
  }

  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 900 });
    for (const [path, label] of [["/", "Calendar"], ["/list/", "List"]]) {
      const response = await page.goto(input.baseUrl + `${path}?month=${input.preceptingMonth}`, { waitUntil: "domcontentloaded" });
      if (!response || !response.ok()) {
        throw new Error(`Precepting ${label} ${width}px: page did not load successfully.`);
      }
      const summary = page.locator(".workspace-work-summary p");
      const text = await summary.innerText();
      if (!/required actions? remain:/.test(text)) {
        throw new Error(`Precepting ${label} ${width}px: typed work summary was not rendered.`);
      }
      const overflows = await summary.evaluate((element) => element.scrollWidth > element.clientWidth + 1);
      if (overflows) {
        throw new Error(`Precepting ${label} ${width}px: typed work summary overflows its surface.`);
      }
      const action = page.getByRole("link", { name: "Open in Schedule Bot" });
      if (await action.count() !== 1) {
        throw new Error(`Precepting ${label} ${width}px: missing Schedule Bot range action.`);
      }
      const alignment = await action.evaluate((element) => {
        const heading = element.parentElement.querySelector("h2");
        const header = element.parentElement.getBoundingClientRect();
        const actionBox = element.getBoundingClientRect();
        const headingBox = heading.getBoundingClientRect();
        return {
          overflows: actionBox.right > header.right + 1,
          rightAligned: header.right - actionBox.right <= 15,
          sharesHeadingRow: actionBox.bottom >= headingBox.top && actionBox.top <= headingBox.bottom,
        };
      });
      if (alignment.overflows || !alignment.rightAligned || !alignment.sharesHeadingRow) {
        throw new Error(`Precepting ${label} ${width}px: Schedule Bot action is not aligned in the Work remaining header.`);
      }
    }
  }

  await browser.close();
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""


class WorkRemainingSpacingBrowserTests(LiveServerTestCase):
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
            username="browser-scheduler", password="browser-password"
        )

    def test_work_remaining_uses_the_standard_section_gap_after_period_controls(self):
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
                    "preceptingMonth": f"{organization_today():%Y-%m}",
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
