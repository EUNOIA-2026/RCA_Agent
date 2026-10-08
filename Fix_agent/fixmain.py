import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Paths / configuration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = ROOT / os.getenv("FIX_APP_DIR", "Orders_Platform")
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
    ".sh",
}

TEXT_FILENAMES = {"dockerfile"}

IGNORED_PARTS = {
    "node_modules",
    ".git",
    ".venv",
    "__pycache__",
    "dist",
    "coverage",
    "build",
}

SKIPPED_FILES = {"package-lock.json"}


def is_secret_file(path: Path) -> bool:
    name = path.name.lower()
    return name.startswith(".env") or name.startswith("secret")


def is_test_file(path: Path) -> bool:
    return "tests" in (part.lower() for part in path.parts)


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
    match = re.search(
        r"\*\*Application layer:\*\*\s*(\w+)",
        report_text,
    )

    if match:
        return match.group(1).lower()

    # Reports are written to reports/<category>/.
    return report_path.parent.name.lower() or "unknown"



def iter_source_files(category: str) -> list[Path]:
    """Every text file of the application and its infrastructure, minus secrets."""
    files: list[Path] = []

    for path in sorted(APP_ROOT.rglob("*")):
        if not path.is_file():
            continue

        if IGNORED_PARTS.intersection(path.relative_to(APP_ROOT).parts):
            continue

        if path.name in SKIPPED_FILES or is_secret_file(path):
            continue

        if (
            path.suffix.lower() not in TEXT_SUFFIXES
            and path.name.lower() not in TEXT_FILENAMES
        ):
            continue

        files.append(path.resolve())

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


def extract_json_object(text: str) -> dict[str, Any] | None:
    """First JSON object in the reply that looks like a fix plan, ignoring surrounding prose."""
    decoder = json.JSONDecoder()
    position = text.find("{")

    while position != -1:
        try:
            candidate, _ = decoder.raw_decode(text[position:])
        except json.JSONDecodeError:
            candidate = None

        if isinstance(candidate, dict) and "decision" in candidate:
            return candidate

        position = text.find("{", position + 1)

    return None


def parse_fix_plan(raw: str) -> dict[str, Any]:
    cleaned = strip_code_fences(raw)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        data = extract_json_object(raw)

        if data is None:
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
            f"Change must target {APP_ROOT.name}: {raw_path}"
        ) from exc

    if is_secret_file(absolute):
        raise RuntimeError(
            f"Secrets and .env files must not be edited by the agent: {raw_path}"
        )

    if is_test_file(relative):
        raise RuntimeError(
            f"Tests must not be edited to make a fix pass: {raw_path}"
        )

    if any(
        part.lower() in IGNORED_PARTS
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



def check_result(
    name: str,
    problems: list[str],
) -> dict[str, Any]:
    return {
        "command": name,
        "returncode": 1 if problems else 0,
        "status": "failed" if problems else "passed",
        "duration_seconds": 0,
        "stdout": "\n".join(problems) or "OK",
        "stderr": "",
    }


def check_dockerfile(path: Path) -> list[str]:
    problems: list[str] = []
    context = path.parent

    instructions = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    if not instructions or not instructions[0].upper().startswith(("FROM", "ARG")):
        problems.append("Dockerfile must start with FROM (or ARG).")

    for line in instructions:
        keyword, _, rest = line.partition(" ")

        if keyword.upper() not in {"COPY", "ADD"}:
            continue

        if "--from=" in rest:
            continue

        parts = [p for p in rest.split() if not p.startswith("--")]
        sources = parts[:-1]

        if parts and parts[0].startswith("["):
            try:
                sources = json.loads(rest)[:-1]
            except json.JSONDecodeError:
                continue

        for source in sources:
            if "$" in source or source.startswith(("http://", "https://")):
                continue

            if not glob.glob(str(context / source)):
                problems.append(
                    f"{keyword} source '{source}' does not exist in build "
                    f"context {context.relative_to(ROOT).as_posix()}"
                )

    return problems


def parse_memory(value: Any) -> int | None:
    match = re.fullmatch(r"(\d+)(Ki|Mi|Gi)?", str(value))

    if not match:
        return None

    unit = {None: 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}[match.group(2)]
    return int(match.group(1)) * unit


def check_kubernetes_document(doc: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    kind = doc.get("kind")

    if not doc.get("apiVersion") or not kind:
        return ["Document is missing apiVersion or kind."]

    if kind != "Deployment":
        return problems

    spec = doc.get("spec", {})
    template = spec.get("template", {})
    selector = spec.get("selector", {}).get("matchLabels", {})
    labels = template.get("metadata", {}).get("labels", {})

    if not selector or any(labels.get(k) != v for k, v in selector.items()):
        problems.append("Deployment selector does not match pod template labels.")

    for container in template.get("spec", {}).get("containers", []):
        name = container.get("name", "?")

        if not container.get("image"):
            problems.append(f"Container {name} has no image.")

        resources = container.get("resources", {})
        requested = parse_memory(resources.get("requests", {}).get("memory"))
        limit = parse_memory(resources.get("limits", {}).get("memory"))

        if requested and limit and requested > limit:
            problems.append(f"Container {name} memory request exceeds its limit.")

    return problems


def check_yaml(path: Path) -> list[str]:
    try:
        import yaml
    except ImportError:
        return ["PyYAML is not installed; cannot validate YAML."]

    try:
        documents = [
            doc
            for doc in yaml.safe_load_all(path.read_text(encoding="utf-8"))
            if doc is not None
        ]
    except yaml.YAMLError as exc:
        return [f"Invalid YAML: {exc}"]

    problems: list[str] = []

    if "k8s" in path.parts:
        for doc in documents:
            problems.extend(check_kubernetes_document(doc))

    return problems


def check_requirements(path: Path) -> list[str]:
    pattern = re.compile(r"^[A-Za-z0-9_.\-]+(\[[A-Za-z0-9_,\-]+\])?\s*([=<>!~]=?.+)?$")

    return [
        f"Unparseable requirement: {line}"
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        and not line.strip().startswith(("#", "-"))
        and not pattern.match(line.strip())
    ]


def run_tests() -> tuple[dict[str, Any] | None, set[str]]:
    """Run the app's pytest suite; returns the raw result and the passing test ids."""
    if not (APP_ROOT / "tests").exists():
        return None, set()

    result = run_command(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA", "tests"],
        APP_ROOT,
        timeout=300,
    )

    if "No module named pytest" in result["stdout"] + result["stderr"]:
        result["status"] = "skipped"
        return result, set()

    passed = {
        line.split()[1]
        for line in result["stdout"].splitlines()
        if line.startswith("PASSED ")
    }

    return result, passed


def run_verification(
    applied: list[dict[str, str]],
    baseline_passed: set[str],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    changed = [ROOT / item["path"] for item in applied]

    for path in changed:
        relative = path.relative_to(ROOT).as_posix()
        name = path.name.lower()

        if path.suffix == ".py":
            results.append(
                run_command([sys.executable, "-m", "py_compile", str(path)], ROOT)
            )
        elif name == "dockerfile":
            results.append(check_result(f"dockerfile check {relative}", check_dockerfile(path)))
        elif path.suffix in {".yaml", ".yml"}:
            results.append(check_result(f"yaml check {relative}", check_yaml(path)))
        elif name.startswith("requirements") and path.suffix == ".txt":
            results.append(check_result(f"requirements check {relative}", check_requirements(path)))

    if any(p.suffix == ".py" for p in changed):
        test_result, passed = run_tests()

        if test_result is not None:
            regressions = sorted(baseline_passed - passed)
            fixed = sorted(passed - baseline_passed)

            if test_result["status"] != "skipped":
                test_result["status"] = "failed" if regressions else "passed"
                test_result["returncode"] = 1 if regressions else 0

            test_result["stdout"] = (
                f"Regressions: {regressions or 'none'}\n"
                f"Newly passing: {fixed or 'none'}\n\n" + test_result["stdout"]
            )
            results.append(test_result)

    frontend_dir = APP_ROOT / "frontend"
    package_json = frontend_dir / "package.json"

    if any(frontend_dir in p.parents for p in changed) and package_json.exists():
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

    return results


def verification_passed(results: list[dict[str, Any]]) -> bool:
    statuses = [result.get("status") for result in results]

    return "passed" in statuses and "failed" not in statuses


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
You are a senior site reliability engineer who also owns the code. You repair
incidents in a multi-service application (API, worker, Dockerfiles, Kubernetes
manifests, configuration).

You must investigate the supplied RCA report against the CURRENT repository.
Do not assume the RCA is correct merely because it says "high confidence".
The current repository is authoritative for what exists now.

Application root:
{APP_ROOT}

RCA report:
{report_path.relative_to(ROOT).as_posix()}

Incident category:
{category}

SOURCE FILE INDEX:
{source_index}

CURRENT SOURCE CODE:
{source_context}

RCA REPORT CONTENT:
{report_text}

Your task:

1. Determine whether the reported problem is still reproducible from the
   current repository and the evidence in the RCA.
2. Compare the RCA claims with the actual files.
3. Explicitly identify discrepancies, stale-deployment possibilities, or
   missing evidence.
4. Decide one of:
   - "fix" when a justified repository change is supported by evidence.
   - "no_fix" when the current repository already handles the reported
     condition or the RCA does not require a repository change (for example
     an external outage, or a stale deployment of already-fixed code).
   - "needs_human_review" when evidence is insufficient, contradictory, risky,
     or the required change cannot be justified safely.
5. For "fix", propose the smallest justified change.
6. Never invent files, functions, APIs, tests, values, or evidence.
7. Never modify generated files, node_modules, build output, .git, .venv,
   tests, secrets, or .env files.
8. Do not propose deployment commands; deployment steps are derived
   separately.
9. Do not blindly map an error message to a predefined fix.
10. Prefer backward-compatible and minimal changes.
11. A change must use exact old_text copied from the supplied current source.
12. old_text must identify exactly one occurrence in the file.
13. A "fix" must directly mitigate the observed failure in the RCA. Do not
    convert preventive recommendations into an immediate change.
14. Do not make unrelated changes to formatting, tooling, dependencies, or
    project metadata.
15. Infrastructure files (Dockerfile, Kubernetes manifests, docker-compose,
    configuration) may be changed only when the RCA evidence identifies them
    as part of the failure, and the corrected value must be derivable from
    the repository itself (for example a Service name, a port, a file path).
    If the right value cannot be derived, choose "needs_human_review".
16. Fix the underlying defect, not just the trigger. A resource limit should
    not be raised to hide a leak; a failing dependency should be handled with
    timeouts, retries or graceful degradation rather than ignored; a crash on
    a recoverable error should recover. Do not weaken health checks, remove
    error handling, or silence logging to make symptoms disappear.
17. When evidence crosses components, inspect all of them before deciding.
18. The proposed change must name the exact function, route, manifest key or
    instruction it fixes and explain the causal connection to the RCA.
19. Prefer one minimal change. Multiple files require explicit causal
    justification.
20. Never place credentials or secrets in any file.

Return ONLY valid JSON using this exact structure:

{{
  "decision": "fix|no_fix|needs_human_review",
  "confidence": "high|medium|low",
  "fix_target": "application_code|dockerfile|kubernetes_manifest|configuration|none",
  "root_cause_assessment": "string",
  "discrepancies": ["string"],
  "reasoning": "string",
  "changes": [
    {{
      "path": "{APP_ROOT.name}/...",
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

def deployment_steps(applied: list[dict[str, str]]) -> list[str]:
    """Derived from the files actually changed; the agent never deploys."""
    steps: list[str] = []

    for item in applied:
        relative = Path(item["path"]).relative_to(APP_ROOT.name)
        area = relative.parts[0]

        if area in {"api", "worker"}:
            step = (
                f"Rebuild and push the orders-{area} image, then "
                f"`kubectl rollout restart deployment/orders-{area}`."
            )
        elif area == "k8s":
            step = f"`kubectl apply -f {item['path']}`."
        elif relative.name.startswith("docker-compose"):
            step = "`docker compose up -d --build`."
        elif area == "frontend":
            step = "Rebuild the frontend bundle into the image build context and redeploy."
        else:
            continue

        if step not in steps:
            steps.append(step)

    return steps


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
        f"**Outcome:** {plan.get('outcome', 'no_change')}",
        f"**Fix target:** {plan.get('fix_target', 'unspecified')}",
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

    steps = deployment_steps(applied)

    if steps:
        lines.extend(["", "## Deployment Steps Required", ""])
        lines.extend(f"- {step}" for step in steps)

    lines.extend(
        [
            "",
            "## Verification",
            "",
        ]
    )

    if verification:
        for result in verification:
            status = {
                "passed": "PASS",
                "skipped": "SKIPPED",
            }.get(result["status"], "FAIL")

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

    try:
        plan = parse_fix_plan(raw_plan)
    except RuntimeError:
        log("Fix agent reply was not valid JSON; retrying once.")

        raw_plan = call_fix_agent(
            prompt
            + "\n\nYour previous reply was not valid JSON. Reply with ONLY the "
            "JSON object: no prose, no markdown."
        )

        try:
            plan = parse_fix_plan(raw_plan)
        except RuntimeError:
            FIX_REPORT_ROOT.mkdir(parents=True, exist_ok=True)
            dump = FIX_REPORT_ROOT / (
                f"invalid-response-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.txt"
            )
            dump.write_text(raw_plan, encoding="utf-8")

            raise RuntimeError(
                f"Fix agent did not return valid JSON twice. Raw reply saved to {dump}"
            ) from None

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

        _, baseline_passed = run_tests()

        try:
            applied = apply_changes(
                changes,
                backup_dir,
            )
        except RuntimeError as exc:
            log(f"Proposed change rejected: {exc}")
            plan["outcome"] = "rejected"
            plan["verification_notes"] = (
                str(plan.get("verification_notes", ""))
                + f"\nProposed change was rejected: {exc}"
            )
        else:
            log("Source changes applied locally.")

            verification = run_verification(applied, baseline_passed)

            if verification_passed(verification):
                log("Verification passed.")
                plan["outcome"] = "applied"
            else:
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
                plan["outcome"] = "rolled_back"

                applied = []

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

def acquire_watcher_lock():
    """Two watchers would process the same RCA report twice and stack duplicate edits."""
    lock_file = open(FIX_ROOT / ".watcher.lock", "w")

    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise RuntimeError(
            "Another Fix Agent watcher is already running."
        ) from None

    return lock_file


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous RCA follow-up code repair agent"
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Process one report and exit.",
    )

    parser.add_argument(
        "--report",
        type=str,
        default=None,
        help="Explicit RCA report path.",
    )

    parser.add_argument(
        "--process-existing",
        action="store_true",
        help="Process existing RCA reports when watch mode starts.",
    )

    args = parser.parse_args()

    log(f"Fix Agent: {AGENT_NAME}")
    log(f"RCA report root: {AGENT_REPORT_ROOT}")
    log(f"Application root: {APP_ROOT}")
    log(f"Fix reports: {FIX_REPORT_ROOT}")

    if args.report:
        report = Path(args.report).resolve()

        if not report.exists():
            raise RuntimeError(
                f"Specified RCA report does not exist: {report}"
            )

        process_report(report)
        return

    if args.once:
        report = find_latest_report()

        if not report:
            raise RuntimeError(
                f"No RCA reports found under {AGENT_REPORT_ROOT}"
            )

        process_report(report)
        return

    watcher_lock = acquire_watcher_lock()  # held until process exit

    initialize_watch_state(args.process_existing)

    log(
        f"Watching RCA reports every {POLL_SECONDS:.1f}s..."
    )

    while True:
        try:
            reports = find_new_reports()

            for report in reports:
                try:
                    process_report(report)
                except Exception as exc:
                    log(
                        f"Fix Agent error for {report.name}: "
                        f"{exc!r}"
                    )
                    mark_processed(report)

        except KeyboardInterrupt:
            log("Fix Agent stopped.")
            return

        except Exception as exc:
            log(f"Watcher error: {exc!r}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()