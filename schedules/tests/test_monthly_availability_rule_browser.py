import json
import shutil
import subprocess
from pathlib import Path
from unittest import SkipTest

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from schedules.models import Physician

_BROWSER_RUNNER = r"""
const { execFileSync } = require("child_process");
const { readFileSync } = require("fs");
const path = require("path");

const input = JSON.parse(readFileSync(0, "utf8"));
const globalRoot = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
const { chromium } = require(path.join(globalRoot, "@playwright/cli/node_modules/playwright"));

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: input.browserExecutable });
  const page = await browser.newPage({ viewport: { width: 900, height: 800 } });
  await page.setContent(input.html);
  await page.addScriptTag({ content: readFileSync(input.script, "utf8") });

  const annualWindow = page.locator("[data-monthly-annual-window]");
  const start = annualWindow.locator('select[name="annual_start_month"]');
  const end = annualWindow.locator('select[name="annual_end_month"]');
  const annualWindowHelp = (await annualWindow.locator("header p").innerText()).replace(/\s+/g, " ").trim();
  const statesAnnualRepeat = /\b(?:repeats?|recurs?)\s+(?:every|each)\s+year\b/i.test(annualWindowHelp)
    && !/\b(?:does not|doesn't|never)\s+(?:repeat|recur)/i.test(annualWindowHelp);
  const statesWindowMayCrossNewYear = /\b(?:may|can)\s+(?:cross|span)\s+(?:the\s+)?New Year\b/i.test(annualWindowHelp)
    && !/\b(?:cannot|can't|does not|doesn't|never)\s+(?:cross|span)|\b(?:may|can)\s+not\s+(?:cross|span)/i.test(annualWindowHelp);
  const statesWeeklyFallback = /\boutside\s+(?:it|(?:the\s+)?(?:annual\s+)?window),?\s+(?:the\s+)?weekly\s+template\s+applies\b|\b(?:the\s+)?weekly\s+template\s+applies\s+outside\s+(?:it|(?:the\s+)?(?:annual\s+)?window)\b/i.test(annualWindowHelp)
    && !/\bweekly\s+template\s+(?:does not|doesn't|never)\s+apply/i.test(annualWindowHelp);
  if (!statesAnnualRepeat || !statesWindowMayCrossNewYear || !statesWeeklyFallback) {
    throw new Error("The annual window helper must explain its annual repeat, New Year boundary, and weekly fallback.");
  }
  if (await start.isDisabled() !== true || await end.isDisabled() !== true) {
    throw new Error("All-year mode must disable inapplicable month values.");
  }
  if (await start.evaluate((input) => input.required)) {
    throw new Error("All-year mode must not require months.");
  }

  await annualWindow.getByLabel("Selected months").check();
  if (await start.isDisabled() || await end.isDisabled()) {
    throw new Error("Selected-month mode must enable both month controls.");
  }
  if (!(await start.evaluate((input) => input.required)) || !(await end.evaluate((input) => input.required))) {
    throw new Error("Selected-month mode must use native required validation.");
  }
  const startControl = await start.evaluate((input) => ({ id: input.id, labelFor: input.labels?.[0]?.htmlFor }));
  if (!startControl.id || startControl.labelFor !== startControl.id) {
    throw new Error("The start-month control must retain its associated label.");
  }
  await annualWindow.locator(`label[for="${startControl.labelFor}"]`).click();
  if (await page.evaluate(() => document.activeElement?.id) !== startControl.id) {
    throw new Error("The start-month label must focus its compact native control.");
  }
  if (await start.evaluate((input) => input.checkValidity())) {
    throw new Error("A partial selected-month window must fail native validation.");
  }
  await start.selectOption("9");
  await end.selectOption("5");
  await annualWindow.getByLabel("All year").check();
  if (await start.isDisabled() !== true || await start.inputValue() !== "9" || await end.inputValue() !== "5") {
    throw new Error("Changing mode must retain month values while making them inapplicable.");
  }
  await annualWindow.getByLabel("Selected months").check();
  if (await start.inputValue() !== "9" || await end.inputValue() !== "5") {
    throw new Error("Changing mode must restore the retained month values.");
  }

  const periodPicker = await browser.newPage();
  await periodPicker.setContent(`
    <form method="get" data-period-picker-form>
      <label for="period-start">Start date</label>
      <input id="period-start" name="start_date" type="date" value="2025-02-03" data-period-picker-input>
      <label for="period-end">End date</label>
      <input id="period-end" name="end_date" type="date" value="2025-02-16" data-period-picker-input>
      <button type="submit" data-period-picker-submit>Inspect weeks</button>
    </form>
  `);
  await periodPicker.evaluate(() => {
    const form = document.querySelector("form");
    const endDate = document.querySelector("#period-end");
    Object.defineProperty(form, "requestSubmit", { value: undefined });
    Object.defineProperty(endDate, "showPicker", { value: undefined });
    window.periodPickerSubmission = new Promise((resolve) => {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        resolve(Object.fromEntries(new FormData(form)));
      }, { once: true });
    });
  });
  await periodPicker.addScriptTag({ content: readFileSync(input.periodPickerScript, "utf8") });
  const fallbackButton = periodPicker.locator("[data-period-picker-submit]");
  if (await fallbackButton.isHidden()) {
    throw new Error("Unsupported period-picker controls must retain their native submit button.");
  }
  await fallbackButton.click();
  const fallbackSubmission = await periodPicker.evaluate(() => window.periodPickerSubmission);
  if (fallbackSubmission.start_date !== "2025-02-03" || fallbackSubmission.end_date !== "2025-02-16") {
    throw new Error("The native period fallback must submit the current complete date range.");
  }

  await browser.close();
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""


class MonthlyAvailabilityRuleBrowserTests(TestCase):
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
        self.user = get_user_model().objects.create_user(username="annual-window-browser")
        self.physician = Physician.objects.create(display_name="Annual window browser physician")

    def test_annual_window_mode_uses_stable_native_controls(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("physician-monthly-availability-rule-create", args=(self.physician.pk,))
        )
        completed = subprocess.run(
            [self.node, "-e", _BROWSER_RUNNER],
            input=json.dumps(
                {
                    "browserExecutable": self.browser_executable,
                    "script": str(
                        Path(__file__).resolve().parents[1]
                        / "static/schedules/monthly-annual-window.js"
                    ),
                    "periodPickerScript": str(
                        Path(__file__).resolve().parents[1] / "static/schedules/period-picker.js"
                    ),
                    "html": response.content.decode(),
                }
            ),
            capture_output=True,
            text=True,
        )

        if completed.returncode:
            self.fail(completed.stderr)
