import re
from datetime import date
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse

from schedules.models import Physician
from schedules.services.preceptors import open_monthly_draft
from schedules.tests.test_work_remaining import _action_matches, _RenderedSurfaceParser


class ApplicationShellTests(TestCase):
    navigation = (
        ("Schedule", "master-schedule"),
        ("Precepting", "monthly-workspace"),
        ("Rounding", "rounding-workspace"),
        ("Vacation", "vacation-workspace"),
        ("Academic Events", "academic-event-workspace"),
        ("Community Precepting", "community-preceptor-workspace"),
        ("Interviews", "interview-workspace"),
        ("Physicians", "physicians-directory"),
        ("Reports", "schedule-reports"),
    )

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )
        self.user.groups.add(Group.objects.get(name="Scheduling Administrators"))
        self.client.force_login(self.user)

    def assert_shell(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "schedules/base.html")
        content = response.content.decode()
        self.assertIn('href="/static/schedules/application.css"', content)
        self.assertIn('src="/static/schedules/images/random-health.png"', content)
        for asset_url in re.findall(r'<(?:link|script|img)\b[^>]*(?:href|src)="([^"]+)"', content):
            with self.subTest(asset_url=asset_url):
                self.assertFalse(asset_url.startswith(("http://", "https://", "//")))

    def rendered_navigation(self, response, *, navigation_label):
        parser = _RenderedSurfaceParser()
        parser.feed(response.content.decode())
        navigations = [
            element
            for element in parser.elements
            if element.tag == "nav" and element.attributes.get("aria-label") == navigation_label
        ]
        self.assertEqual(len(navigations), 1)
        return [element for element in navigations[0].descendants() if element.tag == "a"]

    def assert_active_navigation(self, response, active_label):
        links = self.rendered_navigation(response, navigation_label="Global navigation")
        active_links = [link for link in links if link.attributes.get("aria-current") == "page"]
        self.assertEqual([" ".join(link.text().split()) for link in active_links], [active_label])

    def assert_primary_navigation(self, response, active_label):
        links = self.rendered_navigation(response, navigation_label="Global navigation")
        matched_links = []
        previous_index = -1
        for label, name in self.navigation:
            with self.subTest(label=label):
                matches = [
                    (index, link)
                    for index, link in enumerate(links)
                    if " ".join(link.text().split()) == label
                ]
                self.assertEqual(len(matches), 1)
                index, link = matches[0]
                self.assertGreater(index, previous_index)
                self.assertTrue(_action_matches(link.attributes.get("href", ""), reverse(name)))
                self.assertEqual(
                    link.attributes.get("aria-current") == "page", label == active_label
                )
                previous_index = index
                matched_links.append(link)
        self.assert_active_navigation(response, active_label)
        return matched_links

    def test_primary_navigation_has_documented_destinations_in_order(self):
        response = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})
        links = self.assert_primary_navigation(response, "Precepting")

        for link, (label, _) in zip(links, self.navigation, strict=True):
            with self.subTest(label=label):
                destination = self.client.get(link.attributes["href"])
                self.assertEqual(destination.status_code, 200)
                self.assert_primary_navigation(destination, label)

    def test_primary_workspaces_use_the_shell_and_identify_their_active_section(self):
        responses = (
            (self.client.get(reverse("master-schedule")), "Schedule"),
            (
                self.client.get(reverse("monthly-workspace"), {"month": "2025-02"}),
                "Precepting",
            ),
            (self.client.get(reverse("rounding-workspace")), "Rounding"),
            (self.client.get(reverse("vacation-workspace")), "Vacation"),
            (self.client.get(reverse("academic-event-workspace")), "Academic Events"),
            (self.client.get(reverse("academic-event-definition-workspace")), "Academic Events"),
            (self.client.get(reverse("interview-workspace")), "Interviews"),
            (self.client.get(reverse("interview-list")), "Interviews"),
            (self.client.get(reverse("physicians-directory")), "Physicians"),
            (self.client.get(reverse("community-preceptor-workspace")), "Community Precepting"),
            (self.client.get(reverse("stipend-rate-workspace")), "Reports"),
        )

        for response, active_label in responses:
            with self.subTest(active_label=active_label):
                self.assert_shell(response)
                self.assert_primary_navigation(response, active_label)

    def test_precepting_subviews_keep_precepting_current_in_shared_navigation(self):
        self.assertEqual(reverse("monthly-workspace"), "/")
        responses = (
            self.client.get(reverse("monthly-workspace"), {"month": "2025-02"}),
            self.client.get(reverse("preceptor-list"), {"month": "2025-02"}),
            self.client.get(reverse("precepting-solver-workspace")),
        )

        for response in responses:
            with self.subTest(path=response.request["PATH_INFO"]):
                self.assert_shell(response)
                self.assert_primary_navigation(response, "Precepting")

    def test_nested_form_and_history_pages_keep_their_section_active(self):
        physician = Physician.objects.create(display_name="Shell physician")
        session = open_monthly_draft(date(2025, 2, 1)).sessions.first()
        responses = (
            (self.client.get(reverse("physician-role-add", args=(physician.pk,))), "Physicians"),
            (self.client.get(reverse("rounding-history")), "Rounding"),
            (self.client.get(reverse("interview-create")), "Interviews"),
            (self.client.get(reverse("interview-history")), "Interviews"),
            (
                self.client.get(reverse("session-assignment-edit", args=(session.pk, "primary-1"))),
                "Precepting",
            ),
        )

        for response, active_label in responses:
            with self.subTest(active_label=active_label):
                self.assert_shell(response)
                self.assert_primary_navigation(response, active_label)

    def test_sign_in_is_branded_but_has_no_authenticated_navigation_or_account_controls(self):
        self.client.logout()

        response = self.client.get(reverse("sign-in"))

        content = response.content.decode()
        self.assertContains(response, '<strong class="application-name">Muriel</strong>')
        self.assertContains(
            response, '<span class="application-description">Physician Scheduling</span>'
        )
        self.assertContains(response, '<h1 id="sign-in-heading">Sign in</h1>')
        self.assertContains(response, "Sign in to work on preceptor schedules.")
        self.assertContains(response, '<form class="sign-in-form" method="post">')
        self.assertContains(response, '<div class="sign-in-field">', count=2)
        self.assertContains(response, '<label for="id_username">Username or email:</label>')
        self.assertContains(response, '<label for="id_password">Password:</label>')
        self.assertContains(response, "Forgot password?")
        self.assertNotContains(response, "<h1>Preceptor Schedule</h1>")
        self.assertNotContains(response, "Sign in to work on schedule drafts.")
        self.assertIn('href="/static/schedules/application.css"', content)
        self.assertIn('src="/static/schedules/images/random-health.png"', content)
        self.assertNotIn("primary-navigation", content)
        self.assertNotIn("Signed in as", content)
        self.assertNotIn("Sign out", content)

    def test_shell_has_local_assets_and_a_post_sign_out_form(self):
        response = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})

        self.assert_shell(response)
        self.assert_primary_navigation(response, "Precepting")
        self.assertIsNotNone(finders.find("schedules/application.css"))
        self.assertIsNotNone(finders.find("schedules/images/random-health.png"))
        self.assertContains(response, f'action="{reverse("sign-out")}" method="post"')
        self.assertContains(response, "csrfmiddlewaretoken")

    def test_month_navigation_renders_a_continuous_calendar_without_academic_year_state(self):
        content = render_to_string(
            "schedules/schedule_workspace_month_navigation.html",
            {
                "schedule_workspace_selected_month": date(2026, 1, 1),
                "schedule_workspace_month_min": date(2026, 1, 1),
                "schedule_workspace_month_max": date(2026, 12, 1),
                "schedule_workspace_previous_month_url": "/rounding/?month=2025-12",
                "schedule_workspace_next_month_url": "/rounding/?month=2026-02",
                "schedule_workspace_month_picker_action": "/rounding/",
                "schedule_workspace_month_picker_preserved_query": {"view": "calendar"},
            },
        )
        script = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "schedules"
            / "calendar-period-picker.js"
        ).read_text()

        self.assertIn('action="/rounding/"', content)
        self.assertIn('name="view" value="calendar"', content)
        self.assertIn('name="month" value="2026-01" min="2026-01" max="2026-12"', content)
        self.assertIn('href="/rounding/?month=2025-12"', content)
        self.assertIn('href="/rounding/?month=2026-02"', content)
        self.assertIn('<noscript><button type="submit">View</button></noscript>', content)
        self.assertNotIn("academic_year", content)
        self.assertIn('document.querySelectorAll("[data-calendar-period-picker]")', script)
        self.assertIn("pickerMonth", script)

    def test_header_identifies_muriel_and_fictional_facility(self):
        response = self.client.get(reverse("monthly-workspace"), {"month": "2025-02"})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        parser = _RenderedSurfaceParser()
        parser.feed(content)
        headers = [
            element
            for element in parser.elements
            if element.tag == "header"
            and "site-header" in element.attributes.get("class", "").split()
        ]
        self.assertEqual(len(headers), 1)
        header = headers[0]

        def element_by_class(parent, class_name):
            matches = [
                element
                for element in parent.descendants()
                if class_name in element.attributes.get("class", "").split()
            ]
            self.assertEqual(len(matches), 1)
            return matches[0]

        brand = element_by_class(header, "brand")
        self.assertEqual(brand.tag, "a")
        self.assertEqual(brand.attributes.get("href"), reverse("master-schedule"))
        self.assertEqual(brand.attributes.get("aria-label"), "Muriel Physician Scheduling home")
        images = [element for element in brand.descendants() if element.tag == "img"]
        self.assertEqual(len(images), 1)
        self.assertEqual(
            images[0].attributes.get("src"), "/static/schedules/images/random-health.png"
        )
        self.assertEqual(images[0].attributes.get("alt"), "Random Health")
        self.assertEqual(
            element_by_class(brand, "facility-label").text().strip(), "Family Practice Clinic"
        )
        identity = element_by_class(header, "application-identity")
        self.assertEqual(element_by_class(identity, "application-name").text().strip(), "Muriel")
        self.assertEqual(
            element_by_class(identity, "application-description").text().strip(),
            "Physician Scheduling",
        )

        navigation = re.search(
            r'<nav class="primary-navigation"[^>]*>(.*?)</nav>', content, re.DOTALL
        ).group(1)
        self.assertNotIn("application-identity", navigation)

    def test_muriel_identity_remains_present_in_narrow_screen_styles(self):
        stylesheet = (
            Path(__file__).resolve().parents[1] / "static" / "schedules" / "application.css"
        )
        css = stylesheet.read_text()

        self.assertRegex(css, r"\.application-identity\s*\{[^}]*display:\s*flex;")
        self.assertRegex(
            css,
            re.compile(
                r"@media \(max-width: 700px\) \{.*?\.application-identity\s*\{[^}]*min-width:",
                re.DOTALL,
            ),
        )
        self.assertNotRegex(css, r"\.application-identity\s*\{[^}]*display:\s*none;")

    def test_base_composes_titles_and_child_templates_supply_only_page_names(self):
        templates = Path(__file__).resolve().parents[1] / "templates"
        base = (templates / "schedules" / "base.html").read_text()
        title_block = re.compile(r"{%\s*block\s+title\s*%}(.*?){%\s*endblock\s*%}", re.DOTALL)

        def assert_title_blocks_exclude_application_name(template):
            for title in title_block.findall(template):
                self.assertNotIn("Muriel", title)

        self.assertIn(
            "<title>{% block title %}Physician Scheduling{% endblock %} | Muriel</title>",
            base,
        )
        for template in templates.rglob("*.html"):
            contents = template.read_text()
            if "{% extends" not in contents:
                continue
            with self.subTest(template=template.name):
                assert_title_blocks_exclude_application_name(contents)

        self.client.logout()
        self.assertContains(
            self.client.get(reverse("sign-in")),
            "<title>Sign in | Muriel</title>",
        )
        self.client.force_login(self.user)

        representative_titles = (
            (self.client.get(reverse("monthly-workspace"), {"month": "2025-02"}), "February 2025"),
            (self.client.get(reverse("rounding-workspace")), "Rounding"),
        )
        for response, page_name in representative_titles:
            with self.subTest(page_name=page_name):
                self.assertContains(response, f"<title>{page_name} | Muriel</title>")
                self.assertNotContains(response, "| Muriel | Muriel")

        physician = Physician.objects.create(display_name="Title physician")
        physician_response = self.client.get(reverse("physician-detail", args=(physician.pk,)))
        self.assertContains(physician_response, "<title>Title physician | Muriel</title>")

    def test_stylesheet_uses_shared_semantic_data_table_tones(self):
        with open(finders.find("schedules/application.css")) as stylesheet:
            css = stylesheet.read()

        self.assertIn(
            ".data-table th.data-table-cell--attention, .data-table td.data-table-cell--attention, "
            ".data-table-row--attention > th, .data-table-row--attention > td "
            "{ background: var(--surface-tone-attention); }",
            css,
        )
