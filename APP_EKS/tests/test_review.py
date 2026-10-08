import unittest

from backend.review import analyze_review, load_sample_review
from app import app


class ReviewTests(unittest.TestCase):
    def test_sample_ticket_produces_all_review_statuses_with_evidence(self):
        sample = load_sample_review()
        result = analyze_review(sample["ticket"], sample["diff"])

        self.assertEqual(result["summary"], {
            "met": 2,
            "partially_met": 2,
            "missing": 1,
        })
        self.assertEqual(
            [criterion["status"] for criterion in result["criteria"]],
            ["met", "met", "partially_met", "partially_met", "missing"],
        )
        self.assertTrue(result["criteria"][0]["evidence"])
        self.assertIn(
            "documentation update draft",
            result["documentation_draft"],
        )
        self.assertIn("As an instructor", result["user_story"])
        self.assertEqual(
            result["criteria"][0]["evidence"][0]["file"],
            "backend/app.py",
        )
        self.assertTrue(result["notice"])

    def test_changed_file_evidence_uses_source_line_numbers(self):
        result = analyze_review(
            "# Ticket\n\n## Acceptance Criteria\n- validate email address",
            changed_files=[{
                "path": "src/validator.py",
                "content": "def validate(value):\n    return value.email\n",
            }],
        )

        self.assertEqual(result["criteria"][0]["status"], "partially_met")
        self.assertEqual(
            result["criteria"][0]["evidence"][0]["line"],
            1,
        )
        self.assertEqual(
            result["criteria"][0]["evidence"][0]["file"],
            "src/validator.py",
        )

    def test_review_api_accepts_sample_diff_and_validates_input(self):
        sample = load_sample_review()
        client = app.test_client()

        response = client.post("/api/reviews", json=sample)
        self.assertEqual(response.status_code, 200)
        self.assertIn("criteria", response.get_json())

        invalid_response = client.post(
            "/api/reviews",
            json={"ticket": "A ticket without acceptance criteria", "diff": "x"},
        )
        self.assertEqual(invalid_response.status_code, 400)
        self.assertIn("Acceptance Criteria", invalid_response.get_json()["error"])

    def test_sample_endpoint_serves_bundled_jira_fixtures(self):
        response = app.test_client().get("/api/reviews/sample")

        self.assertEqual(response.status_code, 200)
        self.assertIn("STU-104", response.get_json()["ticket"])
        self.assertIn("diff --git", response.get_json()["diff"])


if __name__ == "__main__":
    unittest.main()
