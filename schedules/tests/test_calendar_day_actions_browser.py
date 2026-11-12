import json
import subprocess
from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from schedules.models import (
    CasualRounderRule,
    CommunityPreceptorRule,
    EffectiveRole,
    NormalRounder,
    Physician,
    Vacation,
)
from schedules.services.interviews import create_interview_shift
from schedules.tests.browser_tools import require_browser_tools

_BROWSER_RUNNER = r"""
const { readFileSync } = require("fs");

const input = JSON.parse(readFileSync(0, "utf8"));
const { chromium } = require(process.env.PRECEPTOR_PLAYWRIGHT_MODULE);

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: input.browserExecutable });
  try {
  const context = await browser.newContext({ viewport: { width: 1280, height: 3000 } });
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
    if (route.request().url().endsWith("/static/schedules/calendar-day-actions.js")) {
      return route.fulfill({ body: readFileSync(input.script), contentType: "application/javascript" });
    }
    return route.request().resourceType() === "document" ? route.continue() : route.abort();
  });

  const urlMatches = (candidate, expected) => {
    const actual = new URL(candidate, input.baseUrl);
    const wanted = new URL(expected, input.baseUrl);
    const entries = (url) => [...url.searchParams.entries()].sort();
    return actual.origin === wanted.origin && actual.pathname === wanted.pathname &&
      JSON.stringify(entries(actual)) === JSON.stringify(entries(wanted));
  };

  const assertForm = async (calendar) => {
    const result = await page.evaluate((calendar) => {
      const form = document.querySelector(calendar.formSelector);
      const returnLink = document.querySelector(".form-page-intro a[href]");
      return {
        selected: Object.fromEntries(calendar.destination.map(([name]) => [
          name,
          form?.querySelector(`[name="${name}"]`)?.value,
        ])),
        returnHref: returnLink?.href,
      };
    }, calendar);
    if (!calendar.destination.every(([name, value]) => result.selected[name] === value) ||
        !urlMatches(result.returnHref, calendar.origin)) {
      throw new Error(`${calendar.name} action did not open its date form and calendar return link: ${JSON.stringify(result)}.`);
    }
  };

  const activateAndCheckNavigation = async (calendar, selector) => {
    let navigations = 0;
    const observeNavigation = (frame) => {
      if (frame === page.mainFrame()) navigations += 1;
    };
    page.on("framenavigated", observeNavigation);
    await Promise.all([
      page.waitForNavigation({ waitUntil: "domcontentloaded" }),
      page.locator(selector).dispatchEvent("click", { button: 0, detail: 1 }),
    ]);
    await assertForm(calendar);
    page.off("framenavigated", observeNavigation);
    if (navigations !== 1) {
      throw new Error(`${calendar.name} activation produced ${navigations} document navigations.`);
    }
  };

  for (const calendar of input.actionCalendars) {
    const response = await page.goto(input.baseUrl + calendar.url, { waitUntil: "domcontentloaded" });
    if (!response || !response.ok()) {
      throw new Error(`${calendar.name} calendar did not load successfully.`);
    }
    const result = await page.evaluate((calendar) => {
      const actionLink = document.getElementById(calendar.actionId);
      const headerCell = document.querySelector(
        `.calendar-lattice-date-cell[data-calendar-day-action="${calendar.actionId}"]`
      );
      const bodyCell = document.querySelector(
        `.calendar-content-cell[data-calendar-day-action="${calendar.actionId}"]`
      );
      const headerWhitespace = headerCell?.querySelector(".calendar-date-header");
      if (!actionLink || !headerCell || !bodyCell || !headerWhitespace) return { missing: true };

      const destination = new URL(actionLink.href);
      if (destination.origin !== location.origin || destination.pathname !== calendar.destinationPath ||
          !calendar.destination.every(([name, value]) => destination.searchParams.get(name) === value)) {
        return { wrongDestination: actionLink.href };
      }

      let followed = 0;
      const observeFollow = (event) => {
        followed += 1;
        event.preventDefault();
      };
      actionLink.addEventListener("click", observeFollow);
      const click = (target, options = {}) =>
        target.dispatchEvent(new MouseEvent("click", {
          bubbles: true,
          button: 0,
          detail: 1,
          ...options,
        }));

      click(headerWhitespace);
      const headerFollows = followed;
      click(bodyCell);
      const bodyFollows = followed - headerFollows;
      const interactiveChild = document.createElement("button");
      bodyCell.append(interactiveChild);
      click(interactiveChild);
      click(headerWhitespace, { detail: 0 });
      click(bodyCell, { ctrlKey: true });
      const range = document.createRange();
      range.selectNodeContents(headerWhitespace);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      click(headerWhitespace);
      selection.removeAllRanges();
      actionLink.removeEventListener("click", observeFollow);

      return {
        bodyCursor: getComputedStyle(bodyCell).cursor,
        bodyFollows,
        headerCursor: getComputedStyle(headerCell).cursor,
        headerFollows,
        protectedFollows: followed,
      };
    }, calendar);

    if (result.missing || result.wrongDestination || result.headerFollows !== 1 ||
        result.bodyFollows !== 1 || result.protectedFollows !== 2 ||
        result.headerCursor !== "pointer" || result.bodyCursor !== "pointer") {
      throw new Error(`${calendar.name} day action did not preserve one shared, protected destination: ${JSON.stringify(result)}.`);
    }

    const headerSelector = `.calendar-lattice-date-cell[data-calendar-day-action="${calendar.actionId}"] .calendar-date-header`;
    await activateAndCheckNavigation(calendar, headerSelector);
    await page.goto(input.baseUrl + calendar.url, { waitUntil: "domcontentloaded" });
    const bodySelector = `.calendar-content-cell[data-calendar-day-action="${calendar.actionId}"]`;
    await activateAndCheckNavigation(calendar, bodySelector);
  }

  for (const calendar of input.layoutCalendars) {
    await page.goto(input.baseUrl + calendar.url, { waitUntil: "domcontentloaded" });
    const result = await page.evaluate((calendar) => {
      const grid = [...document.querySelectorAll('[role="grid"]')]
        .find((element) => element.getAttribute("aria-label") === calendar.label);
      if (!grid) return { missingGrid: true };
      const failures = [];
      const weekResults = [...grid.querySelectorAll('[role="rowgroup"]')].map((week) => {
        const headers = [...week.querySelectorAll('.calendar-date-header-label[datetime]')];
        const headerDates = headers.map((header) => header.getAttribute("datetime"));
        for (const lane of week.querySelectorAll('.calendar-lattice-lane[role="row"]')) {
          const cells = [...lane.querySelectorAll('.calendar-content-cell[data-calendar-day]')];
          const background = lane.querySelector('.calendar-lattice-background');
          const cellDates = cells.map((cell) => cell.dataset.calendarDay);
          if (cells.length !== 7 || JSON.stringify(cellDates) !== JSON.stringify(headerDates)) {
            failures.push({ lane: lane.getAttribute("aria-label"), headerDates, cellDates });
          }
          if (background?.getAttribute("aria-hidden") !== "true") {
            failures.push({ focusableBackground: lane.getAttribute("aria-label") });
          }
          cells.forEach((cell, index) => {
            const cellRect = cell.getBoundingClientRect();
            const headerRect = headers[index].closest('.calendar-lattice-date-cell').getBoundingClientRect();
            if (Math.abs(cellRect.left - headerRect.left) > 1 ||
                Math.abs(cellRect.right - headerRect.right) > 1 || cellRect.width <= 0 ||
                cell.tabIndex >= 0) {
              failures.push({ misalignedDate: cell.dataset.calendarDay });
            }
          });
        }
        return headerDates;
      });

      const records = calendar.records.map((expected) => {
        const links = [...grid.querySelectorAll('.calendar-lattice-item a[href]')]
          .filter((link) => new URL(link.href).pathname === expected.path);
        const items = [...new Set(links.map((link) => link.closest('.calendar-lattice-item')))];
        const dates = new Set();
        const parts = [];
        for (const item of items) {
          const action = links.find((link) => item.contains(link));
          const lane = item.closest('.calendar-lattice-lane[role="row"]');
          const itemRect = item.getBoundingClientRect();
          const actionRect = action.getBoundingClientRect();
          const hit = document.elementFromPoint(
            actionRect.left + actionRect.width / 2,
            actionRect.top + actionRect.height / 2,
          );
          const covered = [...lane.querySelectorAll('.calendar-content-cell[data-calendar-day]')]
            .filter((cell) => {
              const rect = cell.getBoundingClientRect();
              return Math.min(itemRect.right, rect.right) - Math.max(itemRect.left, rect.left) > 1;
            })
            .map((cell) => cell.dataset.calendarDay);
          covered.forEach((onDate) => dates.add(onDate));
          const identity = item.querySelector(expected.identitySelector)?.cloneNode(true);
          // Coverage is a separate field; wrappers and whitespace do not change the name.
          if (expected.identityCoverage) {
            const coverage = [...identity.querySelectorAll("span")]
              .find((element) => element.textContent.trim() === expected.identityCoverage);
            if (coverage) coverage.remove();
          }
          parts.push({
            lane: lane.getAttribute("aria-label"),
            identity: identity?.textContent.replace(/\s+/g, " ").trim(),
            coverage: expected.coverageSelector
              ? item.querySelector(expected.coverageSelector)?.textContent.trim() : null,
            top: itemRect.top,
            bottom: itemRect.bottom,
            actionUsable: getComputedStyle(action).pointerEvents !== "none" &&
              (hit === action || action.contains(hit)),
          });
        }
        return { dates: [...dates].sort(), parts };
      });
      return { failures, weekResults, records };
    }, calendar);

    if (result.missingGrid || result.failures.length ||
        JSON.stringify(result.weekResults) !== JSON.stringify(calendar.weekDates) ||
        result.records.length !== calendar.records.length) {
      throw new Error(`${calendar.name} calendar geometry is invalid: ${JSON.stringify(result)}.`);
    }
    calendar.records.forEach((expected, index) => {
      const actual = result.records[index];
      if (JSON.stringify(actual.dates) !== JSON.stringify([...expected.dates].sort()) ||
          actual.parts.length !== expected.parts ||
          actual.parts.some((part) => part.lane !== expected.lane ||
            part.identity !== expected.identity || part.coverage !== (expected.coverage || null) ||
            !part.actionUsable)) {
        throw new Error(`${calendar.name} record is under the wrong dates or lane: ${JSON.stringify({ expected, actual })}.`);
      }
    });
    if (calendar.visualOrder) {
      const ordered = calendar.visualOrder.map((recordIndex) => result.records[recordIndex].parts[0]);
      for (let index = 1; index < ordered.length; index += 1) {
        if (ordered[index - 1].bottom > ordered[index].top + 1) {
          throw new Error(`${calendar.name} records overlap or violate name order: ${JSON.stringify(ordered)}.`);
        }
      }
    }
  }

  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""


class CalendarDayActionsBrowserTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        cls.node, cls.browser_executable = require_browser_tools()
        super().setUpClass()

    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(username="calendar-browser-scheduler")
        administrators, _ = Group.objects.get_or_create(name="Scheduling Administrators")
        self.scheduler.groups.add(administrators)
        self.client.force_login(self.scheduler)
        self.physician = Physician.objects.create(display_name="Calendar browser physician")
        self.single_vacation = Vacation.objects.create(
            physician=self.physician, start_date=date(2026, 8, 14), end_date=date(2026, 8, 14)
        )
        self.cross_month_vacation = Vacation.objects.create(
            physician=self.physician, start_date=date(2026, 7, 31), end_date=date(2026, 8, 2)
        )
        self.overlapping_vacations = []
        for name, coverage in (
            ("Alpha Browser", Vacation.PeriodOfDay.AM),
            ("Morgan Browser", Vacation.PeriodOfDay.PM),
            ("Zulu Browser", Vacation.PeriodOfDay.WHOLE_DAY),
        ):
            physician = Physician.objects.create(display_name=name)
            self.overlapping_vacations.append(
                Vacation.objects.create(
                    physician=physician,
                    start_date=date(2026, 8, 12),
                    end_date=date(2026, 8, 12),
                    period_of_day=coverage,
                )
            )
        self.interview = create_interview_shift(
            academic_year=2026, date=date(2026, 8, 14), scheduler=self.scheduler
        )
        self.community_rules = []
        for name, period in (("Alpha Community", "AM"), ("Beta Community", "PM")):
            physician = Physician.objects.create(display_name=name)
            self.community_rules.append(
                CommunityPreceptorRule.objects.create(
                    physician=physician, weekday=2, ordinals=[2], periods=[period]
                )
            )
        rounder = Physician.objects.create(display_name="Monday Rounder")
        EffectiveRole.objects.create(
            physician=rounder,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2026, 1, 1),
        )
        self.normal_rounder = NormalRounder.objects.create(
            physician=rounder, week=date(2026, 8, 10)
        )

        self.casual_rules = []
        for name in ("Alpha Casual", "Beta Casual"):
            physician = Physician.objects.create(display_name=name)
            EffectiveRole.objects.create(
                physician=physician,
                role=EffectiveRole.Role.CASUAL_ROUNDER,
                effective_start=date(2026, 1, 1),
            )
            self.casual_rules.append(
                CasualRounderRule.objects.create(physician=physician, weekday=2, ordinals=[2])
            )

    def test_calendars_place_records_and_day_actions_open_the_owned_date_forms(self):
        completed = subprocess.run(
            [self.node, "-e", _BROWSER_RUNNER],
            input=json.dumps(
                {
                    "baseUrl": self.live_server_url,
                    "browserExecutable": self.browser_executable,
                    "stylesheet": str(
                        Path(__file__).resolve().parents[1] / "static/schedules/application.css"
                    ),
                    "script": str(
                        Path(__file__).resolve().parents[1]
                        / "static/schedules/calendar-day-actions.js"
                    ),
                    "sessionCookie": {
                        "name": settings.SESSION_COOKIE_NAME,
                        "value": self.client.cookies[settings.SESSION_COOKIE_NAME].value,
                    },
                    "actionCalendars": [
                        {
                            "name": "Vacation",
                            "url": f"{reverse('vacation-workspace')}?month=2026-08",
                            "actionId": "vacation-add-2026-08-12",
                            "destinationPath": reverse("vacation-create"),
                            "formSelector": "form.vacation-form",
                            "origin": "/vacations/?month=2026-08",
                            "destination": [
                                ["start_date", "2026-08-12"],
                                ["end_date", "2026-08-12"],
                            ],
                        },
                        {
                            "name": "Interview",
                            "url": f"{reverse('interview-workspace')}?academic_year=2026&month=2026-08",
                            "actionId": "interview-add-2026-08-12",
                            "destinationPath": reverse("interview-create"),
                            "formSelector": "form.interview-form",
                            "origin": "/interviews/?academic_year=2026&month=2026-08",
                            "destination": [["date", "2026-08-12"]],
                        },
                    ],
                    "layoutCalendars": self._layout_calendars(),
                }
            ),
            capture_output=True,
            text=True,
        )

        if completed.returncode:
            self.fail(completed.stderr)

    def _layout_calendars(self):
        week_dates = [
            [(date(2026, 7, 26) + timedelta(days=week * 7 + day)).isoformat() for day in range(7)]
            for week in range(6)
        ]

        def record(
            path,
            dates,
            lane,
            identity,
            *,
            coverage=None,
            parts=1,
            identity_selector,
            identity_coverage=None,
        ):
            return {
                "path": path,
                "dates": [on_date.isoformat() for on_date in dates],
                "lane": lane,
                "identity": identity,
                "identitySelector": identity_selector,
                "identityCoverage": identity_coverage,
                "coverage": coverage,
                "coverageSelector": ".vacation-segment-coverage" if coverage else None,
                "parts": parts,
            }

        vacation_records = [
            record(
                reverse("vacation-correct", args=(self.cross_month_vacation.pk,)),
                [date(2026, 7, 31), date(2026, 8, 1), date(2026, 8, 2)],
                "Vacation entries",
                "Calendar browser physician",
                parts=2,
                identity_selector=".vacation-segment-name",
            )
        ]
        for vacation, name, coverage in zip(
            self.overlapping_vacations,
            ("Alpha Browser", "Morgan Browser", "Zulu Browser"),
            ("(AM)", "(PM)", None),
            strict=True,
        ):
            vacation_records.append(
                record(
                    reverse("vacation-correct", args=(vacation.pk,)),
                    [date(2026, 8, 12)],
                    "Vacation entries",
                    name,
                    coverage=coverage,
                    identity_selector=".vacation-segment-name",
                )
            )

        return [
            {
                "name": "Vacation",
                "url": f"{reverse('vacation-workspace')}?month=2026-08",
                "label": "August 2026 vacation calendar",
                "weekDates": week_dates,
                "records": vacation_records,
                "visualOrder": [1, 2, 3],
            },
            {
                "name": "Interview",
                "url": f"{reverse('interview-workspace')}?academic_year=2026&month=2026-08",
                "label": "August 2026 Interviews calendar",
                "weekDates": week_dates,
                "records": [
                    record(
                        reverse("interview-correct", args=(self.interview.pk,)),
                        [date(2026, 8, 14)],
                        "Interview AM",
                        "Interview AM",
                        identity_selector="strong",
                    )
                ],
            },
            {
                "name": "Community Precepting",
                "url": f"{reverse('community-preceptor-workspace')}?month=2026-08",
                "label": "August 2026 Community Precepting calendar",
                "weekDates": week_dates,
                "records": [
                    record(
                        reverse("community-preceptor-rule-correct", args=(rule.pk,)),
                        [date(2026, 8, 12)],
                        period,
                        name,
                        identity_selector="a",
                        identity_coverage=period,
                    )
                    for rule, name, period in zip(
                        self.community_rules,
                        ("Alpha Community", "Beta Community"),
                        ("AM", "PM"),
                        strict=True,
                    )
                ],
            },
            {
                "name": "Rounding",
                "url": f"{reverse('rounding-workspace')}?month=2026-08",
                "label": "August 2026 Rounding calendar",
                "weekDates": week_dates,
                "records": [
                    record(
                        reverse("normal-rounder-correct", args=(self.normal_rounder.pk,)),
                        [date(2026, 8, 10) + timedelta(days=offset) for offset in range(7)],
                        "Rounder",
                        "Monday Rounder",
                        parts=2,
                        identity_selector="strong",
                    ),
                    *[
                        record(
                            reverse("casual-rounder-rule-correct", args=(rule.pk,)),
                            [date(2026, 8, 12)],
                            "Casual",
                            name,
                            identity_selector=f'a[href*="/rounding/casual/{rule.pk}/correct/"]',
                        )
                        for rule, name in zip(
                            self.casual_rules, ("Alpha Casual", "Beta Casual"), strict=True
                        )
                    ],
                ],
            },
        ]
