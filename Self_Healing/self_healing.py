import argparse
from pathlib import Path

from analyzer import analyze_ticket


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze a completed developer change against a Jira ticket."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze = subparsers.add_parser("analyze", help="Generate a ticket impact report.")
    analyze.add_argument("--ticket", required=True, type=Path, help="Jira ticket Markdown file.")
    analyze.add_argument("--repo", required=True, type=Path, help="Repository containing the developer change.")
    analyze.add_argument("--base-ref", default="HEAD", help="Git ref to compare against (default: HEAD).")
    analyze.add_argument("--diff", type=Path, help="Use a supplied unified diff instead of collecting Git changes.")
    analyze.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "reports",
        help="Directory for generated Markdown reports.",
    )
    args = parser.parse_args()
    try:
        paths = analyze_ticket(
            ticket_path=args.ticket,
            repo=args.repo,
            base_ref=args.base_ref,
            output_dir=args.output_dir,
            diff_path=args.diff,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"Analysis failed: {exc}\n")
    for path in paths:
        print(path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())