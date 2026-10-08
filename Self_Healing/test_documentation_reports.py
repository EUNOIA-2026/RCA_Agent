import tempfile
import unittest
from pathlib import Path

from documentation_healer import DocumentationHealer


class DocumentationReportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name)
        self.healer = DocumentationHealer(
            root=base / "repo",
            fix_root=base / "Self_Healing",
            invoke_agent=lambda _: "",
            logger=lambda _: None,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_report(self, report_type, summary):
        return self.healer.write_report(
            event={"kind": "local_code_change"},
            source=[{"path": "APP_EKS/backend/app.py", "status": "modified"}],
            plan={
                "report_type": report_type,
                "decision": "no_update",
                "change_summary": summary,
                "traceability": [{
                    "source_path": "APP_EKS/backend/app.py",
                    "change_summary": summary,
                    "documentation_status": "documented",
                    "document_refs": ["APP_EKS/docs/technical-documentation.md"],
                    "reason": "The docs explain the behavior.",
                }],
                "requirements": [{
                    "ticket": "STU-104",
                    "requirement": "Correct grade validation message.",
                    "implementation_status": "covered",
                    "code_evidence": "backend/app.py",
                    "document_reference": "APP_EKS/tests/jira/STU-104.md",
                }],
            },
            issues=[],
            applied=[],
            version_dir=None,
        )

    def test_functional_report_contains_audit_and_traceability_and_is_overwritten(self):
        first_report = self.write_report("functional", "First change.")
        latest_report = self.write_report("functional", "Latest change.")

        self.assertEqual(
            first_report,
            self.healer.report_root / "functional_changes" / "latest.md",
        )
        self.assertEqual(first_report, latest_report)
        self.assertEqual(
            list((self.healer.report_root / "functional_changes").glob("*.md")),
            [latest_report],
        )
        content = latest_report.read_text(encoding="utf-8")
        self.assertIn("# Functional Change Report", content)
        self.assertIn("Latest change.", content)
        self.assertIn("## Code-to-document mapping", content)
        self.assertIn("## Jira requirement coverage", content)
        self.assertNotIn("First change.", content)

    def test_technical_changes_use_technical_report_folder(self):
        report = self.write_report("technical", "Build configuration change.")

        self.assertEqual(
            report,
            self.healer.report_root / "technical" / "latest.md",
        )
        self.assertIn(
            "# Technical Change Report",
            report.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
