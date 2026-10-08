# Self-Healing Documentation Agent

The agent supports two modes:

- **Plan mode** calls the configured Azure AI agent once and prints its response.
- **Watch mode** monitors source/configuration files in the `RCA_Agent` repository.
  When it detects a change, it sends the relevant source context and existing
  documentation to the agent, validates its proposed patch, and updates only
  the relevant sections of existing documentation. It can also process merged
  GitHub pull requests.

The watcher uses the repository's existing documentation, including
`APP_EKS/docs/technical-documentation.md` and
`APP_EKS/docs/functional-documentation.md`. The agent chooses which existing
document is relevant to each change. It does not create or replace documents.

## Setup

Create `Self_Healing\.env` with the Azure AI settings:

```dotenv
AZURE_AI_PROJECT_ENDPOINT=https://your-project-endpoint
AGENT_NAME_2=your-agent-name
API_KEY=your-api-key
API_VERSION=v1
```

`API_VERSION` is optional and defaults to `v1`; `AGENT_NAME` can be used
instead of `AGENT_NAME_2`. Keep credentials out of source control.

Automatic documentation pull requests also require a GitHub remote (or
`GITHUB_REPOSITORY=owner/repo`) and a `GITHUB_TOKEN` or `GH_TOKEN` with
permission to read the repository and push a branch. The watcher will not
apply documentation patches if the GitHub pull-request preflight fails.

Install the dependencies from the repository root:

```powershell
python -m pip install -r RCA_Agent\Self_Healing\requirements.txt
```

## Run

From the repository root, check the one-shot plan mode:

```powershell
python RCA_Agent\Self_Healing\fixmain.py
```

Ask the agent to explain a particular request:

```powershell
python RCA_Agent\Self_Healing\fixmain.py "Review the login flow and explain your proposed steps"
```

Start the documentation watcher:

```powershell
python RCA_Agent\Self_Healing\fixmain.py --watch
```

The watcher polls every 5 seconds by default. Set
`FIX_AGENT_WATCH_INTERVAL` to change that interval. It records a baseline on
first startup and handles subsequent changes. Transient Azure or processing
errors are reported and retried with backoff rather than stopping the watcher.
Stop it with Ctrl+C. The watcher keeps one latest combined report per change
type, overwriting the previous report of that type:

- Functional changes: `RCA_Agent\Self_Healing\reports\functional_changes\latest.md`
- Technical changes: `RCA_Agent\Self_Healing\reports\technical\latest.md`
