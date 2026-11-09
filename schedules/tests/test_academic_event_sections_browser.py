import json
import shutil
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from unittest import SkipTest
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import LiveServerTestCase
from django.urls import reverse

from schedules.models import AcademicEvent, Physician
from schedules.services.academic_events import (
    create_academic_event_definition,
    create_academic_event_monthly_rule,
    create_academic_event_occurrence,
    initialize_monthly_academic_event_occurrences,
)
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
  const response = await page.goto(input.baseUrl + input.url, { waitUntil: "domcontentloaded" });
  if (!response || !response.ok()) {
    throw new Error(`Academic Events List did not load (${response ? response.status() : "no response"}).`);
  }

  // TEXT-CONTRACT: academic-event-monthly-list-disclosure-labels —
  // The List identifies monthly occurrences with the exact Monthly Events
  // heading; capitalization and whitespace matter.
  // Production: schedules/templates/schedules/academic_event_list.html.
  const monthlySection = page.getByRole("heading", { name: "Monthly Events", exact: true })
    .locator("xpath=ancestor::section[1]");
  const yearlySection = page.getByRole("heading", { name: "Scheduled yearly events", exact: true })
    .locator("xpath=ancestor::section[1]");
  if (await monthlySection.count() !== 1 || await yearlySection.count() !== 1) {
    throw new Error("Academic Events List does not expose named Monthly and scheduled Yearly sections.");
  }

  const filledSummary = monthlySection.locator("summary").filter({ hasText: /^Filled$/ });
  const unfilledSummary = monthlySection.locator("summary").filter({ hasText: /^Unfilled$/ });
  const filledDisclosure = filledSummary.locator("xpath=ancestor::details[1]");
  const unfilledDisclosure = unfilledSummary.locator("xpath=ancestor::details[1]");
  if (await filledDisclosure.count() !== 1 || await unfilledDisclosure.count() !== 1) {
    throw new Error("Monthly Events does not distinguish its Filled and Unfilled disclosures.");
  }
  // TEXT-CONTRACT: academic-event-monthly-list-disclosure-labels —
  // The List partitions monthly occurrences under Filled then Unfilled,
  // with these exact labels and order; capitalization and whitespace within each
  // label matter. Production: schedules/templates/schedules/academic_event_list.html.
  const filledPrecedesUnfilled = await filledSummary.evaluate(
    (filled, unfilled) => Boolean(
      filled.compareDocumentPosition(unfilled) & Node.DOCUMENT_POSITION_FOLLOWING),
    await unfilledSummary.elementHandle(),
  );
  if (!filledPrecedesUnfilled) {
    throw new Error("Monthly Events does not retain the Filled-before-Unfilled disclosure order.");
  }
  const summary = filledSummary;
  if (await filledDisclosure.evaluate((element) => element.open) ||
      !(await unfilledDisclosure.evaluate((element) => element.open))) {
    throw new Error("Monthly Event disclosures do not have the expected initial states.");
  }

  await summary.focus();
  if (!(await summary.evaluate((element) => document.activeElement === element))) {
    throw new Error("Filled Monthly Events summary is not keyboard focusable.");
  }
  await summary.press("Enter");
  if (!(await filledDisclosure.evaluate((element) => element.open))) {
    throw new Error("Filled Monthly Events does not open through native keyboard activation.");
  }

  async function cellText(row, header) {
    const table = row.locator("xpath=ancestor::table[1]");
    const headers = (await table.getByRole("columnheader").allTextContents()).map((text) => text.trim());
    const index = headers.indexOf(header);
    if (index < 0) throw new Error(`Academic Events List does not expose its ${header} field.`);
    return (await row.locator(":scope > th, :scope > td").nth(index).innerText()).trim();
  }

  function sameOriginUrlMatches(actual, expected) {
    const actualUrl = new URL(actual, input.baseUrl);
    const expectedUrl = new URL(expected, input.baseUrl);
    const parameters = (url) => [...url.searchParams.entries()].sort(
      ([leftName, leftValue], [rightName, rightValue]) =>
        leftName.localeCompare(rightName) || leftValue.localeCompare(rightValue));
    return actualUrl.origin === new URL(input.baseUrl).origin &&
      actualUrl.pathname === expectedUrl.pathname &&
      JSON.stringify(parameters(actualUrl)) === JSON.stringify(parameters(expectedUrl));
  }

  function representedDates(text) {
    const monthNames = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
    const monthPattern = monthNames.map((month) => `${month}|${month.slice(0, 3)}\\.?`).join("|");
    const displayedDate = new RegExp(
      `\\b(?:((${monthPattern})\\s+([0-9]{1,2})(?:st|nd|rd|th)?[\\s,./-]+([0-9]{4}))|(([0-9]{1,2})(?:st|nd|rd|th)?[\\s,./-]+(?:of\\s+)?(${monthPattern})[\\s,./-]+([0-9]{4})))\\b`,
      "gi",
    );
    const dates = [];
    for (const match of text.matchAll(displayedDate)) {
      const [monthName, day, year] = match[2]
        ? [match[2], match[3], match[4]]
        : [match[7], match[6], match[8]];
      const month = monthNames.findIndex((name) =>
        name.toLowerCase().startsWith(monthName.replace(".", "").toLowerCase())) + 1;
      dates.push(`${year}-${String(month).padStart(2, "0")}-${day.padStart(2, "0")}`);
    }
    return dates;
  }

  function inclusiveDatesMatch(text, occurrence) {
    const dates = representedDates(text);
    const expected = [occurrence.startDate, occurrence.endDate];
    if (occurrence.startDate === occurrence.endDate) {
      return (dates.length === 1 && dates[0] === occurrence.startDate) ||
        (dates.length === 2 && dates[0] === expected[0] && dates[1] === expected[1]);
    }
    return dates.length === 2 && dates[0] === expected[0] && dates[1] === expected[1];
  }

  async function occurrenceRows(disclosure, actionUrl) {
    const rows = disclosure.locator("tbody tr");
    const indexes = await rows.evaluateAll((elements, expectedUrl) => elements
      .map((row, index) => ({ row, index }))
      .filter(({ row }) => [...row.querySelectorAll("a[href]")].some((link) => {
        const actual = new URL(link.href, document.baseURI);
        const expected = new URL(expectedUrl, document.baseURI);
        const parameters = (url) => [...url.searchParams.entries()].sort(
          ([leftName, leftValue], [rightName, rightValue]) =>
            leftName.localeCompare(rightName) || leftValue.localeCompare(rightValue));
        return actual.origin === expected.origin && actual.pathname === expected.pathname &&
          JSON.stringify(parameters(actual)) === JSON.stringify(parameters(expected));
      }))
      .map(({ index }) => index), actionUrl);
    return indexes.map((index) => rows.nth(index));
  }

  async function rowsAreChronological(disclosure, occurrences) {
    const expectedUrls = occurrences.slice().sort(
      (left, right) => left.startDate.localeCompare(right.startDate)).map((occurrence) => occurrence.actionUrl);
    const displayedTargets = await disclosure.locator("tbody tr").evaluateAll((rows) => rows.map((row) =>
      [...row.querySelectorAll("a[href]")].map((link) => link.getAttribute("href"))));
    return displayedTargets.length === expectedUrls.length && displayedTargets.every(
      (targets, index) => targets.some((target) => target && sameOriginUrlMatches(target, expectedUrls[index])));
  }

  for (const occurrence of input.monthlyOccurrences) {
    const ownDisclosure = occurrence.partition === "Filled" ? filledDisclosure : unfilledDisclosure;
    const otherDisclosure = occurrence.partition === "Filled" ? unfilledDisclosure : filledDisclosure;
    const ownRows = await occurrenceRows(ownDisclosure, occurrence.actionUrl);
    const otherRows = await occurrenceRows(otherDisclosure, occurrence.actionUrl);
    if (ownRows.length !== 1 || otherRows.length !== 0 ||
        await cellText(ownRows[0], "Event") !== occurrence.name ||
        !inclusiveDatesMatch(await cellText(ownRows[0], "Inclusive dates"), occurrence) ||
        await cellText(ownRows[0], "Participants") !== occurrence.participants ||
        await cellText(ownRows[0], "Assigned") !== occurrence.assigned ||
        await cellText(ownRows[0], "Status") !== occurrence.status) {
      throw new Error(`${occurrence.name} on ${occurrence.date} is not shown once with its own coverage state.`);
    }
    const action = ownRows[0].getByRole("link", { name: occurrence.action, exact: true });
    if (await action.count() !== 1 || !sameOriginUrlMatches(await action.getAttribute("href"), occurrence.actionUrl)) {
      throw new Error(`${occurrence.name} on ${occurrence.date} does not retain its assignment action.`);
    }
  }
  if (await yearlySection.getByRole("rowheader", { name: "Yearly browser event", exact: true }).count() !== 1 ||
      await yearlySection.getByRole("rowheader", { name: "Filled monthly browser event", exact: true }).count() !== 0 ||
      await yearlySection.getByRole("rowheader", { name: "Unfilled monthly browser event", exact: true }).count() !== 0 ||
      await yearlySection.getByRole("rowheader", { name: "All Faculty monthly browser event", exact: true }).count() !== 0) {
    throw new Error("Scheduled yearly events does not retain only yearly occurrences.");
  }
  if (!(await rowsAreChronological(filledDisclosure, input.monthlyOccurrences.filter((item) => item.partition === "Filled"))) ||
      !(await rowsAreChronological(unfilledDisclosure, input.monthlyOccurrences.filter((item) => item.partition === "Unfilled")))) {
    throw new Error("Monthly Event occurrences are not chronologically ordered within their partitions.");
  }

  const unscheduledSection = page.getByRole("heading", { name: "Unscheduled yearly events", exact: true })
    .locator("xpath=ancestor::section[1]");
  const resolutionTable = unscheduledSection.getByRole("table");
  for (const header of ["Expected quarter", "Typically scheduled", "Base name", "Actions"]) {
    if (await resolutionTable.getByRole("columnheader", { name: header, exact: true }).count() !== 1) {
      throw new Error(`Unscheduled yearly events does not expose its ${header} column.`);
    }
  }
  for (const resolution of input.yearlyResolutions) {
    const rows = resolutionTable.getByRole("row");
    const matchingIndexes = await rows.evaluateAll((elements, expected) => {
      const headers = [...elements[0].closest("table").querySelectorAll("thead th")]
        .map((header) => header.innerText.trim());
      const baseNameIndex = headers.indexOf("Base name");
      return elements.map((row, index) => ({ row, index })).filter(({ row }) =>
        row.querySelectorAll(":scope > th, :scope > td")[baseNameIndex]?.innerText.trim() === expected)
        .map(({ index }) => index);
    }, resolution.name);
    if (matchingIndexes.length !== 1) {
      throw new Error(`${resolution.name} is not uniquely identified in its own yearly row.`);
    }
    const row = rows.nth(matchingIndexes[0]);
    if (await cellText(row, "Expected quarter") !== resolution.quarter ||
        await cellText(row, "Typically scheduled") !== resolution.typicalSchedule) {
      throw new Error(`${resolution.name} does not retain its named yearly scheduling fields.`);
    }
    const links = row.getByRole("link");
    const forms = row.locator("form");
    if (await links.count() !== 1 || await forms.count() !== 1 ||
        !sameOriginUrlMatches(await links.first().getAttribute("href"), resolution.scheduleUrl) ||
        !sameOriginUrlMatches(await forms.first().getAttribute("action"), resolution.notOccurringUrl) ||
        await forms.first().getAttribute("method") !== "post") {
      throw new Error(`${resolution.name} does not retain its own yearly resolution controls.`);
    }
  }
  const desktopLayout = await resolutionTable.evaluate((table) => {
    const columnIndex = (name) => [...table.querySelectorAll("thead th")]
      .findIndex((header) => header.innerText.trim() === name);
    const byName = (name) => table.querySelectorAll("thead th")[columnIndex(name)].getBoundingClientRect();
    const style = getComputedStyle(table.closest(".surface-body"));
    const contentWidth = table.closest(".surface-body").clientWidth -
      parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const actionIndex = columnIndex("Actions");
    const actionCells = [...table.querySelectorAll("tbody tr")].map(
      (row) => row.querySelectorAll(":scope > th, :scope > td")[actionIndex]);
    const controlsStayOnOneLine = actionCells.every((cell) => {
      const controls = [...cell.querySelectorAll("a, button")];
      const renderedLineCount = (control) => {
        const range = document.createRange();
        range.selectNodeContents(control);
        const lineTops = [];
        for (const rect of range.getClientRects()) {
          if (rect.width > 0 && rect.height > 0 && !lineTops.some((top) => Math.abs(top - rect.top) <= 1)) {
            lineTops.push(rect.top);
          }
        }
        return lineTops.length;
      };
      return controls.length === 2 && controls.map((control) => control.innerText.trim()).join("|") ===
        "Schedule|Not occurring this year" && Math.abs(
          controls[0].getBoundingClientRect().top - controls[1].getBoundingClientRect().top) <= 1 &&
        controls.every((control) => renderedLineCount(control) === 1);
    });
    const controlsFit = actionCells.every((cell) => {
      const cellStyle = getComputedStyle(cell);
      const available = cell.clientWidth - parseFloat(cellStyle.paddingLeft) - parseFloat(cellStyle.paddingRight);
      const bounds = [...cell.querySelectorAll("a, button")].map((control) => control.getBoundingClientRect());
      return bounds.every((bound) => bound.width <= available + 1) &&
        !bounds.some((bound, index) => bounds.slice(index + 1).some((other) =>
          bound.left < other.right && other.left < bound.right && bound.top < other.bottom && other.top < bound.bottom));
    });
    const baseName = byName("Base name");
    return {
      usesSurfaceWidth: Math.abs(table.clientWidth - contentWidth) < 2,
      baseNameGetsRemainingWidth: baseName.width > byName("Expected quarter").width && baseName.width > byName("Typically scheduled").width && baseName.width > byName("Actions").width,
      controlsStayOnOneLine,
      controlsFit,
    };
  });
  if (!desktopLayout.usesSurfaceWidth || !desktopLayout.baseNameGetsRemainingWidth || !desktopLayout.controlsStayOnOneLine || !desktopLayout.controlsFit) {
    throw new Error("Unscheduled yearly events does not keep its content-aware desktop layout and controls intact.");
  }
  await page.setViewportSize({ width: 400, height: 900 });
  const narrowLayout = await resolutionTable.evaluate((table) => {
    const headers = [...table.querySelectorAll("thead th")];
    const actionIndex = headers.findIndex((header) => header.innerText.trim() === "Actions");
    const cells = [...table.querySelectorAll("tbody tr")].map(
      (row) => row.querySelectorAll(":scope > th, :scope > td")[actionIndex]);
    return document.documentElement.scrollWidth <= window.innerWidth && cells.every((cell) => {
      const bounds = [...cell.querySelectorAll("a, button")].map((action) => action.getBoundingClientRect());
      const container = cell.getBoundingClientRect();
      return cell.scrollWidth <= cell.clientWidth && bounds.every((bound) =>
        bound.left >= container.left - 1 && bound.right <= container.right + 1 &&
        bound.top >= container.top - 1 && bound.bottom <= container.bottom + 1) &&
        !bounds.some((bound, index) => bounds.slice(index + 1).some((other) =>
          bound.left < other.right && other.left < bound.right && bound.top < other.bottom && other.top < bound.bottom));
    });
  });
  if (!narrowLayout) throw new Error("Unscheduled yearly resolution controls do not wrap cleanly on a narrow screen.");

  await browser.close();
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""


class AcademicEventSectionsBrowserTests(LiveServerTestCase):
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
        self.scheduler = get_user_model().objects.create_user(username="browser-scheduler")
        self.client.force_login(self.scheduler)
        self.filled_physician = Physician.objects.create(display_name="Filled browser physician")
        self.unfilled_physician = Physician.objects.create(
            display_name="Unfilled browser physician"
        )
        monthly_definition = create_academic_event_definition(
            base_name="Filled monthly browser event",
            scheduling_mode="monthly_rules",
            start_academic_year=2024,
            default_participants=(self.filled_physician,),
            scheduler=self.scheduler,
        )
        create_academic_event_monthly_rule(
            definition=monthly_definition,
            weekday=3,
            ordinals=[3],
            effective_start=date(2025, 1, 16),
            effective_end=date(2025, 1, 16),
            scheduler=self.scheduler,
        )
        unfilled_monthly_definition = create_academic_event_definition(
            base_name="Unfilled monthly browser event",
            scheduling_mode="monthly_rules",
            start_academic_year=2024,
            default_required_participant_count=2,
            default_participants=(self.unfilled_physician,),
            scheduler=self.scheduler,
        )
        create_academic_event_monthly_rule(
            definition=unfilled_monthly_definition,
            weekday=3,
            ordinals=[2],
            effective_start=date(2025, 1, 9),
            effective_end=date(2025, 1, 9),
            scheduler=self.scheduler,
        )
        all_faculty_monthly_definition = create_academic_event_definition(
            base_name="All Faculty monthly browser event",
            scheduling_mode="monthly_rules",
            start_academic_year=2024,
            all_available_faculty=True,
            scheduler=self.scheduler,
        )
        create_academic_event_monthly_rule(
            definition=all_faculty_monthly_definition,
            weekday=3,
            ordinals=[1],
            effective_start=date(2024, 8, 1),
            effective_end=date(2024, 8, 1),
            scheduler=self.scheduler,
        )
        yearly_definition = create_academic_event_definition(
            base_name="Yearly browser event",
            start_academic_year=2024,
            scheduler=self.scheduler,
        )
        self.unscheduled_yearly_definition = create_academic_event_definition(
            base_name="Unscheduled yearly browser event",
            expected_quarter="Q1",
            typically_scheduled="Early August",
            start_academic_year=2024,
            scheduler=self.scheduler,
        )
        self.second_unscheduled_yearly_definition = create_academic_event_definition(
            base_name="Long named unscheduled yearly browser event",
            expected_quarter="Q2",
            typically_scheduled="Late November planning window",
            start_academic_year=2024,
            scheduler=self.scheduler,
        )
        create_academic_event_occurrence(
            definition=yearly_definition,
            academic_year=2024,
            start_date=date(2024, 8, 1),
            end_date=date(2024, 8, 1),
            scheduler=self.scheduler,
        )
        initialize_monthly_academic_event_occurrences(
            start_date=date(2024, 8, 1),
            end_date=date(2025, 7, 31),
            scheduler=self.scheduler,
        )
        self.filled_occurrence = AcademicEvent.objects.get(
            definition=monthly_definition, start_date=date(2025, 1, 16)
        )
        self.unfilled_occurrence = AcademicEvent.objects.get(
            definition=unfilled_monthly_definition, start_date=date(2025, 1, 9)
        )
        self.all_faculty_occurrence = AcademicEvent.objects.get(
            definition=all_faculty_monthly_definition, start_date=date(2024, 8, 1)
        )

    @patch(
        "schedules.services.operational_attention.timezone.now",
        return_value=datetime(2024, 8, 1, 18, tzinfo=UTC),
    )
    def test_list_uses_partitioned_keyboard_operable_monthly_disclosures(self, _now):
        completed = subprocess.run(
            [self.node, "-e", _BROWSER_RUNNER],
            input=json.dumps(
                {
                    "baseUrl": self.live_server_url,
                    "browserExecutable": self.browser_executable,
                    "stylesheet": str(
                        Path(__file__).resolve().parents[1] / "static/schedules/application.css"
                    ),
                    "url": f"{reverse('academic-event-list')}?academic_year=2024",
                    "monthlyOccurrences": [
                        {
                            "name": "All Faculty monthly browser event",
                            "date": "2024-08-01",
                            "startDate": "2024-08-01",
                            "endDate": "2024-08-01",
                            "participants": "All available Faculty",
                            "assigned": "All Faculty",
                            "status": "Filled",
                            "partition": "Filled",
                            "action": "Edit assignments",
                            "actionUrl": (
                                f"{reverse('academic-event-monthly-assignment-correct', args=(self.all_faculty_occurrence.pk,))}"
                                "?academic_year=2024&view=list"
                            ),
                        },
                        {
                            "name": "Filled monthly browser event",
                            "date": "2025-01-16",
                            "startDate": "2025-01-16",
                            "endDate": "2025-01-16",
                            "participants": "Filled browser physician",
                            "assigned": "1/1",
                            "status": "Filled",
                            "partition": "Filled",
                            "action": "Edit assignments",
                            "actionUrl": (
                                f"{reverse('academic-event-monthly-assignment-correct', args=(self.filled_occurrence.pk,))}"
                                "?academic_year=2024&view=list"
                            ),
                        },
                        {
                            "name": "Unfilled monthly browser event",
                            "date": "2025-01-09",
                            "startDate": "2025-01-09",
                            "endDate": "2025-01-09",
                            "participants": "Unfilled browser physician",
                            "assigned": "1/2",
                            "status": "Assignment incomplete (1 needed)",
                            "partition": "Unfilled",
                            "action": "Edit assignments",
                            "actionUrl": (
                                f"{reverse('academic-event-monthly-assignment-correct', args=(self.unfilled_occurrence.pk,))}"
                                "?academic_year=2024&view=list"
                            ),
                        },
                    ],
                    "yearlyResolutions": [
                        {
                            "name": "Unscheduled yearly browser event",
                            "quarter": "Q1",
                            "typicalSchedule": "Early August",
                            "scheduleUrl": (
                                f"{reverse('academic-event-create')}?academic_year=2024&view=list"
                                f"&definition={self.unscheduled_yearly_definition.pk}"
                            ),
                            "notOccurringUrl": (
                                f"{reverse('academic-event-mark-not-occurring', args=(self.unscheduled_yearly_definition.pk,))}"
                                "?academic_year=2024&view=list"
                            ),
                        },
                        {
                            "name": self.second_unscheduled_yearly_definition.base_name,
                            "quarter": "Q2",
                            "typicalSchedule": "Late November planning window",
                            "scheduleUrl": (
                                f"{reverse('academic-event-create')}?academic_year=2024&view=list"
                                f"&definition={self.second_unscheduled_yearly_definition.pk}"
                            ),
                            "notOccurringUrl": (
                                f"{reverse('academic-event-mark-not-occurring', args=(self.second_unscheduled_yearly_definition.pk,))}"
                                "?academic_year=2024&view=list"
                            ),
                        },
                    ],
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
