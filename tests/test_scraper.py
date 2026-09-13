import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Unit tests exercise local filtering and CSV behavior; they do not need the
# optional HTTP client installed by the GitHub Actions runtime.
sys.modules.setdefault("requests", unittest.mock.Mock())

import scraper


class JobScoringTests(unittest.TestCase):
    def test_relevant_internship_meets_threshold(self):
        self.assertEqual(scraper.score_job("Electrical Engineering Intern"), 30)
        self.assertTrue(scraper.matches_filters("Electrical Engineering Intern"))

    def test_generic_internship_does_not_meet_threshold(self):
        self.assertEqual(scraper.score_job("Business Operations Intern"), 10)
        self.assertFalse(scraper.matches_filters("Business Operations Intern"))

    def test_description_terms_can_make_an_engineering_intern_relevant(self):
        evaluation = scraper.evaluate_job(
            "Engineering Intern",
            "Support substation protection and relay-control projects.",
        )
        self.assertEqual(evaluation["title_score"], 10)
        self.assertEqual(evaluation["description_score"], 30)
        self.assertEqual(evaluation["match_score"], 40)
        self.assertIn("description: substation (+12)", evaluation["match_reasons"])

    def test_description_cannot_bypass_title_role_requirement(self):
        self.assertIsNone(
            scraper.evaluate_job(
                "Electrical Engineer",
                "Work on power systems, substations, and protection relays.",
            )
        )

    def test_html_description_is_normalized_before_matching(self):
        self.assertEqual(
            scraper.html_to_text("<p>Power&nbsp;systems<br>and relays</p>"),
            "Power systems and relays",
        )

    def test_specific_phrase_does_not_double_count_its_shorter_keyword(self):
        score, reasons = scraper.score_keyword_matches(
            "Protection and control intern",
            {"protection": 10, "protection and control": 12},
            "description",
        )
        self.assertEqual(score, 12)
        self.assertEqual(reasons, ["description: protection and control (+12)"])

    def test_excluded_title_is_rejected_regardless_of_score(self):
        self.assertIsNone(scraper.score_job("Senior Embedded Systems Intern"))

    def test_posting_key_includes_company_and_source(self):
        posting = {"source": "greenhouse", "company": "Example", "job_id": "42"}
        self.assertEqual(scraper.posting_key(posting), "greenhouse:Example:42")


class CsvMigrationTests(unittest.TestCase):
    def test_existing_log_is_migrated_to_include_score(self):
        with tempfile.TemporaryDirectory() as directory:
            data_file = Path(directory) / "postings.csv"
            data_file.write_text(
                "job_id,company,source,title,location,url,date_found\n"
                "42,Example,greenhouse,Electrical Intern,Remote,https://example.com,2026-08-05\n",
                encoding="utf-8",
            )

            with patch.object(scraper, "DATA_FILE", data_file):
                scraper.ensure_posting_schema()

            with data_file.open(newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(rows[0]["match_score"], "")


class CompanyConfigurationTests(unittest.TestCase):
    def test_load_companies_defaults_enabled_to_true(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "companies.json"
            config_file.write_text(
                json.dumps([{"name": "Example", "platform": "lever", "slug": "example"}]),
                encoding="utf-8",
            )
            self.assertEqual(
                scraper.load_companies(config_file),
                [{"name": "Example", "platform": "lever", "slug": "example", "enabled": True}],
            )

    def test_load_companies_rejects_missing_workday_field(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "companies.json"
            config_file.write_text(
                json.dumps([{"name": "Example", "platform": "workday", "tenant": "example", "site": "careers"}]),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "wd_server"):
                scraper.load_companies(config_file)


class RequestRetryTests(unittest.TestCase):
    def test_retries_transient_server_error_then_returns_response(self):
        first_response = unittest.mock.Mock(status_code=503, headers={})
        second_response = unittest.mock.Mock(status_code=200, headers={})
        with patch.object(scraper.requests, "request", side_effect=[first_response, second_response]) as request:
            with patch.object(scraper.time, "sleep") as sleep:
                response = scraper.request_with_retries("get", "https://example.com", max_attempts=2, backoff_seconds=0.5)

        self.assertIs(response, second_response)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(0.5)

    def test_does_not_retry_non_transient_client_error(self):
        response = unittest.mock.Mock(status_code=404, headers={})
        with patch.object(scraper.requests, "request", return_value=response) as request:
            with patch.object(scraper.time, "sleep") as sleep:
                result = scraper.request_with_retries("get", "https://example.com")

        self.assertIs(result, response)
        request.assert_called_once()
        sleep.assert_not_called()


class GeminiFailoverTests(unittest.TestCase):
    def test_503_unavailable_routes_to_the_next_model(self):
        router = scraper.GeminiModelRouter(
            (("first-model", 60), ("second-model", 60)),
            clock=lambda: 0,
            sleeper=lambda _: None,
        )
        unavailable = Exception("503 UNAVAILABLE: model is experiencing high demand")
        response = unittest.mock.Mock(
            text='{"is_entry_level_or_intern": true, "is_relevant_engineering": true, "match_score": 85, "match_reasons": "Good fit"}'
        )

        with patch.object(scraper, "genai") as genai, patch.dict(
            "os.environ", {"GEMINI_API_KEY": "test-key"}, clear=False
        ):
            client = genai.Client.return_value
            client.models.generate_content.side_effect = [unavailable, response]
            result = scraper.evaluate_job_with_gemini(
                "Electrical Engineering Intern",
                "Power systems internship",
                keyword_evaluation={"match_score": 40, "match_reasons": "Keyword fit"},
                router=router,
            )

        self.assertEqual(result["match_score"], 85)
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertEqual(client.models.generate_content.call_args_list[0].kwargs["model"], "first-model")
        self.assertEqual(client.models.generate_content.call_args_list[1].kwargs["model"], "second-model")

    def test_secondary_key_is_used_after_primary_routes_are_unavailable(self):
        router = scraper.GeminiModelRouter(
            (("shared-model", 60),),
            api_keys=("primary-key", "secondary-key"),
            clock=lambda: 0,
            sleeper=lambda _: None,
        )
        primary_client = unittest.mock.Mock()
        secondary_client = unittest.mock.Mock()
        primary_client.models.generate_content.side_effect = [
            Exception("503 UNAVAILABLE: model is experiencing high demand")
        ]
        secondary_client.models.generate_content.return_value = unittest.mock.Mock(
            text='{"is_entry_level_or_intern": true, "is_relevant_engineering": true, "match_score": 85, "match_reasons": "Good fit"}'
        )

        with patch.object(scraper, "genai") as genai, patch.dict(
            "os.environ",
            {"GEMINI_API_KEY": "primary-key", "GEMINI_API_KEY_SECONDARY": "secondary-key"},
            clear=False,
        ):
            genai.Client.side_effect = [primary_client, secondary_client]
            result = scraper.evaluate_job_with_gemini(
                "Electrical Engineering Intern",
                "Power systems internship",
                keyword_evaluation={"match_score": 40, "match_reasons": "Keyword fit"},
                router=router,
            )

        self.assertEqual(result["match_score"], 85)
        self.assertEqual(genai.Client.call_args_list[0].kwargs["api_key"], "primary-key")
        self.assertEqual(genai.Client.call_args_list[1].kwargs["api_key"], "secondary-key")
        primary_client.models.generate_content.assert_called_once()
        secondary_client.models.generate_content.assert_called_once()


if __name__ == "__main__":
    unittest.main()
