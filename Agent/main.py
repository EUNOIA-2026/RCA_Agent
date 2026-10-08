import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().with_name(".env"))


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


PROJECT_ENDPOINT = required("AZURE_AI_PROJECT_ENDPOINT").rstrip("/")
AGENT_NAME = required("AGENT_NAME")
API_KEY = required("API_KEY")
API_VERSION = os.getenv("API_VERSION", "v1")

REPORT_DIR = Path(os.getenv("REPORT_DIR", "reports"))
if not REPORT_DIR.is_absolute():
    REPORT_DIR = (Path(__file__).resolve().parent / REPORT_DIR).resolve()
else:
    REPORT_DIR = REPORT_DIR.resolve()
#REPORT_DIR = Path(os.getenv("REPORT_DIR", "reports")).resolve()
POLL_INTERVAL = float(os.getenv("FALLBACK_POLL_INTERVAL", "2"))
PROCESS_EXISTING = os.getenv("PROCESS_EXISTING", "false").lower() == "true"

CLOUDWATCH_REGION = os.getenv("CLOUDWATCH_REGION", "us-east-1")
CLOUDWATCH_LOG_GROUP = os.getenv(
    "CLOUDWATCH_LOG_GROUP",
    "/aws/containerinsights/Eunoia/application",
)
CLOUDWATCH_STREAM_MATCH = os.getenv(
    "CLOUDWATCH_STREAM_MATCH",
    "simple-python-app",
)
ERROR_LOOKBACK_SECONDS = float(os.getenv("ERROR_LOOKBACK_SECONDS", "2"))
ERROR_LOOKAHEAD_SECONDS = float(os.getenv("ERROR_LOOKAHEAD_SECONDS", "5"))

URL = (
    f"{PROJECT_ENDPOINT}/agents/{AGENT_NAME}"
    f"/endpoint/protocols/openai/responses?api-version={API_VERSION}"
)
HEADERS = {
    "api-key": API_KEY,
    "Content-Type": "application/json",
}


def log(message: str) -> None:
    print(
        f"[{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}] {message}",
        flush=True,
    )


def parse_cloudwatch_message(message: str) -> str:
    try:
        payload = json.loads(message)
        return str(payload.get("log", message)).strip()
    except json.JSONDecodeError:
        return message.strip()


def describe_cloudwatch_event(stream_name: str, event: dict[str, Any]) -> str:
    timestamp = int(event.get("timestamp", 0))
    event_id = str(event.get("eventId", "unknown"))
    raw_message = event.get("message", "")
    text = parse_cloudwatch_message(raw_message)

    metadata: list[str] = []
    try:
        payload = json.loads(raw_message)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict):
        kubernetes = payload.get("kubernetes")
        if isinstance(kubernetes, dict):
            pod_name = kubernetes.get("pod_name")
            container_name = kubernetes.get("container_name")
            namespace_name = kubernetes.get("namespace_name")
            if pod_name:
                metadata.append(f"pod={pod_name}")
            if container_name:
                metadata.append(f"container={container_name}")
            if namespace_name:
                metadata.append(f"namespace={namespace_name}")

    metadata_suffix = f" ({', '.join(metadata)})" if metadata else ""
    return (
        f"[{datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()}] "
        f"stream={stream_name} eventId={event_id}{metadata_suffix} {text}"
    )


def classify(log_text: str) -> str:
    text = log_text.upper()
    if "FRONTEND_ERROR" in text:
        return "frontend"
    if "DATABASE_ERROR" in text:
        return "database"
    if "BACKEND_ERROR" in text:
        return "backend"
    if "TYPEERROR" in text and "NAME" in text:
        return "frontend"
    if (
        "FILENOTFOUNDERROR" in text
        or "CSV" in text
        or "DICTREADER" in text
        or "KEYERROR" in text
        or "VALUEERROR" in text and "COLUMN" in text
    ):
        return "database"
    return "backend"


def iter_sse(response: requests.Response):
    event_name = None
    for raw in response.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        line = raw.strip()
        if not line:
            event_name = None
            continue
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
            continue
        if line.startswith("data:") and event_name:
            data = line.split(":", 1)[1].strip()
            if data == "[DONE]":
                continue
            try:
                yield event_name, json.loads(data)
            except json.JSONDecodeError:
                continue


def invoke_agent(log_text: str, category: str) -> str:
    prompt = f"""Perform root cause analysis for the following production incident.

Application layer: {category}

Analyze the logs and provide:
1. Incident summary
2. Timeline
3. Affected component
4. Error/exception analysis
5. Root cause
6. Evidence from the logs
7. Impact
8. Immediate remediation
9. Preventive actions
10. Confidence level

Do not invent missing evidence. If a stack trace is available, use the exception type,
file and line number when present.

LOGS:
{log_text}"""

    log(f"Invoking agent '{AGENT_NAME}' for {category} error...")
    started = time.monotonic()
    response = requests.post(
        URL,
        headers=HEADERS,
        json={"input": prompt, "stream": True},
        timeout=120,
        stream=True,
    )
    log(f"Agent HTTP {response.status_code} after {time.monotonic() - started:.2f}s")

    if response.status_code >= 400:
        body = response.text[:1000]
        raise RuntimeError(f"Azure agent returned {response.status_code}: {body}")

    response.raise_for_status()

    output = []
    for event_name, payload in iter_sse(response):
        if event_name == "response.output_text.delta":
            output.append(payload.get("delta", ""))
        elif event_name in {"response.error", "error"}:
            raise RuntimeError(f"Azure agent stream error: {payload}")
        elif event_name == "response.completed":
            log(f"Agent finished in {time.monotonic() - started:.2f}s")

    if not output:
        raise RuntimeError("Azure agent returned no output text")

    return "".join(output)


def write_report(category: str, detected_logs: str, analysis: str) -> Path:
    destination = REPORT_DIR / category
    destination.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc)
    path = destination / f"rca-report-{generated:%Y%m%d-%H%M%S-%f}.md"

    report = f"""# Root Cause Analysis Report

**Generated:** {generated.isoformat()}
**Agent:** {AGENT_NAME}
**Application layer:** {category}

## Detected Log Entries

```text
{detected_logs}
```

## Analysis

{analysis}
"""
    path.write_text(report, encoding="utf-8")
    return path


def fetch_context(client, stream_name: str, event_timestamp: int) -> list[str]:
    start = max(0, event_timestamp - int(ERROR_LOOKBACK_SECONDS * 1000))
    end = event_timestamp + int(ERROR_LOOKAHEAD_SECONDS * 1000)
    response = client.get_log_events(
        logGroupName=CLOUDWATCH_LOG_GROUP,
        logStreamName=stream_name,
        startTime=start,
        endTime=end,
        startFromHead=True,
        limit=100,
    )

    items = []
    for event in response.get("events", []):
        items.append(describe_cloudwatch_event(stream_name, event))
    return items


class CloudWatchWatcher:
    def __init__(self) -> None:
        self.client = boto3.client("logs", region_name=CLOUDWATCH_REGION)
        self.positions: dict[str, int] = {}
        self.seen: set[tuple[str, str]] = set()

        now_ms = int(time.time() * 1000)
        self.initial_start_ms = now_ms - (10 * 60 * 1000) if PROCESS_EXISTING else now_ms

    def streams(self) -> list[str]:
        names = []
        paginator = self.client.get_paginator("describe_log_streams")
        for page in paginator.paginate(
            logGroupName=CLOUDWATCH_LOG_GROUP,
            orderBy="LastEventTime",
            descending=True,
        ):
            for item in page.get("logStreams", []):
                name = item.get("logStreamName", "")
                if CLOUDWATCH_STREAM_MATCH in name:
                    names.append(name)
        return names

    def poll(self) -> list[tuple[str, int, str, str]]:
        detections = []
        stream_names = self.streams()

        for stream_name in stream_names:
            start = self.positions.get(stream_name, self.initial_start_ms)
            response = self.client.get_log_events(
                logGroupName=CLOUDWATCH_LOG_GROUP,
                logStreamName=stream_name,
                startTime=start,
                startFromHead=True,
                limit=100,
            )

            latest = start
            for event in response.get("events", []):
                ts = int(event.get("timestamp", 0))
                message = event.get("message", "")
                event_id = str(event.get("eventId", f"{ts}:{hash(message)}"))
                latest = max(latest, ts)

                key = (stream_name, event_id)
                if key in self.seen:
                    continue
                self.seen.add(key)

                text = parse_cloudwatch_message(message)
                if "ERROR" not in text.upper():
                    continue

                detections.append((stream_name, ts, event_id, text))

            self.positions[stream_name] = latest + 1

        if len(self.seen) > 10000:
            self.seen = set(list(self.seen)[-5000:])

        return detections


def worker(q: queue.Queue) -> None:
    while True:
        stream_name, timestamp, event_id, error_line = q.get()
        try:
            # Give CloudWatch a moment to ingest traceback/access-log lines that follow the ERROR.
            time.sleep(1.0)
            context = fetch_context(watcher.client, stream_name, timestamp)
            category = classify(error_line)
            all_logs = "\n".join(context) if context else error_line
            analysis = invoke_agent(all_logs, category)
            report_path = write_report(category, all_logs, analysis)
            log(f"RCA report created: {report_path} (eventId={event_id})")
        except Exception as exc:
            log(f"Failed to process error automatically: {exc!r}")
        finally:
            q.task_done()


def main() -> None:
    global watcher
    watcher = CloudWatchWatcher()
    work: queue.Queue = queue.Queue()
    threading.Thread(target=worker, args=(work,), daemon=True).start()

    log(
        f"Watching CloudWatch '{CLOUDWATCH_LOG_GROUP}' for streams containing "
        f"'{CLOUDWATCH_STREAM_MATCH}' every {POLL_INTERVAL:.1f}s"
    )
    log(f"Reports: {REPORT_DIR / 'frontend'}, {REPORT_DIR / 'backend'}, {REPORT_DIR / 'database'}")

    while True:
        try:
            detections = watcher.poll()
            for stream_name, timestamp, event_id, error_line in detections:
                log(
                    f"AUTO-DETECT: {error_line} "
                    f"(stream={stream_name.split(':')[-1][:80]} eventId={event_id})"
                )
                work.put((stream_name, timestamp, event_id, error_line))
        except Exception as exc:
            log(f"CloudWatch watcher error: {exc!r}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
