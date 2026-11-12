import json
import subprocess
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from schedules.models import ClinicHoliday, HolidayOccurrence
from schedules.services.operational_attention import organization_today
from schedules.services.work_remaining import (
    GlobalWorkRemaining,
    InclusiveDateRange,
    ProjectedWorkItem,
    ProjectedWorkReason,
)
from schedules.tests.browser_tools import require_browser_tools

_BROWSER_RUNNER = r"""
const { readFileSync } = require("fs");

const input = JSON.parse(readFileSync(0, "utf8"));
const { chromium } = require(process.env.PRECEPTOR_PLAYWRIGHT_MODULE);
const { observeRequests } = require(process.env.PRECEPTOR_BROWSER_REQUESTS);

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: input.browserExecutable });
  try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const requests = observeRequests(page);
  await page.route("**/*", (route) => {
    if (route.request().url().endsWith("/static/schedules/application.css")) {
      return route.fulfill({ body: readFileSync(input.stylesheet), contentType: "text/css" });
    }
    return route.request().resourceType() === "document" ? route.continue() : requests.abortExcluded(route);
  });
  await page.goto(input.baseUrl + "/schedule/work-remaining/", { waitUntil: "load" });
  await page.locator('input[name="username"]').fill(input.username);
  await page.locator('input[name="password"]').fill(input.password);
  await page.locator('button[type="submit"]').click();
  await page.waitForLoadState("load");
  if (!page.url().includes("/schedule/work-remaining/")) {
    throw new Error(
      "Sign in did not reach Work Remaining at " + page.url() + ": " + await page.locator("body").innerText()
    );
  }
  if (!await page.locator("#work-remaining").count()) {
    throw new Error("Work Remaining did not render: " + await page.locator("body").innerText());
  }
  if (await page.getByRole("link", { name: "Work Remaining" }).getAttribute("aria-current") !== "page") {
    throw new Error("Work Remaining is not current in Schedule local navigation.");
  }
  if (await page.getByRole("link", { name: "Master Schedule" }).getAttribute("href") !== "/schedule/") {
    throw new Error("Schedule local navigation does not retain Master Schedule.");
  }
  if (await page.locator("#work-remaining").count() !== 1) {
    throw new Error("Work Remaining does not use the standard data table.");
  }
  if (await page.locator(".workspace-work-summary.surface-tone--danger").count() !== 1) {
    throw new Error("Required work does not use the danger summary tone.");
  }
  const summaryHeading = page.getByRole("heading", { name: "Work remaining", level: 2 });
  const itemsHeading = page.getByRole("heading", { name: "Work items", level: 2 });
  if (await summaryHeading.count() !== 1 || await itemsHeading.count() !== 1) {
    throw new Error("Work summary and table do not use distinct level-two headings.");
  }
  if (await summaryHeading.evaluate((heading) => getComputedStyle(heading).fontSize) !== await itemsHeading.evaluate((heading) => getComputedStyle(heading).fontSize)) {
    throw new Error("Work summary and table headings do not share standard surface typography.");
  }
  const table = page.locator("#work-remaining");
  const headers = await table.locator("thead th").allTextContents();
  const column = new Map(headers.map((label, index) => [label.trim(), index]));
  for (const label of ["Area", "Affected period", "Work remaining"]) {
    if (!column.has(label)) {
      throw new Error("Work Remaining is missing its " + label + " field.");
    }
  }
  if (column.has("Actions")) {
    throw new Error("Work Remaining retains its redundant Actions field.");
  }
  const areaLinks = page.locator("#work-remaining .data-table-cell--link > a");
  if (await areaLinks.count() !== input.workItems.length) {
    throw new Error("Work Remaining does not provide exactly one Area link per work item.");
  }
  for (const item of input.workItems) {
    const link = page.locator(`#work-remaining .data-table-cell--link > a[href="${item.url}"]`);
    if (await link.count() !== 1 || await link.getAttribute("href") !== item.url) {
      throw new Error("Area link does not retain the producer target for " + item.area + ".");
    }
    if (await link.evaluate((element) => (element.getAttribute("aria-label") || element.textContent).trim()) !== item.area) {
      throw new Error("Area link has no useful accessible name for " + item.area + ".");
    }
    if (await link.getAttribute("target") !== null) {
      throw new Error("Area link opens a new browsing context for " + item.area + ".");
    }
    await link.focus();
    if (!await link.evaluate((element) => document.activeElement === element)) {
      throw new Error("Area link is not keyboard focusable for " + item.area + ".");
    }
    if (!(await link.evaluate((element) => getComputedStyle(element).outlineStyle !== "none"))) {
      throw new Error("Area link has no visible keyboard focus affordance for " + item.area + ".");
    }
    if (!(await link.evaluate((element) => getComputedStyle(element, "::after").content.includes("\u203a")))) {
      throw new Error("Area link does not use the internal drilldown chevron for " + item.area + ".");
    }
    if (await link.evaluate((element) => element.classList.contains("button-link"))) {
      throw new Error("Area link uses the prominent action-button presentation for " + item.area + ".");
    }
    const record = link.locator("xpath=ancestor::tr[1]");
    if (await record.count() !== 1) {
      throw new Error("Work item action is not contained in one rendered record for " + item.area + ".");
    }
    const cells = record.locator(":scope > th, :scope > td");
    const area = cells.nth(column.get("Area"));
    const period = cells.nth(column.get("Affected period"));
    const remaining = cells.nth(column.get("Work remaining"));
    if (await area.getByRole("link").getAttribute("href") !== item.url) {
      throw new Error("Work item Area action is not in its Area field for " + item.area + ".");
    }
    if ((await period.innerText()).trim() !== item.period) {
      throw new Error("Work item affected period is not in its own field for " + item.area + ".");
    }
    const remainingText = await remaining.innerText();
    if (!remainingText.includes(item.detail) || !remainingText.includes(item.quantity)) {
      throw new Error("Work item reason and quantity are not in their own field for " + item.area + ".");
    }
  }
  for (const width of [1280, 640]) {
    await page.setViewportSize({ width, height: 900 });
    const layout = await table.evaluate((table, workRemainingColumn) => {
      const frame = table.parentElement;
      const headerWidths = [...table.querySelectorAll("thead th")].map(
        (header) => header.getBoundingClientRect().width
      );
      const clippedCells = [...table.querySelectorAll("th, td")].filter(
        (cell) => cell.scrollWidth > cell.clientWidth + 1
      ).length;
      return {
        frameOverflows: frame.scrollWidth > frame.clientWidth + 1,
        clippedCells,
        workRemainingHasSurplus: headerWidths[workRemainingColumn] > Math.max(
          ...headerWidths.filter((_, index) => index !== workRemainingColumn)
        ),
      };
    }, column.get("Work remaining"));
    if (layout.frameOverflows) throw new Error("Work Remaining requires horizontal table scrolling at " + width + "px.");
    if (layout.clippedCells) throw new Error("Work Remaining clips wrapped content at " + width + "px.");
    if (!layout.workRemainingHasSurplus) throw new Error("Work Remaining does not reserve flexible surplus for Work remaining at " + width + "px.");
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


class WorkRemainingBrowserTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        cls.node, cls.browser_executable = require_browser_tools()
        super().setUpClass()

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="browser-scheduler", password="browser-password"
        )
        administrators, _ = Group.objects.get_or_create(name="Scheduling Administrators")
        administrators.permissions.add(
            Permission.objects.get(codename="manage_scheduling_administration")
        )
        self.user.groups.add(administrators)
        nominal_date = organization_today() + timedelta(days=10)
        self.holiday = HolidayOccurrence.objects.create(
            holiday=ClinicHoliday.INDEPENDENCE_DAY,
            calendar_year=nominal_date.year,
            name="Browser holiday",
            nominal_date=nominal_date,
            coverage=HolidayOccurrence.Coverage.WHOLE_DAY,
            state=HolidayOccurrence.State.UNSCHEDULED,
        )

    def test_renders_global_schedule_navigation_and_area_drilldown_links(self):
        work_items = (
            (
                "Academic Events",
                f"{reverse('academic-event-create')}?academic_year=2026&definition=1",
            ),
            ("Academic Events", reverse("academic-event-correct", args=(1,))),
            (
                "Holidays",
                reverse("holiday-occurrence-schedule", args=(self.holiday.pk,)),
            ),
            (
                "Precepting",
                f"{reverse('monthly-workspace')}?month=2026-07#precepting-week-2026-07-06",
            ),
            (
                "Normal Rounding",
                f"{reverse('normal-rounder-workspace')}?start_date=2026-07-06&end_date=2026-07-06",
            ),
            ("Interviews", f"{reverse('interview-list')}?academic_year=2026"),
        )
        item_records = []
        for index, (area, url) in enumerate(work_items):
            starts_on = date(2026, 7, 6) + timedelta(days=index * 7)
            ends_on = starts_on + timedelta(days=4)
            if index == 0:
                starts_on, ends_on = date(2026, 11, 30), date(2027, 12, 2)
            quantity = index + 1
            detail = (
                "1 extended coverage exception needs review before the next scheduling cycle begins"
                if index == 0
                else f"{quantity} distinct coverage gaps require review"
            )
            item_records.append(
                {
                    "area": area,
                    "url": url,
                    "detail": detail,
                    "quantity": str(quantity),
                    "period": (
                        "Nov 30, 2026-Dec 2, 2027"
                        if index == 0
                        else f"{starts_on:%b} {starts_on.day}-{ends_on.day}, {starts_on:%Y}"
                    ),
                    "starts_on": starts_on,
                    "ends_on": ends_on,
                }
            )
        item_records = tuple(item_records)
        projection = GlobalWorkRemaining(
            items=tuple(
                ProjectedWorkItem(
                    key=f"{area}-{index}",
                    area={
                        "Academic Events": "academic-events",
                        "Holidays": "holidays",
                        "Precepting": "precepting",
                        "Normal Rounding": "rounding",
                        "Interviews": "interviews",
                    }[area],
                    label=area,
                    affected_range=InclusiveDateRange(
                        item_records[index]["starts_on"], item_records[index]["ends_on"]
                    ),
                    matters_on=date(2026, 2, 6) + timedelta(days=index),
                    target_url=url,
                    reasons=(
                        ProjectedWorkReason(
                            "required",
                            item_records[index]["detail"],
                            int(item_records[index]["quantity"]),
                        ),
                    ),
                )
                for index, (area, url) in enumerate(work_items)
            ),
            required_item_count=sum(int(record["quantity"]) for record in item_records),
            advisory_item_count=0,
            tone="danger",
            prose=f"{sum(int(record['quantity']) for record in item_records)} required actions remain.",
        )
        with patch("schedules.views.work_remaining.global_work_remaining", return_value=projection):
            completed = subprocess.run(
                [self.node, "-e", _BROWSER_RUNNER],
                input=json.dumps(
                    {
                        "baseUrl": self.live_server_url,
                        "browserExecutable": self.browser_executable,
                        "stylesheet": str(
                            Path(__file__).parents[1] / "static/schedules/application.css"
                        ),
                        "username": self.user.username,
                        "password": "browser-password",
                        "workItems": [
                            {
                                key: record[key]
                                for key in ("area", "url", "detail", "quantity", "period")
                            }
                            for record in item_records
                        ],
                    }
                ),
                capture_output=True,
                text=True,
            )

        if completed.returncode:
            self.fail(completed.stderr)
