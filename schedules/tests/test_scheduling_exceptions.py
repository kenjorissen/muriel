from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from schedules.models import (
    EffectiveRole,
    FindingAcknowledgement,
    Physician,
    PreceptorAssignment,
    PreceptorSession,
)
from schedules.precepting_presentation import marker_for_findings, precepting_finding_targets
from schedules.services.finding_acknowledgements import acknowledge_finding, finding_identity
from schedules.services.preceptors import (
    DraftFinding,
    DraftFindingResult,
    assign_preceptor,
    draft_findings_for_session,
    open_monthly_draft,
)
from schedules.tests.test_work_remaining import _action_matches, _RenderedSurfaceParser

_FIXTURE_TODAY = date(2026, 9, 8)


def finding_section(response, label):
    """Return the one displayed finding with this canonical compact label."""
    parser = _RenderedSurfaceParser()
    parser.feed(response.content.decode())
    sections = [
        element
        for element in parser.elements
        if element.tag == "section"
        and "finding" in element.attributes.get("class", "").split()
        and label.casefold() in element.text().casefold()
    ]
    if len(sections) != 1:
        raise AssertionError(f"Expected one displayed {label!r} finding, found {len(sections)}.")
    return sections[0]


def has_current_findings_ancestor(element):
    return any(
        ancestor.tag == "section"
        and "current-findings-heading" in ancestor.attributes.get("aria-labelledby", "").split()
        for ancestor in element.ancestors()
    )


def calendar_week(response, on_date):
    """Return the rendered week row group that contains ``on_date``."""
    parser = _RenderedSurfaceParser()
    parser.feed(response.content.decode())
    date_headers = [
        element
        for element in parser.elements
        if element.tag == "time" and element.attributes.get("datetime") == on_date.isoformat()
    ]
    if len(date_headers) != 1:
        raise AssertionError(
            f"Expected one rendered date header for {on_date}, found {len(date_headers)}."
        )
    owner = next(
        (
            ancestor
            for ancestor in date_headers[0].ancestors()
            if ancestor.tag == "section" and ancestor.attributes.get("role") == "rowgroup"
        ),
        None,
    )
    if owner is None:
        raise AssertionError(f"Expected a calendar week for {on_date}.")
    return owner


def calendar_date_gutter(response, on_date):
    """Return the one date-row gutter for ``on_date``'s calendar week."""
    gutters = [
        element
        for element in calendar_week(response, on_date).descendants()
        if "calendar-lattice-date-gutter" in element.attributes.get("class", "").split()
    ]
    if len(gutters) != 1:
        raise AssertionError(f"Expected one date-row gutter for {on_date}, found {len(gutters)}.")
    return gutters[0]


def calendar_finding_marker(response, *, on_date, condition, severity, action=None):
    """Return the accessible session-finding state in ``on_date``'s calendar week."""
    owner = calendar_week(response, on_date)
    indicator_class = f"calendar-finding-indicator--{severity}"
    if action is None:
        markers = [
            element
            for element in owner.descendants()
            if element.attributes.get("role") == "img"
            and condition.casefold() in element.attributes.get("aria-label", "").casefold()
            and "calendar-finding-indicator" in element.attributes.get("class", "").split()
            and indicator_class in element.attributes.get("class", "").split()
        ]
    else:
        markers = [
            element
            for element in owner.descendants()
            if element.tag == "a"
            and condition.casefold() in element.attributes.get("aria-label", "").casefold()
            and _action_matches(element.attributes.get("href", ""), action)
            and (
                indicator_class in element.attributes.get("class", "").split()
                or any(
                    "calendar-finding-indicator" in descendant.attributes.get("class", "").split()
                    and indicator_class in descendant.attributes.get("class", "").split()
                    and descendant.attributes.get("aria-hidden") == "true"
                    for descendant in element.descendants()
                )
            )
        ]
    if len(markers) != 1:
        raise AssertionError(
            f"Expected one accessible {severity} finding marker for {condition!r} on {on_date}, "
            f"found {len(markers)}."
        )
    return markers[0]


class SchedulingExceptionsViewsTests(TestCase):
    def setUp(self):
        self.operational_today = patch(
            "schedules.services.operational_attention.organization_today",
            return_value=_FIXTURE_TODAY,
        )
        self.work_remaining_today = patch(
            "schedules.services.work_remaining.organization_today",
            return_value=_FIXTURE_TODAY,
        )
        self.preceptors_view_today = patch(
            "schedules.views.preceptors.organization_today",
            return_value=_FIXTURE_TODAY,
        )
        self.operational_today.start()
        self.work_remaining_today.start()
        self.preceptors_view_today.start()
        self.addCleanup(self.operational_today.stop)
        self.addCleanup(self.work_remaining_today.stop)
        self.addCleanup(self.preceptors_view_today.stop)
        self.scheduler = get_user_model().objects.create_user(username="exception-scheduler")
        self.client.force_login(self.scheduler)
        self.session = open_monthly_draft(date(2026, 9, 1)).sessions.get(
            date=date(2026, 9, 8), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        self.editor_url = reverse("session-assignment-edit", args=(self.session.pk, "primary-1"))

    def test_scheduler_acknowledges_a_live_finding_and_retains_its_history(self):
        # October previews this September session; exception actions retain that origin.
        self.session = open_monthly_draft(date(2026, 9, 1)).sessions.get(
            date=date(2026, 9, 30), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        self.editor_url = reverse("session-assignment-edit", args=(self.session.pk, "primary-1"))
        editor = self.client.get(self.editor_url + "?month=2026-10")
        parser = _RenderedSurfaceParser()
        parser.feed(editor.content.decode())

        acknowledgement_url = reverse("session-finding-acknowledge", args=(self.session.pk,))
        self.assertContains(editor, f'action="{acknowledgement_url}?month=2026-10"')
        self.assertContains(editor, "Current findings", count=1)
        self.assertNotContains(editor, "Findings and exceptions")
        self.assertContains(editor, "Unacknowledged hard finding")
        self.assertContains(editor, "Acknowledge exception")
        finding_sections = [
            element
            for element in parser.elements
            if element.tag == "section"
            and "current-findings-heading" in element.attributes.get("aria-labelledby", "").split()
        ]
        self.assertEqual(len(finding_sections), 1)
        self.assertIn("Reason for this exception", finding_sections[0].text())
        self.assertTrue(
            any(
                element.tag == "button" and element.text().strip() == "Acknowledge exception"
                for element in finding_sections[0].descendants()
            )
        )

        response = self.client.post(
            reverse("session-finding-acknowledge", args=(self.session.pk,)) + "?month=2026-10",
            {
                "code": "missing_primary_position",
                "position": "primary-1",
                "editor_position": "primary-1",
                "reason": "External coverage is unavailable.",
            },
        )

        self.assertRedirects(response, self.editor_url + "?month=2026-10")
        acknowledgement = FindingAcknowledgement.objects.get()
        self.assertEqual(acknowledgement.reason, "External coverage is unavailable.")
        acknowledged_editor = self.client.get(self.editor_url + "?month=2026-10")
        self.assertEqual(
            acknowledged_editor.context["return_url"],
            reverse("monthly-workspace") + "?month=2026-10",
        )
        self.assertContains(acknowledged_editor, "Acknowledged exception")
        self.assertContains(acknowledged_editor, "Acknowledged by exception-scheduler")
        self.assertNotContains(acknowledged_editor, "assignment-findings-disclosure")

        week = self.client.get(reverse("monthly-workspace"), {"month": "2026-09"})
        calendar_finding_marker(
            week,
            on_date=self.session.date,
            condition="Unfilled",
            severity="exception",
            action=self.editor_url,
        )

        physician = Physician.objects.create(display_name="Replacement faculty")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2026, 1, 1),
        )
        assign_preceptor(
            session=self.session,
            physician=physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )

        history = self.client.get(reverse("scheduling-exceptions"))
        self.assertContains(history, "No longer applies")
        self.assertContains(history, "External coverage is unavailable.")
        self.assertContains(history, "Preceptor Schedule")
        self.assertNotContains(history, "fingerprint")
        self.assertNotContains(history, "stale")

    def test_scheduler_acknowledges_a_current_week_owned_finding_from_a_session_editor(self):
        editor = self.client.get(self.editor_url)

        self.assertContains(editor, "No normal rounder is recorded")
        self.assertNotContains(editor, "Schedule context:")
        self.assertNotContains(editor, "Relevant sources:")
        calendar = self.client.get(reverse("monthly-workspace"), {"month": "2026-09"})
        self.assertEqual(calendar_date_gutter(calendar, date(2026, 9, 7)).text().strip(), "")
        response = self.client.post(
            reverse("session-finding-acknowledge", args=(self.session.pk,)),
            {
                "code": "missing_normal_rounder",
                "scope": "week",
                "editor_position": "primary-1",
                "reason": "No faculty member is available to round this week.",
            },
        )

        self.assertRedirects(response, self.editor_url)
        acknowledgement = FindingAcknowledgement.objects.get()
        self.assertEqual(acknowledgement.identity["owner"]["kind"], "preceptor_week")
        acknowledged_editor = self.client.get(self.editor_url)
        self.assertContains(acknowledged_editor, "Acknowledged exception")
        self.assertContains(
            acknowledged_editor, "No faculty member is available to round this week."
        )
        calendar = self.client.get(reverse("monthly-workspace"), {"month": "2026-09"})
        self.assertEqual(calendar_date_gutter(calendar, date(2026, 9, 7)).text().strip(), "")

        listing = self.client.get(reverse("preceptor-list"), {"month": "2026-09"})
        parser = _RenderedSurfaceParser()
        parser.feed(listing.content.decode())
        listed_session = [
            row
            for row in parser.elements
            if row.tag == "tr"
            and any(
                element.tag == "time"
                and element.attributes.get("datetime") == self.session.date.isoformat()
                for element in row.descendants()
            )
            and "AM" in row.text()
            and "No normal rounder is recorded" in row.text()
            and any(
                element.tag == "a"
                and _action_matches(element.attributes.get("href", ""), self.editor_url)
                for element in row.descendants()
            )
        ]
        self.assertEqual(len(listed_session), 1)

    def test_scheduler_removes_a_current_override_and_history_retains_both_actors(self):
        # October previews this September session; exception actions retain that origin.
        self.session = open_monthly_draft(date(2026, 9, 1)).sessions.get(
            date=date(2026, 9, 30), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        self.editor_url = reverse("session-assignment-edit", args=(self.session.pk, "primary-1"))
        acknowledgement = acknowledge_finding(
            finding=next(
                finding
                for finding in draft_findings_for_session(self.session).findings
                if finding.code == "missing_primary_position"
                and finding.explanation["position"] == "primary-1"
            ),
            scheduler=self.scheduler,
            reason="External coverage is unavailable.",
        )

        editor = self.client.get(self.editor_url + "?month=2026-10")
        self.assertContains(editor, "Remove override")

        removal_url = (
            reverse("session-finding-override-remove", args=(self.session.pk,)) + "?month=2026-10"
        )
        self.assertContains(editor, f'action="{removal_url}"')
        removal_data = {
            "acknowledgement_id": acknowledgement.pk,
            "code": "missing_primary_position",
            "position": "primary-1",
            "scope": "session",
            "editor_position": "primary-1",
        }
        response = self.client.post(removal_url, removal_data, follow=True)

        self.assertRedirects(response, self.editor_url + "?month=2026-10")
        self.assertEqual(
            response.context["return_url"], reverse("monthly-workspace") + "?month=2026-10"
        )
        acknowledgement.refresh_from_db()
        self.assertFalse(acknowledgement.is_current)
        self.assertEqual(acknowledgement.acknowledged_by, self.scheduler)
        self.assertEqual(acknowledgement.removed_by, self.scheduler)
        self.assertIsNotNone(acknowledgement.removed_at)

        removed_editor = response
        self.assertContains(removed_editor, "Override removed.")
        self.assertContains(removed_editor, "Unacknowledged hard finding")
        self.assertNotContains(removed_editor, "Remove override")
        week = self.client.get(reverse("monthly-workspace"), {"month": "2026-09"})
        calendar_finding_marker(
            week,
            on_date=self.session.date,
            condition="Unfilled",
            severity="hard",
            action=self.editor_url,
        )
        self.assertNotContains(week, "Preceptor 1: Unfilled — overridden")

        history = self.client.get(reverse("scheduling-exceptions"))
        self.assertContains(history, "Acknowledged by exception-scheduler")
        self.assertContains(history, "Override removed.")
        self.assertContains(history, "Removed by exception-scheduler")

        repeated = self.client.post(removal_url, removal_data, follow=True)
        self.assertContains(repeated, "That override is no longer current and cannot be removed.")
        acknowledgement.refresh_from_db()
        self.assertEqual(acknowledgement.removed_by, self.scheduler)

    @patch(
        "schedules.views.preceptors.draft_findings_for_week",
        return_value=DraftFindingResult(()),
    )
    @patch(
        "schedules.views.preceptors.operational_draft_findings_for_session",
        return_value=DraftFindingResult(()),
    )
    def test_editor_omits_current_findings_section_when_clean(
        self, _session_findings, _week_findings
    ):
        editor = self.client.get(self.editor_url)

        self.assertNotContains(editor, "Current findings")

    def test_current_findings_section_follows_the_current_assignment_summary(self):
        physician = Physician.objects.create(display_name="Current assignment faculty")
        EffectiveRole.objects.create(
            physician=physician,
            role=EffectiveRole.Role.FACULTY,
            effective_start=date(2026, 1, 1),
        )
        assign_preceptor(
            session=self.session,
            physician=physician,
            responsibility=PreceptorAssignment.Responsibility.PRIMARY,
            primary_position=1,
            scheduler=self.scheduler,
        )

        editor_content = self.client.get(self.editor_url).content.decode()

        self.assertLess(
            editor_content.index('id="current-assignment-heading"'),
            editor_content.index('id="current-findings-heading"'),
        )

    @patch(
        "schedules.views.preceptors.draft_findings_for_week",
        return_value=DraftFindingResult(()),
    )
    @patch("schedules.views.preceptors.is_operational_preceptor_week", return_value=True)
    def test_editor_shows_a_preference_without_exception_controls(
        self, _operational, _week_findings
    ):
        preference = DraftFinding(
            "backup_only_assignment",
            "preference",
            self.session,
            self.session.date,
            (),
            {"position": "primary-1"},
        )
        with patch(
            "schedules.views.preceptors.operational_draft_findings_for_session",
            return_value=DraftFindingResult((preference,)),
        ):
            editor = self.client.get(self.editor_url)

        parser = _RenderedSurfaceParser()
        parser.feed(editor.content.decode())
        preference_sections = [
            element
            for element in parser.elements
            if element.tag == "section"
            and "finding-preference" in element.attributes.get("class", "").split()
            and "backup only" in element.text().casefold()
        ]
        self.assertEqual(len(preference_sections), 1)
        self.assertTrue(has_current_findings_ancestor(preference_sections[0]))
        self.assertFalse(
            any(element.tag == "form" for element in preference_sections[0].descendants())
        )

    @patch(
        "schedules.views.preceptors.draft_findings_for_week",
        return_value=DraftFindingResult(()),
    )
    @patch("schedules.views.preceptors.is_operational_preceptor_week", return_value=True)
    def test_editor_keeps_unavailable_assignment_diagnosis_visible_once_in_section(
        self, _operational, _week_findings
    ):
        unavailable = DraftFinding(
            "not_available_assignment",
            "hard",
            self.session,
            self.session.date,
            (),
            {"position": "primary-1"},
        )
        with patch(
            "schedules.views.preceptors.operational_draft_findings_for_session",
            return_value=DraftFindingResult((unavailable,)),
        ):
            editor = self.client.get(self.editor_url)

        unavailable_section = finding_section(editor, "not available")
        self.assertIn("finding-hard", unavailable_section.attributes.get("class", "").split())
        self.assertIn("not available", unavailable_section.text().casefold())
        acknowledgement_form = next(
            element for element in unavailable_section.descendants() if element.tag == "form"
        )
        self.assertTrue(
            _action_matches(
                acknowledgement_form.attributes.get("action", ""),
                reverse("session-finding-acknowledge", args=(self.session.pk,)),
            )
        )
        self.assertTrue(
            any(
                element.attributes.get("name") == "position"
                and element.attributes.get("value") == "primary-1"
                for element in acknowledgement_form.descendants()
            )
        )

    @patch("schedules.views.preceptors.is_operational_preceptor_week", return_value=True)
    def test_editor_presents_mixed_findings_once_with_their_controls(self, _operational):
        hard_finding = DraftFinding(
            "missing_primary_position",
            "hard",
            self.session,
            self.session.date,
            (),
            {"position": "primary-1"},
        )
        preference = DraftFinding(
            "backup_only_assignment",
            "preference",
            self.session,
            self.session.date,
            (),
            {"position": "primary-1"},
        )
        week_finding = DraftFinding(
            "missing_normal_rounder",
            "hard",
            None,
            date(2026, 9, 7),
            (),
            {},
        )
        with (
            patch(
                "schedules.views.preceptors.operational_draft_findings_for_session",
                return_value=DraftFindingResult((hard_finding, hard_finding, preference)),
            ),
            patch(
                "schedules.views.preceptors.draft_findings_for_week",
                return_value=DraftFindingResult((week_finding,)),
            ),
        ):
            editor = self.client.get(self.editor_url)

        parser = _RenderedSurfaceParser()
        parser.feed(editor.content.decode())
        session_section = finding_section(editor, "unfilled")
        week_section = finding_section(editor, "no normal rounder")
        self.assertIn("unfilled", session_section.text().casefold())
        self.assertRegex(session_section.text(), r"\bPreceptor 1\b")
        self.assertIn("no normal rounder", week_section.text().casefold())
        self.assertIn("finding-hard", session_section.attributes.get("class", "").split())
        self.assertIn("finding-hard", week_section.attributes.get("class", "").split())
        for section, expected_scope, expected_position, expected_code in (
            (session_section, "session", "primary-1", "missing_primary_position"),
            (week_section, "week", "", "missing_normal_rounder"),
        ):
            form = next(element for element in section.descendants() if element.tag == "form")
            self.assertTrue(
                _action_matches(
                    form.attributes.get("action", ""),
                    reverse("session-finding-acknowledge", args=(self.session.pk,)),
                )
            )
            values = {
                element.attributes.get("name"): element.attributes.get("value", "")
                for element in form.descendants()
                if element.tag == "input"
            }
            self.assertEqual(values.get("scope"), expected_scope)
            self.assertEqual(values.get("position"), expected_position)
            self.assertEqual(values.get("code"), expected_code)
        preference_sections = [
            element
            for element in parser.elements
            if element.tag == "section"
            and "finding-preference" in element.attributes.get("class", "").split()
            and "backup only" in element.text().casefold()
        ]
        self.assertEqual(len(preference_sections), 1)
        self.assertTrue(has_current_findings_ancestor(preference_sections[0]))
        self.assertFalse(
            any(element.tag == "form" for element in preference_sections[0].descendants())
        )


class PreceptingFindingMarkerPresentationTests(TestCase):
    def setUp(self):
        self.session = open_monthly_draft(date(2026, 9, 1)).sessions.get(
            date=date(2026, 9, 8), period_of_day=PreceptorSession.PeriodOfDay.AM
        )
        self.unfilled = DraftFinding(
            "missing_primary_position",
            "hard",
            self.session,
            self.session.date,
            (),
            {"position": "primary-1"},
        )
        self.interview_conflict = DraftFinding(
            "interview_assignment_conflict",
            "hard",
            self.session,
            self.session.date,
            (),
            {"position": "primary-1"},
        )
        self.preference = DraftFinding(
            "backup_only_assignment",
            "preference",
            self.session,
            self.session.date,
            (),
            {"position": "primary-1"},
        )

    def test_compact_marker_omits_empty_targets_and_keeps_unacknowledged_labels_plain(self):
        self.assertEqual(
            precepting_finding_targets(
                SimpleNamespace(date=self.session.date, period_of_day=self.session.period_of_day),
                (),
            ),
            (),
        )

        marker = marker_for_findings((self.unfilled, self.preference), {})

        self.assertEqual(marker.severity, "hard")
        self.assertEqual(marker.labels, ("Unfilled", "Backup only"))
        self.assertNotIn("overridden", marker.summary)

    def test_compact_marker_marks_only_acknowledged_hard_labels_as_overridden(self):
        acknowledgements = {finding_identity(self.unfilled).key: object()}

        acknowledged = marker_for_findings((self.unfilled,), acknowledgements)
        mixed = marker_for_findings(
            (self.unfilled, self.interview_conflict, self.preference), acknowledgements
        )

        self.assertEqual(acknowledged.severity, "exception")
        # TEXT-CONTRACT: acknowledged-hard-finding-overridden-suffix —
        # Compact calendar popovers distinguish acknowledged hard findings with
        # the exact ` — overridden` suffix, including punctuation and whitespace.
        # Protects schedules/precepting_presentation.py; the preceding finding
        # label is not part of this text contract.
        self.assertEqual(acknowledged.labels, ("Unfilled — overridden",))
        self.assertEqual(
            mixed.labels,
            ("Unfilled — overridden", "Interview conflict", "Backup only"),
        )
        self.assertEqual(mixed.severity, "hard")
        self.assertIn("Unfilled — overridden", mixed.summary)
        self.assertNotIn("Interview conflict — overridden", mixed.summary)
        self.assertNotIn("Backup only — overridden", mixed.summary)
