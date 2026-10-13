# Jira Code Change Intelligence

This tool compares a Jira ticket with a developer's completed code changes,
asks the configured Azure AI Agent to trace the supported technical and
functional impact, and writes a Markdown analysis report.

The agent must be configured to use GPT-5 in Azure AI Foundry. The CLI calls
that agent through its Responses endpoint; it does not select or override the
model itself.

## Setup

1. Copy `.env.example` to `.env` in this directory and set the Azure AI project
   endpoint, agent name, and API key. Keep `.env` private.
2. Install dependencies:

   ```powershell
   python -m pip install -r Self_Healing/requirements.txt
   ```

## Analyze a developer change

From the workspace root, provide the Jira Markdown file and repository path.
The default comparison is the worktree against `HEAD`:

```powershell
python Self_Healing/self_healing.py analyze `
  --ticket APP_EKS/tests/jira/STU-104.md `
  --repo APP_EKS
```

To compare committed work against a base branch, name it explicitly:

```powershell
python Self_Healing/self_healing.py analyze `
  --ticket APP_EKS/tests/jira/STU-104.md `
  --repo APP_EKS `
  --base-ref origin/main
```

Alternatively, supply a unified diff with `--diff PATH`; this is useful when
the change is not available as a local Git worktree. Use `--output-dir PATH` to
choose another report directory. By default reports go to
`Self_Healing/reports/`.

The ticket key is read from the ticket content or filename. A functional
classification creates `functional_<ticket-key>.md`; a technical
classification creates `technical_<ticket-key>.md`. BOTH creates both files,
each containing the complete combined analysis.

## Analysis boundaries

Only application source files are included as code context. Configuration,
build outputs, dependencies, and likely credential/security files are excluded;
credential-like literals in eligible source or diffs are redacted before the
Azure request. The agent is instructed not to perform security analysis. The
tool does not modify application files or the Jira ticket.

The generated report distinguishes confirmed, inferred, and unknown impact.
It is an evidence-based review, not proof that tests passed or that a behavior
was measured. Review the report and run the recommended application tests.
