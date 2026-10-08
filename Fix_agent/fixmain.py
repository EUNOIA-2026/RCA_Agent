import argparse
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

if __package__:
    from .documentation_healer import DocumentationHealer
else:
    from documentation_healer import DocumentationHealer


# ---------------------------------------------------------------------------
# Paths / configuration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = ROOT / "APP_EKS"
AGENT_REPORT_ROOT = ROOT / "Agent" / "reports"
FIX_ROOT = ROOT / "Fix_agent"
FIX_REPORT_ROOT = FIX_ROOT / "reports"
BACKUP_ROOT = FIX_ROOT / "backups"
STATE_FILE = FIX_ROOT / "state.json"

# Reuse the existing Agent/.env. Do not copy or print the secret values.
load_dotenv(ROOT / "Agent" / ".env")


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


PROJECT_ENDPOINT = required("AZURE_AI_PROJECT_ENDPOINT").rstrip("/")
AGENT_NAME = required("AGENT_NAME_2")
API_KEY = required("API_KEY")
API_VERSION = os.getenv("API_VERSION", "v1")

AZURE_URL = (
    f"{PROJECT_ENDPOINT}/agents/{AGENT_NAME}"
    f"/endpoint/protocols/openai/responses?api-version={API_VERSION}"
)

HEADERS = {
    "api-key": API_KEY,
    "Content-Type": "application/json",
}

POLL_SECONDS = float(os.getenv("FIX_AGENT_POLL_INTERVAL", "5"))

# Keep prompts bounded. This project is small, but we still avoid accidentally
# sending node_modules, build output, virtual environments, binaries, etc.
MAX_TOTAL_SOURCE_CHARS = 180_000
MAX_FILE_CHARS = 30_000

TEXT_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".html",
    ".css",
    ".scss",
    ".json",
    ".md",
    ".yaml",
    ".yml",
    ".txt",
    ".toml",
    ".ini",
}


# ---------------------------------------------------------------------------
# Logging / state
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"processed_reports": []}

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    return {"processed_reports": []}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# RCA report discovery
# ---------------------------------------------------------------------------

def find_latest_report() -> Path | None:
    reports = list(AGENT_REPORT_ROOT.rglob("rca-report-*.md"))

    if not reports:
        return None

    return max(reports, key=lambda p: p.stat().st_mtime)


def find_new_reports() -> list[Path]:
    state = load_state()
    processed = set(state.get("processed_reports", []))

    reports = sorted(
        AGENT_REPORT_ROOT.rglob("rca-report-*.md"),
        key=lambda p: p.stat().st_mtime,
    )

    return [
        report
        for report in reports
        if str(report.resolve()) not in processed
    ]


def mark_processed(report: Path) -> None:
    state = load_state()
    processed = list(state.get("processed_reports", []))

    resolved = str(report.resolve())

    if resolved not in processed:
        processed.append(resolved)

    # Keep state reasonably small.
    state["processed_reports"] = processed[-500:]
    save_state(state)


# ---------------------------------------------------------------------------
# Source discovery
# ---------------------------------------------------------------------------

def infer_category(report_text: str, report_path: Path) -> str:
    lower = report_text.lower()

    if "application layer:** frontend" in lower:
        return "frontend"

    if "application layer:** backend" in lower:
        return "backend"

    if "application layer:** database" in lower:
        return "database"

    path_lower = str(report_path).lower()

    if "\\frontend\\" in path_lower:
        return "frontend"

    if "\\backend\\" in path_lower:
        return "backend"

    if "\\database\\" in path_lower:
        return "database"

    return "unknown"



def source_roots_for_category(category: str) -> list[Path]:
    """
    RCA evidence can cross application layers. Always make both backend and
    frontend source available to the repair agent, while putting the reported
    layer first.
    """
    backend = APP_ROOT / "backend"
    frontend = APP_ROOT / "frontend" / "src"

    if category == "frontend":
        roots = [frontend, backend]
    elif category == "backend":
        roots = [backend, frontend]
    else:
        roots = [backend, frontend]

    for candidate in (
        APP_ROOT / "frontend" / "package.json",
        APP_ROOT / "frontend" / "angular.json",
        APP_ROOT / "Dockerfile",
        APP_ROOT / "app.py",
    ):
        if candidate.exists():
            roots.append(candidate)

    return roots


def iter_source_files(category: str) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()

    ignored_parts = {
        "node_modules",
        ".git",
        ".venv",
        "__pycache__",
        "dist",
        "coverage",
        "build",
    }

    for root in source_roots_for_category(category):
        if not root.exists():
            continue

        if root.is_file():
            candidates = [root]
        else:
            candidates = root.rglob("*")

        for path in candidates:
            if not path.is_file():
                continue

            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue

            if any(part in ignored_parts for part in path.parts):
                continue

            resolved = path.resolve()

            if resolved not in seen:
                seen.add(resolved)
                files.append(resolved)

    return files


def rank_source_files(
    files: list[Path],
    report_text: str,
) -> list[Path]:
    """
    Put likely relevant files first without hard-coding a particular fix.
    """
    terms = set(
        re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", report_text.lower())
    )

    def score(path: Path) -> tuple[int, str]:
        name = path.name.lower()
        path_text = str(path).lower()

        score_value = 0

        for term in terms:
            if term in name:
                score_value += 5
            elif term in path_text:
                score_value += 1

        for keyword in (
            "app.py",
            "app.ts",
            "app.html",
            "service",
            "controller",
            "route",
        ):
            if keyword in name:
                score_value += 3

        return (-score_value, str(path))

    return sorted(files, key=score)


def collect_source_context(
    category: str,
    report_text: str,
) -> tuple[str, list[str]]:
    files = rank_source_files(iter_source_files(category), report_text)

    chunks: list[str] = []
    included: list[str] = []
    total = 0

    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        except OSError:
            continue

        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + "\n...[file truncated]..."

        relative = path.relative_to(ROOT).as_posix()

        block = (
            f"\n===== FILE: {relative} =====\n"
            f"{text}\n"
            f"===== END FILE: {relative} =====\n"
        )

        if total + len(block) > MAX_TOTAL_SOURCE_CHARS:
            continue

        chunks.append(block)
        included.append(relative)
        total += len(block)

    return "".join(chunks), included


# ---------------------------------------------------------------------------
# Azure AI agent
# ---------------------------------------------------------------------------

def extract_sse_events(response: requests.Response):
    """
    Parse the SSE format used by the existing RCA Agent.
    """
    event_name = None
    data_lines: list[str] = []

    for raw_line in response.iter_lines(decode_unicode=True):
        if raw_line is None:
            continue

        line = raw_line.strip()

        if not line:
            if event_name and data_lines:
                data_text = "\n".join(data_lines)

                try:
                    payload = json.loads(data_text)
                except json.JSONDecodeError:
                    payload = {"raw": data_text}

                yield event_name, payload

            event_name = None
            data_lines = []
            continue

        if line.startswith("event:"):
            event_name = line[6:].strip()

        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())


def call_fix_agent(prompt: str) -> str:
    log(f"Invoking fix agent '{AGENT_NAME}'...")

    started = time.monotonic()

    response = requests.post(
        AZURE_URL,
        headers=HEADERS,
        json={
            "input": prompt,
            "stream": True,
        },
        timeout=180,
        stream=True,
    )

    log(
        f"Fix agent HTTP {response.status_code} "
        f"after {time.monotonic() - started:.2f}s"
    )

    if response.status_code >= 400:
        body = response.text[:2000]
        raise RuntimeError(
            f"Azure fix agent returned {response.status_code}: {body}"
        )

    response.raise_for_status()

    output: list[str] = []

    for event_name, payload in extract_sse_events(response):
        if event_name == "response.output_text.delta":
            output.append(str(payload.get("delta", "")))

        elif event_name in {"response.error", "error"}:
            raise RuntimeError(
                f"Azure fix agent stream error: {payload}"
            )

        elif event_name == "response.completed":
            log(
                f"Fix agent finished in "
                f"{time.monotonic() - started:.2f}s"
            )

    result = "".join(output).strip()

    if not result:
        raise RuntimeError("Azure fix agent returned no output text")

    return result


# ---------------------------------------------------------------------------
# Fix-plan parsing / validation
# ---------------------------------------------------------------------------

def strip_code_fences(text: str) -> str:
    cleaned = text.strip()

    if cleaned.startswith("```"):
        lines = cleaned.splitlines()

        if lines and lines[0].startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        cleaned = "\n".join(lines).strip()

    return cleaned


def parse_fix_plan(raw: str) -> dict[str, Any]:
    cleaned = strip_code_fences(raw)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Fix agent did not return valid JSON."
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError("Fix agent response must be a JSON object.")

    decision = data.get("decision")

    if decision not in {
        "fix",
        "no_fix",
        "needs_human_review",
    }:
        raise RuntimeError(
            "Fix agent returned invalid decision. "
            "Expected fix, no_fix, or needs_human_review."
        )

    changes = data.get("changes", [])

    if not isinstance(changes, list):
        raise RuntimeError("Fix agent 'changes' must be a list.")

    return data


def validate_relative_path(raw_path: Any) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise RuntimeError("Change contains an invalid file path.")

    candidate = Path(raw_path)

    if candidate.is_absolute():
        raise RuntimeError(
            f"Absolute paths are not allowed in changes: {raw_path}"
        )

    if ".." in candidate.parts:
        raise RuntimeError(
            f"Path traversal is not allowed: {raw_path}"
        )

    relative = Path(raw_path.replace("/", os.sep))
    absolute = (ROOT / relative).resolve()

    try:
        absolute.relative_to(APP_ROOT.resolve())
    except ValueError as exc:
        raise RuntimeError(
            f"Change must target APP_EKS: {raw_path}"
        ) from exc

    if any(
        part.lower() in {
            "node_modules",
            ".git",
            ".venv",
            "__pycache__",
            "build",
            "dist",
        }
        for part in relative.parts
    ):
        raise RuntimeError(
            f"Changes to generated/dependency files are not allowed: "
            f"{raw_path}"
        )

    return absolute


# ---------------------------------------------------------------------------
# Backup / apply / rollback
# ---------------------------------------------------------------------------

def backup_file(path: Path, backup_dir: Path) -> Path:
    relative = path.relative_to(ROOT)
    target = backup_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)

    shutil.copy2(path, target)

    return target


def apply_changes(
    changes: list[dict[str, Any]],
    backup_dir: Path,
) -> list[dict[str, str]]:
    applied: list[dict[str, str]] = []

    # Validate everything before modifying the first file.
    validated: list[tuple[Path, dict[str, Any]]] = []

    for change in changes:
        if not isinstance(change, dict):
            raise RuntimeError("Every change must be a JSON object.")

        path = validate_relative_path(change.get("path"))

        old_text = change.get("old_text")
        new_text = change.get("new_text")

        if not isinstance(old_text, str):
            raise RuntimeError(
                f"Missing string old_text for {change.get('path')}"
            )

        if not isinstance(new_text, str):
            raise RuntimeError(
                f"Missing string new_text for {change.get('path')}"
            )

        if not path.exists():
            raise RuntimeError(
                f"Target file does not exist: {change.get('path')}"
            )

        current = path.read_text(encoding="utf-8")

        occurrences = current.count(old_text)

        if occurrences != 1:
            raise RuntimeError(
                f"Expected exactly one old_text match in "
                f"{change.get('path')}, found {occurrences}."
            )

        validated.append((path, change))

    # Create backups before applying.
    for path, _ in validated:
        backup_file(path, backup_dir)

    try:
        for path, change in validated:
            current = path.read_text(encoding="utf-8")

            updated = current.replace(
                change["old_text"],
                change["new_text"],
                1,
            )

            path.write_text(updated, encoding="utf-8")

            relative = path.relative_to(ROOT).as_posix()

            applied.append(
                {
                    "path": relative,
                    "reason": str(change.get("why", "")),
                }
            )

    except Exception:
        # Restore every backed-up file.
        for path, _ in validated:
            backup_path = backup_dir / path.relative_to(ROOT)

            if backup_path.exists():
                shutil.copy2(backup_path, path)

        raise

    return applied


def rollback_changes(
    applied: list[dict[str, str]],
    backup_dir: Path,
) -> None:
    for item in applied:
        relative = Path(item["path"].replace("/", os.sep))
        target = ROOT / relative
        backup_path = backup_dir / relative

        if backup_path.exists():
            shutil.copy2(backup_path, target)


# ---------------------------------------------------------------------------
# Safe verification
# ---------------------------------------------------------------------------


def run_command(
    command: list[str],
    cwd: Path,
    timeout: int = 180,
) -> dict[str, Any]:
    started = time.monotonic()

    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )

        return {
            "command": " ".join(command),
            "returncode": completed.returncode,
            "status": "passed" if completed.returncode == 0 else "failed",
            "duration_seconds": round(
                time.monotonic() - started,
                2,
            ),
            "stdout": completed.stdout[-6000:],
            "stderr": completed.stderr[-6000:],
        }

    except FileNotFoundError:
        return {
            "command": " ".join(command),
            "returncode": None,
            "status": "skipped",
            "duration_seconds": round(
                time.monotonic() - started,
                2,
            ),
            "stdout": "",
            "stderr": "Required executable was not found.",
        }

    except subprocess.TimeoutExpired as exc:
        return {
            "command": " ".join(command),
            "returncode": -1,
            "status": "failed",
            "duration_seconds": round(
                time.monotonic() - started,
                2,
            ),
            "stdout": str(exc.stdout or "")[-6000:],
            "stderr": "Command timed out.",
        }



def run_verification(
    applied: list[dict[str, str]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    changed_paths = [item["path"].lower() for item in applied]

    backend_changed = any(
        p == "app_eks/app.py"
        or p.startswith("app_eks/backend/")
        for p in changed_paths
    )

    frontend_changed = any(
        p.startswith("app_eks/frontend/")
        for p in changed_paths
    )

    python_exe = ROOT / ".venv" / "Scripts" / "python.exe"

    if backend_changed and python_exe.exists():
        backend_targets = []

        root_app = APP_ROOT / "app.py"
        backend_dir = APP_ROOT / "backend"

        if root_app.exists():
            backend_targets.append(root_app)

        if backend_dir.exists():
            backend_targets.append(backend_dir)

        if backend_targets:
            for target in backend_targets:
                results.append(
                    run_command(
                        [
                            str(python_exe),
                            "-m",
                            "py_compile",
                            str(target),
                        ],
                        ROOT,
                    )
                    if target.is_file()
                    else run_command(
                        [
                            str(python_exe),
                            "-m",
                            "compileall",
                            "-q",
                            str(target),
                        ],
                        ROOT,
                    )
                )

    frontend_dir = APP_ROOT / "frontend"
    package_json = frontend_dir / "package.json"

    if frontend_changed and package_json.exists():
        try:
            package = json.loads(
                package_json.read_text(encoding="utf-8")
            )
        except Exception:
            package = {}

        scripts = package.get("scripts", {})

        if isinstance(scripts, dict) and "build" in scripts:
            npm = shutil.which("npm.cmd") or shutil.which("npm")

            if npm:
                results.append(
                    run_command(
                        [npm, "run", "build"],
                        frontend_dir,
                        timeout=300,
                    )
                )
            else:
                results.append(
                    {
                        "command": "npm run build",
                        "returncode": None,
                        "status": "skipped",
                        "duration_seconds": 0,
                        "stdout": "",
                        "stderr": "npm/npm.cmd was not found on PATH.",
                    }
                )

    return results


def verification_passed(results: list[dict[str, Any]]) -> bool:
    if not results:
        return False

    return bool(results) and all(result.get("status") == "passed" for result in results)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_prompt(
    report_path: Path,
    report_text: str,
    category: str,
    source_context: str,
    source_files: list[str],
) -> str:
    source_index = "\n".join(f"- {p}" for p in source_files)

    return f"""
You are the code-repair engineer for an application.

You must investigate the supplied RCA report against the CURRENT source code.
Do not assume the RCA is correct merely because it says "high confidence".
The current source code is authoritative for what exists now.

Application root:
{APP_ROOT}

RCA report:
{report_path.relative_to(ROOT).as_posix()}

Application category:
{category}

SOURCE FILE INDEX:
{source_index}

CURRENT SOURCE CODE:
{source_context}

RCA REPORT CONTENT:
{report_text}

Your task:

1. Determine whether the reported problem is still reproducible from the
   current source code and the evidence in the RCA.
2. Compare the RCA claims with the actual source.
3. Explicitly identify discrepancies, stale-code possibilities, or missing
   evidence.
4. Decide one of:
   - "fix" when a justified source change is supported by evidence.
   - "no_fix" when current code already handles the reported condition or the
     RCA does not require a source change.
   - "needs_human_review" when evidence is insufficient, contradictory, risky,
     or the required change cannot be justified safely.
5. For "fix", propose the smallest justified change.
6. Never invent files, functions, APIs, tests, or evidence.
7. Never modify generated files, node_modules, build output, .git, or .venv.
8. Do not propose deployment commands.
9. Do not blindly map an error message to a predefined fix.
10. Prefer backward-compatible and minimal changes.
11. A change must use exact old_text copied from the supplied current source.
12. old_text must identify exactly one occurrence in the file.
13. A "fix" must directly mitigate the observed failure in the RCA. Do not
    convert preventive recommendations into an immediate code fix.
14. Do not make unrelated changes to analytics, formatting, tooling,
    configuration, dependencies, or project metadata.
15. Do not modify angular.json, package.json, Dockerfile, or other project
    configuration unless the RCA evidence specifically identifies that
    configuration as part of the failure.
16. When the RCA says a frontend exception occurred but the current frontend
    source already handles the reported condition, do NOT invent a backend
    enhancement as a substitute. Choose "no_fix" or "needs_human_review".
17. When evidence crosses backend and frontend layers, inspect both before
    deciding.
18. The proposed change must name the exact current function, handler, route,
    or code path it fixes and explain the causal connection to the RCA.
19. Prefer one minimal source change. Multiple files require explicit causal
    justification.

Return ONLY valid JSON using this exact structure:

{{
  "decision": "fix|no_fix|needs_human_review",
  "confidence": "high|medium|low",
  "root_cause_assessment": "string",
  "discrepancies": ["string"],
  "reasoning": "string",
  "changes": [
    {{
      "path": "APP_EKS/...",
      "old_text": "exact existing text",
      "new_text": "replacement text",
      "why": "specific justification"
    }}
  ],
  "verification_notes": "string"
}}

For "no_fix" or "needs_human_review", changes must be [].
"""


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------

def write_fix_report(
    report_path: Path,
    category: str,
    plan: dict[str, Any],
    verification: list[dict[str, Any]],
    applied: list[dict[str, str]],
    backup_dir: Path | None,
) -> Path:
    FIX_REPORT_ROOT.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime(
        "%Y%m%d-%H%M%S-%f"
    )

    output = FIX_REPORT_ROOT / f"fix-report-{timestamp}.md"

    lines = [
        "# Code Fix Report",
        "",
        f"**Generated:** "
        f"{datetime.now(timezone.utc).isoformat()}",
        f"**Fix agent:** {AGENT_NAME}",
        f"**Application layer:** {category}",
        f"**RCA report:** "
        f"{report_path.relative_to(ROOT).as_posix()}",
        f"**Decision:** {plan.get('decision')}",
        f"**Confidence:** {plan.get('confidence')}",
        "",
        "## Root Cause Assessment",
        "",
        str(plan.get("root_cause_assessment", "")),
        "",
        "## Reasoning",
        "",
        str(plan.get("reasoning", "")),
        "",
        "## Discrepancies",
        "",
    ]

    discrepancies = plan.get("discrepancies", [])

    if discrepancies:
        for item in discrepancies:
            lines.append(f"- {item}")
    else:
        lines.append("- None identified.")

    lines.extend(
        [
            "",
            "## Changes Applied",
            "",
        ]
    )

    if applied:
        for item in applied:
            lines.append(f"- `{item['path']}`")
            if item["reason"]:
                lines.append(f"  - {item['reason']}")
    else:
        lines.append("- No source changes were applied.")

    lines.extend(
        [
            "",
            "## Verification",
            "",
        ]
    )

    if verification:
        for result in verification:
            status = (
                "PASS"
                if result["returncode"] == 0
                else "FAIL"
            )

            lines.extend(
                [
                    f"### {status}: `{result['command']}`",
                    "",
                    f"- Return code: `{result['returncode']}`",
                    f"- Duration: "
                    f"`{result['duration_seconds']}s`",
                    "",
                    "```text",
                    result["stdout"],
                    result["stderr"],
                    "```",
                    "",
                ]
            )
    else:
        lines.append(
            "No automatic verification command was available."
        )

    lines.extend(
        [
            "## Verification Notes",
            "",
            str(plan.get("verification_notes", "")),
            "",
        ]
    )

    if backup_dir:
        lines.extend(
            [
                "## Backup",
                "",
                f"Backup created at: "
                f"`{backup_dir.relative_to(ROOT).as_posix()}`",
                "",
            ]
        )

    output.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )

    return output


# ---------------------------------------------------------------------------
# One report processing cycle
# ---------------------------------------------------------------------------

def process_report(report_path: Path) -> None:
    log(f"Processing RCA report: {report_path}")

    report_text = report_path.read_text(encoding="utf-8")
    category = infer_category(report_text, report_path)

    log(f"Detected application category: {category}")

    source_context, source_files = collect_source_context(
        category,
        report_text,
    )

    log(
        f"Collected {len(source_files)} source files "
        f"for investigation."
    )

    if not source_files:
        raise RuntimeError(
            "No current application source files were found."
        )

    prompt = build_prompt(
        report_path,
        report_text,
        category,
        source_context,
        source_files,
    )

    raw_plan = call_fix_agent(prompt)
    plan = parse_fix_plan(raw_plan)

    log(
        f"Fix agent decision: {plan.get('decision')} "
        f"(confidence={plan.get('confidence')})"
    )

    applied: list[dict[str, str]] = []
    verification: list[dict[str, Any]] = []
    backup_dir: Path | None = None

    decision = plan["decision"]

    if decision == "fix":
        changes = plan.get("changes", [])

        if not changes:
            raise RuntimeError(
                "Fix agent selected 'fix' but returned no changes."
            )

        timestamp = datetime.now(timezone.utc).strftime(
            "%Y%m%d-%H%M%S-%f"
        )

        backup_dir = BACKUP_ROOT / timestamp
        backup_dir.mkdir(parents=True, exist_ok=True)

        log(f"Applying {len(changes)} validated change(s)...")

        applied = apply_changes(
            changes,
            backup_dir,
        )

        log("Source changes applied locally.")

        verification = run_verification(applied)

        if not verification_passed(verification):
            log(
                "Verification failed. Restoring backed-up source files."
            )

            rollback_changes(
                applied,
                backup_dir,
            )

            plan["verification_notes"] = (
                str(plan.get("verification_notes", ""))
                + "\nAutomatic verification failed; changes were rolled back."
            )

            applied = []

        else:
            log("Verification passed.")

    elif decision == "no_fix":
        log(
            "No source fix required according to the current evidence."
        )

    else:
        log(
            "Human review required. No source changes will be made."
        )

    output = write_fix_report(
        report_path,
        category,
        plan,
        verification,
        applied,
        backup_dir,
    )

    log(f"Fix report written: {output}")

    mark_processed(report_path)


# ---------------------------------------------------------------------------
# Watch-mode startup policy
# ---------------------------------------------------------------------------

def initialize_watch_state(process_existing: bool) -> None:
    """
    By default, treat reports that already exist when the watcher starts as
    historical. New reports created after startup will still be processed.

    Use --process-existing when an explicit backlog replay is desired.
    """
    if process_existing:
        return

    state = load_state()
    processed = set(state.get("processed_reports", []))

    existing = sorted(
        AGENT_REPORT_ROOT.rglob("rca-report-*.md"),
        key=lambda p: p.stat().st_mtime,
    )

    for report in existing:
        processed.add(str(report.resolve()))

    state["processed_reports"] = sorted(processed)[-500:]
    save_state(state)

    if existing:
        log(
            f"Watch-mode baseline established: "
            f"{len(existing)} existing RCA report(s) marked historical."
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="RCA repair and documentation self-healing agent"
    )
    parser.add_argument("--once", action="store_true", help="Process one RCA report and exit.")
    parser.add_argument("--report", type=str, default=None, help="Explicit RCA report path.")
    parser.add_argument("--process-existing", action="store_true", help="Process existing RCA reports when watch mode starts.")
    parser.add_argument("--docs-once", action="store_true", help="Document current uncommitted source/config changes, then exit.")
    parser.add_argument("--process-existing-docs", action="store_true", help="Analyze current repository source on startup.")
    parser.add_argument("--no-docs", action="store_true", help="Disable documentation watching.")
    args = parser.parse_args()

    log(f"Fix Agent: {AGENT_NAME}")
    log(f"RCA report root: {AGENT_REPORT_ROOT}")
    log(f"Application root: {APP_ROOT}")
    log(f"Fix reports: {FIX_REPORT_ROOT}")

    if args.report:
        report = Path(args.report).resolve()
        if not report.exists():
            raise RuntimeError(f"Specified RCA report does not exist: {report}")
        process_report(report)
        return
    if args.once:
        report = find_latest_report()
        if not report:
            raise RuntimeError(f"No RCA reports found under {AGENT_REPORT_ROOT}")
        process_report(report)
        return

    healer = DocumentationHealer(ROOT, FIX_ROOT, call_fix_agent, log)
    if args.docs_once:
        healer.sync_working_tree_once()
        return

    initialize_watch_state(args.process_existing)
    if not args.no_docs:
        healer.initialize_watch(args.process_existing_docs)
        healer.log_configuration()
    log(f"Watching RCA reports every {POLL_SECONDS:.1f}s...")

    while True:
        try:
            reports = find_new_reports()
            for report in reports:
                try:
                    process_report(report)
                except Exception as exc:
                    log(f"Fix Agent error for {report.name}: {exc!r}")
                    mark_processed(report)

            if not args.no_docs:
                try:
                    healer.poll_local_changes()
                except Exception as exc:
                    log(f"Local documentation sync failed and will retry: {exc!r}")
                try:
                    healer.poll_merged_pull_requests()
                except Exception as exc:
                    log(f"Merged-PR documentation sync failed and will retry: {exc!r}")
        except KeyboardInterrupt:
            log("Fix Agent stopped.")
            return
        except Exception as exc:
            log(f"Watcher error: {exc!r}")
        try:
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            log("Fix Agent stopped.")
            return

if __name__ == "__main__":
    main()