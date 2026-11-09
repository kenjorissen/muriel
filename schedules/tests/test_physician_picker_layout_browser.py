import json
import subprocess
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import LiveServerTestCase
from django.urls import reverse

from schedules.models import AcademicEventDefinition, Physician
from schedules.tests.browser_tools import require_browser_tools

_BROWSER_RUNNER = r"""
const { readFileSync } = require("fs");

const input = JSON.parse(readFileSync(0, "utf8"));
const { chromium } = require(process.env.PRECEPTOR_PLAYWRIGHT_MODULE);

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: input.browserExecutable });
  try {
  const context = await browser.newContext({ viewport: { width: 1100, height: 800 } });
  await context.addCookies([{
    name: input.sessionCookie.name,
    value: input.sessionCookie.value,
    url: input.baseUrl,
  }]);
  const page = await context.newPage();
  await page.route("**/*", (route) => {
    const script = Object.entries(input.staticFiles).find(([suffix]) => route.request().url().endsWith(suffix));
    if (script) return route.fulfill({ body: readFileSync(script[1]), contentType: script[0].endsWith(".css") ? "text/css" : "text/javascript" });
    return route.request().resourceType() === "document" ? route.continue() : route.abort();
  });
  const response = await page.goto(input.baseUrl + input.url, { waitUntil: "load" });
  if (!response || !response.ok()) throw new Error("Physician picker did not load.");

  const picker = page.locator("[data-physician-picker]");
  if (await picker.getAttribute("data-physician-picker-ready") === null) {
    throw new Error("Physician picker did not enhance.");
  }
  const desktopLayout = await picker.evaluate((element) => {
    const availableLabel = [...element.querySelectorAll("legend")].find(
      (label) => label.textContent.trim() === "Physicians",
    );
    const selectedLabel = [...element.querySelectorAll("h1, h2, h3, h4, h5, h6")].find(
      (label) => label.textContent.trim() === "Selected physicians",
    );
    const available = element.querySelector("[data-physician-picker-choices]");
    const selected = element.querySelector("[data-physician-picker-selected]");
    const instruction = element.querySelector("[data-physician-picker-selected-instruction]");
    const labelEdgesAlign = availableLabel && selectedLabel &&
      Math.abs(availableLabel.getBoundingClientRect().top - selectedLabel.getBoundingClientRect().top) <= 1;
    const boxEdgesAlign = available && selected &&
      Math.abs(available.getBoundingClientRect().top - selected.getBoundingClientRect().top) <= 1;
    return Boolean(
      labelEdgesAlign && boxEdgesAlign && selected && selectedLabel && instruction &&
      !available.contains(availableLabel) && !selected.contains(selectedLabel) &&
      !selected.contains(instruction),
    );
  });
  if (!desktopLayout) {
    throw new Error("Picker labels and list boxes must align while selected instructions stay outside the box.");
  }

  await page.setViewportSize({ width: 600, height: 800 });
  const narrowLayout = await picker.evaluate((element) => {
    const filter = element.querySelector("[data-physician-picker-filter-area]");
    const available = element.querySelector("[data-physician-picker-choices]");
    const selected = element.querySelector("[data-physician-picker-selected]");
    return Boolean(filter && available && selected &&
      filter.getBoundingClientRect().bottom <= available.getBoundingClientRect().top &&
      available.getBoundingClientRect().bottom <= selected.getBoundingClientRect().top);
  });
  if (!narrowLayout) throw new Error("Picker must stack filter, available choices, then selected choices.");

  await page.setViewportSize({ width: 1100, height: 800 });
  const avery = picker.getByRole("checkbox", { name: "Avery Participant", exact: true });
  const blair = picker.getByRole("checkbox", { name: "Blair Participant", exact: true });
  await avery.focus();
  await page.keyboard.press("Space");
  await blair.check();
  const removeAvery = picker.getByRole("button", { name: "Remove Avery Participant", exact: true });
  const removeBlair = picker.getByRole("button", { name: "Remove Blair Participant", exact: true });
  if (!await removeAvery.isVisible() || !await removeBlair.isVisible()) {
    throw new Error("Selected physicians must each have their own named removal action.");
  }
  await removeBlair.focus();
  await page.keyboard.press("Enter");
  if (!await avery.isChecked() || await blair.isChecked() ||
      !await removeAvery.isVisible() || await removeBlair.count() !== 0) {
    throw new Error("Keyboard removal must clear only Blair's checkbox and chip.");
  }

  for (const width of [1100, 600]) {
    await page.setViewportSize({ width, height: 800 });
    const choices = picker.locator("[data-physician-picker-choices]");
    const bounded = await choices.evaluate((element) => {
      element.scrollTop = element.scrollHeight;
      return element.clientHeight > 0 &&
        element.clientHeight < window.innerHeight &&
        element.scrollHeight > element.clientHeight && element.scrollTop > 0;
    });
    if (!bounded) throw new Error("Long physician choices must scroll inside a bounded list.");
    await picker.getByRole("checkbox", { name: "Roster Physician 39", exact: true }).check();
    if (!await picker.getByRole("button", { name: "Remove Roster Physician 39", exact: true }).isVisible()) {
      throw new Error("A physician reached by scrolling must appear in the selected list.");
    }
    await picker.getByRole("button", { name: "Remove Roster Physician 39", exact: true }).click();
    if (await picker.evaluate((element) => element.getBoundingClientRect().right > window.innerWidth)) {
      throw new Error("Picker overflows the narrow viewport.");
    }
    if (width === 600) {
      const longChoice = picker.getByRole("checkbox", {
        name: "An exceptionally long physician name that must wrap inside the picker without pushing any control beyond its narrow viewport",
        exact: true,
      });
      await longChoice.check();
      await longChoice.focus();
      const longToken = picker.getByRole("button", {
        name: "Remove An exceptionally long physician name that must wrap inside the picker without pushing any control beyond its narrow viewport",
        exact: true,
      });
      const longChoiceFits = await longChoice.evaluate((input) => {
        const label = input.labels?.[0];
        const bounds = label?.getBoundingClientRect();
        return document.activeElement === input && bounds && bounds.left >= 0 &&
          bounds.right <= window.innerWidth && label.scrollWidth <= label.clientWidth;
      });
      const longTokenFits = await longToken.evaluate((button) => {
        const bounds = button.getBoundingClientRect();
        return bounds.left >= 0 && bounds.right <= window.innerWidth &&
          button.scrollWidth <= button.clientWidth;
      });
      if (!longChoiceFits || !longTokenFits) {
        throw new Error("Long names and focused controls must remain within the narrow picker.");
      }
      await longToken.click();
    }
  }

  await page.setViewportSize({ width: 1100, height: 800 });
  await page.locator('input[name="start_date"]').fill("2026-08-10");
  await page.locator('input[name="end_date"]').fill("2026-08-10");
  await avery.check();
  await page.evaluate(() => {
    const staleChoice = document.createElement("input");
    staleChoice.type = "hidden";
    staleChoice.name = "participants";
    staleChoice.value = "999999";
    document.querySelector("form[data-academic-event-form]").append(staleChoice);
  });
  await Promise.all([
    page.waitForNavigation({ waitUntil: "load" }),
    page.locator("form[data-academic-event-form]").evaluate((form) => form.submit()),
  ]);
  const invalidPicker = page.locator("[data-physician-picker]");
  if (await invalidPicker.getAttribute("data-physician-picker-ready") === null) {
    throw new Error("Physician picker did not enhance after validation failed.");
  }
  const validation = invalidPicker.locator(".physician-picker-validation");
  if (!await validation.isVisible()) {
    throw new Error("Invalid participant submission must show picker validation.");
  }
  if (!await invalidPicker.getByRole("checkbox", { name: "Avery Participant", exact: true }).isChecked() ||
      await invalidPicker.getByRole("checkbox", { name: "Blair Participant", exact: true }).isChecked() ||
      !await invalidPicker.getByRole("button", { name: "Remove Avery Participant", exact: true }).isVisible() ||
      await invalidPicker.getByRole("button", { name: "Remove Blair Participant", exact: true }).count() !== 0) {
    throw new Error("Invalid response must retain the selected physician's native choice and chip only.");
  }
  await page.setViewportSize({ width: 600, height: 800 });
  const containedError = await invalidPicker.evaluate((element) => {
    const error = element.querySelector(".physician-picker-validation");
    const available = element.querySelector("[data-physician-picker-choices]");
    const selected = element.querySelector("[data-physician-picker-selected]");
    if (!error || !available || !selected) return false;
    const errorBox = error.getBoundingClientRect();
    const pickerBox = element.getBoundingClientRect();
    return errorBox.left >= pickerBox.left && errorBox.right <= pickerBox.right &&
      errorBox.top >= available.getBoundingClientRect().bottom &&
      selected.getBoundingClientRect().top >= errorBox.bottom;
  });
  if (!containedError) throw new Error("Picker validation must remain contained without overlapping selections.");

  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""


class PhysicianPickerLayoutBrowserTests(LiveServerTestCase):
    serve_static = False

    @classmethod
    def setUpClass(cls):
        cls.node, cls.browser_executable = require_browser_tools()
        super().setUpClass()

    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(username="picker-browser-scheduler")
        self.client.force_login(self.scheduler)
        self.definition = AcademicEventDefinition.objects.create(
            base_name="Picker browser event", start_academic_year=2026
        )
        Physician.objects.create(display_name="Avery Participant")
        Physician.objects.create(display_name="Blair Participant")
        Physician.objects.bulk_create(
            Physician(display_name=f"Roster Physician {index:02}") for index in range(40)
        )
        Physician.objects.create(
            display_name=(
                "An exceptionally long physician name that must wrap inside the picker without "
                "pushing any control beyond its narrow viewport"
            )
        )

    def test_picker_selection_removal_validation_and_bounded_scrolling(self):
        static_root = Path(__file__).resolve().parents[1] / "static" / "schedules"
        completed = subprocess.run(
            [self.node, "-e", _BROWSER_RUNNER],
            input=json.dumps(
                {
                    "baseUrl": self.live_server_url,
                    "browserExecutable": self.browser_executable,
                    "url": (
                        f"{reverse('academic-event-create')}?academic_year=2026&definition="
                        f"{self.definition.pk}"
                    ),
                    "sessionCookie": {
                        "name": settings.SESSION_COOKIE_NAME,
                        "value": self.client.cookies[settings.SESSION_COOKIE_NAME].value,
                    },
                    "staticFiles": {
                        "/static/schedules/application.css": str(static_root / "application.css"),
                        "/static/schedules/physicians-picker.js": str(
                            static_root / "physicians-picker.js"
                        ),
                    },
                }
            ),
            capture_output=True,
            text=True,
        )

        if completed.returncode:
            self.fail(completed.stderr)
