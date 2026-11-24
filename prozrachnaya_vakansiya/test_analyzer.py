import json
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from main import extract_vacancies, build_report, fetch_api
from analyzer import analyze, vacancy_fields


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class AnalyzerTests(unittest.TestCase):
    def test_api_wrapper_and_demo_report(self):
        data = json.loads(Path("sample_data/example_api.json").read_text(encoding="utf-8"))
        jobs = extract_vacancies(data)
        self.assertEqual(len(jobs), 2)
        report = build_report(jobs, use_ai=False)
        self.assertEqual(report["vacancies_count"], 2)
        self.assertEqual(report["vacancies"][0]["id"], "demo-001")
        self.assertTrue(any(x["topic"] == "График" for x in report["vacancies"][0]["issues"]))
        self.assertEqual(report["vacancies"][1]["issues"], [])

    def test_missing_quote_is_empty(self):
        result = analyze({"vacancy": {"id": "x", "job-name": "Тест"}}, use_ai=False)
        missing = [x for x in result["issues"] if x["topic"] == "Оплата"]
        self.assertEqual(missing[0]["quote"], "")

    def test_salary_bounds_and_currency_are_combined(self):
        _, fields = vacancy_fields({
            "vacancy": {"salary_min": 45000, "salary_max": 60000, "currency": "руб."}
        })
        self.assertEqual(fields["salary"], "45000–60000 руб.")

    def test_nested_address_array_is_readable(self):
        _, fields = vacancy_fields({
            "vacancy": {"addresses": {"address": [{"location": "Калининград"}]}}
        })
        self.assertEqual(fields["location"], "Калининград")

    def test_ambiguous_schedule_in_description_is_detected(self):
        result = analyze({
            "vacancy": {
                "id": "schedule-1",
                "job-name": "Оператор",
                "description": "Предлагаем гибкий график работы",
            }
        }, use_ai=False)
        self.assertTrue(any(
            issue["topic"] == "График" and issue["quote"] == "гибкий график"
            for issue in result["issues"]
        ))

    def test_missing_ids_do_not_collapse_distinct_vacancies(self):
        report = build_report([
            {"vacancy": {"job-name": "Первый"}},
            {"vacancy": {"job-name": "Второй"}},
        ], use_ai=False)
        self.assertEqual(report["vacancies_count"], 2)

    def test_fetch_api_uses_page_number_offset(self):
        pages = [
            {"status": "200", "results": {"vacancies": [
                {"vacancy": {"id": "1"}}, {"vacancy": {"id": "2"}}
            ]}},
            {"status": "200", "results": {"vacancies": [
                {"vacancy": {"id": "3"}}
            ]}},
        ]
        urls = []

        def fake_urlopen(request, timeout=30):
            urls.append(request.full_url)
            return FakeResponse(pages[len(urls) - 1])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = fetch_api("http://example.test/vacancies/region/39", 3, 2)

        self.assertEqual([item["vacancy"]["id"] for item in result], ["1", "2", "3"])
        self.assertIn("offset=0", urls[0])
        self.assertIn("offset=1", urls[1])

    def test_page_size_cannot_exceed_api_limit(self):
        with self.assertRaises(ValueError):
            fetch_api("http://example.test/api", 10, 101)


if __name__ == "__main__":
    unittest.main()
