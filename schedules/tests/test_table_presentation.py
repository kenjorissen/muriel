from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar, cast

from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.utils.safestring import mark_safe

from schedules.table_presentation import (
    CellKind,
    DataTable,
    DataTableCell,
    DataTableColumn,
    DataTableLink,
    DataTableRow,
)


class _RenderedElement:
    def __init__(self, tag, attributes, parent=None):
        self.tag = tag
        self.attributes = dict(attributes)
        self.parent = parent
        self.children = []

    def text(self):
        return "".join(child if isinstance(child, str) else child.text() for child in self.children)

    def children_named(self, *tags):
        return [
            child
            for child in self.children
            if isinstance(child, _RenderedElement) and child.tag in tags
        ]

    def ancestors(self):
        element = self.parent
        ancestors = []
        while element:
            ancestors.append(element)
            element = element.parent
        return ancestors

    def descendants_named(self, *tags):
        descendants = []
        for child in self.children:
            if isinstance(child, _RenderedElement):
                if child.tag in tags:
                    descendants.append(child)
                descendants.extend(child.descendants_named(*tags))
        return descendants


class _RenderedTableParser(HTMLParser):
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

    def handle_starttag(self, tag, attrs):
        element = _RenderedElement(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(element)
        if tag not in self._void_tags:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


class _RenderedDataTable:
    def __init__(self, content, identity=None, *, table_class=None):
        parser = _RenderedTableParser()
        parser.feed(content)
        tables = [
            element
            for element in parser.root.descendants_named("table")
            if (identity is None or element.attributes.get("id") == identity)
            and (table_class is None or table_class in element.attributes.get("class", "").split())
        ]
        if len(tables) != 1:
            raise AssertionError(
                f"Expected one rendered table for id={identity!r}, class={table_class!r}; "
                f"found {len(tables)}."
            )
        self.table = tables[0]

    def header_labels(self):
        return [header.text() for header in self.headers()]

    def headers(self):
        return self._section_rows("thead")[0].children_named("th")

    def body_rows(self):
        return self._section_rows("tbody")

    def footer_rows(self):
        return self._section_rows("tfoot")

    def cell_for_header(self, row, header_label):
        header_index = self.header_labels().index(header_label)
        return row.children_named("th", "td")[header_index]

    def _section_rows(self, section_name):
        section = self.table.children_named(section_name)[0]
        return section.children_named("tr")


class _RenderedDocument:
    """Small rendered-HTML fixture adapter for form-level interaction assertions."""

    def __init__(self, content):
        parser = _RenderedTableParser()
        parser.feed(content)
        self.root = parser.root

    def forms_with_submission_action(self, *, name, value):
        return [
            form
            for form in self.root.descendants_named("form")
            if any(
                button.attributes.get("name") == name and button.attributes.get("value") == value
                for button in form.descendants_named("button")
            )
        ]

    def form_with_submission_action(self, *, name, value):
        forms = self.forms_with_submission_action(name=name, value=value)
        if len(forms) != 1:
            raise AssertionError(
                f"Expected one form submitting {name}={value!r}; found {len(forms)}."
            )
        return forms[0]


class DataTableRendererTests(SimpleTestCase):
    def test_renderer_escapes_values_and_provides_semantic_sections(self):
        table = DataTable(
            identity="table-contract",
            label="Escaped table",
            columns=(DataTableColumn("Name"), DataTableColumn("Status")),
            rows=(
                DataTableRow(
                    cells=(
                        DataTableCell(mark_safe("<script>unsafe</script>"), row_header=True),
                        DataTableCell("Needs review", kind="status", tone="attention"),
                    ),
                    tone="attention",
                ),
            ),
            footer_rows=(
                DataTableRow(
                    cells=(DataTableCell("Total", row_header=True), DataTableCell("1")),
                ),
            ),
            empty_state="Nothing <recorded>",
        )

        rendered = _RenderedDataTable(
            render_to_string("schedules/data_table.html", {"table": table}), table.identity
        )

        scroll_frame = next(
            ancestor
            for ancestor in rendered.table.ancestors()
            if "data-table-scroll-frame" in ancestor.attributes.get("class", "").split()
        )
        self.assertIn("table-scroll-frame", scroll_frame.attributes["class"].split())
        self.assertEqual(rendered.header_labels(), [column.label for column in table.columns])
        self.assertEqual(
            [header.attributes.get("scope") for header in rendered.headers()],
            ["col"] * len(table.columns),
        )

        body_row = rendered.body_rows()[0]
        name_cell = rendered.cell_for_header(body_row, "Name")
        self.assertEqual(name_cell.tag, "th")
        self.assertEqual(name_cell.attributes["scope"], "row")
        self.assertEqual(name_cell.text(), "<script>unsafe</script>")
        self.assertEqual(rendered.cell_for_header(body_row, "Status").text(), "Needs review")
        self.assertIn("data-table-row--attention", body_row.attributes["class"])
        self.assertIn(
            "data-table-status--attention",
            rendered.cell_for_header(body_row, "Status")
            .children_named("span")[0]
            .attributes["class"],
        )
        self.assertEqual(
            rendered.cell_for_header(rendered.footer_rows()[0], "Name").text(), "Total"
        )
        self.assertFalse(rendered.table.descendants_named("script"))

    def test_renderer_supports_dates_code_links_empty_state_and_compact_width(self):
        table = DataTable(
            identity="table-details",
            label="Details",
            width="compact",
            columns=(
                DataTableColumn("Recorded"),
                DataTableColumn("Value"),
                DataTableColumn("Actions"),
            ),
            rows=(
                DataTableRow(
                    cells=(
                        DataTableCell("Feb 3", kind="date", machine_value="2025-02-03"),
                        DataTableCell("before -> after", kind="code"),
                        DataTableCell(
                            "",
                            kind="links",
                            links=(
                                DataTableLink("Edit", "/edit/"),
                                DataTableLink("View", "/view/"),
                            ),
                        ),
                    )
                ),
            ),
            empty_state="No details.",
        )

        rendered = _RenderedDataTable(
            render_to_string("schedules/data_table.html", {"table": table}), table.identity
        )
        details_row = rendered.body_rows()[0]

        self.assertIn("data-table--compact", rendered.table.attributes["class"])
        recorded_time = rendered.cell_for_header(details_row, "Recorded").children_named("time")[0]
        self.assertEqual(recorded_time.attributes["datetime"], "2025-02-03")
        self.assertEqual(recorded_time.text(), "Feb 3")
        self.assertEqual(
            rendered.cell_for_header(details_row, "Value").children_named("code")[0].text(),
            "before -> after",
        )
        action_links = rendered.cell_for_header(details_row, "Actions").children_named("a")
        self.assertEqual(
            [(link.text(), link.attributes["href"]) for link in action_links],
            [("Edit", "/edit/"), ("View", "/view/")],
        )

        bounded_table = DataTable(
            identity="table-bounded",
            label="Bounded details",
            width="bounded",
            columns=(DataTableColumn("Value"),),
            rows=(DataTableRow((DataTableCell("A long value that can wrap."),)),),
            empty_state="No details.",
        )
        bounded_rendered = _RenderedDataTable(
            render_to_string("schedules/data_table.html", {"table": bounded_table}),
            bounded_table.identity,
        )
        self.assertIn("data-table--bounded", bounded_rendered.table.attributes["class"])

        empty_table = DataTable(
            identity="table-empty",
            label="Empty",
            columns=(
                DataTableColumn("First value"),
                DataTableColumn("Second value"),
                DataTableColumn("Third value"),
            ),
            rows=(),
            empty_state="No details.",
        )
        empty_rendered = _RenderedDataTable(
            render_to_string("schedules/data_table.html", {"table": empty_table}),
            empty_table.identity,
        )
        empty_cell = empty_rendered.body_rows()[0].children_named("td")[0]
        self.assertEqual(empty_cell.text(), empty_table.empty_state)
        self.assertEqual(empty_cell.attributes["colspan"], str(len(empty_table.columns)))

    def test_renderer_supports_timestamp_metadata_and_emphasized_actions(self):
        table = DataTable(
            identity="table-metadata",
            label="Metadata",
            columns=(DataTableColumn("Created"), DataTableColumn("Actions")),
            rows=(
                DataTableRow(
                    cells=(
                        DataTableCell(
                            "scheduler",
                            kind="metadata",
                            detail="Feb 3, 2025, 9:30 AM",
                            machine_value="2025-02-03T09:30:00+00:00",
                        ),
                        DataTableCell(
                            "",
                            kind="links",
                            links=(
                                DataTableLink("Review", "/review/", emphasized=True),
                                DataTableLink("Reuse", "/reuse/"),
                            ),
                        ),
                    )
                ),
            ),
            empty_state="No metadata.",
        )

        rendered = _RenderedDataTable(
            render_to_string("schedules/data_table.html", {"table": table}), table.identity
        )
        metadata_cell = rendered.cell_for_header(rendered.body_rows()[0], "Created")
        metadata_time = metadata_cell.descendants_named("time")[0]
        self.assertEqual(
            metadata_cell.text().replace(metadata_time.text(), "", 1).strip(), "scheduler"
        )
        self.assertEqual(metadata_time.text(), "Feb 3, 2025, 9:30 AM")
        self.assertEqual(metadata_time.attributes["datetime"], "2025-02-03T09:30:00+00:00")
        action_links = rendered.cell_for_header(rendered.body_rows()[0], "Actions").children_named(
            "a"
        )
        self.assertEqual(
            [(link.text(), link.attributes["href"]) for link in action_links],
            [("Review", "/review/"), ("Reuse", "/reuse/")],
        )
        self.assertIn("button-link", action_links[0].attributes["class"])

    def test_renderer_supports_escaped_ordered_tag_lists(self):
        table = DataTable(
            identity="table-tags",
            label="Tags",
            columns=(DataTableColumn("Roles"),),
            rows=(
                DataTableRow(
                    (
                        DataTableCell(
                            "",
                            kind="tags",
                            tags=(mark_safe("<script>unsafe</script>"), "Faculty-equivalent"),
                        ),
                    )
                ),
            ),
            empty_state="No roles.",
        )

        rendered = _RenderedDataTable(
            render_to_string("schedules/data_table.html", {"table": table}), table.identity
        )
        tag_cell = rendered.cell_for_header(rendered.body_rows()[0], "Roles")
        tag_list = tag_cell.children_named("ul")[0]

        self.assertIn("data-table-cell--tags", tag_cell.attributes["class"])
        self.assertEqual(
            [tag.text() for tag in tag_list.children_named("li")],
            ["<script>unsafe</script>", "Faculty-equivalent"],
        )
        self.assertFalse(rendered.table.descendants_named("script"))

    def test_contract_rejects_unknown_variants_and_incomplete_rows(self):
        with self.assertRaises(ValueError):
            DataTableCell("Value", kind=cast(CellKind, "html"))
        with self.assertRaises(ValueError):
            DataTableCell("Link", kind="link")
        with self.assertRaises(ValueError):
            DataTableCell("", kind="tags")
        with self.assertRaises(ValueError):
            DataTableCell("", kind="tags", tags=("",))
        with self.assertRaises(TypeError):
            DataTableCell("", kind="tags", tags=cast(tuple[str, ...], ["Faculty"]))
        with self.assertRaises(ValueError):
            DataTable(
                identity="invalid",
                label="Invalid",
                columns=(DataTableColumn("One"), DataTableColumn("Two")),
                rows=(DataTableRow((DataTableCell("Only one"),)),),
                empty_state="No rows.",
            )
        with self.assertRaises(TypeError):
            DataTableRow(cast(tuple[DataTableCell, ...], ("not a typed cell",)))


class DataTableInventoryTests(SimpleTestCase):
    def test_every_history_template_uses_the_contained_full_width_intro_and_raised_content_surface(
        self,
    ):
        template_root = Path(__file__).resolve().parents[1] / "templates" / "schedules"
        expected_history_templates = {
            "academic_event_definition_history.html",
            "academic_event_history.html",
            "account_history.html",
            "assignment_history.html",
            "community_preceptor_history.html",
            "custom_availability_category_history.html",
            "holiday_history.html",
            "interview_history.html",
            "late_doc_history.html",
            "lead_time_history.html",
            "physician_history.html",
            "physicians_history.html",
            "rounding_history.html",
            "stipend_rate_history.html",
            "vacation_history.html",
        }
        history_templates = {
            template.name: template.read_text() for template in template_root.glob("*history.html")
        }

        self.assertEqual(set(history_templates), expected_history_templates)
        for template_name, content in history_templates.items():
            with self.subTest(template=template_name):
                self.assertIn(
                    'class="form-page-intro form-page-intro--contained form-page-intro--full-width"',
                    content,
                )
                self.assertIn('class="form-return-link"', content)
                self.assertIn('class="form-page-title"', content)
                self.assertIn('class="surface-frame surface-frame--raised', content)

    def test_standard_renderer_inventory_covers_the_baseline_and_specialized_exclusions(self):
        template_root = Path(__file__).resolve().parents[1] / "templates" / "schedules"
        adoptions = (
            ("_casual_rounder_roster.html", "casual_rounder_roster_table"),
            ("_existing_role_context.html", "existing_roles_table"),
            ("_recurrence_rule_context.html", "recurrence_rule_context_table"),
            ("academic_event_definition_history.html", "academic_event_definition_history_table"),
            (
                "academic_event_definition_workspace.html",
                "academic_event_monthly_rule_definitions_table",
            ),
            ("academic_event_definition_workspace.html", "academic_event_yearly_definitions_table"),
            ("academic_event_history.html", "academic_event_history_table"),
            ("academic_event_list.html", "academic_event_monthly_filled_events_table"),
            ("academic_event_list.html", "academic_event_monthly_unfilled_events_table"),
            ("academic_event_list.html", "academic_event_yearly_events_table"),
            ("account_history.html", "account_history_table"),
            ("community_preceptor_history.html", "community_preceptor_history_table"),
            ("community_preceptor_list.html", "community_preceptor_list_table"),
            ("community_preceptor_rules.html", "community_preceptor_rules_table"),
            (
                "custom_availability_category_history.html",
                "custom_availability_category_history_table",
            ),
            ("custom_availability_category_workspace.html", "schedule_options_table"),
            ("holiday_history.html", "holiday_history_table"),
            ("holiday_workspace.html", "holiday_occurrences_table"),
            ("interview_history.html", "interview_history_table"),
            ("interview_list.html", "interview_list_table"),
            ("lead_time_history.html", "lead_time_history_table"),
            ("lead_time_workspace.html", "lead_time_table"),
            ("normal_rounder_workspace.html", "normal_rounder_table"),
            ("physicians_directory.html", "active_physicians_table"),
            ("physicians_directory.html", "inactive_physicians_table"),
            ("physicians_directory.html", "directory_table"),
            ("physician_detail.html", "physician_roles_table"),
            ("physician_detail.html", "faculty_availability_templates_table"),
            ("physician_detail.html", "monthly_availability_rules_table"),
            ("physician_detail.html", "physician_community_preceptor_rules_table"),
            ("physician_detail.html", "physician_casual_rounder_rules_table"),
            ("physician_history.html", "physician_history_table"),
            ("physicians_history.html", "physicians_history_table"),
            ("rounding_history.html", "rounding_history_table"),
            ("rounding_list.html", "rounding_list_table"),
            ("schedule_report.html", "schedule_report_table"),
            ("schedule_report.html", "schedule_report_findings_table"),
            ("faculty_admin_time_report.html", "admin_time_table"),
            ("stipend_rate_history.html", "stipend_rate_history_table"),
            ("stipend_rate_workspace.html", "community_preceptor_rates_table"),
            ("stipend_rate_workspace.html", "late_doc_rates_table"),
            ("stipend_report.html", "stipend_detail_table"),
            ("stipend_report.html", "stipend_totals_table"),
            ("vacation_history.html", "vacation_history_table"),
            ("vacation_list.html", "vacation_list_table"),
            ("work_remaining_workspace.html", "work_remaining_table"),
            ("precepting_solver_workspace.html", "recent_proposals_table"),
            ("precepting_solver_review.html", "proposal_workload_table"),
            ("precepting_solver_review.html", "comparison_workload_table"),
        )
        exclusions = (
            ("Accounts", "account_workspace.html", "account-table"),
            (
                "Academic Events unscheduled",
                "academic_event_list.html",
                "academic-event-mark-not-occurring",
            ),
            (
                "Academic Events not occurring",
                "academic_event_list.html",
                "academic-event-reverse-not-occurring",
            ),
            ("Schedule Bot change review", "precepting_solver_review.html", "solver-review-week"),
            ("Late Doc decisions", "precepting_solver_review.html", "late-doc-review-table"),
            (
                "Master Schedule clinic grid",
                "master_schedule_grids.html",
                "master-schedule-grid--clinic",
            ),
            (
                "Master Schedule supporting grid",
                "master_schedule_grids.html",
                "supporting_grid.label",
            ),
            ("Precepting List", "preceptor_schedule_list.html", "preceptor-schedule-list-table"),
        )
        actual_adoptions = Counter(
            (template.name, table_name)
            for template in template_root.glob("*.html")
            for table_name in _table_includes(template.read_text())
        )

        self.assertEqual(actual_adoptions, Counter(adoptions))
        for name, template_name, marker in exclusions:
            with self.subTest(exclusion=name):
                template = (template_root / template_name).read_text()
                self.assertIn("<table", template)
                self.assertIn(marker, template)


def _table_includes(template):
    prefix = '{% include "schedules/data_table.html" with table='
    return tuple(line.split(" %}", 1)[0] for line in template.split(prefix)[1:] if " %}" in line)
