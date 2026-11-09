import json
import shutil
import subprocess
from datetime import date
from pathlib import Path
from unittest import SkipTest

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import LiveServerTestCase
from django.urls import reverse

from schedules.models import AcademicEventDefinition, EffectiveRole, InterviewShift, Physician
from schedules.services.preceptors import open_monthly_draft

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
    if (route.request().url().endsWith("/static/schedules/role-attributes.js")) {
      return route.fulfill({ body: readFileSync(path.join(path.dirname(input.stylesheet), "role-attributes.js")), contentType: "text/javascript" });
    }
    return route.request().resourceType() === "document" ? route.continue() : route.abort();
  });

  const forms = input.forms;

  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 900 });
    for (const [label, url] of forms) {
      const response = await page.goto(input.baseUrl + url, { waitUntil: "domcontentloaded" });
      if (!response || !response.ok()) {
        throw new Error(`${label} at ${width}px did not load successfully (${response ? response.status() : "no response"}).`);
      }
      await page.addStyleTag({ content: readFileSync(input.stylesheet, "utf8") });
      const role = page.locator('select[name="role"]');
      if (await role.count()) {
        for (const [value, applicable] of [["faculty", "faculty-leadership"], ["community_preceptor", "faculty-equivalent"], ["casual_rounder", null]]) {
          await role.selectOption(value);
          for (const attribute of ["faculty-equivalent", "faculty-leadership"]) {
            const group = page.locator(`[data-role-attribute="${attribute}"]`);
            if (await group.isVisible() !== (attribute === applicable)) {
              throw new Error(`${label}: ${attribute} visibility does not follow ${value} at ${width}px.`);
            }
            if (attribute === applicable) {
              const name = attribute.replaceAll("-", "_");
              const required = group.locator(`[name="${name}"]`);
              if (await required.count() !== 1) throw new Error(`${label}: missing ${name} control.`);
              for (const control of await required.all()) {
                if (!await control.isVisible() || !await control.isEnabled()) {
                  throw new Error(`${label}: applicable role control is unusable.`);
                }
                const bound = await control.evaluate((element) => [...element.labels].some((label) => label.control === element));
                if (!bound) throw new Error(`${label}: applicable role control has no associated label.`);
              }
            }
          }
        }
        await role.selectOption("faculty");
      }
      const geometry = await page.locator("form.form-layout, form.assignment-form, .surface-frame--removal form").evaluateAll((forms) => forms.flatMap((form) => {
        const controls = [...form.querySelectorAll("input:not([type=hidden]), select, textarea, button")]
          .filter((control) => control.getClientRects().length && getComputedStyle(control).visibility !== "hidden");
        const bounds = controls.map((control) => control.getBoundingClientRect());
        return controls.flatMap((control, index) => {
          const box = bounds[index];
          const failures = [];
          if (box.width <= 0 || box.left < 0 || box.right > innerWidth + 1) failures.push(`${control.name}: unbounded width`);
          if (control.tagName !== "BUTTON" && !control.labels?.length) failures.push(`${control.name}: detached label`);
          for (const other of bounds.slice(index + 1)) {
            if (Math.min(box.right, other.right) - Math.max(box.left, other.left) > 1 &&
                Math.min(box.bottom, other.bottom) - Math.max(box.top, other.top) > 1) failures.push(`${control.name}: overlapping controls`);
          }
          return failures;
        });
      }));
      if (geometry.length) throw new Error(`${label} at ${width}px: ${geometry.join(", ")}`);
      const result = await page.evaluate(() => {
        const intro = document.querySelector("h1")?.closest("header");
        const form = document.querySelector(".form-layout, .assignment-form");
        const returnLink = intro?.querySelector("a[href]");
        if (!intro || !form || !returnLink) return { missing: true };
        returnLink.focus();
        const introBounds = intro.getBoundingClientRect();
        const formBounds = form.getBoundingClientRect();
        return {
          h1Count: document.querySelectorAll("h1").length,
          introH1Count: intro.querySelectorAll("h1").length,
          focusableReturnLink: document.activeElement === returnLink,
          separated: introBounds.bottom <= formBounds.top,
          aligned: Math.abs(introBounds.left - formBounds.left) < .1 && Math.abs(introBounds.width - formBounds.width) < .1,
          introOverflows: intro.scrollWidth > intro.clientWidth || introBounds.left < 0 || introBounds.right > innerWidth,
        };
      });
      if (result.missing) throw new Error(`${label} is missing its contained form intro.`);
      if (result.h1Count !== 1 || result.introH1Count !== 1) {
        throw new Error(`${label} does not retain one contained page title.`);
      }
      if (!result.focusableReturnLink) throw new Error(`${label} return navigation is not keyboard focusable.`);
      if (!result.separated || !result.aligned || result.introOverflows) {
        throw new Error(`${label} intro does not align with its form at ${width}px.`);
      }
    }
  }

  await browser.close();
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""

_INTERVIEW_BROWSER_RUNNER = r"""
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

  for (const [width, date] of [[1280, "2026-10-01"], [375, "2026-10-02"]]) {
    await page.setViewportSize({ width, height: 900 });
    const response = await page.goto(input.baseUrl + input.formUrl + "&date=" + date);
    if (!response?.ok()) throw new Error(`Interview at ${width}px failed to load.`);
    const selector = page.getByLabel("Faculty assignment", { exact: true });
    const option = selector.locator(`optgroup[label="Override-required Faculty"] option[value="${input.physician}"]`);
    const text = await option.textContent();
    if (!/\bAvailability physician\b/.test(text) ||
        !/\bunavailable\b/i.test(text) ||
        !/\bhard[\s-]+finding\b/i.test(text)) {
      throw new Error("The unavailable Faculty option lost its candidate information.");
    }
    // Ported from the reviewed audit: measure the rendered control, not CSS declarations.
    // closest() allows harmless wrappers between the field and its native control.
    const geometry = await selector.evaluate((control) => {
      const box = control.getBoundingClientRect();
      const field = control.closest(".form-field-group").getBoundingClientRect();
      return {
        positive: box.width > 0,
        inField: box.left >= field.left - 1 && box.right <= field.right + 1,
        inViewport: box.left >= 0 && box.right <= innerWidth + 1,
        labeled: [...control.labels].some((label) => label.control === control),
      };
    });
    if (!geometry.positive || !geometry.inField || !geometry.inViewport || !geometry.labeled) {
      throw new Error(`Interview physician geometry at ${width}px: ${JSON.stringify(geometry)}`);
    }
    await selector.selectOption(input.physician);
    await page.getByLabel("Optional note", { exact: true }).fill(`Selected at ${width}px`);
    await Promise.all([
      page.waitForURL((url) => url.pathname === input.successPath),
      page.locator("form.interview-form").getByRole("button", { name: "Schedule Interview", exact: true }).click(),
    ]);
  }
  await browser.close();
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""

_PAGE_INTRO_BROWSER_RUNNER = r"""
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
  page.route("**/*", (route) => {
    if (route.request().url().endsWith("/static/schedules/application.css")) {
      return route.fulfill({ body: readFileSync(input.stylesheet), contentType: "text/css" });
    }
    return route.request().resourceType() === "document" ? route.continue() : route.abort();
  });

  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 900 });
    for (const [label, url, requiresReturnLink, hasWideFollowingSurface, requiresFullWidth] of input.pages) {
      const response = await page.goto(input.baseUrl + url, { waitUntil: "domcontentloaded" });
      if (!response || !response.ok()) {
        throw new Error(`${label} at ${width}px did not load successfully (${response ? response.status() : "no response"}).`);
      }
      await page.addStyleTag({ content: readFileSync(input.stylesheet, "utf8") });
      const result = await page.evaluate((requiresReturnLink) => {
        const intro = document.querySelector("h1")?.closest("header");
        const followingSurface = intro?.nextElementSibling;
        const returnLink = intro?.querySelector("a[href]");
        if (!intro || !followingSurface || (requiresReturnLink && !returnLink)) return { missing: true };
        returnLink?.focus();
        const introBounds = intro.getBoundingClientRect();
        const followingSurfaceBounds = followingSurface.getBoundingClientRect();
        return {
          h1Count: document.querySelectorAll("h1").length,
          introH1Count: intro.querySelectorAll("h1").length,
          focusableReturnLink: !requiresReturnLink || document.activeElement === returnLink,
          separated: introBounds.bottom <= followingSurfaceBounds.top,
          aligned: Math.abs(introBounds.left - followingSurfaceBounds.left) < .1,
          fillsFollowingSurface: Math.abs(introBounds.width - followingSurfaceBounds.width) < .1,
          compact: introBounds.width <= followingSurfaceBounds.width + .1,
          compactAheadOfWorkspace: introBounds.width < followingSurfaceBounds.width,
          introOverflows: intro.scrollWidth > intro.clientWidth || introBounds.left < 0 || introBounds.right > innerWidth,
        };
      }, requiresReturnLink);
      if (result.missing) throw new Error(`${label} is missing its contained page intro.`);
      if (result.h1Count !== 1 || result.introH1Count !== 1) {
        throw new Error(`${label} does not retain one contained page title.`);
      }
      if (!result.focusableReturnLink) throw new Error(`${label} return navigation is not keyboard focusable.`);
      if (!result.separated || !result.aligned || !result.compact || result.introOverflows) {
        throw new Error(`${label} intro does not align with its page content at ${width}px.`);
      }
      if (requiresFullWidth && !result.fillsFollowingSurface) {
        throw new Error(`${label} intro does not fill its history content surface at ${width}px.`);
      }
      if (hasWideFollowingSurface && width > 375 && !result.compactAheadOfWorkspace) {
        throw new Error(`${label} intro is not distinct from its following workspace surface.`);
      }
    }
  }

  await browser.close();
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""


class FormPageIntroBrowserTests(LiveServerTestCase):
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

    def test_interview_form_geometry_at_desktop_and_phone_widths(self):
        self.client.force_login(self.user)
        physician = Physician.objects.create(display_name="Availability physician")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2026-01-01",
        )
        completed = subprocess.run(
            [self.node, "-e", _INTERVIEW_BROWSER_RUNNER],
            input=json.dumps(
                {
                    "baseUrl": self.live_server_url,
                    "browserExecutable": self.browser_executable,
                    "stylesheet": str(
                        Path(__file__).resolve().parents[1] / "static/schedules/application.css"
                    ),
                    "formUrl": reverse("interview-create") + "?academic_year=2026&view=list",
                    "successPath": reverse("interview-list"),
                    "physician": str(physician.pk),
                    "sessionCookie": {
                        "name": settings.SESSION_COOKIE_NAME,
                        "value": self.client.cookies[settings.SESSION_COOKIE_NAME].value,
                    },
                }
            ),
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for day, width in [(1, 1280), (2, 375)]:
            shift = InterviewShift.objects.get(date=date(2026, 10, day))
            self.assertEqual(shift.physician, physician)
            self.assertEqual(shift.note, f"Selected at {width}px")

    def test_representative_data_entry_forms_share_the_contained_intro_at_all_widths(self):
        self.client.force_login(self.user)
        physician = Physician.objects.create(display_name="Availability physician")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start="2026-01-01",
        )
        session = open_monthly_draft(date(2026, 8, 1)).sessions.first()
        definition = AcademicEventDefinition.objects.create(
            base_name="Form geometry", start_academic_year=2026
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
                    "forms": [
                        ["Rounding", "/rounding/normal/add/?week=2026-08-03"],
                        ["Vacation", "/vacations/add/"],
                        [
                            "Assignment",
                            reverse("session-assignment-edit", args=(session.pk, "primary-1")),
                        ],
                        [
                            "Definition edit",
                            reverse("academic-event-definition-correct", args=(definition.pk,)),
                        ],
                        ["Event definition", "/event-definitions/add/"],
                        ["Community precepting", "/community-preceptors/add/"],
                        ["Interview", "/interviews/add/?academic_year=2026"],
                        ["Physician", "/physicians/add/"],
                        ["Physician role", reverse("physician-role-add", args=(physician.pk,))],
                        [
                            "Academic Event",
                            reverse("academic-event-create")
                            + f"?academic_year=2026&definition={definition.pk}",
                        ],
                        [
                            "Faculty availability",
                            reverse("physician-faculty-availability-add", args=(physician.pk,)),
                        ],
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

    def test_workspace_history_and_read_only_pages_share_the_contained_intro_at_all_widths(self):
        self.client.force_login(self.user)
        physician = Physician.objects.create(display_name="History physician")
        session = open_monthly_draft(date(2026, 8, 1)).sessions.first()
        django_administrator = get_user_model().objects.create_user(
            username="browser-django-administrator",
            password="browser-password",
            is_staff=True,
        )
        completed = subprocess.run(
            [self.node, "-e", _PAGE_INTRO_BROWSER_RUNNER],
            input=json.dumps(
                {
                    "baseUrl": self.live_server_url,
                    "browserExecutable": self.browser_executable,
                    "stylesheet": str(
                        Path(__file__).resolve().parents[1] / "static/schedules/application.css"
                    ),
                    "pages": [
                        [
                            "Account administration",
                            reverse("account-workspace"),
                            False,
                            True,
                            False,
                        ],
                        ["Account history", reverse("account-history"), True, False, True],
                        [
                            "Django administrator account",
                            reverse("account-edit", args=(django_administrator.pk,)),
                            True,
                            False,
                            False,
                        ],
                        [
                            "Physician history",
                            reverse("physician-history", args=(physician.pk,)),
                            True,
                            False,
                            True,
                        ],
                        ["Physicians history", reverse("physicians-history"), True, False, True],
                        [
                            "Schedule option history",
                            reverse("custom-availability-category-history"),
                            True,
                            False,
                            True,
                        ],
                        [
                            "Stipend rate history",
                            reverse("stipend-rate-history"),
                            True,
                            False,
                            True,
                        ],
                        ["Lead-time history", reverse("lead-time-history"), True, False, True],
                        ["Vacation history", reverse("vacation-history"), True, False, True],
                        [
                            "Assignment history",
                            reverse("session-assignment-history", args=(session.pk,)),
                            True,
                            False,
                            True,
                        ],
                        [
                            "Academic Event history",
                            reverse("academic-event-history"),
                            True,
                            False,
                            True,
                        ],
                        [
                            "Community Preceptor history",
                            reverse("community-preceptor-history"),
                            True,
                            False,
                            True,
                        ],
                        ["Interview history", reverse("interview-history"), True, False, True],
                        [
                            "Holiday history",
                            f"{reverse('holiday-history')}?calendar_year=2026",
                            True,
                            False,
                            True,
                        ],
                        ["Rounding history", reverse("rounding-history"), True, False, True],
                        [
                            "Late Doc history",
                            reverse("late-doc-history", args=("2026-08-03",)),
                            True,
                            False,
                            True,
                        ],
                        [
                            "Event definition history",
                            reverse("academic-event-definition-history"),
                            True,
                            False,
                            True,
                        ],
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
