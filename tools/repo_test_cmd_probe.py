#!/usr/bin/env python3
"""repo-test-cmd-probe — find repos with tests but no documented test command.

Small, stdlib-only Greenhouse probe. It reads local repo directories, detects a
likely test suite, scans README-like docs for a one-command test invocation, and
prints findings plus a draft README patch. It never mutates target repos.
"""
from __future__ import annotations

import argparse
import dataclasses
import difflib
import os
import pathlib
import re
import sys
import tempfile
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

DEFAULT_TARGET = pathlib.Path(os.environ.get("GREENHOUSE_REPO_TARGET", ".")).expanduser()
README_NAMES = ("README.md", "README.rst", "README.txt", "readme.md")
TEST_BASENAME_RE = re.compile(r"^(test_[A-Za-z0-9_\-]+|[A-Za-z0-9_\-]+_test)\.py$")
TEST_DIR_NAMES = {"test", "tests", "spec", "specs", "__tests__"}
MANIFEST_TEST_HINTS = (
    "pyproject.toml",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "package.json",
    "Cargo.toml",
    "go.mod",
)
RUNNER_TOKENS = (
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest"),
    ("pytest",),
    ("tox",),
    ("npm", "test"),
    ("pnpm", "test"),
    ("yarn", "test"),
    ("cargo", "test"),
    ("go", "test"),
    ("make", "test"),
)
PREFIX_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|>\s*)?(?:[$#>]\s*)?")
BARE_WORD_RE = re.compile(r"^[a-z]+$")
INTERIOR_SENTENCE_RE = re.compile(r"[.!?]\s+[A-Za-z]")
LAUNCHD_STDERR = "<key>StandardErrorPath</key>"


@dataclasses.dataclass(frozen=True)
class Finding:
    repo: str
    test_reason: str
    suggested_command: str
    readme: Optional[str]
    draft_patch: str


@dataclasses.dataclass(frozen=True)
class ScanResult:
    repos_seen: int
    repos_with_tests: int
    documented: int
    findings: Tuple[Finding, ...]


def iter_repo_dirs(root: pathlib.Path, limit: Optional[int] = None) -> Iterator[pathlib.Path]:
    """Yield git repo directories under root, shallow and deterministic."""
    root = root.expanduser().resolve()
    yielded = 0
    if (root / ".git").exists():
        yield root
        return
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if (child / ".git").exists():
            yield child
            yielded += 1
            if limit is not None and yielded >= limit:
                return


def _interesting_paths(repo: pathlib.Path) -> Iterator[pathlib.Path]:
    ignored = {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in ignored and not d.startswith(".")]
        base = pathlib.Path(dirpath)
        for name in filenames:
            yield base / name


def detect_test_suite(repo: pathlib.Path) -> Optional[str]:
    """Return a short reason when repo appears to ship tests."""
    for path in _interesting_paths(repo):
        rel = path.relative_to(repo)
        parts = set(rel.parts[:-1])
        if path.name in MANIFEST_TEST_HINTS:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "pytest" in text or "npm test" in text or "go test" in text or "cargo test" in text:
                return f"test manifest hint: {rel.as_posix()}"
        if path.name == "package.json":
            try:
                if '"test"' in path.read_text(encoding="utf-8", errors="replace"):
                    return f"package test script: {rel.as_posix()}"
            except OSError:
                continue
        if path.suffix == ".py" and (TEST_BASENAME_RE.match(path.name) or parts & TEST_DIR_NAMES):
            return f"python test file: {rel.as_posix()}"
        if path.suffix in {".js", ".ts", ".tsx"} and ("__tests__" in parts or path.name.endswith((".test.js", ".spec.js", ".test.ts", ".spec.ts"))):
            return f"node test file: {rel.as_posix()}"
    return None


def readme_path(repo: pathlib.Path) -> Optional[pathlib.Path]:
    for name in README_NAMES:
        path = repo / name
        if path.is_file():
            return path
    return None


def strip_prefix(line: str) -> str:
    return PREFIX_RE.sub("", line).strip()


def tokenize_command(line: str) -> List[str]:
    cleaned = strip_prefix(line).strip("` ")
    return re.findall(r"[^\s`]+", cleaned)


def runner_span(tokens: Sequence[str]) -> Optional[Tuple[int, int]]:
    lower = [t.lower() for t in tokens]
    for runner in sorted(RUNNER_TOKENS, key=len, reverse=True):
        size = len(runner)
        if tuple(lower[:size]) == runner:
            return (0, size)
    return None


def _is_bare_english_arg(token: str) -> bool:
    """Binding prose heuristic: lowercase words only; flags/paths/punct are args."""
    if token.startswith("-"):
        return False
    if any(ch in token for ch in "/.=_:~{}[]()$*@"):
        return False
    return bool(BARE_WORD_RE.fullmatch(token))


def is_prose_structured_command(line: str) -> bool:
    """Reject prose regardless of fence context.

    A line is prose after prefix stripping and runner extraction when either:
    * it has >=2 additional bare lowercase-English words after the runner token;
    * or it has an interior sentence terminal (. / ! / ?) followed by another word.

    A trailing period alone is not prose. This keeps legitimate commands such as
    `pytest .` and `pytest -q` documented while rejecting `pytest is our runner`.
    """
    stripped = strip_prefix(line).strip("` ")
    interior_sentence = INTERIOR_SENTENCE_RE.search(stripped)
    if interior_sentence is not None:
        first_sentence = stripped[: interior_sentence.start() + 1].rstrip(".!?")
        if runner_span(tokenize_command(first_sentence)) is not None:
            return True
    tokens = tokenize_command(stripped)
    span = runner_span(tokens)
    if span is None:
        return False
    after = tokens[span[1]:]
    bare_words = [_tok for _tok in after if _is_bare_english_arg(_tok)]
    return len(bare_words) >= 2 or interior_sentence is not None


def is_documented_test_command_line(line: str) -> bool:
    tokens = tokenize_command(line)
    if runner_span(tokens) is None:
        return False
    return not is_prose_structured_command(line)


def documented_test_command(readme: pathlib.Path) -> Optional[str]:
    try:
        lines = readme.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    in_fence = False
    for line in lines:
        if line.lstrip().startswith("```") or line.lstrip().startswith("~~~"):
            in_fence = not in_fence
            continue
        # Fenced lines are candidates unless the prose guard rejects them; the
        # same prose rule also applies outside fences to bullets and indents.
        if is_documented_test_command_line(line):
            return strip_prefix(line).strip("` ")
    return None


def infer_command(repo: pathlib.Path) -> str:
    names = {p.name for p in repo.iterdir() if p.is_file()}
    if "pytest.ini" in names or "pyproject.toml" in names or (repo / "tests").exists():
        return "pytest -q"
    if "package.json" in names:
        return "npm test"
    if "Cargo.toml" in names:
        return "cargo test"
    if "go.mod" in names:
        return "go test ./..."
    return "pytest -q"


def draft_readme_patch(repo: pathlib.Path, command: str) -> str:
    path = readme_path(repo)
    if path is None:
        before = ""
        after = f"# {repo.name}\n\n## Test\n```sh\n{command}\n```\n"
        old = []
        new = after.splitlines(keepends=True)
        filename = "README.md"
    else:
        before = path.read_text(encoding="utf-8", errors="replace")
        addition = f"\n## Test\n```sh\n{command}\n```\n"
        old = before.splitlines(keepends=True)
        new = (before.rstrip() + addition).splitlines(keepends=True)
        filename = path.name
    return "".join(difflib.unified_diff(old, new, fromfile=f"a/{filename}", tofile=f"b/{filename}"))


def scan(root: pathlib.Path, limit: Optional[int] = None) -> ScanResult:
    findings: List[Finding] = []
    repos_seen = 0
    repos_with_tests = 0
    documented = 0
    for repo in iter_repo_dirs(root, limit=limit):
        repos_seen += 1
        reason = detect_test_suite(repo)
        if not reason:
            continue
        repos_with_tests += 1
        rp = readme_path(repo)
        doc = documented_test_command(rp) if rp is not None else None
        if doc:
            documented += 1
            continue
        command = infer_command(repo)
        findings.append(
            Finding(
                repo=str(repo),
                test_reason=reason,
                suggested_command=command,
                readme=str(rp) if rp else None,
                draft_patch=draft_readme_patch(repo, command),
            )
        )
    return ScanResult(repos_seen, repos_with_tests, documented, tuple(findings))


def format_result(result: ScanResult) -> str:
    lines = [
        "repo-test-cmd-probe summary",
        f"repos_seen={result.repos_seen} repos_with_tests={result.repos_with_tests} documented={result.documented} findings={len(result.findings)}",
    ]
    for finding in result.findings:
        lines.extend(
            [
                "",
                f"HIT {finding.repo}",
                f"reason: {finding.test_reason}",
                f"suggested_command: {finding.suggested_command}",
                f"readme: {finding.readme or 'MISSING'}",
                "draft_patch:",
                finding.draft_patch.rstrip(),
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_selfcheck_fixture(root: pathlib.Path) -> None:
    good = root / "documented"
    hit = root / "missing-doc"
    prose = root / "fenced-prose"
    for repo in (good, hit, prose):
        (repo / ".git").mkdir(parents=True)
        (repo / "tests").mkdir()
        _write(repo / "tests" / "test_sample.py", "def test_ok():\n    assert True\n")
    _write(good / "README.md", "# documented\n\n```sh\npytest .\npytest -q\n```\n")
    _write(hit / "README.md", "# missing\n\nNo command yet.\n")
    _write(prose / "README.md", "# prose\n\n```\npytest is our runner\n```\n")


def run_selfcheck() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        build_selfcheck_fixture(root)
        result = scan(root)
        repos = {pathlib.Path(f.repo).name: f for f in result.findings}
        ok = (
            result.repos_seen == 3
            and result.repos_with_tests == 3
            and result.documented == 1
            and set(repos) == {"missing-doc", "fenced-prose"}
            and documented_test_command(root / "documented" / "README.md") == "pytest ."
            and is_documented_test_command_line("pytest -q")
            and is_documented_test_command_line("pytest .")
            and is_prose_structured_command("pytest is our runner")
        )
    if ok:
        print("SELFCHECK PASS repo-test-cmd-probe fixture ok")
        return 0
    print("SELFCHECK FAIL repo-test-cmd-probe fixture mismatch", file=sys.stderr)
    return 1


def check_target(path: pathlib.Path, limit: Optional[int] = None) -> int:
    if not path.exists():
        print(f"LIVENESS FAILURE: target does not exist: {path}", file=sys.stderr)
        return 2
    if not path.is_dir():
        print(f"LIVENESS FAILURE: target is not a directory: {path}", file=sys.stderr)
        return 2
    repos = list(iter_repo_dirs(path, limit=limit))
    if not repos:
        print(f"LIVENESS FAILURE: no git repos found under target: {path}", file=sys.stderr)
        return 2
    print(f"LIVENESS PASS: found {len(repos)} repo(s) under {path}")
    return 0


def probe_text() -> str:
    return "\n".join(
        [
            "repo-test-cmd-probe launchd requirements:",
            "- run --check-target before the nightly scan",
            "- wire stderr to a durable log path",
            LAUNCHD_STDERR,
            "<string>~/.hermes/greenhouse/logs/repo-test-cmd-probe.err</string>",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=pathlib.Path, default=DEFAULT_TARGET)
    parser.add_argument("--limit", type=int, default=None, help="maximum repos to scan")
    parser.add_argument("--selfcheck", action="store_true", help="offline deploy health probe")
    parser.add_argument("--check-target", action="store_true", help="real-target liveness check")
    parser.add_argument("--probe", action="store_true", help="print launchd probe requirements and exit")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.selfcheck:
        return run_selfcheck()
    if args.check_target:
        return check_target(args.target, limit=args.limit)
    if args.probe:
        print(probe_text())
        return 0
    result = scan(args.target, limit=args.limit)
    print(format_result(result), end="")
    return 1 if result.findings else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
