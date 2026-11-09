import re
from datetime import UTC, date, datetime
from decimal import Decimal
from html.parser import HTMLParser
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch
from urllib.parse import parse_qs, urljoin, urlsplit

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from schedules.models import (
    ClinicHoliday,
    HolidayOccurrence,
    InterviewShift,
    LeadTimeSetting,
    Vacation,
)
from schedules.services.academic_events import create_academic_event_definition
from schedules.services.community import create_community_preceptor_rule
from schedules.services.finding_acknowledgements import acknowledge_finding
from schedules.services.interviews import (
    InterviewScheduleSummary,
    interview_findings_for_shift,
    interview_projected_work_items,
)
from schedules.services.lead_times import set_lead_time
from schedules.services.physicians import create_physician
from schedules.services.preceptors import (
    DraftFinding,
    DraftFindingResult,
    draft_findings_for_week,
    focused_draft_findings_for_range,
    open_monthly_draft,
    precepting_projected_work_items,
    preceptor_date_has_focused_membership,
    work_week_start,
)
from schedules.services.rounding import (
    create_casual_rounder_rule,
    rounding_week_has_focused_membership,
)
from schedules.services.stipend_rates import set_initial_stipend_rate
from schedules.services.work_remaining import (
    InclusiveDateRange,
    ProjectedWorkItem,
    ProjectedWorkReason,
    WorkRemainingRangeAction,
    WorkSummaryType,
    focused_range_has_membership,
    focused_work_remaining,
    global_work_remaining,
    project_actionable_work,
    scoped_work_remaining,
)
from schedules.views.interviews import _interview_scoped_work_remaining


class _RenderedElement:
    def __init__(self, tag, attributes, parent=None):
        self.tag = tag
        self.attributes = dict(attributes)
        self.parent = parent
        self.children = []

    def text(self):
        return "".join(child if isinstance(child, str) else child.text() for child in self.children)

    def ancestors(self):
        element = self.parent
        while element is not None:
            yield element
            element = element.parent

    def action_targets(self):
        targets = []
        if self.tag == "a" and "href" in self.attributes:
            targets.append(self.attributes["href"])
        if self.tag == "form" and "action" in self.attributes:
            targets.append(self.attributes["action"])
        for child in self.children:
            if isinstance(child, _RenderedElement):
                targets.extend(child.action_targets())
        return targets

    def descendants(self):
        for child in self.children:
            if isinstance(child, _RenderedElement):
                yield child
                yield from child.descendants()

    def submitted_values(self):
        """Return the successful GET controls for this narrow rendered form check."""
        values = {}
        for control in self.descendants():
            if "disabled" in control.attributes:
                continue
            name = control.attributes.get("name")
            if not name:
                continue
            if control.tag == "input":
                values[name] = control.attributes.get("value", "")
            elif control.tag == "select":
                options = [
                    option
                    for option in control.descendants()
                    if option.tag == "option" and "disabled" not in option.attributes
                ]
                selected = next(
                    (option for option in options if "selected" in option.attributes),
                    options[0] if options else None,
                )
                if selected is not None:
                    values[name] = selected.attributes.get("value", selected.text())
        return values

    def has_option(self, *, select_name, value, label):
        return any(
            select.attributes.get("name") == select_name
            and any(
                option.attributes.get("value") == value and label in option.text()
                for option in select.descendants()
                if option.tag == "option"
            )
            for select in self.descendants()
            if select.tag == "select"
        )


class _RenderedSurfaceParser(HTMLParser):
    _void_tags: ClassVar = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
    }

    def __init__(self):
        super().__init__()
        self.root = _RenderedElement("root", ())
        self.stack = [self.root]
        self.elements = []

    def handle_starttag(self, tag, attrs):
        element = _RenderedElement(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(element)
        self.elements.append(element)
        if tag not in self._void_tags:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def records_with(
        self,
        *,
        detail,
        action,
        record_tag,
        record_class=None,
        submitted_values=None,
        option=None,
    ):
        """Find rendered record elements that visibly describe their own action."""
        return [
            element
            for element in self.elements
            if element.tag == record_tag
            and (
                record_class is None or record_class in element.attributes.get("class", "").split()
            )
            and detail in element.text()
            and any(_action_matches(target, action) for target in element.action_targets())
            and (
                submitted_values is None
                or (
                    element.tag == "form"
                    and element.attributes.get("method", "get").lower() == "get"
                    and all(
                        element.submitted_values().get(name) == value
                        for name, value in submitted_values.items()
                    )
                )
            )
            and (
                option is None
                or (
                    element.tag == "form"
                    and element.has_option(
                        select_name=option["select_name"],
                        value=option["value"],
                        label=option["label"],
                    )
                )
            )
        ]


def _action_matches(target, expected):
    """Compare a rendered action's supported path and required query parameters."""
    request_origin = "http://testserver/"
    target_parts = urlsplit(urljoin(request_origin, target))
    expected_parts = urlsplit(urljoin(request_origin, expected))
    target_query = parse_qs(target_parts.query, keep_blank_values=True)
    expected_query = parse_qs(expected_parts.query, keep_blank_values=True)
    return (target_parts.scheme, target_parts.netloc, target_parts.path) == (
        expected_parts.scheme,
        expected_parts.netloc,
        expected_parts.path,
    ) and all(target_query.get(name) == values for name, values in expected_query.items())


class ScopedWorkRemainingTests(SimpleTestCase):
    def test_rendered_action_matching_normalizes_the_internal_origin_and_rejects_external(self):
        self.assertTrue(
            _action_matches(
                "http://testserver/work/?month=2031-10&view=list",
                "/work/?view=list&month=2031-10",
            )
        )
        self.assertFalse(
            _action_matches(
                "https://untrusted.example/work/?month=2031-10&view=list",
                "/work/?view=list&month=2031-10",
            )
        )

    def _item(self, key, start, end, classification, *, matters_on=None, label=None, quantity=1):
        return ProjectedWorkItem(
            key=key,
            area="test-area",
            label=label or key,
            affected_range=InclusiveDateRange(start, end),
            matters_on=matters_on or start,
            target_url=f"/work/{key}/",
            reasons=(ProjectedWorkReason(classification, f"{key} reason", quantity),),
        )

    def test_selected_range_uses_stable_required_first_order_without_matters_on_cutoff(self):
        selected_range = InclusiveDateRange(date(2026, 5, 1), date(2026, 5, 31))
        summary = scoped_work_remaining(
            (
                self._item(
                    "advisory",
                    date(2026, 5, 2),
                    date(2026, 5, 2),
                    "advisory",
                    label="Advisory work",
                ),
                self._item(
                    "later-required",
                    date(2026, 5, 20),
                    date(2026, 6, 2),
                    "required",
                    label="Later required work",
                ),
                self._item(
                    "earlier-required",
                    date(2026, 4, 30),
                    date(2026, 5, 3),
                    "required",
                    matters_on=date(2026, 9, 1),
                    label="Earlier required work",
                ),
            ),
            selected_range,
            evaluated=True,
        )

        self.assertEqual(
            [item.key for item in summary.items],
            ["earlier-required", "later-required", "advisory"],
        )
        self.assertEqual(summary.required_item_count, 2)
        self.assertEqual(summary.advisory_item_count, 1)
        self.assertEqual(summary.tone, "danger")

    def test_current_attention_policy_includes_the_attention_and_historical_boundaries(self):
        item = self._item(
            "cross-month",
            date(2026, 3, 31),
            date(2026, 4, 1),
            "required",
            matters_on=date(2026, 3, 31),
        )

        for as_of, expected_keys in (
            (date(2026, 3, 30), []),
            (date(2026, 3, 31), ["cross-month"]),
            (date(2026, 4, 1), ["cross-month"]),
            (date(2026, 4, 2), []),
        ):
            with self.subTest(as_of=as_of):
                projection = project_actionable_work(
                    (item,),
                    None,
                    as_of=as_of,
                    policy="current-attention",
                )
                self.assertEqual([projected.key for projected in projection.items], expected_keys)

    def test_current_attention_policy_handles_quarter_and_academic_year_crossings(self):
        quarter_item = self._item(
            "quarter",
            date(2026, 3, 31),
            date(2026, 4, 1),
            "required",
            matters_on=date(2026, 3, 31),
        )
        academic_year_item = self._item(
            "academic-year",
            date(2026, 6, 30),
            date(2026, 7, 1),
            "advisory",
            matters_on=date(2026, 6, 30),
        )

        for as_of, expected_keys in (
            (date(2026, 3, 30), []),
            (date(2026, 3, 31), ["quarter"]),
            (date(2026, 4, 2), []),
            (date(2026, 6, 29), []),
            (date(2026, 6, 30), ["academic-year"]),
            (date(2026, 7, 2), []),
        ):
            with self.subTest(as_of=as_of):
                projection = project_actionable_work(
                    (quarter_item, academic_year_item),
                    None,
                    as_of=as_of,
                    policy="current-attention",
                )
                self.assertEqual([projected.key for projected in projection.items], expected_keys)

    def test_focused_range_includes_far_future_work_before_attention(self):
        item = self._item(
            "far-future",
            date(2031, 7, 1),
            date(2031, 7, 1),
            "required",
            matters_on=date(2031, 1, 1),
        )
        selected_range = InclusiveDateRange(date(2031, 7, 1), date(2031, 7, 31))

        focused = project_actionable_work(
            (item,), selected_range, as_of=date(2026, 7, 1), policy="focused-range"
        )
        attention = project_actionable_work(
            (item,), selected_range, as_of=date(2026, 7, 1), policy="current-attention"
        )

        self.assertEqual(focused.items, (item,))
        self.assertEqual(attention.items, ())

    def test_focused_range_requires_an_explicit_selection(self):
        with self.assertRaisesRegex(ValueError, "explicit selected range"):
            project_actionable_work((), None, as_of=date(2026, 7, 1), policy="focused-range")

    def test_focused_range_ignores_attention_boundary(self):
        item = self._item(
            "attention-boundary",
            date(2026, 8, 1),
            date(2026, 8, 1),
            "required",
            matters_on=date(2026, 7, 15),
        )
        selected_range = InclusiveDateRange(date(2026, 8, 1), date(2026, 8, 1))

        for as_of, expected_attention_keys in (
            (date(2026, 7, 14), []),
            (date(2026, 7, 15), ["attention-boundary"]),
            (date(2026, 7, 16), ["attention-boundary"]),
        ):
            with self.subTest(as_of=as_of):
                focused = project_actionable_work(
                    (item,), selected_range, as_of=as_of, policy="focused-range"
                )
                attention = project_actionable_work(
                    (item,), selected_range, as_of=as_of, policy="current-attention"
                )

                self.assertEqual(focused.items, (item,))
                self.assertEqual(
                    [projected.key for projected in attention.items], expected_attention_keys
                )

    def test_focused_range_keeps_only_current_portions_of_partial_ranges(self):
        month_item = self._item("month", date(2026, 7, 1), date(2026, 7, 31), "required")
        week_item = self._item("week", date(2026, 7, 13), date(2026, 7, 17), "advisory")
        current_month = InclusiveDateRange(date(2026, 7, 1), date(2026, 7, 31))
        historical_week = InclusiveDateRange(date(2026, 7, 13), date(2026, 7, 17))

        current = project_actionable_work(
            (month_item, week_item), current_month, as_of=date(2026, 7, 15), policy="focused-range"
        )
        historical = project_actionable_work(
            (month_item, week_item),
            historical_week,
            as_of=date(2026, 7, 20),
            policy="focused-range",
        )

        self.assertEqual([item.key for item in current.items], ["month", "week"])
        self.assertEqual(historical.items, ())

    def test_focused_membership_is_shared_by_item_and_display_range_consumers(self):
        affected_range = InclusiveDateRange(date(2026, 7, 13), date(2026, 7, 17))
        selected_range = InclusiveDateRange(date(2026, 7, 1), date(2026, 7, 31))
        item = self._item("week", affected_range.start, affected_range.end, "required")

        self.assertTrue(
            focused_range_has_membership(affected_range, selected_range, as_of=date(2026, 7, 15))
        )
        self.assertEqual(
            project_actionable_work(
                (item,), selected_range, as_of=date(2026, 7, 15), policy="focused-range"
            ).items,
            (item,),
        )
        self.assertFalse(
            focused_range_has_membership(affected_range, selected_range, as_of=date(2026, 7, 18))
        )

    def test_preceptor_date_and_rounding_week_use_focused_membership_boundaries(self):
        selected_range = InclusiveDateRange(date(2026, 7, 1), date(2026, 7, 31))

        with patch(
            "schedules.services.work_remaining.organization_today", return_value=date(2026, 7, 15)
        ):
            self.assertTrue(
                preceptor_date_has_focused_membership(date(2026, 7, 15), selected_range)
            )
            self.assertFalse(
                preceptor_date_has_focused_membership(date(2026, 7, 14), selected_range)
            )
            self.assertTrue(rounding_week_has_focused_membership(date(2026, 7, 13), selected_range))
            self.assertFalse(rounding_week_has_focused_membership(date(2026, 7, 6), selected_range))

    def test_focused_preceptor_findings_keep_week_ownership_through_friday(self):
        week = date(2026, 9, 7)
        week_owned_finding = DraftFinding(
            "missing_normal_rounder", "hard", None, week, (), {"week_start": week}
        )
        monday_late_doc_finding = DraftFinding(
            "missing_late_doc",
            "hard",
            SimpleNamespace(date=week),
            None,
            (),
            {"position": "late-doc"},
        )
        findings = DraftFindingResult((week_owned_finding, monday_late_doc_finding))
        current_week = InclusiveDateRange(week, date(2026, 9, 11))

        for today, selected_range, expected_codes in (
            (date(2026, 9, 8), current_week, ("missing_normal_rounder",)),
            (date(2026, 9, 11), current_week, ("missing_normal_rounder",)),
            (date(2026, 9, 12), current_week, ()),
            (date(2026, 9, 8), InclusiveDateRange(date(2026, 9, 14), date(2026, 9, 18)), ()),
        ):
            with self.subTest(today=today, selected_range=selected_range):
                with patch(
                    "schedules.services.work_remaining.organization_today", return_value=today
                ):
                    focused = focused_draft_findings_for_range(findings, selected_range)

                self.assertEqual(
                    tuple(finding.code for finding in focused.findings), expected_codes
                )

    def test_focused_range_handles_quarter_and_academic_year_crossings(self):
        quarter_item = self._item("quarter", date(2026, 3, 31), date(2026, 4, 1), "required")
        academic_year_item = self._item(
            "academic-year", date(2026, 6, 30), date(2026, 7, 1), "advisory"
        )

        quarter = project_actionable_work(
            (quarter_item,),
            InclusiveDateRange(date(2026, 3, 31), date(2026, 4, 1)),
            as_of=date(2026, 4, 1),
            policy="focused-range",
        )
        academic_year = project_actionable_work(
            (academic_year_item,),
            InclusiveDateRange(date(2026, 6, 30), date(2026, 7, 1)),
            as_of=date(2026, 7, 1),
            policy="focused-range",
        )

        self.assertEqual(quarter.items, (quarter_item,))
        self.assertEqual(academic_year.items, (academic_year_item,))

    def test_focused_range_uses_domain_owned_actionable_dates(self):
        item = ProjectedWorkItem(
            key="undated",
            area="test-area",
            label="Actionable independently of its affected range",
            affected_range=InclusiveDateRange(date(2026, 6, 1), date(2026, 6, 30)),
            matters_on=date(2026, 1, 1),
            target_url="/work/undated/",
            reasons=(ProjectedWorkReason("required", "Needs action", 1),),
            actionable_dates=(date(2026, 7, 15),),
        )

        focused = project_actionable_work(
            (item,),
            InclusiveDateRange(date(2026, 7, 15), date(2026, 7, 15)),
            as_of=date(2026, 7, 1),
            policy="focused-range",
        )

        self.assertEqual(focused.items, (item,))

    def test_lead_time_changes_current_attention_not_focused_range_membership(self):
        selected_range = InclusiveDateRange(date(2026, 8, 1), date(2026, 8, 1))
        short_lead_time = self._item(
            "short-lead-time",
            date(2026, 8, 1),
            date(2026, 8, 1),
            "required",
            matters_on=date(2026, 7, 31),
        )
        long_lead_time = self._item(
            "long-lead-time",
            date(2026, 8, 1),
            date(2026, 8, 1),
            "required",
            matters_on=date(2026, 7, 1),
        )

        short_attention = project_actionable_work(
            (short_lead_time,), selected_range, as_of=date(2026, 7, 15), policy="current-attention"
        )
        long_attention = project_actionable_work(
            (long_lead_time,), selected_range, as_of=date(2026, 7, 15), policy="current-attention"
        )
        short_focused = project_actionable_work(
            (short_lead_time,), selected_range, as_of=date(2026, 7, 15), policy="focused-range"
        )
        long_focused = project_actionable_work(
            (long_lead_time,), selected_range, as_of=date(2026, 7, 15), policy="focused-range"
        )

        self.assertEqual(short_attention.items, ())
        self.assertEqual(long_attention.items, (long_lead_time,))
        self.assertEqual(short_focused.items, (short_lead_time,))
        self.assertEqual(long_focused.items, (long_lead_time,))

    def test_retained_history_scope_uses_affected_and_explicit_actionable_dates(self):
        item = ProjectedWorkItem(
            key="actionable-date",
            area="test-area",
            label="Actionable on the selected day",
            affected_range=InclusiveDateRange(date(2026, 7, 6), date(2026, 7, 10)),
            matters_on=date(2026, 7, 1),
            target_url="/work/actionable-date/",
            reasons=(ProjectedWorkReason("required", "Needs action", 1),),
            actionable_dates=(date(2026, 6, 30),),
        )

        action_scope = project_actionable_work(
            (item,),
            InclusiveDateRange(date(2026, 6, 30), date(2026, 6, 30)),
            as_of=date(2026, 7, 11),
            policy="retained-history",
        )
        outside_scope = project_actionable_work(
            (item,),
            InclusiveDateRange(date(2026, 7, 11), date(2026, 7, 11)),
            as_of=date(2026, 7, 11),
            policy="retained-history",
        )

        self.assertEqual(action_scope.items, (item,))
        self.assertEqual(outside_scope.items, ())
        self.assertEqual(action_scope.required_item_count, 1)
        self.assertEqual(action_scope.required_summary_items, ())

    def test_advisory_clear_and_unevaluated_scopes_have_distinct_tones(self):
        clear_work_prose = r"(?i)\b(?:no\s+work\s+remains|all\s+required\s+work\s+is\s+complete)\b"
        unevaluated_work_prose = r"(?i)\b(?:work\s+remaining\s+is\s+not\s+evaluated|work\s+has\s+not\s+been\s+assessed)\b"
        selected_range = InclusiveDateRange(date(2026, 5, 1), date(2026, 5, 31))
        advisory = scoped_work_remaining(
            (self._item("advisory", date(2026, 5, 1), date(2026, 5, 1), "advisory"),),
            selected_range,
            evaluated=True,
        )
        clear = scoped_work_remaining((), selected_range, evaluated=True)
        neutral = scoped_work_remaining((), selected_range, evaluated=False)

        self.assertEqual(advisory.tone, "attention")
        self.assertEqual(clear.tone, "clear")
        self.assertTrue(clear.evaluated)
        self.assertEqual((clear.required_item_count, clear.advisory_item_count), (0, 0))
        self.assertRegex(clear.prose, clear_work_prose)
        self.assertEqual(neutral.tone, "neutral")
        self.assertFalse(neutral.evaluated)
        self.assertEqual((neutral.required_item_count, neutral.advisory_item_count), (0, 0))
        self.assertRegex(neutral.prose, unevaluated_work_prose)

    def test_focused_summary_includes_far_future_and_excludes_historical_membership(self):
        selected_range = InclusiveDateRange(date(2026, 7, 1), date(2026, 7, 31))
        future = self._item(
            "future", date(2026, 7, 20), date(2026, 7, 20), "required", matters_on=date(2026, 1, 1)
        )
        historical = self._item("historical", date(2026, 7, 1), date(2026, 7, 5), "required")

        with patch(
            "schedules.services.work_remaining.organization_today", return_value=date(2026, 7, 10)
        ):
            summary = focused_work_remaining((future, historical), selected_range)

        self.assertEqual(summary.items, (future,))
        self.assertEqual(summary.required_item_count, 1)
        self.assertEqual(summary.tone, "danger")

    def test_wholly_historical_focused_range_is_neutral_correction_context(self):
        selected_range = InclusiveDateRange(date(2026, 7, 1), date(2026, 7, 5))

        with patch(
            "schedules.services.work_remaining.organization_today", return_value=date(2026, 7, 10)
        ):
            summary = focused_work_remaining(
                (self._item("historical", selected_range.start, selected_range.end, "required"),),
                selected_range,
            )

        self.assertEqual(summary.items, ())
        self.assertEqual((summary.required_item_count, summary.advisory_item_count), (0, 0))
        self.assertFalse(summary.evaluated)
        self.assertEqual(summary.tone, "neutral")

    def test_unevaluated_scope_keeps_required_and_advisory_prose_with_neutral_tone(self):
        selected_range = InclusiveDateRange(date(2026, 5, 1), date(2026, 5, 31))
        required = scoped_work_remaining(
            (self._item("required", date(2026, 5, 1), date(2026, 5, 1), "required"),),
            selected_range,
            evaluated=False,
        )
        advisory = scoped_work_remaining(
            (self._item("advisory", date(2026, 5, 1), date(2026, 5, 1), "advisory"),),
            selected_range,
            evaluated=False,
        )

        self.assertEqual(required.tone, "neutral")
        self.assertEqual(required.required_item_count, 1)
        # TEXT-CONTRACT: work-remaining-count-grammar
        self.assertRegex(required.prose, r"\b1 required action remains\b")
        self.assertEqual(advisory.tone, "neutral")
        self.assertEqual(advisory.advisory_item_count, 1)
        # TEXT-CONTRACT: work-remaining-count-grammar
        self.assertRegex(advisory.prose, r"\b1 advisory action remains\b")

    def test_summary_prose_contains_counts_without_projected_item_details(self):
        summary = scoped_work_remaining(
            (
                self._item(
                    "unsafe",
                    date(2026, 5, 1),
                    date(2026, 5, 1),
                    "required",
                    label='<script>alert("unsafe")</script>',
                ),
            ),
            InclusiveDateRange(date(2026, 5, 1), date(2026, 5, 31)),
            evaluated=True,
        )

        content = render_to_string(
            "schedules/work_remaining_summary.html", {"work_remaining": summary}
        )

        # TEXT-CONTRACT: work-remaining-count-grammar
        self.assertRegex(content, r"\b1 required action remains\b")
        self.assertNotIn("unsafe", content)
        self.assertNotIn("<a", content)
        self.assertNotIn("<ul", content)
        self.assertNotIn("<ol", content)

    def test_summary_renders_an_optional_header_action_without_an_empty_wrapper(self):
        selected_range = InclusiveDateRange(date(2026, 5, 1), date(2026, 5, 31))
        summary = scoped_work_remaining(
            (),
            selected_range,
            evaluated=True,
            range_action=WorkRemainingRangeAction("Open in Schedule Bot", "/solve/?month=2026-05"),
        )

        content = render_to_string(
            "schedules/work_remaining_summary.html", {"work_remaining": summary}
        )

        parser = _RenderedSurfaceParser()
        parser.feed(content)
        summaries = [
            element
            for element in parser.elements
            if element.tag == "section"
            and "workspace-work-summary" in element.attributes.get("class", "").split()
        ]
        self.assertEqual(len(summaries), 1)
        heading = next(
            element
            for element in summaries[0].descendants()
            if element.tag == "h2"
            and element.attributes.get("id") == "work-remaining-summary-heading"
        )
        self.assertEqual(heading.text().strip(), "Work remaining")
        action = next(
            element
            for element in parser.elements
            if element.tag == "a"
            and "workspace-work-summary-action" in element.attributes.get("class", "").split()
        )

        def header_ancestor(element):
            while element is not None:
                if "surface-header" in element.attributes.get("class", "").split():
                    return element
                element = element.parent
            return None

        heading_header = header_ancestor(heading)
        action_header = header_ancestor(action)
        self.assertIsNotNone(heading_header)
        self.assertIs(heading_header, action_header)
        self.assertEqual(action.text().strip(), "Open in Schedule Bot")
        self.assertEqual(action.attributes.get("href"), "/solve/?month=2026-05")

        without_action = render_to_string(
            "schedules/work_remaining_summary.html",
            {"work_remaining": scoped_work_remaining((), selected_range, evaluated=True)},
        )
        parser = _RenderedSurfaceParser()
        parser.feed(without_action)
        self.assertFalse(
            any(
                element.tag == "a"
                and "workspace-work-summary-action" in element.attributes.get("class", "").split()
                for element in parser.elements
            )
        )

    def test_grouped_reasons_sum_quantities_by_classification(self):
        summary = scoped_work_remaining(
            (
                ProjectedWorkItem(
                    key="grouped",
                    area="test-area",
                    label="One grouped row",
                    affected_range=InclusiveDateRange(date(2026, 5, 1), date(2026, 5, 1)),
                    matters_on=date(2026, 5, 1),
                    target_url="/work/grouped/",
                    reasons=(
                        ProjectedWorkReason("required", "Three required actions", 3),
                        ProjectedWorkReason("advisory", "Two advisory actions", 2),
                    ),
                ),
            ),
            InclusiveDateRange(date(2026, 5, 1), date(2026, 5, 31)),
            evaluated=True,
        )

        self.assertEqual((summary.required_item_count, summary.advisory_item_count), (3, 2))
        self.assertEqual(summary.tone, "danger")

    def test_typed_required_summary_uses_ordered_grammar_and_reconciles_to_total(self):
        selected_range = InclusiveDateRange(date(2026, 5, 1), date(2026, 5, 31))
        summary = scoped_work_remaining(
            (
                ProjectedWorkItem(
                    key="typed",
                    area="test-area",
                    label="Typed work",
                    affected_range=selected_range,
                    matters_on=selected_range.start,
                    target_url="/work/typed/",
                    reasons=(
                        ProjectedWorkReason(
                            "required",
                            "One position",
                            1,
                            WorkSummaryType("position", "unfilled position", "unfilled positions"),
                        ),
                        ProjectedWorkReason(
                            "required",
                            "Two weeks",
                            2,
                            WorkSummaryType("week", "incomplete week", "incomplete weeks"),
                        ),
                        ProjectedWorkReason(
                            "required",
                            "Three findings",
                            3,
                            WorkSummaryType(
                                "finding", "unavailable finding", "unavailable findings"
                            ),
                        ),
                    ),
                ),
            ),
            selected_range,
            evaluated=True,
        )

        self.assertEqual(
            [(item.key, item.count, item.label) for item in summary.required_summary_items],
            [
                ("position", 1, "unfilled position"),
                ("week", 2, "incomplete weeks"),
                ("finding", 3, "unavailable findings"),
            ],
        )
        self.assertEqual(
            sum(item.count for item in summary.required_summary_items),
            summary.required_item_count,
        )
        # TEXT-CONTRACT: work-remaining-count-grammar
        self.assertRegex(summary.prose, r"\b6 required actions remain\b")
        typed_phrases = (
            "1 unfilled position",
            "2 incomplete weeks",
            "3 unavailable findings",
        )
        for phrase in typed_phrases:
            self.assertRegex(summary.prose, rf"\b{re.escape(phrase)}\b")
        self.assertEqual(
            [summary.prose.index(phrase) for phrase in typed_phrases],
            sorted(summary.prose.index(phrase) for phrase in typed_phrases),
        )
        self.assertIn(" and ", summary.prose)

    def test_typed_required_summary_uses_conjunction_for_one_and_two_types(self):
        selected_range = InclusiveDateRange(date(2026, 5, 1), date(2026, 5, 31))
        position = WorkSummaryType("position", "unfilled position", "unfilled positions")
        week = WorkSummaryType("week", "incomplete week", "incomplete weeks")

        for reasons, required_count, expected_phrases, has_conjunction in (
            (
                (ProjectedWorkReason("required", "One position", 1, position),),
                1,
                ("1 unfilled position",),
                False,
            ),
            (
                (
                    ProjectedWorkReason("required", "One position", 1, position),
                    ProjectedWorkReason("required", "One week", 1, week),
                ),
                2,
                ("1 unfilled position", "1 incomplete week"),
                True,
            ),
        ):
            with self.subTest(required_count=required_count):
                summary = scoped_work_remaining(
                    (
                        ProjectedWorkItem(
                            key="typed",
                            area="test-area",
                            label="Typed work",
                            affected_range=selected_range,
                            matters_on=selected_range.start,
                            target_url="/work/typed/",
                            reasons=reasons,
                        ),
                    ),
                    selected_range,
                    evaluated=True,
                )

                self.assertEqual(summary.required_item_count, required_count)
                expected_count_clause = (
                    "1 required action remains"
                    if required_count == 1
                    else "2 required actions remain"
                )
                # TEXT-CONTRACT: work-remaining-count-grammar
                self.assertRegex(summary.prose, rf"\b{expected_count_clause}\b")
                for phrase in expected_phrases:
                    self.assertRegex(summary.prose, rf"\b{re.escape(phrase)}\b")
                self.assertEqual(" and " in summary.prose, has_conjunction)

    def test_typed_required_summary_rejects_unclassified_required_work(self):
        selected_range = InclusiveDateRange(date(2026, 5, 1), date(2026, 5, 31))
        item = ProjectedWorkItem(
            key="partly-typed",
            area="test-area",
            label="Partly typed work",
            affected_range=selected_range,
            matters_on=selected_range.start,
            target_url="/work/partly-typed/",
            reasons=(
                ProjectedWorkReason(
                    "required",
                    "Typed",
                    1,
                    WorkSummaryType("typed", "typed action", "typed actions"),
                ),
                ProjectedWorkReason("required", "Untyped", 1),
            ),
        )

        with self.assertRaisesRegex(ValueError, "classify every required action"):
            scoped_work_remaining((item,), selected_range, evaluated=True)

    def test_reason_quantity_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "positive actionable quantity"):
            ProjectedWorkReason("required", "Invalid", 0)


class WorkProjectionContractMatrixTests(TestCase):
    """Cross-domain contracts using the actual producers and workspace surfaces."""

    today = date(2026, 7, 15)
    far_future_year = 2031

    def setUp(self):
        self.scheduler = get_user_model().objects.create_user(username="scheduler")
        self.scheduler.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )
        self.client.force_login(self.scheduler)
        for family in LeadTimeSetting.Family:
            set_lead_time(family=family, months=1, scheduler=self.scheduler)
        # Q1 begins in July, so three months makes the configured boundary current in April.
        set_lead_time(
            family=LeadTimeSetting.Family.ACADEMIC_EVENTS,
            months=3,
            scheduler=self.scheduler,
        )
        self.definition = create_academic_event_definition(
            base_name="Contract event",
            start_academic_year=2026,
            expected_quarter="Q1",
            default_required_participant_count=1,
            scheduler=self.scheduler,
        )

    def at_today(self, today=None):
        today = today or self.today
        return patch(
            "schedules.services.operational_attention.timezone.now",
            return_value=datetime(today.year, today.month, today.day, 18, tzinfo=UTC),
        )

    def holiday(self, year, nominal_date, *, state=HolidayOccurrence.State.UNSCHEDULED):
        return HolidayOccurrence.objects.create(
            holiday=ClinicHoliday.INDEPENDENCE_DAY,
            calendar_year=year,
            name="Contract holiday",
            nominal_date=nominal_date,
            observed_date=nominal_date if state != HolidayOccurrence.State.UNSCHEDULED else None,
            coverage=HolidayOccurrence.Coverage.WHOLE_DAY,
            state=state,
        )

    def assert_summary_matches_details(self, summary, *, consumer):
        self.assertTrue(summary.items, f"{consumer} should expose unresolved work")
        self.assertTrue(all(item.target_url for item in summary.items))
        self.assertEqual(
            summary.required_item_count,
            sum(
                reason.quantity
                for item in summary.items
                for reason in item.reasons
                if reason.classification == "required"
            ),
            consumer,
        )
        self.assertEqual(
            summary.advisory_item_count,
            sum(
                reason.quantity
                for item in summary.items
                for reason in item.reasons
                if reason.classification == "advisory"
            ),
            consumer,
        )

    def assert_focused_consumer_contract(
        self,
        response,
        *,
        domain,
        consumer,
        expected_records,
        record_tag,
        record_class=None,
    ):
        """Prove every focused summary item has its own visible record and action."""
        summary = response.context["work_remaining"]
        self.assert_summary_matches_details(summary, consumer=consumer)
        if summary.required_item_count:
            self.assertContains(
                response,
                f"{summary.required_item_count} required action"
                f"{'s' if summary.required_item_count != 1 else ''}",
            )
        if summary.advisory_item_count:
            self.assertContains(
                response,
                f"{summary.advisory_item_count} advisory action"
                f"{'s' if summary.advisory_item_count != 1 else ''}",
            )
        self.assertTrue(
            all(item.area == domain for item in summary.items),
            f"{consumer} should only summarize its {domain} details",
        )
        self.assertEqual(
            [item.key for item in summary.items],
            [record[0] for record in expected_records],
            f"{consumer} should expose the fixture's independently identified work records",
        )
        parser = _RenderedSurfaceParser()
        parser.feed(response.content.decode())
        for record in expected_records:
            key, detail, action, *form_expectation = record
            with self.subTest(domain=domain, consumer=consumer, item=key):
                submitted_values = None
                option = None
                if form_expectation:
                    submitted_values, option = form_expectation[0]
                self.assertTrue(
                    parser.records_with(
                        detail=detail,
                        action=action,
                        record_tag=record_tag,
                        record_class=record_class,
                        submitted_values=submitted_values,
                        option=option,
                    ),
                    f"{consumer} should bind {key}'s visible {detail!r} detail and action",
                )

    def test_global_work_remaining_includes_current_fixture_work_at_its_configured_boundaries(self):
        holiday = self.holiday(2026, date(2026, 7, 20))
        current_week = work_week_start(self.today)

        with self.at_today():
            global_summary = global_work_remaining()
            expected_items = (
                (
                    "holidays",
                    f"holiday-{holiday.pk}",
                    date(2026, 6, 20),
                    InclusiveDateRange(date(2026, 7, 20), date(2026, 7, 20)),
                    reverse("holiday-occurrence-schedule", args=(holiday.pk,)),
                ),
                (
                    "academic-events",
                    "academic-events-unscheduled-yearly-2026-Q1",
                    date(2026, 4, 1),
                    InclusiveDateRange(date(2026, 7, 1), date(2026, 9, 30)),
                    f"{reverse('academic-event-list')}?academic_year=2026",
                ),
                (
                    "precepting",
                    f"precepting-week-{current_week:%Y-%m-%d}",
                    date(2026, 6, 13),
                    InclusiveDateRange(current_week, current_week + date.resolution * 4),
                    f"{reverse('monthly-workspace')}?month={current_week:%Y-%m}#precepting-week-{current_week:%Y-%m-%d}",
                ),
                (
                    "rounding",
                    f"rounding-week-{current_week:%Y-%m-%d}",
                    date(2026, 6, 13),
                    InclusiveDateRange(current_week, current_week + date.resolution * 6),
                    f"{reverse('normal-rounder-workspace')}?start_date={current_week:%Y-%m-%d}"
                    f"&end_date={current_week:%Y-%m-%d}",
                ),
                (
                    "interviews",
                    "interview-2026",
                    date(2026, 6, 1),
                    InclusiveDateRange(date(2026, 7, 1), date(2027, 6, 30)),
                    f"{reverse('interview-list')}?academic_year=2026",
                ),
            )

        self.assert_summary_matches_details(global_summary, consumer="Schedule > Work Remaining")
        for domain, key, matters_on, affected_range, target_url in expected_items:
            with self.subTest(domain=domain, consumer="Schedule > Work Remaining"):
                actual = next(item for item in global_summary.items if item.key == key)
                self.assertEqual(
                    (actual.area, actual.matters_on, actual.affected_range, actual.target_url),
                    (domain, matters_on, affected_range, target_url),
                )
                self.assertTrue(actual.required)

    def test_non_producing_schedule_sources_do_not_create_global_work(self):
        with self.at_today():
            baseline = global_work_remaining()
            physician = create_physician(
                display_name="Non-producing source physician", scheduler=self.scheduler
            )
            Vacation.objects.create(
                physician=physician,
                start_date=date(2026, 7, 20),
                end_date=date(2026, 7, 24),
            )
            create_community_preceptor_rule(
                physician=physician,
                weekday=0,
                ordinals=[1],
                periods=["AM"],
                scheduler=self.scheduler,
            )
            create_casual_rounder_rule(
                physician=physician, weekday=1, ordinals=[2], scheduler=self.scheduler
            )
            set_initial_stipend_rate(
                kind="community_preceptor",
                hourly_rate=Decimal("100.00"),
                scheduler=self.scheduler,
            )
            with_non_producing_sources = global_work_remaining()

        self.assertEqual(with_non_producing_sources, baseline)

    def test_focused_workspaces_show_future_producer_work_before_global_attention(self):
        future_holiday = self.holiday(self.far_future_year, date(self.far_future_year, 7, 4))
        precepting_weeks = (
            ("2031-09-29", "Sep 29", "Wed, Oct 1"),
            ("2031-10-06", "Oct 6", "Mon, Oct 6"),
            ("2031-10-13", "Oct 13", "Mon, Oct 13"),
            ("2031-10-20", "Oct 20", "Mon, Oct 20"),
            ("2031-10-27", "Oct 27", "Mon, Oct 27"),
        )
        # Work remains grouped by week, but the month opens individual slots.
        # Use in-month work at both edges: September's preview cells do not
        # contribute to October's focused summary.
        month_sessions = {
            (session.date.day, session.period_of_day): session
            for session in open_monthly_draft(date(2031, 10, 1)).sessions.all()
        }
        precepting_month_records = tuple(
            (
                f"precepting-week-{week}",
                f"Oct {day}",
                reverse(
                    "session-assignment-edit",
                    args=(month_sessions[day, period].pk, position),
                )
                + "?month=2031-10",
            )
            for week, day, period, position in (
                ("2031-09-29", 1, "AM", "primary-1"),
                ("2031-10-06", 6, "AM", "primary-1"),
                ("2031-10-13", 13, "AM", "primary-1"),
                ("2031-10-20", 20, "AM", "primary-1"),
                ("2031-10-27", 31, "PM", "primary-2"),
            )
        )
        precepting_list_records = tuple(
            (
                f"precepting-week-{week}",
                list_detail,
                reverse("session-assignment-edit", args=(month_sessions[day, "AM"].pk, "primary-1"))
                + "?month=2031-10",
            )
            for (week, _month_detail, list_detail), day in zip(
                precepting_weeks, (1, 6, 13, 20, 27), strict=True
            )
        )
        rounding_calendar_records = tuple(
            (
                f"rounding-week-{week}",
                f"Edit week of {week}",
                f"{reverse('normal-rounder-record')}?view=calendar&month=2031-10&week={week}",
            )
            for week, _month_detail, _list_detail in precepting_weeks
        )
        rounding_list_records = tuple(
            (
                f"rounding-week-{week}",
                f"{month_detail} - {end_detail}",
                f"{reverse('normal-rounder-record')}?view=list&month=2031-10&week={week}",
            )
            for (week, month_detail, _list_detail), end_detail in zip(
                precepting_weeks,
                ("Oct 5", "Oct 12", "Oct 19", "Oct 26", "Nov 2"),
                strict=True,
            )
        )
        responses = (
            (
                "holidays",
                "Holidays calendar year",
                reverse("holiday-workspace"),
                {"calendar_year": self.far_future_year},
                (
                    (
                        f"holiday-{future_holiday.pk}",
                        "Contract holiday",
                        f"{reverse('holiday-occurrence-schedule', args=(future_holiday.pk,))}?calendar_year=2031",
                    ),
                ),
                ("tr", None),
            ),
            (
                "academic-events",
                "Academic Events calendar",
                reverse("academic-event-workspace"),
                {"academic_year": self.far_future_year, "month": f"{self.far_future_year}-07"},
                (
                    (
                        f"academic-event-definition-{self.definition.pk}-{self.far_future_year}",
                        "Contract event",
                        reverse("academic-event-create"),
                        (
                            {
                                "academic_year": "2031",
                                "view": "calendar",
                                "month": "2031-07",
                                "definition": str(self.definition.pk),
                            },
                            {
                                "select_name": "definition",
                                "value": str(self.definition.pk),
                                "label": "Contract event (Q1)",
                            },
                        ),
                    ),
                ),
                ("form", "academic-event-add-form"),
            ),
            (
                "academic-events",
                "Academic Events list",
                reverse("academic-event-list"),
                {"academic_year": self.far_future_year},
                (
                    (
                        f"academic-event-definition-{self.definition.pk}-{self.far_future_year}",
                        "Contract event",
                        f"{reverse('academic-event-create')}?academic_year=2031&view=list&definition={self.definition.pk}",
                    ),
                ),
                ("tr", None),
            ),
            (
                "precepting",
                "Precepting month",
                reverse("monthly-workspace"),
                {"month": f"{self.far_future_year}-10"},
                precepting_month_records,
                ("section", "calendar-week-boundary"),
            ),
            (
                "precepting",
                "Precepting list",
                reverse("preceptor-list"),
                {"month": f"{self.far_future_year}-10"},
                precepting_list_records,
                ("tr", None),
            ),
            (
                "rounding",
                "Rounding calendar",
                reverse("rounding-workspace"),
                {"month": f"{self.far_future_year}-10"},
                rounding_calendar_records,
                ("a", "rounding-normal-span--missing"),
            ),
            (
                "rounding",
                "Rounding list",
                reverse("rounding-list"),
                {"month": f"{self.far_future_year}-10"},
                rounding_list_records,
                ("tr", None),
            ),
            (
                "rounding",
                "Normal Rounding range",
                reverse("normal-rounder-workspace"),
                {
                    "start_date": f"{self.far_future_year}-10-06",
                    "end_date": f"{self.far_future_year}-10-06",
                },
                (
                    (
                        "rounding-week-2031-10-06",
                        "Oct. 6, 2031",
                        f"{reverse('normal-rounder-record')}?view=normal&start_date=2031-10-06&end_date=2031-10-06&week=2031-10-06",
                    ),
                ),
                ("tr", None),
            ),
            (
                "interviews",
                "Interviews calendar",
                reverse("interview-workspace"),
                {"academic_year": self.far_future_year, "month": f"{self.far_future_year}-10"},
                (
                    (
                        "interview-2031",
                        "20 interviews remain to be scheduled",
                        f"{reverse('interview-create')}?academic_year=2031&view=calendar&month=2031-10",
                    ),
                ),
                ("div", "contextual-action-row"),
            ),
            (
                "interviews",
                "Interviews list",
                reverse("interview-list"),
                {"academic_year": self.far_future_year},
                (
                    (
                        "interview-2031",
                        "20 interviews remain to be scheduled",
                        f"{reverse('interview-create')}?academic_year=2031&view=list",
                    ),
                ),
                ("div", "contextual-action-row"),
            ),
        )

        with self.at_today():
            global_keys = {item.key for item in global_work_remaining().items}
            self.assertNotIn(f"holiday-{future_holiday.pk}", global_keys)
            for (
                domain,
                consumer,
                url,
                query,
                expected_records,
                record_shape,
            ) in responses:
                with self.subTest(domain=domain, consumer=consumer):
                    response = self.client.get(url, query)
                    self.assertEqual(response.status_code, 200)
                    self.assert_focused_consumer_contract(
                        response,
                        domain=domain,
                        consumer=consumer,
                        expected_records=expected_records,
                        record_tag=record_shape[0],
                        record_class=record_shape[1],
                    )
                    if consumer == "Precepting month":
                        parser = _RenderedSurfaceParser()
                        parser.feed(response.content.decode())
                        for key, _detail, action in precepting_month_records:
                            with self.subTest(item=key):
                                links = parser.records_with(
                                    detail="Unfilled", action=action, record_tag="a"
                                )
                                self.assertTrue(links, "Unresolved slots need visible actions")
                                editor = self.client.get(links[0].attributes["href"])
                                self.assertEqual(editor.status_code, 200)
                                editor_parser = _RenderedSurfaceParser()
                                editor_parser.feed(editor.content.decode())
                                self.assertTrue(
                                    editor_parser.records_with(
                                        detail="",
                                        action=f"{reverse('monthly-workspace')}?month=2031-10",
                                        record_tag="a",
                                        record_class="form-return-link",
                                    ),
                                    "Focused slot actions must return to the originating month",
                                )
                    summary = response.context["work_remaining"]
                    content = response.content.decode()
                    period_controls = (
                        "compact-filter-form"
                        if consumer == "Normal Rounding range"
                        else "period-navigation"
                    )
                    self.assertLess(
                        content.index(period_controls),
                        content.index("workspace-work-summary"),
                        f"{consumer} should render its summary after period controls",
                    )
                    self.assertLess(
                        content.index("workspace-work-summary"),
                        content.index("workspace-surface-stack"),
                        f"{consumer} should render its summary before its workspace details",
                    )
                    self.assertTrue(
                        all(item.key not in global_keys for item in summary.items),
                        "focused work must be visible before it enters global attention",
                    )

    def test_deliberate_non_consumers_render_without_a_work_remaining_summary(self):
        responses = (
            ("Vacation calendar", reverse("vacation-workspace"), {"month": "2031-10"}),
            ("Vacation list", reverse("vacation-list"), {"month": "2031-10"}),
            (
                "Community Precepting calendar",
                reverse("community-preceptor-workspace"),
                {"month": "2031-10"},
            ),
            (
                "Community Precepting rules",
                reverse("community-preceptor-rules"),
                {"month": "2031-10"},
            ),
            (
                "Academic Event definitions",
                reverse("academic-event-definition-workspace"),
                {"academic_year": self.far_future_year},
            ),
            ("Physicians", reverse("physicians-directory"), {}),
            ("Reports", reverse("schedule-reports"), {}),
            ("Master Schedule", reverse("master-schedule"), {"date": "2031-10-06"}),
            ("Schedule Bot", reverse("precepting-solver-workspace"), {}),
        )

        for consumer, url, query in responses:
            with self.subTest(consumer=consumer):
                response = self.client.get(url, query)
                self.assertEqual(response.status_code, 200)
                parser = _RenderedSurfaceParser()
                parser.feed(response.content.decode())
                self.assertFalse(
                    any(
                        "workspace-work-summary" in element.attributes.get("class", "").split()
                        for element in parser.elements
                    ),
                    f"{consumer} should not render a Work Remaining summary",
                )

    def test_precepting_advisory_reason_reconciles_to_its_focused_workspace_summary(self):
        week = work_week_start(self.today)
        advisory_finding = DraftFinding("backup_only_assignment", "preference", None, week, (), {})
        with (
            self.at_today(),
            patch("schedules.services.preceptors.is_operational_preceptor_week", return_value=True),
            patch(
                "schedules.services.preceptors.operational_draft_findings_for_week",
                return_value=DraftFindingResult((advisory_finding,)),
            ),
        ):
            summary = focused_work_remaining(
                precepting_projected_work_items((week,)),
                InclusiveDateRange(week, week + date.resolution * 4),
            )

        self.assertEqual((summary.required_item_count, summary.advisory_item_count), (0, 1))
        self.assert_summary_matches_details(summary, consumer="Precepting week")

    def test_acknowledged_hard_findings_are_excluded_consistently_from_focused_and_global_work(
        self,
    ):
        week = work_week_start(self.today)
        open_monthly_draft(week)
        findings = draft_findings_for_week(week).findings
        hard_findings = tuple(finding for finding in findings if finding.severity == "hard")
        acknowledged = next(
            finding for finding in hard_findings if finding.code == "missing_primary_position"
        )
        acknowledge_finding(
            finding=acknowledged,
            scheduler=self.scheduler,
            reason="External coverage is confirmed.",
        )

        with (
            self.at_today(),
            patch("schedules.services.preceptors.is_operational_preceptor_week", return_value=True),
            patch(
                "schedules.services.preceptors.operational_draft_findings_for_week",
                return_value=DraftFindingResult(findings),
            ),
        ):
            items = precepting_projected_work_items((week,))
            focused = focused_work_remaining(
                items, InclusiveDateRange(week, week + date.resolution * 4)
            )
            with patch(
                "schedules.services.work_remaining._global_projected_work_items", return_value=items
            ):
                global_summary = global_work_remaining()

        expected_required_count = len(hard_findings) - 1
        self.assertEqual(focused.required_item_count, expected_required_count)
        self.assertEqual(global_summary.required_item_count, expected_required_count)
        self.assertEqual(focused.items[0].reasons, global_summary.items[0].reasons)

    def test_acknowledged_interview_hard_findings_are_excluded_from_focused_and_global_work(
        self,
    ):
        focused_range = InclusiveDateRange(date(2026, 7, 20), date(2026, 7, 21))
        physician = create_physician(
            display_name="Unavailable interviewer", scheduler=self.scheduler
        )
        shifts = (
            InterviewShift.objects.create(date=focused_range.start, physician=physician),
            InterviewShift.objects.create(date=focused_range.end, physician=physician),
        )

        with self.at_today():
            findings = tuple(
                finding
                for shift in shifts
                for finding in interview_findings_for_shift(shift).findings
            )
            self.assertEqual([finding.severity for finding in findings], ["hard", "hard"])
            acknowledge_finding(
                finding=findings[0],
                scheduler=self.scheduler,
                reason="No alternate interviewer is available.",
            )
            summary = InterviewScheduleSummary(
                academic_year=2026,
                shifts=shifts,
                attention_active=True,
                active_count=20,
                remaining_to_20=0,
                unassigned_count=0,
                over_target_count=0,
                hard_findings=findings,
                findings=findings,
                complete=False,
            )
            with patch(
                "schedules.services.interviews.operational_interview_schedule_summary",
                return_value=summary,
            ):
                items = interview_projected_work_items((2026,))
            focused = _interview_scoped_work_remaining(summary, focused_range)
            with patch(
                "schedules.services.work_remaining._global_projected_work_items", return_value=items
            ):
                global_summary = global_work_remaining()

        self.assertEqual(len(summary.findings), 2)
        self.assertIn(findings[0], summary.findings)
        self.assertEqual(focused.required_item_count, 1)
        self.assertEqual(global_summary.required_item_count, 1)
        self.assertEqual(focused.items[0].reasons, global_summary.items[0].reasons)

    def test_completed_historical_holiday_keeps_its_correction_action_without_operational_work(
        self,
    ):
        historical = self.holiday(
            2025,
            date(2025, 7, 4),
            state=HolidayOccurrence.State.SCHEDULED,
        )

        with self.at_today():
            response = self.client.get(reverse("holiday-workspace"), {"calendar_year": 2025})
            global_keys = {item.key for item in global_work_remaining().items}

        summary = response.context["work_remaining"]
        self.assertEqual((summary.required_item_count, summary.advisory_item_count), (0, 0))
        self.assertEqual(summary.tone, "neutral")
        self.assertContains(response, "surface-tone--neutral workspace-work-summary")
        self.assertNotIn(f"holiday-{historical.pk}", global_keys)
        self.assertContains(
            response,
            reverse("holiday-occurrence-correct", args=(historical.pk,)),
        )
