import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


load_dotenv()


def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


PROJECT_ENDPOINT = get_required_env("AZURE_AI_PROJECT_ENDPOINT").rstrip("/")
AGENT_NAME = get_required_env("AGENT_NAME")
API_KEY = get_required_env("API_KEY")
API_VERSION = os.getenv("API_VERSION", "v1")
LOG_FILE = Path(os.getenv("LOG_FILE", "logs/application.log")).resolve()
REPORT_DIR = Path(os.getenv("REPORT_DIR", "reports"))
PROCESS_EXISTING = os.getenv("PROCESS_EXISTING", "false").lower() == "true"
FALLBACK_POLL_INTERVAL = float(os.getenv("FALLBACK_POLL_INTERVAL", "2"))

URL = (
    f"{PROJECT_ENDPOINT}/agents/{AGENT_NAME}"
    f"/endpoint/protocols/openai/responses?api-version={API_VERSION}"
)
HEADERS = {
    "api-key": API_KEY,
    "Content-Type": "application/json",
}


def console(message: str) -> None:
    print(f"[{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def iter_stream_events(response: requests.Response):
    event_name = None
    for raw_line in response.iter_lines(decode_unicode=True):
        if raw_line is None:
            continue
        line = raw_line.strip()
        if not line:
            event_name = None
            continue
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
            continue
        if line.startswith("data:") and event_name:
            payload = line.split(":", 1)[1].strip()
            if payload == "[DONE]":
                continue
            try:
                yield event_name, json.loads(payload)
            except json.JSONDecodeError:
                continue


def create_report(log: str) -> None:
    prompt = f"""Analyze the following production logs and generate a complete RCA report.
Include the detected error, likely root cause, impact, evidence, and recommended remediation.

LOG:
{log}"""

    console(f"Invoking agent '{AGENT_NAME}'...")
    started = time.monotonic()
    response = requests.post(
        URL,
        headers=HEADERS,
        json={"input": prompt, "stream": True},
        timeout=120,
        stream=True,
    )
    console(f"Connected in {time.monotonic() - started:.2f}s - streaming response...")

    if response.status_code >= 400:
        console(f"Response body: {response.text[:500]}")
    response.raise_for_status()

    output_text = []
    reasoning_started = False
    writing_started = False
    status_code = response.status_code

    for event_name, payload in iter_stream_events(response):
        if event_name == "response.output_item.added" and not reasoning_started:
            item_type = payload.get("item", {}).get("type")
            if item_type == "reasoning":
                reasoning_started = True
                console(f"Agent is reasoning... ({time.monotonic() - started:.2f}s)")
        elif event_name == "response.output_text.delta":
            if not writing_started:
                writing_started = True
                console(f"Agent is writing the report... ({time.monotonic() - started:.2f}s)")
            output_text.append(payload.get("delta", ""))
        elif event_name == "response.completed":
            console(f"Agent finished in {time.monotonic() - started:.2f}s")
        elif event_name == "response.error" or event_name == "error":
            raise RuntimeError(f"Agent stream error: {payload}")

    if not output_text:
        raise RuntimeError("The agent response did not contain output text")

    generated_at = datetime.now(timezone.utc)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"rca-report-{generated_at:%Y%m%d-%H%M%S-%f}.md"
    report = f"""# Root Cause Analysis Report

**Generated:** {generated_at.isoformat()}
**Agent:** {AGENT_NAME}
**HTTP status:** {status_code}

## Detected Log Entries

```text
{log}
```

## Analysis

{"".join(output_text)}
"""
    report_path.write_text(report, encoding="utf-8")
    console(f"RCA report created: {report_path}")


def file_identity(stat_result: os.stat_result) -> tuple:
    return stat_result.st_dev, stat_result.st_ino, stat_result.st_ctime_ns


def report_worker(work: "queue.Queue[tuple[list[str], float]]") -> None:
    while True:
        lines, detected_at = work.get()
        try:
            create_report("\n".join(lines))
            console(f"Done - {time.monotonic() - detected_at:.2f}s from detection to report")
        except requests.RequestException as error:
            console(f"Failed to invoke RCA agent: {error}")
        except RuntimeError as error:
            console(f"Failed to create RCA report: {error}")
        except Exception as error:  # noqa: BLE001 - never let the worker die silently
            console(f"Unexpected error while creating report: {error!r}")
        finally:
            work.task_done()


def read_new_text(position: int, size: int) -> tuple[str, int]:
    if size <= position:
        return "", position
    with LOG_FILE.open("rb") as log_file:
        log_file.seek(position)
        chunk = log_file.read()
        return chunk.decode("utf-8", errors="replace"), log_file.tell()


class LogTracker:
    """Tracks the read position of LOG_FILE and extracts newly appended ERROR lines."""

    def __init__(self) -> None:
        self.position = 0
        self.identity = None
        if LOG_FILE.exists():
            stat_result = LOG_FILE.stat()
            self.identity = file_identity(stat_result)
            if not PROCESS_EXISTING:
                self.position = stat_result.st_size

    def poll(self) -> list[str]:
        try:
            stat_result = LOG_FILE.stat()
        except OSError:
            return []

        current_identity = file_identity(stat_result)
        if self.identity is not None and current_identity != self.identity:
            console("Log file was replaced - reading from the start")
            self.position = 0
        self.identity = current_identity

        if stat_result.st_size < self.position:
            console("Log file was truncated - reading from the start")
            self.position = 0

        if stat_result.st_size == self.position:
            return []

        before = self.position
        new_text, self.position = read_new_text(self.position, stat_result.st_size)
        console(
            f"File grew from {before} to {self.position} bytes "
            f"({self.position - before} new byte(s))"
        )
        if not new_text:
            return []

        return [
            stripped
            for line in new_text.splitlines()
            if "ERROR" in line.upper() and (stripped := line.strip())
        ]


class LogChangeHandler(FileSystemEventHandler):
    """Wakes the watcher loop the instant the log file is written to or replaced."""

    def __init__(self, wake_event: threading.Event) -> None:
        self.wake_event = wake_event

    def _maybe_wake(self, event) -> None:
        if event.is_directory:
            return
        if Path(event.src_path).resolve() == LOG_FILE:
            self.wake_event.set()

    def on_modified(self, event) -> None:
        self._maybe_wake(event)

    def on_created(self, event) -> None:
        self._maybe_wake(event)

    def on_moved(self, event) -> None:
        if not event.is_directory and Path(event.dest_path).resolve() == LOG_FILE:
            self.wake_event.set()


def watch_log() -> None:
    work: "queue.Queue[tuple[list[str], float]]" = queue.Queue()
    threading.Thread(target=report_worker, args=(work,), daemon=True).start()

    tracker = LogTracker()
    console(
        f"Watching {LOG_FILE} for appended ERROR lines (event-driven, "
        f"{'beginning' if tracker.position == 0 else 'end'} of file) "
        f"- fallback poll every {FALLBACK_POLL_INTERVAL}s"
    )

    wake_event = threading.Event()
    observer = Observer()
    observer.schedule(LogChangeHandler(wake_event), str(LOG_FILE.parent), recursive=False)
    observer.start()

    try:
        while True:
            woke = wake_event.wait(timeout=FALLBACK_POLL_INTERVAL)
            wake_event.clear()

            lines = tracker.poll()
            if not lines:
                continue

            console(
                f"Detected {len(lines)} new ERROR line(s) in {LOG_FILE} "
                f"({'event' if woke else 'fallback poll'}):"
            )
            for line in lines:
                console(f"  | {line}")
            work.put((lines, time.monotonic()))
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    watch_log()
