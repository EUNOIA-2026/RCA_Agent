import argparse
import json
import os
from pathlib import Path
import time
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from documentation_healer import DocumentationHealer


FIX_AGENT_DIR = Path(__file__).resolve().parent
load_dotenv(FIX_AGENT_DIR / ".env")


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name!r} "
            f"(expected in {FIX_AGENT_DIR / '.env'} or the process environment)."
        )
    return value


def get_agent_name() -> str:
    name = os.getenv("AGENT_NAME_2") or os.getenv("AGENT_NAME")
    if not name:
        raise RuntimeError(
            "Missing required environment variable 'AGENT_NAME_2' "
            "(or 'AGENT_NAME') in Fix_agent/.env or the process environment."
        )
    return name


def call_agent(prompt: str) -> str:
    project_endpoint = required_env("AZURE_AI_PROJECT_ENDPOINT").rstrip("/")
    agent_name = get_agent_name()
    api_key = required_env("API_KEY")
    api_version = os.getenv("API_VERSION", "v1")

    endpoint = (
        f"{project_endpoint}/agents/{quote(agent_name, safe='')}"
        f"/endpoint/protocols/openai/responses?api-version={quote(api_version, safe='')}"
    )

    with requests.post(
        endpoint,
        headers={
            "api-key": api_key,
            "Content-Type": "application/json",
        },
        json={"input": prompt, "stream": True},
        timeout=(10, 180),
        stream=True,
    ) as response:
        if response.status_code >= 400:
            body = response.text[:2000]
            raise RuntimeError(
                f"Azure agent returned HTTP {response.status_code}: {body}"
            )
        response.raise_for_status()
        response.encoding = "utf-8"

        output: list[str] = []
        event_name: str | None = None
        data_lines: list[str] = []

        for raw_line in response.iter_lines(decode_unicode=True):
            if raw_line is None:
                continue

            line = raw_line.strip()
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif not line:
                if event_name and data_lines:
                    payload_text = "\n".join(data_lines)
                    try:
                        payload = json.loads(payload_text)
                    except json.JSONDecodeError:
                        payload = {"raw": payload_text}

                    if event_name == "response.output_text.delta":
                        output.append(str(payload.get("delta", "")))
                    elif event_name in {"response.error", "error"}:
                        raise RuntimeError(f"Azure agent stream error: {payload}")

                event_name = None
                data_lines = []

    result = "".join(output).strip()
    if not result:
        raise RuntimeError("Azure agent returned no output text.")
    return result


def watch_for_changes() -> None:
    agent_name = get_agent_name()
    required_env("AZURE_AI_PROJECT_ENDPOINT")
    required_env("API_KEY")

    try:
        interval = float(os.getenv("FIX_AGENT_WATCH_INTERVAL", "5"))
    except ValueError as exc:
        raise RuntimeError("FIX_AGENT_WATCH_INTERVAL must be a positive number of seconds.") from exc
    if interval <= 0:
        raise RuntimeError("FIX_AGENT_WATCH_INTERVAL must be a positive number of seconds.")

    healer = DocumentationHealer(
        root=FIX_AGENT_DIR.parent,
        fix_root=FIX_AGENT_DIR,
        invoke_agent=call_agent,
        logger=lambda message: print(f"[Self-Healing] {message}", flush=True),
    )
    print(f"Agent: {agent_name}", flush=True)
    healer.log_configuration()
    healer.initialize_watch()
    print(
        f"Watching {healer.root} for source changes every {interval:g} seconds. "
        "Press Ctrl+C to stop.",
        flush=True,
    )

    retry_delay = max(interval, 60.0)
    while True:
        try:
            healer.poll_local_changes()
            healer.poll_merged_pull_requests()
            retry_delay = max(interval, 60.0)
        except (requests.RequestException, RuntimeError) as exc:
            print(
                f"[Self-Healing] Watch iteration failed: {exc}. "
                f"Retrying in {retry_delay:g} seconds.",
                flush=True,
            )
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300.0)
            continue
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Azure AI agent or watch project changes for documentation updates."
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch project source changes and update the relevant existing documentation.",
    )
    parser.add_argument(
        "task",
        nargs="?",
        help="Optional task for the agent to explain a plan for.",
    )
    args = parser.parse_args()

    if args.watch:
        watch_for_changes()
        return

    task = args.task or "Describe your role and what you will do."
    prompt = (
        "Explain what you would do for the request below. Give a concise, "
        "ordered plan. Do not make changes or execute the task yet.\n\n"
        f"Request: {task}"
    )

    print(f"Agent: {get_agent_name()}", flush=True)
    print(call_agent(prompt))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nSelf-Healing watcher stopped.", flush=True)
    except (requests.RequestException, RuntimeError) as exc:
        raise SystemExit(f"Fix Agent error: {exc}") from exc
