import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv


HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java",
    ".js", ".jsx", ".kt", ".kts", ".php", ".py", ".rb", ".rs",
    ".scala", ".sc", ".sql", ".swift", ".ts", ".tsx", ".vue",
}
IGNORED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "build", "dist", "target",
    "vendor", "site-packages", "coverage", "__pycache__",
}
EXCLUDED_PATH_PARTS = {
    "auth", "authorization", "certificate", "certificates", "config",
    "configuration", "credential", "credentials", "encryption", "key",
    "keys", "security", "secret", "secrets",
}
MAX_SOURCE_FILES = 120
MAX_FILE_CHARS = 20000
MAX_CONTEXT_CHARS = 100000
MAX_DIFF_CHARS = 100000
TICKET_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
DIFF_PATH_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)

_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"(?i)\b([A-Za-z0-9_.-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|credential|private[_-]?key|authorization|connection[_-]?string)[A-Za-z0-9_.-]*)(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"), r"\1\2[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "[REDACTED TOKEN]"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[REDACTED CLOUD KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "[REDACTED TOKEN]"),
    (re.compile(r"https?://[^:/\s]+:[^@/\s]+@", re.IGNORECASE), "https://[REDACTED]@"),
]


def redact_sensitive_text(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def is_application_source(path: str) -> bool:
    normalized = path.replace("\\", "/")
    parts = [part.lower() for part in normalized.split("/")]
    file_stem = Path(parts[-1]).stem
    excluded_markers = ("auth", "security", "secret", "credential", "config", "certificate", "encryption")
    return (
        Path(parts[-1]).suffix.lower() in SOURCE_SUFFIXES
        and not any(part in IGNORED_DIRS for part in parts)
        and not any(part in EXCLUDED_PATH_PARTS for part in parts)
        and not any(marker in file_stem for marker in excluded_markers)
        and not parts[-1].startswith(".")
    )


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode:
        detail = result.stderr.strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "Git command failed")
    return result.stdout


def _eligible_diff(diff: str) -> tuple[str, list[str], bool]:
    sections = re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE)
    eligible: list[str] = []
    paths: list[str] = []
    excluded = False
    for section in sections:
        match = DIFF_PATH_RE.search(section)
        if not match:
            continue
        if is_application_source(match.group(2)):
            eligible.append(section)
            paths.append(match.group(2))
        else:
            excluded = True
    return "".join(eligible), sorted(set(paths)), excluded


def collect_changes(repo: Path, base_ref: str) -> tuple[str, list[str]]:
    raw_diff = _run_git(repo, "diff", "--no-ext-diff", "--unified=5", base_ref, "--")
    diff, changed, excluded = _eligible_diff(raw_diff)
    untracked = _run_git(repo, "ls-files", "--others", "--exclude-standard")
    added_paths = [
        path.strip() for path in untracked.splitlines()
        if path.strip() and is_application_source(path.strip())
    ]
    excluded = excluded or len(added_paths) < len(untracked.splitlines())
    added_blocks = []
    for relative in added_paths[:MAX_SOURCE_FILES]:
        path = (repo / relative).resolve()
        try:
            path.relative_to(repo.resolve())
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        added_blocks.append(
            f"\n===== UNTRACKED SOURCE: {relative} =====\n"
            f"{content[:MAX_FILE_CHARS]}\n===== END SOURCE =====\n"
        )
    changed.extend(path for path in added_paths if path not in changed)
    diff = diff[:MAX_DIFF_CHARS] + "\n".join(added_blocks)
    if excluded:
        diff += "\nSensitive or configuration-only changes were excluded from analysis.\n"
    if not diff.strip():
        raise ValueError("No eligible application source changes found against the selected base.")
    return redact_sensitive_text(diff[:MAX_DIFF_CHARS]), changed[:MAX_SOURCE_FILES]


def load_diff(diff_path: Path) -> tuple[str, list[str]]:
    raw_diff = diff_path.read_text(encoding="utf-8")
    diff, changed, excluded = _eligible_diff(raw_diff)
    if not changed:
        raise ValueError("The supplied diff contains no eligible application source files.")
    if excluded:
        diff += "\nSensitive or configuration-only changes were excluded from analysis.\n"
    return redact_sensitive_text(diff[:MAX_DIFF_CHARS]), changed[:MAX_SOURCE_FILES]


def _source_symbols(path: Path) -> set[str]:
    try:
        content = path.read_text(encoding="utf-8")[:MAX_FILE_CHARS]
    except (OSError, UnicodeDecodeError):
        return set()
    declarations = re.findall(
        r"\b(?:def|class|function|interface|type|struct|enum)\s+([A-Za-z_$][\w$]*)",
        content,
    )
    return {symbol for symbol in declarations if len(symbol) > 2}


def collect_source_context(repo: Path, changed_paths: list[str]) -> tuple[str, bool]:
    changed_set = set(changed_paths)
    symbols: set[str] = set()
    for relative in changed_paths:
        if is_application_source(relative):
            symbols.update(_source_symbols((repo / relative).resolve()))

    candidates: list[tuple[int, str, Path]] = []
    if symbols:
        for directory, names, filenames in os.walk(repo):
            names[:] = [
                name for name in names
                if name.lower() not in IGNORED_DIRS
                and name.lower() not in EXCLUDED_PATH_PARTS
            ]
            for filename in filenames:
                path = Path(directory) / filename
                relative = path.relative_to(repo).as_posix()
                if relative in changed_set or not is_application_source(relative):
                    continue
                try:
                    content = path.read_text(encoding="utf-8")[:MAX_FILE_CHARS]
                except (OSError, UnicodeDecodeError):
                    continue
                matches = sum(bool(re.search(rf"\b{re.escape(symbol)}\b", content)) for symbol in symbols)
                if matches:
                    candidates.append((matches, relative, path))
    candidates.sort(key=lambda item: (-item[0], item[1].lower()))

    blocks: list[str] = []
    total = 0
    excluded_content = False
    for relative in changed_paths:
        path = (repo / relative).resolve()
        if not is_application_source(relative):
            excluded_content = True
            continue
        try:
            path.relative_to(repo.resolve())
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError, ValueError):
            excluded_content = True
            continue
        if len(content) > MAX_FILE_CHARS:
            excluded_content = True
        block = f"\n===== CHANGED SOURCE: {relative} =====\n{content[:MAX_FILE_CHARS]}\n===== END SOURCE =====\n"
        if total + len(block) > MAX_CONTEXT_CHARS:
            excluded_content = True
            continue
        blocks.append(block)
        total += len(block)

    for _, relative, path in candidates[:30]:
        try:
            content = path.read_text(encoding="utf-8")[:MAX_FILE_CHARS]
        except (OSError, UnicodeDecodeError):
            excluded_content = True
            continue
        if len(content) > MAX_FILE_CHARS:
            excluded_content = True
        block = f"\n===== RELATED SOURCE: {relative} =====\n{content}\n===== END SOURCE =====\n"
        if total + len(block) > MAX_CONTEXT_CHARS:
            excluded_content = True
            break
        blocks.append(block)
        total += len(block)
    return redact_sensitive_text("".join(blocks)), excluded_content


def ticket_key(ticket_path: Path, ticket: str) -> str:
    match = TICKET_KEY_RE.search(ticket) or TICKET_KEY_RE.search(ticket_path.stem.upper())
    if not match:
        raise ValueError("Could not determine a Jira key (expected format such as STU-104).")
    return match.group(0)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required Azure setting: {name} (set it in Self_Healing/.env).")
    return value


def call_azure_agent(prompt: str) -> str:
    project_endpoint = _required_env("AZURE_AI_PROJECT_ENDPOINT").rstrip("/")
    agent_name = os.getenv("AGENT_NAME", "").strip() or _required_env("AGENT_NAME_2")
    api_key = _required_env("API_KEY")
    api_version = os.getenv("API_VERSION", "v1").strip() or "v1"
    endpoint = (
        f"{project_endpoint}/agents/{quote(agent_name, safe='')}"
        f"/endpoint/protocols/openai/responses?api-version={quote(api_version, safe='')}"
    )
    try:
        with requests.post(
            endpoint,
            headers={"api-key": api_key, "Content-Type": "application/json"},
            json={"input": prompt, "stream": True},
            timeout=(10, 240),
            stream=True,
        ) as response:
            if response.status_code >= 400:
                raise RuntimeError(f"Azure AI Agent returned HTTP {response.status_code}.")
            response.encoding = "utf-8"
            output: list[str] = []
            event_name = ""
            data_lines: list[str] = []
            for raw_line in response.iter_lines(decode_unicode=True):
                line = (raw_line or "").strip()
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif not line:
                    if event_name and data_lines:
                        try:
                            payload = json.loads("\n".join(data_lines))
                        except json.JSONDecodeError:
                            payload = {}
                        if event_name == "response.output_text.delta":
                            output.append(str(payload.get("delta", "")))
                        elif event_name in {"response.error", "error"}:
                            raise RuntimeError("Azure AI Agent reported a response error.")
                    event_name = ""
                    data_lines = []
    except requests.RequestException as exc:
        raise RuntimeError(f"Azure AI Agent request failed: {exc.__class__.__name__}.") from exc
    result = "".join(output).strip()
    if not result:
        raise RuntimeError("Azure AI Agent returned no report text.")
    return result


def parse_agent_result(response_text: str) -> tuple[str, str]:
    text = response_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Azure response was not the required JSON report object.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Azure response must be a JSON object.")
    classification = str(payload.get("classification", "")).strip().upper()
    valid = {"TECHNICAL", "FUNCTIONAL", "BOTH TECHNICAL AND FUNCTIONAL"}
    if classification not in valid:
        raise ValueError("Azure response included an unsupported classification.")
    report = payload.get("report_markdown")
    if not isinstance(report, str) or not report.strip():
        raise ValueError("Azure response did not include report_markdown.")
    return classification, redact_sensitive_text(report.strip()) + "\n"


def output_paths(directory: Path, key: str, classification: str) -> list[Path]:
    prefixes = {
        "TECHNICAL": ["technical"],
        "FUNCTIONAL": ["functional"],
        "BOTH TECHNICAL AND FUNCTIONAL": ["functional", "technical"],
    }
    return [directory / f"{prefix}_{key}.md" for prefix in prefixes[classification]]


def build_prompt(ticket: str, diff: str, source_context: str, excluded: bool) -> str:
    instructions = (HERE / "analysis_prompt.md").read_text(encoding="utf-8")
    if excluded:
        source_context += "\nSome source content was unavailable or excluded from analysis."
    values = {
        "ticket": ticket,
        "diff": diff,
        "source_context": source_context or "No readable changed source context was available.",
    }
    return re.sub(
        r"\{(ticket|diff|source_context)\}",
        lambda match: values[match.group(1)],
        instructions,
    )


def analyze_ticket(
    ticket_path: Path,
    repo: Path,
    base_ref: str,
    output_dir: Path,
    diff_path: Path | None = None,
    invoke_agent=call_azure_agent,
) -> list[Path]:
    ticket = ticket_path.read_text(encoding="utf-8")
    key = ticket_key(ticket_path, ticket)
    ticket = redact_sensitive_text(ticket)
    repo = repo.resolve()
    if diff_path:
        diff, changed_paths = load_diff(diff_path.resolve())
    else:
        diff, changed_paths = collect_changes(repo, base_ref)
    source_context, excluded = collect_source_context(repo, changed_paths)
    result = invoke_agent(build_prompt(ticket, diff, source_context, excluded))
    classification, report = parse_agent_result(result)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = output_paths(output_dir, key, classification)
    for path in paths:
        path.write_text(report, encoding="utf-8")
    return paths