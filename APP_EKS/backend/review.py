import re
from pathlib import Path
from typing import Any


SAMPLE_TICKET_PATH = (
    Path(__file__).parent.parent / "tests" / "jira" / "STU-104.md"
)
SAMPLE_DIFF_PATH = (
    Path(__file__).parent.parent / "tests" / "jira" / "STU-104.diff"
)

_HEADING_RE = re.compile(
    r"^\s{0,3}#{1,6}\s*(?:\d+(?:\.\d+)*\.?\s*)?"
    r"(?:acceptance criteria|acceptance criteria and definition of done|ac)\b",
    re.IGNORECASE,
)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+?)\s*$")
_DIFF_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_WORD_RE = re.compile(r"[a-z][a-z0-9]*|\b\d+\b", re.IGNORECASE)
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "if", "in", "into", "is", "it", "its", "must", "of",
    "on", "or", "should", "that", "the", "their", "then", "this", "to",
    "when", "with", "without", "user", "users", "application", "system",
}
_MET_THRESHOLD = 0.7
_PARTIAL_THRESHOLD = 0.25


def _criterion_tokens(text: str) -> set[str]:
    tokens = set()
    for raw_token in _WORD_RE.findall(text):
        token = raw_token.lower()
        if token.endswith("ies") and len(token) > 4:
            token = token[:-3] + "y"
        elif token.endswith("ing") and len(token) > 5:
            token = token[:-3]
            if len(token) > 2 and token[-1] == token[-2]:
                token = token[:-1]
        elif token.endswith("ed") and len(token) > 4:
            token = token[:-2]
        elif token.endswith("s") and not token.endswith("ss") and len(token) > 3:
            token = token[:-1]
        if token not in _STOP_WORDS and (len(token) > 2 or token.isdigit()):
            tokens.add(token)
    return tokens


def extract_acceptance_criteria(ticket: str) -> list[str]:
    lines = ticket.splitlines()
    in_criteria_section = False
    criteria: list[str] = []

    for line in lines:
        if _HEADING_RE.match(line):
            in_criteria_section = True
            continue
        if in_criteria_section and re.match(r"^\s{0,3}#{1,6}\s+", line):
            in_criteria_section = False
        if not in_criteria_section:
            continue

        match = _LIST_ITEM_RE.match(line)
        if match:
            criterion = re.sub(r"^\[[ xX]\]\s*", "", match.group(1)).strip()
            if criterion:
                criteria.append(criterion)

    return criteria


def _diff_evidence(diff_text: str) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    current_file: str | None = None
    new_line_number: int | None = None

    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            current_file = line[4:].strip()
            if current_file.startswith("b/"):
                current_file = current_file[2:]
            if current_file == "/dev/null":
                current_file = None
            new_line_number = None
            continue

        hunk = _DIFF_HUNK_RE.match(line)
        if hunk:
            new_line_number = int(hunk.group(1))
            continue

        if line.startswith("diff --git "):
            current_file = None
            new_line_number = None
            continue

        if new_line_number is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if current_file:
                evidence.append({
                    "file": current_file,
                    "line": new_line_number,
                    "text": line[1:].strip(),
                })
            new_line_number += 1
        elif line.startswith(" "):
            new_line_number += 1

    return evidence


def _file_evidence(changed_files: list[dict[str, str]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for changed_file in changed_files:
        path = changed_file["path"].replace("\\", "/")
        for line_number, line in enumerate(
            changed_file["content"].splitlines(), start=1
        ):
            if line.strip():
                evidence.append({
                    "file": path,
                    "line": line_number,
                    "text": line.strip(),
                })
    return evidence


def analyze_review(
    ticket: str,
    diff_text: str = "",
    changed_files: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    criteria = extract_acceptance_criteria(ticket)
    if not criteria:
        raise ValueError(
            "Ticket must include an 'Acceptance Criteria' heading "
            "with at least one bullet or numbered requirement"
        )

    evidence_lines = _diff_evidence(diff_text)
    if changed_files:
        evidence_lines.extend(_file_evidence(changed_files))
    if not evidence_lines:
        raise ValueError(
            "Provide a unified diff with added lines or at least one "
            "changed file"
        )

    results = []
    for criterion in criteria:
        tokens = _criterion_tokens(criterion)
        matching_tokens = {
            token
            for item in evidence_lines
            for token in _criterion_tokens(item["text"])
            if token in tokens
        }
        coverage = len(matching_tokens) / len(tokens) if tokens else 0.0

        if tokens and coverage >= _MET_THRESHOLD:
            status = "met"
        elif tokens and coverage >= _PARTIAL_THRESHOLD:
            status = "partially_met"
        else:
            status = "missing"

        matching_lines = []
        for item in evidence_lines:
            line_tokens = _criterion_tokens(item["text"])
            overlap = line_tokens & tokens
            if overlap:
                matching_lines.append((len(overlap), item))
        matching_lines.sort(key=lambda pair: pair[0], reverse=True)
        selected_evidence = []
        seen_locations = set()
        for _, item in matching_lines:
            location = (item["file"], item["line"])
            if location in seen_locations:
                continue
            selected_evidence.append(item)
            seen_locations.add(location)
            if len(selected_evidence) == 3:
                break

        if status == "missing":
            note = "No changed line shares meaningful keywords with this criterion."
        elif status == "partially_met":
            note = "Some criterion keywords appear in the changes; verify the behavior manually."
        else:
            note = "Most criterion keywords appear in the changes; this does not prove the behavior."

        results.append({
            "criterion": criterion,
            "status": status,
            "keyword_coverage": round(coverage, 2),
            "evidence": selected_evidence,
            "note": note,
        })

    summary = {
        status: sum(result["status"] == status for result in results)
        for status in ("met", "partially_met", "missing")
    }
    return {
        "method": "keyword_coverage_heuristic",
        "notice": (
            "Provisional keyword-based review only. It does not understand "
            "code semantics, execute tests, or establish that a requirement "
            "is implemented. Verify every finding manually."
        ),
        "summary": summary,
        "criteria": results,
        "documentation_draft": _documentation_draft(ticket, results),
    }


def _documentation_draft(
    ticket: str,
    results: list[dict[str, Any]],
) -> str:
    title = next(
        (
            line.lstrip("# ").strip()
            for line in ticket.splitlines()
            if line.startswith("# ")
        ),
        "Jira implementation review",
    )
    lines = [
        f"## {title} — documentation update draft",
        "",
        "> Draft generated from keyword matches. Verify the implementation and "
        "tests before copying any statement into maintained documentation.",
        "",
        "### Functional documentation candidate",
        "",
    ]

    for result in results:
        label = result["status"].replace("_", " ").title()
        lines.append(f"- **{label} (provisional):** {result['criterion']}")
        for evidence in result["evidence"]:
            path = evidence["file"].replace("`", "\\`")
            text = evidence["text"].replace("`", "\\`")
            lines.append(
                f"  - Evidence: `{path}:{evidence['line']}` - `{text}`"
            )
        if not result["evidence"]:
            lines.append("  - Evidence: no matching changed line was found.")

    lines.extend([
        "",
        "### Technical documentation follow-up",
        "",
        "- Confirm each status against the full implementation and relevant tests.",
        "- Document only verified behavior, API changes, data changes, and "
        "operational requirements in the appropriate technical or functional guide.",
        "- Resolve partially met or missing criteria before describing them as "
        "implemented.",
    ])
    return "\n".join(lines)


def load_sample_review() -> dict[str, str]:
    return {
        "ticket": SAMPLE_TICKET_PATH.read_text(encoding="utf-8"),
        "diff": SAMPLE_DIFF_PATH.read_text(encoding="utf-8"),
    }


def validate_review_payload(
    payload: Any,
) -> tuple[str, str, list[dict[str, str]]]:
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object")

    ticket = payload.get("ticket")
    if not isinstance(ticket, str) or not ticket.strip():
        raise ValueError("Ticket text is required")

    diff_text = payload.get("diff", "")
    if not isinstance(diff_text, str):
        raise ValueError("'diff' must be a string")

    raw_files = payload.get("changed_files", [])
    if not isinstance(raw_files, list) or len(raw_files) > 20:
        raise ValueError("'changed_files' must be a list of at most 20 files")

    changed_files = []
    for changed_file in raw_files:
        if not isinstance(changed_file, dict):
            raise ValueError("Each changed file must include path and content")
        path = changed_file.get("path")
        content = changed_file.get("content")
        if (
            not isinstance(path, str)
            or not path.strip()
            or "\x00" in path
            or not isinstance(content, str)
        ):
            raise ValueError("Each changed file must include a valid path and text content")
        changed_files.append({"path": path.strip(), "content": content})

    if not diff_text.strip() and not changed_files:
        raise ValueError("Provide a unified diff or at least one changed file")

    return ticket, diff_text, changed_files
