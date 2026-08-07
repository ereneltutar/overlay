#!/usr/bin/env python3
"""Validates every .github/workflows/*.yml file parses as YAML.

Exists because this project has hit the same failure mode three times:
a multi-line block embedded in a `run: |` step silently de-indents past
the block scalar's level, and GitHub only discovers the workflow is
broken when it tries to run it (sometimes as a "startup_failure" with
zero jobs, sometimes with a confusing parser error). Catching that in
CI, before merge, is cheaper than debugging it live in the Actions tab.
"""

import sys
from pathlib import Path

import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def main():
    ok = True
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or "jobs" not in doc:
                print(f"{path.name}: INVALID (parsed but missing a top-level 'jobs' key)")
                ok = False
            else:
                print(f"{path.name}: OK")
        except yaml.YAMLError as exc:
            print(f"{path.name}: INVALID YAML - {exc}")
            ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
