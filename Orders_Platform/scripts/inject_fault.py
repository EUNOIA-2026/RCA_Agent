"""
Reversible infrastructure fault injection for incident drills.

  python scripts/inject_fault.py --list
  python scripts/inject_fault.py bad_probe
  python scripts/inject_fault.py --restore bad_probe

After injecting, rebuild/redeploy (docker build, kubectl apply) so the fault is
observable by the RCA agent. Application-level incidents are triggered through
the /chaos endpoints instead (see api/chaos.py).
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# name: (file, original text, faulty text, symptom)
FAULTS = {
    "bad_probe": (
        "k8s/api.yaml",
        "path: /ready",
        "path: /readyz",
        "readiness probe 404s -> pod never Ready (Unhealthy events)",
    ),
    "low_memory_limit": (
        "k8s/api.yaml",
        "memory: 256Mi",
        "memory: 40Mi",
        "api container OOMKilled -> CrashLoopBackOff",
    ),
    "missing_config_key": (
        "k8s/configmap.yaml",
        "REDIS_URL:",
        "REDIS_URI:",
        "required REDIS_URL absent -> CONFIG_ERROR crash loop",
    ),
    "bad_dependency_host": (
        "k8s/configmap.yaml",
        "redis://redis:6379/0",
        "redis://redis-master:6379/0",
        "redis host does not resolve -> DEPENDENCY_ERROR",
    ),
    "bad_dockerfile_copy": (
        "api/Dockerfile",
        "COPY requirements.txt .",
        "COPY requirement.txt .",
        "docker build fails (feed the build log to Agent/incoming/)",
    ),
}


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_bytes().decode("utf-8")

    if text.count(old) != 1:
        sys.exit(f"Expected exactly one occurrence of {old!r} in {path}, found {text.count(old)}")

    path.write_bytes(text.replace(old, new, 1).encode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fault", nargs="?", choices=sorted(FAULTS))
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.list or not args.fault:
        for name, (file, _, _, symptom) in FAULTS.items():
            print(f"{name:22} {file:20} {symptom}")
        return

    file, old, new, _ = FAULTS[args.fault]
    path = ROOT / file

    if args.restore:
        replace_once(path, new, old)
        print(f"restored {args.fault} in {file}")
    else:
        replace_once(path, old, new)
        print(f"injected {args.fault} into {file}")


if __name__ == "__main__":
    main()
