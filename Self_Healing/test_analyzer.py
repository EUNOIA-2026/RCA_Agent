import json
import tempfile
import unittest
from pathlib import Path

from analyzer import (
    analyze_ticket,
    is_application_source,
    load_diff,
    output_paths,
    parse_agent_result,
    redact_sensitive_text,
    ticket_key,
)


class AnalyzerTests(unittest.TestCase):
    def test_ticket_key_is_taken_from_content(self):
        self.assertEqual(ticket_key(Path("ticket.md"), "# STU-104: Fix grade"), "STU-104")

    def test_output_paths_cover_both_classifications(self):
        paths = output_paths(Path("reports"), "STU-104", "BOTH TECHNICAL AND FUNCTIONAL")
        self.assertEqual(
            [path.name for path in paths],
            ["functional_STU-104.md", "technical_STU-104.md"],
        )

    def test_agent_result_requires_classification_and_markdown(self):
        result = json.dumps({
            "classification": "FUNCTIONAL",
            "report_markdown": "# Application Code Change Analysis\n",
        })
        self.assertEqual(parse_agent_result(result)[0], "FUNCTIONAL")
        with self.assertRaises(ValueError):
            parse_agent_result('{"classification":"UNKNOWN","report_markdown":"x"}')

    def test_configuration_and_security_paths_are_not_application_context(self):
        self.assertTrue(is_application_source("backend/review.py"))
        self.assertFalse(is_application_source("backend/config/settings.py"))
        self.assertFalse(is_application_source("backend/security/tokens.py"))
        self.assertFalse(is_application_source("deploy/values.yaml"))

    def test_sensitive_literals_are_redacted(self):
        text = redact_sensitive_text('API_KEY = "private-value"')
        self.assertNotIn("private-value", text)
        self.assertIn("[REDACTED]", text)

    def test_supplied_diff_excludes_configuration_and_redacts_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "change.diff"
            path.write_text(
                "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
                "@@ -1 +1 @@\n-old()\n+new()\n"
                "diff --git a/config/settings.py b/config/settings.py\n"
                "--- a/config/settings.py\n+++ b/config/settings.py\n"
                "@@ -1 +1 @@\n-API_KEY = 'old'\n+API_KEY = 'sensitive-value'\n",
                encoding="utf-8",
            )
            diff, changed = load_diff(path)
            self.assertEqual(changed, ["app.py"])
            self.assertNotIn("settings.py", diff)
            self.assertNotIn("sensitive-value", diff)
            self.assertIn("excluded from analysis", diff)

    def test_end_to_end_writes_both_reports_without_azure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = Path(__file__).resolve().parents[1]
            repo = workspace / "APP_EKS"
            ticket = repo / "tests/jira/STU-104.md"
            diff = repo / "tests/jira/STU-104.diff"
            agent_response = json.dumps({
                "classification": "BOTH TECHNICAL AND FUNCTIONAL",
                "report_markdown": "# Application Code Change Analysis\n",
            })

            def fake_agent(prompt):
                self.assertIn("STU-104", prompt)
                self.assertIn("Grade must be A, A+, C, D or F", prompt)
                self.assertIn("CHANGED SOURCE: backend/app.py", prompt)
                return agent_response

            paths = analyze_ticket(
                ticket, repo, "HEAD", root / "reports", diff_path=diff,
                invoke_agent=fake_agent,
            )
            self.assertEqual(
                [path.name for path in paths],
                ["functional_STU-104.md", "technical_STU-104.md"],
            )
            self.assertTrue(all(path.read_text(encoding="utf-8").startswith("# Application") for path in paths))


if __name__ == "__main__":
    unittest.main()