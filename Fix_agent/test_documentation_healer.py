import tempfile
import unittest
import json
import subprocess
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from documentation_healer import DocumentationHealer


class DocumentationHealerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.temp.name)
        self.fix_root = self.root / "fix_agent"
        self.fix_root.mkdir()
        self.healer = DocumentationHealer(self.root, self.fix_root, lambda _: "{}", lambda _: None)

    def tearDown(self):
        self.temp.cleanup()

    def test_source_discovery_includes_java_python_and_skips_generated_dirs(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "SparkJob.py").write_text("from pyspark.sql import SparkSession\n", encoding="utf-8")
        (self.root / "src" / "Service.java").write_text("class Service {}\n", encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "ignored.py").write_text("ignored", encoding="utf-8")
        (self.fix_root / "ignored.py").write_text("ignored", encoding="utf-8")

        snapshot = self.healer.source_snapshot()

        self.assertEqual(set(snapshot), {"src/SparkJob.py", "src/Service.java"})

    def test_patch_validation_and_version_backup(self):
        docs = self.root / "docs"
        docs.mkdir()
        target = docs / "technical.md"
        target.write_text("Overview\nOld section\nEnd\n", encoding="utf-8")
        source = [{"path": "src/job.py", "status": "modified"}]
        plan = {
            "decision": "update",
            "risk_level": "low",
            "changes": [{"path": "docs/technical.md", "old_text": "Old section", "new_text": "New section", "why": "Document change"}],
            "traceability": [{"source_path": "src/job.py", "documentation_status": "documented", "document_refs": ["docs/technical.md"], "reason": "Updated overview"}],
            "requirements": [],
            "undocumented_changes": [],
            "validation": {"all_code_changes_documented": True, "jira_requirements_covered": True},
        }

        self.assertEqual(self.healer.validate_plan(plan, source, {"docs/technical.md"}, []), [])
        version = self.fix_root / "documentation_versions" / "v1"
        applied = self.healer.apply_patches(plan["changes"], version)

        self.assertEqual(target.read_text(encoding="utf-8"), "Overview\nNew section\nEnd\n")
        self.assertEqual((version / "docs" / "technical.md").read_text(encoding="utf-8"), "Overview\nOld section\nEnd\n")
        self.assertEqual(applied[0]["path"], "docs/technical.md")

    def test_unsafe_or_non_unique_patch_is_rejected(self):
        source = [{"path": "src/job.py", "status": "modified"}]
        plan = {
            "decision": "update",
            "risk_level": "medium",
            "changes": [{"path": "../outside.md", "old_text": "x", "new_text": "y"}],
            "traceability": [{"source_path": "src/job.py", "documentation_status": "documented", "document_refs": ["docs/technical.md"], "reason": "Updated"}],
            "requirements": [],
            "undocumented_changes": [],
            "validation": {"all_code_changes_documented": True},
        }
        issues = self.healer.validate_plan(plan, source, {"docs/technical.md"}, [])
        self.assertTrue(any("Unsafe documentation path" in issue for issue in issues))

    def test_missing_source_traceability_fails_validation(self):
        plan = {
            "decision": "no_update", "risk_level": "low", "changes": [],
            "traceability": [], "requirements": [], "undocumented_changes": [],
            "validation": {"all_code_changes_documented": True},
        }
        issues = self.healer.validate_plan(plan, [{"path": "src/job.py"}], set(), [])
        self.assertTrue(any("missing from traceability" in issue for issue in issues))

    def test_initialize_watch_preserves_saved_snapshot_for_restart(self):
        documentation = {
            "local_snapshot": {"src/job.py": "previous-content-hash"},
            "local_head": "previous-head",
            "last_pr_poll_at": "2026-01-01T00:00:00+00:00",
        }
        self.healer.state_file.write_text(
            json.dumps({"documentation": documentation}), encoding="utf-8"
        )

        self.healer.initialize_watch()

        saved = json.loads(self.healer.state_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["documentation"]["local_snapshot"], {"src/job.py": "previous-content-hash"})
        self.assertEqual(saved["documentation"]["local_head"], "previous-head")

    def test_local_changes_wait_for_github_token_without_repeating_warning(self):
        source = self.root / "src"
        source.mkdir()
        (source / "job.py").write_text("value = 2\n", encoding="utf-8")
        self.healer._save_state({
            "documentation": {
                "local_snapshot": {"src/job.py": "previous-hash"},
                "local_head": "previous-head",
            }
        })
        self.healer.github_repository = lambda: "org/repo"

        def unexpected_event(event):
            self.fail(f"Must not process event {event.get('source_ref')} without PR credentials")

        self.healer.process_event = unexpected_event
        messages = []
        self.healer.log = messages.append

        with patch.dict("os.environ", {}, clear=True):
            self.healer.poll_local_changes()
            self.healer.poll_local_changes()

        self.assertEqual(len(messages), 1)
        self.assertIn("no documentation files were changed", messages[0].lower())
        saved = self.healer._state()["documentation"]
        self.assertEqual(saved["local_snapshot"], {"src/job.py": "previous-hash"})

    def test_local_changes_pause_after_github_rejects_token(self):
        source = self.root / "src"
        source.mkdir()
        (source / "job.py").write_text("value = 2\n", encoding="utf-8")
        self.healer._save_state({
            "documentation": {
                "local_snapshot": {"src/job.py": "previous-hash"},
                "local_head": "previous-head",
            }
        })
        self.healer.github_repository = lambda: "org/repo"
        requests_seen = []

        def reject_token(method, url, **kwargs):
            requests_seen.append(url)
            raise RuntimeError("GitHub API returned HTTP 401")

        self.healer._github_request = reject_token

        def unexpected_event(event):
            self.fail(f"Must not analyze {event.get('source_ref')} after GitHub rejects token")

        self.healer.process_event = unexpected_event
        messages = []
        self.healer.log = messages.append
        with patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"}, clear=True):
            self.healer.poll_local_changes()
            self.healer.poll_local_changes()

        self.assertEqual(len(requests_seen), 1)
        self.assertEqual(len(messages), 1)
        self.assertIn("HTTP 401", messages[0])
        self.assertEqual(
            self.healer._state()["documentation"]["local_snapshot"],
            {"src/job.py": "previous-hash"},
        )

    def test_missing_github_token_prevents_document_patch(self):
        docs = self.root / "docs"
        docs.mkdir()
        target = docs / "technical.md"
        target.write_text("Old section\n", encoding="utf-8")
        source = self.root / "src"
        source.mkdir()
        (source / "job.py").write_text("value = 2\n", encoding="utf-8")
        plan = {
            "decision": "update", "risk_level": "low", "changes": [{
                "path": "docs/technical.md", "old_text": "Old section",
                "new_text": "New section", "why": "Reflect code change",
            }],
            "traceability": [{
                "source_path": "src/job.py", "documentation_status": "documented",
                "document_refs": ["docs/technical.md"], "reason": "Section updated",
            }],
            "requirements": [], "undocumented_changes": [],
            "validation": {"all_code_changes_documented": True},
        }
        def fake_agent(prompt):
            self.assertIn("docs/technical.md", prompt)
            return json.dumps(plan)

        self.healer.invoke_agent = fake_agent
        self.healer.github_repository = lambda: "org/repo"
        self.healer._github_request = lambda method, url, **kwargs: {"default_branch": "main"}

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "set GITHUB_TOKEN or GH_TOKEN"):
                self.healer.process_event({
                    "kind": "local_code_change", "source_ref": "test",
                    "changed_files": [{"path": "src/job.py", "status": "modified"}],
                    "jira_keys": [], "jira_tickets": [],
                })

        self.assertEqual(target.read_text(encoding="utf-8"), "Old section\n")

    def test_pr_base_uses_current_branch_and_never_substitutes_another_branch(self):
        head = "shared-commit"
        checked_refs = []

        def fake_git(args, check=False):
            if args == ["branch", "--show-current"]:
                return SimpleNamespace(stdout="features/selfheal\n", returncode=0)
            if args[:3] == ["show-ref", "--verify", "--quiet"]:
                checked_refs.append(args[3])
                return SimpleNamespace(stdout="", returncode=0 if args[3] == "refs/remotes/origin/features/selfheal" else 1)
            raise AssertionError(args)

        self.healer.git = fake_git
        self.healer.head = lambda: head

        self.assertEqual(self.healer._select_pr_base_branch({}), "features/selfheal")
        self.assertEqual(checked_refs, ["refs/remotes/origin/features/selfheal"])

    def test_pr_base_fails_when_current_branch_is_not_published(self):
        checked_refs = []

        def fake_git(args, check=False):
            if args == ["branch", "--show-current"]:
                return SimpleNamespace(stdout="features/selfheal\n", returncode=0)
            if args[:3] == ["show-ref", "--verify", "--quiet"]:
                checked_refs.append(args[3])
                return SimpleNamespace(stdout="", returncode=1)
            raise AssertionError(args)

        self.healer.git = fake_git

        with self.assertRaisesRegex(RuntimeError, "will not substitute another branch"):
            self.healer._select_pr_base_branch({})

        self.assertEqual(checked_refs, ["refs/remotes/origin/features/selfheal"])

    def test_redaction_removes_common_credentials_and_personal_data(self):
        text = (
            'API_KEY="demo-value" Authorization: Bearer demo-token '
            "contact dev@example.com"
        )

        redacted = self.healer.redact_sensitive_text(text)

        self.assertNotIn("demo-value", redacted)
        self.assertNotIn("demo-token", redacted)
        self.assertNotIn("dev@example.com", redacted)
        self.assertIn("[REDACTED]", redacted)


    def test_process_event_updates_only_document_and_writes_reports(self):
        docs = self.root / "docs"
        docs.mkdir()
        target = docs / "technical.md"
        target.write_text("Overview\nOld section\nEnd\n", encoding="utf-8")
        plan = {
            "decision": "update", "risk_level": "low", "impact_summary": "A module changed.",
            "impacted_modules": ["service"], "change_categories": ["business_logic"],
            "changes": [{"path": "docs/technical.md", "old_text": "Old section", "new_text": "New section", "why": "Keep docs current"}],
            "change_summary": "Updated technical behavior.",
            "traceability": [{"source_path": "src/job.py", "change_summary": "Updated logic", "documentation_status": "documented", "document_refs": ["docs/technical.md"], "reason": "Section updated"}],
            "requirements": [], "undocumented_changes": [],
            "validation": {"all_code_changes_documented": True, "jira_requirements_covered": True},
        }
        def fake_agent(prompt):
            self.assertIn("RETRIEVED DOCUMENTATION", prompt)
            return json.dumps(plan)

        self.healer.invoke_agent = fake_agent
        self.healer._github_request = lambda method, url, **kwargs: {"default_branch": "main"}

        def fake_publish(event, proposed_plan, applied):
            self.assertEqual(event["source_ref"], "test")
            self.assertEqual(proposed_plan["decision"], "update")
            self.assertEqual(applied[0]["path"], "docs/technical.md")
            return {
                "url": "https://example/pr/13", "branch": "docs/self-healing/test",
                "status": "auto-merge enabled after required approval",
            }

        self.healer.publish_documentation_pr = fake_publish
        with patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"}):
            self.healer.process_event({
                "kind": "local_code_change", "source_ref": "test", "jira_keys": [], "jira_tickets": [],
                "changed_files": [{"path": "src/job.py", "status": "modified", "diff": "-old\n+new", "current_content": "new"}],
            })

        self.assertIn("New section", target.read_text(encoding="utf-8"))
        self.assertTrue(list((self.fix_root / "documentation_versions").rglob("technical.md")))
        self.assertTrue(list((self.fix_root / "reports" / "documentation").glob("audit-*.md")))
        self.assertTrue(list((self.fix_root / "reports" / "documentation").glob("traceability-*.md")))
        audit = next((self.fix_root / "reports" / "documentation").glob("audit-*.md")).read_text(encoding="utf-8")
        self.assertIn("https://example/pr/13", audit)

    def test_auto_merge_requires_at_least_one_approval(self):
        requests_seen = []

        def fake_github_request(method, url, **kwargs):
            requests_seen.append((method, url, kwargs))
            if url.endswith("/protection"):
                return {"required_pull_request_reviews": {"required_approving_review_count": 1}}
            return {"data": {"enablePullRequestAutoMerge": {"pullRequest": {"number": 12}}}}

        self.healer._github_request = fake_github_request
        enabled = self.healer._enable_auto_merge_after_approval(
            "org/repo", "main", {"node_id": "PR_node_id"}
        )

        self.assertTrue(enabled)
        self.assertEqual(len(requests_seen), 2)
        self.assertIn("graphql", requests_seen[1][1])

    def test_github_api_uses_configured_token(self):
        with patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"}, clear=True):
            headers = self.healer._github_headers()

        self.assertEqual(headers["Authorization"], "Bearer test-token")

    def test_auto_merge_is_not_enabled_without_required_review_protection(self):
        requests_seen = []

        def fake_github_request(method, url, **kwargs):
            requests_seen.append((method, url, kwargs))
            return {"required_pull_request_reviews": {"required_approving_review_count": 0}}

        self.healer._github_request = fake_github_request
        enabled = self.healer._enable_auto_merge_after_approval(
            "org/repo", "main", {"node_id": "PR_node_id"}
        )

        self.assertFalse(enabled)
        self.assertEqual(len(requests_seen), 1)

    def test_publish_documentation_pr_creates_branch_and_review_request(self):
        requests_seen = []
        branch_args = []

        (self.root / "docs").mkdir()
        (self.root / "docs" / "technical.md").write_text("Updated docs\n", encoding="utf-8")
        self.healer.github_repository = lambda: "org/repo"
        self.healer._select_pr_base_branch = lambda event: "feature/docs"

        def fake_git(args, check=False):
            if check:
                self.assertTrue(args)
            return SimpleNamespace(
                stdout="feature/docs\n" if args == ["branch", "--show-current"] else "",
                returncode=0,
            )

        def fake_create_branch(base, branch, docs, sources, extra_documents):
            branch_args.append((base, branch, docs, sources, extra_documents))

        def fake_enable_auto_merge(repository, base, pull_request):
            self.assertEqual(repository, "org/repo")
            self.assertEqual(base, "feature/docs")
            self.assertEqual(pull_request["node_id"], "PR_node_id")
            return True

        self.healer.git = fake_git
        self.healer._create_documentation_branch = fake_create_branch
        self.healer._enable_auto_merge_after_approval = fake_enable_auto_merge

        def fake_github_request(method, url, **kwargs):
            requests_seen.append((method, url, kwargs))
            if method == "GET":
                return []
            return {"html_url": "https://example/pr/14", "node_id": "PR_node_id"}

        self.healer._github_request = fake_github_request
        event = {
            "kind": "local_code_change", "source_ref": "test-change",
            "changed_files": [{"path": "src/job.py", "status": "modified"}],
        }
        plan = {"change_summary": "Updated job behavior"}
        applied = [{"path": "docs/technical.md", "why": "Updated behavior"}]

        with patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"}):
            result = self.healer.publish_documentation_pr(event, plan, applied)

        self.assertEqual(result["url"], "https://example/pr/14")
        self.assertEqual(result["status"], "auto-merge enabled after required approval")
        self.assertEqual(branch_args[0][0], "feature/docs")
        self.assertEqual(branch_args[0][2], applied)
        self.assertEqual(branch_args[0][3], event["changed_files"])
        self.assertEqual(branch_args[0][4], [])
        self.assertEqual([item[0] for item in requests_seen], ["GET", "POST"])

    def test_documentation_branch_push_contains_local_source_and_docs_without_switching_user_branch(self):
        def run_git(*args, cwd=None):
            return subprocess.run(
                ["git", *args], cwd=cwd, check=True,
                capture_output=True, text=True,
            ).stdout.strip()

        repo = self.root / "repo"
        repo.mkdir()
        remote = self.root / "origin.git"
        fix_root = repo / "Fix_agent"
        (repo / "src").mkdir()
        (repo / "docs").mkdir()
        (repo / "Fix_agent").mkdir()
        (repo / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
        (repo / "docs" / "technical.md").write_text("Old behavior\n", encoding="utf-8")

        run_git("init", "--bare", str(remote))
        run_git("init", "-b", "main", str(repo))
        run_git("config", "user.name", "Test User", cwd=repo)
        run_git("config", "user.email", "test@example.com", cwd=repo)
        run_git("add", "src/app.py", "docs/technical.md", cwd=repo)
        run_git("commit", "-m", "initial", cwd=repo)
        run_git("remote", "add", "origin", str(remote), cwd=repo)
        run_git("push", "-u", "origin", "main", cwd=repo)

        (repo / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        (repo / "docs" / "technical.md").write_text("New behavior\n", encoding="utf-8")
        healer = DocumentationHealer(repo, fix_root, lambda _: "{}", lambda _: None)
        applied = [{"path": "docs/technical.md", "why": "Describe new behavior"}]
        changed_sources = [{"path": "src/app.py", "status": "modified"}]

        healer._create_documentation_branch(
            "main", "docs/self-healing/integration-test", applied, changed_sources, []
        )

        branch_files = run_git(
            "--git-dir", str(remote), "ls-tree", "-r", "--name-only",
            "refs/heads/docs/self-healing/integration-test",
        ).splitlines()
        source_content = run_git(
            "--git-dir", str(remote), "show",
            "refs/heads/docs/self-healing/integration-test:src/app.py",
        )
        docs_content = run_git(
            "--git-dir", str(remote), "show",
            "refs/heads/docs/self-healing/integration-test:docs/technical.md",
        )
        self.assertEqual(set(branch_files), {"src/app.py", "docs/technical.md"})
        self.assertEqual(source_content, "value = 2")
        self.assertEqual(docs_content, "New behavior")
        self.assertEqual(run_git("branch", "--show-current", cwd=repo), "main")
        self.assertEqual(
            set(run_git("diff", "--name-only", cwd=repo).splitlines()),
            {"src/app.py", "docs/technical.md"},
        )

    def test_no_document_patch_still_opens_pr_for_local_source_change(self):
        docs = self.root / "docs"
        docs.mkdir()
        target = docs / "technical.md"
        target.write_text("Current behavior\n", encoding="utf-8")
        self.healer.invoke_agent = lambda prompt: json.dumps({
            "decision": "no_update", "risk_level": "low", "changes": [],
            "traceability": [{
                "source_path": "src/job.py", "documentation_status": "documented",
                "document_refs": ["docs/technical.md"], "reason": "Existing documentation is accurate",
            }],
            "requirements": [], "undocumented_changes": [],
            "validation": {"all_code_changes_documented": True},
        })
        self.healer.github_repository = lambda: "org/repo"
        self.healer._github_request = lambda method, url, **kwargs: {"default_branch": "main"}
        published = []

        def fake_publish(event, plan, applied, extra_documents):
            published.append((event, plan, applied, extra_documents))
            return {
                "url": "https://example/pr/15", "branch": "docs/self-healing/test",
                "status": "review required; auto-merge unavailable",
            }

        self.healer.publish_documentation_pr = fake_publish
        with patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"}):
            self.healer.process_event({
                "kind": "local_code_change", "source_ref": "test",
                "changed_files": [{"path": "src/job.py", "status": "modified"}],
                "jira_keys": [], "jira_tickets": [],
            })

        self.assertEqual(len(published), 1)
        self.assertEqual(published[0][2], [])
        self.assertEqual(published[0][3], ["docs/technical.md"])

    def test_pull_request_event_extracts_jira_and_all_file_statuses(self):
        def fake_github(url, params=None):
            if params is not None:
                self.assertIn("per_page", params)
            if url.endswith("/pulls/12"):
                return {"number": 12, "title": "ABC-42 update Spark job", "body": "Implements ABC-42", "head": {"ref": "feature/ABC-42"}, "base": {"ref": "main"}, "html_url": "https://example/pr/12", "merged_at": "2026-10-08T10:00:00Z", "merge_commit_sha": "deadbeef"}
            if url.endswith("/commits"):
                return [{"commit": {"message": "ABC-42 update"}}]
            if url.endswith("/files"):
                return [
                    {"filename": "jobs/spark_job.py", "status": "modified", "patch": "@@ -1 +1 @@", "sha": "blob"},
                    {"filename": "docs/functional.md", "status": "modified", "additions": 1, "deletions": 1},
                ]
            raise AssertionError(url)
        self.healer.github_json = fake_github
        self.healer.jira_ticket = lambda key: {"key": key}

        event = self.healer.pull_request_event("org/repo", {"number": 12})

        self.assertEqual(event["jira_keys"], ["ABC-42"])
        self.assertEqual(event["base_branch"], "main")
        self.assertEqual(event["changed_files"][0]["path"], "jobs/spark_job.py")
        self.assertEqual(len(event["all_changed_files"]), 2)
        self.assertEqual(event["all_changed_files"][1]["status"], "modified")
if __name__ == "__main__":
    unittest.main()