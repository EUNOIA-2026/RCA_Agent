# Fix Agent

This program calls the Azure AI agent configured for `Fix_agent` and asks it
to explain what it would do for a request. It only returns the agent's plan;
it does not apply changes or run the requested task.

## Configuration

Place a `.env` file in this directory, or provide the variables through the
process environment:

```dotenv
AZURE_AI_PROJECT_ENDPOINT=https://your-project-endpoint
AGENT_NAME_2=your-agent-name
API_KEY=your-api-key
API_VERSION=v1
```

`API_VERSION` is optional and defaults to `v1`. `AGENT_NAME` may be used
instead of `AGENT_NAME_2`. Keep credentials out of source control.

Install the dependencies from the repository root:

```powershell
python -m pip install -r Fix_agent\requirements.txt
```

## Usage

From the repository root, ask the agent for a general description:

```powershell
python Fix_agent\fixmain.py
```

Or ask it to describe a plan for a specific task:

```powershell
python Fix_agent\fixmain.py "Review the login flow and explain your proposed steps"
```
