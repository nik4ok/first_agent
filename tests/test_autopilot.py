import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.responder.autopilot import compute_apply_interval, parse_duration_arg, vacancy_matches_keywords
from src.auth.hh_oauth import parse_hh_resume_id
from src.analyzer.matcher import AIResumeAnalyzer


class IntervalTests(unittest.TestCase):
    def test_fifty_over_three_hours(self):
        interval = compute_apply_interval(50, 3.0)
        self.assertAlmostEqual(interval, 216.0, places=1)

    def test_single_item(self):
        interval = compute_apply_interval(1, 3.0)
        self.assertGreaterEqual(interval, 30.0)

    def test_fast_mode_uses_delay(self):
        interval = compute_apply_interval(50, 0, delay_seconds=90)
        self.assertEqual(interval, 90.0)

    def test_minimum_floor(self):
        self.assertGreaterEqual(compute_apply_interval(500, 1.0), 30.0)


class DurationParseTests(unittest.TestCase):
    def test_hours(self):
        self.assertEqual(parse_duration_arg("3h"), 3.0)
        self.assertEqual(parse_duration_arg("3ч"), 3.0)

    def test_minutes(self):
        self.assertEqual(parse_duration_arg("90m"), 1.5)
        self.assertEqual(parse_duration_arg("30мин"), 0.5)

    def test_bare_number_is_hours(self):
        self.assertEqual(parse_duration_arg("2"), 2.0)


class ResumeIdTests(unittest.TestCase):
    def test_from_url(self):
        raw = "https://hh.ru/resume/0123456789abcdef?query=1"
        self.assertEqual(parse_hh_resume_id(raw), "0123456789abcdef")

    def test_plain_id(self):
        self.assertEqual(parse_hh_resume_id("0123456789abcdef"), "0123456789abcdef")


class CoverLetterTests(unittest.TestCase):
    def test_local_letter_uses_role_and_skills(self):
        analyzer = AIResumeAnalyzer()
        analyzer.load_resume_text = lambda: (
            "Желаемая должность: Продуктовый аналитик\n"
            "Senior Product Analyst. Опыт A/B тестирования и SQL в X5.\n"
            "Hard skills: Python, SQL, ClickHouse\n"
            "• поднял конверсию воронки на 18% через A/B"
        )
        letter = analyzer._local_cover_letter(
            "Product Analyst",
            "TestCorp",
            "Нужен SQL и A/B",
            {"matching_skills": ["SQL", "Python"]},
        )
        self.assertIn("TestCorp", letter)
        self.assertIn("SQL", letter)
        self.assertIn("A/B", letter)
        self.assertNotIn("стрессоустойчив", letter.lower())
        self.assertGreater(len(letter), 200)

    def test_local_letter_uses_resume_numbers_for_econ_role(self):
        analyzer = AIResumeAnalyzer()
        letter = analyzer._local_cover_letter(
            "Продуктовый аналитик",
            "FinCorp",
            "Нужен unit-экономист, EBITDA, маржа и финмодель подписки",
            {"matching_skills": ["SQL"]},
        )
        self.assertIn("FinCorp", letter)
        self.assertTrue(any(token in letter for token in ("50 млн", "EBITDA", "16%")))
        self.assertNotIn("буду рад сотрудничеству", letter.lower())

    def test_letter_follows_any_resume_not_hardcoded_x5(self):
        analyzer = AIResumeAnalyzer()
        analyzer.load_resume_text = lambda: (
            "Желаемая должность: Backend Developer\n"
            "Опыт работы\n"
            "Yandex\n"
            "Senior Backend Developer\n"
            "• снизил p99 latency API на 40% на Python и Go\n"
            "• PostgreSQL, Redis, Kubernetes"
        )
        letter = analyzer._local_cover_letter(
            "Backend Developer",
            "Acme",
            "Ищем Python, API, latency, PostgreSQL",
            {"matching_skills": ["Python", "PostgreSQL"]},
        )
        self.assertIn("Acme", letter)
        self.assertIn("Yandex", letter)
        self.assertIn("40%", letter)
        self.assertNotIn("X5", letter)
        self.assertNotIn("EBITDA", letter)
        self.assertNotIn("Сбер", letter)


class KeywordFilterTests(unittest.TestCase):
    def test_comma_and(self):
        self.assertTrue(
            vacancy_matches_keywords(
                "Продуктовый аналитик",
                "PAYNET",
                "SQL и A/B тесты",
                "продуктовый аналитик, SQL",
            )
        )
        self.assertFalse(
            vacancy_matches_keywords(
                "Python Developer",
                "Acme",
                "Django",
                "продуктовый аналитик, SQL",
            )
        )


class SentLogTests(unittest.TestCase):
    def test_dedup_by_vacancy_id(self):
        import tempfile
        from src.parser.applications_log import SentApplicationsLog

        tmp = Path(tempfile.mkdtemp())
        log = SentApplicationsLog(json_path=tmp / "s.json", xlsx_path=tmp / "s.xlsx")
        self.assertTrue(log.record({"vacancy_id": "111", "title": "A", "employer": "X"}))
        self.assertFalse(log.record({"vacancy_id": "111", "title": "A", "employer": "X"}))
        self.assertTrue(log.record({"vacancy_id": "222", "title": "B"}))
        self.assertEqual(log.count(), 2)
        self.assertTrue((tmp / "s.xlsx").exists())


if __name__ == "__main__":
    unittest.main()
