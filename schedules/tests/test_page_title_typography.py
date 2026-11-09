import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import SkipTest

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from schedules.tests.test_work_remaining import _RenderedSurfaceParser

_BROWSER_RUNNER = r"""
const { execFileSync } = require("child_process");
const { readFileSync } = require("fs");
const path = require("path");
const input = JSON.parse(readFileSync(0, "utf8"));
const globalRoot = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
const { chromium } = require(path.join(globalRoot, "@playwright/cli/node_modules/playwright"));
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: input.browserExecutable });
  const page = await browser.newPage();
  await page.route("**/*", (route) => route.abort());
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 900 });
    for (const [name, html] of input.pages) {
      await page.setContent(html);
      await page.addStyleTag({ content: readFileSync(input.stylesheet, "utf8") });
      const title = page.getByRole("heading", { level: 1 });
      if (await title.count() !== 1 || !await title.isVisible()) throw new Error(`${name}: missing visible h1`);
      const typography = await title.evaluate((element) => {
        const style = getComputedStyle(element);
        const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
        return { size: parseFloat(style.fontSize) / rem, weight: style.fontWeight,
          lineHeight: parseFloat(style.lineHeight) / parseFloat(style.fontSize) };
      });
      // docs/design.md specifies the size and weight; compact line height may vary.
      if (Math.abs(typography.size - 1.35) > .01 || typography.weight !== "600" ||
          typography.lineHeight < 1 || typography.lineHeight > 1.5) {
        throw new Error(`${name} at ${width}px: incorrect title typography ${JSON.stringify(typography)}`);
      }
      for (const sectionName of name === "vacation_form.html" ? ["Vacation details", "Dates", "Actions"] : []) {
        const heading = page.getByRole("heading", { level: 2, name: sectionName, exact: true });
        if (await heading.count() !== 1 || !await heading.isVisible()) {
          throw new Error(`${name}: missing visible section ${sectionName}`);
        }
        const size = await heading.evaluate((element) => parseFloat(getComputedStyle(element).fontSize) /
          parseFloat(getComputedStyle(document.documentElement).fontSize));
        if (Math.abs(size - 1.1) > .01) throw new Error(`${name}: incorrect section heading tier`);
      }
    }
  }
  await browser.close();
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""


class PageTitleTypographyTests(SimpleTestCase):
    def rendered_pages(self):
        return [
            (
                "assignment_form.html",
                {"position_label": "Preceptor 2", "session": SimpleNamespace(pk=1)},
                ["Preceptor 2", "assignment"],
            ),
            ("vacation_form.html", {"title": "Record vacation"}, ["Record vacation"]),
            (
                "precepting_solver_review.html",
                {"proposal": SimpleNamespace(pk=1, get_mode_display="Repair")},
                ["Review", "Repair", "proposal"],
            ),
        ]

    def test_representative_templates_render_their_context_in_semantic_page_titles(self):
        for template, context, terms in self.rendered_pages():
            with self.subTest(template=template):
                parser = _RenderedSurfaceParser()
                parser.feed(render_to_string(f"schedules/{template}", context))
                headings = [element for element in parser.elements if element.tag == "h1"]
                self.assertEqual(len(headings), 1)
                text = " ".join(headings[0].text().split())
                for term in terms:
                    self.assertIn(term, text)
                if template == "vacation_form.html":
                    title = next(
                        element.text() for element in parser.elements if element.tag == "title"
                    )
                    for term in (context["title"], "Vacation Schedule", "Muriel"):
                        self.assertIn(term, title)

    def test_rendered_title_and_section_typography_at_desktop_and_phone_widths(self):
        node, browser = shutil.which("node"), shutil.which("google-chrome")
        if not node or not browser:
            raise SkipTest("Playwright browser coverage requires Node and Google Chrome.")
        completed = subprocess.run(
            [node, "-e", _BROWSER_RUNNER],
            input=json.dumps(
                {
                    "browserExecutable": browser,
                    "stylesheet": str(
                        Path(__file__).resolve().parents[1] / "static/schedules/application.css"
                    ),
                    "pages": [
                        (template, render_to_string(f"schedules/{template}", context))
                        for template, context, _ in self.rendered_pages()
                    ],
                }
            ),
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
