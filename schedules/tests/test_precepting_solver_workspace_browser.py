import json
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.http import HttpResponse
from django.test import LiveServerTestCase, override_settings
from django.urls import include, path, reverse
from openpyxl import Workbook

from schedules.models import (
    EffectiveRole,
    FacultyAvailabilityTemplate,
    FindingAcknowledgement,
    InterviewShift,
    LateDocAssignment,
    LateDocAssignmentChange,
    PreceptingSolverProposal,
    PreceptorAssignment,
    PreceptorAssignmentChange,
    PreceptorSession,
    SchedulePeriod,
)
from schedules.services.interviews import assign_interview_shift, create_interview_shift
from schedules.services.physicians import (
    add_role,
    create_faculty_availability_template,
    create_physician,
)
from schedules.services.precepting_solver import edit_proposed_assignment, review_proposed_late_doc
from schedules.services.preceptors import assign_preceptor, open_weekly_draft
from schedules.services.rounding import record_normal_rounder
from schedules.services.vacations import create_vacation
from schedules.tests.browser_tools import require_browser_tools

_BROWSER_RUNNER = r"""
const { readFileSync } = require("fs");

const input = JSON.parse(readFileSync(0, "utf8"));
const { withBrowserPage } = require(process.env.PRECEPTOR_BROWSER_SESSION);
const REQUEST_BOUNDARY_TIMEOUT_MS = 10_000;

function waitForBoundary(promise, description) {
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`Timed out waiting for ${description}`)), REQUEST_BOUNDARY_TIMEOUT_MS);
  });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}

let interceptedRequest;
let closing = false;
withBrowserPage(input.browserExecutable, async ({ page }) => {
    await page.route("**/*", async (route) => {
    if (
      interceptedRequest &&
      route.request().method() === interceptedRequest.method &&
      route.request().url().endsWith(interceptedRequest.path)
    ) {
      interceptedRequest.markObserved();
      try {
        await interceptedRequest.released;
        const boundaryUrl = input.baseUrl + interceptedRequest.boundaryPath;
        const responsePromise = page.waitForResponse(
          (response) => response.url() === boundaryUrl && response.request().method() === interceptedRequest.method,
          { timeout: REQUEST_BOUNDARY_TIMEOUT_MS }
        );
        const [, response] = await Promise.all([
          route.continue({ url: boundaryUrl }), responsePromise,
        ]);
        interceptedRequest.responses.push(response.status());
        interceptedRequest.markCompleted();
      } catch (error) {
        if (!closing) interceptedRequest.failCompletion(error);
      }
      return;
    }
    const script = Object.entries(input.staticScripts || {}).find(([suffix]) => route.request().url().endsWith(suffix));
    if (script) return route.fulfill({ body: readFileSync(script[1]), contentType: script[0].endsWith(".css") ? "text/css" : "text/javascript" });
    return route.request().resourceType() === "document" ? route.continue() : route.abort();
    });
    for (const action of input.actions) {
    if (action.kind === "goto") {
      await page.goto(input.baseUrl + action.path, { waitUntil: "domcontentloaded" });
    } else if (action.kind === "scroll") {
      await page.locator(action.selector).scrollIntoViewIfNeeded();
    } else if (action.kind === "hover") {
      await page.locator(action.selector).hover(
        action.position ? { position: action.position } : undefined
      );
    } else if (action.kind === "viewport") {
      await page.setViewportSize({ width: action.width, height: action.height });
    } else if (action.kind === "wait-visible") {
      await page.locator(action.selector).waitFor({ state: "visible" });
    } else if (action.kind === "focus") {
      await page.locator(action.selector).focus();
    } else if (action.kind === "press") {
      await Promise.all([
        page.waitForNavigation({ waitUntil: "domcontentloaded" }),
        page.locator(action.selector).press(action.key),
      ]);
    } else if (action.kind === "url-equals") {
      if (page.url() !== input.baseUrl + action.path) {
        throw new Error(`Expected ${action.path}, got ${page.url()}`);
      }
    } else if (action.kind === "fill") {
      await page.locator(action.selector).fill(action.value);
    } else if (action.kind === "month-geometry") {
      const aligned = await page.locator('.calendar-surface--assignment-month').evaluate((grid) => {
        const weeks = [...grid.querySelectorAll('.calendar-lattice-week')];
        // Date backgrounds fill their tracks; assignment items are inset by
        // the shared calendar's margins. Compare their margin boxes, allowing
        // one CSS pixel for independently rounded grid/subgrid boundaries.
        const aligns = (bounds, header, leftInset = 0, rightInset = 0) =>
          bounds.width > 0 && Math.abs(bounds.left - leftInset - header.left) <= 1 &&
          Math.abs(bounds.right + rightInset - header.right) <= 1;
        return weeks.length > 0 && weeks.every((week) => {
          const headers = [...week.querySelectorAll('.calendar-lattice-date-cell')];
          const lanes = [...week.querySelectorAll('.calendar-lattice-lane')];
          return headers.length === 5 && lanes.length > 0 && lanes.every((lane) => {
            const backgrounds = [...lane.querySelectorAll('.calendar-content-cell')];
            const items = [...lane.querySelectorAll('.calendar-lattice-item')];
            return backgrounds.length === headers.length && backgrounds.every((cell, index) => {
              const header = headers[index];
              return cell.dataset.calendarDay === header.querySelector('time').dateTime &&
                aligns(cell.getBoundingClientRect(), header.getBoundingClientRect());
            }) && items.length > 0 && items.every((item) => {
              const style = getComputedStyle(item);
              // Use the item's declared date column, not its position among
              // siblings: empty dates need not have placeholder items.
              const header = headers[Number(style.gridColumnStart) - 1];
              return header && aligns(
                item.getBoundingClientRect(), header.getBoundingClientRect(),
                parseFloat(style.marginLeft), parseFloat(style.marginRight)
              );
            });
          });
        });
      });
      if (!aligned) throw new Error('Month assignment lanes no longer align with their date columns');
    } else if (action.kind === "month-edit-readable") {
      const readable = await page.locator('#month-last-edit').evaluate((field) => {
        const bounds = field.getBoundingClientRect();
        const pdf = field.parentElement.querySelector('a').getBoundingClientRect();
        const range = document.createRange();
        range.selectNodeContents(field);
        const lines = [...range.getClientRects()].filter((rect) => rect.width > 0);
        return bounds.width > 0 && bounds.left >= 0 && bounds.right <= innerWidth &&
          lines.every((line) => line.left >= bounds.left - 1 && line.right <= bounds.right + 1) &&
          (bounds.right <= pdf.left || bounds.left >= pdf.right ||
           bounds.bottom <= pdf.top || bounds.top >= pdf.bottom);
      });
      if (!readable) throw new Error('Month edit time is clipped or overlaps the PDF action');
    } else if (action.kind === "select") {
      await page.locator(action.selector).selectOption(String(action.value));
    } else if (action.kind === "set-input-files") {
      await page.locator(action.selector).setInputFiles(action.path);
    } else if (action.kind === "intercept-request") {
      let observed, release, completed, failCompletion;
      interceptedRequest = {
        method: action.method || "POST",
        observed: new Promise((resolve) => { observed = resolve; }),
        released: new Promise((resolve) => { release = resolve; }),
        completed: new Promise((resolve, reject) => {
          completed = resolve;
          failCompletion = reject;
        }),
        path: action.path,
        boundaryPath: action.boundaryPath,
        responses: [],
      };
      // The response can fail before the release action starts awaiting it.
      // Retain that rejection for release-request without an unhandled rejection
      // aborting Node before the owning browser cleanup runs.
      interceptedRequest.completed.catch(() => {});
      interceptedRequest.markObserved = observed;
      interceptedRequest.release = release;
      interceptedRequest.markCompleted = completed;
      interceptedRequest.failCompletion = failCompletion;
    } else if (action.kind === "click") {
      await page.locator(action.selector).click(
        {
          ...(action.position ? { position: action.position } : {}),
          ...(action.waitForRequest ? { noWaitAfter: true } : {}),
        }
      );
      if (!action.waitForRequest) await page.waitForLoadState("domcontentloaded");
    } else if (action.kind === "check") {
      await page.locator(action.selector).check();
    } else if (action.kind === "checked") {
      if ((await page.locator(action.selector).isChecked()) !== action.value) {
        throw new Error(`Unexpected checked state for ${action.selector}`);
      }
    } else if (action.kind === "submit") {
      await page.locator(action.selector).evaluate((form) => form.submit());
      await page.waitForLoadState("domcontentloaded");
    } else if (action.kind === "request-observed") {
      await waitForBoundary(interceptedRequest.observed, "the intercepted request");
    } else if (action.kind === "release-request") {
      interceptedRequest.release();
      await waitForBoundary(interceptedRequest.completed, "the controlled request response");
    } else if (action.kind === "request-count") {
      if (interceptedRequest.responses.length !== action.value) {
        throw new Error(`Expected ${action.value} intercepted requests; got ${interceptedRequest.responses.length}`);
      }
    } else if (action.kind === "response-status") {
      if (!interceptedRequest.responses.every((status) => status === action.value)) {
        throw new Error(`Expected intercepted response status ${action.value}; got ${interceptedRequest.responses}`);
      }
    } else if (action.kind === "text") {
      const bodyText = await page.locator(action.selector || "body").innerText();
      if (!bodyText.includes(action.value)) {
        throw new Error(`Expected page text: ${action.value}\n${bodyText}`);
      }
    } else if (action.kind === "summary-lines") {
      const summaries = await page.locator(action.selector).evaluate((tooltip, values) => {
        // Finding tooltips require one rendered summary per line, while the text within
        // a summary may wrap. innerText preserves a line break introduced by
        // nested markup so its whitespace can be normalized for that comparison.
        const normalizedText = (element) => element.innerText.replace(/\s+/g, " ").trim();
        const elements = values.map((value) =>
          [...tooltip.querySelectorAll("*")].find((element) =>
            normalizedText(element) === value &&
            element.getBoundingClientRect().width > 0 &&
            element.getBoundingClientRect().height > 0
          )
        );
        if (elements.some((element) => !element)) return null;
        const bounds = elements.map((element) => element.getBoundingClientRect());
        return bounds.every((bound, index) =>
          bounds.slice(index + 1).every((later) =>
            bound.bottom <= later.top || later.bottom <= bound.top
          )
        );
      }, action.values);
      if (!summaries) {
        throw new Error(`Expected separate rendered tooltip summaries ${action.values}`);
      }
    } else if (action.kind === "url") {
      if (!page.url().includes(action.value)) {
        throw new Error(`Expected URL containing ${action.value}; got ${page.url()}`);
      }
    } else if (action.kind === "disabled") {
      if ((await page.locator(action.selector).isDisabled()) !== action.value) {
        throw new Error(`Unexpected disabled state for ${action.selector}`);
      }
    } else if (action.kind === "visible") {
      if (!await page.locator(action.selector).isVisible()) {
        throw new Error(`Expected visible selector: ${action.selector}`);
      }
    } else if (action.kind === "hidden") {
      if (await page.locator(action.selector).isVisible()) {
        throw new Error(`Expected hidden selector: ${action.selector}`);
      }
    } else if (action.kind === "painted") {
      const painted = await page.locator(action.selector).evaluate((element) => {
        const bounds = element.getBoundingClientRect();
        if (!bounds.width || !bounds.height) return false;
        const x = Math.max(bounds.left + 1, Math.min(bounds.right - 1, window.innerWidth - 1));
        const y = Math.max(bounds.top + 1, Math.min(bounds.bottom - 1, window.innerHeight - 1));
        const hit = document.elementFromPoint(x, y);
        return Boolean(hit && element.contains(hit));
      });
      if (!painted) throw new Error(`Expected painted selector: ${action.selector}`);
    } else if (action.kind === "legend-readable") {
      const readable = await page.locator(action.selector).evaluate((legend) => {
        // Keep each finding label paired with its severity glyph and readable
        // when the legend wraps; surrounding markup may vary.
        const legendBounds = legend.getBoundingClientRect();
        if (!legendBounds.width || legendBounds.left < -1 ||
          legendBounds.right > window.innerWidth + 1) return false;
        const expected = [
          [".calendar-finding-indicator--hard", "Hard finding"],
          [".calendar-finding-indicator--exception", "Acknowledged exception"],
          [".calendar-finding-indicator--preference", "Preference finding"],
        ];
        const items = expected.map(([selector, name]) => {
          const glyph = legend.querySelector(`.calendar-status-legend-glyph${selector}`);
          const item = glyph?.closest(".calendar-status-legend-item");
          const label = item?.querySelector(".calendar-status-legend-label");
          if (!label || label.innerText !== name) return null;
          const itemBounds = item.getBoundingClientRect();
          const labelBounds = label.getBoundingClientRect();
          if (!(itemBounds.left >= legendBounds.left - 1 &&
            itemBounds.right <= legendBounds.right + 1 &&
            itemBounds.top >= legendBounds.top - 1 &&
            itemBounds.bottom <= legendBounds.bottom + 1 &&
            labelBounds.left >= itemBounds.left - 1 &&
            labelBounds.right <= itemBounds.right + 1 &&
            labelBounds.top >= itemBounds.top - 1 &&
            labelBounds.bottom <= itemBounds.bottom + 1 &&
            label.scrollWidth <= label.clientWidth + 1)) return null;
          return item;
        });
        return !items.includes(null) && items.every((item, index) =>
          items.slice(index + 1).every((later) =>
            item.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING
          )
        );
      });
      if (!readable) throw new Error(`Expected complete readable legend: ${action.selector}`);
    } else if (action.kind === "open") {
      const open = await page.locator(action.selector).evaluate((element) => element.open);
      if (open !== action.value) throw new Error(`Unexpected open state for ${action.selector}`);
    } else if (action.kind === "before") {
      const isBefore = await page.locator(action.selector).evaluate((element, otherSelector) => {
        const other = document.querySelector(otherSelector);
        return Boolean(other && element.compareDocumentPosition(other) & Node.DOCUMENT_POSITION_FOLLOWING);
      }, action.other);
      if (!isBefore) throw new Error(`Expected ${action.selector} before ${action.other}`);
    } else if (action.kind === "right-aligned") {
      const aligned = await page.locator(action.selector).evaluate((element, containerSelector) => {
        const container = element.closest(containerSelector);
        if (!container) return false;
        const marker = element.getBoundingClientRect();
        const slot = container.getBoundingClientRect();
        return marker.left >= slot.left + slot.width / 2 && slot.right - marker.right <= 7;
      }, action.container);
      if (!aligned) throw new Error(`Expected ${action.selector} aligned to the right of ${action.container}`);
    } else if (action.kind === "output") {
      const value = await page.locator(action.selector).evaluate((element) => element.value);
      if (value !== action.value) throw new Error(`Expected ${action.selector} to be ${action.value}; got ${value}`);
    } else if (action.kind === "page-show") {
      await page.evaluate(() => window.dispatchEvent(new Event("pageshow")));
    }
    }
}, { beforeClose: () => {
  closing = true;
  interceptedRequest?.release();
} }).catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
"""


workbook_submissions = []


def accept_browser_submission(request):
    return HttpResponse(status=204)


def record_workbook_submission(request):
    workbook = request.FILES["workbook"]
    workbook_submissions.append(
        {
            "actions": request.POST.getlist("action"),
            "filename": workbook.name,
            "content": workbook.read(),
        }
    )
    return HttpResponse(status=204)


urlpatterns = [
    path("_browser-test/request-boundary/", accept_browser_submission),
    path("_browser-test/workbook-submission/", record_workbook_submission),
    path("", include("preceptor_project.urls")),
]


class PreceptingSolverWorkspaceBrowserTests(LiveServerTestCase):
    """Exercise the no-JavaScript solve and review forms in a real browser."""

    serve_static = False
    reset_sequences = True
    week = date(2027, 2, 1)

    @classmethod
    def setUpClass(cls):
        cls.node, cls.browser_executable = require_browser_tools()
        super().setUpClass()

    def setUp(self):
        workbook_submissions.clear()
        self.scheduler = get_user_model().objects.create_user(
            username="browser-scheduler", password="browser-password"
        )
        group, _ = Group.objects.get_or_create(name="Scheduling Administrators")
        group.permissions.add(Permission.objects.get(codename="manage_scheduling_administration"))
        self.scheduler.groups.add(group)
        self.today = patch(
            "schedules.services.precepting_solver.organization_today", return_value=date(2026, 1, 1)
        )
        self.today.start()
        self.addCleanup(self.today.stop)
        self.workbook_directory = TemporaryDirectory()
        self.addCleanup(self.workbook_directory.cleanup)
        self.workbook_path = Path(self.workbook_directory.name) / "valid-workbook.xlsx"
        workbook = Workbook()
        workbook.save(self.workbook_path)
        workbook.close()

    def browser(self, *actions, load_static=True):
        try:
            subprocess.run(
                [self.node, "-e", _BROWSER_RUNNER],
                input=json.dumps(
                    {
                        "baseUrl": self.live_server_url,
                        "browserExecutable": self.browser_executable,
                        "actions": actions,
                        "staticScripts": (
                            {
                                "/static/schedules/range-controls.js": str(
                                    Path(__file__).parents[1] / "static/schedules/range-controls.js"
                                ),
                                "/static/schedules/precepting-solve-controls.js": str(
                                    Path(__file__).parents[1]
                                    / "static/schedules/precepting-solve-controls.js"
                                ),
                                "/static/schedules/form-processing-feedback.js": str(
                                    Path(__file__).parents[1]
                                    / "static/schedules/form-processing-feedback.js"
                                ),
                                "/static/schedules/application.css": str(
                                    Path(__file__).parents[1] / "static/schedules/application.css"
                                ),
                                "/static/schedules/calendar-week-actions.js": str(
                                    Path(__file__).parents[1]
                                    / "static/schedules/calendar-week-actions.js"
                                ),
                            }
                            if load_static
                            else {}
                        ),
                    }
                ),
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as error:
            self.fail(error.stderr)

    def draft_state(self):
        return {
            "late_doc_assignments": list(
                LateDocAssignment.objects.order_by("pk").values_list(
                    "pk", "date", "physician_id", "created_at"
                )
            ),
            "late_doc_history": list(
                LateDocAssignmentChange.objects.order_by("pk").values_list(
                    "pk",
                    "assignment_id",
                    "assignment_id_at_recording",
                    "date",
                    "action",
                    "previous_value",
                    "recorded_value",
                    "note",
                    "changed_by_id",
                    "changed_at",
                )
            ),
            "preceptor_assignments": list(
                PreceptorAssignment.objects.order_by("pk").values_list(
                    "pk",
                    "session_id",
                    "physician_id",
                    "responsibility",
                    "primary_position",
                    "is_locked",
                )
            ),
            "preceptor_history": list(
                PreceptorAssignmentChange.objects.order_by("pk").values_list(
                    "pk",
                    "session_id",
                    "assignment_id",
                    "action",
                    "previous_value",
                    "recorded_value",
                    "reason",
                    "changed_by_id",
                    "changed_at",
                )
            ),
        }

    def sign_in_actions(self):
        return (
            {"kind": "goto", "path": "/solve/"},
            {"kind": "text", "value": "Sign in"},
            {
                "kind": "fill",
                "selector": 'input[name="username"]',
                "value": self.scheduler.username,
            },
            {"kind": "fill", "selector": 'input[name="password"]', "value": "browser-password"},
            {"kind": "click", "selector": 'button[type="submit"]'},
            {"kind": "text", "value": "Schedule Bot"},
        )

    def faculty(self, name, availability="open"):
        physician = create_physician(display_name=name, scheduler=self.scheduler)
        add_role(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2026, 1, 1),
            effective_end=None,
            scheduler=self.scheduler,
        )
        create_faculty_availability_template(
            physician=physician,
            effective_start=date(2026, 1, 1),
            effective_end=None,
            slots={slot: availability for slot, _ in FacultyAvailabilityTemplate.SLOT_CHOICES},
            scheduler=self.scheduler,
        )
        return physician

    def ready_range(self, start_date, end_date, availability="open"):
        rounder = self.faculty("Rounder", availability)
        week = start_date - timedelta(days=start_date.weekday())
        while week <= end_date:
            record_normal_rounder(week=week, physician=rounder, scheduler=self.scheduler)
            open_weekly_draft(week)
            week += timedelta(days=7)
        return rounder

    def record_normal_rounders(self, start_date, end_date):
        rounder = self.faculty("Rounder")
        week = start_date - timedelta(days=start_date.weekday())
        while week <= end_date:
            record_normal_rounder(week=week, physician=rounder, scheduler=self.scheduler)
            week += timedelta(days=7)
        return rounder

    def retained_proposal(self, *, include_alternative=False):
        self.ready_range(self.week, self.week.replace(day=5))
        self.faculty("Second")
        alternative = self.faculty("Z Late Doc alternative") if include_alternative else None
        self.client.force_login(self.scheduler)
        response = self.client.post(
            "/solve/",
            {
                "mode": "fill",
                "range_kind": "custom",
                "start_date": self.week.isoformat(),
                "end_date": self.week.replace(day=5).isoformat(),
                "balance_span": "6-months",
            },
        )
        self.assertEqual(response.status_code, 302)
        proposal = PreceptingSolverProposal.objects.get()
        if alternative:
            proposal.proposed_assignments["late_doc"][0]["explanation"][
                "equally_good_alternatives"
            ] = [
                {
                    "physician_id": alternative.pk,
                    "physician_name": alternative.display_name,
                    "reason": "Produces the same complete named policy outcome.",
                }
            ]
            proposal.save(update_fields=("proposed_assignments",))
        return proposal, alternative

    def test_browser_posts_a_hard_conflict_assignment_without_javascript(self):
        # Sort before Direct Interviewer: the disclosure below opens the first conflict.
        arlo = self.faculty("Arlo Alder")
        arlo_role = arlo.roles.get(role=EffectiveRole.Role.FACULTY)
        arlo_role.faculty_leadership = EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR
        arlo_role.save(update_fields=("faculty_leadership",))
        session = next(
            item
            for item in open_weekly_draft(self.week)
            if item.period_of_day == PreceptorSession.PeriodOfDay.AM
        )
        create_vacation(
            physician=arlo,
            start_date=self.week,
            end_date=self.week,
            period_of_day="AM",
            scheduler=self.scheduler,
        )
        interviewer = self.faculty("Direct Interviewer")
        shift = create_interview_shift(academic_year=2026, date=self.week, scheduler=self.scheduler)
        assign_interview_shift(shift=shift, physician=interviewer, scheduler=self.scheduler)

        self.browser(
            *self.sign_in_actions(),
            {"kind": "goto", "path": f"/sessions/{session.pk}/assignments/primary-1/"},
            {"kind": "text", "value": "Hard-conflict or not-available physicians"},
            {"kind": "text", "value": "Assessment details"},
            {
                "kind": "click",
                "selector": "fieldset.hard-conflict .alternative:first-of-type details summary",
            },
            {"kind": "text", "value": "Program Director Interview"},
            {"kind": "select", "selector": 'select[name="physician"]', "value": arlo.pk},
            {"kind": "click", "selector": 'form.assignment-form button[type="submit"]'},
            {"kind": "url", "value": "?month=2027-02"},
            load_static=False,
        )

        self.assertEqual(
            PreceptorAssignment.objects.get(session=session, primary_position=1).physician,
            arlo,
        )

    def test_browser_groups_every_hard_exclusion_by_physician(self):
        self.ready_range(self.week, self.week.replace(day=5))
        self.faculty("Available")
        self.faculty("Alternative")
        director = self.faculty("Program Director")
        director_role = director.roles.get(role=EffectiveRole.Role.FACULTY)
        director_role.faculty_leadership = EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR
        director_role.save(update_fields=("faculty_leadership",))
        interviewer = self.faculty("Interviewer")
        create_vacation(
            physician=director,
            start_date=self.week,
            end_date=self.week,
            period_of_day="AM",
            scheduler=self.scheduler,
        )
        shift = create_interview_shift(academic_year=2026, date=self.week, scheduler=self.scheduler)
        assign_interview_shift(shift=shift, physician=interviewer, scheduler=self.scheduler)
        self.client.force_login(self.scheduler)
        response = self.client.post(
            "/solve/",
            {
                "mode": "fill",
                "range_kind": "custom",
                "start_date": self.week.isoformat(),
                "end_date": self.week.replace(day=5).isoformat(),
                "balance_span": "6-months",
            },
        )
        self.assertEqual(response.status_code, 302)
        proposal = PreceptingSolverProposal.objects.get()

        slot_row = 'tr:has(td:has-text("2027-02-01 AM Preceptor 1"))'
        why_this = (
            f"{slot_row} .solver-review-compact-disclosure:has(> summary:has-text('Why this?'))"
        )
        hard_exclusions_heading = f'{why_this} > h3:has-text("Hard exclusions")'
        hard_exclusions = f"{hard_exclusions_heading} + ul"
        director_exclusion = f'{hard_exclusions} > li:has(> strong:has-text("Program Director"))'
        interviewer_exclusion = f'{hard_exclusions} > li:has(> strong:has-text("Interviewer"))'

        self.browser(
            *self.sign_in_actions(),
            {"kind": "goto", "path": f"/solve/{proposal.pk}/?view=changes"},
            {
                "kind": "click",
                "selector": f"{why_this} > summary",
            },
            {"kind": "text", "selector": hard_exclusions_heading, "value": "Hard exclusions"},
            {"kind": "text", "selector": director_exclusion, "value": "Vacation"},
            {
                "kind": "text",
                "selector": director_exclusion,
                "value": "Program Director Interview",
            },
            {"kind": "text", "selector": interviewer_exclusion, "value": "Interview conflict"},
            {
                "kind": "visible",
                "selector": (
                    f'{slot_row} label:has-text("Compare with...") '
                    'select[name="alternative_physician_id"]'
                ),
            },
            load_static=False,
        )

    def test_browser_selects_month_quarter_and_custom_ranges(self):
        self.ready_range(date(2027, 1, 1), date(2027, 3, 31))
        self.ready_range(date(2027, 7, 1), date(2027, 9, 30))
        self.faculty("Second")

        self.browser(
            *self.sign_in_actions(),
            {"kind": "click", "selector": 'input[name="range_kind"][value="month"]'},
            {"kind": "fill", "selector": 'input[name="month"]', "value": "2027-02"},
            {"kind": "click", "selector": 'button:has-text("Run Schedule Bot")'},
            {"kind": "text", "value": "Feb. 1, 2027 through Feb. 28, 2027"},
            {"kind": "goto", "path": "/solve/"},
            {"kind": "click", "selector": 'input[name="range_kind"][value="quarter"]'},
            {"kind": "select", "selector": 'select[name="academic_year"]', "value": "2027"},
            {"kind": "select", "selector": 'select[name="quarter"]', "value": "1"},
            {"kind": "click", "selector": 'button:has-text("Run Schedule Bot")'},
            {"kind": "text", "value": "July 1, 2027 through Sept. 30, 2027"},
            {"kind": "goto", "path": "/solve/"},
            {"kind": "click", "selector": 'input[name="range_kind"][value="custom"]'},
            {"kind": "fill", "selector": 'input[name="start_date"]', "value": "2027-02-01"},
            {"kind": "fill", "selector": 'input[name="end_date"]', "value": "2027-02-05"},
            {"kind": "click", "selector": 'button:has-text("Run Schedule Bot")'},
            {"kind": "text", "value": "Feb. 1, 2027 through Feb. 5, 2027"},
            load_static=False,
        )

    def test_browser_solves_an_untouched_future_month(self):
        month = date(2027, 3, 1)
        self.record_normal_rounders(month, date(2027, 3, 31))
        self.faculty("First")
        self.faculty("Second")
        self.assertFalse(PreceptorSession.objects.exists())

        self.browser(
            *self.sign_in_actions(),
            {"kind": "click", "selector": 'input[name="range_kind"][value="month"]'},
            {"kind": "fill", "selector": 'input[name="month"]', "value": "2027-03"},
            {"kind": "click", "selector": 'button:has-text("Run Schedule Bot")'},
            {"kind": "text", "value": "Proposed schedule"},
            load_static=False,
        )

        self.assertTrue(SchedulePeriod.objects.filter(month=month).exists())
        self.assertEqual(PreceptorSession.objects.filter(period__month=month).count(), 46)
        self.assertFalse(PreceptorAssignment.objects.exists())
        self.assertFalse(LateDocAssignment.objects.exists())

    def test_browser_initializes_and_steps_the_clinic_current_month(self):
        with patch("schedules.views.solver.organization_today", return_value=date(2027, 2, 17)):
            self.browser(
                *self.sign_in_actions(),
                {"kind": "output", "selector": 'input[name="month"]', "value": "2027-02"},
                {"kind": "click", "selector": '[data-solve-step-month="1"]'},
                {"kind": "output", "selector": 'input[name="month"]', "value": "2027-03"},
            )

    def test_browser_reuses_recent_settings_without_javascript(self):
        proposal, _ = self.retained_proposal()

        self.browser(
            *self.sign_in_actions(),
            {"kind": "text", "value": "Recent unapplied proposals"},
            {"kind": "visible", "selector": "#recent-proposals"},
            {"kind": "before", "selector": "#recent-proposals", "other": ".solve-setup"},
            {"kind": "visible", "selector": "#recent-proposals .table-scroll-frame table"},
            {
                "kind": "visible",
                "selector": "#recent-proposals th[scope='row']:has-text('Feb 1, 2027 through Feb 5, 2027')",
            },
            {
                "kind": "visible",
                "selector": "#recent-proposals a.button-link:has-text('Continue review')",
            },
            {"kind": "visible", "selector": "#recent-proposals a:has-text('Reuse settings')"},
            {"kind": "click", "selector": '#recent-proposals a:has-text("Reuse settings")'},
            {"kind": "output", "selector": 'input[name="start_date"]', "value": "2027-02-01"},
            {"kind": "output", "selector": 'input[name="end_date"]', "value": "2027-02-05"},
            {"kind": "output", "selector": 'select[name="balance_span"]', "value": "6-months"},
            {"kind": "text", "value": f"Reusing setup from proposal {proposal.pk}"},
            load_static=False,
        )

    def test_browser_recomputes_effective_and_balance_ranges_for_every_span(self):
        self.browser(
            *self.sign_in_actions(),
            {"kind": "click", "selector": 'input[name="range_kind"][value="custom"]'},
            {"kind": "fill", "selector": 'input[name="start_date"]', "value": "2028-02-01"},
            {"kind": "fill", "selector": 'input[name="end_date"]', "value": "2028-02-29"},
            {
                "kind": "output",
                "selector": "[data-range-controls-effective-range]",
                "value": "Feb 1, 2028 through Feb 29, 2028",
            },
            {"kind": "select", "selector": 'select[name="balance_span"]', "value": "1-day"},
            {
                "kind": "output",
                "selector": "[data-solve-balance-range]",
                "value": "Feb 29, 2028 through Feb 29, 2028",
            },
            {"kind": "select", "selector": 'select[name="balance_span"]', "value": "1-week"},
            {
                "kind": "output",
                "selector": "[data-solve-balance-range]",
                "value": "Feb 23, 2028 through Feb 29, 2028",
            },
            {"kind": "select", "selector": 'select[name="balance_span"]', "value": "2-weeks"},
            {
                "kind": "output",
                "selector": "[data-solve-balance-range]",
                "value": "Feb 16, 2028 through Feb 29, 2028",
            },
            {"kind": "select", "selector": 'select[name="balance_span"]', "value": "4-weeks"},
            {
                "kind": "output",
                "selector": "[data-solve-balance-range]",
                "value": "Feb 2, 2028 through Feb 29, 2028",
            },
            {"kind": "select", "selector": 'select[name="balance_span"]', "value": "1-month"},
            {
                "kind": "output",
                "selector": "[data-solve-balance-range]",
                "value": "Jan 30, 2028 through Feb 29, 2028",
            },
            {"kind": "select", "selector": 'select[name="balance_span"]', "value": "3-months"},
            {
                "kind": "output",
                "selector": "[data-solve-balance-range]",
                "value": "Nov 30, 2027 through Feb 29, 2028",
            },
            {"kind": "select", "selector": 'select[name="balance_span"]', "value": "6-months"},
            {
                "kind": "output",
                "selector": "[data-solve-balance-range]",
                "value": "Aug 30, 2027 through Feb 29, 2028",
            },
            {"kind": "select", "selector": 'select[name="balance_span"]', "value": "1-year"},
            {
                "kind": "output",
                "selector": "[data-solve-balance-range]",
                "value": "Mar 1, 2027 through Feb 29, 2028",
            },
            {"kind": "select", "selector": 'select[name="balance_span"]', "value": "custom"},
            {"kind": "fill", "selector": 'input[name="balance_custom_days"]', "value": "45"},
            {
                "kind": "output",
                "selector": "[data-solve-balance-range]",
                "value": "Jan 16, 2028 through Feb 29, 2028",
            },
        )

    def test_browser_steps_months_and_academic_quarters_with_effective_range_updates(self):
        self.browser(
            *self.sign_in_actions(),
            {"kind": "click", "selector": 'input[name="range_kind"][value="month"]'},
            {"kind": "fill", "selector": 'input[name="month"]', "value": "2027-12"},
            {"kind": "click", "selector": '[data-solve-step-month="1"]'},
            {"kind": "output", "selector": 'input[name="month"]', "value": "2028-01"},
            {
                "kind": "output",
                "selector": "[data-range-controls-effective-range]",
                "value": "Jan 1, 2028 through Jan 31, 2028",
            },
            {"kind": "click", "selector": 'input[name="range_kind"][value="quarter"]'},
            {"kind": "select", "selector": 'select[name="academic_year"]', "value": "2026"},
            {"kind": "select", "selector": 'select[name="quarter"]', "value": "4"},
            {"kind": "click", "selector": '[data-solve-step-academic-year="1"]'},
            {"kind": "output", "selector": 'select[name="academic_year"]', "value": "2027"},
            {"kind": "output", "selector": 'select[name="quarter"]', "value": "4"},
            {"kind": "click", "selector": '[data-solve-step-academic-year="-1"]'},
            {"kind": "output", "selector": 'select[name="academic_year"]', "value": "2026"},
            {"kind": "output", "selector": 'select[name="quarter"]', "value": "4"},
            {"kind": "click", "selector": '[data-solve-step-quarter="1"]'},
            {"kind": "output", "selector": 'select[name="academic_year"]', "value": "2027"},
            {"kind": "output", "selector": 'select[name="quarter"]', "value": "1"},
            {
                "kind": "output",
                "selector": "[data-range-controls-effective-range]",
                "value": "Jul 1, 2027 through Sep 30, 2027",
            },
            {"kind": "click", "selector": '[data-solve-step-quarter="-1"]'},
            {"kind": "output", "selector": 'select[name="academic_year"]', "value": "2026"},
            {"kind": "output", "selector": 'select[name="quarter"]', "value": "4"},
            {
                "kind": "output",
                "selector": "[data-range-controls-effective-range]",
                "value": "Apr 1, 2027 through Jun 30, 2027",
            },
        )

    def test_browser_shows_an_incomplete_proposal(self):
        self.ready_range(self.week, self.week.replace(day=5), availability="off")

        self.browser(
            *self.sign_in_actions(),
            {"kind": "click", "selector": 'input[name="range_kind"][value="custom"]'},
            {"kind": "fill", "selector": 'input[name="start_date"]', "value": "2027-02-01"},
            {"kind": "fill", "selector": 'input[name="end_date"]', "value": "2027-02-05"},
            {"kind": "click", "selector": 'button:has-text("Run Schedule Bot")'},
            {"kind": "text", "value": "Unresolved work"},
            {"kind": "text", "value": "Hard count: 21"},
            {"kind": "text", "value": "Preference count: 0"},
            {
                "kind": "visible",
                "selector": ".solver-review-unresolved",
            },
            {
                "kind": "open",
                "selector": '.solver-review-subsection:has(> summary:has-text("Before findings"))',
                "value": False,
            },
            {
                "kind": "open",
                "selector": '.solver-review-subsection:has(> summary:has-text("After findings"))',
                "value": True,
            },
            {"kind": "text", "value": "Preceptor 1 is unfilled."},
        )

    def test_browser_after_finding_link_opens_its_proposed_calendar_target(self):
        proposal, _ = self.retained_proposal()
        assignment = proposal.proposed_assignments["precepting"][0]
        proposal.after_findings = [
            {
                "date": assignment["date"],
                "code": "not_available_assignment",
                "severity": "hard",
                "period_of_day": assignment["period_of_day"],
                "explanation": {"position": assignment["position"]},
            }
        ]
        proposal.save(update_fields=("after_findings",))
        target = (
            "proposal-position-"
            f"{assignment['date']}-{assignment['period_of_day']}-{assignment['position']}"
        )

        self.browser(
            *self.sign_in_actions(),
            {"kind": "goto", "path": f"/solve/{proposal.pk}/?view=changes"},
            {
                "kind": "click",
                "selector": '.solver-review-subsection[open] a:has-text("Show in proposed schedule")',
            },
            {"kind": "url", "value": f"?view=proposed#{target}"},
            {"kind": "visible", "selector": f"#{target}"},
            {"kind": "text", "value": "Filled"},
            {
                "kind": "text",
                "selector": f"#{target}",
                "value": assignment["physician_name"],
            },
        )

    def test_browser_shows_blocked_solve_feedback_before_the_setup_form(self):
        self.browser(
            *self.sign_in_actions(),
            {"kind": "click", "selector": 'input[name="range_kind"][value="custom"]'},
            {"kind": "fill", "selector": 'input[name="start_date"]', "value": "2027-02-01"},
            {"kind": "fill", "selector": 'input[name="end_date"]', "value": "2027-02-05"},
            {"kind": "click", "selector": 'button:has-text("Run Schedule Bot")'},
            {"kind": "visible", "selector": "#solve-feedback"},
            {"kind": "text", "value": "No normal rounder is recorded for a solve week."},
            {"kind": "before", "selector": "#solve-feedback", "other": "form.solve-setup"},
            load_static=False,
        )

    @override_settings(ROOT_URLCONF="schedules.tests.test_precepting_solver_workspace_browser")
    def test_browser_shows_busy_treatment_only_for_a_valid_solve_submit(self):
        self.browser(
            *self.sign_in_actions(),
            {"kind": "click", "selector": 'input[name="range_kind"][value="custom"]'},
            {"kind": "click", "selector": 'button:has-text("Run Schedule Bot")'},
            {"kind": "hidden", "selector": "[data-processing-feedback]"},
            {"kind": "fill", "selector": 'input[name="start_date"]', "value": "2027-02-01"},
            {"kind": "fill", "selector": 'input[name="end_date"]', "value": "2027-02-05"},
            {
                "kind": "intercept-request",
                "path": "/solve/",
                "boundaryPath": "/_browser-test/request-boundary/",
            },
            {
                "kind": "click",
                "selector": 'button:has-text("Run Schedule Bot")',
                "waitForRequest": True,
            },
            {"kind": "request-observed"},
            {"kind": "release-request"},
            {"kind": "response-status", "value": 204},
            {"kind": "request-count", "value": 1},
            {"kind": "visible", "selector": "[data-processing-feedback]"},
            {"kind": "text", "value": "Building proposal"},
            {"kind": "text", "value": "Schedule Bot can take several seconds."},
            {
                "kind": "disabled",
                "selector": 'form.solve-setup button[type="submit"]',
                "value": True,
            },
            {"kind": "page-show"},
            {"kind": "hidden", "selector": "[data-processing-feedback]"},
            {
                "kind": "disabled",
                "selector": 'form.solve-setup button[type="submit"]',
                "value": False,
            },
        )

    @override_settings(ROOT_URLCONF="schedules.tests.test_precepting_solver_workspace_browser")
    def test_browser_shows_import_processing_feedback_for_each_native_workbook_submit(self):
        for action in ("preview", "apply"):
            workbook_submissions.clear()
            self.browser(
                *self.sign_in_actions(),
                {"kind": "goto", "path": "/vacations/import/"},
                {
                    "kind": "intercept-request",
                    "path": "/vacations/import/",
                    "boundaryPath": "/_browser-test/workbook-submission/",
                },
                {
                    "kind": "click",
                    "selector": 'form.vacation-import-form button[name="action"][value="preview"]',
                },
                {"kind": "hidden", "selector": "[data-processing-feedback]"},
                {"kind": "request-count", "value": 0},
                {
                    "kind": "set-input-files",
                    "selector": 'form.vacation-import-form input[name="workbook"]',
                    "path": str(self.workbook_path),
                },
                {
                    "kind": "click",
                    "selector": f'form.vacation-import-form button[name="action"][value="{action}"]',
                    "waitForRequest": True,
                },
                {"kind": "request-observed"},
                {"kind": "release-request"},
                {"kind": "response-status", "value": 204},
                {"kind": "request-count", "value": 1},
                {"kind": "visible", "selector": "[data-processing-feedback]"},
                {"kind": "text", "value": "Muriel is processing the workbook"},
                {
                    "kind": "disabled",
                    "selector": 'form.vacation-import-form button[name="action"][value="preview"]',
                    "value": True,
                },
                {
                    "kind": "disabled",
                    "selector": 'form.vacation-import-form button[name="action"][value="apply"]',
                    "value": True,
                },
                {
                    "kind": "disabled",
                    "selector": 'form.vacation-import-form button[name="action"][value="preview"]',
                    "value": True,
                },
                {"kind": "page-show"},
                {"kind": "hidden", "selector": "[data-processing-feedback]"},
                {
                    "kind": "disabled",
                    "selector": 'form.vacation-import-form button[name="action"][value="preview"]',
                    "value": False,
                },
                {
                    "kind": "disabled",
                    "selector": 'form.vacation-import-form button[name="action"][value="apply"]',
                    "value": False,
                },
            )
            self.assertEqual(
                workbook_submissions,
                [
                    {
                        "actions": [action],
                        "filename": self.workbook_path.name,
                        "content": self.workbook_path.read_bytes(),
                    }
                ],
            )

    def test_browser_confirms_and_replaces_late_doc_then_compares_and_applies(self):
        proposal, alternative = self.retained_proposal(include_alternative=True)
        review_path_after = f"/solve/{proposal.pk}/"
        late_doc_form = 'form[action$="/compare/"]:has(input[value="late_doc"])'
        expected_primary = next(
            assignment
            for assignment in proposal.proposed_assignments["precepting"]
            if assignment["date"] == self.week.isoformat()
            and assignment["period_of_day"] == PreceptorSession.PeriodOfDay.AM
            and assignment["position"] == "primary-1"
        )

        self.browser(
            *self.sign_in_actions(),
            {"kind": "goto", "path": review_path_after},
            {"kind": "disabled", "selector": 'form[action$="/apply/"] button', "value": True},
            {"kind": "visible", "selector": 'a:has-text("Replace")'},
            {"kind": "click", "selector": 'a:has-text("Replace")'},
            {"kind": "select", "selector": 'select[name="decision"]', "value": "replaced"},
            {
                "kind": "select",
                "selector": 'select[name="replacement_physician"]',
                "value": alternative.pk,
            },
            {"kind": "click", "selector": 'button:has-text("Save decision")'},
            {"kind": "text", "value": "Replaced"},
            {"kind": "click", "selector": 'a:has-text("Week list")'},
            {
                "kind": "select",
                "selector": f'{late_doc_form} select[name="alternative_physician_id"]',
                "value": alternative.pk,
            },
            {"kind": "click", "selector": f"{late_doc_form} button"},
            {"kind": "text", "value": "Latest comparison"},
            {"kind": "click", "selector": 'form[action$="/apply/"] button'},
            {"kind": "url", "value": "?month=2027-02"},
        )
        self.assertEqual(
            list(
                PreceptorAssignment.objects.filter(
                    session__date=self.week,
                    session__period_of_day=PreceptorSession.PeriodOfDay.AM,
                    responsibility=PreceptorAssignment.Responsibility.PRIMARY,
                    primary_position=1,
                ).values_list(
                    "session__date",
                    "session__period_of_day",
                    "responsibility",
                    "primary_position",
                    "physician_id",
                )
            ),
            [
                (
                    self.week,
                    PreceptorSession.PeriodOfDay.AM,
                    PreceptorAssignment.Responsibility.PRIMARY,
                    1,
                    expected_primary["physician_id"],
                )
            ],
        )
        self.assertEqual(
            list(
                LateDocAssignment.objects.filter(date=self.week).values_list("date", "physician_id")
            ),
            [(self.week, alternative.pk)],
        )

    def test_browser_confirmation_enables_apply(self):
        proposal, _ = self.retained_proposal()

        self.browser(
            *self.sign_in_actions(),
            {"kind": "goto", "path": f"/solve/{proposal.pk}/"},
            {"kind": "click", "selector": 'button:has-text("Confirm")'},
            {"kind": "text", "value": "Confirmed"},
            {"kind": "disabled", "selector": 'form[action$="/apply/"] button', "value": False},
        )

    def test_browser_applies_a_checkpoint_with_an_explicit_selected_exception(self):
        unavailable = self.faculty("Unavailable", availability="off")
        proposal, _ = self.retained_proposal()
        assignment = proposal.proposed_assignments["precepting"][0]
        edit_proposed_assignment(
            proposal=proposal,
            assignment_type="precepting",
            on_date=date.fromisoformat(assignment["date"]),
            period_of_day=assignment["period_of_day"],
            position=assignment["position"],
            physician=unavailable,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
        )
        proposal.refresh_from_db()
        review = proposal.late_doc_reviews.get()
        review_proposed_late_doc(
            proposal=proposal,
            on_date=review.date,
            decision="confirmed",
            replacement_physician=None,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
        )

        self.browser(
            *self.sign_in_actions(),
            {"kind": "goto", "path": f"/solve/{proposal.pk}/"},
            {
                "kind": "text",
                "value": "Apply records a useful checkpoint; it does not resolve those findings.",
            },
            {
                "kind": "click",
                "selector": (
                    'form[action$="/apply/"] button:text-matches("before applying$", "i")'
                ),
            },
            {"kind": "text", "value": "Applying is a checkpoint"},
            {"kind": "text", "value": "Acknowledge as an exception"},
            {
                "kind": "checked",
                "selector": (
                    'fieldset:has(legend:has-text("Proposed hard finding")) '
                    'input[name="acknowledge_finding"]'
                ),
                "value": False,
            },
            {
                "kind": "fill",
                "selector": 'textarea[name="reason"]',
                "value": "External coverage is unavailable.",
            },
            {
                "kind": "check",
                "selector": (
                    'fieldset:has(legend:has-text("Proposed hard finding")) '
                    'input[name="acknowledge_finding"]'
                ),
            },
            {"kind": "click", "selector": 'button:has-text("Apply checkpoint")'},
            {"kind": "url", "value": "?month=2027-02"},
        )
        self.assertEqual(FindingAcknowledgement.objects.count(), 1)

    def test_browser_stale_apply_route_refuses_mutation(self):
        proposal, _ = self.retained_proposal()
        review = proposal.late_doc_reviews.get()
        review_proposed_late_doc(
            proposal=proposal,
            on_date=review.date,
            decision="confirmed",
            replacement_physician=None,
            scheduler=self.scheduler,
            expected_revision=proposal.revision,
        )
        session = next(
            session for session in open_weekly_draft(self.week) if session.period_of_day == "AM"
        )
        assign_preceptor(
            session=session,
            physician=self.faculty("Changed"),
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        before_apply = self.draft_state()

        self.browser(
            *self.sign_in_actions(),
            {"kind": "goto", "path": f"/solve/{proposal.pk}/"},
            {"kind": "text", "value": "stale"},
            {"kind": "submit", "selector": 'form[action$="/apply/"]'},
            {"kind": "text", "value": "stale"},
        )
        self.assertEqual(self.draft_state(), before_apply)

    def test_browser_month_renders_shift_finding_tooltips_and_opens_exact_assignment(self):
        self.ready_range(self.week, self.week.replace(day=5))
        unavailable = self.faculty("Unavailable faculty", availability="off")
        backup_only = self.faculty("Backup-only faculty", availability="backup_only")
        session = PreceptorSession.objects.get(date=self.week, period_of_day="AM")
        assign_preceptor(
            session=session,
            physician=unavailable,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        InterviewShift.objects.create(date=self.week, physician=unavailable)
        backup_session = PreceptorSession.objects.get(
            date=self.week + timedelta(days=1), period_of_day="AM"
        )
        assign_preceptor(
            session=backup_session,
            physician=backup_only,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )

        hard_slot = f'.assignment-slot-link[href^="/sessions/{session.pk}/assignments/primary-1/"]'
        hard_tooltip = f"{hard_slot} .calendar-finding-tooltip"
        preference_slot = (
            f'.assignment-slot-link[href^="/sessions/{backup_session.pk}/assignments/primary-1/"]'
        )
        preference_tooltip = f"{preference_slot} .calendar-finding-tooltip"
        hard_marker = f"{hard_slot} .calendar-finding-indicator--hard"
        preference_marker = f"{preference_slot} .calendar-finding-indicator--preference"

        self.browser(
            *self.sign_in_actions(),
            {"kind": "goto", "path": "/?month=2027-02"},
            {"kind": "text", "selector": hard_slot, "value": unavailable.display_name},
            {"kind": "visible", "selector": ".calendar-surface--assignment-month"},
            {
                "kind": "text",
                "selector": '.calendar-lattice-date-row time[datetime="2027-02-01"]',
                "value": "Feb 1",
            },
            {
                "kind": "hidden",
                "selector": ".calendar-week-publication-status",
            },
            {
                "kind": "hidden",
                "selector": 'form[action="/week/publish/"]',
            },
            {"kind": "hidden", "selector": 'h2:has-text("Schedule status")'},
            {
                "kind": "visible",
                "selector": hard_marker,
            },
            {
                "kind": "visible",
                "selector": preference_marker,
            },
            {
                "kind": "right-aligned",
                "selector": hard_marker,
                "container": ".assignment-slot-link",
            },
            {
                "kind": "right-aligned",
                "selector": preference_marker,
                "container": ".assignment-slot-link",
            },
            {"kind": "hover", "selector": hard_slot},
            {
                "kind": "hidden",
                "selector": hard_tooltip,
            },
            {"kind": "wait-visible", "selector": hard_tooltip},
            {
                "kind": "visible",
                "selector": hard_tooltip,
            },
            {
                "kind": "painted",
                "selector": hard_tooltip,
            },
            {
                "kind": "summary-lines",
                "selector": hard_tooltip,
                "values": ["Unavailable", "Interview conflict"],
            },
            {"kind": "hover", "selector": preference_slot},
            {
                "kind": "hidden",
                "selector": preference_tooltip,
            },
            {"kind": "wait-visible", "selector": preference_tooltip},
            {
                "kind": "visible",
                "selector": preference_tooltip,
            },
            {
                "kind": "painted",
                "selector": preference_tooltip,
            },
            {
                "kind": "summary-lines",
                "selector": preference_tooltip,
                "values": ["Backup only"],
            },
            {"kind": "focus", "selector": hard_slot},
            {
                "kind": "visible",
                "selector": hard_tooltip,
            },
            {
                "kind": "visible",
                "selector": '[aria-label="Assignment finding legend"]',
            },
            {
                "kind": "click",
                "selector": hard_slot,
            },
            {"kind": "url", "value": f"/sessions/{session.pk}/assignments/primary-1/"},
            {"kind": "text", "value": "Assigned physician is not available for this session."},
        )

    def test_browser_month_reveals_findings_and_opens_the_slot_editor(self):
        self.ready_range(self.week, self.week.replace(day=5))
        unavailable = self.faculty("Unavailable faculty", availability="off")
        backup_only = self.faculty("Backup-only faculty", availability="backup_only")
        session = PreceptorSession.objects.get(date=self.week, period_of_day="AM")
        assign_preceptor(
            session=session,
            physician=unavailable,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )
        InterviewShift.objects.create(date=self.week, physician=unavailable)
        backup_session = PreceptorSession.objects.get(
            date=self.week + timedelta(days=1), period_of_day="AM"
        )
        assign_preceptor(
            session=backup_session,
            physician=backup_only,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )

        hard_slot = f".month-slot:has-text({json.dumps(unavailable.display_name)})"
        preference_slot = f".month-slot:has-text({json.dumps(backup_only.display_name)})"
        hard_marker = f"{hard_slot} .calendar-finding-indicator--hard"
        preference_marker = f"{preference_slot} .calendar-finding-indicator--preference"
        hard_tooltip = f"{hard_marker} .calendar-finding-tooltip"

        self.browser(
            *self.sign_in_actions(),
            {"kind": "goto", "path": "/?month=2027-02"},
            {"kind": "viewport", "width": 1280, "height": 800},
            {"kind": "visible", "selector": hard_marker},
            {"kind": "visible", "selector": preference_marker},
            {"kind": "right-aligned", "selector": hard_marker, "container": ".month-slot"},
            {
                "kind": "right-aligned",
                "selector": preference_marker,
                "container": ".month-slot",
            },
            {"kind": "scroll", "selector": hard_slot},
            {"kind": "hover", "selector": hard_slot, "position": {"x": 6, "y": 6}},
            {"kind": "hidden", "selector": hard_tooltip},
            {"kind": "wait-visible", "selector": hard_tooltip},
            {"kind": "visible", "selector": hard_tooltip},
            {"kind": "painted", "selector": hard_tooltip},
            {
                "kind": "summary-lines",
                "selector": hard_tooltip,
                "values": ["Unavailable", "Interview conflict"],
            },
            {"kind": "focus", "selector": f"{hard_slot} a"},
            {"kind": "visible", "selector": hard_tooltip},
            {
                "kind": "text",
                "selector": '[aria-label="Assignment finding legend"]',
                "value": "Hard finding",
            },
            {
                "kind": "text",
                "selector": '[aria-label="Assignment finding legend"]',
                "value": "Preference finding",
            },
            {
                "kind": "legend-readable",
                "selector": '[aria-label="Assignment finding legend"]',
            },
            {"kind": "viewport", "width": 320, "height": 800},
            {
                "kind": "legend-readable",
                "selector": '[aria-label="Assignment finding legend"]',
            },
            {"kind": "scroll", "selector": hard_slot},
            {"kind": "focus", "selector": f"{hard_slot} a"},
            {"kind": "visible", "selector": hard_tooltip},
            {"kind": "painted", "selector": hard_tooltip},
            {"kind": "press", "selector": f"{hard_slot} a", "key": "Enter"},
            {
                "kind": "url-equals",
                "path": f"/sessions/{session.pk}/assignments/primary-1/?month=2027-02",
            },
            {
                "kind": "text",
                "selector": ".assignment-findings",
                "value": "Assigned physician is not available",
            },
            {"kind": "click", "selector": ".form-return-link"},
            {"kind": "url-equals", "path": "/?month=2027-02"},
        )

    def test_month_edit_time_fits_calendar_controls_at_desktop_and_mobile_widths(self):
        SchedulePeriod.objects.create(
            month=self.week,
            last_edited_at=datetime(2027, 2, 1, 18, 35, tzinfo=UTC),
        )
        actions = list(self.sign_in_actions())
        actions.append({"kind": "goto", "path": "/?month=2027-02"})
        for width in (1280, 320):
            actions.extend(
                [
                    {"kind": "viewport", "width": width, "height": 800},
                    {"kind": "visible", "selector": "#month-last-edit time"},
                    {"kind": "month-edit-readable"},
                    {"kind": "month-geometry"},
                ]
            )
        self.browser(*actions)

    def test_browser_redirects_anonymous_scheduler_to_sign_in(self):
        self.browser(
            {"kind": "goto", "path": "/solve/"},
            {"kind": "url", "value": "/sign-in/?next=/solve/"},
            {"kind": "text", "value": "Sign in"},
        )

    def test_month_adjacent_slots_open_actual_session_and_return_after_save_and_removal(self):
        monday = date(2027, 3, 29)
        self.ready_range(monday, monday)
        physician = self.faculty("Adjacent physician")
        session = PreceptorSession.objects.get(date=monday, period_of_day="PM")
        origin = "/?month=2027-04"
        editor = f"/sessions/{session.pk}/assignments/primary-2/?month=2027-04"
        secondary = f"/sessions/{session.pk}/assignments/secondary/?month=2027-04"
        slot = f'.assignment-slot-link[href="{editor}"]'
        self.browser(
            *self.sign_in_actions(),
            {"kind": "goto", "path": origin},
            {"kind": "month-geometry"},
            {"kind": "focus", "selector": slot},
            {"kind": "press", "selector": slot, "key": "Enter"},
            {"kind": "url-equals", "path": editor},
            {"kind": "text", "selector": ".form-page-title", "value": "Preceptor 2"},
            {"kind": "text", "selector": ".form-page-intro", "value": "March 29, 2027 · PM"},
            {"kind": "select", "selector": "#id_physician", "value": physician.pk},
            {"kind": "check", "selector": "#id_is_locked"},
            {"kind": "click", "selector": 'button:has-text("Save assignment")'},
            {"kind": "url-equals", "path": origin},
            {"kind": "text", "selector": slot, "value": physician.display_name},
            {"kind": "viewport", "width": 320, "height": 800},
            {"kind": "month-geometry"},
            {"kind": "click", "selector": f'.assignment-slot-link[href="{secondary}"]'},
            {"kind": "text", "selector": ".form-page-title", "value": "Secondary"},
            {"kind": "click", "selector": ".form-return-link"},
            {"kind": "url-equals", "path": origin},
            {"kind": "click", "selector": slot},
            {"kind": "checked", "selector": "#id_is_locked", "value": True},
            {"kind": "click", "selector": 'button:has-text("Remove assignment")'},
            {"kind": "url-equals", "path": origin},
        )
        self.assertFalse(PreceptorAssignment.objects.filter(session=session).exists())

    def test_month_adjacent_late_doc_returns_after_save_history_and_removal(self):
        monday = date(2027, 3, 29)
        self.ready_range(monday, monday)
        physician = self.faculty("Adjacent Late Doc")
        origin = "/?month=2027-04"
        editor = reverse("late-doc-edit", args=(monday.isoformat(),)) + "?month=2027-04"
        slot = f'.assignment-slot-link[href="{editor}"]'
        self.browser(
            *self.sign_in_actions(),
            {"kind": "goto", "path": origin},
            {"kind": "viewport", "width": 320, "height": 800},
            {"kind": "focus", "selector": slot},
            {"kind": "press", "selector": slot, "key": "Enter"},
            {"kind": "url-equals", "path": editor},
            {"kind": "text", "selector": ".form-page-title", "value": "March 29, 2027"},
            {"kind": "select", "selector": "#id_physician", "value": physician.pk},
            {"kind": "click", "selector": 'button:has-text("Save Late Doc")'},
            {"kind": "url-equals", "path": origin},
            {"kind": "text", "selector": slot, "value": physician.display_name},
            {"kind": "click", "selector": slot},
            {"kind": "click", "selector": 'a:has-text("View Late Doc history")'},
            {"kind": "click", "selector": ".form-return-link"},
            {"kind": "url-equals", "path": origin},
            {"kind": "click", "selector": slot},
            {"kind": "click", "selector": 'button:has-text("Remove Late Doc")'},
            {"kind": "url-equals", "path": origin},
        )
        self.assertFalse(LateDocAssignment.objects.filter(date=monday).exists())
