"""Real asset transport for test servers, independent of Playwright interception."""

from http.client import HTTPConnection
from urllib.parse import urlsplit

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.templatetags.static import static
from django.test import override_settings


@override_settings(STATIC_ROOT=None)
class BrowserStaticFilesTests(StaticLiveServerTestCase):
    def setUp(self):
        server = urlsplit(self.live_server_url)
        self.connection = HTTPConnection(server.hostname, server.port, timeout=10)
        self.addCleanup(self.connection.close)

    def test_application_assets_are_served_without_collectstatic(self):
        # Content markers identify the assets without freezing their styling or logic.
        assets = (
            ("schedules/application.css", {"text/css"}, b".site-header"),
            (
                "schedules/monthly-annual-window.js",
                {"text/javascript", "application/javascript"},
                b"initializeMonthlyAnnualWindow",
            ),
        )
        for asset, content_types, marker in assets:
            with self.subTest(asset=asset):
                self.connection.request("GET", static(asset))
                with self.connection.getresponse() as response:
                    body = response.read()
                    self.assertEqual(response.status, 200)
                    self.assertIn(response.headers.get_content_type(), content_types)
                    self.assertIn(marker, body)

    def test_missing_asset_returns_not_found(self):
        self.connection.request("GET", static("schedules/nonexistent-browser-test-asset.js"))
        with self.connection.getresponse() as response:
            response.read()
            self.assertEqual(response.status, 404)
