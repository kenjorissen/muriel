import json
import subprocess
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import LiveServerTestCase

from schedules.models import (
    CasualRounderRule,
    CommunityPreceptorRule,
    EffectiveRole,
    FacultyAvailabilityTemplate,
    Physician,
)
from schedules.tests.browser_tools import require_browser_tools

_BROWSER_RUNNER = r"""
const { readFileSync } = require("fs");

const input = JSON.parse(readFileSync(0, "utf8"));
const { chromium } = require(process.env.PRECEPTOR_PLAYWRIGHT_MODULE);

async function columnIndex(table, label) {
  const headers = table.locator("thead th[scope=col]");
  const labels = (await headers.allTextContents()).map((text) => text.trim());
  const index = labels.indexOf(label);
  if (index === -1) throw new Error(`The table is missing its ${label} column header.`);
  return index;
}

async function cellForHeader(table, row, headerLabel) {
  const index = await columnIndex(table, headerLabel);
  const cells = row.locator(":scope > th, :scope > td");
  if (await cells.count() <= index) {
    throw new Error(`The ${headerLabel} header has no matching row cell.`);
  }
  return cells.nth(index);
}

async function rowForRecord(table, recordLabel) {
  const rowHeader = table.getByRole("rowheader", { name: recordLabel, exact: true });
  if (await rowHeader.count() !== 1) throw new Error(`The ${recordLabel} record is not uniquely rendered by its row header.`);
  const row = rowHeader.locator("..");
  if (await row.count() !== 1 || await row.evaluate((element) => element.tagName !== "TR")) {
    throw new Error(`The ${recordLabel} row header is not in a table row.`);
  }
  return row;
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: input.browserExecutable });
  try {
  const page = await browser.newPage({ viewport: { width: 320, height: 800 } });
  await page.route("**/*", (route) => {
    if (route.request().url().endsWith("/static/schedules/application.css")) {
      return route.fulfill({ body: readFileSync(input.stylesheet), contentType: "text/css" });
    }
    return route.request().resourceType() === "document" ? route.continue() : route.abort();
  });
  await page.goto(input.baseUrl + "/rounding/list/?month=2025-02", { waitUntil: "domcontentloaded" });
  await page.locator('input[name="username"]').fill(input.username);
  await page.locator('input[name="password"]').fill(input.password);
  await page.locator('button[type="submit"]').click();
  await page.waitForLoadState("domcontentloaded");

  const table = page.locator("#rounding-list");
  const roundingRow = table.locator("tbody tr").first();
  const roundingActions = await cellForHeader(table, roundingRow, "Actions");
  if (await roundingActions.getByRole("link", { name: "Edit" }).getAttribute("href") === null) {
    throw new Error("The Rounding Actions column does not expose its GET edit action.");
  }
  const overflow = await page.locator("#rounding-list").evaluate((table) => {
    const frame = table.parentElement;
    return frame.scrollWidth > frame.clientWidth && document.documentElement.scrollWidth <= document.documentElement.clientWidth;
  });
  if (!overflow) throw new Error("The table does not keep narrow-screen overflow inside its scroll frame.");

  await page.goto(input.baseUrl + "/physicians/", { waitUntil: "domcontentloaded" });
  for (const tableId of ["physicians-active", "physicians-inactive"]) {
    const directoryTable = page.locator(`#${tableId}`);
    await columnIndex(directoryTable, "Physician");
    const directoryOverflow = await directoryTable.evaluate((table) => {
      const frame = table.parentElement;
      return frame.scrollWidth >= frame.clientWidth && document.documentElement.scrollWidth <= document.documentElement.clientWidth;
    });
    if (!directoryOverflow) throw new Error("The Physicians table does not keep narrow-screen framing.");
  }
  const activePhysicians = page.locator("#physicians-active");
  const facultyRow = await rowForRecord(activePhysicians, "Browser faculty");
  const tags = (await cellForHeader(activePhysicians, facultyRow, "Current roles and attributes")).locator(":scope > .data-table-tags");
  if (await tags.count() !== 1 || await tags.evaluate((list) => list.tagName) !== "UL") {
    throw new Error("The Physicians roles do not use a semantic tag list.");
  }
  const tagItems = tags.locator(":scope > .data-table-tag");
  if (await tagItems.allTextContents().then((labels) => labels.join("|")) !== "Faculty|PD") {
    throw new Error("The Physicians role tags do not preserve their reading order.");
  }
  const tagsWrap = await tags.evaluate((list) => getComputedStyle(list).flexWrap === "wrap");
  if (!tagsWrap) throw new Error("The Physicians role tags do not support narrow-screen wrapping.");

  const configuredRow = await rowForRecord(activePhysicians, "Browser configured");
  const configuredSourceTags = (await cellForHeader(activePhysicians, configuredRow, "Source schedules")).locator(":scope > .data-table-tags");
  if (await configuredSourceTags.count() !== 1 || await configuredSourceTags.evaluate((list) => list.tagName) !== "UL") {
    throw new Error("The Physicians source schedules do not use a semantic tag list.");
  }
  const sourceTagItems = configuredSourceTags.locator(":scope > .data-table-tag");
  if (await sourceTagItems.allTextContents().then((labels) => labels.join("|")) !== "Community: 1 rule|Casual: 1 rule") {
    throw new Error("The Physicians source schedule tags do not preserve source order.");
  }
  const sourceTagsWrap = await configuredSourceTags.evaluate((list) => getComputedStyle(list).flexWrap === "wrap");
  if (!sourceTagsWrap) throw new Error("The Physicians source schedule tags do not support narrow-screen wrapping.");
  const sourceTagsWrapOnNarrowWidth = await sourceTagItems.evaluateAll((items) =>
    new Set(items.map((item) => item.getBoundingClientRect().top)).size > 1
  );
  if (!sourceTagsWrapOnNarrowWidth) throw new Error("The Physicians source schedule tags do not wrap on narrow screens.");
  const unconfiguredFacultyRow = await rowForRecord(activePhysicians, "Browser unconfigured faculty");
  const zeroConfiguredSources = (await cellForHeader(activePhysicians, unconfiguredFacultyRow, "Source schedules")).locator(":scope > .data-table-tags > .data-table-tag");
  if (await zeroConfiguredSources.allTextContents().then((labels) => labels.join("|")) !== "Faculty: 0 templates") {
    throw new Error("The Physicians Faculty source summary does not retain its zero-template count.");
  }
  const unconfiguredRow = await rowForRecord(activePhysicians, "Browser unconfigured");
  const unconfiguredSources = (await cellForHeader(activePhysicians, unconfiguredRow, "Source schedules")).locator(":scope > .data-table-tags > .data-table-tag");
  if (await unconfiguredSources.allTextContents().then((labels) => labels.join("|")) !== "Community: not configured") {
    throw new Error("The Physicians unconfigured source summary is not retained.");
  }
  const facultyCapacity = await cellForHeader(activePhysicians, facultyRow, "Capacity");
  if (await facultyCapacity.locator(':scope > [aria-hidden="true"]').textContent() !== "10") {
    throw new Error("The Browser faculty Capacity cell does not render its schedule-derived value.");
  }
  const facultyCapacityMeaning = await facultyCapacity.locator(":scope > .visually-hidden").textContent();
  if (!/\bschedule-derived\b/i.test(facultyCapacityMeaning) || !/\bcapacity\b/i.test(facultyCapacityMeaning) || !/\b10\b/.test(facultyCapacityMeaning) || !/\bweekly\b/i.test(facultyCapacityMeaning)) {
    throw new Error("The Browser faculty Capacity cell does not render its accessible schedule-derived meaning.");
  }
  const customCapacityRow = await rowForRecord(activePhysicians, "Browser custom capacity");
  const customCapacity = await cellForHeader(activePhysicians, customCapacityRow, "Capacity");
  // TEXT-CONTRACT: physician-weekly-capacity-compact —
  // The directory shows custom(schedule-derived) weekly capacity with exact
  // parentheses, punctuation, and whitespace, without intervening spaces. Production:
  // schedules/views/physicians.py; protecting tests:
  // schedules/tests/test_views.py and schedules/tests/test_table_presentation_browser.py.
  if (await customCapacity.locator(':scope > [aria-hidden="true"]').textContent() !== "4.5(5)") {
    throw new Error("The Browser custom capacity is not rendered in the required compact form.");
  }
  const customCapacityMeaning = await customCapacity.locator(":scope > .visually-hidden").textContent();
  const customCapacityMeaningHasOverride = /\bcustom\b(?:(?!\bschedule(?:[-\s]+)derived\b)[\s\S])*?\bcapacity\b(?:(?!\bschedule(?:[-\s]+)derived\b)[\s\S])*?\b4\.5\b/i.test(customCapacityMeaning);
  const customCapacityMeaningHasScheduleDerivedValue = /\bschedule(?:[-\s]+)derived\b[\s\S]*?\bcapacity\b[\s\S]*?(?:^|[^\d.])5(?:$|[^\d.])/i.test(customCapacityMeaning);
  if (!customCapacityMeaningHasOverride || !customCapacityMeaningHasScheduleDerivedValue || !/\bweekly\b/i.test(customCapacityMeaning)) {
    throw new Error("The Browser custom Capacity cell does not render its accessible custom and schedule-derived meaning.");
  }

  await page.goto(input.baseUrl + "/reports/community-preceptor-stipend/?month=2025-02", { waitUntil: "domcontentloaded" });
  const stipendDetails = page.locator("#stipend-itemized-shifts");
  const emptyCell = stipendDetails.locator("tbody .data-table__empty");
  if (await emptyCell.count() !== 1) {
    throw new Error("The stipend detail table does not render its empty state.");
  }
  if (await emptyCell.getAttribute("colspan") !== String(await stipendDetails.locator("thead th[scope=col]").count())) {
    throw new Error("The stipend detail empty state does not span its supplied columns.");
  }
  const stipendTotals = page.locator("#stipend-totals");
  const totalsFooter = stipendTotals.locator("tfoot tr", { hasText: "Overall total" });
  const totalsPhysician = await cellForHeader(stipendTotals, totalsFooter, "Physician");
  if (await totalsPhysician.evaluate((cell) => cell.tagName) !== "TH" || await totalsPhysician.getAttribute("scope") !== "row") {
    throw new Error("The stipend totals table does not render its footer row header.");
  }

  await page.setContent(`
    <style>${readFileSync(input.stylesheet, "utf8")}</style>
    <ul id="wrapped-tags" class="data-table-tags" style="width: 4rem">
      <li class="data-table-tag">Faculty-equivalent</li>
      <li class="data-table-tag">Community Preceptor</li>
    </ul>
    <table style="width: 22rem"><tbody><tr><td>
      <ul id="available-cell-tags" class="data-table-tags">
        <li class="data-table-tag">Faculty-equivalent</li>
        <li class="data-table-tag">Community Preceptor</li>
      </ul>
    </td></tr></tbody></table>
    <table class="data-table"><tbody><tr>
      <th scope="row" class="data-table-cell--text data-table-cell--attention">Attention</th>
      <th scope="row" class="data-table-cell--text data-table-cell--danger">Danger</th>
    </tr></tbody></table>
  `);
  const wrappedTags = page.locator("#wrapped-tags > .data-table-tag");
  const tagsWrapOnNarrowWidth = await wrappedTags.evaluateAll((items) =>
    new Set(items.map((item) => item.getBoundingClientRect().top)).size > 1
  );
  if (!tagsWrapOnNarrowWidth) throw new Error("The tag cell does not wrap on narrow screens.");
  const labelsStayOnOneLine = await wrappedTags.evaluateAll((items) =>
    items.every((item) => {
      const label = document.createRange();
      label.selectNodeContents(item);
      return label.getClientRects().length === 1;
    })
  );
  if (!labelsStayOnOneLine) throw new Error("The tag cell breaks an individual label on narrow screens.");
  const availableCellTags = page.locator("#available-cell-tags > .data-table-tag");
  const tagsFitAvailableCell = await availableCellTags.evaluateAll((items) => {
    const cell = items[0].closest("td");
    const gap = parseFloat(getComputedStyle(items[0].parentElement).columnGap);
    return items.reduce((width, item) => width + item.getBoundingClientRect().width, gap) <= cell.clientWidth;
  });
  if (!tagsFitAvailableCell) throw new Error("The available-cell tag fixture is too narrow.");
  const tagsStayOnOneRowWhenTheyFit = await availableCellTags.evaluateAll((items) =>
    new Set(items.map((item) => item.getBoundingClientRect().top)).size === 1
  );
  if (!tagsStayOnOneRowWhenTheyFit) throw new Error("The tag cell does not use its available cell width.");
  const tones = await page.locator("th[scope=row]").evaluateAll((cells) =>
    cells.map((cell) => getComputedStyle(cell).backgroundColor)
  );
  if (tones[0] !== "rgb(255, 248, 223)" || tones[1] !== "rgb(255, 240, 240)") {
    throw new Error("Toned row-header cells do not receive their semantic fills.");
  }

  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""


class DataTableBrowserTests(LiveServerTestCase):
    serve_static = False

    @classmethod
    def setUpClass(cls):
        cls.node, cls.browser_executable = require_browser_tools()
        super().setUpClass()

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="browser-scheduler", password="browser-password"
        )
        physician = Physician.objects.create(display_name="Browser faculty")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2000-01-01",
            faculty_leadership=EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR,
        )
        FacultyAvailabilityTemplate.objects.create(
            physician=physician,
            slots={
                slot: FacultyAvailabilityTemplate.base_selection(
                    FacultyAvailabilityTemplate.AvailabilityState.OPEN
                )
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
        )
        unconfigured_faculty = Physician.objects.create(display_name="Browser unconfigured faculty")
        EffectiveRole.objects.create(
            physician=unconfigured_faculty,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2000-01-01",
        )
        custom_capacity = Physician.objects.create(
            display_name="Browser custom capacity",
            faculty_precepting_capacity_override="4.5",
        )
        EffectiveRole.objects.create(
            physician=custom_capacity,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2000-01-01",
        )
        FacultyAvailabilityTemplate.objects.create(
            physician=custom_capacity,
            slots={
                slot: FacultyAvailabilityTemplate.base_selection(
                    FacultyAvailabilityTemplate.AvailabilityState.OPEN
                    if slot.endswith("am")
                    else FacultyAvailabilityTemplate.AvailabilityState.OFF
                )
                for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES
            },
        )
        configured = Physician.objects.create(display_name="Browser configured")
        for role in (
            EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            EffectiveRole.Role.CASUAL_ROUNDER,
        ):
            EffectiveRole.objects.create(
                physician=configured,
                role=role,
                effective_start="2000-01-01",
            )
        CommunityPreceptorRule.objects.create(
            physician=configured,
            weekday=0,
            ordinals=[1],
            periods=[CommunityPreceptorRule.PeriodOfDay.AM],
        )
        CasualRounderRule.objects.create(physician=configured, weekday=0, ordinals=[1])
        unconfigured = Physician.objects.create(display_name="Browser unconfigured")
        EffectiveRole.objects.create(
            physician=unconfigured,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            effective_start="2000-01-01",
        )

    def test_shared_table_renderer_preserves_mobile_scroll_and_semantics(self):
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
                }
            ),
            capture_output=True,
            text=True,
        )

        if completed.returncode:
            self.fail(completed.stderr)
