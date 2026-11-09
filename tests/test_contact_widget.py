"""Contact links, MAX fallback and non-invasive analytics hooks."""
from pathlib import Path
import re
import unittest

HTML = Path(__file__).resolve().parents[1] / "web" / "index.html"


class ContactWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text(encoding="utf-8")

    def test_all_call_links_use_real_contact(self):
        self.assertNotIn("tel:+78460000000", self.html)
        self.assertNotIn("+7 (846) 000-00-00", self.html)
        self.assertGreaterEqual(self.html.count('href="tel:{{OWNER_PHONE_E164}}"'), 5)
        self.assertIn('{{OWNER_PHONE}}', self.html)
        self.assertIn("const PHONE = '{{OWNER_PHONE_E164}}';", self.html)

    def test_max_widget_is_accessible_and_has_fallback(self):
        for part in ('id="maxFab"', 'aria-controls="maxCard"',
                     'aria-expanded="false"', 'id="maxCard"',
                     'id="maxCopy"', 'id="maxCopyStatus"',
                     'role="status"', 'aria-live="polite"',
                     'href="https://web.max.ru/"',
                     'max-card[hidden]', 'id="maxProfile"'):
            with self.subTest(part=part):
                self.assertIn(part, self.html)

    def test_profile_not_fabricated_and_tracking_opt_in(self):
        self.assertIn("const MAX_PROFILE_URL = '';", self.html)
        self.assertIn("max_profile_click", self.html)
        self.assertIn("max_widget_open", self.html)
        self.assertIn("max_copy_phone", self.html)
        self.assertIn("phone_click", self.html)
        self.assertIn("byttech:contact", self.html)
        self.assertIn("Array.isArray(window.dataLayer)", self.html)
        self.assertNotRegex(self.html, r"https://max\\.ru/u/79053031551")

if __name__ == "__main__":
    unittest.main()
