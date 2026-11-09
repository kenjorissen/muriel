import json
import subprocess
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import LiveServerTestCase
from django.urls import reverse

from schedules.models import Physician
from schedules.tests.browser_tools import require_browser_tools

_BROWSER_RUNNER = r"""
const { readFileSync } = require("fs");

const input = JSON.parse(readFileSync(0, "utf8"));
const { chromium } = require(process.env.PRECEPTOR_PLAYWRIGHT_MODULE);

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: input.browserExecutable });
  try {
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
    if (route.request().url().endsWith("/static/schedules/capacity-override-control.js")) {
      return route.fulfill({ body: readFileSync(input.capacityOverrideScript), contentType: "text/javascript" });
    }
    return route.request().resourceType() === "document" ? route.continue() : route.abort();
  });

  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 900 });
    const response = await page.goto(input.baseUrl + input.url, { waitUntil: "domcontentloaded" });
    if (!response || !response.ok()) throw new Error(`Detail at ${width}px did not load.`);
    const result = await page.evaluate(() => {
      const intro = document.querySelector("h1")?.closest("header");
      const forms = Object.fromEntries(["account", "capacity_override", "identity"].map((action) =>
        [action, document.querySelector(`form input[name="action"][value="${action}"]`)?.form]));
      const noteFields = Object.entries(forms).map(([action, form]) => {
        const control = form?.querySelector('textarea[name="reason"]');
        const label = control?.labels[0];
        let group = control?.parentElement;
        while (group && group !== form && !(group.contains(label) && group.querySelector("small"))) group = group.parentElement;
        const help = group && group !== form ? group.querySelector("small") : null;
        return { action, form, control, label, help, button: form?.querySelector('button[type="submit"]') };
      });
      const missing = noteFields.flatMap(({ action, form, control, label, help, button }) =>
        Object.entries({ form, control, "associated label": label, help, "submit button": button })
          .filter(([, element]) => !element)
          .map(([name]) => `${action}: missing ${name}`));
      if (!intro) missing.push("missing page introduction");
      if (missing.length) return { missing };
      const capacityForm = forms.capacity_override;
      const introBounds = intro.getBoundingClientRect();
      const capacity = capacityForm.querySelector("[data-capacity-override-control]");
      const useOverride = capacity?.querySelector('input[name="use_override"]');
      const capacityOverride = capacity?.querySelector('input[name="capacity_override"]');
      const capacityLabel = capacity?.querySelector(".faculty-capacity-override-control-label");
      const capacityBounds = capacity?.getBoundingClientRect();
      const labelBounds = capacityLabel?.getBoundingClientRect();
      const checkboxBounds = useOverride?.getBoundingClientRect();
      const inputBounds = capacityOverride?.getBoundingClientRect();
      return {
        h1Count: document.querySelectorAll("h1").length,
        introTitleCount: intro.querySelectorAll("h1").length,
        introOverflow: intro.scrollWidth > intro.clientWidth,
        fields: noteFields.map(({ action, form, label, help, control, button }) => {
          const labelBounds = label.getBoundingClientRect();
          const helpBounds = help.getBoundingClientRect();
          const controlBounds = control.getBoundingClientRect();
          const buttonBounds = button.getBoundingClientRect();
          const formBounds = form.getBoundingClientRect();
          const style = getComputedStyle(control);
          return {
            action,
            compact: controlBounds.height > 0 && controlBounds.height < 50,
            resizable: style.resize === "vertical" || style.resize === "both",
            labelsBound: label.control === control && form.contains(label),
            doesNotOverlap: controlBounds.top >= Math.max(labelBounds.bottom, helpBounds.bottom) && helpBounds.right <= formBounds.right && buttonBounds.top >= controlBounds.bottom,
            overflows: controlBounds.left < formBounds.left || controlBounds.right > Math.min(formBounds.right, innerWidth),
          };
        }),
        capacity: !capacity || !useOverride || !capacityOverride || !capacityLabel ? null : {
          uncheckedAndDisabled: !useOverride.checked && capacityOverride.disabled,
          excludedFromSubmission: !new FormData(capacityForm).has("capacity_override"),
          labelsBound: useOverride.labels.length === 1 && capacityOverride.labels.length === 1,
          singleLine: Math.max(labelBounds.top, checkboxBounds.top, inputBounds.top) - Math.min(labelBounds.top, checkboxBounds.top, inputBounds.top) < 8,
          compact: inputBounds.width <= 100,
          overflows: capacity.scrollWidth > capacity.clientWidth || capacityBounds.right > capacity.parentElement.getBoundingClientRect().right,
        },
      };
    });
    if (result.missing) throw new Error(`Detail at ${width}px: ${result.missing.join("; ")}.`);
    if (result.h1Count !== 1 || result.introTitleCount !== 1 || result.introOverflow) {
      throw new Error(`Detail at ${width}px does not retain one compact contained title.`);
    }
    if (result.fields.some((field) => !field.labelsBound || !field.compact || !field.resizable || !field.doesNotOverlap || field.overflows)) {
      throw new Error(`Detail note controls do not remain usable at ${width}px: ${JSON.stringify(result)}.`);
    }
    if (!result.capacity || !result.capacity.uncheckedAndDisabled || !result.capacity.excludedFromSubmission || !result.capacity.labelsBound || !result.capacity.singleLine || !result.capacity.compact || result.capacity.overflows) {
      throw new Error(`Capacity override control is not compact and usable at ${width}px: ${JSON.stringify(result.capacity)}.`);
    }

    for (const action of ["account", "capacity_override", "identity"]) {
      const form = page.locator("form").filter({ has: page.locator(`input[name="action"][value="${action}"]`) });
      const note = form.locator('textarea[name="reason"]');
      const label = form.locator("label").filter({ hasText: "History note" });
      await label.click();
      if (!await note.evaluate((control) => document.activeElement === control)) {
        throw new Error(`${action} label does not focus its note at ${width}px.`);
      }
      const helpersValid = await note.evaluate((control) =>
        (control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean)
          .every((id) => {
            const helpers = document.querySelectorAll(`[id="${CSS.escape(id)}"]`);
            return helpers.length === 1 && control.form.contains(helpers[0]);
          }));
      if (!helpersValid) throw new Error(`${action} note has a broken helper reference.`);
      await note.evaluate((control) => { control.style.height = "120px"; });
      const resized = await note.boundingBox();
      const button = await form.locator('button[type="submit"]').boundingBox();
      if (resized.height < 120 || button.y < resized.y + resized.height) {
        throw new Error(`${action} note overlaps its action after resizing at ${width}px.`);
      }
    }

    await page.locator('input[name="use_override"]').check();
    const enabledAfterCheck = await page.locator('input[name="capacity_override"]').isEnabled();
    const restoredDisabled = await page.evaluate(() => {
      const useOverride = document.querySelector('input[name="use_override"]');
      const capacityOverride = document.querySelector('input[name="capacity_override"]');
      useOverride.checked = false;
      window.dispatchEvent(new Event("pageshow"));
      return capacityOverride.disabled;
    });
    const restoredEnabled = await page.evaluate(() => {
      const useOverride = document.querySelector('input[name="use_override"]');
      const capacityOverride = document.querySelector('input[name="capacity_override"]');
      useOverride.checked = true;
      window.dispatchEvent(new Event("pageshow"));
      return !capacityOverride.disabled;
    });
    if (!enabledAfterCheck || !restoredDisabled || !restoredEnabled) {
      throw new Error(`Capacity override state does not follow the checkbox at ${width}px.`);
    }

    const enabledResponse = await page.goto(input.enabledUrl, { waitUntil: "domcontentloaded" });
    if (!enabledResponse || !enabledResponse.ok()) throw new Error(`Enabled detail at ${width}px did not load.`);
    const initiallyEnabled = await page.evaluate(() => {
      const useOverride = document.querySelector('input[name="use_override"]');
      const capacityOverride = document.querySelector('input[name="capacity_override"]');
      return useOverride.checked && !capacityOverride.disabled;
    });
    if (!initiallyEnabled) throw new Error(`Enabled capacity override is not usable at ${width}px.`);
  }

  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""


class PhysicianDetailFormsBrowserTests(LiveServerTestCase):
    serve_static = False

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
        self.physician = Physician.objects.create(display_name="Compact form physician")
        self.overridden_physician = Physician.objects.create(
            display_name="Enabled compact form physician",
            faculty_precepting_capacity_override=Decimal("4.5"),
        )

    def test_compact_note_controls_remain_usable_at_narrow_widths(self):
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
                    "capacityOverrideScript": str(
                        Path(__file__).resolve().parents[1]
                        / "static/schedules/capacity-override-control.js"
                    ),
                    "url": reverse("physician-detail", args=(self.physician.pk,)),
                    "enabledUrl": self.live_server_url
                    + reverse("physician-detail", args=(self.overridden_physician.pk,)),
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
