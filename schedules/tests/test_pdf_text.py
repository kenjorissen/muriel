"""Shared PDF text boundary: bundled glyphs, Arabic joining and literal user text."""

from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from xml.etree.ElementTree import fromstring

from django.test import SimpleTestCase
from pypdf import PdfReader

from schedules.pdf import _text, build_precepting_calendar_pdf


class PdfTextTests(SimpleTestCase):
    def test_arabic_run_display_order_preserves_surrounding_literal_text(self):
        # Muhammad: initial meem, medial hah, medial meem, final dal (Unicode
        # Arabic Presentation Forms-B). For left-to-right PDF painting, display
        # those four joined forms in reverse order, leaving Latin/numbers alone.
        # Parse markup so equivalent font markup/escaping remains permissible.
        markup = _text('Dr 12 محمد & <7> "name"')
        rendered = "".join(fromstring(f"<span>{markup}</span>").itertext())
        self.assertEqual(rendered, 'Dr 12 \ufeaa\ufee4\ufea4\ufee3 & <7> "name"')

    def test_calendar_embeds_supported_name_fonts_and_keeps_user_markup_literal(self):
        names = (
            "李雷",
            "Dr 12 محمد & <7>",
            "A & B <b>literal</b>",
            '<img src="https://example.invalid/font.png"/>',
        )
        weeks = [
            {
                "days": [
                    {
                        "date": date(2025, 2, 3) + timedelta(days=index),
                        "am": [("Primary", name)],
                        "pm": [],
                        "late_doc": [],
                    }
                    for index, name in enumerate((*names, "Ordinary"))
                ]
            }
        ]
        data = build_precepting_calendar_pdf(
            date(2025, 2, 1), weeks, captured_at=datetime(2025, 2, 1, tzinfo=UTC)
        )
        page = PdfReader(BytesIO(data)).pages[0]
        fragments = []
        text = page.extract_text(
            visitor_text=lambda text, cm, tm, font, size: fragments.append((text, font))
        )
        # Layout extraction preserves painted display order; plain extraction
        # applies bidi again and can reorder neutral punctuation after the run.
        # Check the whole assignment, not disconnected page-wide fragments.
        self.assertIn("Dr 12 ﺪﻤﺤﻣ & <7>", page.extract_text(extraction_mode="layout"))
        for literal in names[2:]:
            self.assertIn(literal, text)
        # Independent expected joined forms for meem-hah-meem-dal. pypdf presents
        # this RTL run in logical order; isolated unshaped letters must not pass.
        for name, family in (("李雷", "NotoSansSC"), ("ﻣﺤﻤﺪ", "NotoNaskhArabic")):
            with self.subTest(name=name):
                self.assertIn(name, text)
                fonts = [font for fragment, font in fragments if name in fragment]
                self.assertTrue(fonts)
                for font in fonts:
                    self.assertIn(family, font["/BaseFont"])
                    self.assertTrue(font["/FontDescriptor"]["/FontFile2"].get_data())
