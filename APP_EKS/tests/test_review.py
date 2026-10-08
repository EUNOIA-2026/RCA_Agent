import unittest

from backend.review import analyze_review, load_sample_review


class ReviewTests(unittest.TestCase):
    def test_sample_ticket_is_reported_done_with_exact_text_evidence(self):
        sample = load_sample_review()
        result = analyze_review(sample["ticket"], sample["diff"])

        self.assertEqual(result["summary"], {
            "met": 1,
            "partially_met": 0,
            "missing": 0,
        })
        self.assertEqual(result["verdict"], "Done as per requirements")
        self.assertEqual(result["criteria"][0]["status"], "met")
        self.assertTrue(result["criteria"][0]["evidence"])
        self.assertIn(
            "documentation update draft",
            result["documentation_draft"],
        )
        self.assertIn("grade-validation error", result["user_story"])
        self.assertEqual(
            result["criteria"][0]["evidence"][0]["file"],
            "backend/app.py",
        )
        self.assertTrue(result["notice"])

    def test_exact_text_requirement_is_not_met_when_text_is_absent(self):
        sample = load_sample_review()
        result = analyze_review(
            sample["ticket"],
            sample["diff"].replace(
                "Grade must be A, A+, C, D or F",
                "Grade must be A, B, C, D or F",
            ),
        )

        self.assertEqual(result["criteria"][0]["status"], "missing")
        self.assertEqual(result["verdict"], "Not done")
        self.assertEqual(result["criteria"][0]["evidence"], [])

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

if __name__ == "__main__":
    unittest.main()
