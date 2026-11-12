import json
import subprocess
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase

from schedules.services.lead_times import ensure_lead_time_defaults
from schedules.services.operational_attention import organization_today
from schedules.tests.browser_tools import require_browser_tools

_BROWSER_RUNNER = r"""
const { readFileSync } = require("fs");

const input = JSON.parse(readFileSync(0, "utf8"));
const { chromium } = require(process.env.PRECEPTOR_PLAYWRIGHT_MODULE);
const { observeRequests } = require(process.env.PRECEPTOR_BROWSER_REQUESTS);

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: input.browserExecutable });
  try {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 }, javaScriptEnabled: false,
  });
  await context.addCookies([{
    name: input.sessionCookie.name,
    value: input.sessionCookie.value,
    url: input.baseUrl,
  }]);
  const page = await context.newPage();
  const requests = observeRequests(page);
  let widerTextFixture = false;
  await page.route("**/*", (route) => {
    if (route.request().url().endsWith("/static/schedules/application.css")) {
      // Serve the fixture through the existing CSS seam, also with JS disabled.
      // Wider text stresses metrics without installing or assuming a system font.
      const fixture = widerTextFixture ? `
        .workspace-work-summary .surface-header {
          font-family: sans-serif;
          letter-spacing: .07em;
        }
      ` : "";
      return route.fulfill({ body: readFileSync(input.stylesheet, "utf8") + fixture, contentType: "text/css" });
    }
    return route.request().resourceType() === "document" ? route.continue() : requests.abortExcluded(route);
  });

  for (const [url, periodControls, firstSection, label] of [
    ["/vacations/list/?month=2026-08", ".period-navigation", ".workspace-surface-stack", "Vacation List Actions"],
    ["/vacations/holidays/?calendar_year=2026", ".period-navigation", ".workspace-work-summary", "Vacation Holidays Work remaining"],
    ["/rounding/list/?month=2026-08", ".period-navigation", ".workspace-work-summary", "Rounding List Work remaining"],
  ]) {
    // Without application JS, DOMContentLoaded need not wait for the stylesheet.
    const response = await page.goto(input.baseUrl + url, { waitUntil: "load" });
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

  for (const [width, widerText] of [[1280, false], [375, false], [375, true]]) {
    widerTextFixture = widerText;
    await page.setViewportSize({ width, height: 900 });
    for (const [path, label] of [["/", "Calendar"], ["/list/", "List"]]) {
      const response = await page.goto(input.baseUrl + `${path}?month=${input.preceptingMonth}`, { waitUntil: "load" });
      if (!response || !response.ok()) {
        throw new Error(`Precepting ${label} ${width}px: page did not load successfully.`);
      }
      const scenario = `Precepting ${label} ${width}px${widerText ? " constrained wider text" : ""}`;
      const surface = page.getByRole("region", { name: "Work remaining", exact: true });
      const header = surface.locator("header");
      const heading = header.getByRole("heading", { name: "Work remaining", exact: true });
      const action = header.getByRole("link", { name: "Open in Schedule Bot", exact: true });
      if (await heading.count() !== 1 || !await heading.isVisible() ||
          await action.count() !== 1 || !await action.isVisible()) {
        throw new Error(`${scenario}: missing visible Work remaining heading or Schedule Bot link in its header.`);
      }
      if (widerText) {
        // Derive a narrow fixture from the rendered text: either item fits alone,
        // but not both together. No viewport/font combination promises a row count.
        await surface.evaluate((element) => {
          const header = element.querySelector("header");
          const style = getComputedStyle(header);
          const widest = Math.max(...[...header.children].map(child => child.getBoundingClientRect().width));
          const insets = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight) +
            parseFloat(style.borderLeftWidth) + parseFloat(style.borderRightWidth);
          const surfaceStyle = getComputedStyle(element);
          element.style.maxWidth = `${Math.ceil(widest + insets + parseFloat(style.columnGap) +
            parseFloat(surfaceStyle.borderLeftWidth) + parseFloat(surfaceStyle.borderRightWidth))}px`;
        });
      }
      const summary = surface.locator("p");
      const text = await summary.innerText();
      if (!/required actions? remain:/.test(text)) {
        throw new Error(`${scenario}: typed work summary was not rendered.`);
      }
      const overflows = await summary.evaluate((element) => element.scrollWidth > element.clientWidth + 1);
      if (overflows) {
        throw new Error(`${scenario}: typed work summary overflows its surface.`);
      }
      await header.scrollIntoViewIfNeeded();
      const layout = await header.evaluate((element) => {
        const heading = element.querySelector("h2");
        const action = element.querySelector("a");
        const header = element.getBoundingClientRect();
        const headingBox = heading.getBoundingClientRect();
        const actionBox = action.getBoundingClientRect();
        const style = getComputedStyle(element);
        const contentLeft = header.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft);
        const contentRight = header.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight);
        const contained = box => box.width > 0 && box.height > 0 &&
          box.left >= header.left && box.right <= header.right &&
          box.top >= header.top && box.bottom <= header.bottom &&
          box.left >= 0 && box.right <= innerWidth && box.top >= 0 && box.bottom <= innerHeight;
        const readable = element => {
          const range = document.createRange();
          range.selectNodeContents(element);
          const box = element.getBoundingClientRect();
          return [...range.getClientRects()].every(text => contained(text) &&
            text.left >= box.left && text.right <= box.right &&
            text.top >= box.top && text.bottom <= box.bottom) &&
            // scroll/client dimensions are integer-rounded, unlike DOMRects.
            element.scrollWidth <= element.clientWidth + 1 &&
            element.scrollHeight <= element.clientHeight + 1;
        };
        return {
          header: header.toJSON(), heading: headingBox.toJSON(), action: actionBox.toJSON(),
          contained: contained(headingBox) && contained(actionBox),
          readable: readable(heading) && readable(action),
          fits: headingBox.width + actionBox.width + parseFloat(style.columnGap) <= contentRight - contentLeft,
          rightAligned: Math.abs(contentRight - actionBox.right) <= 0.1,
          sameRow: actionBox.bottom > headingBox.top && actionBox.top < headingBox.bottom &&
            headingBox.right <= actionBox.left,
          wrapped: actionBox.top >= headingBox.bottom,
        };
      });
      if (!layout.contained || !layout.readable || (!layout.sameRow && !layout.wrapped) ||
          ((width === 1280 || layout.fits) && (!layout.sameRow || !layout.rightAligned))) {
        throw new Error(`${scenario}: unreadable, overlapping, overflowing or misaligned header: ${JSON.stringify(layout)}`);
      }
      if (widerText && (layout.fits || !layout.wrapped)) {
        throw new Error(`${scenario}: fixture did not exercise constrained wrapping: ${JSON.stringify(layout)}`);
      }
      // Native link semantics and hit testing must work without application JS.
      await action.click({ trial: true });
      await action.focus();
      if (!await action.evaluate(element => document.activeElement === element && element.tabIndex >= 0)) {
        throw new Error(`${scenario}: Schedule Bot link is not keyboard focusable.`);
      }
    }
  }

  await requests.finish();
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""


class WorkRemainingSpacingBrowserTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        cls.node, cls.browser_executable = require_browser_tools()
        super().setUpClass()

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
