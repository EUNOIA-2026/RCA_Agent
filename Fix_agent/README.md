# Fix Agent: Documentation Self-Healing

This component extends the existing RCA repair agent with automated documentation analysis. It watches source/configuration changes in the repository, checks merged GitHub pull requests, retrieves linked Jira tickets when configured, and proposes focused updates to existing project documentation.

## Scope and safety

- All implementation, state, audit/traceability reports, and previous document versions are stored under `Fix_agent`.
- Documentation patches may update existing Markdown, reStructuredText, AsciiDoc, or text documents elsewhere in the repository. The agent does not create/delete/rename documentation, edit source code, or push/commit changes.
- Updates use an exact, unique existing text match. Before changing a document, the agent validates traceability for every changed code/configuration path and, when Jira tickets were retrieved, checks requirement mappings. If validation fails, no documentation patch is applied and findings are recorded for human review.
- Earlier document versions are copied to `Fix_agent/documentation_versions`. Audit and traceability reports are written to `Fix_agent/reports/documentation`.
- Source, diffs, retrieved documentation, PR metadata, and Jira content are sent to the configured Azure AI agent for analysis. The prompt redacts common credential/token/private-key patterns and email addresses, but redaction is heuristic; review your data-handling requirements before enabling this integration.

## Requirements and installation

Use Python 3.10 or newer. From the repository root:

```powershell
python -m pip install -r Fix_agent\requirements.txt
```

The existing Fix Agent also requires these environment variables:

- `AZURE_AI_PROJECT_ENDPOINT`
- `AGENT_NAME_2`
- `API_KEY`

The entry point loads the existing `Agent\.env` file, if present. You may also set variables in the process environment. Never commit credentials.

### Optional GitHub integration

- The GitHub repository is inferred from the `origin` remote. Set `GITHUB_REPOSITORY=owner/repo` to override it.
- Set `GITHUB_TOKEN` or `GH_TOKEN` to create pull requests and enable auto-merge. A fine-grained token needs repository metadata read, contents read/write, pull requests read/write, and administration read (to verify the approval rule). Git's configured credentials must also permit `git push`.
- The token is validated against the GitHub repository before any local document patch is written. Local-change PRs always target the currently checked-out branch, and merged-PR-triggered documentation PRs target the source PR's base branch. The selected base must exist on `origin`; if it does not, processing fails safely and asks the operator to publish that intended base branch. The healer never substitutes another branch.
- Merged pull requests are polled every 60 seconds by default. Set `FIX_AGENT_PR_POLL_INTERVAL` to change the interval.
- A validated documentation update is committed on a new `docs/self-healing/...` branch and opened as a GitHub pull request. For a local source change that is already fully documented, the source change still gets a PR (with any related local documentation edits referenced by traceability). The original working branch and index are not switched or committed. For local changes, changed source files are included in that PR when they differ from the remote base; for merged-PR triggers, the PR contains documentation only.
- GitHub auto-merge is enabled only when the base branch's classic branch-protection API confirms at least one required approving review. GitHub then merges only after its required approvals and checks pass. If approval protection cannot be verified, auto-merge is unavailable, or the repository disallows it, the PR is left open for manual review/merge. Configure classic branch protection to require reviews and enable auto-merge in repository settings; rulesets-only review requirements are not currently detected by this guard.
- Local source changes are not analyzed or applied to documents until GitHub PR publishing is configured; the watcher logs the missing setup once per unchanged change set and preserves it for processing after the token is added and the watcher restarted. Interrupt the watcher with `Ctrl+C` for a clean shutdown.

### Optional Jira integration

Set `JIRA_BASE_URL` and `JIRA_API_TOKEN`. For Jira Cloud, also set `JIRA_EMAIL`; for a bearer-token Jira server, leave the email unset. The agent looks for issue keys such as `TEAM-123` in the PR title, body, branch name, and commit messages. When a key is detected but its issue cannot be retrieved, documentation updates for that PR are held for human review.

## Run modes

From the repository root:

```powershell
python Fix_agent\fixmain.py
```

The default long-running mode preserves RCA-report processing and additionally watches for local code/config changes and merged PRs. Local changes are polled at `FIX_AGENT_POLL_INTERVAL` (default 5 seconds). On startup, the current local source tree establishes the baseline; older local changes are treated as historical unless requested explicitly.

Useful options:

```powershell
python Fix_agent\fixmain.py --docs-once
python Fix_agent\fixmain.py --process-existing-docs
python Fix_agent\fixmain.py --no-docs
python Fix_agent\fixmain.py --once
python Fix_agent\fixmain.py --report Agent\reports\backend\rca-report-example.md
```

- `--docs-once` analyzes currently staged, unstaged, and untracked source/configuration changes, creates a documentation PR if updates pass validation, then exits. It does not process merged PRs.
- `--process-existing-docs` analyzes a repository-wide initial source snapshot when starting watch mode; this can generate a large analysis workload.
- `--no-docs` retains the original RCA watch behavior without documentation watching.
- `--once` and `--report` retain the original RCA repair behavior and do not run documentation sync.

## Workflow

1. Discover changed source/configuration files and compare their previous/current hashes and Git diffs. Supported source formats include Java, Python/PySpark, SQL, TypeScript/JavaScript, and common configuration formats. Generated/dependency directories and `Fix_agent` itself are excluded.
2. For merged PRs, retrieve the PR metadata, commits, file statuses/patches, and linked Jira issues when configured.
3. Retrieve relevant existing documentation from the repository as RAG context.
4. Ask the configured Azure AI agent to explain impacts, categorize API/database/configuration/business-logic changes, assess risk, map Jira requirements, and propose section-level documentation patches.
5. Validate paths, exact unique old-text matches, source traceability, undocumented changes, and Jira coverage before applying any patch.
6. Archive the previous document version, update only approved existing document sections, create a reviewable branch/PR, and write audit plus traceability reports under `Fix_agent`.

Review the PR and generated reports. The updater never pushes directly to the protected base branch; GitHub performs the merge after the required approval and checks.