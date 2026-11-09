"""The services section keeps the eight approved marked-up reference prices."""
import re
import unittest
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "web" / "index.html"

class ServicePricesTests(unittest.TestCase):
    def test_service_prices_and_markup(self):
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('id="services"', html)
        self.assertIn('id="prices"', html)
        section = html.split('<!-- PRICES / SERVICES -->', 1)[1].split('<!-- COMPLAINTS -->', 1)[0]
        rows = re.findall(r'<div class="service-row" role="listitem">(.+?)</div>', section)
        self.assertEqual(len(rows), 8)
        expected = (
            ("Не вращается барабан", 1250),
            ("Не нагревает воду", 1300),
            ("Протекает стиральная машина", 1350),
            ("Не сливает воду", 1300),
            ("Не отжимает", 1400),
            ("Сильно шумит", 1250),
            ("Не включается", 1300),
            ("Не набирает воду", 1300),
        )
        for row, (name, value) in zip(rows, expected):
            row = " ".join(row.split())
            self.assertIn(f"<h3>{name}</h3>", row)
            self.assertIn(f"от {value:,}".replace(",", " ") + " ₽", row)

    def test_detailed_work_prices_and_original_features_preserved(self):
        html = HTML.read_text(encoding="utf-8")
        section = html.split('<!-- PRICES / SERVICES -->', 1)[1].split('<!-- COMPLAINTS -->', 1)[0]
        self.assertEqual(section.count('class="price-card"'), 11)
        self.assertIn("Диагностика при последующем ремонте", section)
        self.assertIn("Замена подшипников", section)
        self.assertIn('class="price-val free">бесплатно', section)
        self.assertIn('href="#quiz"', section)
        self.assertIn('name="preferredDate"', html)
        self.assertIn('name="preferredTime"', html)

if __name__ == "__main__":
    unittest.main()
