import json
import shutil
import subprocess
from datetime import date
from pathlib import Path
from unittest import SkipTest

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import LiveServerTestCase

from schedules.models import EffectiveRole, FacultyAvailabilityTemplate, PreceptorAssignment
from schedules.services.academic_events import (
    create_academic_event_definition,
    create_academic_event_occurrence,
)
from schedules.services.community import create_community_preceptor_rule
from schedules.services.physicians import create_faculty_availability_template, create_physician
from schedules.services.preceptors import assign_preceptor, open_weekly_draft
from schedules.services.rounding import create_casual_rounder_rule, record_normal_rounder
from schedules.services.vacations import create_vacation

_BROWSER_RUNNER = r"""
const { execFileSync } = require("child_process");
const { readFileSync } = require("fs");
const path = require("path");

const input = JSON.parse(readFileSync(0, "utf8"));
const globalRoot = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
const { chromium } = require(path.join(globalRoot, "@playwright/cli/node_modules/playwright"));

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: input.browserExecutable });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.route("**/*", (route) => {
    if (route.request().url().endsWith("/static/schedules/application.css")) {
      return route.fulfill({ body: readFileSync(input.stylesheet), contentType: "text/css" });
    }
    return route.request().resourceType() === "document" ? route.continue() : route.abort();
  });
  await page.goto(input.baseUrl + "/schedule/?date=2025-02-03", { waitUntil: "domcontentloaded" });
  await page.locator('input[name="username"]').fill(input.username);
  await page.locator('input[name="password"]').fill(input.password);
  await page.locator('button[type="submit"]').click();
  await page.waitForLoadState("domcontentloaded");
  if (await page.locator(".master-schedule-surface").count() !== 2) {
    throw new Error("Master Schedule does not show its two weekly grids at desktop width.");
  }
  const titleSurface = page.locator(".master-schedule-title-surface");
  if (await titleSurface.count() !== 1 || !await titleSurface.locator(".surface-header").count()) {
    throw new Error("Master Schedule title is not contained in the shared surface header.");
  }
  const titleRow = titleSurface.locator(".master-schedule-title-row");
  const titleHeading = titleSurface.getByRole("heading", { level: 1 });
  if (await titleHeading.evaluate((heading) => parseFloat(getComputedStyle(heading).fontSize)) >= 32) {
    throw new Error("Master Schedule heading remains visually dominant.");
  }
  const pdfAction = titleSurface.getByRole("link", { name: "PDF" });
  if (await pdfAction.getAttribute("href") !== "/schedule/pdf/?date=2025-02-03") {
    throw new Error("Master Schedule PDF action does not carry the selected week.");
  }
  const titleIsContained = await titleSurface.evaluate((surface) => {
    const heading = surface.querySelector("h1").getBoundingClientRect();
    const action = surface.querySelector("a").getBoundingClientRect();
    const bounds = surface.getBoundingClientRect();
    const contains = (rect) => rect.left >= bounds.left && rect.right <= bounds.right
      && rect.top >= bounds.top && rect.bottom <= bounds.bottom;
    return contains(heading) && contains(action) && action.left > heading.right
      && Math.abs((heading.top + heading.bottom) / 2 - (action.top + action.bottom) / 2) < Math.max(heading.height, action.height);
  });
  if (!titleIsContained) throw new Error("Master Schedule title and PDF action are not aligned within the compact title surface.");
  const pdfActionIsRightAligned = await titleRow.evaluate((row) => {
    const action = row.querySelector("a").getBoundingClientRect();
    const paddingRight = parseFloat(getComputedStyle(row).paddingRight);
    return Math.abs(action.right + paddingRight - row.getBoundingClientRect().right) < 1;
  });
  if (!pdfActionIsRightAligned) throw new Error("Master Schedule PDF action is not right-aligned.");
  const dayHeadings = await page.locator(".master-schedule-grid thead time").allInnerTexts();
  if (JSON.stringify(dayHeadings) !== JSON.stringify([
    "Mon 2/3", "Tue 2/4", "Wed 2/5", "Thu 2/6", "Fri 2/7",
    "Sun 2/2", "Mon 2/3", "Tue 2/4", "Wed 2/5", "Thu 2/6", "Fri 2/7", "Sat 2/8",
  ])) {
    throw new Error("Master Schedule day headings do not use abbreviated weekday and numeric month/day.");
  }
  const clinicAssignments = await page.locator(".master-schedule-grid--clinic").evaluate((table, names) => {
    const headers = Array.from(table.tHead.rows[0].cells).slice(1);
    const dayFor = (cell) => {
      const cellRect = cell.getBoundingClientRect();
      const header = headers.find((candidate) => {
        const headerRect = candidate.getBoundingClientRect();
        return cellRect.left >= headerRect.left && cellRect.right <= headerRect.right;
      });
      return header?.innerText.trim();
    };
    return Array.from(table.tBodies)
      .filter((body) => ["Morning", "Afternoon"].includes(body.querySelector('th[scope="rowgroup"]')?.textContent.trim()))
      .flatMap((body) => {
      const period = body.querySelector('th[scope="rowgroup"]').textContent.trim();
      return Array.from(body.rows).flatMap((row) => {
        const position = row.querySelector('th[scope="row"]')?.innerText.trim();
        return Array.from(row.querySelectorAll("td")).flatMap((cell) => names
          .filter((name) => cell.innerText.includes(name))
          .map((name) => ({ name, period, position, date: dayFor(cell) })));
      });
    });
  }, [input.focusPhysicianName, input.outsidePhysicianName]);
  if (JSON.stringify(clinicAssignments) !== JSON.stringify([
    { name: input.focusPhysicianName, period: "Morning", position: "Preceptor 1", date: "Mon 2/3" },
    { name: input.outsidePhysicianName, period: "Afternoon", position: "Preceptor 2", date: "Tue 2/4" },
  ])) {
    throw new Error("Master Schedule assignments are not rendered in their date and period lanes.");
  }
  if (!await page.getByText("Academic Event").count()) {
    throw new Error("Master Schedule did not render representative supporting facts.");
  }
  if (await page.locator(".master-schedule-surface a").count()) {
    throw new Error("Master Schedule contents still include navigation links.");
  }
  const clinicTable = page.getByRole("region", { name: /clinic schedule/i })
    .getByRole("table", { name: /clinic schedule/i });
  if (await clinicTable.count() !== 1) {
    throw new Error("The clinic schedule lacks meaningful accessible region and table labels.");
  }
  if (await clinicTable.locator('thead th[scope="col"][colspan="2"]').innerText() !== "Clinic") {
    throw new Error("Clinic does not use one header spanning its two stub columns.");
  }
  const gridSummaryHeaders = await page.locator(".master-schedule-surface .surface-header").allInnerTexts();
  if (JSON.stringify(gridSummaryHeaders) !== JSON.stringify(["Supporting schedules"])) {
    throw new Error("Master Schedule retains redundant grid summary text.");
  }
  const supportingLabels = await page.locator(".master-schedule-grid").evaluateAll((tables) => {
    const supporting = tables.find((table) => !table.classList.contains("master-schedule-grid--clinic"));
    return {
      groups: Array.from(supporting.querySelectorAll('.master-schedule-grid-group th')).map((header) => header.textContent.trim()),
      rows: Array.from(supporting.querySelectorAll('th[scope="row"]')).map((header) => header.innerText.trim()),
    };
  });
  if (JSON.stringify(supportingLabels) !== JSON.stringify({
    groups: ["Rounding", "Community Precepting", "Vacation", "Academic Events"],
    rows: ["Normal", "Casual", "Preceptor", "Vacation", "Academic Events"],
  })) {
    throw new Error("Supporting Schedule labels do not use their accepted abbreviations.");
  }
  const clinicGroups = await page.locator(".master-schedule-grid--clinic").evaluate((table) => {
    return Array.from(table.querySelectorAll('th[scope="rowgroup"]')).map((header) => {
      const rows = Array.from(header.closest("tbody").rows);
      const index = rows.indexOf(header.parentElement);
      return {
        label: header.textContent.trim(),
        rowSpan: header.rowSpan,
        rows: rows.slice(index, index + header.rowSpan).map((row) => row.querySelector('th[scope="row"]')?.innerText.trim()),
      };
    });
  });
  if (JSON.stringify(clinicGroups) !== JSON.stringify([
    { label: "Morning", rowSpan: 3, rows: ["Preceptor 1", "Preceptor 2", "Secondary"] },
    { label: "Afternoon", rowSpan: 4, rows: ["Preceptor 1", "Preceptor 2", "Secondary", "Late Doc"] },
  ])) {
    throw new Error("Clinic period row-group headers do not span their position rows.");
  }
  const counterclockwiseLabels = async () => page.locator(".master-schedule-grid-rowgroup-label").evaluateAll((labels) => labels.every((label) => {
    const characters = label.firstChild;
    const characterRect = (index) => {
      const range = document.createRange();
      range.setStart(characters, index);
      range.setEnd(characters, index + 1);
      return range.getBoundingClientRect();
    };
    const first = characterRect(0);
    const last = characterRect(characters.length - 1);
    const labelRect = label.getBoundingClientRect();
    return labelRect.height > labelRect.width && first.top > last.top;
  }));
  if (!await counterclockwiseLabels()) {
    throw new Error("Clinic period labels do not render counterclockwise with their first letters at the bottom.");
  }
  const dayAlignment = await page.locator(".master-schedule-grid--clinic").evaluate((table) => {
    const headerLefts = Array.from(table.tHead.rows[0].cells).slice(1).map((cell) => Math.round(cell.getBoundingClientRect().left));
    const firstPositionRow = table.tBodies[0].rows[0];
    const cellLefts = Array.from(firstPositionRow.cells).slice(2).map((cell) => Math.round(cell.getBoundingClientRect().left));
    return JSON.stringify(headerLefts) === JSON.stringify(cellLefts);
  });
  if (!dayAlignment) throw new Error("Clinic row-group stubs misalign the day columns.");
  const masterGridPresentation = await page.locator(".master-schedule-grid").evaluateAll((tables) => {
    const clinic = tables.find((table) => table.classList.contains("master-schedule-grid--clinic"));
    const supporting = tables.find((table) => !table.classList.contains("master-schedule-grid--clinic"));
    const coordinateHeader = clinic.tHead.querySelector('th[scope="col"]');
    const dateHeaderTypography = Array.from(tables.flatMap((table) => Array.from(table.querySelectorAll("thead th time")))).map((header) => ({
      fontSize: parseFloat(getComputedStyle(header).fontSize),
      fontWeight: parseFloat(getComputedStyle(header).fontWeight),
      renderedHeight: header.getBoundingClientRect().height,
    }));
    const roleStubs = Array.from(tables.flatMap((table) => Array.from(table.querySelectorAll('th[scope="row"]'))));
    const factLabels = Array.from(tables.flatMap((table) => Array.from(table.querySelectorAll(".master-schedule-grid-fact > strong:not(.master-schedule-grid-attention)"))));
    const rowGroups = Array.from(clinic.querySelectorAll('th[scope="rowgroup"]'));
    const scheduleGroups = Array.from(supporting.querySelectorAll(".master-schedule-grid-group th"));
    const presentation = (element) => ({
      background: getComputedStyle(element).backgroundColor,
      color: getComputedStyle(element).color,
      fontWeight: parseFloat(getComputedStyle(element).fontWeight),
    });
    return {
      dateHeaderTypography,
      roleWeights: roleStubs.map((stub) => parseFloat(getComputedStyle(stub).fontWeight)),
      factLabelWeights: factLabels.map((label) => parseFloat(getComputedStyle(label).fontWeight)),
      surfaceHeader: presentation(document.querySelector(".master-schedule-title-row")),
      coordinateHeader: presentation(coordinateHeader),
      rowGroups: rowGroups.map(presentation),
      scheduleGroups: scheduleGroups.map(presentation),
    };
  });
  if (!masterGridPresentation.dateHeaderTypography.length || !masterGridPresentation.dateHeaderTypography.every((style) => style.fontSize > 0 && style.renderedHeight >= style.fontSize && style.fontWeight >= Math.max(...masterGridPresentation.roleWeights))) {
    throw new Error("Master Schedule date labels are not legible calendar coordinates with their required hierarchy.");
  }
  const semanticHeaders = [masterGridPresentation.coordinateHeader, ...masterGridPresentation.rowGroups, ...masterGridPresentation.scheduleGroups];
  const ordinaryWeights = [...masterGridPresentation.roleWeights, ...masterGridPresentation.factLabelWeights];
  if (!ordinaryWeights.length || !semanticHeaders.every((header) => ordinaryWeights.every((weight) => header.fontWeight > weight))) {
    throw new Error("Master Schedule semantic headers do not retain stronger emphasis than ordinary labels.");
  }
  const roleTones = [masterGridPresentation.surfaceHeader, masterGridPresentation.coordinateHeader, masterGridPresentation.rowGroups[0]];
  if (new Set(roleTones.map((role) => role.background)).size !== 3) {
    throw new Error("Subtle, calendar, and schedule-group tones must remain visually distinct.");
  }
  if (!await page.locator('.master-schedule-grid th[scope="row"]').evaluateAll((stubs, subtle) => stubs.every((stub) => getComputedStyle(stub).backgroundColor === subtle), masterGridPresentation.surfaceHeader.background)) {
    throw new Error("Master Schedule role stubs do not retain the subtle surface treatment.");
  }
  if (masterGridPresentation.rowGroups.length !== 2 || !masterGridPresentation.scheduleGroups.length || !masterGridPresentation.rowGroups.every((group) => group.background === masterGridPresentation.scheduleGroups[0].background) || !masterGridPresentation.scheduleGroups.every((group) => group.background === masterGridPresentation.rowGroups[0].background)) {
    throw new Error("Master Schedule row groups do not share the schedule-group treatment.");
  }
  const channel = (color) => color.match(/\d+(?:\.\d+)?/g).slice(0, 3).map(Number);
  const contrast = ({ color, background }) => {
    const luminance = (value) => channel(value).map((part) => part / 255).map((part) => part <= .03928 ? part / 12.92 : ((part + .055) / 1.055) ** 2.4).reduce((result, part, index) => result + part * [0.2126, 0.7152, 0.0722][index], 0);
    const [foreground, surface] = [luminance(color), luminance(background)].sort((left, right) => right - left);
    return (foreground + .05) / (surface + .05);
  };
  if (!semanticHeaders.every((header) => contrast(header) >= 3)) {
    throw new Error("Master Schedule headers do not retain readable contrast.");
  }
  await page.locator(".physician-scope-chooser summary").click();
  await page.locator(`input[name="physicians"][value="${input.focusPhysicianId}"]`).check();
  await page.getByRole("button", { name: "Apply focus" }).click();
  await page.waitForLoadState("domcontentloaded");
  if (!new URL(await pdfAction.getAttribute("href"), input.baseUrl).searchParams.getAll("physicians").includes(String(input.focusPhysicianId))) {
    throw new Error("Master Schedule PDF action does not carry the physician focus.");
  }
  const availability = await page.locator(".master-schedule-grid tr", { hasText: "Available faculty" }).first().innerText();
  if (!availability.includes(input.focusPhysicianName) || availability.includes(input.outsidePhysicianName)) {
    throw new Error("Focused Faculty availability does not retain the selected physician only.");
  }
  await page.getByRole("button", { name: "Everyone" }).click();
  await page.waitForLoadState("domcontentloaded");
  await page.setViewportSize({ width: 320, height: 800 });
  const scrollPositions = await page.locator(".master-schedule-surface .table-scroll-frame").evaluateAll((frames) => {
    if (frames.length !== 2) throw new Error("Master Schedule does not show two weekly grids.");
    return frames.map((frame) => {
      if (frame.scrollWidth <= frame.clientWidth) {
        throw new Error("Master Schedule grid does not retain horizontal scrolling at 320px.");
      }
      frame.scrollLeft = frame.scrollWidth - frame.clientWidth;
      return frame.scrollLeft;
    });
  });
  if (scrollPositions.some((position) => position <= 0)) throw new Error("Master Schedule grid did not scroll horizontally.");
  const clinicStubWidths = await page.locator(".master-schedule-grid--clinic").evaluate((table) => {
    const firstRow = table.tBodies[0].rows[0];
    return [firstRow.cells[0].getBoundingClientRect().width, firstRow.cells[1].getBoundingClientRect().width];
  });
  if (clinicStubWidths[0] > 32 || clinicStubWidths[1] < 70) {
    throw new Error("Clinic period stub is too broad or its position stub is not legible at narrow widths.");
  }
  const rowGroupLabelCenters = await page.locator('.master-schedule-grid--clinic th[scope="rowgroup"]').evaluateAll((headers) => headers.every((header) => {
    const label = header.querySelector(".master-schedule-grid-rowgroup-label");
    const headerRect = header.getBoundingClientRect();
    const labelRect = label.getBoundingClientRect();
    return Math.abs((headerRect.left + headerRect.width / 2) - (labelRect.left + labelRect.width / 2)) < 1
      && Math.abs((headerRect.top + headerRect.height / 2) - (labelRect.top + labelRect.height / 2)) < 1;
  }));
  if (!rowGroupLabelCenters) {
    throw new Error("Clinic period row-group labels are not centered in their spanning cells.");
  }
  await page.emulateMedia({ media: "print" });
  if (await page.locator(".site-header").evaluate((header) => getComputedStyle(header).display) !== "none") {
    throw new Error("Master Schedule print rendering did not remove application controls.");
  }
  if (await page.locator('.master-schedule-grid--clinic th[scope="rowgroup"]').count() !== 2) {
    throw new Error("Clinic row-group headers do not survive print rendering.");
  }
  if (!await counterclockwiseLabels()) {
    throw new Error("Clinic period labels do not retain counterclockwise glyph orientation in print rendering.");
  }
  await page.goto(input.baseUrl + "/schedule/?date=2025-09-01", { waitUntil: "domcontentloaded" });
  const laborDay = page.locator('.master-schedule-grid--clinic th[scope="col"]', { hasText: "Labor Day" });
  if (await laborDay.count() !== 1 || await laborDay.getAttribute("aria-label")) {
    throw new Error("Labor Day must be an informational Master Schedule header label.");
  }
  if (await laborDay.locator('.calendar-holiday-header[aria-label="Holiday: Labor Day, AM and PM"]').count() !== 1) {
    throw new Error("Labor Day does not retain its full-day informative header label.");
  }
  const holidayPresentation = await page.locator(".master-schedule-grid:not(.master-schedule-grid--clinic)").evaluate((table) => {
    const holidayColumn = Array.from(table.tHead.rows[0].cells).findIndex((cell) => cell.textContent.includes("Labor Day"));
    const dataRows = Array.from(table.tBodies[0].rows)
      .filter((row) => !row.classList.contains("master-schedule-grid-group"))
    return {
      holiday: dataRows.map((row) => getComputedStyle(row.cells[holidayColumn]).backgroundColor),
      ordinary: getComputedStyle(dataRows[0].cells[holidayColumn + 1]).backgroundColor,
      header: getComputedStyle(table.tHead.rows[0].cells[holidayColumn]).backgroundColor,
    };
  });
  if (holidayPresentation.holiday.length !== 5 || new Set(holidayPresentation.holiday).size !== 1 || holidayPresentation.holiday[0] === holidayPresentation.ordinary || holidayPresentation.header !== masterGridPresentation.coordinateHeader.background) {
    throw new Error("Labor Day does not retain its holiday tint without changing ordinary cells or calendar headers.");
  }
  const closureGeometry = async () => page.locator(".master-schedule-grid--clinic").evaluate((table) => {
    const headers = Array.from(table.tHead.rows[0].cells).slice(1);
    const dayFor = (cell) => {
      const cellRect = cell.getBoundingClientRect();
      return headers.find((header) => {
        const headerRect = header.getBoundingClientRect();
        return cellRect.left >= headerRect.left && cellRect.right <= headerRect.right;
      })?.querySelector("time")?.textContent.trim();
    };
    return Array.from(table.tBodies)
      .filter((body) => ["Morning", "Afternoon"].includes(body.querySelector('th[scope="rowgroup"]')?.textContent.trim()))
      .flatMap((body) => Array.from(body.querySelectorAll("td.master-schedule-grid-closed")).map((cell) => ({
      period: body.querySelector('th[scope="rowgroup"]').textContent.trim(),
      date: dayFor(cell),
      rowSpan: cell.rowSpan,
    })));
  });
  if (JSON.stringify(await closureGeometry()) !== JSON.stringify([
    { period: "Morning", date: "Mon 9/1", rowSpan: 3 },
    { period: "Afternoon", date: "Mon 9/1", rowSpan: 4 },
  ])) {
    throw new Error("Labor Day does not close the matching date lane in each clinic period.");
  }
  await page.goto(input.baseUrl + "/schedule/?date=2025-12-24", { waitUntil: "domcontentloaded" });
  const christmasEve = page.locator('.master-schedule-grid th[scope="col"]', { hasText: "Christmas Eve, PM" });
  if (await christmasEve.count() !== 2 || await christmasEve.locator('.calendar-holiday-header[aria-label="Holiday: Christmas Eve, PM only"]').count() !== 2) {
    throw new Error("Christmas Eve does not retain its PM-only informative labels in both grids.");
  }
  if (JSON.stringify(await closureGeometry()) !== JSON.stringify([
    { period: "Morning", date: "Thu 12/25", rowSpan: 4 },
    { period: "Afternoon", date: "Wed 12/24", rowSpan: 4 },
    { period: "Afternoon", date: "Thu 12/25", rowSpan: 4 },
  ])) {
    throw new Error("Christmas Eve must close only the clinic afternoon block.");
  }
  await browser.close();
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""


class MasterScheduleWorkspaceBrowserTests(LiveServerTestCase):
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
        self.user = get_user_model().objects.create_user(
            username="browser-scheduler", password="browser-password"
        )
        administrators, _ = Group.objects.get_or_create(name="Scheduling Administrators")
        administrators.permissions.add(
            Permission.objects.get(codename="manage_scheduling_administration")
        )
        self.user.groups.add(administrators)
        self.monday = date(2025, 2, 3)
        self.focused = self.faculty("Focused Faculty")
        self.outside = self.faculty("Outside Faculty")
        session = next(
            session for session in open_weekly_draft(self.monday) if session.period_of_day == "AM"
        )
        assign_preceptor(
            session=session,
            physician=self.focused,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.user,
        )
        afternoon_session = next(
            session
            for session in open_weekly_draft(self.monday)
            if (session.date, session.period_of_day) == (date(2025, 2, 4), "PM")
        )
        assign_preceptor(
            session=afternoon_session,
            physician=self.outside,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=2,
            scheduler=self.user,
        )
        definition = create_academic_event_definition(
            base_name="Academic Event",
            start_academic_year=2024,
            expected_quarter="Q4",
            default_required_participant_count=1,
            scheduler=self.user,
        )
        create_academic_event_occurrence(
            definition=definition,
            academic_year=2024,
            start_date=self.monday,
            end_date=self.monday,
            participants=(self.outside,),
            scheduler=self.user,
        )
        create_vacation(
            physician=self.outside,
            start_date=self.monday,
            end_date=self.monday,
            period_of_day="PM",
            scheduler=self.user,
        )
        create_community_preceptor_rule(
            physician=create_physician(display_name="Community", scheduler=self.user),
            weekday=0,
            ordinals=[1],
            periods=["PM"],
            scheduler=self.user,
        )
        normal_rounder = self.faculty("Normal rounder")
        record_normal_rounder(week=self.monday, physician=normal_rounder, scheduler=self.user)
        create_casual_rounder_rule(
            physician=create_physician(display_name="Casual rounder", scheduler=self.user),
            weekday=0,
            ordinals=[1],
            scheduler=self.user,
        )

    def faculty(self, name):
        physician = create_physician(display_name=name, scheduler=self.user)
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2024, 1, 1),
        )
        create_faculty_availability_template(
            physician=physician,
            effective_start=None,
            effective_end=None,
            slots={slot: "open" for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.user,
        )
        return physician

    def test_renders_representative_schedule_at_desktop_narrow_focus_and_print(self):
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
                    "focusPhysicianId": self.focused.pk,
                    "focusPhysicianName": self.focused.display_name,
                    "outsidePhysicianName": self.outside.display_name,
                }
            ),
            capture_output=True,
            text=True,
        )

        if completed.returncode:
            self.fail(completed.stderr)
