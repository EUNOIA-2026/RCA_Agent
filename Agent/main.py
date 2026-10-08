import json
import os
import queue
import re
import subprocess
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
# Comma-separated substrings; a stream/pod is watched if its name contains any.
CLOUDWATCH_STREAM_MATCH = os.getenv(
    "CLOUDWATCH_STREAM_MATCH",
    "orders-,simple-python-app",
)
ERROR_LOOKBACK_SECONDS = float(os.getenv("ERROR_LOOKBACK_SECONDS", "2"))
ERROR_LOOKAHEAD_SECONDS = float(os.getenv("ERROR_LOOKAHEAD_SECONDS", "5"))

K8S_WATCH_ENABLED = os.getenv("K8S_WATCH_ENABLED", "true").lower() == "true"
K8S_NAMESPACE = os.getenv("K8S_NAMESPACE", "default")
K8S_POLL_INTERVAL = float(os.getenv("K8S_POLL_INTERVAL", "15"))

# Drop *.log files here (e.g. docker build / CI output) to raise an incident.
INCOMING_DIR = Path(
    os.getenv("INCOMING_LOG_DIR", str(Path(__file__).resolve().parent / "incoming"))
)

# Repeats of the same incident inside this window do not trigger another RCA.
DEDUPE_SECONDS = float(os.getenv("DEDUPE_SECONDS", "300"))

APP_SOURCE_DIR = Path(
    os.getenv(
        "APP_SOURCE_DIR",
        str(Path(__file__).resolve().parent.parent / "Orders_Platform"),
    )
)

MATCHES = [m.strip() for m in CLOUDWATCH_STREAM_MATCH.split(",") if m.strip()]

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
    first_line = log_text.strip().splitlines()[0] if log_text.strip() else ""
    text = first_line.upper()

    if "K8S_POD_STATUS" in text or "K8S_EVENT" in text:
        if "REASON=OOMKILLED" in text:
            return "resource"
        if "REASON=CREATECONTAINERCONFIGERROR" in text:
            return "config"
        return "infrastructure"
    if "CONFIG_ERROR" in text:
        return "config"
    if "DEPENDENCY_ERROR" in text:
        return "dependency"
    if "RESOURCE_ERROR" in text:
        return "resource"
    if "BUILD_ERROR" in text or "FAILED TO SOLVE" in text:
        return "build"
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


def repo_file_index() -> str:
    if not APP_SOURCE_DIR.exists():
        return "(repository not available)"

    skip = {"__pycache__", ".git", ".venv", "node_modules"}
    files = sorted(
        p.relative_to(APP_SOURCE_DIR).as_posix()
        for p in APP_SOURCE_DIR.rglob("*")
        if p.is_file() and not skip.intersection(p.parts)
    )
    return "\n".join(files[:200])


def invoke_agent(log_text: str, category: str) -> str:
    prompt = f"""Perform root cause analysis for the following production incident as a
senior site reliability engineer.

Application layer: {category}
(categories: frontend, backend, database, config, dependency, resource,
infrastructure, build)

Analyze the logs and provide:
1. Incident summary
2. Timeline
3. Affected component
4. Error/exception analysis
5. Root cause
6. Evidence from the logs
7. Impact (blast radius, user-facing symptoms)
8. Immediate remediation
9. Preventive actions
10. Confidence level
11. Fix target: the repository file(s), chosen from the file list below, that
    need to change, and their type (application code, Dockerfile, Kubernetes
    manifest, or configuration). If no repository change is appropriate
    (for example an external outage), say so.

Do not invent missing evidence. If a stack trace is available, use the exception type,
file and line number when present. Distinguish the trigger from the underlying
defect, and prefer the durable fix over a workaround.

REPOSITORY FILES:
{repo_file_index()}

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
                if any(match in name for match in MATCHES):
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


BAD_WAITING_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
    "CreateContainerError",
    "InvalidImageName",
}


class K8sWatcher:
    """Surfaces failures that never reach application logs (OOMKilled, probes, image pulls)."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.primed = PROCESS_EXISTING
        self.last_poll = 0.0

    def _kubectl(self, *args: str) -> str:
        result = subprocess.run(
            ["kubectl", "-n", K8S_NAMESPACE, *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return result.stdout

    def _previous_logs(self, pod: str, container: str) -> str:
        try:
            return self._kubectl("logs", pod, "-c", container, "--previous", "--tail=30")
        except Exception:
            return ""

    def poll(self) -> list[tuple[str, int, str, str]]:
        if time.monotonic() - self.last_poll < K8S_POLL_INTERVAL:
            return []
        self.last_poll = time.monotonic()

        now_ms = int(time.time() * 1000)
        detections = []

        pods = json.loads(self._kubectl("get", "pods", "-o", "json")).get("items", [])
        for pod in pods:
            name = pod["metadata"]["name"]
            if not any(match in name for match in MATCHES):
                continue

            for status in pod.get("status", {}).get("containerStatuses", []):
                waiting = status.get("state", {}).get("waiting") or {}
                last = status.get("lastState", {}).get("terminated") or {}
                restarts = status.get("restartCount", 0)

                reason = None
                if last.get("reason") == "OOMKilled":
                    reason = "OOMKilled"
                elif waiting.get("reason") in BAD_WAITING_REASONS:
                    reason = waiting["reason"]
                if not reason:
                    continue

                key = f"{name}/{status['name']}/{reason}/{restarts}"
                if key in self.seen:
                    continue
                self.seen.add(key)

                line = (
                    f"ERROR K8S_POD_STATUS pod={name} container={status['name']} "
                    f"reason={reason} restarts={restarts} "
                    f"last_exit_code={last.get('exitCode')} "
                    f"last_reason={last.get('reason')} "
                    f"message={waiting.get('message', '')}"
                )
                previous = (
                    self._previous_logs(name, status["name"]) if self.primed else ""
                )
                if previous:
                    line += f"\nPrevious container logs:\n{previous}"
                detections.append((f"k8s:{name}", now_ms, key, line))

        events = json.loads(
            self._kubectl("get", "events", "--field-selector", "type=Warning", "-o", "json")
        ).get("items", [])
        for event in events:
            obj = event.get("involvedObject", {})
            if not any(match in obj.get("name", "") for match in MATCHES):
                continue
            if event.get("reason") == "BackOff":
                continue  # already reported through pod status

            key = f"{event['metadata']['uid']}/{event.get('count', 1)}"
            if key in self.seen:
                continue
            self.seen.add(key)

            line = (
                f"ERROR K8S_EVENT object={obj.get('kind')}/{obj.get('name')} "
                f"reason={event.get('reason')} count={event.get('count', 1)} "
                f"message={event.get('message', '')}"
            )
            detections.append((f"k8s:{obj.get('name')}", now_ms, key, line))

        if not self.primed:
            self.primed = True
            return []

        return detections


class IncomingLogWatcher:
    """Raises an incident for each new *.log file (build/CI output) in INCOMING_DIR."""

    def __init__(self) -> None:
        INCOMING_DIR.mkdir(parents=True, exist_ok=True)
        self.seen: set[str] = set()

        if not PROCESS_EXISTING:
            self.seen = {str(p) for p in INCOMING_DIR.glob("*.log")}

    def poll(self) -> list[tuple[str, int, str, str]]:
        detections = []

        for path in sorted(INCOMING_DIR.glob("*.log")):
            if str(path) in self.seen:
                continue
            self.seen.add(str(path))

            text = path.read_text(encoding="utf-8", errors="replace")[-8000:].strip()
            if not text:
                continue

            if "BUILD_ERROR" not in text.upper():
                text = f"BUILD_ERROR {path.name}\n{text}"
            detections.append((f"file:{path.name}", int(time.time() * 1000), path.name, text))

        return detections


VOLATILE_TOKENS = re.compile(r"[0-9a-f]{6,}|\d+")
recent_incidents: dict[str, float] = {}


def is_duplicate(category: str, error_line: str) -> bool:
    first_line = error_line.strip().splitlines()[0]
    normalized = VOLATILE_TOKENS.sub("#", first_line)[-160:]
    signature = f"{category}:{normalized}"
    now = time.monotonic()

    last = recent_incidents.get(signature)
    recent_incidents[signature] = now
    return last is not None and now - last < DEDUPE_SECONDS


def worker(q: queue.Queue) -> None:
    while True:
        stream_name, timestamp, event_id, error_line = q.get()
        try:
            category = classify(error_line)

            if is_duplicate(category, error_line):
                log(f"Skipping repeat of a recent {category} incident (eventId={event_id})")
                continue

            if stream_name.startswith(("k8s:", "file:")):
                all_logs = error_line
            else:
                # Give CloudWatch a moment to ingest traceback/access-log lines that follow the ERROR.
                time.sleep(1.0)
                context = fetch_context(watcher.client, stream_name, timestamp)
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
    sources = [("CloudWatch", watcher), ("build logs", IncomingLogWatcher())]
    if K8S_WATCH_ENABLED:
        sources.append(("Kubernetes", K8sWatcher()))

    work: queue.Queue = queue.Queue()
    threading.Thread(target=worker, args=(work,), daemon=True).start()

    log(
        f"Watching CloudWatch '{CLOUDWATCH_LOG_GROUP}' for streams containing "
        f"{MATCHES} every {POLL_INTERVAL:.1f}s"
    )
    log(f"Also watching: {', '.join(name for name, _ in sources[1:])} ({INCOMING_DIR})")
    log(f"Reports: {REPORT_DIR}/<category>")

    while True:
        for source_name, source in sources:
            try:
                for stream_name, timestamp, event_id, error_line in source.poll():
                    log(
                        f"AUTO-DETECT[{source_name}]: {error_line.splitlines()[0]} "
                        f"(stream={stream_name.split(':')[-1][:80]} eventId={event_id})"
                    )
                    work.put((stream_name, timestamp, event_id, error_line))
            except Exception as exc:
                log(f"{source_name} watcher error: {exc!r}")
if __name__ == "__main__":
    main()
