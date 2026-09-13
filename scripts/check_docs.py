#!/usr/bin/env python3
"""Validate the documentation and the branding rules mechanically.

    python scripts/check_docs.py

Checks, over README.md and docs/*.md (plus the component READMEs):

  * every relative link resolves to a file that exists
  * every backticked repository path exists, or is a path the roadmap has not built yet
    (PLANNED_PATHS below — a planned path that starts existing must be removed from that list)
  * every NILO_* environment variable mentioned is one the code actually reads
  * no Chinese *prose*; Chinese quoted as data inside `backticks` is allowed, because the
    pages have to name legacy config values and wake-word phrases
  * no legacy identifiers (xiaozhi-server, XIAOZHI_, ...) outside the pages that
    document provenance and the migration

and, over the whole tree, that every remaining "xiaozhi" occurrence sits in a file
that is allowed to contain one (docs/branding.md lists the categories).

Exit code 0 when clean, 1 otherwise.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "main/nilo-server"

# Pages whose subject is the inherited project: they may name it and describe removed paths.
PROVENANCE_PAGES = {"migration.md", "upstream.md", "branding.md"}

ENV_VARS = {"NILO_CONFIG", "NILO_SERVER_HOST", "NILO_SERVER_PORT", "NILO_HTTP_PORT", "NILO_LOG_LEVEL"}

# Paths the documentation names as *planned* (docs/robot-roadmap.md). They must not exist yet:
# once one does, delete it here so the normal "this path exists" rule takes over.
PLANNED_PATHS = {
    "robot/.ruff.toml",
    "robot/actions",
    "robot/agent",
    "robot/animation",
    "robot/api",
    "robot/behavior",
    "robot/devices",
    "robot/events",
    "robot/memory",
    "robot/personality",
    "robot/safety",
    "robot/simulator",
    "robot/state",
    "robot/vision",
    "robot/tests/fixtures",
}

# Files allowed to contain "xiaozhi". The legacy protocol was removed, so the only legitimate
# mentions left are: the licence, the upstream sync script, the provenance and migration docs,
# and the regression tests that assert the retired routes stay retired.
LEGACY_ALLOWED = {
    "LICENSE",
    "README.md",  # the required attribution sentence names the upstream project
    "docs/branding.md",
    "docs/migration.md",
    "docs/upstream.md",
    "scripts/check_docs.py",
    "scripts/smoke_check.py",
    "scripts/sync-upstream.sh",
    "main/nilo-server/tests/config/test_nilo_config.py",
    "main/nilo-server/tests/test_compose.py",
    "main/nilo-server/tests/core/test_http_routes.py",
    "main/nilo-server/tests/core/test_ws_path_gate.py",
    "main/nilo-server/tests/robot/test_protocol.py",
}

CJK = re.compile(r"[一-鿿]")
LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)]*)?\)")
# A backticked repository path: contains a "/" or is one of the known top-level files.
PATHISH = re.compile(
    r"`((?:main|docs|scripts|core|config|robot|plugins|plugins_func|tests|\.github)/[A-Za-z0-9_./\-]*"
    r"|Dockerfile[A-Za-z0-9._-]*|Makefile|LICENSE|README\.md)`"
)
INLINE_CODE = re.compile(r"`[^`]*`")
ENV = re.compile(r"\bNILO_[A-Z_]+\b")
LEGACY = re.compile(r"xiaozhi-server|XIAOZHI_|xiaozhi_|小智")


def docs() -> list[Path]:
    pages = [ROOT / "README.md", SERVER / "CLAUDE.md", SERVER / "plugins/README.md"]
    pages += sorted((ROOT / "docs").glob("*.md"))
    return [p for p in pages if p.exists()]


def check_pages() -> list[str]:
    problems = []
    for doc in docs():
        rel = doc.relative_to(ROOT)
        text = doc.read_text(encoding="utf-8")
        provenance = doc.name in PROVENANCE_PAGES

        for m in LINK.finditer(text):
            target = m.group(1)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (doc.parent / target).exists():
                problems.append(f"{rel}: broken link -> {target}")

        if not provenance:
            for m in PATHISH.finditer(text):
                p = m.group(1).rstrip("/")
                if p in PLANNED_PATHS:
                    if (SERVER / p).exists():
                        problems.append(
                            f"{rel}: `{p}` now exists - remove it from PLANNED_PATHS in scripts/check_docs.py"
                        )
                    continue
                if not any((base / p).exists() for base in (ROOT, SERVER, doc.parent)):
                    problems.append(f"{rel}: path does not exist `{p}`")

        for m in ENV.finditer(text):
            if m.group(0) not in ENV_VARS:
                problems.append(f"{rel}: unknown environment variable {m.group(0)}")

        in_fence = False
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            # Chinese inside a fence or `backticks` is quoted data (a legacy value, a search pattern).
            if not in_fence and CJK.search(INLINE_CODE.sub("``", line)):
                problems.append(f"{rel}:{i}: Chinese prose in first-party documentation")
            if not provenance and LEGACY.search(line):
                problems.append(f"{rel}:{i}: legacy identifier: {line.strip()[:70]}")
    return problems


def check_tree() -> list[str]:
    """Every tracked file containing 'xiaozhi' must be on the allow-list."""
    import subprocess

    problems = []
    tracked = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    for rel in tracked:
        path = ROOT / rel
        if rel in LEGACY_ALLOWED or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary asset
        if re.search(r"xiaozhi", text, re.I):
            problems.append(f"{rel}: contains 'xiaozhi' but is not in the allow-list in scripts/check_docs.py")
    return problems


def main() -> int:
    problems = check_pages() + check_tree()
    for p in problems:
        print(p)
    print(f"checked {len(docs())} pages; problems: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
