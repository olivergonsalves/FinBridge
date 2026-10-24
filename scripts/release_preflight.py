#!/usr/bin/env python3
"""Conservative offline pre-publication check for standalone FinBridge.

No network requests and no credentials printed. It scans publishable source
files, ignores local .env content, and reports only category/path/line for
possible hardcoded secrets. Review flagged lines locally.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist"}
SUFFIXES = {".py", ".js", ".ts", ".html", ".sh", ".json", ".toml", ".yml", ".yaml", ".md"}
SECRET_NAMES = re.compile(r"(?i)\b(?:plaid_secret|plaid_client_id|client_secret|api_key|access_token|private_key)\s*[:=]\s*(['\"])(.*?)\1")
TOKEN_LIKE = re.compile(r"(?i)\b(?:access-(?:sandbox|development|production)-|secret-(?:sandbox|development|production)-|sk_(?:live|test)_)[A-Za-z0-9_-]{8,}")
DEMO_MARKERS = ("your_", "example", "placeholder", "dummy", "test", "mock", "sample", "fake", "<", "${", "{")


def run_git(*args: str):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)


def scan_source():
    matches = []
    count = 0
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(x in SKIP_DIRS for x in path.relative_to(ROOT).parts):
            continue
        if path.name.startswith(".env") or path.suffix.lower() not in SUFFIXES:
            continue
        if path.stat().st_size > 2_000_000:
            continue
        count += 1
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, line in enumerate(lines, 1):
            assignment = SECRET_NAMES.search(line)
            suspicious_assignment = assignment and len(assignment.group(2)) > 6 and not any(
                marker in assignment.group(2).lower() for marker in DEMO_MARKERS
            )
            # The Milestone 9 test fixture intentionally uses one known,
            # nonfunctional token. Suppress ONLY that exact value in that
            # exact test file; continue reporting all other token patterns.
            rel_path = path.relative_to(ROOT).as_posix()
            token_hits = [
                match.group(0)
                for match in TOKEN_LIKE.finditer(line)
                if not (
                    rel_path == "tests/test_finbridge_diagnostic_endpoint.py"
                    and match.group(0) == ("access-" + "sandbox-test-only")
                )
            ]
            if token_hits:
                matches.append((path.relative_to(ROOT), line_no, "token-like string"))
            elif suspicious_assignment:
                matches.append((path.relative_to(ROOT), line_no, "possible hardcoded credential"))
    return count, matches


def main():
    print("FinBridge pre-publication review (offline; prints no secret values)")
    print(f"Project root: {ROOT}")
    issues = 0
    for name in (".gitignore", ".env.example", "requirements.txt", "README.md"):
        if not (ROOT / name).is_file():
            print(f"[ACTION] Missing {name}")
            issues += 1

    if (ROOT / ".env").exists():
        print("[INFO] Local .env exists. It must remain untracked and unpublished.")
    if (ROOT / "finbridge.db").exists():
        print("[INFO] Local finbridge.db exists. It must remain unpublished.")

    files, warnings = scan_source()
    print(f"[INFO] Checked {files} candidate source/documentation files.")
    for path, line, kind in warnings:
        print(f"[REVIEW] {path}:{line} — {kind}; inspect locally, do not paste secrets")
    if not warnings:
        print("[OK] No high-confidence hardcoded secret patterns detected (not a guarantee).")

    if (ROOT / ".git").exists():
        inside = run_git("rev-parse", "--is-inside-work-tree")
        if inside.returncode == 0:
            tracked = run_git("ls-files", "-z")
            tracked_names = [x for x in tracked.stdout.split("\0") if x]
            problem_names = [name for name in tracked_names if (
                Path(name).name == ".env" or Path(name).name.startswith(".env.") and Path(name).name != ".env.example"
                or Path(name).suffix.lower() in {".db", ".sqlite", ".sqlite3"}
                or any(part.startswith(".venv") or part in {"node_modules", "__pycache__"} for part in Path(name).parts)
            )]
            for name in problem_names:
                print(f"[ACTION] Tracked sensitive/local path: {name}")
                issues += 1
            staged = run_git("diff", "--cached", "--name-only")
            if staged.stdout.strip():
                print("[INFO] Staged paths present. Review with: git diff --cached --name-only")
            if not problem_names:
                print("[OK] No obvious tracked local-secret/database/environment paths.")
    else:
        print("[INFO] No .git directory yet; repeat after git init and before pushing.")

    if warnings:
        issues += len(warnings)
    if issues:
        print(f"[REVIEW REQUIRED] {issues} finding(s). Resolve or review before publication.")
        return 1
    print("[PASS] Basic preflight checks passed. Manual review still required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
